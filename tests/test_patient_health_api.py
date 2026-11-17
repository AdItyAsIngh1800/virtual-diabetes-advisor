"""Tests for ``GET /api/me/health`` (the patient's own limits, readings and alerts).

Fakes only: the patient lookup and the repository call are swapped out. The live
query is covered by ``test_own_health_is_the_patients_own_view_without_risk`` in
``tests/test_dashboard_repository.py``.
"""

from __future__ import annotations

import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from app import main
from app.api import chat, patient_health
from app.api.auth import current_auth_user
from app.core.config import Settings, get_settings
from app.db import dashboard_repository as dash
from app.db.models import User
from app.db.session import get_session_factory
from app.services.supabase_auth import AuthUser

SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="srk",
    database_url="postgresql://u:p@localhost:5432/db",
    pre_breach_band=0.07,
)
PATIENT = AuthUser(id=uuid.uuid4(), email="patient@example.com")
DOCTOR = AuthUser(id=uuid.uuid4(), email="doctor@example.com")
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


class FakeSession:
    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict[str, object]]]:
    me = User(id=uuid.uuid4())
    calls: dict[str, object] = {}

    def find_patient(_s: object, auth_id: uuid.UUID) -> User | None:
        return me if auth_id == PATIENT.id else None

    def own_health(_s: object, user_id: uuid.UUID, *, days: int, band: float) -> dash.OwnHealth:
        calls.update(user_id=user_id, days=days, band=band)
        if not 1 <= days <= dash.MAX_DETAIL_DAYS:
            raise ValueError("days out of range")
        return dash.OwnHealth(
            window=dash.TimeWindow(since=NOW - timedelta(days=days), until=NOW),
            pre_breach_band=band,
            limits=dash.CurrentLimits(fasting_high=126.0, post_meal_high=180.0, low=70.0),
            readings=[],
            advisories=[],
        )

    monkeypatch.setattr(chat, "find_patient_by_auth_user", find_patient)
    monkeypatch.setattr(patient_health, "own_health", own_health)
    app = main.create_app(expose_docs=False)
    login: dict[str, AuthUser] = {"who": PATIENT}
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_session_factory] = lambda: FakeSession
    app.dependency_overrides[current_auth_user] = lambda: login["who"]
    calls["me"] = me
    calls["login"] = login
    yield TestClient(app), calls
    app.dependency_overrides.clear()


def test_returns_only_the_signed_in_patients_data(
    client: tuple[TestClient, dict[str, object]],
) -> None:
    http, calls = client
    response = http.get("/api/me/health")
    assert response.status_code == 200
    body = response.json()
    assert body["limits"] == {"fasting_high": 126.0, "post_meal_high": 180.0, "low": 70.0}
    me = calls["me"]
    assert isinstance(me, User)
    assert calls["user_id"] == me.id  # from the login, never a parameter
    assert (calls["days"], calls["band"]) == (patient_health.DEFAULT_DAYS, 0.07)


def test_has_no_risk_score_or_threshold_components(
    client: tuple[TestClient, dict[str, object]],
) -> None:
    http, _ = client
    body = http.get("/api/me/health").json()
    assert set(body) == {"window", "pre_breach_band", "limits", "readings", "advisories"}
    assert "risk" not in http.get("/api/me/health").text


def test_ignores_a_user_id_parameter(client: tuple[TestClient, dict[str, object]]) -> None:
    http, calls = client
    other = uuid.uuid4()
    assert http.get(f"/api/me/health?user_id={other}").status_code == 200
    assert calls["user_id"] != other


def test_practitioner_login_is_refused(client: tuple[TestClient, dict[str, object]]) -> None:
    http, calls = client
    login = calls["login"]
    assert isinstance(login, dict)
    login["who"] = DOCTOR
    assert http.get("/api/me/health").status_code == 403


@pytest.mark.parametrize("days", [0, dash.MAX_DETAIL_DAYS + 1])
def test_days_out_of_range(client: tuple[TestClient, dict[str, object]], days: int) -> None:
    http, _ = client
    assert http.get(f"/api/me/health?days={days}").status_code == 422


def test_requires_a_login() -> None:
    app = main.create_app(expose_docs=False)
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    assert TestClient(app).get("/api/me/health").status_code == 401
