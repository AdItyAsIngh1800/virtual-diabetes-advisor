"""Seed ~20 synthetic demo patients with 30 days of glucose readings (R1-R3 demo).

Usage::

    uv run python -m scripts.seed_demo                          # no login linked
    uv run python -m scripts.seed_demo --my-email you@example.com  # link your /app login
    uv run python -m scripts.seed_demo --my-email you@example.com \
        --practitioner-email doctor@example.com     # + a demo practitioner login (R4)
    uv run python -m app.jobs.batch_update                      # then run the batch job

Safe to re-run, in one transaction with the new inserts. Real patients are never
touched. Demo users are handled in two ways:

* A demo user **without** conversation turns is deleted (with, by cascade, its
  profile, readings, thresholds, risk assessments and advisories) and created afresh.
* A demo user **with** conversation turns is kept: the turns are an append-only audit
  log (R5) and cannot be deleted. Only its readings, thresholds and advisories are
  replaced from the seed; its id, profile and turns stay. Advisories that a turn
  refers to are kept too (with their reading and threshold), because the turn's
  foreign key must stay valid. ``--my-email`` re-attaches to this same user.

The cohort covers people with and without diabetes, all three risk tiers, healthy and
complex older adults (ADA 2026 Table 13.2 via the threshold engine's proxy), and these
scenarios: drift into the pre-breach band, a significant rising trend, readings below
54 mg/dL, and steady near-limit readings in every band state. Values are generated from
a fixed seed, so every run produces the same cohort.

Demo patients have no phone number and no login. ``--my-email`` links one of them
(Meera, the pre-breach drift scenario) to your own Supabase login, the account you
created at ``/app``. That patient is the only demo patient the notification guard
lets Web Push reach (``app.services.notifications.check_recipient``).

``--practitioner-email`` (R4) links a second existing login as the demo practitioner:
it gets a (non-demo) ``practitioner`` user and ``practitioners`` row, every seeded
patient is assigned to it, and it approves the seeded knowledge base
(``scripts.kb_seed``, ~15 diabetes-basics entries with sources). Without the flag, the
KB is approved by an existing active practitioner, or skipped if there is none.
Re-seeding never replaces an approved entry a practitioner may have edited or
re-approved (it is reported as kept); ``--overwrite-seed`` replaces such entries with
the seed text after printing which ones it retires.
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import uuid
from collections.abc import Collection, Mapping, Sequence
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
from app.core.logging import configure_logging
from app.db.auth_users import find_auth_user_id_by_email
from app.db.chat_repository import assign_patients
from app.db.kb_repository import SEED_PRACTITIONER_NAME
from app.db.models import (
    Advisory,
    ConversationTurn,
    Language,
    Practitioner,
    Profile,
    Reading,
    ReadingContext,
    RiskTier,
    ThresholdHistory,
    User,
)
from app.ml.predict import RiskAssessment
from app.ml.schema import RiskProfile, age_to_bucket
from scripts.kb_seed import KbSeedReport, seed_kb

logger = logging.getLogger(__name__)

# Shape check only (something@domain.tld); Supabase Auth owns real validation.
_EMAIL: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

DAYS: Final = 30
# Demo patients live in India (IST); readings are stamped at local clock times.
DEMO_TZ: Final = ZoneInfo("Asia/Kolkata")
# Fasting before breakfast; post-meal about 1.5 h after a 12:30 lunch. Demo
# scheduling only, not a clinical timing rule.
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
        my_login_slot: The patient ``--my-email`` links to your login (also the
            patient a kept demo user that is already linked belongs to).
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
    my_login_slot: bool = False


_HEALTHY_OLDER: Final[Mapping[ConditionName, bool]] = dict.fromkeys(_CONDITIONS, False)
# Thresholds quoted below are the engine's defaults (5% band): diabetes fasting 130,
# post-meal 180, low 70; complex older adults fasting 140, low 75; no diabetes
# fasting 112 and post-meal 160 (the risk tier does not change any limit).
DEMO_PATIENTS: Final[tuple[PatientSpec, ...]] = (
    # --- People with diabetes ------------------------------------------------
    PatientSpec(
        "meera", "Meera Iyer", 58, 0, "diabetes", "en", "moderate",
        "fasting drifts up into the pre-breach band (~121-127 vs limit 130)",
        Series(112, 124, 2.0), steady(150, 8), my_login_slot=True,
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
        "fasting ~108 is pre-breach of the 112 limit",
        steady(108.5, 1.2), steady(125, 6),
    ),
    PatientSpec(
        "arjun", "Arjun Patel", 60, 1, "no_diabetes", "hi", "high",
        "fasting rises 100 -> 116 past the 112 limit",
        Series(100, 116, 2.0), steady(130, 5),
    ),
    PatientSpec(
        "suresh", "Suresh Babu", 47, 1, "no_diabetes", "ta", "moderate",
        "post-meal ~155 in the pre-breach band of the 160 limit",
        steady(95, 3), steady(155, 2.5),
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
        "high risk tier (limits unchanged); readings mostly normal",
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
        linked: Linked to a Supabase login (receives notifications).
        diabetes_status: Limit set.
        age: Age.
        target_tier: Tier searched for.
        model_tier: Tier the model gave (``None`` without a model).
        complex_conditions: Complex-health proxy conditions answered yes.
        readings: Readings inserted.
        scenario: What the readings show.
        kept: ``True`` if an existing demo user with conversation turns was reused.
    """

    key: str
    user_id: uuid.UUID
    name: str
    linked: bool
    diabetes_status: DiabetesStatus
    age: int
    target_tier: RiskTier
    model_tier: RiskTier | None
    complex_conditions: int
    readings: int
    scenario: str
    kept: bool = False


