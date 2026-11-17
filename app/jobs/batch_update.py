"""Batch update job (R1): thresholds, band checks and queued advisories for every patient.

One run::

    window = (end of the last *succeeded* run, now]      # on readings.created_at
    for each active patient (one transaction each):
        risk     = latest stored assessment if the BRFSS profile is unchanged,
                   otherwise re-score with the risk model and store it
        refresh_user_thresholds(as_of = window end)        # R2, logged revisions
        classify every reading stored in the window        # R3 band check
        insert queued advisories (cooldown + de-duplication) # R3, no sending yet
    record counts and status in batch_runs

Idempotency: the same window always yields the same advisories. Thresholds are computed
``as_of`` the window end from readings stored by then, the planner skips readings that
already have an advisory and applies the cooldown on measured time, and the
``advisories_reading_limit_key`` unique constraint backs both up in the database.

Failure isolation: a user whose transaction fails is rolled back and logged; the others
still commit. The run is then marked ``failed``, so the next run starts again from the
last succeeded window and retries that user; users that did succeed get no duplicates.

Entry points: :func:`run_batch_update` (APScheduler in ``app.main``, ``POST
/admin/batch-run``) and ``uv run python -m app.jobs.batch_update`` for a manual run.
Every entry point then runs the advisory dispatcher (``app.jobs.dispatch_advisories``),
which sends the queued advisories (R3); this module itself never sends anything.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import uuid
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Final, Protocol

from pydantic import AwareDatetime, BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.core.clinical_limits import GlucoseLimitKind
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ModelMetadataError,
    ModelNotTrainedError,
    ThresholdComputationError,
)
from app.core.logging import configure_logging
from app.db.batch_repository import SqlBatchRepository
from app.db.models import AdvisoryState, BatchStatus, BatchTrigger, Profile
from app.db.threshold_repository import SqlThresholdRepository
from app.ml.artifacts import DEFAULT_MODEL_DIR, model_paths
from app.ml.predict import RiskAssessment, RiskModel
from app.ml.schema import RiskProfile
from app.services.advisory import EvaluatedReading, plan_advisories
from app.services.thresholds import (
    UNSCORED_MODEL_VERSION,
    ReadingPoint,
    StoredThreshold,
    ThresholdDecision,
    ThresholdInputs,
    classify_reading,
    refresh_user_thresholds,
    threshold_values,
)

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

# batch_runs.error is free text shown on the dashboard; keep it short and PII-free
# (user ids and exception class names only - messages may echo row values).
_MAX_LISTED_FAILURES: Final = 10


class RiskScorer(Protocol):
    """What the job needs from the risk model (:class:`app.ml.predict.RiskModel`)."""

    @property
    def version(self) -> str:
        """Model version stored with each assessment."""
        ...

    def assess(self, profile: RiskProfile) -> RiskAssessment:
        """Score one profile.

        Args:
            profile: Validated BRFSS profile.

        Returns:
            Probability, tier, flag and model version.
        """
        ...


class BatchRunSummary(BaseModel):
    """Result of one run; also the response body of ``POST /admin/batch-run``.

    Attributes:
        run_id: ``batch_runs.id``.
        status: ``succeeded`` or ``failed`` (some users failed).
        triggered_by: Who started the run.
        window_start: Exclusive start of the readings window (``None``: everything).
        window_end: Inclusive end of the window; thresholds are computed as of it.
        model_version: Risk-model version, or ``"unscored"`` without a model.
        users_processed: Users whose transaction committed.
        users_skipped: Active patients without a diabetes status (no limit set applies).
        users_failed: Users rolled back because of an error.
        failed_user_ids: Ids of those users.
        risk_rescored: Users whose profile (or the model) changed and were re-scored.
        readings_evaluated: New readings classified.
        thresholds_written: ``thresholds_history`` rows written.
        advisories_created: Advisories inserted as ``queued``.
        advisories_by_state: Those advisories per band state.
    """

    run_id: uuid.UUID
    status: BatchStatus
    triggered_by: BatchTrigger
    window_start: AwareDatetime | None
    window_end: AwareDatetime
    model_version: str
    users_processed: int = Field(ge=0)
    users_skipped: int = Field(ge=0)
    users_failed: int = Field(ge=0)
    failed_user_ids: list[uuid.UUID]
    risk_rescored: int = Field(ge=0)
    readings_evaluated: int = Field(ge=0)
    thresholds_written: int = Field(ge=0)
    advisories_created: int = Field(ge=0)
    advisories_by_state: dict[AdvisoryState, int]


@dataclass
class _UserOutcome:
    """Counts from one user's committed transaction."""

    rescored: bool = False
    readings_evaluated: int = 0
    thresholds_written: int = 0
    advisories: Counter[AdvisoryState] = field(default_factory=Counter)


