"""Cited clinical glucose limits and the hard clamps for personal thresholds (R2).

Every number in this module carries a source. Nothing clinical is allowed inline
anywhere else in the codebase: services import from here.

Each limit has a *base* (the published value the threshold engine starts from)
and a hard ``[min, max]`` clamp. The engine may tighten or relax a user's personal
threshold with ML/trend adjustments, but :meth:`GlucoseLimit.clamp` guarantees it
never leaves the clamp. All values are capillary/plasma glucose in mg/dL.

Sources (abbreviations used in the comments below):
    ADA-2:  American Diabetes Association. "2. Diagnosis and Classification of
            Diabetes: Standards of Care in Diabetes-2025." Diabetes Care
            2025;48(Suppl. 1):S27-S49.
    ADA-6:  American Diabetes Association. "6. Glycemic Goals and Hypoglycemia:
            Standards of Care in Diabetes-2025." Diabetes Care 2025;48(Suppl. 1).
    ADA-13: American Diabetes Association. "13. Older Adults: Standards of Care in
            Diabetes-2025." Diabetes Care 2025;48(Suppl. 1).
    TIR:    Battelino T, et al. "Clinical Targets for Continuous Glucose Monitoring
            Data Interpretation: Recommendations From the International Consensus
            on Time in Range." Diabetes Care 2019;42(8):1593-1603.
    IHSG:   International Hypoglycaemia Study Group. "Glucose Concentrations of
            Less Than 3.0 mmol/L (54 mg/dL) Should Be Reported in Clinical Trials."
            Diabetes Care 2017;40(1):155-157.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.exceptions import ClinicalLimitError

DiabetesStatus = Literal["diabetes", "no_diabetes"]
GlucoseLimitKind = Literal["fasting_high", "post_meal_high", "low"]
LimitDirection = Literal["upper", "lower"]

# ---------------------------------------------------------------------------
# Hypoglycaemia (lower limits)
# ---------------------------------------------------------------------------

# ADA-6 Table 6.4 + IHSG: Level 1 hypoglycaemia is glucose < 70 mg/dL (and >= 54);
# 70 mg/dL is the "alert value". Lower limits are never relaxed below this.
HYPO_LEVEL_1_MG_DL = 70.0
# ADA-6 Table 6.4 + IHSG: Level 2 (clinically significant) hypoglycaemia is
# glucose < 54 mg/dL. Not personalised: used for breach escalation (R3/R4).
HYPO_LEVEL_2_MG_DL = 54.0
# ADA-6 Table 6.3: preprandial target 80-130 mg/dL for many nonpregnant adults with
# diabetes. 80 is the bottom of that target, so it is the highest a low-glucose
# alert may be raised to (e.g. for hypoglycaemia-prone users) - any higher and it
# would fire on in-target readings.
PREPRANDIAL_TARGET_LOW_DM_MG_DL = 80.0

# ---------------------------------------------------------------------------
# Adults with diabetes (upper limits)
# ---------------------------------------------------------------------------

# ADA-6 Table 6.3: preprandial (fasting) capillary glucose target upper bound
# 130 mg/dL for many nonpregnant adults with diabetes.
FASTING_HIGH_DM_MG_DL = 130.0
# ADA-13 Table 13.1: the most relaxed preprandial target ADA gives is 100-180 mg/dL
# (older adults, very complex/poor health). A fasting limit is never relaxed past 180.
FASTING_HIGH_DM_HARD_MAX_MG_DL = 180.0
# ADA-6 Table 6.3: peak postprandial capillary glucose < 180 mg/dL (1-2 h after the
# start of a meal) for many nonpregnant adults with diabetes.
POST_MEAL_HIGH_DM_MG_DL = 180.0
# ADA-6 / TIR: > 250 mg/dL is Level 2 ("very high") hyperglycaemia. A post-meal
# limit is never relaxed into Level 2 territory.
LEVEL_2_HYPERGLYCAEMIA_MG_DL = 250.0

# ---------------------------------------------------------------------------
# Adults without diabetes (upper limits)
# ---------------------------------------------------------------------------

# ADA-2: normal fasting plasma glucose is < 100 mg/dL; 100-125 mg/dL is impaired
# fasting glucose (prediabetes). Also used as the tightest fasting upper limit for
# people *with* diabetes: alerting below normoglycaemia would fire on normal readings.
NORMAL_FASTING_MAX_MG_DL = 100.0
# ADA-2: impaired fasting glucose is 100-125 mg/dL; >= 126 mg/dL meets the diagnostic
# criterion for diabetes. A non-diabetic fasting limit never reaches the diagnostic cut.
IFG_MAX_MG_DL = 125.0
# ADA-2: 2-h plasma glucose during a 75-g OGTT < 140 mg/dL is normal. PROXY: ADA
# gives no post-meal target for people without diabetes; the team agreed (2026-11-15)
# to use this diagnostic OGTT cut-off as the post-meal base. Also the tightest
# post-meal limit for people with diabetes, for the same reason as above.
OGTT_2H_NORMAL_MAX_MG_DL = 140.0
# ADA-2: 2-h OGTT glucose 140-199 mg/dL is impaired glucose tolerance; >= 200 mg/dL
# is diagnostic for diabetes. PROXY as above: hard max for the non-diabetic
# post-meal limit.
IGT_MAX_MG_DL = 199.0


class GlucoseLimit(BaseModel):
    """One cited glucose limit with its hard clamp.

    Attributes:
        status: Whether this limit applies to adults with or without diabetes.
        kind: Which reading the limit applies to.
        direction: ``"upper"`` alerts on high readings, ``"lower"`` on low readings.
        base_mg_dl: Published value the threshold engine starts from.
        hard_min_mg_dl: The engine may never set the threshold below this.
        hard_max_mg_dl: The engine may never set the threshold above this.
        source: Short human-readable citation, shown on the dashboard (R6).
    """

    model_config = ConfigDict(frozen=True)

    status: DiabetesStatus
    kind: GlucoseLimitKind
    direction: LimitDirection
    base_mg_dl: float = Field(gt=0)
    hard_min_mg_dl: float = Field(gt=0)
    hard_max_mg_dl: float = Field(gt=0)
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def _base_within_clamp(self) -> Self:
        """Check that ``hard_min <= base <= hard_max``.

        Returns:
            The validated limit.

        Raises:
            ValueError: If the base lies outside its own clamp.
        """
        if not self.hard_min_mg_dl <= self.base_mg_dl <= self.hard_max_mg_dl:
            raise ValueError(
                f"{self.status}/{self.kind}: base {self.base_mg_dl} outside "
                f"[{self.hard_min_mg_dl}, {self.hard_max_mg_dl}]"
            )
        return self

    def clamp(self, value_mg_dl: float) -> float:
        """Clamp a proposed personal threshold into this limit's hard range.

        Args:
            value_mg_dl: Threshold proposed by base + adjustments (R2).

        Returns:
            The value limited to ``[hard_min_mg_dl, hard_max_mg_dl]``.
        """
        return min(max(value_mg_dl, self.hard_min_mg_dl), self.hard_max_mg_dl)


_LIMITS: tuple[GlucoseLimit, ...] = (
    # --- Adults with diabetes ---------------------------------------------
    GlucoseLimit(
        status="diabetes",
        kind="fasting_high",
        direction="upper",
        base_mg_dl=FASTING_HIGH_DM_MG_DL,
        hard_min_mg_dl=NORMAL_FASTING_MAX_MG_DL,
        hard_max_mg_dl=FASTING_HIGH_DM_HARD_MAX_MG_DL,
        source="ADA Standards of Care 2025, Tables 6.3, 13.1; Sec. 2",
    ),
    GlucoseLimit(
        status="diabetes",
        kind="post_meal_high",
        direction="upper",
        base_mg_dl=POST_MEAL_HIGH_DM_MG_DL,
        hard_min_mg_dl=OGTT_2H_NORMAL_MAX_MG_DL,
        hard_max_mg_dl=LEVEL_2_HYPERGLYCAEMIA_MG_DL,
        source="ADA Standards of Care 2025, Table 6.3, Sec. 2; Battelino 2019 (TIR)",
    ),
    GlucoseLimit(
        status="diabetes",
        kind="low",
        direction="lower",
        base_mg_dl=HYPO_LEVEL_1_MG_DL,
        hard_min_mg_dl=HYPO_LEVEL_1_MG_DL,
        hard_max_mg_dl=PREPRANDIAL_TARGET_LOW_DM_MG_DL,
        source="ADA Standards of Care 2025, Tables 6.3, 6.4; IHSG 2017",
    ),
    # --- Adults without diabetes ------------------------------------------
    GlucoseLimit(
        status="no_diabetes",
        kind="fasting_high",
        direction="upper",
        base_mg_dl=NORMAL_FASTING_MAX_MG_DL,
        hard_min_mg_dl=NORMAL_FASTING_MAX_MG_DL,
        hard_max_mg_dl=IFG_MAX_MG_DL,
        source="ADA Standards of Care 2025, Sec. 2 (FPG criteria)",
    ),
    GlucoseLimit(
        status="no_diabetes",
        kind="post_meal_high",
        direction="upper",
        base_mg_dl=OGTT_2H_NORMAL_MAX_MG_DL,
        hard_min_mg_dl=OGTT_2H_NORMAL_MAX_MG_DL,
        hard_max_mg_dl=IGT_MAX_MG_DL,
        source="ADA Standards of Care 2025, Sec. 2 (2-h OGTT criteria, used as proxy)",
    ),
    # Team decision 2026-11-15: reuse ADA's Level 1 alert value (defined for people
    # with diabetes) as a conservative low alert for people without diabetes. No
    # cited basis to move it either way, so the clamp is a single point: not
    # personalised.
    GlucoseLimit(
        status="no_diabetes",
        kind="low",
        direction="lower",
        base_mg_dl=HYPO_LEVEL_1_MG_DL,
        hard_min_mg_dl=HYPO_LEVEL_1_MG_DL,
        hard_max_mg_dl=HYPO_LEVEL_1_MG_DL,
        source="ADA Standards of Care 2025, Table 6.4 (applied conservatively)",
    ),
)

GLUCOSE_LIMITS: Mapping[tuple[DiabetesStatus, GlucoseLimitKind], GlucoseLimit] = MappingProxyType(
    {(limit.status, limit.kind): limit for limit in _LIMITS}
)


def get_glucose_limit(status: DiabetesStatus, kind: GlucoseLimitKind) -> GlucoseLimit:
    """Look up the cited limit for a population and reading kind.

    Args:
        status: ``"diabetes"`` or ``"no_diabetes"``.
        kind: ``"fasting_high"``, ``"post_meal_high"`` or ``"low"``.

    Returns:
        The matching :class:`GlucoseLimit`.

    Raises:
        ClinicalLimitError: If no cited limit exists for the combination.
    """
    try:
        return GLUCOSE_LIMITS[(status, kind)]
    except KeyError:
        raise ClinicalLimitError(f"No cited glucose limit for {status}/{kind}") from None