class SeedReport(BaseModel):
    """What :func:`seed_demo` did.

    Attributes:
        deleted_users: Previous demo users removed.
        patients: Patients seeded (created, or kept and re-filled).
        readings: Readings created.
        kept_users: Demo users kept because they have conversation turns.
        preserved_advisories: Advisories kept because a conversation turn refers to them.
        unmatched_user_ids: Kept demo users that match no patient in the cohort; they
            are left exactly as they were.
        practitioner_id: Demo practitioner linked by ``--practitioner-email``.
        assigned_patients: Seeded patients assigned to that practitioner.
        routed_handoffs: Open handoffs (opened before anyone was linked) now routed
            to that practitioner.
        kb: Knowledge-base seeding result (``None`` when no approver exists).
    """

    deleted_users: int
    patients: list[SeededPatient]
    readings: int
    kept_users: int = 0
    preserved_advisories: int = 0
    unmatched_user_ids: list[uuid.UUID] = Field(default_factory=list)
    practitioner_id: uuid.UUID | None = None
    assigned_patients: int = 0
    routed_handoffs: int = 0
    kb: KbSeedReport | None = None


def validate_my_email(email: str, flag: str = "--my-email") -> str:
    """Check an email flag value (``--my-email`` or ``--practitioner-email``).

    Args:
        email: Login email of an account created at ``/app``.
        flag: Flag name, for the error message.

    Returns:
        The email, trimmed and lower-cased (Supabase Auth stores it lower-case).

    Raises:
        SeedError: If it does not look like an email address.
    """
    cleaned = email.strip().lower()
    if not _EMAIL.match(cleaned):
        raise SeedError(f"{flag} must be an email address, e.g. you@example.com")
    return cleaned


class KeptUser(BaseModel):
    """A demo user that has conversation turns and therefore cannot be deleted.

    Attributes:
        user_id: ``users.id``.
        full_name: ``profiles.full_name`` (the seed writes the spec's name there).
        auth_user_id: Linked Supabase login, if any.
    """

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    full_name: str | None
    auth_user_id: uuid.UUID | None = None


def demo_users_with_turns(session: Session) -> list[KeptUser]:
    """List demo users that have conversation turns.

    Args:
        session: Open session.

    Returns:
        The users, ordered by id for a stable result.
    """
    has_turns = select(ConversationTurn.user_id).where(ConversationTurn.user_id == User.id)
    rows = session.execute(
        select(User.id, Profile.full_name, User.auth_user_id)
        .outerjoin(Profile, Profile.user_id == User.id)
        .where(User.is_demo, has_turns.exists())
        .order_by(User.id)
    )
    return [
        KeptUser(user_id=user_id, full_name=name, auth_user_id=auth_id)
        for user_id, name, auth_id in rows
    ]