# ---------------------------------------------------------------------------
# Risk: re-score only when the profile changed
# ---------------------------------------------------------------------------


def risk_profile_from(profile: Profile) -> RiskProfile | None:
    """Build the model input from a profile row.

    Args:
        profile: The ``profiles`` row.

    Returns:
        The validated profile, or ``None`` if onboarding is incomplete (any BRFSS
        field missing): such users are unscored and get no risk adjustment (R2).
    """
    data = {name: getattr(profile, name) for name in RiskProfile.model_fields if name != "age"}
    # BRFSS models age as a 13-level bucket; Postgres derives it from age_years.
    data["age"] = profile.brfss_age_bucket
    try:
        return RiskProfile.model_validate(data)
    except ValidationError:
        return None


def profile_digest(profile: RiskProfile) -> str:
    """Fingerprint the model inputs, to tell whether a profile changed.

    Args:
        profile: Validated model input.

    Returns:
        SHA-256 hex digest of the canonical JSON of every model feature.
    """
    return hashlib.sha256(profile.model_dump_json().encode()).hexdigest()


def resolve_risk(
    repo: SqlBatchRepository,
    *,
    profile: Profile,
    scorer: RiskScorer | None,
    batch_run_id: uuid.UUID,
) -> tuple[RiskAssessment | None, bool]:
    """Return the user's risk assessment, re-scoring only if the profile changed.

    Args:
        repo: Batch persistence (the user's transaction).
        profile: The user's profile row.
        scorer: Loaded risk model, or ``None`` if unavailable.
        batch_run_id: Current run, recorded on a new assessment.

    Returns:
        ``(assessment or None if unscored, whether the model was run)``.
    """
    risk_profile = risk_profile_from(profile)
    if risk_profile is None or scorer is None:
        return None, False
    digest = profile_digest(risk_profile)
    latest = repo.latest_risk_assessment(profile.user_id)
    if (
        latest is not None
        and latest.profile_hash == digest
        and latest.model_version == scorer.version
    ):
        return (
            RiskAssessment(
                probability=float(latest.risk_score),
                tier=latest.risk_tier,
                flagged=latest.flagged,
                model_version=latest.model_version,
            ),
            False,
        )
    assessment = scorer.assess(risk_profile)
    repo.save_risk_assessment(
        user_id=profile.user_id,
        profile_hash=digest,
        assessment=assessment,
        batch_run_id=batch_run_id,
    )
    logger.info(
        "risk re-scored for user %s: %s (%.3f, model %s)",
        profile.user_id,
        assessment.tier,
        assessment.probability,
        assessment.model_version,
    )
    return assessment, True


# ---------------------------------------------------------------------------
# One user
# ---------------------------------------------------------------------------


def _stored(
    decisions: dict[GlucoseLimitKind, ThresholdDecision],
) -> dict[GlucoseLimitKind, StoredThreshold]:
    """Extract the stored threshold rows from refresh decisions.

    Args:
        decisions: Output of :func:`refresh_user_thresholds`.

    Returns:
        The stored row per limit kind (advisories reference its id).

    Raises:
        ThresholdComputationError: If a decision carries an unsaved threshold.
    """
    stored: dict[GlucoseLimitKind, StoredThreshold] = {}
    for kind, decision in decisions.items():
        if not isinstance(decision.threshold, StoredThreshold):
            raise ThresholdComputationError(f"{kind} threshold was not persisted")
        stored[kind] = decision.threshold
    return stored


