"""Personal glucose thresholds (R2) and the pre-breach band check (R3).

For every user and limit kind (``fasting_high``, ``post_meal_high``, ``low``)::

    threshold = clamp(base + demo_adj + risk_adj + trend_adj, hard_min, hard_max)

* ``base`` and the clamp come from :mod:`app.core.clinical_limits` (cited, never inline).
* ``demo_adj`` (age, sex), ``risk_adj`` (risk-model tier) and ``trend_adj`` (14-day
  OLS slope) are each bounded below; the clamp is the final safety net, so no
  combination of adjustments can push a limit outside its cited range.

Layering: everything here except :func:`refresh_user_thresholds` is a pure function
(no I/O, no clock), so the maths is tested without a database. Persistence goes
through the :class:`ThresholdRepository` protocol; the SQL implementation lives in
:mod:`app.db.threshold_repository`.

Glucose values are :class:`~decimal.Decimal` throughout: they are ``numeric(5, 1)``
in Postgres, and the band check must be exact at its boundaries (in binary floating
point ``0.95 * 130`` is not ``123.5``).
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Final, Literal, Protocol, Self, get_args

import numpy as np
import numpy.typing as npt
import statsmodels.api as sm
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.core.clinical_limits import (
    COMPLEX_HEALTH_FASTING_HIGH_MG_DL,
    COMPLEX_HEALTH_MIN_CONDITIONS,
    FASTING_HIGH_DM_MG_DL,
    HYPO_LEVEL_2_MG_DL,
    OLDER_ADULT_MIN_AGE_YEARS,
    DiabetesStatus,
    GlucoseLimit,
    GlucoseLimitKind,
    LimitDirection,
    get_glucose_limit,
)
from app.core.config import get_settings
from app.core.exceptions import ThresholdComputationError
from app.db.models import ReadingContext, ThresholdReason
from app.ml.predict import RiskAssessment, RiskTier

logger = logging.getLogger(__name__)

BandState = Literal["normal", "pre_breach_high", "pre_breach_low", "breach_high", "breach_low"]
DecisionAction = Literal["initial", "trend_revision", "profile_change", "kept"]

ALL_LIMIT_KINDS: Final[tuple[GlucoseLimitKind, ...]] = get_args(GlucoseLimitKind)

# Stored precision of thresholds_history (0001_core_schema.sql): glucose numeric(5, 1),
# risk_score numeric(5, 4), slope numeric(7, 3), p-value numeric(6, 5). Rounding here
# means the value we compare and log is exactly the value the database keeps.
_MG_DL_STEP: Final = Decimal("0.1")
_RISK_SCORE_STEP: Final = Decimal("0.0001")
_SLOPE_STEP: Final = Decimal("0.001")
_P_VALUE_STEP: Final = Decimal("0.00001")
_ZERO: Final = Decimal(0)

# thresholds_history.model_version is NOT NULL. A user whose BRFSS profile is not yet
# complete cannot be scored, gets no risk adjustment, and is labelled with this.
UNSCORED_MODEL_VERSION: Final = "unscored"

# ---------------------------------------------------------------------------
# Demographic adjustment (R2)
# ---------------------------------------------------------------------------

# ADA-13-2026 Table 13.2 (constants in clinical_limits): healthy older adults keep the
# standard 80-130 fasting goal, so they get no adjustment. Older adults with
# complex/intermediate health have a 90-150 goal. Our category comes from a survey
# proxy (see COMPLEX_HEALTH_PROXY_FIELDS), so the fasting limit relaxes only half-way
# from the standard 130 toward 150, i.e. +10 -> 140. Table 13.2 gives no post-meal
# goal, so post-meal is unchanged. The complex goal starts at 90 (not 80), i.e. lows
# matter more: the low limit rises by +5 (70 -> 75), half of its 70-80 clamp, leaving
# room for the trend term.
_COMPLEX_OLDER_ADULT_ADJ_MG_DL: Final[Mapping[GlucoseLimitKind, Decimal]] = MappingProxyType(
    {
        "fasting_high": (
            Decimal(str(COMPLEX_HEALTH_FASTING_HIGH_MG_DL)) - Decimal(str(FASTING_HIGH_DM_MG_DL))
        )
        / 2,
        "post_meal_high": _ZERO,
        "low": Decimal(5),
    }
)
# PROXY for ADA-13-2026 "complex/intermediate" health (multiple = at least 3 coexisting
# chronic illnesses, or >= 2 ADL impairments, or cognitive impairment) built from the
# BRFSS answers we hold. Team decision 2026-11-15. Limits of the proxy: hypertension,
# myocardial infarction (here: coronary disease or heart attack) and stroke are on
# ADA's example list; high cholesterol is not; difficulty walking is a functional
# limitation standing in for the ADL criterion. Unknown answers (None) count as "no",
# so an incomplete profile falls back to the standard (stricter) targets.
COMPLEX_HEALTH_PROXY_FIELDS: Final[tuple[str, ...]] = (
    "high_bp",
    "high_chol",
    "heart_disease_or_attack",
    "stroke",
    "diff_walk",
)
# ADA Standards of Care 2025 set no sex-specific glycaemic goals, so the sex term is 0
# for both codes. It is kept as an explicit term so the formula's shape matches the
# spec and a cited value can be added in one place.
_SEX_ADJ_MG_DL: Final[Mapping[int, Decimal]] = MappingProxyType({0: _ZERO, 1: _ZERO})
# Bounds on the *total* demographic term, per direction. Demographics only ever
# relax an upper limit (older adults) or raise a low limit (earlier hypo warning);
# the maxima equal the largest cited step above so a future term cannot stack on it.
_DEMO_ADJ_BOUNDS: Final[Mapping[LimitDirection, tuple[Decimal, Decimal]]] = MappingProxyType(
    {"upper": (_ZERO, Decimal(10)), "lower": (_ZERO, Decimal(5))}
)

# ---------------------------------------------------------------------------
# Risk-tier adjustment (R2)
# ---------------------------------------------------------------------------

# The BRFSS model predicts whether someone *has* prediabetes/diabetes - it is a
# screening model. So the tier only personalises limits for users WITHOUT diabetes,
# and only *upper* limits (it says nothing about hypoglycaemia). For users WITH
# diabetes the question it answers is already settled; it does not predict how well
# their glucose is controlled, so it must not move their limits (risk_adj = 0).
#
# For users without diabetes the tier moves the upper limit from its base toward the
# clamp floor by this fraction of (base - hard_min): low stays at the base, moderate
# goes half-way, high reaches the floor (fasting 112 -> 106 -> 100, post-meal
# 160 -> 150 -> 140). Bound: the fraction is at most 1, so this term alone can never
# go below the cited floor (ADA-2 normal fasting / 2-h OGTT values).
RISK_TIGHTENING_FRACTION: Final[Mapping[RiskTier, Decimal]] = MappingProxyType(
    {"low": _ZERO, "moderate": Decimal("0.5"), "high": Decimal(1)}
)

# ---------------------------------------------------------------------------
# Trend adjustment (R2 revision)
# ---------------------------------------------------------------------------

# Rolling window for the trend, from the threshold-engine spec: two weeks smooths
# day-to-day variation while still reacting within one clinic follow-up interval.
TREND_WINDOW_DAYS: Final = 14
# Spec: revise only with >= 7 readings (about one every other day in the window);
# fewer gives an OLS slope whose p-value is too unstable to act on.
MIN_TREND_READINGS: Final = 7
# Spec: conventional 5% significance level for the slope coefficient.
TREND_P_VALUE_MAX: Final = 0.05
# EWMA half-life: a reading 7 days old weighs ~1/5 of today's (2 ** (-7 / 3)), so the
# smoothed level follows the last few days without being driven by one reading.
EWMA_HALFLIFE_DAYS: Final = 3.0
_SECONDS_PER_DAY: Final = 86_400
# Move the limit by half the change the slope projects over one window: a sustained
# rise brings alerts forward, but the limit never chases the readings all the way.
_TREND_GAIN: Final = Decimal("0.5")
# Bounds: trends only ever *tighten* a limit, toward the side the readings move.
# Upper limits move by at most 10 (same order as the risk term). The low limit's clamp
# is only 10 wide (70-80), so its trend term is capped at half of that.
_TREND_ADJ_MAX_UPPER_MG_DL: Final = Decimal(10)
_TREND_ADJ_MAX_LOWER_MG_DL: Final = Decimal(5)
# Which readings feed each limit's trend. Mixing contexts would make the slope track
# the mix (more post-meal readings this week -> fake "rise"), so each limit uses one.
# Low uses fasting: the most comparable day-to-day state, and where lows are caught.
TREND_CONTEXTS: Final[Mapping[GlucoseLimitKind, frozenset[ReadingContext]]] = MappingProxyType(
    {
        "fasting_high": frozenset({"fasting"}),
        "post_meal_high": frozenset({"post_meal"}),
        "low": frozenset({"fasting"}),
    }
)

# ---------------------------------------------------------------------------
# Band check (R3)
# ---------------------------------------------------------------------------

# Which upper limit a reading is judged against (readings table comment, 0001 schema).
# Random readings have no cited upper target, so only the low limit applies.
_UPPER_KIND_FOR_CONTEXT: Final[Mapping[ReadingContext, GlucoseLimitKind | None]] = MappingProxyType(
    {"fasting": "fasting_high", "post_meal": "post_meal_high", "random": None}
)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


class ThresholdInputs(BaseModel):
    """The profile fields the threshold engine needs.

    Field names match ``profiles`` columns, so a profile row validates directly
    (``ThresholdInputs.model_validate(profile)``); a profile without a diabetes status
    is rejected, because no limit set can be chosen for it.

    Attributes:
        diabetes_status: Selects the cited limit set.
        age_years: Age in years; ``None`` if not given (no age adjustment).
        sex: BRFSS coding, 0 = female, 1 = male; ``None`` if not given.
        high_bp: Told they have high blood pressure.
        high_chol: Told they have high cholesterol.
        heart_disease_or_attack: Coronary heart disease or heart attack.
        stroke: Ever had a stroke.
        diff_walk: Serious difficulty walking or climbing stairs.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    diabetes_status: DiabetesStatus
    # Same adult range as profiles.age_years (BRFSS covers adults only).
    age_years: int | None = Field(default=None, ge=18, le=120)
    sex: Literal[0, 1] | None = None
    high_bp: bool | None = None
    high_chol: bool | None = None
    heart_disease_or_attack: bool | None = None
    stroke: bool | None = None
    diff_walk: bool | None = None

    @property
    def complex_health_conditions(self) -> int:
        """Count the complex-health proxy conditions answered "yes".

        Returns:
            How many of :data:`COMPLEX_HEALTH_PROXY_FIELDS` are ``True``.
        """
        return sum(getattr(self, name) is True for name in COMPLEX_HEALTH_PROXY_FIELDS)

    @property
    def is_complex_older_adult(self) -> bool:
        """Whether ADA-13's complex/intermediate older-adult goals apply (proxy).

        Returns:
            ``True`` for people with diabetes aged >= 65 with >= 3 proxy conditions.
        """
        return (
            self.diabetes_status == "diabetes"
            and self.age_years is not None
            and self.age_years >= OLDER_ADULT_MIN_AGE_YEARS
            and self.complex_health_conditions >= COMPLEX_HEALTH_MIN_CONDITIONS
        )