def match_kept_users(
    kept: Sequence[KeptUser],
    patients: Sequence[PatientSpec],
    my_auth_user_id: uuid.UUID | None,
) -> tuple[dict[int, uuid.UUID], list[uuid.UUID]]:
    """Decide which cohort patient each kept demo user stands for.

    Rules, in order:

    1. A user linked to ``my_auth_user_id`` is the ``my_login_slot`` patient, so
       ``--my-email`` re-attaches to the same user on every re-seed.
    2. A user linked to any other login is the ``my_login_slot`` patient (it was
       linked by an earlier ``--my-email``).
    3. Otherwise the user is the patient whose name equals its ``full_name``.

    A user whose patient is already taken, or that matches none, is unmatched.

    Args:
        kept: Demo users with conversation turns.
        patients: The cohort being seeded.
        my_auth_user_id: Login id from ``--my-email``, or ``None``.

    Returns:
        ``({patient index: user_id}, [unmatched user ids])``.
    """
    slot = next((i for i, spec in enumerate(patients) if spec.my_login_slot), None)
    by_name = {spec.name: i for i, spec in enumerate(patients)}

    def priority(user: KeptUser) -> int:
        if my_auth_user_id is not None and user.auth_user_id == my_auth_user_id:
            return 0
        return 1 if user.auth_user_id is not None else 2

    matched: dict[int, uuid.UUID] = {}
    unmatched: list[uuid.UUID] = []
    # Stable sort: rule 1 first, so your login wins the slot over an older link.
    for user in sorted(kept, key=priority):
        index = slot if user.auth_user_id is not None else by_name.get(user.full_name or "")
        if index is None or index in matched:
            unmatched.append(user.user_id)
        else:
            matched[index] = user.user_id
    return matched, unmatched


def clear_demo_data(session: Session, keep: Collection[uuid.UUID] = ()) -> int:
    """Delete every demo user except ``keep``, with everything that cascades from them.

    Advisories are deleted first: they reference ``thresholds_history`` with ON DELETE
    RESTRICT, so the cascade from ``users`` could otherwise trip over them.

    Args:
        session: Open session (caller commits).
        keep: Demo users to leave alone; must include every demo user with
            conversation turns.

    Returns:
        Number of demo users deleted.

    Raises:
        SeedError: If a demo user outside ``keep`` has conversation turns
            (append-only audit rows, R5), which must never be deleted.
    """
    doomed = select(User.id).where(User.is_demo, User.id.not_in(list(keep))).scalar_subquery()
    turns = session.scalar(
        select(func.count())
        .select_from(ConversationTurn)
        .where(ConversationTurn.user_id.in_(doomed))
    )
    if turns:
        raise SeedError(
            f"demo users to delete have {turns} conversation turns (append-only audit log)"
        )
    session.execute(delete(Advisory).where(Advisory.user_id.in_(doomed)))
    result = session.execute(delete(User).where(User.id.in_(doomed)))
    return int(result.rowcount)  # type: ignore[attr-defined]


def reset_seeded_data(session: Session, user_ids: Collection[uuid.UUID]) -> int:
    """Delete the readings, thresholds and advisories of kept demo users.

    Profiles, risk assessments and conversation turns stay. An advisory that a
    conversation turn refers to is kept (``conversation_turns.advisory_id`` is ON
    DELETE RESTRICT and turns cannot be edited), and so are its reading and threshold,
    which the advisory needs.

    Args:
        session: Open session (caller commits).
        user_ids: Kept demo users.

    Returns:
        Number of advisories kept because a turn refers to them.
    """
    ids = list(user_ids)
    if not ids:
        return 0
    audited = select(ConversationTurn.advisory_id).where(ConversationTurn.advisory_id.is_not(None))
    session.execute(delete(Advisory).where(Advisory.user_id.in_(ids), Advisory.id.not_in(audited)))
    remaining = select(Advisory.reading_id, Advisory.threshold_id).where(Advisory.user_id.in_(ids))
    still_needed = remaining.subquery()
    session.execute(
        delete(ThresholdHistory).where(
            ThresholdHistory.user_id.in_(ids),
            ThresholdHistory.id.not_in(select(still_needed.c.threshold_id)),
        )
    )
    session.execute(
        delete(Reading).where(
            Reading.user_id.in_(ids), Reading.id.not_in(select(still_needed.c.reading_id))
        )
    )
    preserved = session.scalar(
        select(func.count()).select_from(Advisory).where(Advisory.user_id.in_(ids))
    )
    return int(preserved or 0)


