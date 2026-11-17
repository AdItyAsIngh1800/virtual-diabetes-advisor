"""Score a user's profile with the saved risk model.

Usage::

    model = RiskModel.load()
    assessment = model.assess(profile)  # profile is a validated RiskProfile
    assessment.tier                     # "low" | "moderate" | "high"

The model estimates the chance that someone has diabetes from BRFSS 2015 survey answers
(self-reported, US, 2015). It does NOT predict glucose levels, is not real-time and is
not a diagnosis. At the chosen threshold it favours recall over precision (about 0.88 vs
0.28 on the held-out test set), so most people it flags do not have diabetes. The tier is
stored for audit and shown as information only; it never changes a glucose limit (R2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import joblib
import pandas as pd
from pydantic import BaseModel, Field

from app.core.exceptions import ModelNotTrainedError
from app.ml.artifacts import (
    DEFAULT_MODEL_DIR,
    ModelMetadata,
    TierCutoffs,
    load_metadata,
    model_paths,
)
from app.ml.schema import FEATURES, RiskProfile

RiskTier = Literal["low", "moderate", "high"]


class RiskAssessment(BaseModel):
    """Result of scoring one profile.

    Attributes:
        probability: Calibrated probability of prediabetes/diabetes (0-1).
        tier: Risk band derived from the probability.
        flagged: Whether the probability reaches the model's decision threshold.
        model_version: Version of the model that produced this result (for audit, R5).
    """

    probability: float = Field(ge=0, le=1)
    tier: RiskTier
    flagged: bool
    model_version: str


def tier_for_probability(probability: float, cutoffs: TierCutoffs) -> RiskTier:
    """Map a probability to a risk tier.

    Args:
        probability: Calibrated positive-class probability.
        cutoffs: Tier boundaries from the model metadata.

    Returns:
        ``"high"`` if at or above ``cutoffs.high``, ``"moderate"`` if at or above
        ``cutoffs.moderate``, otherwise ``"low"``.
    """
    if probability >= cutoffs.high:
        return "high"
    if probability >= cutoffs.moderate:
        return "moderate"
    return "low"


@dataclass(frozen=True)
class RiskModel:
    """A loaded risk model plus its metadata.

    Attributes:
        estimator: Fitted calibrated classifier with ``predict_proba``.
        metadata: Version, threshold and tier cut-offs saved at training time.
    """

    estimator: Any
    metadata: ModelMetadata

    @property
    def version(self) -> str:
        """Model version recorded with every assessment (R5 audit).

        Returns:
            ``metadata.version``, e.g. ``"risk-20261114-204228"``.
        """
        return self.metadata.version

    @classmethod
    def load(cls, model_dir: Path = DEFAULT_MODEL_DIR) -> RiskModel:
        """Load the model and metadata written by :mod:`app.ml.train`.

        Args:
            model_dir: Folder containing the two artifact files.

        Returns:
            A ready-to-use :class:`RiskModel`.

        Raises:
            ModelNotTrainedError: If either artifact file is missing.
            ModelMetadataError: If the metadata file is not valid metadata.
        """
        model_path, meta_path = model_paths(model_dir)
        if not model_path.exists() or not meta_path.exists():
            raise ModelNotTrainedError(
                f"No model in {model_dir}. Run: uv run python -m app.ml.train"
            )
        metadata = load_metadata(model_dir)
        # Only load artifacts this project wrote itself: joblib files can run code on load.
        estimator = joblib.load(model_path)
        return cls(estimator=estimator, metadata=metadata)

    def probabilities(self, features: pd.DataFrame) -> list[float]:
        """Return the calibrated positive-class probability for each row.

        Args:
            features: Rows with the :data:`FEATURES` columns (extra columns are ignored).

        Returns:
            One probability per row.
        """
        proba = self.estimator.predict_proba(features[list(FEATURES)])[:, 1]
        return [float(p) for p in proba]

    def assess(self, profile: RiskProfile) -> RiskAssessment:
        """Score one user's profile.

        Args:
            profile: Validated BRFSS-coded profile.

        Returns:
            Probability, tier, flag and model version.
        """
        probability = self.probabilities(profile.to_frame())[0]
        return RiskAssessment(
            probability=probability,
            tier=tier_for_probability(probability, self.metadata.tier_cutoffs),
            flagged=probability >= self.metadata.decision_threshold,
            model_version=self.metadata.version,
        )