class ReadingPoint(BaseModel):
    """One glucose reading as the engine sees it.

    Attributes:
        value_mg_dl: Glucose in mg/dL.
        context: ``fasting``, ``post_meal`` or ``random``.
        measured_at: When it was measured; must be timezone-aware.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    value_mg_dl: Decimal = Field(gt=0)
    context: ReadingContext
    measured_at: AwareDatetime


class TrendAnalysis(BaseModel):
    """Trend of one user's readings over the rolling window.

    Attributes:
        n_readings: Readings inside the window.
        slope_mg_dl_per_day: OLS slope; ``None`` if too few readings or no time spread.
        p_value: Two-sided p-value of the slope; ``None`` if undefined.
        ewma_mg_dl: Exponentially weighted level at the latest reading; ``None`` if
            there are no readings.
        significant: ``n_readings >= 7`` and ``p_value < 0.05``.
    """

    model_config = ConfigDict(frozen=True)

    n_readings: int = Field(ge=0)
    slope_mg_dl_per_day: float | None
    p_value: float | None = Field(ge=0, le=1)
    ewma_mg_dl: float | None
    significant: bool


class ThresholdComputation(BaseModel):
    """A computed personal threshold with every component (mirrors ``thresholds_history``).

    Attributes:
        limit_kind: Which limit this is.
        direction: ``upper`` or ``lower``.
        diabetes_status: Population whose cited base was used.
        base_mg_dl: Cited starting value.
        demo_adj_mg_dl: Bounded age/sex adjustment.
        risk_adj_mg_dl: Bounded risk-tier adjustment.
        trend_adj_mg_dl: Bounded trend adjustment.
        unclamped_mg_dl: ``base + demo + risk + trend`` before clamping.
        hard_min_mg_dl: Lower clamp bound.
        hard_max_mg_dl: Upper clamp bound.
        threshold_mg_dl: The personal threshold after clamping.
        risk_score: Model probability that drove ``risk_adj``.
        risk_tier: Model tier that drove ``risk_adj``.
        model_version: Risk-model version, or ``"unscored"``.
        trend_slope_mg_dl_per_day: Slope behind ``trend_adj``.
        trend_p_value: p-value behind ``trend_adj``.
        trend_n_readings: Reading count behind ``trend_adj``.
        ewma_mg_dl: EWMA level of the same readings as the trend fields.
        reason: Why this row was written.
        source_citation: Citation of the clinical limit, shown on the dashboard.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    limit_kind: GlucoseLimitKind
    direction: LimitDirection
    diabetes_status: DiabetesStatus
    base_mg_dl: Decimal
    demo_adj_mg_dl: Decimal
    risk_adj_mg_dl: Decimal
    trend_adj_mg_dl: Decimal
    unclamped_mg_dl: Decimal
    hard_min_mg_dl: Decimal
    hard_max_mg_dl: Decimal
    threshold_mg_dl: Decimal
    risk_score: Decimal | None = None
    risk_tier: RiskTier | None = None
    model_version: str = Field(min_length=1)
    trend_slope_mg_dl_per_day: Decimal | None = None
    trend_p_value: Decimal | None = None
    trend_n_readings: int | None = Field(default=None, ge=0)
    ewma_mg_dl: Decimal | None = Field(default=None, gt=0)
    reason: ThresholdReason
    source_citation: str = Field(min_length=1)

    @model_validator(mode="after")
    def _threshold_within_clamp(self) -> Self:
        """Enforce the same safety net as the table's CHECK constraint.

        Returns:
            The validated computation.

        Raises:
            ValueError: If the threshold lies outside ``[hard_min, hard_max]``.
        """
        if not self.hard_min_mg_dl <= self.threshold_mg_dl <= self.hard_max_mg_dl:
            raise ValueError(
                f"{self.limit_kind}: threshold {self.threshold_mg_dl} outside "
                f"[{self.hard_min_mg_dl}, {self.hard_max_mg_dl}]"
            )
        return self

    def components(self) -> tuple[object, ...]:
        """Return the values that define this threshold, for change detection.

        Model version, risk score and trend statistics are deliberately excluded: if
        they change but every component stays the same, the threshold is unchanged.

        Returns:
            Status, base, the three adjustments and the clamp bounds.
        """
        return (
            self.diabetes_status,
            self.base_mg_dl,
            self.demo_adj_mg_dl,
            self.risk_adj_mg_dl,
            self.trend_adj_mg_dl,
            self.hard_min_mg_dl,
            self.hard_max_mg_dl,
        )


