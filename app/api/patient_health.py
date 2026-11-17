"""The patient's own limits, readings and alerts (``GET /api/me/health``, R6).

Read-only, and only ever the signed-in patient's own data: the patient is resolved
from the login (``_require_patient``), never from a request parameter. The response
has no risk score or tier (patients never see the survey-based risk) and no threshold
components, only the current limits. It feeds the patient app's "My limits" card and
its Readings and Alerts pages.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import current_auth_user
from app.api.chat import _require_patient
from app.core.config import Settings, get_settings
from app.db.dashboard_repository import MAX_DETAIL_DAYS, OwnHealth, own_health
from app.db.session import get_session_factory
from app.services.supabase_auth import AuthUser

router = APIRouter(tags=["patient-app"])

DEFAULT_DAYS: Final = 30


@router.get("/api/me/health")
def get_own_health(
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    settings: Annotated[Settings, Depends(get_settings)],
    days: Annotated[int, Query(ge=1, le=MAX_DETAIL_DAYS)] = DEFAULT_DAYS,
) -> OwnHealth:
    """Return the signed-in patient's current limits, recent readings and alerts.

    Args:
        auth_user: Verified login.
        session_factory: Database sessions.
        settings: Settings (pre-breach band).
        days: Days of readings.

    Returns:
        The patient's own view.

    Raises:
        HTTPException: 401 without a login, 403 if the login is not a patient, 422 for
            ``days`` out of range.
    """
    with session_factory() as session:
        patient = _require_patient(session, auth_user)
        try:
            return own_health(session, patient.id, days=days, band=settings.pre_breach_band)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