def process_user(
    session: Session,
    *,
    profile: Profile,
    inputs: ThresholdInputs,
    scorer: RiskScorer | None,
    batch_run_id: uuid.UUID,
    window_start: datetime | None,
    window_end: datetime,
    settings: Settings,
) -> _UserOutcome:
    """Run every batch step for one user inside the caller's transaction.

    Args:
        session: The user's session; the caller commits or rolls back.
        profile: The user's profile row.
        inputs: Threshold-engine view of the profile.
        scorer: Risk model, or ``None``.
        batch_run_id: Current run.
        window_start: Exclusive start of the readings window.
        window_end: Inclusive end; thresholds are computed as of this moment.
        settings: Band and cooldown configuration.

    Returns:
        Counts for the run summary.

    Raises:
        ThresholdComputationError: If thresholds cannot be computed or applied.
    """
    batch_repo = SqlBatchRepository(session)
    outcome = _UserOutcome()
    user_id = profile.user_id

    # R1 step 2: risk tier only when the profile changed, then thresholds (R2).
    risk, outcome.rescored = resolve_risk(
        batch_repo, profile=profile, scorer=scorer, batch_run_id=batch_run_id
    )
    decisions = refresh_user_thresholds(
        SqlThresholdRepository(session, created_before=window_end),
        user_id=user_id,
        inputs=inputs,
        risk=risk,
        as_of=window_end,
        batch_run_id=batch_run_id,
    )
    outcome.thresholds_written = sum(d.changed for d in decisions.values())
    thresholds = _stored(decisions)

    # R1 step 3: evaluate each new reading against the band (R3 trigger).
    readings = batch_repo.readings_in_window(user_id, window_start, window_end)
    outcome.readings_evaluated = len(readings)
    if not readings:
        return outcome
    values = threshold_values(thresholds)
    evaluated = [
        EvaluatedReading(
            reading_id=r.id,
            measured_at=r.measured_at,
            context=r.context,
            value_mg_dl=r.value_mg_dl,
            band=classify_reading(
                ReadingPoint.model_validate(r), values, band=settings.pre_breach_band
            ),
        )
        for r in readings
    ]

    # R3: de-duplicate and apply the cooldown against advisories near these readings.
    cooldown = timedelta(hours=settings.advisory_cooldown_hours)
    first = min(e.measured_at for e in evaluated)
    last = max(e.measured_at for e in evaluated)
    plan = plan_advisories(
        evaluated,
        batch_repo.prior_advisories(user_id, first - cooldown, last + cooldown),
        cooldown=cooldown,
        preferred_language=profile.language,
    )
    created = batch_repo.insert_advisories(
        user_id=user_id,
        batch_run_id=batch_run_id,
        drafts=plan.drafts,
        threshold_ids={kind: t.id for kind, t in thresholds.items()},
    )
    outcome.advisories.update(created)
    return outcome


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _failure_summary(failed: list[tuple[uuid.UUID, str]], total: int) -> str:
    """Describe failed users for ``batch_runs.error`` without row data.

    Args:
        failed: ``(user_id, exception class name)`` per failed user.
        total: Users attempted.

    Returns:
        E.g. ``"2 of 20 users failed: <id> (ValueError), ..."``.
    """
    listed = ", ".join(f"{uid} ({name})" for uid, name in failed[:_MAX_LISTED_FAILURES])
    more = len(failed) - _MAX_LISTED_FAILURES
    suffix = f", and {more} more" if more > 0 else ""
    return f"{len(failed)} of {total} users failed: {listed}{suffix}"


