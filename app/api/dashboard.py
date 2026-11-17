"""Practitioner dashboard API (R6): read-only views over visible patients.

Practitioner only (``_require_practitioner``, the same check as the handoff inbox and
the Review tab), and only for the patients :func:`visible_patient_ids` allows; an admin
sees every patient. Nothing here writes readings, thresholds or advisories.

* ``GET /api/dashboard/overview?since&until`` - KPI counts. Default windows: readings
  from local midnight (``Settings.dashboard_timezone``), alerts over the last 24 h.
* ``GET /api/dashboard/patients`` - one row per visible patient; ``limit`` + ``cursor``
  pagination; filters ``band_state``, ``breach_7d``, ``live_handoff``.
* ``GET /api/dashboard/patients/{user_id}?days=30`` - readings, threshold history with
  every component, pre-breach band, latest risk assessment, last 20 advisories. 404
  for a patient that does not exist or is not visible (the two are indistinguishable).
* ``GET /api/dashboard/alerts`` - advisories, newest reading first; ``limit`` +
  ``cursor``; filters ``state``, ``escalated``, ``delivery_status``, ``since``, ``until``.

Patients appear by first name only: no phone number, e-mail or push endpoint.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AwareDatetime
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import current_auth_user
from app.api.chat import _require_practitioner
from app.core.config import Settings, get_settings
from app.db.dashboard_repository import (
    MAX_DETAIL_DAYS,
    MAX_PAGE_SIZE,
    AlertCursor,
    AlertPage,
    Overview,
    PatientCursor,
    PatientDetail,
    PatientPage,
    list_alerts,
    list_patients,
    overview,
    patient_detail,
)
from app.db.models import AdvisoryState, DeliveryStatus
from app.db.session import get_session_factory
from app.services.supabase_auth import AuthUser
from app.services.thresholds import BandState

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

DEFAULT_PAGE_SIZE: Final = 25
DEFAULT_DETAIL_DAYS: Final = 30
# A cursor is base64 JSON of at most a timestamp and a UUID; longer is not ours.
_MAX_CURSOR_CHARS: Final = 256
PATIENT_NOT_FOUND: Final = "patient not found"

Auth = Annotated[AuthUser, Depends(current_auth_user)]
Sessions = Annotated[sessionmaker[Session], Depends(get_session_factory)]
Config = Annotated[Settings, Depends(get_settings)]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
Cursor = Annotated[str | None, Query(max_length=_MAX_CURSOR_CHARS)]


def _unprocessable(exc: ValueError) -> HTTPException:
    """Turn a repository ``ValueError`` into a 422.

    Args:
        exc: The error.

    Returns:
        The HTTP error to raise.
    """
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.get("/overview")
def get_overview(
    auth_user: Auth,
    session_factory: Sessions,
    settings: Config,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
) -> Overview:
    """KPI tiles for the caller's patients.

    Args:
        auth_user: Verified login.
        session_factory: Database sessions.
        settings: Settings (clinic timezone).
        since: Inclusive start of every count (default: see module docstring).
        until: Exclusive end (default: now).

    Returns:
        The counts, with the windows they cover.

    Raises:
        HTTPException: 401 without a login, 403 without a practitioner login, 422 for a
            naive timestamp or ``since`` not before ``until``.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        try:
            return overview(
                session,
                practitioner.id,
                since=since,
                until=until,
                timezone_name=settings.dashboard_timezone,
            )
        except ValueError as exc:
            raise _unprocessable(exc) from exc


@router.get("/patients")
def get_patients(  # noqa: PLR0917 - FastAPI injects each dependency as a parameter
    auth_user: Auth,
    session_factory: Sessions,
    settings: Config,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
    band_state: BandState | None = None,
    breach_7d: bool | None = None,
    live_handoff: bool | None = None,
) -> PatientPage:
    """One row per visible patient, by id.

    Args:
        auth_user: Verified login.
        session_factory: Database sessions.
        settings: Settings (pre-breach band).
        limit: Page size.
        cursor: ``next_cursor`` of the previous page.
        band_state: Band state of the last reading.
        breach_7d: Has (or has not) a breach in the last 7 days.
        live_handoff: Has (or has not) an open or claimed handoff.

    Returns:
        One page.

    Raises:
        HTTPException: 401, 403, or 422 for a bad filter or cursor.
    """
    try:
        position = PatientCursor.decode(cursor) if cursor else None
    except ValueError as exc:
        raise _unprocessable(exc) from exc
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        return list_patients(
            session,
            practitioner.id,
            band=settings.pre_breach_band,
            limit=limit,
            cursor=position,
            band_state=band_state,
            breach_7d=breach_7d,
            live_handoff=live_handoff,
        )


@router.get("/patients/{user_id}")
def get_patient(
    user_id: uuid.UUID,
    auth_user: Auth,
    session_factory: Sessions,
    settings: Config,
    days: Annotated[int, Query(ge=1, le=MAX_DETAIL_DAYS)] = DEFAULT_DETAIL_DAYS,
) -> PatientDetail:
    """Chart data for one patient.

    Args:
        user_id: Patient.
        auth_user: Verified login.
        session_factory: Database sessions.
        settings: Settings (pre-breach band).
        days: Days of readings.

    Returns:
        The detail.

    Raises:
        HTTPException: 401, 403, 404 when the patient is unknown or not visible, 422
            for ``days`` out of range.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        detail = patient_detail(
            session, practitioner.id, user_id, days=days, band=settings.pre_breach_band
        )
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, PATIENT_NOT_FOUND)
    return detail


@router.get("/alerts")
def get_alerts(  # noqa: PLR0917 - FastAPI injects each dependency as a parameter
    auth_user: Auth,
    session_factory: Sessions,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
    state: AdvisoryState | None = None,
    escalated: bool | None = None,
    delivery_status: DeliveryStatus | None = None,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
) -> AlertPage:
    """Advisories of the caller's patients, newest reading first.

    Args:
        auth_user: Verified login.
        session_factory: Database sessions.
        limit: Page size.
        cursor: ``next_cursor`` of the previous page.
        state: Band state.
        escalated: Escalated or not.
        delivery_status: Delivery state.
        since: Readings measured at or after this.
        until: Readings measured before this.

    Returns:
        One page.

    Raises:
        HTTPException: 401, 403, or 422 for a bad filter, cursor or range.
    """
    try:
        position = AlertCursor.decode(cursor) if cursor else None
    except ValueError as exc:
        raise _unprocessable(exc) from exc
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        try:
            return list_alerts(
                session,
                practitioner.id,
                limit=limit,
                cursor=position,
                state=state,
                escalated=escalated,
                delivery_status=delivery_status,
                since=since,
                until=until,
            )
        except ValueError as exc:
            raise _unprocessable(exc) from exc