def seed_demo(
    session: Session,
    *,
    now: datetime,
    scorer: RiskScorer | None,
    my_email: str | None = None,
    practitioner_email: str | None = None,
    patients: Sequence[PatientSpec] = DEMO_PATIENTS,
    rng_seed: int = DEFAULT_RNG_SEED,
    overwrite_seed: bool = False,
) -> SeedReport:
    """Replace demo data with a fresh cohort (caller commits).

    Demo users with conversation turns are kept and re-filled rather than replaced;
    see the module docstring.

    Args:
        session: Open session.
        now: Current time; readings cover the 30 days up to it.
        scorer: Risk model used to hit each patient's target tier, or ``None``.
        my_email: Validated login email to link to the ``my_login_slot`` patient.
        practitioner_email: Validated login email of the demo practitioner (R4).
        patients: Cohort to create.
        rng_seed: Global seed.
        overwrite_seed: Replace approved KB entries whose text differs from the seed
            (see :func:`scripts.kb_seed.seed_kb`); off by default.

    Returns:
        What was deleted and created.

    Raises:
        SeedError: If no Supabase login has ``my_email``, or that login already
            belongs to a real (non-demo) user; or the practitioner login is missing,
            is the same as ``my_email``, or belongs to a patient.
    """
    if practitioner_email is not None and practitioner_email == my_email:
        raise SeedError("--practitioner-email must be a different login from --my-email")
    my_auth_user_id: uuid.UUID | None = None
    if my_email is not None:
        my_auth_user_id = find_auth_user_id_by_email(session, my_email)
        if my_auth_user_id is None:
            raise SeedError("no Supabase login uses that email: create the account at /app first")
        owner = session.scalar(select(User.is_demo).where(User.auth_user_id == my_auth_user_id))
        if owner is False:
            raise SeedError("that login already belongs to a real (non-demo) user")
    kept = demo_users_with_turns(session)
    matched, unmatched = match_kept_users(kept, patients, my_auth_user_id)
    if unmatched:
        logger.warning(
            "%d demo users with conversation turns match no seeded patient; left untouched",
            len(unmatched),
        )
    deleted = clear_demo_data(session, keep=[user.user_id for user in kept])
    preserved = reset_seeded_data(session, matched.values())
    # Deletes must reach the database before a kept or new user takes over a login
    # that a deleted user held (users.auth_user_id is unique).
    session.flush()

    seeded: list[SeededPatient] = []
    reading_rows: list[dict[str, object]] = []
    for index, spec in enumerate(patients):
        risk_profile, assessment = build_risk_profile(spec, scorer, rng_seed=rng_seed)
        login = my_auth_user_id if spec.my_login_slot else None
        user_id = matched.get(index)
        if user_id is not None:
            linked = _reattach_login(session, user_id, login)
        else:
            user_id = _create_patient(session, spec, risk_profile, login)
            linked = login is not None
        readings = generate_readings(spec, now=now, rng_seed=rng_seed)
        reading_rows.extend(
            {"user_id": user_id, "source": "seed", **r.model_dump()} for r in readings
        )
        seeded.append(
            SeededPatient(
                key=spec.key,
                user_id=user_id,
                name=spec.name,
                linked=linked,
                diabetes_status=spec.diabetes_status,
                age=spec.age,
                target_tier=spec.target_tier,
                model_tier=assessment.tier if assessment is not None else None,
                complex_conditions=sum(getattr(risk_profile, c) for c in _CONDITIONS),
                readings=len(readings),
                scenario=spec.scenario,
                kept=index in matched,
            )
        )
    session.flush()
    if reading_rows:
        session.execute(insert(Reading), reading_rows)

    practitioner_id: uuid.UUID | None = None
    routed_handoffs = 0
    if practitioner_email is not None:
        practitioner_id = link_practitioner(session, practitioner_email)
        # Routes any handoff a seeded patient opened before a practitioner was linked.
        routed_handoffs = assign_patients(session, practitioner_id, [p.user_id for p in seeded])
    approver = practitioner_id or session.scalar(
        select(Practitioner.id).where(Practitioner.is_active).order_by(Practitioner.created_at)
    )
    kb_report: KbSeedReport | None = None
    if approver is not None:
        kb_report = seed_kb(session, approver_id=approver, now=now, overwrite=overwrite_seed)
    else:
        logger.warning("knowledge base not seeded: no practitioner to approve it")
    return SeedReport(
        deleted_users=deleted,
        patients=seeded,
        readings=len(reading_rows),
        kept_users=len(matched),
        preserved_advisories=preserved,
        unmatched_user_ids=unmatched,
        practitioner_id=practitioner_id,
        assigned_patients=len(seeded) if practitioner_id is not None else 0,
        routed_handoffs=routed_handoffs,
        kb=kb_report,
    )