def _open_run(
    session_factory: SessionFactory,
    *,
    now: datetime | None,
    window: tuple[datetime | None, datetime] | None,
    triggered_by: BatchTrigger,
    model_version: str,
    user_ids: Collection[uuid.UUID] | None,
) -> tuple[uuid.UUID, datetime | None, datetime, list[Profile]]:
    """Resolve the window, claim the run (``running`` row) and list the patients.

    Committed in its own short transaction so the ``running`` row is visible to (and
    blocks) any concurrent starter while users are processed.

    Args:
        session_factory: Creates the session.
        now: Current time, or ``None`` for the database clock.
        window: Explicit ``(start, end)``, or ``None`` for (last success, now].
        triggered_by: Recorded in ``batch_runs``.
        model_version: Recorded in ``batch_runs``.
        user_ids: Restrict to these users, or ``None``.

    Returns:
        ``(run_id, window_start, window_end, profiles)``.

    Raises:
        BatchAlreadyRunningError: If another batch update is in progress.
        ThresholdComputationError: If a time is naive or the window is empty.
    """
    with session_factory() as session:
        repo = SqlBatchRepository(session)
        now = now or repo.database_now()
        abandoned = repo.abandon_stale_runs(now)
        if abandoned:
            logger.warning("marked %d stale batch_update run(s) as failed", abandoned)
        start, end = window if window is not None else (repo.last_successful_window_end(), now)
        for name, moment in (("now", now), ("window end", end), ("window start", start)):
            if moment is not None and moment.utcoffset() is None:
                raise ThresholdComputationError(f"{name} must be timezone-aware")
        if start is not None and start >= end:
            raise ThresholdComputationError(f"empty batch window ({start} >= {end})")
        run_id = repo.start_run(
            triggered_by=triggered_by,
            window_start=start,
            window_end=end,
            model_version=model_version,
        )
        profiles = repo.active_patient_profiles(user_ids)
        # Detach before commit: detached rows keep their loaded values, so they stay
        # readable in the per-user transactions whatever expire_on_commit is set to.
        session.expunge_all()
        session.commit()
    logger.info(
        "batch run %s started: %d patients, window (%s, %s]", run_id, len(profiles), start, end
    )
    return run_id, start, end, profiles


def run_batch_update(
    session_factory: SessionFactory,
    *,
    scorer: RiskScorer | None,
    triggered_by: BatchTrigger = "scheduler",
    now: datetime | None = None,
    window: tuple[datetime | None, datetime] | None = None,
    user_ids: Collection[uuid.UUID] | None = None,
    settings: Settings | None = None,
) -> BatchRunSummary:
    """Run the batch update once (R1).

    Args:
        session_factory: Creates sessions; each user gets its own transaction.
        scorer: Risk model, or ``None`` to leave every user unscored.
        triggered_by: Recorded in ``batch_runs``.
        now: Current time (tests); defaults to the database clock, the same clock
            that stamps ``readings.created_at``.
        window: Explicit ``(start, end)`` to (re-)process; by default the window
            runs from the last succeeded run's end to ``now``.
        user_ids: Only process these users (tests, targeted re-runs).
        settings: Configuration; defaults to :func:`get_settings`.

    Returns:
        Counts and status of the run.

    Raises:
        BatchAlreadyRunningError: If another batch update is in progress.
        ThresholdComputationError: If ``now`` or the window bounds are naive or the
            window is empty/inverted.
    """
    settings = settings or get_settings()
    model_version = scorer.version if scorer is not None else UNSCORED_MODEL_VERSION
    if scorer is None:
        logger.warning("batch update running without a risk model: users stay unscored")
    run_id, start, end, profiles = _open_run(
        session_factory,
        now=now,
        window=window,
        triggered_by=triggered_by,
        model_version=model_version,
        user_ids=user_ids,
    )

    totals = _UserOutcome()
    processed = skipped = rescored = 0
    failed: list[tuple[uuid.UUID, str]] = []
    for profile in profiles:
        if profile.diabetes_status is None:
            # No limit set can be chosen (ThresholdInputs); onboarding will fill it in.
            skipped += 1
            logger.info("user %s skipped: no diabetes status yet", profile.user_id)
            continue
        inputs = ThresholdInputs.model_validate(profile)
        # One transaction per user: a failure rolls back only this user (R1).
        with session_factory() as session:
            try:
                outcome = process_user(
                    session,
                    profile=profile,
                    inputs=inputs,
                    scorer=scorer,
                    batch_run_id=run_id,
                    window_start=start,
                    window_end=end,
                    settings=settings,
                )
                session.commit()
            except Exception as exc:
                session.rollback()
                failed.append((profile.user_id, type(exc).__name__))
                logger.exception("batch run %s: user %s failed", run_id, profile.user_id)
                continue
        processed += 1
        rescored += outcome.rescored
        totals.readings_evaluated += outcome.readings_evaluated
        totals.thresholds_written += outcome.thresholds_written
        totals.advisories.update(outcome.advisories)

    status: BatchStatus = "failed" if failed else "succeeded"
    error = _failure_summary(failed, len(profiles) - skipped) if failed else None
    advisories_created = sum(totals.advisories.values())
    with session_factory() as session:
        SqlBatchRepository(session).finish_run(
            run_id,
            status=status,
            finished_at=datetime.now(UTC),
            users_processed=processed,
            readings_evaluated=totals.readings_evaluated,
            thresholds_written=totals.thresholds_written,
            advisories_created=advisories_created,
            error=error,
        )
        session.commit()

    summary = BatchRunSummary(
        run_id=run_id,
        status=status,
        triggered_by=triggered_by,
        window_start=start,
        window_end=end,
        model_version=model_version,
        users_processed=processed,
        users_skipped=skipped,
        users_failed=len(failed),
        failed_user_ids=[uid for uid, _ in failed],
        risk_rescored=rescored,
        readings_evaluated=totals.readings_evaluated,
        thresholds_written=totals.thresholds_written,
        advisories_created=advisories_created,
        advisories_by_state=dict(totals.advisories),
    )
    log = logger.error if failed else logger.info
    log(
        "batch run %s %s: %d users, %d readings, %d thresholds, %d advisories %s",
        run_id,
        status,
        processed,
        summary.readings_evaluated,
        summary.thresholds_written,
        advisories_created,
        summary.advisories_by_state,
    )
    return summary


