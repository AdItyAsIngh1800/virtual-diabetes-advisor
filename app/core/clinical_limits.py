"""Cited clinical glucose limits and the hard clamps for personal thresholds (R2).

Every number in this module carries a source. Nothing clinical is allowed inline
anywhere else in the codebase: services import from here.

Each limit has a *base* (the published value the threshold engine starts from)
and a hard ``[min, max]`` clamp. The engine may tighten or relax a user's personal
threshold with ML/trend adjustments, but :meth:`GlucoseLimit.clamp` guarantees it
never leaves the clamp. All values are capillary/plasma glucose in mg/dL.

Sources (abbreviations used in the comments below). Every ADA citation refers to the
Standards of Care in Diabetes-2026 edition (Diabetes Care 2026;49(Suppl. 1)). Section,
table and recommendation numbers were checked against the 2026 text on PMC on
2026-11-15.

    ADA-2:  ADA Professional Practice Committee for Diabetes. "2. Diagnosis and
            Classification of Diabetes: Standards of Care in Diabetes-2026."
            Diabetes Care 2026;49(Suppl. 1):S27-S49. doi:10.2337/dc26-S002.
            Checked: Table 2.1 (diagnostic criteria), Table 2.2 (prediabetes).
    ADA-6:  ADA Professional Practice Committee for Diabetes. "6. Glycemic Goals,
            Hypoglycemia, and Hyperglycemic Crises: Standards of Care in
            Diabetes-2026." Diabetes Care 2026;49(Suppl. 1):S132-S149.
            doi:10.2337/dc26-S006 (PMC12690178). Checked by the team (the full
            text blocks automated access): Table 6.2 "CGM metrics for clinical
            care", Table 6.3 "Summary of glycemic goals for many nonpregnant adults
            with diabetes", Table 6.4 "Classification of hypoglycemia".
    ADA-13: ADA Professional Practice Committee for Diabetes. "13. Older Adults:
            Standards of Care in Diabetes-2026." Diabetes Care 2026;49(Suppl.
            1):S277-S296. doi:10.2337/dc26-S013. Checked: Table 13.2 (goals by
            health status, incl. footnote defining "multiple"), Recs 13.3 and 13.7b.
            (Table 13.1 is geriatric-syndrome screening, not glycaemic goals.)
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

# ADA-6 Table 6.4 + IHSG: Level 1 hypoglycaemia is glucose < 70 mg/dL and
# >= 54 mg/dL. 70 is the Level 1 threshold, so lower limits are never relaxed below it.
HYPO_LEVEL_1_MG_DL = 70.0
# ADA-6 Table 6.4 + IHSG: Level 2 (clinically significant) hypoglycaemia is
# glucose < 54 mg/dL. Not personalised: every such reading escalates to a
# practitioner and bypasses the advisory cooldown (R3/R4).
HYPO_LEVEL_2_MG_DL = 54.0
# ADA-6 Table 6.3: preprandial target 80-130 mg/dL for many nonpregnant adults with
# diabetes; ADA-13 Table 13.2 gives the same 80-130 for healthy older adults. 80 is the
# bottom of that target, so it is the highest a low-glucose alert may be raised to
# (e.g. for hypoglycaemia-prone users) - any higher and it would fire on in-target
# readings.
PREPRANDIAL_TARGET_LOW_DM_MG_DL = 80.0

# ---------------------------------------------------------------------------
# Demographics
# ---------------------------------------------------------------------------

# ADA-13 Rec 13.3 uses "adults 65 years of age or older" as the older-adult age
# cut-off (the section gives no formal definition). Table 13.2 gives older adults
# individualised (often relaxed) glycaemic goals, and Rec 13.7b says goal selection
# "should prioritize avoidance of hypoglycemia". Used by the threshold engine's
# bounded age adjustment (R2).
OLDER_ADULT_MIN_AGE_YEARS = 65
# ADA-13 Table 13.2: "complex/intermediate" health is defined by *multiple*
# coexisting chronic illnesses (or >= 2 ADL impairments, or mild-moderate cognitive
# impairment); the table footnote defines "multiple" as at least three.
COMPLEX_HEALTH_MIN_CONDITIONS = 3
# ADA-13 Table 13.2: fasting/preprandial goal 90-150 mg/dL for older adults with
# complex/intermediate health (healthy older adults: 80-130, i.e. the standard goal).
COMPLEX_HEALTH_FASTING_LOW_MG_DL = 90.0
# ADA-13 Table 13.2: upper end of the complex/intermediate fasting goal above.
COMPLEX_HEALTH_FASTING_HIGH_MG_DL = 150.0

# ---------------------------------------------------------------------------
# Adults with diabetes (upper limits)
# ---------------------------------------------------------------------------

# ADA-6 Table 6.3: preprandial (fasting) capillary glucose goal upper bound 130 mg/dL
# for many nonpregnant adults with diabetes. ADA-13 Table 13.2 gives the same 80-130
# goal for healthy older adults.
FASTING_HIGH_DM_MG_DL = 130.0
# ADA-13 Table 13.2: the most relaxed fasting/preprandial goal ADA gives is
# 100-180 mg/dL (older adults, very complex/poor health). A fasting limit is never relaxed past 180.
FASTING_HIGH_DM_HARD_MAX_MG_DL = 180.0
# ADA-6 Table 6.3: peak postprandial capillary glucose < 180 mg/dL for many
# nonpregnant adults with diabetes.
POST_MEAL_HIGH_DM_MG_DL = 180.0
# ADA-6 Table 6.2 (CGM metrics, time above range) + TIR: > 250 mg/dL is Level 2
# hyperglycaemia. A post-meal limit is never relaxed into Level 2 territory.
LEVEL_2_HYPERGLYCAEMIA_MG_DL = 250.0

# ---------------------------------------------------------------------------
# Adults without diabetes (upper limits)
# ---------------------------------------------------------------------------

# ADA-2 Table 2.2: impaired fasting glucose (prediabetes) is FPG 100-125 mg/dL, so
# normal is < 100 mg/dL (implied by that lower bound; not stated as a number). Also
# used as the tightest fasting upper limit for people *with* diabetes: alerting below
# normoglycaemia would fire on normal readings.
NORMAL_FASTING_MAX_MG_DL = 100.0
# ADA-2 Table 2.2: impaired fasting glucose is 100-125 mg/dL; Table 2.1: FPG
# >= 126 mg/dL meets the diagnostic criterion for diabetes. A non-diabetic fasting
# limit never reaches the diagnostic cut.
IFG_MAX_MG_DL = 125.0
# ADA-2 Table 2.2 range 100-125 mg/dL (above). DESIGN CHOICE, not an ADA value: the base for
# people without diabetes sits near the middle of the cited range (midpoint 112.5,
# rounded down) instead of on its floor, so the risk-model tier has room to tighten
# it toward the floor (R2). The clamp keeps every personalised value in 100-125.
NO_DM_FASTING_BASE_MG_DL = 112.0
# ADA-2 Table 2.2: impaired glucose tolerance starts at 2-h plasma glucose 140 mg/dL
# during a 75-g OGTT, so < 140 is normal (implied by that lower bound). PROXY: ADA
# gives no post-meal target for people without diabetes; the team agreed (2026-11-15)
# to use this diagnostic OGTT cut-off as the post-meal base. Also the tightest
# post-meal limit for people with diabetes, for the same reason as above.
OGTT_2H_NORMAL_MAX_MG_DL = 140.0
# ADA-2 Table 2.2: 2-h OGTT glucose 140-199 mg/dL is impaired glucose tolerance;
# Table 2.1: >= 200 mg/dL is diagnostic for diabetes. PROXY as above: hard max for the non-diabetic
# post-meal limit.
IGT_MAX_MG_DL = 199.0
# ADA-2 Table 2.2 2-h OGTT range 140-199 mg/dL (above, used as proxy). DESIGN CHOICE, not an
# ADA value: the post-meal base for people without diabetes sits inside the range at
# 160 (below the 169.5 midpoint, staying closer to normal), so the risk tier can
# tighten it toward the 140 floor (R2). The clamp keeps every value in 140-199.
NO_DM_POST_MEAL_BASE_MG_DL = 160.0


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
        source="ADA Standards of Care 2026, Tables 6.3, 13.2, 2.2",
    ),
    GlucoseLimit(
        status="diabetes",
        kind="post_meal_high",
        direction="upper",
        base_mg_dl=POST_MEAL_HIGH_DM_MG_DL,
        hard_min_mg_dl=OGTT_2H_NORMAL_MAX_MG_DL,
        hard_max_mg_dl=LEVEL_2_HYPERGLYCAEMIA_MG_DL,
        source="ADA Standards of Care 2026, Tables 6.3, 6.2, 2.2; Battelino 2019 (TIR)",
    ),
    GlucoseLimit(
        status="diabetes",
        kind="low",
        direction="lower",
        base_mg_dl=HYPO_LEVEL_1_MG_DL,
        hard_min_mg_dl=HYPO_LEVEL_1_MG_DL,
        hard_max_mg_dl=PREPRANDIAL_TARGET_LOW_DM_MG_DL,
        source="ADA Standards of Care 2026, Tables 6.3, 6.4; IHSG 2017",
    ),
    # --- Adults without diabetes ------------------------------------------
    GlucoseLimit(
        status="no_diabetes",
        kind="fasting_high",
        direction="upper",
        base_mg_dl=NO_DM_FASTING_BASE_MG_DL,
        hard_min_mg_dl=NORMAL_FASTING_MAX_MG_DL,
        hard_max_mg_dl=IFG_MAX_MG_DL,
        source="ADA Standards of Care 2026, Tables 2.1, 2.2 (FPG criteria)",
    ),
    GlucoseLimit(
        status="no_diabetes",
        kind="post_meal_high",
        direction="upper",
        base_mg_dl=NO_DM_POST_MEAL_BASE_MG_DL,
        hard_min_mg_dl=OGTT_2H_NORMAL_MAX_MG_DL,
        hard_max_mg_dl=IGT_MAX_MG_DL,
        source="ADA Standards of Care 2026, Tables 2.1, 2.2 (2-h OGTT criteria, used as proxy)",
    ),
    # Team decision 2026-11-15: reuse ADA's Level 1 threshold (defined for people
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
        source="ADA Standards of Care 2026, Table 6.4 (applied conservatively)",
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