# The Audit tab (R5) counts KB entries approved under this name as "not yet reviewed".
DEMO_PRACTITIONER_NAME: Final = SEED_PRACTITIONER_NAME


def link_practitioner(session: Session, email: str) -> uuid.UUID:
    """Make an existing login the demo practitioner (R4), reusing earlier links.

    The practitioner user is **not** a demo user: ``clear_demo_data`` deletes demo
    users on every re-seed, and practitioners are referenced by audit rows (RESTRICT).

    Args:
        session: Open session (caller commits).
        email: Validated login email.

    Returns:
        ``practitioners.id``.

    Raises:
        SeedError: If no login has that email, or it belongs to a real patient.
    """
    auth_user_id = find_auth_user_id_by_email(session, email)
    if auth_user_id is None:
        raise SeedError(
            "no Supabase login uses the practitioner email: create that account at /app first"
        )
    user = session.scalar(select(User).where(User.auth_user_id == auth_user_id))
    if user is not None and user.role == "patient" and user.is_demo:
        # An earlier --my-email linked this login to a synthetic patient. Demo data is
        # disposable, so the login moves to the practitioner; the demo patient stays
        # (with its turns, if any) but is no longer linked.
        logger.info("practitioner login was linked to demo patient %s; unlinking", user.id)
        user.auth_user_id = None
        session.flush()
        user = None
    if user is not None and user.role == "patient":
        raise SeedError(
            "the practitioner login belongs to a real patient; use a separate account "
            "for the practitioner"
        )
    if user is None:
        user = User(id=uuid.uuid4(), role="practitioner", auth_user_id=auth_user_id)
        session.add(user)
        session.flush()
    practitioner = session.scalar(select(Practitioner).where(Practitioner.user_id == user.id))
    if practitioner is None:
        practitioner = Practitioner(
            id=uuid.uuid4(), user_id=user.id, full_name=DEMO_PRACTITIONER_NAME
        )
        session.add(practitioner)
    practitioner.is_active = True
    user.is_active = True
    session.flush()
    return practitioner.id


def _create_patient(
    session: Session,
    spec: PatientSpec,
    risk_profile: RiskProfile,
    auth_user_id: uuid.UUID | None,
) -> uuid.UUID:
    """Insert a new demo user and profile (no phone number).

    Args:
        session: Open session.
        spec: The patient.
        risk_profile: BRFSS answers found for the target tier.
        auth_user_id: Login to link (``--my-email`` patient only), or ``None``.

    Returns:
        The new user id.
    """
    user = User(id=uuid.uuid4(), role="patient", is_demo=True, auth_user_id=auth_user_id)
    session.add(user)
    session.flush()
    answers = risk_profile.model_dump(exclude={"age", "sex"})
    session.add(
        Profile(
            user_id=user.id,
            full_name=spec.name,
            language=spec.language,
            age_years=spec.age,
            sex=spec.sex,
            diabetes_status=spec.diabetes_status,
            **{**answers, "bmi": Decimal(str(answers["bmi"]))},
        )
    )
    return user.id