def load_risk_scorer() -> RiskScorer | None:
    """Return the current risk model, reloading it after a retrain promoted a new one.

    Cached on the metadata file's modification time: the model is loaded once, and
    again only when ``app.jobs.retrain`` (in this or another process) replaces the
    files. The next batch run then records the new ``model_version`` with each risk
    assessment (audit only; the tier never moves a glucose limit).

    Returns:
        The model, or ``None`` if it has not been trained yet (logged as an error:
        the job still runs, but users are left unscored until it exists).
    """
    _, meta_path = model_paths(DEFAULT_MODEL_DIR)
    try:
        stamp: int | None = meta_path.stat().st_mtime_ns
    except FileNotFoundError:
        stamp = None
    return cached_risk_scorer(stamp)


@lru_cache(maxsize=1)
def cached_risk_scorer(meta_mtime_ns: int | None) -> RiskScorer | None:
    """Load the risk model; cached per metadata version (see :func:`load_risk_scorer`).

    Args:
        meta_mtime_ns: Modification time of ``risk_model.meta.json`` (cache key only).

    Returns:
        The model, or ``None`` if it is missing or its metadata is invalid (logged).
    """
    try:
        return RiskModel.load()
    except (ModelNotTrainedError, ModelMetadataError):
        logger.exception("risk model unavailable; batch update will leave users unscored")
        return None


def main(argv: list[str] | None = None) -> int:
    """Run one batch update from the command line (``triggered_by='manual'``), then dispatch.

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 if the run succeeded, 1 otherwise.
    """
    # CLI-only imports: the dispatcher pulls in Web Push and TTS clients.
    from app.db.session import get_session_factory  # noqa: PLC0415
    from app.jobs.dispatch_advisories import scheduled_dispatch  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Run the R1 batch update once.")
    parser.add_argument(
        "--no-dispatch",
        action="store_true",
        help="only queue advisories; do not send them (R3 dispatch)",
    )
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    summary = run_batch_update(
        get_session_factory(), scorer=load_risk_scorer(), triggered_by="manual"
    )
    print(summary.model_dump_json(indent=2))
    if not args.no_dispatch:
        scheduled_dispatch()
    return 0 if summary.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
