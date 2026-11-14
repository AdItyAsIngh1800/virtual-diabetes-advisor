"""Feature definitions for the BRFSS 2015 diabetes health indicators.

This module is the single source of truth for:
    * which columns the model uses and in what order,
    * the valid range of every feature (used to validate CSVs and user input),
    * :class:`RiskProfile`, the validated profile a user fills in during onboarding.

Coding of the ordinal features follows the BRFSS 2015 codebook:
    * ``GenHlth``   1 = excellent ... 5 = poor (self-rated general health)
    * ``Age``       13 five-year buckets: 1 = 18-24, 2 = 25-29, ..., 12 = 75-79, 13 = 80+
    * ``Education`` 1 = never attended school ... 6 = college graduate
    * ``Income``    1 = < $10k ... 8 = >= $75k (annual household income)
    * ``MentHlth`` / ``PhysHlth``  days of poor mental / physical health in the past 30 days
"""

from __future__ import annotations

from typing import Final, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

# --- Targets -----------------------------------------------------------------
BINARY_TARGET: Final = "Diabetes_binary"  # 0 = no diabetes, 1 = prediabetes or diabetes
THREE_CLASS_TARGET: Final = "Diabetes_012"  # 0 = none, 1 = prediabetes, 2 = diabetes

# --- Feature groups ----------------------------------------------------------
# Yes/no survey answers coded 0/1.
BINARY_FEATURES: Final[tuple[str, ...]] = (
    "HighBP",
    "HighChol",
    "CholCheck",
    "Smoker",
    "Stroke",
    "HeartDiseaseorAttack",
    "PhysActivity",
    "Fruits",
    "Veggies",
    "HvyAlcoholConsump",
    "AnyHealthcare",
    "NoDocbcCost",
    "DiffWalk",
    "Sex",  # 0 = female, 1 = male
)
# Ordered categories: kept as integers (one-hot encoding would throw away the order).
ORDINAL_FEATURES: Final[tuple[str, ...]] = ("GenHlth", "Age", "Education", "Income")
# Genuinely numeric, skewed, with outliers -> robust scaling for linear models.
CONTINUOUS_FEATURES: Final[tuple[str, ...]] = ("BMI", "MentHlth", "PhysHlth")

# Column order exactly as in the CSVs; the model is trained and served in this order.
FEATURES: Final[tuple[str, ...]] = (
    "HighBP",
    "HighChol",
    "CholCheck",
    "BMI",
    "Smoker",
    "Stroke",
    "HeartDiseaseorAttack",
    "PhysActivity",
    "Fruits",
    "Veggies",
    "HvyAlcoholConsump",
    "AnyHealthcare",
    "NoDocbcCost",
    "GenHlth",
    "MentHlth",
    "PhysHlth",
    "DiffWalk",
    "Sex",
    "Age",
    "Education",
    "Income",
)

# Inclusive (min, max) per feature, taken from the BRFSS codebook / observed data.
FEATURE_RANGES: Final[dict[str, tuple[float, float]]] = {
    **{name: (0.0, 1.0) for name in BINARY_FEATURES},
    "BMI": (12.0, 98.0),
    "MentHlth": (0.0, 30.0),
    "PhysHlth": (0.0, 30.0),
    "GenHlth": (1.0, 5.0),
    "Age": (1.0, 13.0),
    "Education": (1.0, 6.0),
    "Income": (1.0, 8.0),
}

DatasetKind = Literal["binary", "binary_5050", "three_class"]

# Age bucket boundaries: bucket k (1-based) starts at AGE_BUCKET_STARTS[k - 1].
_AGE_BUCKET_STARTS: Final[tuple[int, ...]] = (18, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80)
_MIN_ADULT_AGE: Final = 18


def age_to_bucket(age_years: int) -> int:
    """Convert an age in years to the BRFSS 13-level age bucket.

    Args:
        age_years: Age in completed years. BRFSS only covers adults.

    Returns:
        The bucket code from 1 (18-24) to 13 (80+).

    Raises:
        ValueError: If ``age_years`` is below 18.
    """
    if age_years < _MIN_ADULT_AGE:
        raise ValueError(f"BRFSS covers adults only; got age {age_years}")
    # Count how many bucket starts the age has passed; that count is the bucket code.
    return sum(1 for start in _AGE_BUCKET_STARTS if age_years >= start)


class RiskProfile(BaseModel):
    """A user's health profile in BRFSS coding, validated on construction.

    Field names are snake_case for Python; each has an alias equal to the BRFSS
    column name, so ``to_frame()`` produces exactly the columns the model expects.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    high_bp: bool = Field(alias="HighBP", description="Told they have high blood pressure")
    high_chol: bool = Field(alias="HighChol", description="Told they have high cholesterol")
    chol_check: bool = Field(alias="CholCheck", description="Cholesterol checked in last 5 years")
    bmi: float = Field(alias="BMI", ge=12, le=98, description="Body mass index (kg/m^2)")
    smoker: bool = Field(alias="Smoker", description="Smoked at least 100 cigarettes in life")
    stroke: bool = Field(alias="Stroke", description="Ever had a stroke")
    heart_disease_or_attack: bool = Field(
        alias="HeartDiseaseorAttack", description="Coronary heart disease or heart attack"
    )
    phys_activity: bool = Field(alias="PhysActivity", description="Exercise in past 30 days")
    fruits: bool = Field(alias="Fruits", description="Eats fruit at least once a day")
    veggies: bool = Field(alias="Veggies", description="Eats vegetables at least once a day")
    hvy_alcohol_consump: bool = Field(
        alias="HvyAlcoholConsump", description="Men >14, women >7 drinks per week"
    )
    any_healthcare: bool = Field(alias="AnyHealthcare", description="Has any health coverage")
    no_doc_bc_cost: bool = Field(alias="NoDocbcCost", description="Skipped a doctor due to cost")
    gen_hlth: int = Field(alias="GenHlth", ge=1, le=5, description="1 excellent ... 5 poor")
    ment_hlth: int = Field(alias="MentHlth", ge=0, le=30, description="Poor mental-health days")
    phys_hlth: int = Field(alias="PhysHlth", ge=0, le=30, description="Poor physical-health days")
    diff_walk: bool = Field(alias="DiffWalk", description="Serious difficulty walking/climbing")
    sex: Literal[0, 1] = Field(alias="Sex", description="0 = female, 1 = male")
    age: int = Field(alias="Age", ge=1, le=13, description="BRFSS age bucket (use age_to_bucket)")
    education: int = Field(alias="Education", ge=1, le=6, description="BRFSS education level")
    income: int = Field(alias="Income", ge=1, le=8, description="BRFSS income bracket")

    def to_frame(self) -> pd.DataFrame:
        """Return this profile as a one-row DataFrame in model column order.

        Returns:
            A DataFrame with columns :data:`FEATURES`, all values as floats.
        """
        row = self.model_dump(by_alias=True)
        return pd.DataFrame([row], columns=list(FEATURES)).astype(float)
