"""Tests for the ML package: schema validation, data checks, thresholds, tiers and a smoke train."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.core.exceptions import DataValidationError, ModelNotTrainedError
from app.ml.artifacts import TierCutoffs
from app.ml.data import split_data, validate_frame
from app.ml.predict import RiskModel, RiskTier, tier_for_probability
from app.ml.schema import BINARY_TARGET, FEATURES, RiskProfile, age_to_bucket
from app.ml.train import choose_threshold, tier_cutoffs_for, train_risk_model

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"

PROFILE = {
    "HighBP": 1,
    "HighChol": 1,
    "CholCheck": 1,
    "BMI": 31.5,
    "Smoker": 0,
    "Stroke": 0,
    "HeartDiseaseorAttack": 0,
    "PhysActivity": 0,
    "Fruits": 1,
    "Veggies": 1,
    "HvyAlcoholConsump": 0,
    "AnyHealthcare": 1,
    "NoDocbcCost": 0,
    "GenHlth": 4,
    "MentHlth": 2,
    "PhysHlth": 10,
    "DiffWalk": 1,
    "Sex": 1,
    "Age": 10,
    "Education": 5,
    "Income": 4,
}


def _synthetic_frame(rows: int = 400) -> pd.DataFrame:
    """Random in-range BRFSS-shaped data where the label depends on BMI and HighBP."""
    rng = np.random.default_rng(0)
    df = pd.DataFrame({col: rng.integers(0, 2, rows) for col in FEATURES}).astype(float)
    df["BMI"] = rng.uniform(18, 45, rows)
    df["GenHlth"] = rng.integers(1, 6, rows)
    df["Age"] = rng.integers(1, 14, rows)
    df["Education"] = rng.integers(1, 7, rows)
    df["Income"] = rng.integers(1, 9, rows)
    df["MentHlth"] = rng.integers(0, 31, rows)
    df["PhysHlth"] = rng.integers(0, 31, rows)
    df[BINARY_TARGET] = ((df["BMI"] > 32) & (df["HighBP"] == 1)).astype(float)
    return df


# --- schema --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("age", "bucket"),
    [(18, 1), (24, 1), (25, 2), (59, 8), (60, 9), (64, 9), (65, 10), (79, 12), (80, 13), (99, 13)],
)
def test_age_to_bucket(age: int, bucket: int) -> None:
    assert age_to_bucket(age) == bucket


def test_age_to_bucket_rejects_minors() -> None:
    with pytest.raises(ValueError, match="adults"):
        age_to_bucket(17)


def test_profile_round_trip_keeps_model_column_order() -> None:
    frame = RiskProfile.model_validate(PROFILE).to_frame()
    assert list(frame.columns) == list(FEATURES)
    assert frame.loc[0, "BMI"] == pytest.approx(31.5)


@pytest.mark.parametrize(("field", "value"), [("BMI", 5), ("GenHlth", 6), ("Age", 0), ("Sex", 2)])
def test_profile_rejects_out_of_range(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        RiskProfile.model_validate({**PROFILE, field: value})


# --- data ----------------------------------------------------------------------------------
def test_validate_frame_flags_missing_column() -> None:
    with pytest.raises(DataValidationError, match="Missing"):
        validate_frame(_synthetic_frame().drop(columns=["BMI"]), BINARY_TARGET)


def test_validate_frame_flags_out_of_range() -> None:
    df = _synthetic_frame()
    df.loc[0, "Income"] = 9
    with pytest.raises(DataValidationError, match="Income"):
        validate_frame(df, BINARY_TARGET)


def test_split_is_stratified_and_disjoint() -> None:
    df = _synthetic_frame(1000)
    s = split_data(df, BINARY_TARGET)
    assert len(s.x_train) + len(s.x_val) + len(s.x_test) == len(df)
    assert set(s.x_train.index).isdisjoint(s.x_test.index)
    assert abs(s.y_train.mean() - s.y_test.mean()) < 0.03


# --- thresholds and tiers --------------------------------------------------------------------
def test_choose_threshold_separates_perfect_scores() -> None:
    y = np.array([0, 0, 0, 1, 1])
    proba = np.array([0.1, 0.2, 0.3, 0.8, 0.9])
    assert 0.3 < choose_threshold(y, proba) <= 0.8


def test_tier_cutoffs_scale_with_prevalence() -> None:
    cutoffs = tier_cutoffs_for(0.14)
    assert cutoffs.moderate == pytest.approx(0.14)
    assert cutoffs.high == pytest.approx(0.35)


@pytest.mark.parametrize(
    ("p", "tier"),
    [(0.0, "low"), (0.139, "low"), (0.14, "moderate"), (0.349, "moderate"), (0.35, "high")],
)
def test_tier_boundaries_are_inclusive(p: float, tier: RiskTier) -> None:
    assert tier_for_probability(p, TierCutoffs(moderate=0.14, high=0.35)) == tier


def test_load_without_artifacts_raises(tmp_path: Path) -> None:
    with pytest.raises(ModelNotTrainedError):
        RiskModel.load(tmp_path)


# --- end to end ------------------------------------------------------------------------------
@pytest.mark.skipif(not DATA_DIR.exists(), reason="BRFSS CSVs not present")
def test_smoke_train_and_assess(tmp_path: Path) -> None:
    metadata = train_risk_model(DATA_DIR, tmp_path, sample_frac=0.05)
    assert metadata.test.roc_auc > 0.75  # BRFSS models land around 0.82
    model = RiskModel.load(tmp_path)
    result = model.assess(RiskProfile.model_validate(PROFILE))
    assert 0 <= result.probability <= 1
    assert result.model_version == metadata.version