class StoredThreshold(ThresholdComputation):
    """A :class:`ThresholdComputation` as saved in ``thresholds_history``.

    Attributes:
        id: Row id.
        user_id: Owner.
        effective_from: When the threshold took effect.
        batch_run_id: Batch run that wrote it, if any.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    effective_from: AwareDatetime
    batch_run_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ThresholdDecision:
    """Outcome of a revision check for one limit.

    Attributes:
        action: ``initial``, ``trend_revision``, ``profile_change`` or ``kept``.
        threshold: The new threshold, or the previous one when ``kept``.
    """

    action: DecisionAction
    threshold: ThresholdComputation

    @property
    def changed(self) -> bool:
        """Whether a new row must be written.

        Returns:
            ``True`` unless the previous threshold was kept.
        """
        return self.action != "kept"


@dataclass(frozen=True)
class BandResult:
    """Classification of one reading against a user's thresholds (R3).

    Attributes:
        state: Band state.
        limit_kind: The limit the state refers to; ``None`` when ``normal``.
        threshold_mg_dl: That limit's value; ``None`` when ``normal``.
        escalate: Reading is Level 2 hypoglycaemia (< 54 mg/dL): urgent.
    """

    state: BandState
    limit_kind: GlucoseLimitKind | None
    threshold_mg_dl: Decimal | None
    escalate: bool

    @property
    def needs_practitioner(self) -> bool:
        """Whether a practitioner must be told (threshold-engine skill, R4).

        Every breach escalates (``advisories`` CHECK: breach implies ``escalated``);
        Level 2 hypoglycaemia always does.

        Returns:
            ``True`` for any ``breach_*`` state or when :attr:`escalate` is set.
        """
        return self.escalate or self.state in ("breach_high", "breach_low")


class ThresholdRepository(Protocol):
    """Persistence the threshold service needs; see ``app.db.threshold_repository``."""

    def current_thresholds(self, user_id: uuid.UUID) -> dict[GlucoseLimitKind, StoredThreshold]:
        """Return the user's current threshold per limit kind.

        Args:
            user_id: The user.

        Returns:
            Latest stored threshold for each limit kind that has one.
        """
        ...

    def readings_since(self, user_id: uuid.UUID, since: datetime) -> list[ReadingPoint]:
        """Return the user's readings measured strictly after ``since``.

        Args:
            user_id: The user.
            since: Exclusive lower bound on ``measured_at``.

        Returns:
            Readings, oldest first.
        """
        ...

    def save_threshold(
        self,
        user_id: uuid.UUID,
        computation: ThresholdComputation,
        *,
        effective_from: datetime,
        batch_run_id: uuid.UUID | None = None,
    ) -> StoredThreshold:
        """Append a computed threshold to the history.

        Args:
            user_id: The user.
            computation: The threshold and its components.
            effective_from: When it takes effect.
            batch_run_id: Batch run writing it, if any.

        Returns:
            The stored row.
        """
        ...


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Decimal | float) -> Decimal:
    """Convert to Decimal without importing binary floating-point error.

    Args:
        value: A Decimal or float.

    Returns:
        ``value`` as a Decimal (``Decimal(str(x))`` for floats, so 0.95 stays 0.95).
    """
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _quantize(value: Decimal, step: Decimal) -> Decimal:
    """Round half-up to the stored precision and normalise ``-0`` to ``0``.

    Args:
        value: Value to round.
        step: Precision, e.g. ``Decimal("0.1")``.

    Returns:
        The rounded value.
    """
    return value.quantize(step, rounding=ROUND_HALF_UP) + 0


def _clip(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    """Limit ``value`` to ``[low, high]``.

    Args:
        value: Value to limit.
        low: Inclusive minimum.
        high: Inclusive maximum.

    Returns:
        The clipped value.
    """
    return min(max(value, low), high)


def _require_aware(moment: datetime, name: str) -> None:
    """Reject naive datetimes, which would silently shift the trend window.

    Args:
        moment: Datetime to check.
        name: Argument name for the error message.

    Raises:
        ThresholdComputationError: If ``moment`` has no timezone.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ThresholdComputationError(f"{name} must be timezone-aware, got {moment!r}")


