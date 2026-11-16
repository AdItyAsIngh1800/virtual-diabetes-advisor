"""Weekly knowledge-base upgrade job (R5): propose DRAFT entries for practitioners.

One run::

    claim a 'running' kb_upgrade row in batch_runs     # one run at a time (0005 index)
    load reviewed answers (latest verdict incorrect / needs_kb_update), "I don't know"
        replies from the last KB_UPGRADE_LOOKBACK_DAYS days, and the existing drafts
    plan_drafts(...)                                    # pure, app.services.kb_upgrade
    insert the drafts: status 'draft', title = the patient's question, body EMPTY;
        drafts from answers marked unsafe are priority, and an open draft an unsafe
        review matches is promoted to priority
    record kb_candidates / kb_drafts_proposed and the status in batch_runs

The job never writes medical content, never calls an LLM and never approves anything;
a practitioner writes, cites and approves each draft on the Knowledge base tab.

Idempotent: a turn produces at most one draft (checked in the plan and by
``kb_entries_one_proposal_per_turn_idx``) and a question with an open draft is skipped,
so running it twice in a row adds nothing the second time.

Entry points: APScheduler in ``app.main`` (weekly, when ``KB_UPGRADE_ENABLED``),
``POST /admin/kb-upgrade-run``, and ``uv run python -m app.jobs.kb_upgrade``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Final

from pydantic import AwareDatetime, BaseModel
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.exceptions import BatchAlreadyRunningError
from app.core.logging import configure_logging
from app.db.batch_repository import SqlBatchRepository
from app.db.kb_repository import insert_drafts, load_upgrade_inputs, promote_drafts
from app.db.models import BatchJob, BatchStatus, BatchTrigger
from app.services.kb_upgrade import plan_drafts

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

KB_UPGRADE_JOB: Final[BatchJob] = "kb_upgrade"
# Recorded in batch_runs.model_version: the planning rules, not an ML model. Bump it
# when app.services.kb_upgrade changes what it proposes.
RULESET_VERSION: Final = "kb-upgrade-rules-1"


class KbUpgradeSummary(BaseModel):
    """Result of one run; also the body of ``POST /admin/kb-upgrade-run``.

    Attributes:
        run_id: ``batch_runs.id``.
        status: ``succeeded`` or ``failed``.
        triggered_by: Who started the run.
        window_start: Start of the lookback for unanswered questions.
        window_end: When the run started (database clock).
        candidates: Distinct questions considered.
        drafts_proposed: Drafts inserted.
        skipped: Candidates not proposed (open draft, already proposed, no text).
        promoted: Open drafts marked priority because an unsafe review matched them.
        draft_ids: Ids of the new drafts.
        error: Failure summary (exception class only), or ``None``.
    """

    run_id: uuid.UUID
    status: BatchStatus
    triggered_by: BatchTrigger
    window_start: AwareDatetime
    window_end: AwareDatetime
    candidates: int
    drafts_proposed: int
    skipped: int
    promoted: int
    draft_ids: list[uuid.UUID]
    error: str | None


def run_kb_upgrade(
    session_factory: SessionFactory,
    *,
    triggered_by: BatchTrigger = "scheduler",
    settings: Settings | None = None,
) -> KbUpgradeSummary:
    """Propose draft KB entries once (R5).

    A failure while planning or inserting rolls the drafts back and marks the run
    ``failed`` (returned, not raised); the next run simply tries again.

    Args:
        session_factory: Creates sessions.
        triggered_by: Recorded in ``batch_runs``.
        settings: Configuration; defaults to :func:`get_settings`.

    Returns:
        Counts and status of the run.

    Raises:
        BatchAlreadyRunningError: Another ``kb_upgrade`` run is in progress.
    """
    settings = settings or get_settings()
    with session_factory() as session:
        repo = SqlBatchRepository(session)
        now = repo.database_now()
        abandoned = repo.abandon_stale_runs(now, job=KB_UPGRADE_JOB)
        if abandoned:
            logger.warning("marked %d stale kb_upgrade run(s) as failed", abandoned)
        since = now - timedelta(days=settings.kb_upgrade_lookback_days)
        # Committed on its own so a concurrent starter sees the 'running' row.
        run_id = repo.start_run(
            triggered_by=triggered_by,
            window_start=since,
            window_end=now,
            model_version=RULESET_VERSION,
            job=KB_UPGRADE_JOB,
        )
        session.commit()

    status: BatchStatus = "succeeded"
    error: str | None = None
    candidates = skipped = promoted = 0
    draft_ids: list[uuid.UUID] = []
    with session_factory() as session:
        try:
            inputs = load_upgrade_inputs(session, since=since)
            plan = plan_drafts(
                inputs.reviewed,
                inputs.unanswered,
                open_drafts=inputs.open_drafts,
                proposed_turn_ids=inputs.proposed_turn_ids,
                min_repeats=settings.kb_upgrade_min_repeats,
            )
            candidates, skipped = plan.candidates, plan.skipped
            draft_ids = insert_drafts(session, plan.proposals)
            promoted = promote_drafts(session, plan.promote_draft_ids)
            session.commit()
        except Exception as exc:
            session.rollback()
            status, error, draft_ids, promoted = "failed", type(exc).__name__, [], 0
            logger.exception("kb_upgrade run %s failed", run_id)

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
            kb_candidates=candidates,
            kb_drafts_proposed=len(draft_ids),
        )
        session.commit()

    log = logger.error if error else logger.info
    log(
        "kb_upgrade run %s %s: %d candidates, %d drafts proposed, %d promoted, %d skipped",
        run_id,
        status,
        candidates,
        len(draft_ids),
        promoted,
        skipped,
    )
    return KbUpgradeSummary(
        run_id=run_id,
        status=status,
        triggered_by=triggered_by,
        window_start=since,
        window_end=now,
        candidates=candidates,
        drafts_proposed=len(draft_ids),
        skipped=skipped,
        promoted=promoted,
        draft_ids=draft_ids,
        error=error,
    )


def scheduled_kb_upgrade() -> None:
    """APScheduler entry point: one run, never raising (errors are logged)."""
    # Imported here so importing this module does not build a database engine.
    from app.db.session import get_session_factory  # noqa: PLC0415

    try:
        run_kb_upgrade(get_session_factory())
    except BatchAlreadyRunningError:
        logger.info("scheduled kb_upgrade skipped: another run is in progress")
    except Exception:
        logger.exception("scheduled kb_upgrade failed")


def main() -> int:
    """Run the job once from the command line (``triggered_by='manual'``).

    Returns:
        Process exit code: 0 if the run succeeded, 1 otherwise.
    """
    from app.db.session import get_session_factory  # noqa: PLC0415

    configure_logging(get_settings().log_level)
    summary = run_kb_upgrade(get_session_factory(), triggered_by="manual")
    print(summary.model_dump_json(indent=2))
    return 0 if summary.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
