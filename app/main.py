"""FastAPI application and the in-process batch scheduler (R1).

Run with ``uv run uvicorn app.main:app``. On startup the app schedules the batch update
every ``BATCH_INTERVAL_MINUTES`` (default 15) and the advisory dispatcher (R3) every
``DISPATCH_INTERVAL_MINUTES`` (default 2) with APScheduler, unless
``BATCH_SCHEDULER_ENABLED=false``. Every batch run is followed by a dispatch, so new
advisories go out at once. ``POST /admin/batch-run`` triggers a run on demand.
``/api/push/*`` manages Web Push subscriptions, ``/api/advisories/{id}`` serves one
advisory to its patient, ``/api/chat`` and ``/api/practitioner/*`` are the two-way help
chat and practitioner connect (R4), and ``/app`` serves the patient web app (Supabase
login, "Enable alerts", chat, the advisory page) and ``/app/practitioner``; see
``app.api``.
Scheduled and manual runs cannot overlap: the second gets ``BatchAlreadyRunningError``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from app.api import admin, advisories, chat, patient_app, push
from app.core.config import Settings, get_settings
from app.core.exceptions import BatchAlreadyRunningError
from app.core.logging import configure_logging
from app.db.session import get_session_factory
from app.jobs.batch_update import load_risk_scorer, run_batch_update
from app.jobs.dispatch_advisories import scheduled_dispatch

logger = logging.getLogger(__name__)

BATCH_JOB_ID = "batch_update"
DISPATCH_JOB_ID = "dispatch_advisories"


def scheduled_batch_run() -> None:
    """APScheduler entry point: one batch update then a dispatch, never raising.

    A run already in progress (e.g. started from the admin API) is skipped. Other
    errors are logged with the traceback; the scheduler keeps its timetable and the
    next run retries the same window (see ``app.jobs.batch_update``). The dispatch
    runs even after a failed batch: advisories of users that did commit are queued.
    """
    try:
        run_batch_update(get_session_factory(), scorer=load_risk_scorer())
    except BatchAlreadyRunningError:
        logger.info("scheduled batch update skipped: another run is in progress")
    except Exception:
        logger.exception("scheduled batch update failed")
    scheduled_dispatch()


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    """Create (not start) the scheduler with the batch and dispatch jobs registered.

    Args:
        settings: Supplies ``batch_interval_minutes`` and ``dispatch_interval_minutes``.

    Returns:
        A configured, stopped scheduler.
    """
    scheduler = BackgroundScheduler(timezone=UTC)
    scheduler.add_job(
        scheduled_batch_run,
        "interval",
        minutes=settings.batch_interval_minutes,
        id=BATCH_JOB_ID,
        # One at a time, and after downtime run once rather than replaying every
        # missed slot: each run already covers everything since the last success.
        max_instances=1,
        coalesce=True,
    )
    # R3: also dispatch between batch runs, so an advisory queued by any path waits
    # at most a couple of minutes.
    scheduler.add_job(
        scheduled_dispatch,
        "interval",
        minutes=settings.dispatch_interval_minutes,
        id=DISPATCH_JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    return scheduler


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Configure logging and run the batch scheduler for the app's lifetime.

    Args:
        _app: The application (unused).

    Yields:
        Control while the app serves requests.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    scheduler: BackgroundScheduler | None = None
    if settings.batch_scheduler_enabled:
        scheduler = build_scheduler(settings)
        scheduler.start()
        logger.info(
            "batch update every %d min, advisory dispatch every %d min",
            settings.batch_interval_minutes,
            settings.dispatch_interval_minutes,
        )
    try:
        yield
    finally:
        if scheduler is not None:
            # Don't block shutdown on a running batch; its user transactions are
            # atomic and the run row is reclaimed as stale after an hour.
            scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    """Build the FastAPI application.

    Returns:
        The app with the admin, push, advisory, chat/practitioner and patient-app
        routes mounted.
    """
    app = FastAPI(title="Virtual Diabetes Advisor", version="0.1.0", lifespan=lifespan)
    app.include_router(admin.router)
    app.include_router(push.router)
    app.include_router(advisories.router)
    app.include_router(chat.router)
    # Before mount_patient_app: the /app static mount would otherwise swallow
    # /app/advisory/{id}.
    app.include_router(patient_app.router)
    patient_app.mount_patient_app(app)
    return app


app = create_app()
