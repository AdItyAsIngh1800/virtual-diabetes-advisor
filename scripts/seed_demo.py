"""Seed ~20 synthetic demo patients with 30 days of glucose readings (R1-R3 demo).

Usage::

    uv run python -m scripts.seed_demo                      # fake numbers only
    uv run python -m scripts.seed_demo --my-phone +9198...  # one patient -> your WhatsApp
    uv run python -m app.jobs.batch_update                  # then run the batch job

Safe to re-run: every ``users.is_demo`` row (and, by cascade, its profile, readings,
thresholds, risk assessments and advisories) is deleted first, in the same transaction
as the new inserts. Real patients are never touched.

The cohort covers people with and without diabetes, all three risk tiers, healthy and
complex older adults (ADA 2026 Table 13.2 via the threshold engine's proxy), and these
scenarios: drift into the pre-breach band, a significant rising trend, readings below
54 mg/dL, and steady near-limit readings in every band state. Values are generated from
a fixed seed, so every run produces the same cohort.

Phone numbers are fake: ``+1 555 0100`` to ``+1 555 0199`` (the 555-01xx block is
reserved for fiction). ``--my-phone`` swaps one patient's number for a real one so the
Phase 5 WhatsApp delivery can be tested end to end.
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Final, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.core.clinical_limits import DiabetesStatus
from app.core.config import get_settings
from app.core.exceptions import DiabetesAdvisorError
from app.core.logging import configure_logging, mask_phone
from app.db.models import (
    Advisory,
    ConversationTurn,
    Language,
    Profile,
    Reading,
    ReadingContext,
    RiskTier,
    User,
)
from app.ml.predict import RiskAssessment
from app.ml.schema import RiskProfile, age_to_bucket

logger = logging.getLogger(__name__)

# Fake numbers: +1 555 0100 ... +1 555 0199 (NANP fictional 555-01xx block).
DEMO_PHONE_PREFIX: Final = "+1555"
DEMO_PHONE_FIRST: Final = 100
DEMO_PHONE_LAST: Final = 199
# Same rule as the profiles.phone_e164 CHECK (0001_core_schema.sql).
_E164: Final = re.compile(r"^\+[1-9][0-9]{6,14}$")

DAYS: Final = 30
# Demo patients live in India (IST); readings are stamped at local clock times.
DEMO_TZ: Final = ZoneInfo("Asia/Kolkata")
# Fasting before breakfast; post-meal 1-2 h after a 12:30 lunch, inside ADA's 1-2 h
# postprandial timing (clinical_limits.POST_MEAL_HIGH_DM_MG_DL).
_FASTING_AT: Final = time(7, 0)
_POST_MEAL_AT: Final = time(14, 0)
_FASTING_JITTER_MIN: Final = 20
_POST_MEAL_JITTER_MIN: Final = 30
# readings.value_mg_dl sanity CHECK is 10-1000; meters read ~20-600.
_MIN_VALUE: Final = 20.0
_MAX_VALUE: Final = 600.0
# Profile search: attempts per patient to find BRFSS answers the model scores into the
# target tier. Each attempt is one predict_proba call (~ms).
_TIER_SEARCH_ATTEMPTS: Final = 400
DEFAULT_RNG_SEED: Final = 20261115
# Share of synthetic profiles answering yes to heavy drinking, whatever the tier; a
# plausibility choice for demo data, not a statistic.
_HEAVY_DRINKING_RATE: Final = 0.05

ConditionName = str
_CONDITIONS: Final[tuple[ConditionName, ...]] = (
    "high_bp",
    "high_chol",
    "heart_disease_or_attack",
    "stroke",
    "diff_walk",
)


class SeedError(DiabetesAdvisorError):
    """Raised when demo data cannot be (re)seeded safely."""


class RiskScorer(Protocol):
    """The part of :class:`app.ml.predict.RiskModel` the seed needs."""

    def assess(self, profile: RiskProfile) -> RiskAssessment:
        """Score one profile.

        Args:
            profile: Validated BRFSS profile.

        Returns:
            The assessment.
        """
        ...


@dataclass(frozen=True)
class Series:
    """A linear glucose course with Gaussian noise.

    Attributes:
        start: Mean on day 0 (mg/dL).
        end: Mean on the last day (mg/dL).
        sd: Day-to-day standard deviation.
    """

    start: float
    end: float
    sd: float

    def mean_on(self, day: int) -> float:
        """Mean value on ``day`` (0 = first day).

        Args:
            day: Day index, 0 to ``DAYS - 1``.

        Returns:
            Linearly interpolated mean.
        """
        return self.start + (self.end - self.start) * day / (DAYS - 1)


def steady(mean: float, sd: float) -> Series:
    """Build a flat :class:`Series`.

    Args:
        mean: Constant mean.
        sd: Noise.

    Returns:
        The series.
    """
    return Series(mean, mean, sd)


@dataclass(frozen=True)
class PatientSpec:
    """One synthetic patient and the scenario their readings demonstrate.

    Attributes:
        key: Stable identifier (also seeds the patient's random generator).
        name: Display name.
        age: Age in years.
        sex: BRFSS coding, 0 = female, 1 = male.
        diabetes_status: Selects the cited limit set.
        language: Profile language.
        target_tier: Risk tier the BRFSS answers are searched for.
        scenario: What the readings show (printed in the report).
        fasting: Fasting reading course.
        post_meal: Post-meal reading course.
        conditions: Fixed complex-health proxy answers (older adults); unset ones
            are chosen by the tier search.
        events: ``(day, context, value)`` overrides, e.g. a hypoglycaemic episode.
        my_phone_slot: The patient ``--my-phone`` attaches to.
    """

    key: str
    name: str
    age: int
    sex: int
    diabetes_status: DiabetesStatus
    language: Language
    target_tier: RiskTier
    scenario: str
    fasting: Series
    post_meal: Series
    conditions: Mapping[ConditionName, bool] = field(default_factory=dict)
    events: tuple[tuple[int, ReadingContext, float], ...] = ()
    my_phone_slot: bool = False


_HEALTHY_OLDER: Final[Mapping[ConditionName, bool]] = dict.fromkeys(_CONDITIONS, False)
# Thresholds quoted below are the engine's defaults (5% band): diabetes fasting 130,
# post-meal 180, low 70; complex older adults fasting 140, low 75; no diabetes
# fasting 112/106/100 and post-meal 160/150/140 for risk tier low/moderate/high.
DEMO_PATIENTS: Final[tuple[PatientSpec, ...]] = (
    # --- People with diabetes ------------------------------------------------
    PatientSpec(
        "meera", "Meera Iyer", 58, 0, "diabetes", "en", "moderate",
        "fasting drifts up into the pre-breach band (~121-127 vs limit 130)",
        Series(112, 124, 2.0), steady(150, 8), my_phone_slot=True,
    ),
    PatientSpec(
        "rohan", "Rohan Mehta", 62, 1, "diabetes", "hi", "high",
        "significant rising trend; trend revision tightens both upper limits",
        Series(115, 160, 4.0), Series(160, 215, 6.0),
    ),
    PatientSpec(
        "priya", "Priya Nair", 35, 0, "diabetes", "ta", "low",
        "two fasting readings below 54 mg/dL (Level 2 hypoglycaemia, escalated)",
        steady(100, 6), steady(140, 10),
        events=((18, "fasting", 52.0), (25, "fasting", 48.5)),
    ),
    PatientSpec(
        "kavya", "Kavya Reddy", 41, 0, "diabetes", "en", "low",
        "fasting slides toward the low limit (pre-breach low, some breaches)",
        Series(76, 72, 1.2), steady(135, 8),
    ),
    PatientSpec(
        "imran", "Imran Khan", 50, 1, "diabetes", "en", "high",
        "post-meal above 180 (breach high), fasting near 130",
        steady(125, 4), steady(192, 8),
    ),
    PatientSpec(
        "nandini", "Nandini Rao", 52, 0, "diabetes", "en", "moderate",
        "post-meal steady in the pre-breach band (171-180)",
        steady(110, 5), steady(173, 2.5),
    ),
    PatientSpec(
        "aarav", "Aarav Sharma", 45, 1, "diabetes", "en", "moderate",
        "well controlled", steady(105, 5), steady(145, 10),
    ),
    PatientSpec(
        "rahul", "Rahul Verma", 29, 1, "diabetes", "hi", "low",
        "well controlled", steady(100, 6), steady(140, 12),
    ),
    PatientSpec(
        "lakshmi", "Lakshmi Pillai", 68, 0, "diabetes", "ta", "moderate",
        "healthy older adult (no proxy conditions): standard limits",
        steady(112, 6), steady(155, 10), conditions=_HEALTHY_OLDER,
    ),
    PatientSpec(
        "mohan", "Mohan Das", 83, 1, "diabetes", "en", "high",
        "healthy older adult (1 condition): standard limits, occasional pre-breach",
        steady(118, 3), steady(160, 8), conditions={**_HEALTHY_OLDER, "high_bp": True},
    ),
    PatientSpec(
        "gopal", "Gopal Krishnan", 74, 1, "diabetes", "en", "high",
        "complex older adult: fasting ~134 is pre-breach of the relaxed 140 limit",
        steady(134, 2.5), steady(170, 8),
        conditions={
            "high_bp": True, "high_chol": True, "heart_disease_or_attack": True,
            "stroke": False, "diff_walk": True,
        },
    ),
    PatientSpec(
        "saroja", "Saroja Menon", 79, 0, "diabetes", "ta", "high",
        "complex older adult: fasting ~79 is pre-breach of the raised 75 low limit",
        steady(79, 1.5), steady(150, 10),
        conditions={
            "high_bp": True, "high_chol": False, "heart_disease_or_attack": False,
            "stroke": True, "diff_walk": True,
        },
    ),
    # --- People without diabetes ---------------------------------------------
    PatientSpec(
        "vikram", "Vikram Singh", 55, 1, "no_diabetes", "en", "high",
        "high risk tier tightens fasting limit to 100; readings ~96 are pre-breach",
        steady(96.5, 1.5), steady(125, 6),
    ),
    PatientSpec(
        "arjun", "Arjun Patel", 60, 1, "no_diabetes", "hi", "high",
        "fasting rises 92 -> 108 past the tightened 100 limit",
        Series(92, 108, 2.0), steady(130, 5),
    ),
    PatientSpec(
        "suresh", "Suresh Babu", 47, 1, "no_diabetes", "ta", "moderate",
        "post-meal ~145 in the pre-breach band of the 150 limit",
        steady(95, 3), steady(145, 2.5),
    ),
    PatientSpec(
        "deepa", "Deepa Joshi", 66, 0, "no_diabetes", "en", "moderate",
        "mostly normal readings", steady(92, 3), steady(128, 8),
    ),
    PatientSpec(
        "ananya", "Ananya Gupta", 28, 0, "no_diabetes", "en", "low",
        "normal readings", steady(86, 4), steady(115, 8),
    ),
    PatientSpec(
        "fatima", "Fatima Sheikh", 33, 0, "no_diabetes", "hi", "low",
        "normal readings", steady(88, 4), steady(118, 8),
    ),
    PatientSpec(
        "sneha", "Sneha Kulkarni", 24, 0, "no_diabetes", "en", "low",
        "normal, with one fasting reading of 66 (breach low)",
        steady(84, 3), steady(110, 7), events=((22, "fasting", 66.0),),
    ),
    PatientSpec(
        "joseph", "Joseph Thomas", 71, 1, "no_diabetes", "en", "high",
        "high risk tier (tighter limits); readings mostly normal",
        steady(89, 2.5), steady(124, 6),
    ),
)  # fmt: skip


# ---------------------------------------------------------------------------
# Generation (pure)
# ---------------------------------------------------------------------------


class GeneratedReading(BaseModel):
    """One synthetic reading.

    Attributes:
        context: ``fasting`` or ``post_meal``.
        value_mg_dl: Value rounded to the stored precision.
        measured_at: Timezone-aware measurement time.
    """

    model_config = ConfigDict(frozen=True)

    context: ReadingContext
    value_mg_dl: Decimal = Field(gt=0)
    measured_at: datetime


def demo_phone(index: int) -> str:
    """Return the fake E.164 number of the ``index``-th demo patient.

    Args:
        index: 0-based patient index.

    Returns:
        ``+15550100`` for index 0, up to ``+15550199``.

    Raises:
        SeedError: If the index is outside the 100-number fake block.
    """
    number = DEMO_PHONE_FIRST + index
    if number > DEMO_PHONE_LAST:
        raise SeedError(f"only {DEMO_PHONE_LAST - DEMO_PHONE_FIRST + 1} fake numbers exist")
    return f"{DEMO_PHONE_PREFIX}{number:04d}"


def _patient_rng(rng_seed: int, key: str, purpose: str) -> random.Random:
    """Return a generator seeded per patient and purpose (stable across runs).

    Args:
        rng_seed: Global seed.
        key: Patient key.
        purpose: ``"profile"`` or ``"readings"``.

    Returns:
        A seeded :class:`random.Random` (not for security: synthetic data only).
    """
    return random.Random(f"{rng_seed}:{key}:{purpose}")  # noqa: S311


def generate_readings(
    spec: PatientSpec, *, now: datetime, rng_seed: int = DEFAULT_RNG_SEED
) -> list[GeneratedReading]:
    """Generate a fasting and a post-meal reading per day for the last 30 days.

    Day ``DAYS - 1`` is today in IST; readings scheduled after ``now`` are left out.

    Args:
        spec: The patient.
        now: Current time (timezone-aware).
        rng_seed: Global seed.

    Returns:
        Readings, oldest first.
    """
    rng = _patient_rng(rng_seed, spec.key, "readings")
    events = {(day, context): value for day, context, value in spec.events}
    today = now.astimezone(DEMO_TZ).date()
    out: list[GeneratedReading] = []
    for day in range(DAYS):
        date = today - timedelta(days=DAYS - 1 - day)
        for context, series, at, jitter in (
            ("fasting", spec.fasting, _FASTING_AT, _FASTING_JITTER_MIN),
            ("post_meal", spec.post_meal, _POST_MEAL_AT, _POST_MEAL_JITTER_MIN),
        ):
            ctx: ReadingContext = "fasting" if context == "fasting" else "post_meal"
            noise = rng.gauss(0, series.sd)
            offset = timedelta(minutes=rng.randint(-jitter, jitter))
            measured = datetime.combine(date, at, tzinfo=DEMO_TZ) + offset
            value = events.get((day, ctx), series.mean_on(day) + noise)
            if measured > now:
                continue
            clipped = min(max(value, _MIN_VALUE), _MAX_VALUE)
            out.append(
                GeneratedReading(
                    context=ctx,
                    value_mg_dl=Decimal(str(round(clipped, 1))),
                    measured_at=measured,
                )
            )
    return out


def _candidate_profile(spec: PatientSpec, rng: random.Random, tier: RiskTier) -> RiskProfile:
    """Draw BRFSS answers typical of ``tier`` (fixed spec conditions win).

    Args:
        spec: The patient.
        rng: Profile generator.
        tier: Tier whose typical answers to draw.

    Returns:
        A validated candidate profile.
    """
    unhealthy = {"low": 0.1, "moderate": 0.45, "high": 0.85}[tier]
    bmi_range = {"low": (20.0, 26.0), "moderate": (26.0, 32.0), "high": (31.0, 42.0)}[tier]
    health = {"low": (1, 2), "moderate": (2, 3), "high": (3, 5)}[tier]
    income = {"low": (6, 8), "moderate": (4, 7), "high": (1, 4)}[tier]
    answers: dict[str, object] = {
        name: spec.conditions.get(name, rng.random() < unhealthy) for name in _CONDITIONS
    }
    answers.update(
        chol_check=True,
        bmi=round(rng.uniform(*bmi_range), 1),
        smoker=rng.random() < unhealthy / 2,
        phys_activity=rng.random() > unhealthy,
        fruits=rng.random() > unhealthy / 2,
        veggies=rng.random() > unhealthy / 3,
        hvy_alcohol_consump=rng.random() < _HEAVY_DRINKING_RATE,
        any_healthcare=True,
        no_doc_bc_cost=rng.random() < unhealthy / 4,
        gen_hlth=rng.randint(*health),
        ment_hlth=rng.randint(0, 3 if tier == "low" else 10),
        phys_hlth=rng.randint(0, 2 if tier == "low" else 15),
        sex=spec.sex,
        age=age_to_bucket(spec.age),
        education=rng.randint(4, 6) if tier != "high" else rng.randint(2, 5),
        income=rng.randint(*income),
    )
    return RiskProfile.model_validate(answers)


def build_risk_profile(
    spec: PatientSpec, scorer: RiskScorer | None, *, rng_seed: int = DEFAULT_RNG_SEED
) -> tuple[RiskProfile, RiskAssessment | None]:
    """Find BRFSS answers that the risk model scores into the spec's target tier.

    Samples answers typical of the tier and keeps the first the model agrees with.
    If none of the attempts lands in the tier, the last candidate is kept and the
    mismatch is logged (the report shows the actual tier).

    Args:
        spec: The patient.
        scorer: Risk model, or ``None`` (first candidate kept, tier unknown).
        rng_seed: Global seed.

    Returns:
        ``(profile, assessment or None without a model)``.
    """
    rng = _patient_rng(rng_seed, spec.key, "profile")
    candidate = _candidate_profile(spec, rng, spec.target_tier)
    if scorer is None:
        return candidate, None
    assessment = scorer.assess(candidate)
    for _ in range(_TIER_SEARCH_ATTEMPTS - 1):
        if assessment.tier == spec.target_tier:
            break
        candidate = _candidate_profile(spec, rng, spec.target_tier)
        assessment = scorer.assess(candidate)
    if assessment.tier != spec.target_tier:
        logger.warning(
            "%s: no profile in %d tries scored %s (got %s)",
            spec.key,
            _TIER_SEARCH_ATTEMPTS,
            spec.target_tier,
            assessment.tier,
        )
    return candidate, assessment


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


class SeededPatient(BaseModel):
    """One seeded patient, for the report.

    Attributes:
        key: Spec key.
        user_id: New ``users.id``.
        name: Display name.
        phone_masked: Masked phone number (PII-safe).
        diabetes_status: Limit set.
        age: Age.
        target_tier: Tier searched for.
        model_tier: Tier the model gave (``None`` without a model).
        complex_conditions: Complex-health proxy conditions answered yes.
        readings: Readings inserted.
        scenario: What the readings show.
    """

    key: str
    user_id: uuid.UUID
    name: str
    phone_masked: str
    diabetes_status: DiabetesStatus
    age: int
    target_tier: RiskTier
    model_tier: RiskTier | None
    complex_conditions: int
    readings: int
    scenario: str


class SeedReport(BaseModel):
    """What :func:`seed_demo` did.

    Attributes:
        deleted_users: Previous demo users removed.
        patients: Patients created.
        readings: Readings created.
    """

    deleted_users: int
    patients: list[SeededPatient]
    readings: int


def validate_my_phone(number: str) -> str:
    """Check a real phone number for ``--my-phone``.

    Args:
        number: E.164 number, e.g. ``+919876543210`` (spaces and dashes allowed).

    Returns:
        The number in E.164 without separators.

    Raises:
        SeedError: If it is not E.164 or lies in the fake demo block.
    """
    compact = re.sub(r"[\s\-()]", "", number)
    if not _E164.match(compact):
        raise SeedError("--my-phone must be E.164, e.g. +919876543210")
    if compact.startswith(DEMO_PHONE_PREFIX) and len(compact) == len(demo_phone(0)):
        raise SeedError("--my-phone must be a real number, not one of the fake 555 numbers")
    return compact


def clear_demo_data(session: Session) -> int:
    """Delete every demo user and everything that cascades from them.

    Advisories are deleted first: they reference ``thresholds_history`` with ON DELETE
    RESTRICT, so the cascade from ``users`` could otherwise trip over them.

    Args:
        session: Open session (caller commits).

    Returns:
        Number of demo users deleted.

    Raises:
        SeedError: If a demo user has conversation turns (append-only audit rows,
            R5), which must never be deleted.
    """
    demo_ids = select(User.id).where(User.is_demo).scalar_subquery()
    turns = session.scalar(
        select(func.count())
        .select_from(ConversationTurn)
        .where(ConversationTurn.user_id.in_(demo_ids))
    )
    if turns:
        raise SeedError(
            f"demo users have {turns} conversation turns (append-only audit log); "
            "they cannot be deleted - deactivate them instead of re-seeding"
        )
    session.execute(delete(Advisory).where(Advisory.user_id.in_(demo_ids)))
    result = session.execute(delete(User).where(User.is_demo))
    return int(result.rowcount)  # type: ignore[attr-defined]


def _phone_numbers(my_phone: str | None) -> Iterator[str]:
    """Yield each patient's number in :data:`DEMO_PATIENTS` order.

    Args:
        my_phone: Real number for the ``my_phone_slot`` patient, or ``None``.

    Yields:
        E.164 numbers.
    """
    for index, spec in enumerate(DEMO_PATIENTS):
        yield my_phone if (spec.my_phone_slot and my_phone) else demo_phone(index)


def seed_demo(
    session: Session,
    *,
    now: datetime,
    scorer: RiskScorer | None,
    my_phone: str | None = None,
    patients: Sequence[PatientSpec] = DEMO_PATIENTS,
    rng_seed: int = DEFAULT_RNG_SEED,
) -> SeedReport:
    """Replace all demo data with a fresh cohort (caller commits).

    Args:
        session: Open session.
        now: Current time; readings cover the 30 days up to it.
        scorer: Risk model used to hit each patient's target tier, or ``None``.
        my_phone: Validated real number for the ``my_phone_slot`` patient.
        patients: Cohort to create.
        rng_seed: Global seed.

    Returns:
        What was deleted and created.

    Raises:
        SeedError: If ``my_phone`` belongs to a real (non-demo) patient, or demo
            users cannot be deleted.
    """
    if my_phone is not None:
        owner = session.scalar(
            select(User.is_demo)
            .join(Profile, Profile.user_id == User.id)
            .where(Profile.phone_e164 == my_phone)
        )
        if owner is False:
            raise SeedError("--my-phone already belongs to a real (non-demo) patient")
    deleted = clear_demo_data(session)

    seeded: list[SeededPatient] = []
    reading_rows: list[dict[str, object]] = []
    for spec, phone in zip(patients, _phone_numbers(my_phone), strict=False):
        risk_profile, assessment = build_risk_profile(spec, scorer, rng_seed=rng_seed)
        user = User(id=uuid.uuid4(), role="patient", is_demo=True)
        session.add(user)
        session.flush()
        answers = risk_profile.model_dump(exclude={"age", "sex"})
        session.add(
            Profile(
                user_id=user.id,
                full_name=spec.name,
                phone_e164=phone,
                language=spec.language,
                age_years=spec.age,
                sex=spec.sex,
                diabetes_status=spec.diabetes_status,
                **{**answers, "bmi": Decimal(str(answers["bmi"]))},
            )
        )
        readings = generate_readings(spec, now=now, rng_seed=rng_seed)
        reading_rows.extend(
            {"user_id": user.id, "source": "seed", **r.model_dump()} for r in readings
        )
        seeded.append(
            SeededPatient(
                key=spec.key,
                user_id=user.id,
                name=spec.name,
                phone_masked=mask_phone(phone),
                diabetes_status=spec.diabetes_status,
                age=spec.age,
                target_tier=spec.target_tier,
                model_tier=assessment.tier if assessment is not None else None,
                complex_conditions=sum(getattr(risk_profile, c) for c in _CONDITIONS),
                readings=len(readings),
                scenario=spec.scenario,
            )
        )
    session.flush()
    if reading_rows:
        session.execute(insert(Reading), reading_rows)
    return SeedReport(deleted_users=deleted, patients=seeded, readings=len(reading_rows))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_report(report: SeedReport) -> None:
    """Print a table of the seeded cohort.

    Args:
        report: Result of :func:`seed_demo`.
    """
    lines = [
        f"Deleted {report.deleted_users} previous demo users.",
        f"Seeded {len(report.patients)} demo patients, {report.readings} readings.",
        "",
        f"{'patient':<16} {'phone':<14} {'status':<12} {'age':>3} {'tier(target/model)':<20} "
        "scenario",
    ]
    for p in report.patients:
        tier = f"{p.target_tier}/{p.model_tier or '?'}"
        lines.append(
            f"{p.name:<16} {p.phone_masked:<14} {p.diabetes_status:<12} {p.age:>3} "
            f"{tier:<20} {p.scenario}"
        )
    lines += ["", "Next: uv run python -m app.jobs.batch_update"]
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    """Seed demo data from the command line.

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    # CLI-only imports: loading the engine/model is not needed to import this module.
    from app.db.session import get_session_factory  # noqa: PLC0415
    from app.jobs.batch_update import load_risk_scorer  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument(
        "--my-phone",
        help="attach the first demo patient to this real WhatsApp number (E.164)",
    )
    parser.add_argument("--rng-seed", type=int, default=DEFAULT_RNG_SEED)
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)

    my_phone = validate_my_phone(args.my_phone) if args.my_phone else None
    factory = get_session_factory()
    with factory() as session:
        now = session.scalar(select(func.now()))
        if not isinstance(now, datetime):  # pragma: no cover - now() is never NULL
            raise SeedError("database returned no now()")
        report = seed_demo(
            session, now=now, scorer=load_risk_scorer(), my_phone=my_phone, rng_seed=args.rng_seed
        )
        session.commit()
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
