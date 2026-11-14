"""Tests for the personal threshold engine (R2) and the pre-breach band check (R3).

All maths is pure, so nothing here touches a database: persistence is exercised
through an in-memory fake that implements the ``ThresholdRepository`` protocol.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.clinical_limits import GlucoseLimitKind, get_glucose_limit
from app.core.exceptions import ThresholdComputationError
from app.ml.predict import RiskAssessment, RiskTier
from app.services import thresholds as th
from app.services.thresholds import (
    COMPLEX_HEALTH_PROXY_FIELDS,
    MIN_TREND_READINGS,
    TREND_WINDOW_DAYS,
    UNSCORED_MODEL_VERSION,
    ReadingPoint,
    StoredThreshold,
    ThresholdComputation,
    ThresholdInputs,
    TrendAnalysis,
    analyse_trend,
    assemble_threshold,
    classify_reading,
    demographic_adjustment,
    plan_threshold_update,
    refresh_user_thresholds,
    risk_adjustment,
    threshold_values,
    trend_adjustment,
)

AS_OF = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)
BAND = 0.05
U = Decimal("130.0")
L = Decimal("70.0")
LIMITS: dict[GlucoseLimitKind, Decimal] = {
    "fasting_high": U,
    "post_meal_high": Decimal("180.0"),
    "low": L,
}
DM_FASTING = get_glucose_limit("diabetes", "fasting_high")
DM_POST_MEAL = get_glucose_limit("diabetes", "post_meal_high")
DM_LOW = get_glucose_limit("diabetes", "low")
NO_DM_FASTING = get_glucose_limit("no_diabetes", "fasting_high")
NO_DM_POST_MEAL = get_glucose_limit("no_diabetes", "post_meal_high")
NO_DM_LOW = get_glucose_limit("no_diabetes", "low")
ADULT_DM = ThresholdInputs(diabetes_status="diabetes", age_years=50, sex=1)
ADULT_NO_DM = ThresholdInputs(diabetes_status="no_diabetes", age_years=50, sex=1)
TIERS: tuple[RiskTier, ...] = ("low", "moderate", "high")


def _older_dm(age: int, n_conditions: int) -> ThresholdInputs:
    """A diabetic user of ``age`` with the first ``n_conditions`` proxy answers 'yes'."""
    flags = {name: i < n_conditions for i, name in enumerate(COMPLEX_HEALTH_PROXY_FIELDS)}
    return ThresholdInputs.model_validate(
        {"diabetes_status": "diabetes", "age_years": age, "sex": 0, **flags}
    )


def _limits_for(inputs: ThresholdInputs, risk: RiskAssessment | None) -> dict[str, Decimal]:
    """Initial threshold per limit kind for a user with no readings."""
    return {
        kind: plan_threshold_update(
            limit_kind=kind,
            inputs=inputs,
            risk=risk,
            trend=analyse_trend([], AS_OF),
            previous=None,
        ).threshold.threshold_mg_dl
        for kind in ("fasting_high", "post_meal_high", "low")
    }


def _reading(value: Decimal | float, context: str = "fasting", days_ago: float = 0) -> ReadingPoint:
    return ReadingPoint.model_validate(
        {
            "value_mg_dl": value,
            "context": context,
            "measured_at": AS_OF - timedelta(days=days_ago),
        }
    )


def _series(values: Sequence[float], context: str = "fasting") -> list[ReadingPoint]:
    """One reading per day, oldest first, the last one at AS_OF."""
    n = len(values)
    return [_reading(v, context, days_ago=n - 1 - i) for i, v in enumerate(values)]


def _risk(tier: RiskTier, probability: float = 0.5) -> RiskAssessment:
    return RiskAssessment(
        probability=probability, tier=tier, flagged=False, model_version="test-v1"
    )


def _trend(slope: float | None, *, significant: bool, n: int = 10) -> TrendAnalysis:
    return TrendAnalysis(
        n_readings=n,
        slope_mg_dl_per_day=slope,
        p_value=0.001 if significant else 0.4,
        ewma_mg_dl=130.0,
        significant=significant,
    )


# Strongly rising fasting glucose: ~3 mg/dL/day with small noise (p << 0.05).
RISING = [120, 124, 125, 129, 133, 134, 138]
# Noise around 130 with no real trend: slope != 0 but p well above 0.05.
NOISY_FLAT = [125, 140, 118, 137, 121, 123, 137, 118, 140, 127]


# ---------------------------------------------------------------------------
# R3: pre-breach band - upper limit U
# ---------------------------------------------------------------------------


def _state(value: Decimal | float, context: str = "fasting", band: float = BAND) -> str:
    return classify_reading(_reading(value, context), LIMITS, band=band).state


def test_upper_exactly_95_percent_is_pre_breach() -> None:
    assert _state(U * Decimal("0.95")) == "pre_breach_high"  # 123.5


def test_upper_94_99_percent_is_normal() -> None:
    assert _state(U * Decimal("0.9499")) == "normal"  # 123.487


def test_upper_exactly_100_percent_is_breach() -> None:
    assert _state(U) == "breach_high"


def test_upper_just_below_limit_is_pre_breach() -> None:
    assert _state(U - Decimal("0.1")) == "pre_breach_high"


def test_above_upper_is_breach() -> None:
    assert _state(Decimal("250")) == "breach_high"


def test_float_input_matches_decimal_at_boundary() -> None:
    # 0.95 * 130 is not exact in binary floating point; the band check must be.
    assert _state(123.5) == "pre_breach_high"
    assert _state(123.49) == "normal"


@pytest.mark.parametrize(
    ("limit", "at_95", "below_95"),
    [
        (Decimal("123.4"), Decimal("117.23"), Decimal("117.22")),
        (Decimal("100.0"), Decimal("95.0"), Decimal("94.99")),
        (Decimal("180.0"), Decimal("171.0"), Decimal("170.99")),
    ],
)
def test_upper_boundary_for_non_round_limits(
    limit: Decimal, at_95: Decimal, below_95: Decimal
) -> None:
    limits = {**LIMITS, "fasting_high": limit}
    assert classify_reading(_reading(at_95), limits, band=BAND).state == "pre_breach_high"
    assert classify_reading(_reading(below_95), limits, band=BAND).state == "normal"
    assert classify_reading(_reading(limit), limits, band=BAND).state == "breach_high"


# ---------------------------------------------------------------------------
# R3: pre-breach band - lower limit L (mirror cases)
# ---------------------------------------------------------------------------


def test_low_exactly_105_percent_is_pre_breach() -> None:
    assert _state(L * Decimal("1.05")) == "pre_breach_low"  # 73.5


def test_low_105_01_percent_is_normal() -> None:
    assert _state(L * Decimal("1.0501")) == "normal"  # 73.507


def test_low_exactly_100_percent_is_breach() -> None:
    assert _state(L) == "breach_low"


def test_low_just_above_limit_is_pre_breach() -> None:
    assert _state(L + Decimal("0.1")) == "pre_breach_low"


def test_below_low_is_breach() -> None:
    assert _state(Decimal("60")) == "breach_low"


@pytest.mark.parametrize(
    ("limit", "at_105", "above_105"),
    [
        (Decimal("75.0"), Decimal("78.75"), Decimal("78.76")),
        (Decimal("80.0"), Decimal("84.0"), Decimal("84.01")),
    ],
)
def test_low_boundary_for_non_round_limits(
    limit: Decimal, at_105: Decimal, above_105: Decimal
) -> None:
    limits = {**LIMITS, "low": limit}
    assert classify_reading(_reading(at_105), limits, band=BAND).state == "pre_breach_low"
    assert classify_reading(_reading(above_105), limits, band=BAND).state == "normal"
    assert classify_reading(_reading(limit), limits, band=BAND).state == "breach_low"


def test_mid_range_is_normal() -> None:
    result = classify_reading(_reading(100), LIMITS, band=BAND)
    assert result.state == "normal"
    assert result.limit_kind is None
    assert result.threshold_mg_dl is None
    assert not result.escalate
    assert not result.needs_practitioner


# ---------------------------------------------------------------------------
# R3/R4: Level 2 hypoglycaemia escalation
# ---------------------------------------------------------------------------


def test_below_level_2_escalates() -> None:
    result = classify_reading(_reading(Decimal("53.9")), LIMITS, band=BAND)
    assert result.state == "breach_low"
    assert result.escalate


def test_exactly_54_does_not_escalate() -> None:
    # ADA Level 2 is glucose < 54 mg/dL, so 54 itself is Level 1.
    result = classify_reading(_reading(Decimal("54")), LIMITS, band=BAND)
    assert result.state == "breach_low"
    assert not result.escalate


def test_level_2_escalates_for_random_readings_too() -> None:
    assert classify_reading(_reading(40, "random"), LIMITS, band=BAND).escalate


def test_every_breach_needs_a_practitioner() -> None:
    high = classify_reading(_reading(200), LIMITS, band=BAND)
    assert high.state == "breach_high"
    assert not high.escalate
    assert high.needs_practitioner
    pre = classify_reading(_reading(125), LIMITS, band=BAND)
    assert not pre.needs_practitioner


# ---------------------------------------------------------------------------
# R3: which limits apply, and where the band comes from
# ---------------------------------------------------------------------------


def test_result_names_the_limit_it_was_judged_against() -> None:
    result = classify_reading(_reading(125), LIMITS, band=BAND)
    assert result.limit_kind == "fasting_high"
    assert result.threshold_mg_dl == U
    low = classify_reading(_reading(72), LIMITS, band=BAND)
    assert low.limit_kind == "low"
    assert low.threshold_mg_dl == L


def test_post_meal_reading_uses_post_meal_limit() -> None:
    assert _state(Decimal("171"), "post_meal") == "pre_breach_high"
    assert _state(Decimal("170.9"), "post_meal") == "normal"
    assert _state(Decimal("140"), "post_meal") == "normal"  # would breach the fasting limit


def test_random_reading_checks_low_limit_only() -> None:
    assert _state(Decimal("300"), "random") == "normal"
    assert _state(Decimal("70"), "random") == "breach_low"


def test_missing_threshold_raises() -> None:
    with pytest.raises(ThresholdComputationError, match="fasting_high"):
        classify_reading(_reading(100), {"low": L}, band=BAND)


def test_band_defaults_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(th, "get_settings", lambda: SimpleNamespace(pre_breach_band=0.10))
    # 0.90 * 130 = 117 is inside a 10% band but outside the default 5% band.
    assert classify_reading(_reading(117), LIMITS).state == "pre_breach_high"
    assert classify_reading(_reading(117), LIMITS, band=BAND).state == "normal"


# ---------------------------------------------------------------------------
# R2: bounded adjustments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "fasting", "post_meal"),
    [
        (None, Decimal(0), Decimal(0)),
        ("low", Decimal(0), Decimal(0)),
        ("moderate", Decimal("-6.0"), Decimal("-10.0")),
        ("high", Decimal("-12.0"), Decimal("-20.0")),
    ],
)
def test_risk_tightens_non_diabetic_upper_limits_toward_floor(
    tier: RiskTier | None, fasting: Decimal, post_meal: Decimal
) -> None:
    assert risk_adjustment(NO_DM_FASTING, tier) == fasting
    assert risk_adjustment(NO_DM_POST_MEAL, tier) == post_meal


@pytest.mark.parametrize("tier", TIERS)
def test_risk_never_moves_low_limit(tier: RiskTier) -> None:
    assert risk_adjustment(NO_DM_LOW, tier) == 0
    assert risk_adjustment(DM_LOW, tier) == 0


@pytest.mark.parametrize("tier", [None, *TIERS])
def test_risk_never_moves_diabetic_limits(tier: RiskTier | None) -> None:
    for limit in (DM_FASTING, DM_POST_MEAL, DM_LOW):
        assert risk_adjustment(limit, tier) == 0


@pytest.mark.parametrize(
    ("tier", "fasting", "post_meal"),
    [("low", "112.0", "160.0"), ("moderate", "106.0", "150.0"), ("high", "100.0", "140.0")],
)
def test_non_diabetic_limits_by_tier(tier: RiskTier, fasting: str, post_meal: str) -> None:
    limits = _limits_for(ADULT_NO_DM, _risk(tier))
    assert limits["fasting_high"] == Decimal(fasting)
    assert limits["post_meal_high"] == Decimal(post_meal)
    assert limits["low"] == Decimal("70.0")


def test_high_risk_non_diabetic_reaches_floor_without_clamping() -> None:
    d = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_NO_DM,
        risk=_risk("high"),
        trend=analyse_trend([], AS_OF),
        previous=None,
    )
    assert d.threshold.unclamped_mg_dl == d.threshold.threshold_mg_dl == Decimal("100.0")


def test_diabetic_limits_unaffected_by_tier() -> None:
    expected = {"fasting_high": Decimal(130), "post_meal_high": Decimal(180), "low": Decimal(70)}
    for tier in TIERS:
        assert _limits_for(ADULT_DM, _risk(tier)) == expected
    assert _limits_for(ADULT_DM, None) == expected


def test_complex_older_adult_adjustment() -> None:
    older = _older_dm(65, 3)
    assert older.is_complex_older_adult
    assert demographic_adjustment(DM_FASTING, older) == Decimal(10)
    assert demographic_adjustment(DM_POST_MEAL, older) == 0
    assert demographic_adjustment(DM_LOW, older) == Decimal(5)


def test_complex_70_year_old_gets_relaxed_targets() -> None:
    assert _limits_for(_older_dm(70, 3), _risk("high")) == {
        "fasting_high": Decimal(140),
        "post_meal_high": Decimal(180),
        "low": Decimal(75),
    }


@pytest.mark.parametrize("n_conditions", [0, 2])
def test_healthy_70_year_old_gets_standard_targets(n_conditions: int) -> None:
    healthy = _older_dm(70, n_conditions)
    assert not healthy.is_complex_older_adult
    assert _limits_for(healthy, _risk("low")) == {
        "fasting_high": Decimal(130),
        "post_meal_high": Decimal(180),
        "low": Decimal(70),
    }


@pytest.mark.parametrize("field", COMPLEX_HEALTH_PROXY_FIELDS)
def test_each_proxy_condition_counts(field: str) -> None:
    inputs = ThresholdInputs.model_validate({"diabetes_status": "diabetes", field: True})
    assert inputs.complex_health_conditions == 1


def test_unknown_conditions_count_as_absent() -> None:
    inputs = ThresholdInputs.model_validate(
        {"diabetes_status": "diabetes", "age_years": 80, "high_bp": True, "stroke": True}
    )
    assert inputs.complex_health_conditions == 2
    assert not inputs.is_complex_older_adult


def test_inputs_validate_from_a_profile_row() -> None:
    profile = SimpleNamespace(
        diabetes_status="diabetes",
        age_years=72,
        sex=1,
        high_bp=True,
        high_chol=True,
        heart_disease_or_attack=False,
        stroke=None,
        diff_walk=True,
        bmi=31.5,
    )
    assert ThresholdInputs.model_validate(profile).is_complex_older_adult


def test_profile_without_diabetes_status_is_rejected() -> None:
    profile = SimpleNamespace(diabetes_status=None, age_years=40, sex=0)
    with pytest.raises(ValidationError):
        ThresholdInputs.model_validate(profile)


@pytest.mark.parametrize(
    "inputs",
    [
        ThresholdInputs(diabetes_status="diabetes", age_years=64, sex=0),
        ThresholdInputs(diabetes_status="diabetes", age_years=None, sex=None),
        ThresholdInputs(diabetes_status="no_diabetes", age_years=80, sex=1),
        _older_dm(64, 5),
        ThresholdInputs.model_validate(
            {
                "diabetes_status": "no_diabetes",
                "age_years": 80,
                **dict.fromkeys(COMPLEX_HEALTH_PROXY_FIELDS, True),
            }
        ),
    ],
)
def test_no_demographic_adjustment(inputs: ThresholdInputs) -> None:
    limit = get_glucose_limit(inputs.diabetes_status, "fasting_high")
    assert demographic_adjustment(limit, inputs) == 0


def test_sex_alone_never_changes_a_limit() -> None:
    for kind in ("fasting_high", "post_meal_high", "low"):
        limit = get_glucose_limit("diabetes", kind)
        female = ThresholdInputs(diabetes_status="diabetes", age_years=50, sex=0)
        male = ThresholdInputs(diabetes_status="diabetes", age_years=50, sex=1)
        assert demographic_adjustment(limit, female) == demographic_adjustment(limit, male)


def test_rising_trend_tightens_upper_limit() -> None:
    # 1 mg/dL/day over a 14-day window is a 14 mg/dL rise; half of it is applied.
    assert trend_adjustment(DM_FASTING, _trend(1.0, significant=True)) == Decimal("-7.0")


def test_trend_adjustment_is_capped() -> None:
    assert trend_adjustment(DM_FASTING, _trend(50.0, significant=True)) == Decimal(-10)
    assert trend_adjustment(DM_LOW, _trend(-50.0, significant=True)) == Decimal(5)


def test_falling_trend_tightens_low_limit() -> None:
    assert trend_adjustment(DM_LOW, _trend(-0.5, significant=True)) == Decimal("3.5")


def test_trend_away_from_a_limit_never_relaxes_it() -> None:
    assert trend_adjustment(DM_FASTING, _trend(-3.0, significant=True)) == 0
    assert trend_adjustment(DM_LOW, _trend(3.0, significant=True)) == 0


def test_non_significant_trend_has_no_adjustment() -> None:
    assert trend_adjustment(DM_FASTING, _trend(5.0, significant=False)) == 0


# ---------------------------------------------------------------------------
# R2: assembling and clamping
# ---------------------------------------------------------------------------


def test_components_are_recorded() -> None:
    t = assemble_threshold(
        DM_FASTING,
        demo_adj=Decimal(10),
        risk_adj=Decimal(-5),
        trend_adj=Decimal("-7.0"),
        reason="initial",
        risk=_risk("moderate", 0.41234),
        trend=_trend(1.0, significant=True, n=9),
    )
    assert t.base_mg_dl == Decimal("130.0")
    assert t.unclamped_mg_dl == Decimal("128.0")
    assert t.threshold_mg_dl == Decimal("128.0")
    assert (t.hard_min_mg_dl, t.hard_max_mg_dl) == (Decimal("100.0"), Decimal("180.0"))
    assert t.direction == "upper"
    assert t.diabetes_status == "diabetes"
    assert t.model_version == "test-v1"
    assert t.risk_tier == "moderate"
    assert t.risk_score == Decimal("0.4123")
    assert t.trend_n_readings == 9
    assert t.source_citation == DM_FASTING.source


def test_clamps_at_hard_min() -> None:
    t = assemble_threshold(
        DM_FASTING,
        demo_adj=Decimal(0),
        risk_adj=Decimal(-40),
        trend_adj=Decimal(0),
        reason="initial",
    )
    assert t.unclamped_mg_dl == Decimal("90.0")
    assert t.threshold_mg_dl == DM_FASTING.hard_min_mg_dl


def test_clamps_at_hard_max() -> None:
    t = assemble_threshold(
        DM_FASTING,
        demo_adj=Decimal(60),
        risk_adj=Decimal(0),
        trend_adj=Decimal(0),
        reason="initial",
    )
    assert t.unclamped_mg_dl == Decimal("190.0")
    assert t.threshold_mg_dl == DM_FASTING.hard_max_mg_dl


def test_high_risk_and_rising_trend_without_diabetes_clamps_at_normal_fasting() -> None:
    # 112 - 12 (high risk) - 10 (trend) = 90, below the ADA-2 normal-fasting floor.
    trend = analyse_trend(_series(RISING), AS_OF)
    d = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_NO_DM,
        risk=_risk("high"),
        trend=trend,
        previous=None,
    )
    assert d.threshold.unclamped_mg_dl == Decimal("90.0")
    assert d.threshold.threshold_mg_dl == NO_DM_FASTING.hard_min_mg_dl


def test_falling_trend_without_diabetes_is_clamped_at_fixed_low() -> None:
    inputs = ThresholdInputs(diabetes_status="no_diabetes", age_years=50, sex=0)
    falling = analyse_trend(_series([95, 91, 88, 84, 80, 77, 73]), AS_OF)
    assert falling.significant
    d = plan_threshold_update(
        limit_kind="low", inputs=inputs, risk=None, trend=falling, previous=None
    )
    assert d.threshold.unclamped_mg_dl == Decimal("75.0")
    assert d.threshold.threshold_mg_dl == NO_DM_LOW.hard_max_mg_dl


def test_unscored_profile_has_no_risk_adjustment() -> None:
    t = assemble_threshold(
        DM_FASTING,
        demo_adj=Decimal(0),
        risk_adj=Decimal(0),
        trend_adj=Decimal(0),
        reason="initial",
        risk=None,
    )
    assert t.model_version == UNSCORED_MODEL_VERSION
    assert t.risk_tier is None
    assert t.risk_score is None


# ---------------------------------------------------------------------------
# R2: trend analysis (14-day OLS + EWMA)
# ---------------------------------------------------------------------------


def test_fewer_than_7_readings_is_not_significant() -> None:
    trend = analyse_trend(_series(RISING[1:]), AS_OF)
    assert trend.n_readings == MIN_TREND_READINGS - 1
    assert trend.slope_mg_dl_per_day is None
    assert trend.p_value is None
    assert not trend.significant


def test_exactly_7_rising_readings_is_significant() -> None:
    trend = analyse_trend(_series(RISING), AS_OF)
    assert trend.n_readings == MIN_TREND_READINGS
    assert trend.slope_mg_dl_per_day is not None
    assert trend.slope_mg_dl_per_day == pytest.approx(3.0, abs=0.3)
    assert trend.p_value is not None
    assert trend.p_value < 0.05
    assert trend.significant


def test_non_significant_slope() -> None:
    trend = analyse_trend(_series(NOISY_FLAT), AS_OF)
    assert trend.n_readings == len(NOISY_FLAT)
    assert trend.slope_mg_dl_per_day is not None
    assert trend.slope_mg_dl_per_day != 0
    assert trend.p_value is not None
    assert trend.p_value >= 0.05
    assert not trend.significant


def test_constant_readings_have_zero_slope() -> None:
    trend = analyse_trend(_series([130] * 8), AS_OF)
    assert trend.slope_mg_dl_per_day == 0
    assert not trend.significant


def test_readings_outside_window_are_ignored() -> None:
    old = [_reading(200 + i, days_ago=TREND_WINDOW_DAYS + 1 + i) for i in range(5)]
    on_edge = [_reading(250, days_ago=TREND_WINDOW_DAYS)]
    future = [_reading(300, days_ago=-1)]
    recent = _series(RISING[1:])
    trend = analyse_trend(old + on_edge + future + recent, AS_OF)
    assert trend.n_readings == len(recent)
    assert not trend.significant


def test_ewma_weights_recent_readings() -> None:
    trend = analyse_trend(_series(RISING), AS_OF)
    assert trend.ewma_mg_dl is not None
    mean = sum(RISING) / len(RISING)
    assert mean < trend.ewma_mg_dl < RISING[-1]


def test_no_readings() -> None:
    trend = analyse_trend([], AS_OF)
    assert trend.n_readings == 0
    assert trend.ewma_mg_dl is None
    assert not trend.significant


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(ThresholdComputationError, match="timezone"):
        analyse_trend([], datetime(2026, 11, 15))


# ---------------------------------------------------------------------------
# R2: revision decisions
# ---------------------------------------------------------------------------


def _initial(kind: GlucoseLimitKind = "fasting_high") -> ThresholdComputation:
    d = plan_threshold_update(
        limit_kind=kind,
        inputs=ADULT_DM,
        risk=_risk("low"),
        trend=analyse_trend([], AS_OF),
        previous=None,
    )
    assert d.action == "initial"
    return d.threshold


def test_first_threshold_is_initial_without_trend() -> None:
    d = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_DM,
        risk=_risk("moderate"),
        trend=analyse_trend(_series(RISING[1:]), AS_OF),
        previous=None,
    )
    assert d.action == "initial"
    assert d.changed
    assert d.threshold.reason == "initial"
    assert d.threshold.trend_adj_mg_dl == 0
    assert d.threshold.risk_adj_mg_dl == 0
    assert d.threshold.risk_tier == "moderate"  # recorded for audit even though unused
    assert d.threshold.threshold_mg_dl == Decimal("130.0")


def test_fewer_than_7_readings_keeps_previous() -> None:
    previous = _initial()
    d = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_DM,
        risk=_risk("low"),
        trend=analyse_trend(_series(RISING[1:]), AS_OF),
        previous=previous,
    )
    assert d.action == "kept"
    assert not d.changed
    assert d.threshold is previous


def test_non_significant_slope_keeps_previous() -> None:
    revised = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_DM,
        risk=_risk("low"),
        trend=analyse_trend(_series(RISING), AS_OF),
        previous=_initial(),
    ).threshold
    assert revised.trend_adj_mg_dl < 0
    d = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_DM,
        risk=_risk("low"),
        trend=analyse_trend(_series(NOISY_FLAT), AS_OF),
        previous=revised,
    )
    assert d.action == "kept"
    assert d.threshold is revised


def test_significant_rise_revises_threshold() -> None:
    previous = _initial()
    d = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_DM,
        risk=_risk("low"),
        trend=analyse_trend(_series(RISING), AS_OF),
        previous=previous,
    )
    assert d.action == "trend_revision"
    assert d.threshold.reason == "trend_revision"
    assert d.threshold.trend_adj_mg_dl == Decimal(-10)
    assert d.threshold.threshold_mg_dl == Decimal("120.0")
    assert d.threshold.trend_p_value is not None
    assert d.threshold.trend_p_value < Decimal("0.05")


def test_significant_trend_with_no_change_keeps_previous() -> None:
    first = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_DM,
        risk=_risk("low"),
        trend=analyse_trend(_series(RISING), AS_OF),
        previous=_initial(),
    ).threshold
    again = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_DM,
        risk=_risk("low"),
        trend=analyse_trend(_series(RISING), AS_OF),
        previous=first,
    )
    assert again.action == "kept"


def test_risk_change_without_trend_is_profile_change() -> None:
    initial = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_NO_DM,
        risk=_risk("low"),
        trend=analyse_trend([], AS_OF),
        previous=None,
    ).threshold
    revised = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_NO_DM,
        risk=_risk("low"),
        trend=analyse_trend(_series(RISING), AS_OF),
        previous=initial,
    ).threshold
    assert revised.threshold_mg_dl == Decimal("102.0")
    d = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_NO_DM,
        risk=_risk("moderate"),
        trend=analyse_trend(_series(NOISY_FLAT), AS_OF),
        previous=revised,
    )
    assert d.action == "profile_change"
    assert d.threshold.risk_adj_mg_dl == Decimal("-6.0")
    # The trend component and the statistics that justified it are carried forward.
    assert d.threshold.trend_adj_mg_dl == revised.trend_adj_mg_dl
    assert d.threshold.trend_p_value == revised.trend_p_value
    assert d.threshold.trend_slope_mg_dl_per_day == revised.trend_slope_mg_dl_per_day
    assert d.threshold.ewma_mg_dl == revised.ewma_mg_dl
    assert d.threshold.threshold_mg_dl == Decimal("100.0")  # 112 - 6 - 10 = 96 -> clamp


def test_risk_change_for_diabetic_user_keeps_previous() -> None:
    d = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_DM,
        risk=_risk("high"),
        trend=analyse_trend([], AS_OF),
        previous=_initial(),
    )
    assert d.action == "kept"


def test_becoming_complex_is_profile_change() -> None:
    d = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=_older_dm(70, 3),
        risk=_risk("low"),
        trend=analyse_trend([], AS_OF),
        previous=_initial(),
    )
    assert d.action == "profile_change"
    assert d.threshold.demo_adj_mg_dl == Decimal(10)


def test_ewma_is_recorded() -> None:
    trend = analyse_trend(_series(RISING), AS_OF)
    assert trend.ewma_mg_dl is not None
    d = plan_threshold_update(
        limit_kind="fasting_high", inputs=ADULT_DM, risk=None, trend=trend, previous=None
    )
    assert d.threshold.ewma_mg_dl == Decimal(str(round(trend.ewma_mg_dl, 1)))


def test_ewma_is_empty_without_readings() -> None:
    assert _initial().ewma_mg_dl is None


def test_new_model_version_with_same_tier_keeps_previous() -> None:
    previous = _initial()
    retrained = RiskAssessment(probability=0.1, tier="low", flagged=False, model_version="v2")
    d = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ADULT_DM,
        risk=retrained,
        trend=analyse_trend([], AS_OF),
        previous=previous,
    )
    assert d.action == "kept"


def test_diabetes_status_change_is_profile_change() -> None:
    previous = _initial()
    d = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=ThresholdInputs(diabetes_status="no_diabetes", age_years=50, sex=1),
        risk=_risk("low"),
        trend=analyse_trend([], AS_OF),
        previous=previous,
    )
    assert d.action == "profile_change"
    assert d.threshold.diabetes_status == "no_diabetes"
    assert d.threshold.base_mg_dl == Decimal("112.0")


def test_previous_for_another_limit_is_rejected() -> None:
    with pytest.raises(ThresholdComputationError, match="low"):
        plan_threshold_update(
            limit_kind="fasting_high",
            inputs=ADULT_DM,
            risk=None,
            trend=analyse_trend([], AS_OF),
            previous=_initial("low"),
        )


# ---------------------------------------------------------------------------
# Service: refresh through a repository (fake, no database)
# ---------------------------------------------------------------------------


class FakeRepository:
    """In-memory stand-in for the SQL repository."""

    def __init__(self, readings: Sequence[ReadingPoint]) -> None:
        self.readings = list(readings)
        self.rows: list[StoredThreshold] = []
        self.since: datetime | None = None

    def current_thresholds(self, user_id: uuid.UUID) -> dict[GlucoseLimitKind, StoredThreshold]:
        current: dict[GlucoseLimitKind, StoredThreshold] = {}
        for row in self.rows:  # appended in time order, so the last one wins
            if row.user_id == user_id:
                current[row.limit_kind] = row
        return current

    def readings_since(self, user_id: uuid.UUID, since: datetime) -> list[ReadingPoint]:
        self.since = since
        return [r for r in self.readings if r.measured_at > since]

    def save_threshold(
        self,
        user_id: uuid.UUID,
        computation: ThresholdComputation,
        *,
        effective_from: datetime,
        batch_run_id: uuid.UUID | None = None,
    ) -> StoredThreshold:
        row = StoredThreshold(
            **computation.model_dump(),
            id=uuid.uuid4(),
            user_id=user_id,
            effective_from=effective_from,
            batch_run_id=batch_run_id,
        )
        self.rows.append(row)
        return row


def _refresh(
    repo: FakeRepository, user: uuid.UUID, as_of: datetime = AS_OF
) -> Mapping[GlucoseLimitKind, th.ThresholdDecision]:
    return refresh_user_thresholds(
        repo, user_id=user, inputs=ADULT_DM, risk=_risk("low"), as_of=as_of, batch_run_id=None
    )


def test_refresh_saves_every_limit_once() -> None:
    user = uuid.uuid4()
    run = uuid.uuid4()
    repo = FakeRepository([])
    decisions = refresh_user_thresholds(
        repo, user_id=user, inputs=ADULT_DM, risk=_risk("low"), as_of=AS_OF, batch_run_id=run
    )
    assert set(decisions) == {"fasting_high", "post_meal_high", "low"}
    assert all(d.action == "initial" for d in decisions.values())
    assert len(repo.rows) == 3
    assert {r.batch_run_id for r in repo.rows} == {run}
    assert {r.effective_from for r in repo.rows} == {AS_OF}
    assert repo.since == AS_OF - timedelta(days=TREND_WINDOW_DAYS)


def test_refresh_is_idempotent_without_new_information() -> None:
    user = uuid.uuid4()
    repo = FakeRepository(_series(NOISY_FLAT))
    _refresh(repo, user)
    decisions = _refresh(repo, user, AS_OF + timedelta(hours=1))
    assert all(d.action == "kept" for d in decisions.values())
    assert len(repo.rows) == 3


def test_refresh_uses_matching_reading_context() -> None:
    user = uuid.uuid4()
    repo = FakeRepository([])
    _refresh(repo, user)
    repo.readings = _series(RISING, "post_meal")
    decisions = _refresh(repo, user, AS_OF + timedelta(hours=1))
    assert decisions["post_meal_high"].action == "trend_revision"
    assert decisions["post_meal_high"].threshold.trend_adj_mg_dl == Decimal(-10)
    assert decisions["fasting_high"].action == "kept"
    assert decisions["low"].action == "kept"
    assert len(repo.rows) == 4


def test_current_values_feed_classification() -> None:
    user = uuid.uuid4()
    repo = FakeRepository([])
    _refresh(repo, user)
    values = threshold_values(repo.current_thresholds(user))
    assert values == {
        "fasting_high": Decimal("130.0"),
        "post_meal_high": Decimal("180.0"),
        "low": Decimal("70.0"),
    }
    assert classify_reading(_reading(Decimal("123.5")), values, band=BAND).state == (
        "pre_breach_high"
    )


def test_readings_at_one_instant_have_no_slope() -> None:
    trend = analyse_trend([_reading(100 + i) for i in range(MIN_TREND_READINGS)], AS_OF)
    assert trend.n_readings == MIN_TREND_READINGS
    assert trend.slope_mg_dl_per_day is None
    assert not trend.significant


def test_threshold_outside_clamp_is_rejected() -> None:
    valid = _initial()
    with pytest.raises(ValueError, match="outside"):
        ThresholdComputation.model_validate(
            {**valid.model_dump(), "threshold_mg_dl": Decimal("99.9")}
        )
