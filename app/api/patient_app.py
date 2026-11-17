"""The web apps at ``/app`` and the config they bootstrap from (R3, R4, R6).

``app/static/web`` is the React build of ``web/`` (committed, so the demo machine
needs no Node): the patient app (``patient.html``) at ``/app/`` and every client route
under it (``/app/advisory/{id}``, ``/app/readings``, ...), and the practitioner app
(``practitioner.html``) at ``/app/practitioner`` and ``/app/practitioner/*``. The
service worker ``/app/sw.js`` (scope ``/app/``), ``manifest.webmanifest`` and the icons
ship from Vite's ``public/`` folder unhashed.

The earlier plain HTML/JS pages in ``app/static/patient`` stay reachable at
``/app/classic/`` and ``/app/practitioner-classic`` until parity is confirmed; their
scripts (``/app/common.js`` ...) are found by the same ``/app/{path}`` lookup, which
tries the React build first and the classic folder second.

Serving from FastAPI keeps the pages same-origin with ``/api``, so the ngrok https URL
covers both.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.db.session import DatabaseHealth, get_database_health

STATIC_ROOT: Final = Path(__file__).resolve().parents[1] / "static"
WEB_DIR: Final = STATIC_ROOT / "web"
CLASSIC_DIR: Final = STATIC_ROOT / "patient"
# Kept for callers that read the classic folder (tests, scripts).
STATIC_DIR: Final = CLASSIC_DIR
APP_PATH: Final = "/app"
# HTML shells and the service worker must be revalidated, or a phone keeps running an
# old build; Vite's hashed /app/assets/* files never change, so they cache for a year.
_NO_CACHE: Final = {"Cache-Control": "no-cache"}
_IMMUTABLE: Final = {"Cache-Control": "public, max-age=31536000, immutable"}
_MEDIA_TYPES: Final = {".webmanifest": "application/manifest+json", ".js": "text/javascript"}

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
def app_config(
    settings: Annotated[Settings, Depends(get_settings)],
    db_health: Annotated[DatabaseHealth, Depends(get_database_health)],
) -> AppConfigOut:
    """Return the public configuration for the patient web app.

    It is the first call both pages make, so it also checks (cached, bounded by the
    connect timeout) that the database is reachable: the page then shows the reason
    instead of every later call hanging.

    Args:
        settings: Application settings.
        db_health: Cached database reachability check.

    Returns:
        Supabase URL, publishable key and VAPID public key.

    Raises:
        HTTPException: 503 if ``SUPABASE_ANON_KEY`` is not set, or if the database is
            unreachable (detail "database unreachable (port 5432 blocked?)").
    """
    db = db_health.status()
    if not db.reachable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, db.detail)
    try:
        anon_key = settings.require_supabase_anon_key()
    except ConfigurationError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return AppConfigOut(
        supabase_url=str(settings.supabase_url).rstrip("/"),
        supabase_anon_key=anon_key,
        vapid_public_key=settings.vapid_public_key,
    )


def _file(path: Path) -> FileResponse:
    """Serve one file with the right content type and cache policy.

    Args:
        path: An existing file under :data:`STATIC_ROOT`.

    Returns:
        The response.
    """
    hashed = path.parent.name == "assets" and path.parent.parent == WEB_DIR
    headers = _IMMUTABLE if hashed else _NO_CACHE
    return FileResponse(path, media_type=_MEDIA_TYPES.get(path.suffix), headers=headers)


def _shell(name: str) -> FileResponse:
    """Serve one of the React build's HTML shells.

    Args:
        name: ``patient.html`` or ``practitioner.html``.

    Returns:
        The page.

    Raises:
        HTTPException: 503 if the build is missing (run ``npm run build`` in ``web/``).
    """
    page = WEB_DIR / name
    if not page.is_file():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "web app not built (npm run build in web/)"
        )
    return FileResponse(page, media_type="text/html", headers=_NO_CACHE)


def _find(base: Path, relative: str) -> Path | None:
    """Return ``base / relative`` if it is a file inside ``base`` (no ``..`` escapes).

    Args:
        base: Folder to look in.
        relative: Request path below ``/app/``.

    Returns:
        The file, or ``None``.
    """
    candidate = (base / relative).resolve()
    if candidate.is_file() and candidate.is_relative_to(base.resolve()):
        return candidate
    return None


@router.get(APP_PATH, include_in_schema=False)
def app_root() -> RedirectResponse:
    """Redirect ``/app`` to ``/app/`` (the service worker scope).

    Returns:
        A redirect.
    """
    return RedirectResponse(f"{APP_PATH}/")


@router.get(f"{APP_PATH}/advisory/{{advisory_id}}", include_in_schema=False)
def advisory_page(advisory_id: uuid.UUID) -> FileResponse:
    """Serve the patient app for an advisory link; the page loads the advisory by id.

    The id is validated as a UUID here so arbitrary paths 422 instead of serving the
    page. Ownership is checked by ``GET /api/advisories/{id}``, not here: the page
    itself holds no patient data.

    Args:
        advisory_id: Advisory id from the notification link.

    Returns:
        ``patient.html``.
    """
    del advisory_id  # read by the page from location.pathname
    return _shell("patient.html")


@router.get(f"{APP_PATH}/practitioner", include_in_schema=False)
@router.get(f"{APP_PATH}/practitioner/{{rest:path}}", include_in_schema=False)
def practitioner_page(rest: str = "") -> FileResponse:
    """Serve the practitioner app for any of its client routes.

    Access is checked by the ``/api/practitioner/*`` and ``/api/dashboard/*`` routes;
    the page holds no data.

    Args:
        rest: Client route below ``/app/practitioner/`` (read by the page).

    Returns:
        ``practitioner.html``.
    """
    del rest
    return _shell("practitioner.html")


@router.get(f"{APP_PATH}/classic", include_in_schema=False)
@router.get(f"{APP_PATH}/classic/", include_in_schema=False)
def classic_patient_page() -> FileResponse:
    """Serve the earlier plain HTML patient page (kept until parity is confirmed).

    Returns:
        The classic ``index.html``.
    """
    return _file(CLASSIC_DIR / "index.html")


@router.get(f"{APP_PATH}/classic/advisory/{{advisory_id}}", include_in_schema=False)
def classic_advisory_page(advisory_id: uuid.UUID) -> FileResponse:
    """Serve the earlier plain HTML advisory page.

    Args:
        advisory_id: Advisory id (read by the page's script).

    Returns:
        The classic ``advisory.html``.
    """
    del advisory_id
    return _file(CLASSIC_DIR / "advisory.html")


@router.get(f"{APP_PATH}/practitioner-classic", include_in_schema=False)
def classic_practitioner_page() -> FileResponse:
    """Serve the earlier plain HTML practitioner page.

    Returns:
        The classic ``practitioner.html``.
    """
    return _file(CLASSIC_DIR / "practitioner.html")


@router.get(f"{APP_PATH}/{{path:path}}", include_in_schema=False)
def app_file_or_route(path: str) -> FileResponse:
    """Serve a file of the React build or the classic pages, else the patient app.

    A path with a file extension that matches nothing is a 404 (a missing script must
    not come back as HTML); any other path is a patient-app client route.

    Args:
        path: Request path below ``/app/``.

    Returns:
        The file, or ``patient.html`` for a client route.

    Raises:
        HTTPException: 404 for a missing file.
    """
    for base in (WEB_DIR, CLASSIC_DIR):
        found = _find(base, path) if path else None
        if found is not None:
            return _file(found)
    if Path(path).suffix:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return _shell("patient.html")