# ---------------------------------------------------------------------------
# R2: adjustments
# ---------------------------------------------------------------------------


def demographic_adjustment(limit: GlucoseLimit, inputs: ThresholdInputs) -> Decimal:
    """Bounded age + sex adjustment for one limit.

    The age term applies only to older adults with diabetes whose profile indicates
    complex/intermediate health (:attr:`ThresholdInputs.is_complex_older_adult`).
    Healthy older adults keep the standard targets (ADA-13-2026 Table 13.2), and
    the diagnostic cut-offs used for people without diabetes (ADA-2) do not change
    with age.

    Args:
        limit: The cited limit being personalised.
        inputs: The user's profile fields.

    Returns:
        The adjustment in mg/dL, within the per-direction demographic bounds.
    """
    age_adj = _COMPLEX_OLDER_ADULT_ADJ_MG_DL[limit.kind] if inputs.is_complex_older_adult else _ZERO
    sex_adj = _ZERO if inputs.sex is None else _SEX_ADJ_MG_DL[inputs.sex]
    low, high = _DEMO_ADJ_BOUNDS[limit.direction]
    return _clip(age_adj + sex_adj, low, high)


def risk_adjustment(limit: GlucoseLimit, tier: RiskTier | None) -> Decimal:
    """Bounded risk-tier adjustment for users without diabetes.

    ``adj = -fraction(tier) * (base - hard_min)``: low 0, moderate half-way to the
    floor, high to the floor.

    Args:
        limit: The cited limit being personalised.
        tier: Risk tier from :meth:`app.ml.predict.RiskModel.assess`, or ``None``
            if the profile could not be scored.

    Returns:
        ``0`` for users with diabetes, lower limits or no tier; otherwise a value
        in ``[-(base - hard_min), 0]``, rounded to 0.1 mg/dL.
    """
    # R2: BRFSS predicts diabetes presence, not glycaemic-control risk -> no effect
    # once a user has diabetes (see RISK_TIGHTENING_FRACTION).
    if tier is None or limit.status == "diabetes" or limit.direction == "lower":
        return _ZERO
    headroom = _to_decimal(limit.base_mg_dl) - _to_decimal(limit.hard_min_mg_dl)
    fraction = _clip(RISK_TIGHTENING_FRACTION[tier], _ZERO, Decimal(1))
    return _quantize(-fraction * headroom, _MG_DL_STEP)


