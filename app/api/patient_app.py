"""The minimal patient web app at ``/app`` and the config it bootstraps from (R3).

``app/static/patient`` holds a plain HTML/JS page (Supabase email login, "Enable
alerts", the help chat with hold-to-record voice), the advisory page ``advisory.html``
(served at ``/app/advisory/{id}``), the practitioner page ``practitioner.html`` (served
at ``/app/practitioner``), the service worker ``sw.js`` and ``manifest.webmanifest``.
It is a small test harness for Web Push and chat, not the R6 dashboard (``web/``).
Serving it from FastAPI keeps it same-origin with ``/api``, so the ngrok https URL
covers both.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError

STATIC_DIR: Final = Path(__file__).resolve().parents[1] / "static" / "patient"
APP_PATH: Final = "/app"

router = APIRouter(tags=["patient-app"])


class AppConfigOut(BaseModel):
    """Public values the browser needs (nothing secret).

    Attributes:
        supabase_url: Supabase project URL.
        supabase_anon_key: Publishable key for Supabase Auth in the browser.
        vapid_public_key: ``applicationServerKey``, or ``None`` before keys exist.
    """

    supabase_url: str
    supabase_anon_key: str
    vapid_public_key: str | None


@router.get("/api/app-config")
def app_config(settings: Annotated[Settings, Depends(get_settings)]) -> AppConfigOut:
    """Return the public configuration for the patient web app.

    Args:
        settings: Application settings.

    Returns:
        Supabase URL, publishable key and VAPID public key.

    Raises:
        HTTPException: 503 if ``SUPABASE_ANON_KEY`` is not set.
    """
    try:
        anon_key = settings.require_supabase_anon_key()
    except ConfigurationError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return AppConfigOut(
        supabase_url=str(settings.supabase_url).rstrip("/"),
        supabase_anon_key=anon_key,
        vapid_public_key=settings.vapid_public_key,
    )


@router.get(f"{APP_PATH}/advisory/{{advisory_id}}", include_in_schema=False)
def advisory_page(advisory_id: uuid.UUID) -> FileResponse:
    """Serve the advisory page; its script loads the advisory by the id in the URL.

    The id is validated as a UUID here so arbitrary paths 404/422 instead of serving
    the page. Ownership is checked by ``GET /api/advisories/{id}``, not here: the
    page itself holds no patient data.

    Args:
        advisory_id: Advisory id from the notification link.

    Returns:
        ``advisory.html``.
    """
    del advisory_id  # read by the page's script from location.pathname
    return FileResponse(STATIC_DIR / "advisory.html", media_type="text/html")


@router.get(f"{APP_PATH}/practitioner", include_in_schema=False)
def practitioner_page() -> FileResponse:
    """Serve the practitioner page (R4): open handoffs, conversation, reply, resolve.

    Access is checked by the ``/api/practitioner/*`` routes; the page holds no data.

    Returns:
        ``practitioner.html``.
    """
    return FileResponse(STATIC_DIR / "practitioner.html", media_type="text/html")


def mount_patient_app(app: FastAPI) -> None:
    """Serve ``app/static/patient`` at ``/app`` (``/app`` redirects to ``/app/``).

    The service worker lives at ``/app/sw.js``, so its scope is ``/app/``: it only
    controls the patient app, never ``/api`` or ``/docs``.

    Args:
        app: The application to mount on.
    """
    app.mount(APP_PATH, StaticFiles(directory=STATIC_DIR, html=True), name="patient-app")