def _reattach_login(session: Session, user_id: uuid.UUID, auth_user_id: uuid.UUID | None) -> bool:
    """Link a kept demo user to ``--my-email``'s login (if given).

    Args:
        session: Open session.
        user_id: Kept demo user.
        auth_user_id: Login to link, or ``None`` to keep the current link.

    Returns:
        Whether the user is linked to a login afterwards.

    Raises:
        SeedError: If the user no longer exists.
    """
    user = session.get(User, user_id)
    if user is None:  # pragma: no cover - demo_users_with_turns just returned it
        raise SeedError(f"kept demo user {user_id} disappeared")
    if auth_user_id is not None and user.auth_user_id != auth_user_id:
        user.auth_user_id = auth_user_id
        session.flush()
    return user.auth_user_id is not None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_report(report: SeedReport) -> None:
    """Print a table of the seeded cohort.

    Args:
        report: Result of :func:`seed_demo`.
    """
    lines = [
        f"Deleted {report.deleted_users} previous demo users; kept {report.kept_users} with "
        f"conversation turns (readings, thresholds and advisories replaced, "
        f"{report.preserved_advisories} audited advisories preserved).",
        f"Seeded {len(report.patients)} demo patients, {report.readings} readings.",
        "",
        f"{'patient':<16} {'login':<6} {'status':<12} {'age':>3} {'tier(target/model)':<20} "
        "scenario",
    ]
    for p in report.patients:
        tier = f"{p.target_tier}/{p.model_tier or '?'}"
        name = f"{p.name}*" if p.kept else p.name
        lines.append(
            f"{name:<16} {'linked' if p.linked else '-':<6} {p.diabetes_status:<12} {p.age:>3} "
            f"{tier:<20} {p.scenario}"
        )
    if report.kept_users:
        lines.append("* kept user (has conversation turns)")
    if report.unmatched_user_ids:
        lines.append(f"Left untouched (match no patient): {report.unmatched_user_ids}")
    if report.practitioner_id is not None:
        lines.append(
            f"Demo practitioner linked; {report.assigned_patients} patients assigned "
            "(open /app/practitioner with that login)."
        )
        if report.routed_handoffs:
            lines.append(
                f"{report.routed_handoffs} open handoff(s) opened before a practitioner was "
                "linked are now routed to them."
            )
    lines += kb_report_lines(report.kb)
    lines += ["", "Next: uv run python -m app.jobs.batch_update"]
    print("\n".join(lines))


def kb_report_lines(kb: KbSeedReport | None) -> list[str]:
    """Describe the knowledge-base part of a seed run.

    Args:
        kb: Result of :func:`scripts.kb_seed.seed_kb`, or ``None`` when not seeded.

    Returns:
        Report lines; an overwrite starts with a WARNING naming the retired entries.
    """
    if kb is None:
        return ["Knowledge base not seeded: pass --practitioner-email to approve it."]
    lines: list[str] = []
    if kb.retired:
        lines.append(
            f"WARNING: --overwrite-seed retired {len(kb.retired)} approved entr"
            f"{'y' if len(kb.retired) == 1 else 'ies'} and replaced practitioner text "
            f"with the seed: {', '.join(kb.retired)}"
        )
    if kb.inserted:
        lines.append(
            f"Knowledge base: {kb.inserted} entries approved as kb_version {kb.kb_version} "
            f"({kb.unchanged} unchanged)."
        )
    else:
        lines.append(f"Knowledge base: {kb.unchanged} seed entries already approved.")
    if kb.kept:
        lines.append(
            f"Knowledge base: {len(kb.kept)} kept (practitioner-owned): {', '.join(kb.kept)}. "
            "Re-run with --overwrite-seed to replace them with the seed text."
        )
    return lines


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
        "--my-email",
        help="link one demo patient to your /app login (the account must exist)",
    )
    parser.add_argument(
        "--practitioner-email",
        help="link another existing /app login as the demo practitioner (R4)",
    )
    parser.add_argument("--rng-seed", type=int, default=DEFAULT_RNG_SEED)
    parser.add_argument(
        "--overwrite-seed",
        action="store_true",
        help=(
            "replace approved KB entries whose text differs from the seed (practitioner "
            "edits) and retire removed seed entries; prints the entries it retires"
        ),
    )
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)

    my_email = validate_my_email(args.my_email) if args.my_email else None
    practitioner_email = (
        validate_my_email(args.practitioner_email, "--practitioner-email")
        if args.practitioner_email
        else None
    )
    factory = get_session_factory()
    with factory() as session:
        now = session.scalar(select(func.now()))
        if not isinstance(now, datetime):  # pragma: no cover - now() is never NULL
            raise SeedError("database returned no now()")
        report = seed_demo(
            session,
            now=now,
            scorer=load_risk_scorer(),
            my_email=my_email,
            practitioner_email=practitioner_email,
            rng_seed=args.rng_seed,
            overwrite_seed=args.overwrite_seed,
        )
        session.commit()
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