def trend_adjustment(limit: GlucoseLimit, trend: TrendAnalysis) -> Decimal:
    """Bounded trend adjustment: tighten a limit the readings are moving toward.

    ``adj = 0.5 * slope * 14`` (half the change projected over the window), signed to
    tighten: a rise lowers an upper limit, a fall raises the low limit. A trend away
    from a limit never relaxes it.

    Args:
        limit: The cited limit being personalised.
        trend: Trend of the readings relevant to this limit.

    Returns:
        ``0`` if the trend is not significant; otherwise a value in ``[-10, 0]`` for
        upper limits or ``[0, 5]`` for the low limit, rounded to 0.1 mg/dL.
    """
    if not trend.significant or trend.slope_mg_dl_per_day is None:
        return _ZERO
    projected = _to_decimal(trend.slope_mg_dl_per_day) * TREND_WINDOW_DAYS
    if limit.direction == "upper":
        if projected <= 0:
            return _ZERO
        return -_quantize(min(_TREND_GAIN * projected, _TREND_ADJ_MAX_UPPER_MG_DL), _MG_DL_STEP)
    if projected >= 0:
        return _ZERO
    return _quantize(min(_TREND_GAIN * -projected, _TREND_ADJ_MAX_LOWER_MG_DL), _MG_DL_STEP)


# ---------------------------------------------------------------------------
# R2: trend analysis
# ---------------------------------------------------------------------------


def _time_weighted_ewma(days: npt.NDArray[np.float64], values: npt.NDArray[np.float64]) -> float:
    """Exponentially weighted mean level at the latest reading.

    Each reading weighs ``0.5 ** (age_days / EWMA_HALFLIFE_DAYS)``, where its age is
    measured back from the latest reading. Weighting by elapsed time (not by reading
    count) keeps irregular WhatsApp reporting from distorting the level; this equals
    pandas' ``ewm(halflife=..., times=..., adjust=True)``.

    Args:
        days: Time of each reading in days since the first, ascending.
        values: Glucose of each reading.

    Returns:
        The weighted mean in mg/dL.
    """
    weights = np.power(0.5, (days[-1] - days) / EWMA_HALFLIFE_DAYS)
    return float(np.sum(weights * values) / np.sum(weights))


