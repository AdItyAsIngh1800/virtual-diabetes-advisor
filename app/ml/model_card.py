"""Model card of the diabetes risk model for the practitioner Audit tab (R5).

Built from ``risk_model.meta.json`` (read and validated by
:func:`app.ml.artifacts.load_metadata`); nothing here re-scores data.

Honesty rules the card follows:

* Every rate is shown with the counts it comes from.
* ROC-AUC is never shown alone: :class:`CardMetrics` puts precision, recall and F1
  first, and ROC-AUC only after them, in one object.
* The share of flagged people who do **not** have diabetes, FP / (FP + TP), is shown
  explicitly: the model favours recall, so most people it flags are not diabetic.
* :data:`LIMITATIONS` is fixed text, shown in full.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict

from app.core.rates import CountRate
from app.ml.artifacts import ClassificationMetrics, ModelMetadata, TierCutoffs

# R5: fixed limitations, shown in full on the Audit tab. Keep in step with the module
# docstring of app/ml/predict.py and the brfss-model skill.
LIMITATIONS: Final[tuple[str, ...]] = (
    "The model estimates diabetes risk from 2015 US self-reported survey data (CDC BRFSS "
    "2015). It has never seen a glucose reading.",
    "It does not predict glucose and is not real-time.",
    "It is not a diagnosis and is not clinically validated.",
    "It favours recall, so most people it flags do not have diabetes.",
    "The risk tier is stored for audit only and never changes a glucose limit.",
)


class CardMetrics(BaseModel):
    """Test-set scores in the order the card shows them.

    Field order is deliberate: precision, recall and F1 first; the threshold-free
    ROC-AUC and PR-AUC only after them, so ROC-AUC never appears without them.

    Attributes:
        precision: Share of flagged people who have diabetes.
        recall: Share of people with diabetes who are flagged.
        f1: Harmonic mean of precision and recall.
        roc_auc: Area under the ROC curve.
        pr_auc: Average precision.
        brier: Mean squared error of the probabilities (lower is better calibrated).
    """

    model_config = ConfigDict(frozen=True)

    precision: float
    recall: float
    f1: float
    roc_auc: float
    pr_auc: float
    brier: float


class ConfusionMatrix(BaseModel):
    """Test-set confusion matrix at the decision threshold, labelled.

    Attributes:
        tn: No diabetes, not flagged.
        fp: No diabetes, flagged.
        fn: Diabetes, not flagged.
        tp: Diabetes, flagged.
    """

    model_config = ConfigDict(frozen=True)

    tn: int
    fp: int
    fn: int
    tp: int


class FeatureImportance(BaseModel):
    """One of the most important features.

    Attributes:
        feature: BRFSS column name.
        importance: Drop in average precision when the feature is shuffled.
    """

    model_config = ConfigDict(frozen=True)

    feature: str
    importance: float


class ModelCard(BaseModel):
    """What a practitioner needs to judge the risk model.

    Attributes:
        version: Model version (recorded with every risk assessment).
        trained_at: When it was trained (ISO 8601).
        model_name: Winning candidate, e.g. ``gradient_boosting``.
        training_rows: Rows in the training split.
        prevalence: Share of positives in the training data.
        test_rows: Rows in the held-out test set (sum of the confusion matrix).
        test_metrics: Test-set scores (precision, recall, F1 first).
        confusion: Test-set confusion matrix.
        flagged_without_diabetes: FP / (FP + TP): flagged people who do not have
            diabetes, with the counts.
        decision_threshold: Probability at or above which a person is flagged.
        tier_cutoffs: Probability boundaries of the moderate and high tiers.
        top_features: Most important features, highest first.
        limitations: :data:`LIMITATIONS`, in full.
    """

    version: str
    trained_at: str
    model_name: str
    training_rows: int
    prevalence: float
    test_rows: int
    test_metrics: CardMetrics
    confusion: ConfusionMatrix
    flagged_without_diabetes: CountRate
    decision_threshold: float
    tier_cutoffs: TierCutoffs
    top_features: list[FeatureImportance]
    limitations: list[str]


def card_metrics(metrics: ClassificationMetrics) -> CardMetrics:
    """Pick the card's scores out of the stored metrics.

    Args:
        metrics: Stored metrics.

    Returns:
        Scores in card order.
    """
    return CardMetrics(
        precision=metrics.precision,
        recall=metrics.recall,
        f1=metrics.f1,
        roc_auc=metrics.roc_auc,
        pr_auc=metrics.pr_auc,
        brier=metrics.brier,
    )


def labelled_confusion(metrics: ClassificationMetrics) -> ConfusionMatrix:
    """Label a ``[[tn, fp], [fn, tp]]`` matrix (2x2 is checked by the metrics model).

    Args:
        metrics: Stored metrics.

    Returns:
        The labelled matrix.
    """
    (tn, fp), (fn, tp) = metrics.confusion
    return ConfusionMatrix(tn=tn, fp=fp, fn=fn, tp=tp)


def build_model_card(metadata: ModelMetadata) -> ModelCard:
    """Build the card from validated metadata.

    Args:
        metadata: From :func:`app.ml.artifacts.load_metadata`.

    Returns:
        The card.
    """
    confusion = labelled_confusion(metadata.test)
    return ModelCard(
        version=metadata.version,
        trained_at=metadata.trained_at,
        model_name=metadata.model_name,
        training_rows=metadata.training_rows,
        prevalence=metadata.prevalence,
        test_rows=confusion.tn + confusion.fp + confusion.fn + confusion.tp,
        test_metrics=card_metrics(metadata.test),
        confusion=confusion,
        flagged_without_diabetes=CountRate.of(confusion.fp, confusion.fp + confusion.tp),
        decision_threshold=metadata.decision_threshold,
        tier_cutoffs=metadata.tier_cutoffs,
        top_features=[
            FeatureImportance(feature=name, importance=score)
            for name, score in sorted(
                metadata.top_features.items(), key=lambda kv: kv[1], reverse=True
            )
        ],
        limitations=list(LIMITATIONS),
    )
