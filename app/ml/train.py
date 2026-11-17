"""Train, compare, calibrate and save the diabetes risk model.

Run from the project root::

    uv run python -m app.ml.train            # full training (a few minutes)
    uv run python -m app.ml.train --sample 0.2   # quick run on 20 % of the rows

Pipeline:
    1. Load the real-prevalence binary file and drop duplicate rows.
    2. Stratified 70 / 15 / 15 train / validation / test split.
    3. Fit three candidates (logistic regression, random forest, gradient boosting)
       and pick the one with the best validation PR-AUC.
    4. Re-fit the winner inside isotonic calibration so its probabilities are real
       probabilities (used for the risk tier, which is stored for audit and never
       changes a glucose limit).
    5. Pick the decision threshold on validation by maximising F2 (recall counts double:
       missing a diabetic person costs more than a false alarm).
    6. Report final metrics on the untouched test set, compute permutation importance,
       and save the model plus metadata.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import joblib
import numpy as np
import numpy.typing as npt
import pandas as pd
from sklearn.base import clone
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import RobustScaler, StandardScaler

from app.ml.artifacts import (
    DEFAULT_MODEL_DIR,
    ClassificationMetrics,
    ModelMetadata,
    TierCutoffs,
    model_paths,
)
from app.ml.data import DEFAULT_DATA_DIR, RANDOM_STATE, load_brfss, split_data
from app.ml.schema import BINARY_TARGET, CONTINUOUS_FEATURES, FEATURES, ORDINAL_FEATURES

logger = logging.getLogger(__name__)

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

F_BETA: Final = 2.0  # F2 weights recall twice as much as precision
# Tier cut-offs are multiples of the base rate: "moderate" = at or above average risk,
# "high" = at least 2.5x the average. Relative cut-offs stay meaningful if the model
# is retrained on data with a different prevalence.
MODERATE_RISK_MULTIPLIER: Final = 1.0
HIGH_RISK_MULTIPLIER: Final = 2.5
IMPORTANCE_SAMPLE_ROWS: Final = 5000  # permutation importance on a sample keeps it fast
TOP_FEATURES: Final = 10


def _gradient_boosting(pos_weight: float) -> Any:  # noqa: ANN401 - sklearn/xgboost are untyped
    """Return XGBoost if it is installed, otherwise scikit-learn's histogram GBM.

    Both are gradient-boosted trees; XGBoost is in the project dependencies, the
    fallback just keeps the pipeline runnable in minimal environments.

    Args:
        pos_weight: Ratio of negatives to positives, used to up-weight the rare class.

    Returns:
        An unfitted classifier with ``fit`` / ``predict_proba``.
    """
    try:
        from xgboost import XGBClassifier  # noqa: PLC0415 - optional dependency
    except ImportError:
        logger.warning("xgboost not installed; using HistGradientBoostingClassifier")
        return HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_iter=400,
            max_leaf_nodes=31,
            class_weight="balanced",
            random_state=RANDOM_STATE,
        )
    return XGBClassifier(
        n_estimators=400,
        learning_rate=0.05,
        max_depth=5,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=pos_weight,
        eval_metric="logloss",
        n_jobs=-1,
        random_state=RANDOM_STATE,
    )


def build_candidates(pos_weight: float) -> dict[str, Any]:
    """Create the unfitted candidate models to compare.

    Args:
        pos_weight: Ratio of negatives to positives in the training labels.

    Returns:
        Mapping of candidate name to unfitted estimator.
    """
    # Linear models need scaled inputs; BMI and the "days" counts are skewed with
    # outliers, so they get a RobustScaler (median/IQR). Binary 0/1 columns pass through.
    linear_preprocess = ColumnTransformer(
        [
            ("continuous", RobustScaler(), list(CONTINUOUS_FEATURES)),
            ("ordinal", StandardScaler(), list(ORDINAL_FEATURES)),
        ],
        remainder="passthrough",
    )
    return {
        "logistic_regression": Pipeline(
            [
                ("preprocess", linear_preprocess),
                ("model", LogisticRegression(max_iter=2000, class_weight="balanced")),
            ]
        ),
        # Trees are scale-invariant, so no preprocessing; min_samples_leaf limits overfitting.
        "random_forest": RandomForestClassifier(
            n_estimators=200,
            min_samples_leaf=20,
            max_features="sqrt",
            class_weight="balanced_subsample",
            n_jobs=-1,
            random_state=RANDOM_STATE,
        ),
        "gradient_boosting": _gradient_boosting(pos_weight),
    }


def evaluate(y_true: IntArray, proba: FloatArray, threshold: float) -> ClassificationMetrics:
    """Score predicted probabilities against true labels.

    Args:
        y_true: True 0/1 labels.
        proba: Predicted probability of the positive class.
        threshold: Probability at or above which a prediction counts as positive.

    Returns:
        The full set of :class:`ClassificationMetrics`.
    """
    y_pred = (proba >= threshold).astype(int)
    return ClassificationMetrics(
        roc_auc=float(roc_auc_score(y_true, proba)),
        pr_auc=float(average_precision_score(y_true, proba)),
        recall=float(recall_score(y_true, y_pred, zero_division=0)),
        precision=float(precision_score(y_true, y_pred, zero_division=0)),
        f1=float(f1_score(y_true, y_pred, zero_division=0)),
        brier=float(brier_score_loss(y_true, proba)),
        threshold=float(threshold),
        confusion=confusion_matrix(y_true, y_pred, labels=[0, 1]).tolist(),
    )


def choose_threshold(y_true: IntArray, proba: FloatArray, beta: float = F_BETA) -> float:
    """Pick the probability threshold that maximises the F-beta score.

    Args:
        y_true: True 0/1 labels (validation set - never the test set).
        proba: Predicted positive-class probabilities.
        beta: How many times more recall matters than precision.

    Returns:
        The best threshold.
    """
    precision, recall, thresholds = precision_recall_curve(y_true, proba)
    # precision/recall have one more entry than thresholds; drop the last (threshold-less) one.
    precision, recall = precision[:-1], recall[:-1]
    beta_sq = beta**2
    denominator = beta_sq * precision + recall
    f_beta = np.divide(
        (1 + beta_sq) * precision * recall,
        denominator,
        out=np.zeros_like(denominator),
        where=denominator > 0,
    )
    return float(thresholds[int(np.argmax(f_beta))])


def tier_cutoffs_for(prevalence: float) -> TierCutoffs:
    """Derive risk-tier probability cut-offs from the base rate.

    Args:
        prevalence: Share of positives in the training data (0-1).

    Returns:
        Cut-offs where ``moderate`` = base rate and ``high`` = 2.5x base rate (capped < 1).
    """
    return TierCutoffs(
        moderate=prevalence * MODERATE_RISK_MULTIPLIER,
        high=min(prevalence * HIGH_RISK_MULTIPLIER, 0.95),
    )


def _top_features(model: Any, x: pd.DataFrame, y: pd.Series) -> dict[str, float]:  # noqa: ANN401
    """Rank features by how much shuffling each one hurts average precision.

    Args:
        model: A fitted classifier.
        x: Held-out features (a random sample is used if larger than the limit).
        y: Matching labels.

    Returns:
        Up to :data:`TOP_FEATURES` features mapped to their mean importance, highest first.
    """
    if len(x) > IMPORTANCE_SAMPLE_ROWS:
        x = x.sample(IMPORTANCE_SAMPLE_ROWS, random_state=RANDOM_STATE)
        y = y.loc[x.index]
    result = permutation_importance(
        model, x, y, scoring="average_precision", n_repeats=3, random_state=RANDOM_STATE
    )
    ranked = sorted(
        zip(FEATURES, result.importances_mean, strict=True), key=lambda kv: kv[1], reverse=True
    )
    return {name: round(float(score), 5) for name, score in ranked[:TOP_FEATURES]}


def train_risk_model(
    data_dir: Path = DEFAULT_DATA_DIR,
    model_dir: Path = DEFAULT_MODEL_DIR,
    sample_frac: float | None = None,
) -> ModelMetadata:
    """Run the full training pipeline and save the model and its metadata.

    Args:
        data_dir: Folder with the BRFSS CSVs.
        model_dir: Folder to write ``risk_model.joblib`` and ``risk_model.meta.json`` to.
        sample_frac: If set (0-1), train on this random share of rows for a quick run.

    Returns:
        The metadata that was saved next to the model.

    Raises:
        ValueError: If ``sample_frac`` is not in (0, 1].
    """
    df = load_brfss("binary", data_dir)
    if sample_frac is not None:
        if not 0 < sample_frac <= 1:
            raise ValueError("sample_frac must be in (0, 1]")
        df = df.sample(frac=sample_frac, random_state=RANDOM_STATE)

    splits = split_data(df, BINARY_TARGET)
    y_train = splits.y_train.to_numpy()
    y_val = splits.y_val.to_numpy()
    prevalence = float(y_train.mean())
    pos_weight = (1 - prevalence) / prevalence
    logger.info("Training rows: %d, prevalence: %.3f", len(splits.x_train), prevalence)

    # --- 1. Compare candidates on validation (threshold-free PR-AUC decides). ---
    candidates = build_candidates(pos_weight)
    candidate_metrics: dict[str, ClassificationMetrics] = {}
    for name, estimator in candidates.items():
        estimator.fit(splits.x_train, y_train)
        proba = estimator.predict_proba(splits.x_val)[:, 1]
        # Uncalibrated, class-weighted models centre around 0.5, so 0.5 is a fair cut here.
        candidate_metrics[name] = evaluate(y_val, proba, threshold=0.5)
        logger.info(
            "%-20s val PR-AUC %.4f  ROC-AUC %.4f",
            name,
            candidate_metrics[name].pr_auc,
            candidate_metrics[name].roc_auc,
        )
    best_name = max(candidate_metrics, key=lambda n: candidate_metrics[n].pr_auc)
    logger.info("Best candidate: %s", best_name)

    # --- 2. Calibrate the winner (isotonic, 3-fold) so probabilities mean something. ---
    calibrated = CalibratedClassifierCV(clone(candidates[best_name]), method="isotonic", cv=3)
    calibrated.fit(splits.x_train, y_train)

    # --- 3. Threshold on validation, final numbers on test. ---
    val_proba = calibrated.predict_proba(splits.x_val)[:, 1]
    threshold = choose_threshold(y_val, val_proba)
    test_proba = calibrated.predict_proba(splits.x_test)[:, 1]
    val_metrics = evaluate(y_val, val_proba, threshold)
    test_metrics = evaluate(splits.y_test.to_numpy(), test_proba, threshold)
    logger.info(
        "Test: recall %.3f  precision %.3f  PR-AUC %.4f  ROC-AUC %.4f",
        test_metrics.recall,
        test_metrics.precision,
        test_metrics.pr_auc,
        test_metrics.roc_auc,
    )

    now = datetime.now(UTC)
    metadata = ModelMetadata(
        version=f"risk-{now:%Y%m%d-%H%M%S}",
        trained_at=now.isoformat(),
        model_name=best_name,
        training_rows=len(splits.x_train),
        prevalence=prevalence,
        features=list(FEATURES),
        decision_threshold=threshold,
        tier_cutoffs=tier_cutoffs_for(prevalence),
        candidates=candidate_metrics,
        validation=val_metrics,
        test=test_metrics,
        top_features=_top_features(calibrated, splits.x_test, splits.y_test),
    )

    model_dir.mkdir(parents=True, exist_ok=True)
    model_path, meta_path = model_paths(model_dir)
    joblib.dump(calibrated, model_path)
    meta_path.write_text(json.dumps(metadata.model_dump(), indent=2))
    logger.info("Saved %s and %s", model_path, meta_path)
    return metadata


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point: parse arguments and train.

    Args:
        argv: Arguments to parse; ``None`` reads ``sys.argv``.
    """
    parser = argparse.ArgumentParser(description="Train the diabetes risk model.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--sample", type=float, default=None, help="Share of rows, e.g. 0.2")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    metadata = train_risk_model(args.data_dir, args.model_dir, args.sample)
    print(json.dumps(metadata.test.model_dump(), indent=2))


if __name__ == "__main__":
    main()