def _ols_slope(
    days: npt.NDArray[np.float64], values: npt.NDArray[np.float64]
) -> tuple[float | None, float | None]:
    """Fit ``value = a + b * day`` and return ``b`` and its p-value.

    Args:
        days: Time of each reading in days since the first.
        values: Glucose of each reading.

    Returns:
        ``(slope, p_value)``. ``(None, None)`` if all readings share one timestamp
        (no time axis); ``(0.0, None)`` if all values are equal (p is 0/0).
    """
    if np.ptp(days) == 0:
        return None, None
    if np.ptp(values) == 0:
        return 0.0, None
    design = sm.add_constant(days, has_constant="add")
    # A noiseless line has zero residual variance; numpy would warn on t = b / 0.
    with np.errstate(divide="ignore", invalid="ignore"):
        fit = sm.OLS(values, design).fit()
    slope = float(fit.params[1])
    p_value = float(fit.pvalues[1])
    return slope, (p_value if math.isfinite(p_value) else None)


def analyse_trend(
    readings: Sequence[ReadingPoint],
    as_of: datetime,
    *,
    window_days: int = TREND_WINDOW_DAYS,
) -> TrendAnalysis:
    """Fit a 14-day OLS slope and EWMA level to a user's readings.

    Only readings with ``as_of - window_days < measured_at <= as_of`` count. The
    caller passes readings of one context (see :data:`TREND_CONTEXTS`).

    Args:
        readings: Readings in any order.
        as_of: End of the window; must be timezone-aware.
        window_days: Window length in days.

    Returns:
        The trend. ``significant`` is set only with >= 7 readings and p < 0.05.

    Raises:
        ThresholdComputationError: If ``as_of`` is naive.
    """
    _require_aware(as_of, "as_of")
    start = as_of - timedelta(days=window_days)
    window = sorted(
        (r for r in readings if start < r.measured_at <= as_of), key=lambda r: r.measured_at
    )
    n = len(window)
    if n == 0:
        return TrendAnalysis(
            n_readings=0, slope_mg_dl_per_day=None, p_value=None, ewma_mg_dl=None, significant=False
        )

    values = np.array([float(r.value_mg_dl) for r in window], dtype=np.float64)
    first = window[0].measured_at
    days = np.array(
        [(r.measured_at - first).total_seconds() / _SECONDS_PER_DAY for r in window],
        dtype=np.float64,
    )
    ewma = _time_weighted_ewma(days, values)
    if n < MIN_TREND_READINGS:
        return TrendAnalysis(
            n_readings=n, slope_mg_dl_per_day=None, p_value=None, ewma_mg_dl=ewma, significant=False
        )

    slope, p_value = _ols_slope(days, values)
    return TrendAnalysis(
        n_readings=n,
        slope_mg_dl_per_day=slope,
        p_value=p_value,
        ewma_mg_dl=ewma,
        significant=p_value is not None and p_value < TREND_P_VALUE_MAX,
    )


# ---------------------------------------------------------------------------
# R2: assembling and revising
# ---------------------------------------------------------------------------


def assemble_threshold(
    limit: GlucoseLimit,
    *,
    demo_adj: Decimal,
    risk_adj: Decimal,
    trend_adj: Decimal,
    reason: ThresholdReason,
    risk: RiskAssessment | None = None,
    trend: TrendAnalysis | None = None,
) -> ThresholdComputation:
    """Combine a cited limit and its adjustments, then clamp.

    Args:
        limit: The cited limit.
        demo_adj: Demographic adjustment.
        risk_adj: Risk-tier adjustment.
        trend_adj: Trend adjustment.
        reason: Why the threshold is being written.
        risk: Assessment behind ``risk_adj`` (recorded for audit), if any.
        trend: Trend behind ``trend_adj`` (recorded for audit), if any.

    Returns:
        The full computation, rounded to the stored precision.
    """
    base = _quantize(_to_decimal(limit.base_mg_dl), _MG_DL_STEP)
    demo = _quantize(demo_adj, _MG_DL_STEP)
    risk_term = _quantize(risk_adj, _MG_DL_STEP)
    trend_term = _quantize(trend_adj, _MG_DL_STEP)
    unclamped = base + demo + risk_term + trend_term
    # R2 safety net: whatever the ML and trend rules propose, the threshold stays
    # inside the cited clamp (GlucoseLimit.clamp is the single implementation).
    threshold = _quantize(_to_decimal(limit.clamp(float(unclamped))), _MG_DL_STEP)
    slope = trend.slope_mg_dl_per_day if trend is not None else None
    p_value = trend.p_value if trend is not None else None
    return ThresholdComputation(
        limit_kind=limit.kind,
        direction=limit.direction,
        diabetes_status=limit.status,
        base_mg_dl=base,
        demo_adj_mg_dl=demo,
        risk_adj_mg_dl=risk_term,
        trend_adj_mg_dl=trend_term,
        unclamped_mg_dl=unclamped,
        hard_min_mg_dl=_quantize(_to_decimal(limit.hard_min_mg_dl), _MG_DL_STEP),
        hard_max_mg_dl=_quantize(_to_decimal(limit.hard_max_mg_dl), _MG_DL_STEP),
        threshold_mg_dl=threshold,
        risk_score=(
            _quantize(_to_decimal(risk.probability), _RISK_SCORE_STEP) if risk is not None else None
        ),
        risk_tier=risk.tier if risk is not None else None,
        model_version=risk.model_version if risk is not None else UNSCORED_MODEL_VERSION,
        trend_slope_mg_dl_per_day=(
            _quantize(_to_decimal(slope), _SLOPE_STEP) if slope is not None else None
        ),
        trend_p_value=(
            _quantize(_to_decimal(p_value), _P_VALUE_STEP) if p_value is not None else None
        ),
        trend_n_readings=trend.n_readings if trend is not None else None,
        ewma_mg_dl=(
            _quantize(_to_decimal(trend.ewma_mg_dl), _MG_DL_STEP)
            if trend is not None and trend.ewma_mg_dl is not None
            else None
        ),
        reason=reason,
        source_citation=limit.source,
    )


