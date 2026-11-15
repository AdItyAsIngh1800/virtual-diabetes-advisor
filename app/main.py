"""FastAPI application and the in-process batch scheduler (R1).

Run with ``uv run uvicorn app.main:app``. On startup the app schedules the batch update
every ``BATCH_INTERVAL_MINUTES`` (default 15) with APScheduler, unless
``BATCH_SCHEDULER_ENABLED=false``. ``POST /admin/batch-run`` triggers a run on demand.
``/webhooks/whatsapp`` receives WhatsApp Cloud API events (see ``app.api.webhooks``).
Scheduled and manual runs cannot overlap: the second gets ``BatchAlreadyRunningError``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from app.api import admin, webhooks
from app.core.config import Settings, get_settings
from app.core.exceptions import BatchAlreadyRunningError
from app.core.logging import configure_logging
from app.db.session import get_session_factory
from app.jobs.batch_update import load_risk_scorer, run_batch_update

logger = logging.getLogger(__name__)

BATCH_JOB_ID = "batch_update"


def scheduled_batch_run() -> None:
    """APScheduler entry point: one batch update, never raising into the scheduler.

    A run already in progress (e.g. started from the admin API) is skipped. Other
    errors are logged with the traceback; the scheduler keeps its timetable and the
    next run retries the same window (see ``app.jobs.batch_update``).
    """
    try:
        run_batch_update(get_session_factory(), scorer=load_risk_scorer())
    except BatchAlreadyRunningError:
        logger.info("scheduled batch update skipped: another run is in progress")
    except Exception:
        logger.exception("scheduled batch update failed")


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    """Create (not start) the scheduler with the batch job registered.

    Args:
        settings: Supplies ``batch_interval_minutes``.

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
        logger.info("batch update scheduled every %d min", settings.batch_interval_minutes)
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
        The app with the admin and WhatsApp webhook routers mounted.
    """
    app = FastAPI(title="Virtual Diabetes Advisor", version="0.1.0", lifespan=lifespan)
    app.include_router(admin.router)
    app.include_router(webhooks.router)
    return app


app = create_app()
