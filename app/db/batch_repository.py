"""SQL persistence for the batch update job (R1).

Everything the job reads or writes except thresholds (``app.db.threshold_repository``):
``batch_runs`` bookkeeping, the window's new readings, prior advisories for the
cooldown, risk assessments and advisory inserts. Like the threshold repository it never
commits; the job owns the transactions (one per user, plus short ones for the run row).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clinical_limits import GlucoseLimitKind
from app.core.exceptions import BatchAlreadyRunningError
from app.db.models import (
    Advisory,
    AdvisoryState,
    BatchJob,
    BatchRun,
    BatchStatus,
    BatchTrigger,
    Profile,
    Reading,
    RiskAssessmentRecord,
    User,
)
from app.ml.predict import RiskAssessment
from app.services.advisory import AdvisoryDraft, PriorAdvisory

BATCH_UPDATE_JOB: BatchJob = "batch_update"
# A 'running' row older than this belongs to a process that died mid-run (a run over a
# few hundred users takes minutes). Marking it failed releases the one-running index
# (0005) so the scheduler is not blocked forever by a crash.
STALE_RUN_AFTER = timedelta(hours=1)
# Same precision as risk_assessments.risk_score / thresholds_history.risk_score.
_RISK_SCORE_STEP = Decimal("0.0001")
# Postgres unique-violation SQLSTATE.
_UNIQUE_VIOLATION = "23505"


class SqlBatchRepository:
    """Batch-job persistence on a SQLAlchemy session.

    Args:
        session: Open session; the caller commits or rolls back.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- batch_runs ------------------------------------------------------------

    def database_now(self) -> datetime:
        """Return the database server's clock.

        The readings window is on ``readings.created_at``, which Postgres stamps with
        its own clock; ending the window on the same clock means local clock skew
        cannot leave freshly stored readings outside it.

        Returns:
            ``clock_timestamp()`` (timezone-aware).
        """
        moment = self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(moment, datetime):  # pragma: no cover - never NULL in Postgres
            raise TypeError(f"clock_timestamp() returned {type(moment).__name__}")
        return moment

    def last_successful_window_end(self, job: BatchJob = BATCH_UPDATE_JOB) -> datetime | None:
        """Return where the last *succeeded* run's window ended.

        Failed runs are ignored on purpose: their window is processed again, and the
        idempotency rules stop users who did succeed from getting duplicates.

        Args:
            job: Job name.

        Returns:
            The latest ``window_end`` of a succeeded run, or ``None`` if none exists.
        """
        stmt = select(func.max(BatchRun.window_end)).where(
            BatchRun.job == job, BatchRun.status == "succeeded"
        )
        return self._session.scalar(stmt)

    def abandon_stale_runs(self, now: datetime, job: BatchJob = BATCH_UPDATE_JOB) -> int:
        """Mark ``running`` rows older than :data:`STALE_RUN_AFTER` as failed.

        Args:
            now: Current time.
            job: Job name.

        Returns:
            How many rows were marked.
        """
        result = self._session.execute(
            update(BatchRun)
            .where(
                BatchRun.job == job,
                BatchRun.status == "running",
                BatchRun.started_at < now - STALE_RUN_AFTER,
            )
            .values(status="failed", finished_at=now, error="abandoned: still running after 1 h")
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    def start_run(
        self,
        *,
        triggered_by: BatchTrigger,
        window_start: datetime | None,
        window_end: datetime,
        model_version: str,
        job: BatchJob = BATCH_UPDATE_JOB,
    ) -> uuid.UUID:
        """Insert the ``running`` row for a new run.

        Args:
            triggered_by: ``scheduler``, ``admin_api`` or ``manual``.
            window_start: Exclusive start of the readings window (``None``: all).
            window_end: Inclusive end of the readings window.
            model_version: Risk-model version used by this run.
            job: Job name.

        Returns:
            The new run id.

        Raises:
            BatchAlreadyRunningError: If another run of ``job`` is in progress.
        """
        run = BatchRun(
            id=uuid.uuid4(),
            job=job,
            triggered_by=triggered_by,
            status="running",
            window_start=window_start,
            window_end=window_end,
            model_version=model_version,
        )
        try:
            # Savepoint: on a unique violation only this insert is undone, not the
            # caller's transaction (which may hold the stale-run cleanup).
            with self._session.begin_nested():
                self._session.add(run)
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
                raise BatchAlreadyRunningError(f"a {job} run is already in progress") from None
            raise
        return run.id

    def finish_run(
        self,
        run_id: uuid.UUID,
        *,
        status: BatchStatus,
        finished_at: datetime,
        users_processed: int,
        readings_evaluated: int,
        thresholds_written: int,
        advisories_created: int,
        error: str | None,
    ) -> None:
        """Record a run's final status and counts.

        Args:
            run_id: The run.
            status: ``succeeded`` or ``failed``.
            finished_at: End time.
            users_processed: Users whose transaction committed.
            readings_evaluated: New readings classified for those users.
            thresholds_written: ``thresholds_history`` rows written.
            advisories_created: ``advisories`` rows written.
            error: Failure summary, or ``None``.
        """
        self._session.execute(
            update(BatchRun)
            .where(BatchRun.id == run_id)
            .values(
                status=status,
                finished_at=finished_at,
                users_processed=users_processed,
                readings_evaluated=readings_evaluated,
                thresholds_written=thresholds_written,
                advisories_created=advisories_created,
                error=error,
            )
        )

    # --- users and readings ------------------------------------------------------

    def active_patient_profiles(
        self, user_ids: Collection[uuid.UUID] | None = None
    ) -> list[Profile]:
        """Return the profiles of every active patient.

        Args:
            user_ids: Restrict to these users (tests, targeted re-runs); ``None``: all.

        Returns:
            Profiles ordered by user id, so runs process users in a stable order.
        """
        stmt = (
            select(Profile)
            .join(User, User.id == Profile.user_id)
            .where(User.is_active, User.role == "patient")
            .order_by(Profile.user_id)
        )
        if user_ids is not None:
            stmt = stmt.where(Profile.user_id.in_(list(user_ids)))
        return list(self._session.scalars(stmt))

    def readings_in_window(
        self, user_id: uuid.UUID, window_start: datetime | None, window_end: datetime
    ) -> list[Reading]:
        """Return readings *stored* in ``(window_start, window_end]``.

        The window is on ``created_at`` (ingestion), not ``measured_at``: a reading
        reported hours late is still picked up by the next run.

        Args:
            user_id: The user.
            window_start: Exclusive start, or ``None`` for no lower bound.
            window_end: Inclusive end.

        Returns:
            Readings, oldest measurement first.
        """
        stmt = (
            select(Reading)
            .where(Reading.user_id == user_id, Reading.created_at <= window_end)
            .order_by(Reading.measured_at, Reading.id)
        )
        if window_start is not None:
            stmt = stmt.where(Reading.created_at > window_start)
        return list(self._session.scalars(stmt))

    # --- advisories --------------------------------------------------------------

    def prior_advisories(
        self, user_id: uuid.UUID, measured_from: datetime, measured_to: datetime
    ) -> list[PriorAdvisory]:
        """Return advisories whose readings were measured in ``[from, to]``.

        Args:
            user_id: The user.
            measured_from: Earliest reading time of interest (inclusive).
            measured_to: Latest reading time of interest (inclusive).

        Returns:
            Existing advisories, for the idempotency and cooldown checks.
        """
        stmt = (
            select(
                Advisory.reading_id,
                Advisory.limit_kind,
                Advisory.state,
                Reading.measured_at.label("reading_measured_at"),
                Advisory.delivery_status,
            )
            .join(Reading, Reading.id == Advisory.reading_id)
            .where(
                Advisory.user_id == user_id,
                Reading.measured_at >= measured_from,
                Reading.measured_at <= measured_to,
            )
        )
        return [PriorAdvisory.model_validate(row) for row in self._session.execute(stmt)]

    def insert_advisories(
        self,
        *,
        user_id: uuid.UUID,
        batch_run_id: uuid.UUID,
        drafts: Sequence[AdvisoryDraft],
        threshold_ids: Mapping[GlucoseLimitKind, uuid.UUID],
    ) -> list[AdvisoryState]:
        """Insert advisories as ``queued``; rows that already exist are skipped.

        ``ON CONFLICT DO NOTHING`` on ``(reading_id, limit_kind)`` backs up the
        planner's idempotency check at the database level (R1).

        Args:
            user_id: The user.
            batch_run_id: Run creating them.
            drafts: Planned advisories.
            threshold_ids: ``thresholds_history.id`` per limit kind used to judge.

        Returns:
            The state of each row actually inserted.
        """
        if not drafts:
            return []
        rows = [
            {
                "id": uuid.uuid4(),
                "user_id": user_id,
                "reading_id": d.reading_id,
                "threshold_id": threshold_ids[d.limit_kind],
                "batch_run_id": batch_run_id,
                "limit_kind": d.limit_kind,
                "state": d.state,
                "reading_value_mg_dl": d.reading_value_mg_dl,
                "threshold_mg_dl": d.threshold_mg_dl,
                "language": d.language,
                "message_text": d.message_text,
                "escalated": d.escalated,
                # R3: Phase 4 only queues; Phase 5 delivery picks up 'queued' rows.
                "delivery_status": "queued",
            }
            for d in drafts
        ]
        stmt = (
            insert(Advisory)
            .values(rows)
            .on_conflict_do_nothing(constraint="advisories_reading_limit_key")
            .returning(Advisory.state)
        )
        return list(self._session.scalars(stmt))

    # --- risk assessments ----------------------------------------------------------

    def latest_risk_assessment(self, user_id: uuid.UUID) -> RiskAssessmentRecord | None:
        """Return the user's most recent stored risk assessment.

        Args:
            user_id: The user.

        Returns:
            The latest row, or ``None`` if the user was never scored.
        """
        stmt = (
            select(RiskAssessmentRecord)
            .where(RiskAssessmentRecord.user_id == user_id)
            .order_by(RiskAssessmentRecord.created_at.desc(), RiskAssessmentRecord.id)
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def save_risk_assessment(
        self,
        *,
        user_id: uuid.UUID,
        profile_hash: str,
        assessment: RiskAssessment,
        batch_run_id: uuid.UUID | None,
    ) -> RiskAssessmentRecord:
        """Store a fresh risk assessment.

        Args:
            user_id: The user.
            profile_hash: SHA-256 of the scored profile.
            assessment: Model output.
            batch_run_id: Run that scored it.

        Returns:
            The stored row (flushed, not committed).
        """
        record = RiskAssessmentRecord(
            id=uuid.uuid4(),
            user_id=user_id,
            profile_hash=profile_hash,
            model_version=assessment.model_version,
            risk_score=Decimal(str(assessment.probability)).quantize(_RISK_SCORE_STEP),
            risk_tier=assessment.tier,
            flagged=assessment.flagged,
            batch_run_id=batch_run_id,
        )
        self._session.add(record)
        self._session.flush()
        return record
