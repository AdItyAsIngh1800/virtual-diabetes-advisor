"""Where the trained risk model lives on disk and what metadata travels with it.

Two files are written side by side in ``models/``:
    * ``risk_model.joblib``    - the fitted, calibrated scikit-learn estimator.
    * ``risk_model.meta.json`` - :class:`ModelMetadata`: version, metrics, decision
      threshold and tier cut-offs. The app reads the cut-offs from here, so retraining
      never needs a code change.

:func:`load_metadata` is the one reader of the metadata file: the risk model
(:mod:`app.ml.predict`), the model card (R5) and the retrain job all go through it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.core.exceptions import ModelMetadataError, ModelNotTrainedError
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

    @field_validator("confusion")
    @classmethod
    def _two_by_two(cls, value: list[list[int]]) -> list[list[int]]:
        """Require a 2x2 matrix of non-negative counts.

        Args:
            value: The parsed matrix.

        Returns:
            The matrix unchanged.

        Raises:
            ValueError: It is not 2x2 or holds a negative count.
        """
        if len(value) != 2 or any(len(row) != 2 for row in value):  # noqa: PLR2004 - binary
            raise ValueError("confusion must be [[tn, fp], [fn, tp]]")
        if any(cell < 0 for row in value for cell in row):
            raise ValueError("confusion counts must be non-negative")
        return value


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


def load_metadata(model_dir: Path = DEFAULT_MODEL_DIR) -> ModelMetadata:
    """Read and validate ``risk_model.meta.json``.

    Args:
        model_dir: Folder that holds the artifacts.

    Returns:
        The validated metadata.

    Raises:
        ModelNotTrainedError: The metadata file does not exist.
        ModelMetadataError: It exists but is unreadable, not JSON, or not valid
            :class:`ModelMetadata`.
    """
    _, meta_path = model_paths(model_dir)
    if not meta_path.exists():
        raise ModelNotTrainedError(f"No model in {model_dir}. Run: uv run python -m app.ml.train")
    try:
        return ModelMetadata.model_validate_json(meta_path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise ModelMetadataError(f"{meta_path} is not valid model metadata") from exc
