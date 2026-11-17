"""Manual risk-model retraining with a promotion guard (R5).

One run::

    check the three BRFSS CSVs are in data/raw          # else TrainingDataMissingError
    claim a 'running' retrain row in batch_runs          # one run at a time (0005 index)
    train_risk_model(...) into models/candidates/<new version>/   # never over the live files
    compare its TEST recall and precision with the current model's
    promote only if  recall    >= current recall    - 0.01
               and   precision >= current precision - 0.02
               and   training finished without error
        promote = back up models/risk_model.* to models/backups/<old version>/,
                  then copy the candidate over them
    record the decision, both sets of metrics and the reasons in batch_runs

Safety: this job never calls the threshold engine and never writes
``thresholds_history``. The risk tier is information for audit only and never moves a
glucose limit (R2); the next batch run simply records the new ``model_version`` with
each risk assessment (``app.jobs.batch_update.load_risk_scorer`` reloads on change).

Undo a promotion by copying ``models/backups/<old version>/risk_model.*`` back into
``models/``. Candidates and backups are kept; delete old ones by hand.

Entry points (retraining stays manual, there is no scheduler job):
``uv run python -m app.jobs.retrain [--dry-run] [--sample 0.2]`` and
``POST /admin/retrain-run``.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import AwareDatetime, BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import (
    BatchAlreadyRunningError,
    ModelMetadataError,
    ModelNotTrainedError,
    TrainingDataMissingError,
)
from app.core.logging import configure_logging
from app.db.batch_repository import SqlBatchRepository
from app.db.models import BatchJob, BatchRun, BatchStatus, BatchTrigger, RetrainDecision
from app.ml.artifacts import (
    DEFAULT_MODEL_DIR,
    ClassificationMetrics,
    ModelMetadata,
    load_metadata,
    model_paths,
)
from app.ml.data import DEFAULT_DATA_DIR, missing_datasets
from app.ml.train import train_risk_model

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

RETRAIN_JOB: Final[BatchJob] = "retrain"
# R5 promotion guard, on the held-out TEST set. Recall comes first (missing a diabetic
# person is the costly error), so it may drop by at most one point; precision may drop
# by two. Both are absolute differences of proportions. The small slack only absorbs
# float rounding, so a candidate exactly at the limit is promoted.
RECALL_TOLERANCE: Final = 0.01
PRECISION_TOLERANCE: Final = 0.02
_FLOAT_SLACK: Final = 1e-9
CANDIDATES_DIR: Final = "candidates"
BACKUPS_DIR: Final = "backups"
RECENT_RUNS: Final = 5


# ---------------------------------------------------------------------------
# Result models (also stored as batch_runs.retrain_report)
# ---------------------------------------------------------------------------


class MetricsSnapshot(BaseModel):
    """One model's test-set scores, as recorded for a run.

    Attributes:
        version: Model version.
        precision: Test precision at its decision threshold.
        recall: Test recall at its decision threshold.
        f1: Test F1.
        roc_auc: Test ROC-AUC (always recorded with precision and recall).
        pr_auc: Test average precision.
        brier: Test Brier score.
    """

    version: str
    precision: float
    recall: float
    f1: float
    roc_auc: float
    pr_auc: float
    brier: float

    @classmethod
    def of(cls, metadata: ModelMetadata) -> MetricsSnapshot:
        """Take the test scores out of a model's metadata.

        Args:
            metadata: Model metadata.

        Returns:
            The snapshot.
        """
        test = metadata.test
        return cls(
            version=metadata.version,
            precision=test.precision,
            recall=test.recall,
            f1=test.f1,
            roc_auc=test.roc_auc,
            pr_auc=test.pr_auc,
            brier=test.brier,
        )


class PromotionDecision(BaseModel):
    """Whether a candidate may replace the current model, and why.

    Attributes:
        promote: All checks passed.
        reasons: One line per check.
    """

    promote: bool
    reasons: list[str]


class RetrainReport(BaseModel):
    """What a run did; stored in ``batch_runs.retrain_report``.

    Attributes:
        dry_run: ``--dry-run``: compared but never promoted.
        sample_frac: Share of rows trained on, or ``None`` for all.
        old: The model that was current when the run started.
        new: The candidate, or ``None`` if training failed.
        reasons: Why it was promoted or kept.
        candidate_dir: Candidate folder, relative to ``models/``.
        backup_dir: Backup of the previous model, relative to ``models/`` (promotions).
    """

    dry_run: bool
    sample_frac: float | None
    old: MetricsSnapshot | None
    new: MetricsSnapshot | None
    reasons: list[str]
    candidate_dir: str | None = None
    backup_dir: str | None = None


class RetrainSummary(BaseModel):
    """Result of one run; also the body of ``POST /admin/retrain-run``.

    Attributes:
        run_id: ``batch_runs.id``.
        status: ``succeeded`` (training finished) or ``failed``.
        triggered_by: Who started the run.
        decision: ``promoted``, ``kept`` or ``dry_run``.
        current_version: Model version in ``models/`` after the run.
        report: Metrics and reasons.
        error: Failure summary (exception class only), or ``None``.
    """

    run_id: uuid.UUID
    status: BatchStatus
    triggered_by: BatchTrigger
    decision: RetrainDecision
    current_version: str | None
    report: RetrainReport
    error: str | None


class RetrainRunView(BaseModel):
    """One past retrain run, for the model card.

    Attributes:
        run_id: ``batch_runs.id``.
        status: ``running``, ``succeeded`` or ``failed``.
        triggered_by: Who started it.
        started_at: Start.
        finished_at: End, or ``None`` while running.
        decision: ``promoted``, ``kept`` or ``dry_run`` (``None`` while running).
        report: Metrics and reasons (``None`` while running or if unreadable).
        error: Failure summary.
    """

    run_id: uuid.UUID
    status: BatchStatus
    triggered_by: BatchTrigger
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    decision: RetrainDecision | None
    report: RetrainReport | None
    error: str | None


# ---------------------------------------------------------------------------
# Pure decision
# ---------------------------------------------------------------------------


def decide_promotion(
    current: ClassificationMetrics | None, candidate: ClassificationMetrics
) -> PromotionDecision:
    """Apply the promotion guard to test-set metrics.

    Args:
        current: The live model's test metrics, or ``None`` if there is no model.
        candidate: The candidate's test metrics.

    Returns:
        The decision and one reason per check.
    """
    if current is None:
        return PromotionDecision(
            promote=True, reasons=["no current model to compare with: the candidate is used"]
        )
    checks = (
        ("recall", candidate.recall, current.recall, RECALL_TOLERANCE),
        ("precision", candidate.precision, current.precision, PRECISION_TOLERANCE),
    )
    reasons: list[str] = []
    promote = True
    for name, new, old, tolerance in checks:
        floor = old - tolerance
        ok = new >= floor - _FLOAT_SLACK
        promote = promote and ok
        reasons.append(
            f"test {name} {new:.4f} vs current {old:.4f} (minimum {floor:.4f}): "
            f"{'ok' if ok else 'too low'}"
        )
    return PromotionDecision(promote=promote, reasons=reasons)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrainOutcome:
    """What :func:`retrain_candidate` did on disk.

    Attributes:
        decision: ``promoted``, ``kept`` or ``dry_run``.
        report: Metrics and reasons.
        current_version: Model version in ``models/`` afterwards.
    """

    decision: RetrainDecision
    report: RetrainReport
    current_version: str | None


def check_training_data(data_dir: Path = DEFAULT_DATA_DIR) -> None:
    """Refuse to start without the BRFSS CSVs.

    Args:
        data_dir: Folder that should hold them.

    Raises:
        TrainingDataMissingError: One or more files are missing (all are named).
    """
    missing = missing_datasets(data_dir)
    if missing:
        names = ", ".join(p.name for p in missing)
        raise TrainingDataMissingError(
            f"BRFSS 2015 CSVs missing from {data_dir}: {names}. Download the 'Diabetes "
            "Health Indicators' dataset (Kaggle) and put the CSVs there."
        )


def current_metadata(model_dir: Path) -> ModelMetadata | None:
    """Return the live model's metadata, or ``None`` when no model is installed.

    Args:
        model_dir: ``models/``.

    Returns:
        The metadata when both artifact files exist.

    Raises:
        ModelMetadataError: The metadata file exists but is invalid (the run fails
            rather than replace a model it cannot compare with).
    """
    model_path, _ = model_paths(model_dir)
    if not model_path.exists():
        return None
    try:
        return load_metadata(model_dir)
    except ModelNotTrainedError:
        return None


def _install(source_dir: Path, target_dir: Path) -> None:
    """Copy a model's two files into ``target_dir``, each replaced atomically.

    The estimator goes first and the metadata last: readers key on the metadata
    (``load_risk_scorer``), so they never see new metadata with an old estimator.

    Args:
        source_dir: Folder with ``risk_model.joblib`` and ``risk_model.meta.json``.
        target_dir: Destination folder.
    """
    for source, target in zip(model_paths(source_dir), model_paths(target_dir), strict=True):
        staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        shutil.copy2(source, staging)
        os.replace(staging, target)


def _backup(model_dir: Path, version: str) -> Path:
    """Copy the live model into ``models/backups/<version>/`` (kept if already there).

    Args:
        model_dir: ``models/``.
        version: Its version.

    Returns:
        The backup folder.
    """
    backup = model_dir / BACKUPS_DIR / version
    if not all(p.exists() for p in model_paths(backup)):
        backup.mkdir(parents=True, exist_ok=True)
        for source, target in zip(model_paths(model_dir), model_paths(backup), strict=True):
            shutil.copy2(source, target)
    return backup


def _train_candidate(model_dir: Path, data_dir: Path, sample_frac: float | None) -> Path:
    """Train into a fresh folder under ``models/candidates/`` named after the version.

    Args:
        model_dir: ``models/``.
        data_dir: BRFSS CSVs.
        sample_frac: Share of rows, or ``None``.

    Returns:
        The candidate folder.

    Raises:
        Exception: Whatever training raised (the partial folder is removed first).
    """
    candidates = model_dir / CANDIDATES_DIR
    staging = candidates / f".training-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        metadata = train_risk_model(data_dir, staging, sample_frac)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    final = candidates / metadata.version
    if final.exists():  # two runs in the same second
        final = candidates / f"{metadata.version}-{uuid.uuid4().hex[:6]}"
    staging.rename(final)
    return final


def retrain_candidate(
    *,
    model_dir: Path = DEFAULT_MODEL_DIR,
    data_dir: Path = DEFAULT_DATA_DIR,
    sample_frac: float | None = None,
    dry_run: bool = False,
) -> RetrainOutcome:
    """Train a candidate and promote it if the guard allows (no database).

    Args:
        model_dir: ``models/``; the live files are only replaced on promotion.
        data_dir: BRFSS CSVs.
        sample_frac: Share of rows (quick runs), or ``None`` for all.
        dry_run: Train and compare, never promote.

    Returns:
        The decision, report and the version now live.

    Raises:
        Exception: Training failed or the current metadata is invalid; nothing in
            ``models/`` was replaced.
    """
    current = current_metadata(model_dir)
    candidate_dir = _train_candidate(model_dir, data_dir, sample_frac)
    candidate = load_metadata(candidate_dir)
    verdict = decide_promotion(current.test if current else None, candidate.test)
    report = RetrainReport(
        dry_run=dry_run,
        sample_frac=sample_frac,
        old=MetricsSnapshot.of(current) if current else None,
        new=MetricsSnapshot.of(candidate),
        reasons=verdict.reasons,
        candidate_dir=candidate_dir.relative_to(model_dir).as_posix(),
    )
    live_version = current.version if current else None
    if dry_run:
        report.reasons.append("dry run: never promoted")
        return RetrainOutcome(decision="dry_run", report=report, current_version=live_version)
    if not verdict.promote:
        report.reasons.append("kept the current model")
        return RetrainOutcome(decision="kept", report=report, current_version=live_version)
    if current is not None:
        report.backup_dir = _backup(model_dir, current.version).relative_to(model_dir).as_posix()
    _install(candidate_dir, model_dir)
    report.reasons.append(f"promoted {candidate.version}")
    logger.info("promoted risk model %s (was %s)", candidate.version, live_version)
    return RetrainOutcome(decision="promoted", report=report, current_version=candidate.version)


# ---------------------------------------------------------------------------
# Run with batch_runs bookkeeping
# ---------------------------------------------------------------------------


def run_retrain(
    session_factory: SessionFactory,
    *,
    triggered_by: BatchTrigger = "manual",
    dry_run: bool = False,
    sample_frac: float | None = None,
    model_dir: Path = DEFAULT_MODEL_DIR,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> RetrainSummary:
    """Retrain once and record the run in ``batch_runs`` (R5).

    A training failure is recorded (status ``failed``, decision ``kept``) and
    returned, not raised; the live model is untouched.

    Args:
        session_factory: Creates SQLAlchemy sessions.
        triggered_by: Recorded in ``batch_runs``.
        dry_run: Never promote.
        sample_frac: Share of rows, or ``None`` for all.
        model_dir: ``models/``.
        data_dir: BRFSS CSVs.

    Returns:
        Status, decision and metrics of the run.

    Raises:
        TrainingDataMissingError: The BRFSS CSVs are missing (checked before a run
            is recorded).
        BatchAlreadyRunningError: Another retrain is in progress.
        ValueError: ``sample_frac`` is not in (0, 1].
    """
    if sample_frac is not None and not 0 < sample_frac <= 1:
        raise ValueError("sample must be in (0, 1]")
    check_training_data(data_dir)
    before: ModelMetadata | None = None
    try:
        before = current_metadata(model_dir)
    except ModelMetadataError:
        logger.exception("current model metadata is invalid; this run will fail")
    started_version = before.version if before else "none"

    with session_factory() as session:
        repo = SqlBatchRepository(session)
        now = repo.database_now()
        abandoned = repo.abandon_stale_runs(now, job=RETRAIN_JOB)
        if abandoned:
            logger.warning("marked %d stale retrain run(s) as failed", abandoned)
        # Committed on its own so a concurrent starter sees the 'running' row.
        run_id = repo.start_run(
            triggered_by=triggered_by,
            window_start=None,
            window_end=now,
            model_version=started_version,
            job=RETRAIN_JOB,
        )
        session.commit()

    status: BatchStatus = "succeeded"
    error: str | None = None
    try:
        outcome = retrain_candidate(
            model_dir=model_dir, data_dir=data_dir, sample_frac=sample_frac, dry_run=dry_run
        )
    except Exception as exc:
        logger.exception("retrain run %s failed; the current model is kept", run_id)
        status, error = "failed", type(exc).__name__
        outcome = RetrainOutcome(
            decision="kept",
            report=RetrainReport(
                dry_run=dry_run,
                sample_frac=sample_frac,
                old=MetricsSnapshot.of(before) if before else None,
                new=None,
                reasons=[f"training failed ({error}): kept the current model"],
            ),
            current_version=before.version if before else None,
        )

    with session_factory() as session:
        repo = SqlBatchRepository(session)
        repo.finish_run(
            run_id,
            status=status,
            finished_at=repo.database_now(),
            users_processed=0,
            readings_evaluated=0,
            thresholds_written=0,
            advisories_created=0,
            error=error,
            retrain_decision=outcome.decision,
            retrain_report=outcome.report.model_dump(mode="json"),
        )
        session.commit()

    log = logger.error if error else logger.info
    log("retrain run %s %s: %s (%s)", run_id, status, outcome.decision, outcome.report.reasons)
    return RetrainSummary(
        run_id=run_id,
        status=status,
        triggered_by=triggered_by,
        decision=outcome.decision,
        current_version=outcome.current_version,
        report=outcome.report,
        error=error,
    )


def run_view(run: BatchRun) -> RetrainRunView:
    """Convert a ``batch_runs`` row for the model card.

    Args:
        run: A ``retrain`` row.

    Returns:
        The view (``report`` is ``None`` if the stored JSON does not validate).
    """
    report: RetrainReport | None = None
    if run.retrain_report is not None:
        try:
            report = RetrainReport.model_validate(run.retrain_report)
        except ValidationError:
            logger.warning("batch_runs %s has an unreadable retrain_report", run.id)
    return RetrainRunView(
        run_id=run.id,
        status=run.status,
        triggered_by=run.triggered_by,
        started_at=run.started_at,
        finished_at=run.finished_at,
        decision=run.retrain_decision,
        report=report,
        error=run.error,
    )


def main(argv: list[str] | None = None) -> int:
    """Run one retrain from the command line (``triggered_by='manual'``).

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        Exit code: 0 when training finished (promoted, kept or dry run), 1 when
        training failed, 2 when the data is missing or another retrain is running.
    """
    parser = argparse.ArgumentParser(description="Retrain the diabetes risk model (R5).")
    parser.add_argument("--dry-run", action="store_true", help="train and report, never promote")
    parser.add_argument("--sample", type=float, default=None, help="share of rows, e.g. 0.2")
    args = parser.parse_args(argv)

    from app.db.session import get_session_factory  # noqa: PLC0415

    configure_logging(get_settings().log_level)
    try:
        summary = run_retrain(
            get_session_factory(),
            triggered_by="manual",
            dry_run=args.dry_run,
            sample_frac=args.sample,
        )
    except (TrainingDataMissingError, BatchAlreadyRunningError, ValueError) as exc:
        logger.error("retrain not started: %s", exc)
        return 2
    print(summary.model_dump_json(indent=2))
    return 0 if summary.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
