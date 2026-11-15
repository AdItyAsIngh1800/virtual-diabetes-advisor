"""Admin routes: trigger the batch update on demand (R1 demo).

Every route requires the ``X-Admin-Api-Key`` header to equal ``ADMIN_API_KEY``. The
check fails closed: with no key configured, every request gets 503.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings
from app.core.exceptions import BatchAlreadyRunningError
from app.db.session import get_session_factory
from app.jobs.batch_update import BatchRunSummary, load_risk_scorer, run_batch_update

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
    """Run the batch update now and return its summary (R1).

    Runs synchronously (FastAPI executes plain ``def`` routes in a worker thread). A
    run with failed users still returns 200; check ``status`` and ``failed_user_ids``.

    Returns:
        Counts and status of the run.

    Raises:
        HTTPException: 409 if a batch update is already in progress.
    """
    try:
        return run_batch_update(
            get_session_factory(), scorer=load_risk_scorer(), triggered_by="admin_api"
        )
    except BatchAlreadyRunningError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