def plan_threshold_update(
    *,
    limit_kind: GlucoseLimitKind,
    inputs: ThresholdInputs,
    risk: RiskAssessment | None,
    trend: TrendAnalysis,
    previous: ThresholdComputation | None,
) -> ThresholdDecision:
    """Decide whether one limit needs a new threshold, and compute it.

    Rules (threshold-engine skill):

    * No previous threshold -> ``initial`` (with the trend term only if significant).
    * Trend significant (>= 7 readings, p < 0.05) -> recompute every term
      (``trend_revision``).
    * Trend not significant -> the previous trend term is kept. If the other terms
      (risk tier, age, diabetes status) also match, the previous threshold is kept
      unchanged; if they changed, a ``profile_change`` row carries the old trend term.
    * A recomputation identical to the previous threshold is ``kept`` (nothing to log).

    Args:
        limit_kind: Which limit to decide on.
        inputs: The user's profile fields.
        risk: Current risk assessment, or ``None`` if the profile cannot be scored.
        trend: Trend of the readings relevant to this limit.
        previous: The user's current threshold for this limit, if any.

    Returns:
        The decision and the threshold to use.

    Raises:
        ThresholdComputationError: If ``previous`` belongs to another limit kind.
    """
    if previous is not None and previous.limit_kind != limit_kind:
        raise ThresholdComputationError(
            f"previous threshold is for {previous.limit_kind}, not {limit_kind}"
        )
    limit = get_glucose_limit(inputs.diabetes_status, limit_kind)
    demo_adj = demographic_adjustment(limit, inputs)
    risk_adj = risk_adjustment(limit, risk.tier if risk is not None else None)

    if previous is None or trend.significant:
        candidate = assemble_threshold(
            limit,
            demo_adj=demo_adj,
            risk_adj=risk_adj,
            trend_adj=trend_adjustment(limit, trend),
            reason="initial" if previous is None else "trend_revision",
            risk=risk,
            trend=trend,
        )
        if previous is None:
            return ThresholdDecision("initial", candidate)
        if candidate.components() == previous.components():
            return ThresholdDecision("kept", previous)
        return ThresholdDecision("trend_revision", candidate)

    # R2: not enough evidence of a trend -> keep the previous trend term (and the
    # statistics that justified it) rather than recompute it from noise.
    candidate = assemble_threshold(
        limit,
        demo_adj=demo_adj,
        risk_adj=risk_adj,
        trend_adj=previous.trend_adj_mg_dl,
        reason="profile_change",
        risk=risk,
    ).model_copy(
        update={
            "trend_slope_mg_dl_per_day": previous.trend_slope_mg_dl_per_day,
            "trend_p_value": previous.trend_p_value,
            "trend_n_readings": previous.trend_n_readings,
            "ewma_mg_dl": previous.ewma_mg_dl,
        }
    )
    if candidate.components() == previous.components():
        return ThresholdDecision("kept", previous)
    return ThresholdDecision("profile_change", candidate)


def threshold_values(
    current: Mapping[GlucoseLimitKind, ThresholdComputation],
) -> dict[GlucoseLimitKind, Decimal]:
    """Extract the threshold value per limit, ready for :func:`classify_reading`.

    Args:
        current: Current thresholds, e.g. from ``repo.current_thresholds(user_id)``.

    Returns:
        ``{limit_kind: threshold_mg_dl}``.
    """
    return {kind: t.threshold_mg_dl for kind, t in current.items()}


