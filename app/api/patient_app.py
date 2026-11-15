"""The minimal patient web app at ``/app`` and the config it bootstraps from (R3).

``app/static/patient`` holds a plain HTML/JS page (Supabase email login, "Enable
alerts"), the service worker ``sw.js`` and ``manifest.webmanifest``. It is a test
harness for Web Push, not the R6 dashboard (``web/``). Serving it from FastAPI keeps
it same-origin with ``/api``, so the ngrok https URL covers both.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
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


def mount_patient_app(app: FastAPI) -> None:
    """Serve ``app/static/patient`` at ``/app`` (``/app`` redirects to ``/app/``).

    The service worker lives at ``/app/sw.js``, so its scope is ``/app/``: it only
    controls the patient app, never ``/api`` or ``/docs``.

    Args:
        app: The application to mount on.
    """
    app.mount(APP_PATH, StaticFiles(directory=STATIC_DIR, html=True), name="patient-app")
