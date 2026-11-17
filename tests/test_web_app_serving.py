"""Serving the React build (``app/static/web``) at ``/app/`` and ``/app/practitioner``.

Covers both apps' client routes and the SPA fallback, the service worker and manifest
(content types, scope, no caching), hashed assets, missing files and path escapes, and
the classic pages kept reachable until parity is confirmed. The lifespan (scheduler)
is not started: ``TestClient`` is used without a ``with`` block.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import main
from app.api.patient_app import WEB_DIR

pytestmark = pytest.mark.skipif(
    not (WEB_DIR / "patient.html").is_file(), reason="web build missing (npm run build)"
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.create_app(expose_docs=False))


def _is_patient_shell(text: str) -> bool:
    return '<div id="root">' in text and "Diabetes Advisor" in text


def _is_practitioner_shell(text: str) -> bool:
    return '<div id="root">' in text and "<title>Practitioner dashboard</title>" in text


@pytest.mark.parametrize(
    "path",
    [
        "/app/",
        "/app/readings",
        "/app/alerts",
        "/app/account",
        f"/app/advisory/{uuid.uuid4()}",
        "/app/some/unknown/route",
    ],
)
def test_patient_routes_serve_the_patient_shell(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert _is_patient_shell(response.text)


@pytest.mark.parametrize(
    "path",
    [
        "/app/practitioner",
        "/app/practitioner/",
        "/app/practitioner/patients",
        f"/app/practitioner/patients/{uuid.uuid4()}",
        "/app/practitioner/alerts?state=breach_high",
        "/app/practitioner/handoffs",
        "/app/practitioner/review",
        "/app/practitioner/kb",
        "/app/practitioner/audit",
    ],
)
def test_practitioner_routes_serve_the_practitioner_shell(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert _is_practitioner_shell(response.text)


def test_app_without_slash_redirects_into_the_worker_scope(client: TestClient) -> None:
    response = client.get("/app", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/app/"


def test_service_worker_is_served_unhashed_and_revalidated(client: TestClient) -> None:
    response = client.get("/app/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    # The exact classic behaviour: push -> notification tagged by advisory id, click ->
    # the advisory page, and no fetch handler (nothing is cached).
    for marker in ("showNotification", "data.advisory_id", "notificationclick"):
        assert marker in response.text
    assert "/app/advisory/${data.advisory_id}" in response.text
    assert "addEventListener('fetch'" not in response.text
    assert 'addEventListener("fetch"' not in response.text


def test_manifest_keeps_the_app_scope(client: TestClient) -> None:
    response = client.get("/app/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(response.text)
    assert manifest["start_url"] == "/app/"
    assert manifest["scope"] == "/app/"


def test_hashed_assets_cache_for_a_year(client: TestClient) -> None:
    shell = client.get("/app/").text
    script = shell.split('src="/app/assets/', 1)[1].split('"', 1)[0]
    response = client.get(f"/app/assets/{script}")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "immutable" in response.headers["cache-control"]


def test_both_shells_load_the_built_bundles(client: TestClient) -> None:
    for path in ("/app/", "/app/practitioner"):
        text = client.get(path).text
        assert "/src/" not in text  # built, not the dev entry
        assert 'type="module"' in text
        assert "/app/assets/" in text


@pytest.mark.parametrize(
    "path", ["/app/missing.js", "/app/assets/nope.css", "/app/practitioner.js.map"]
)
def test_missing_files_are_404_not_html(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_paths_cannot_escape_the_static_folder(client: TestClient) -> None:
    for path in ("/app/..%2f..%2fcore%2fconfig.py", "/app/%2e%2e/%2e%2e/main.py"):
        response = client.get(path)
        assert response.status_code == 404 or _is_patient_shell(response.text)
        assert "Settings" not in response.text


def test_classic_pages_stay_reachable(client: TestClient) -> None:
    patient = client.get("/app/classic/")
    assert patient.status_code == 200
    assert "Enable alerts" in patient.text
    practitioner = client.get("/app/practitioner-classic")
    assert practitioner.status_code == 200
    assert "/app/practitioner.js" in practitioner.text
    # Their scripts still resolve through /app/{path}.
    for script in ("/app/common.js", "/app/app.js", "/app/practitioner.js", "/app/advisory.js"):
        assert client.get(script).status_code == 200


def test_built_bundles_hold_no_server_secrets() -> None:
    for asset in sorted((WEB_DIR / "assets").glob("*.js")):
        text = asset.read_text(encoding="utf-8", errors="ignore")
        for marker in ("service_role", "SUPABASE_SERVICE", "VAPID_PRIVATE", "ADMIN_API_KEY"):
            assert marker not in text, f"{marker} in {asset.name}"