def refresh_user_thresholds(
    repo: ThresholdRepository,
    *,
    user_id: uuid.UUID,
    inputs: ThresholdInputs,
    risk: RiskAssessment | None,
    as_of: datetime,
    batch_run_id: uuid.UUID | None = None,
) -> dict[GlucoseLimitKind, ThresholdDecision]:
    """Recompute and persist a user's thresholds for every limit kind.

    Reads current values through the ``current_thresholds`` view and the last 14 days
    of readings, decides per limit with :func:`plan_threshold_update`, and appends a
    ``thresholds_history`` row for each change. Every change is logged. The caller
    owns the transaction (the batch job commits once for all users, R1).

    Args:
        repo: Persistence implementation.
        user_id: The user.
        inputs: The user's profile fields.
        risk: Current risk assessment, or ``None`` if unscored.
        as_of: Evaluation time; becomes ``effective_from`` of new rows.
        batch_run_id: Batch run, if called from the batch job.

    Returns:
        The decision per limit kind; changed ones carry the stored row.

    Raises:
        ThresholdComputationError: If ``as_of`` is naive.
    """
    _require_aware(as_of, "as_of")
    readings = repo.readings_since(user_id, as_of - timedelta(days=TREND_WINDOW_DAYS))
    current = repo.current_thresholds(user_id)
    decisions: dict[GlucoseLimitKind, ThresholdDecision] = {}
    for kind in ALL_LIMIT_KINDS:
        relevant = [r for r in readings if r.context in TREND_CONTEXTS[kind]]
        previous = current.get(kind)
        decision = plan_threshold_update(
            limit_kind=kind,
            inputs=inputs,
            risk=risk,
            trend=analyse_trend(relevant, as_of),
            previous=previous,
        )
        if decision.changed:
            stored = repo.save_threshold(
                user_id, decision.threshold, effective_from=as_of, batch_run_id=batch_run_id
            )
            decision = ThresholdDecision(decision.action, stored)
            logger.info(
                "threshold %s for user %s: %s -> %s mg/dL (%s; demo %s, risk %s, trend %s)",
                kind,
                user_id,
                previous.threshold_mg_dl if previous is not None else "none",
                stored.threshold_mg_dl,
                decision.action,
                stored.demo_adj_mg_dl,
                stored.risk_adj_mg_dl,
                stored.trend_adj_mg_dl,
            )
        decisions[kind] = decision
    return decisions


# ---------------------------------------------------------------------------
# R3: pre-breach band check
# ---------------------------------------------------------------------------


def _require_threshold(
    thresholds: Mapping[GlucoseLimitKind, Decimal], kind: GlucoseLimitKind
) -> Decimal:
    """Look up one threshold, failing loudly if the user has none yet.

    Args:
        thresholds: Current threshold values.
        kind: Limit needed.

    Returns:
        The value as a Decimal.

    Raises:
        ThresholdComputationError: If ``kind`` is missing.
    """
    try:
        return _to_decimal(thresholds[kind])
    except KeyError:
        raise ThresholdComputationError(
            f"no current {kind} threshold; compute thresholds before classifying readings"
        ) from None


def classify_reading(
    reading: ReadingPoint,
    thresholds: Mapping[GlucoseLimitKind, Decimal],
    *,
    band: float | None = None,
) -> BandResult:
    """Classify a reading against a user's thresholds (R3 trigger).

    With band ``b`` (``PRE_BREACH_BAND``, default 0.05)::

        upper U:  breach_high if x >= U;  pre_breach_high if (1 - b) * U <= x < U
        lower L:  breach_low  if x <= L;  pre_breach_low  if L < x <= (1 + b) * L

    Fasting readings use ``fasting_high`` + ``low``, post-meal readings
    ``post_meal_high`` + ``low``, random readings ``low`` only. Readings below
    54 mg/dL (ADA Level 2 hypoglycaemia) also set ``escalate``.

    Args:
        reading: The reading.
        thresholds: Current threshold value per limit kind.
        band: Band fraction; ``None`` reads ``pre_breach_band`` from settings.

    Returns:
        State, the limit it refers to, and the escalation flag.

    Raises:
        ThresholdComputationError: If a limit the reading needs has no threshold.
    """
    fraction = _to_decimal(get_settings().pre_breach_band if band is None else band)
    value = reading.value_mg_dl
    # ADA-6 / IHSG (clinical_limits): Level 2 hypoglycaemia is strictly < 54 mg/dL.
    escalate = value < _to_decimal(HYPO_LEVEL_2_MG_DL)

    low = _require_threshold(thresholds, "low")
    upper_kind = _UPPER_KIND_FOR_CONTEXT[reading.context]
    upper = _require_threshold(thresholds, upper_kind) if upper_kind is not None else None

    # Breaches before pre-breaches, and low before high: the two bands of a limit pair
    # only overlap for very wide bands, and then hypoglycaemia is the acute danger.
    if value <= low:
        return BandResult("breach_low", "low", low, escalate)
    if upper_kind is not None and upper is not None and value >= upper:
        return BandResult("breach_high", upper_kind, upper, escalate)
    if value <= low * (1 + fraction):
        return BandResult("pre_breach_low", "low", low, escalate)
    if upper_kind is not None and upper is not None and value >= upper * (1 - fraction):
        return BandResult("pre_breach_high", upper_kind, upper, escalate)
    return BandResult("normal", None, None, escalate)
