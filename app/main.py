"""FastAPI application and the in-process batch scheduler (R1).

Run with ``uv run uvicorn app.main:app``. On startup the app schedules the batch update
every ``BATCH_INTERVAL_MINUTES`` (default 15) and the advisory dispatcher (R3) every
``DISPATCH_INTERVAL_MINUTES`` (default 2) with APScheduler, unless
``BATCH_SCHEDULER_ENABLED=false``. Every batch run is followed by a dispatch, so new
advisories go out at once. ``POST /admin/batch-run`` triggers a run on demand. The
weekly knowledge-base upgrade (R5, ``KB_UPGRADE_ENABLED``) proposes draft entries for
practitioners to write and approve; ``POST /admin/kb-upgrade-run`` runs it on demand,
and ``/api/practitioner/kb`` is the practitioner's Knowledge base tab.
``/api/push/*`` manages Web Push subscriptions, ``/api/advisories/{id}`` serves one
advisory to its patient, ``/api/chat`` and ``/api/practitioner/*`` are the two-way help
chat and practitioner connect (R4), and ``/app`` serves the patient web app (Supabase
login, "Enable alerts", chat, the advisory page) and ``/app/practitioner``; see
``app.api``.
Scheduled and manual runs cannot overlap: the second gets ``BatchAlreadyRunningError``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC

import psycopg
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.api import admin, advisories, audit, chat, dashboard, kb_admin, patient_app, push
from app.core.config import Settings, get_settings
from app.core.exceptions import BatchAlreadyRunningError, ConfigurationError
from app.core.logging import configure_logging
from app.db.session import (
    DatabaseStatus,
    build_database_health,
    database_port,
    describe_connection_error,
    get_session_factory,
)
from app.jobs.batch_update import load_risk_scorer, run_batch_update
from app.jobs.dispatch_advisories import scheduled_dispatch
from app.jobs.kb_upgrade import scheduled_kb_upgrade

logger = logging.getLogger(__name__)

BATCH_JOB_ID = "batch_update"
DISPATCH_JOB_ID = "dispatch_advisories"
KB_UPGRADE_JOB_ID = "kb_upgrade"


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
    """Create (not start) the scheduler with the batch, dispatch and KB upgrade jobs.

    Args:
        settings: Supplies ``batch_interval_minutes``, ``dispatch_interval_minutes``
            and ``kb_upgrade_enabled``.

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
    if settings.kb_upgrade_enabled:
        # R5: weekly, Monday 02:30 UTC (08:00 in India), so the week's drafts are
        # waiting when the clinic opens. It only proposes empty drafts; a missed run
        # (server down) is harmless because the next one picks up the same questions.
        scheduler.add_job(
            scheduled_kb_upgrade,
            "cron",
            day_of_week="mon",
            hour=2,
            minute=30,
            id=KB_UPGRADE_JOB_ID,
            max_instances=1,
            coalesce=True,
        )
    return scheduler


def database_status_at_startup(settings: Settings) -> DatabaseStatus:
    """Check once at startup that the database answers (bounded by the connect timeout).

    Args:
        settings: Supplies ``DATABASE_URL``.

    Returns:
        The status. An unreachable database is logged as an error, never raised: the
        server still starts (the pages load and explain the problem) instead of
        hanging or crash-looping.
    """
    return build_database_health(settings).status()


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
    # In a worker thread: the probe may wait up to DB_CONNECT_TIMEOUT_S.
    db = await asyncio.to_thread(database_status_at_startup, settings)
    if db.reachable:
        logger.info("database reachable")
    else:
        logger.error(
            "%s - the server is up, but API calls that need the database will answer "
            "503 until it can be reached (try another network)",
            db.detail,
        )
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


async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """Answer 503 with a clear reason when a request cannot reach the database.

    SQLAlchemy raises ``OperationalError`` once the connect timeout expires; without
    this handler that would be a bare 500.

    Args:
        request: The failed request.
        exc: The database error.

    Returns:
        ``503 {"detail": "database unreachable (port 5432 blocked?)"}`` (or the
        credentials hint when the server rejected the login).
    """
    try:
        port = database_port(get_settings().database_url.get_secret_value())
    except (ConfigurationError, ValueError):
        port = 5432
    detail = describe_connection_error(exc, port)
    logger.error("%s %s: %s", request.method, request.url.path, detail)
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": detail})


def create_app(expose_docs: bool | None = None) -> FastAPI:
    """Build the FastAPI application.

    Args:
        expose_docs: Serve ``/docs``, ``/redoc`` and ``/openapi.json``. ``None`` reads
            ``EXPOSE_API_DOCS`` (off when settings cannot be loaded yet).

    Returns:
        The app with the admin, push, advisory, chat/practitioner, answer-review,
        knowledge-base and patient-app routes mounted.
    """
    if expose_docs is None:
        try:
            expose_docs = get_settings().expose_api_docs
        except ConfigurationError:
            expose_docs = False
    # None turns the route off: the API map would otherwise be public on the tunnel.
    app = FastAPI(
        title="Virtual Diabetes Advisor",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
        openapi_url="/openapi.json" if expose_docs else None,
    )
    app.add_exception_handler(OperationalError, database_unavailable)
    app.add_exception_handler(psycopg.OperationalError, database_unavailable)
    app.include_router(admin.router)
    app.include_router(push.router)
    app.include_router(advisories.router)
    app.include_router(chat.router)
    app.include_router(audit.router)
    app.include_router(kb_admin.router)
    app.include_router(dashboard.router)
    # Before mount_patient_app: the /app static mount would otherwise swallow
    # /app/advisory/{id}.
    app.include_router(patient_app.router)
    patient_app.mount_patient_app(app)
    return app


app = create_app()
