"""Tests for scripts/seed_demo.py: cohort coverage, determinism, phones and scenarios.

The scenario test runs the generated readings through the real threshold engine and
advisory planner (no database), so it proves the cohort yields every advisory state.
The live seed + batch round trip is in tests/test_batch_update.py.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.ml.predict import RiskAssessment
from app.services.advisory import EvaluatedReading, plan_advisories
from app.services.thresholds import (
    ALL_LIMIT_KINDS,
    TREND_CONTEXTS,
    ReadingPoint,
    ThresholdInputs,
    analyse_trend,
    classify_reading,
    plan_threshold_update,
)
from scripts.seed_demo import (
    DAYS,
    DEMO_PATIENTS,
    PatientSpec,
    SeedError,
    build_risk_profile,
    demo_phone,
    generate_readings,
    validate_my_phone,
)

NOW = datetime(2026, 11, 15, 18, 0, tzinfo=UTC)  # 23:30 IST: today's readings exist
# Explicit band (default PRE_BREACH_BAND): tests must not depend on a local .env.
BAND = 0.05


def _spec(key: str) -> PatientSpec:
    return next(s for s in DEMO_PATIENTS if s.key == key)


def _advisory_states(spec: PatientSpec) -> Counter[str]:
    """Simulate one batch run for ``spec`` with the pure engine (risk tier = target)."""
    profile, _ = build_risk_profile(spec, None)
    inputs = ThresholdInputs(
        diabetes_status=spec.diabetes_status,
        age_years=spec.age,
        sex=spec.sex,
        high_bp=profile.high_bp,
        high_chol=profile.high_chol,
        heart_disease_or_attack=profile.heart_disease_or_attack,
        stroke=profile.stroke,
        diff_walk=profile.diff_walk,
    )
    risk = RiskAssessment(probability=0.5, tier=spec.target_tier, flagged=True, model_version="t")
    points = [
        ReadingPoint(value_mg_dl=r.value_mg_dl, context=r.context, measured_at=r.measured_at)
        for r in generate_readings(spec, now=NOW)
    ]
    thresholds = {
        kind: plan_threshold_update(
            limit_kind=kind,
            inputs=inputs,
            risk=risk,
            trend=analyse_trend([p for p in points if p.context in TREND_CONTEXTS[kind]], NOW),
            previous=None,
        ).threshold.threshold_mg_dl
        for kind in ALL_LIMIT_KINDS
    }
    evaluated = [
        EvaluatedReading(
            uuid.uuid4(),
            p.measured_at,
            p.context,
            p.value_mg_dl,
            classify_reading(p, thresholds, band=BAND),
        )
        for p in points
    ]
    drafts = plan_advisories(
        evaluated, [], cooldown=timedelta(hours=24), preferred_language="en"
    ).drafts
    states: Counter[str] = Counter(d.state for d in drafts)
    states["level_2_hypo"] = sum(d.escalated and d.reading_value_mg_dl < 54 for d in drafts)
    return states


def test_cohort_size_and_mix() -> None:
    assert 18 <= len(DEMO_PATIENTS) <= 22
    statuses = Counter(s.diabetes_status for s in DEMO_PATIENTS)
    assert statuses["diabetes"] >= 5 and statuses["no_diabetes"] >= 5
    for status in ("diabetes", "no_diabetes"):
        tiers = {s.target_tier for s in DEMO_PATIENTS if s.diabetes_status == status}
        assert tiers == {"low", "moderate", "high"}, status
    assert len({s.key for s in DEMO_PATIENTS}) == len(DEMO_PATIENTS)
    assert sum(s.my_phone_slot for s in DEMO_PATIENTS) == 1


def test_healthy_and_complex_older_adults_present() -> None:
    older = [s for s in DEMO_PATIENTS if s.diabetes_status == "diabetes" and s.age >= 65]
    conditions = [sum(build_risk_profile(s, None)[0].model_dump()[c] for c in s.conditions)
                  for s in older]  # fmt: skip
    assert any(n >= 3 for n in conditions), "need a complex/intermediate older adult"
    assert any(n <= 1 for n in conditions), "need a healthy older adult"


def test_phone_numbers_are_fake_and_unique() -> None:
    phones = [demo_phone(i) for i in range(len(DEMO_PATIENTS))]
    assert phones[0] == "+15550100"
    assert len(set(phones)) == len(phones)
    assert all("+15550100" <= p <= "+15550199" for p in phones)
    assert demo_phone(99) == "+15550199"
    with pytest.raises(SeedError, match="fake numbers"):
        demo_phone(100)


@pytest.mark.parametrize("raw", ["+91 98765 43210", "+919876543210", "+1 (415) 555-2671"])
def test_my_phone_accepts_e164(raw: str) -> None:
    assert validate_my_phone(raw).startswith("+")
    assert " " not in validate_my_phone(raw)


@pytest.mark.parametrize("raw", ["9876543210", "+0123456789", "+15550142", "hello"])
def test_my_phone_rejects_bad_or_fake(raw: str) -> None:
    with pytest.raises(SeedError):
        validate_my_phone(raw)


def test_readings_cover_30_days_of_fasting_and_post_meal() -> None:
    readings = generate_readings(DEMO_PATIENTS[0], now=NOW)
    ist = ZoneInfo("Asia/Kolkata")
    days = {r.measured_at.astimezone(ist).date() for r in readings}
    assert len(days) == DAYS
    assert Counter(r.context for r in readings) == {"fasting": DAYS, "post_meal": DAYS}
    assert all(r.measured_at <= NOW for r in readings)
    assert all(20 <= r.value_mg_dl <= 600 for r in readings)


def test_no_future_readings_early_in_the_day() -> None:
    early = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)  # 05:30 IST, before 07:00 fasting
    readings = generate_readings(DEMO_PATIENTS[0], now=early)
    assert all(r.measured_at <= early for r in readings)
    assert len(readings) == 2 * (DAYS - 1)


def test_generation_is_deterministic() -> None:
    spec = DEMO_PATIENTS[1]
    assert generate_readings(spec, now=NOW) == generate_readings(spec, now=NOW)
    assert build_risk_profile(spec, None) == build_risk_profile(spec, None)
    assert generate_readings(spec, now=NOW, rng_seed=1) != generate_readings(spec, now=NOW)


def test_events_override_generated_values() -> None:
    values = [r.value_mg_dl for r in generate_readings(_spec("priya"), now=NOW)]
    assert min(values) < 54


def test_cohort_produces_every_advisory_state() -> None:
    total: Counter[str] = Counter()
    for spec in DEMO_PATIENTS:
        total.update(_advisory_states(spec))
    for state in ("pre_breach_high", "pre_breach_low", "breach_high", "breach_low"):
        assert total[state] >= 1, state
    assert total["level_2_hypo"] >= 1


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("meera", "pre_breach_high"),
        ("rohan", "breach_high"),
        ("priya", "breach_low"),
        ("kavya", "pre_breach_low"),
        ("saroja", "pre_breach_low"),
        ("vikram", "pre_breach_high"),
    ],
)
def test_named_scenarios(key: str, expected: str) -> None:
    assert _advisory_states(_spec(key))[expected] >= 1


def test_rising_trend_patient_gets_trend_revision() -> None:
    spec = _spec("rohan")
    points = [
        ReadingPoint(value_mg_dl=r.value_mg_dl, context=r.context, measured_at=r.measured_at)
        for r in generate_readings(spec, now=NOW)
        if r.context == "fasting"
    ]
    trend = analyse_trend(points, NOW)
    assert trend.significant
    assert trend.slope_mg_dl_per_day is not None and trend.slope_mg_dl_per_day > 1


def test_well_controlled_patients_raise_nothing() -> None:
    for key in ("aarav", "rahul", "ananya", "fatima"):
        assert sum(_advisory_states(_spec(key)).values()) == 0, key
