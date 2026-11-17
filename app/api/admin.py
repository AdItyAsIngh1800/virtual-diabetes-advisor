"""Admin routes: run the batch update, dispatcher, KB upgrade and model retrain on demand.

Every route requires the ``X-Admin-Api-Key`` header to equal ``ADMIN_API_KEY``. The
check fails closed: with no key configured, every request gets 503.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    BatchAlreadyRunningError,
    ConfigurationError,
    TrainingDataMissingError,
)
from app.db.session import get_session_factory
from app.jobs.batch_update import BatchRunSummary, load_risk_scorer, run_batch_update
from app.jobs.dispatch_advisories import DispatchSummary, run_dispatch, scheduled_dispatch
from app.jobs.kb_upgrade import KbUpgradeSummary, run_kb_upgrade
from app.jobs.retrain import RetrainSummary, run_retrain

logger = logging.getLogger(__name__)

ADMIN_KEY_HEADER = "X-Admin-Api-Key"


def require_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    x_admin_api_key: Annotated[str | None, Header(alias=ADMIN_KEY_HEADER)] = None,
) -> None:
    """Reject the request unless it carries the configured admin key.

    Args:
        settings: Application settings.
        x_admin_api_key: Value of the ``X-Admin-Api-Key`` header.

    Raises:
        HTTPException: 503 if no key is configured, 401 if the header is missing
            or wrong.
    """
    if settings.admin_api_key is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "admin API is not configured")
    expected = settings.admin_api_key.get_secret_value().encode()
    # Constant-time comparison: response timing must not reveal how much matched.
    if x_admin_api_key is None or not hmac.compare_digest(x_admin_api_key.encode(), expected):
        logger.warning("rejected admin request: missing or invalid %s", ADMIN_KEY_HEADER)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid admin API key",
            headers={"WWW-Authenticate": ADMIN_KEY_HEADER},
        )


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("/batch-run")
def trigger_batch_run() -> BatchRunSummary:
    """Run the batch update now, then dispatch new advisories, and return the batch summary.

    Runs synchronously (FastAPI executes plain ``def`` routes in a worker thread). A
    run with failed users still returns 200; check ``status`` and ``failed_user_ids``.
    The dispatch outcome is logged (``POST /admin/dispatch-run`` returns it).

    Returns:
        Counts and status of the run.

    Raises:
        HTTPException: 409 if a batch update is already in progress.
    """
    try:
        summary = run_batch_update(
            get_session_factory(), scorer=load_risk_scorer(), triggered_by="admin_api"
        )
    except BatchAlreadyRunningError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    scheduled_dispatch()
    return summary


@router.post("/dispatch-run")
def trigger_dispatch_run() -> DispatchSummary:
    """Deliver queued advisories now (R3) and return what happened.

    Returns:
        Counts of sent, failed, expired, suppressed and blocked advisories.

    Raises:
        HTTPException: 503 if Web Push is not configured, 409 if a dispatch is
            already running in this process.
    """
    try:
        summary = run_dispatch()
    except ConfigurationError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if summary is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "a dispatch is already running")
    return summary


@router.post("/kb-upgrade-run")
def trigger_kb_upgrade_run() -> KbUpgradeSummary:
    """Run the knowledge-base upgrade job now (R5) and return what it proposed.

    It only inserts empty DRAFT entries for practitioners to write and approve; it
    never publishes anything. A run that failed still returns 200 with ``status``
    ``failed`` (the error is in ``batch_runs`` and the log).

    Returns:
        Counts, status and the ids of the new drafts.

    Raises:
        HTTPException: 409 if a KB upgrade run is already in progress.
    """
    try:
        return run_kb_upgrade(get_session_factory(), triggered_by="admin_api")
    except BatchAlreadyRunningError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/retrain-run")
def trigger_retrain_run(
    dry_run: bool = False,
    sample: Annotated[float | None, Query(gt=0, le=1)] = None,
) -> RetrainSummary:
    """Retrain the risk model now (R5) and return the promote/keep decision.

    Runs synchronously: a full training takes a few minutes (``sample`` makes it
    quicker). The candidate replaces the live model only if its test recall and
    precision hold up; thresholds are never touched. A run whose training failed
    still returns 200 with ``status`` ``failed`` and decision ``kept``.

    Args:
        dry_run: Train and report, never promote.
        sample: Share of rows to train on, e.g. ``0.2``.

    Returns:
        Status, decision, old and new metrics.

    Raises:
        HTTPException: 503 if the BRFSS CSVs are missing, 409 if a retrain is
            already in progress.
    """
    try:
        return run_retrain(
            get_session_factory(), triggered_by="admin_api", dry_run=dry_run, sample_frac=sample
        )
    except TrainingDataMissingError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except BatchAlreadyRunningError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
