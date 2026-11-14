"""Where the trained risk model lives on disk and what metadata travels with it.

Two files are written side by side in ``models/``:
    * ``risk_model.joblib``    - the fitted, calibrated scikit-learn estimator.
    * ``risk_model.meta.json`` - :class:`ModelMetadata`: version, metrics, decision
      threshold and tier cut-offs. The app reads the cut-offs from here, so retraining
      never needs a code change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import BaseModel, Field

from app.ml.data import PROJECT_ROOT

DEFAULT_MODEL_DIR: Final = PROJECT_ROOT / "models"
MODEL_FILENAME: Final = "risk_model.joblib"
METADATA_FILENAME: Final = "risk_model.meta.json"


class ClassificationMetrics(BaseModel):
    """Binary-classification scores at one decision threshold.

    Attributes:
        roc_auc: Area under the ROC curve (threshold-free ranking quality).
        pr_auc: Average precision; more informative than ROC-AUC when positives are rare.
        recall: Share of diabetic people the model flags (the metric we care most about).
        precision: Share of flagged people who are actually diabetic.
        f1: Harmonic mean of precision and recall.
        brier: Mean squared error of the probabilities (lower = better calibrated).
        threshold: Probability at or above which a person is flagged.
        confusion: ``[[tn, fp], [fn, tp]]``.
    """

    roc_auc: float
    pr_auc: float
    recall: float
    precision: float
    f1: float
    brier: float
    threshold: float
    confusion: list[list[int]]


class TierCutoffs(BaseModel):
    """Probability boundaries for the low / moderate / high risk tiers.

    Attributes:
        moderate: Probabilities at or above this are at least ``moderate``.
        high: Probabilities at or above this are ``high``.
    """

    moderate: float = Field(gt=0, lt=1)
    high: float = Field(gt=0, lt=1)


class ModelMetadata(BaseModel):
    """Everything about a trained model that is needed to serve and explain it."""

    version: str
    trained_at: str
    model_name: str = Field(description="Winning candidate, e.g. 'gradient_boosting'")
    training_rows: int
    prevalence: float = Field(description="Share of positives in the training data")
    features: list[str]
    decision_threshold: float = Field(description="Chosen on validation to maximise F2")
    tier_cutoffs: TierCutoffs
    candidates: dict[str, ClassificationMetrics] = Field(
        description="Validation metrics of every candidate before calibration"
    )
    validation: ClassificationMetrics
    test: ClassificationMetrics
    top_features: dict[str, float] = Field(
        description="Permutation importance (drop in average precision), highest first"
    )


def model_paths(model_dir: Path = DEFAULT_MODEL_DIR) -> tuple[Path, Path]:
    """Return the model and metadata file paths inside ``model_dir``.

    Args:
        model_dir: Folder that holds (or will hold) the artifacts.

    Returns:
        ``(model_path, metadata_path)``.
    """
    return model_dir / MODEL_FILENAME, model_dir / METADATA_FILENAME
