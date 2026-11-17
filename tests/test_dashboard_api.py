"""Tests for the dashboard API (R6) with fakes; no database or network.

The repository functions are swapped for fakes that record their arguments and apply
the same access rule (patients of the asking practitioner only). The live queries are
covered by ``tests/test_dashboard_repository.py``.
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
from app.api import chat, dashboard
from app.api.auth import current_auth_user
from app.core.config import Settings, get_settings
from app.core.rates import CountRate
from app.db import dashboard_repository as dash
from app.db.models import Practitioner
from app.db.session import get_session_factory
from app.services.supabase_auth import AuthUser

SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="srk",
    database_url="postgresql://u:p@localhost:5432/db",
    pre_breach_band=0.07,
    dashboard_timezone="Asia/Kolkata",
)
DOCTOR = AuthUser(id=uuid.uuid4(), email="doctor@example.com")
PATIENT = AuthUser(id=uuid.uuid4(), email="patient@example.com")
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
ZERO = CountRate.of(0, 0)


class FakeSession:
    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class Clinic:
    def __init__(self) -> None:
        self.doctor = Practitioner(id=uuid.uuid4(), user_id=uuid.uuid4(), full_name="Dr Demo")
        self.mine = uuid.uuid4()
        self.calls: dict[str, dict[str, object]] = {}

    def find_practitioner(self, _s: object, auth_id: uuid.UUID) -> Practitioner | None:
        return self.doctor if auth_id == DOCTOR.id else None

    def overview(self, _s: object, practitioner_id: uuid.UUID, **kw: object) -> dash.Overview:
        self.calls["overview"] = {"practitioner_id": practitioner_id, **kw}
        if kw["since"] is not None and kw["until"] is not None and kw["since"] >= kw["until"]:  # type: ignore[operator]
            raise ValueError("since must be before until")
        window = dash.TimeWindow(since=NOW - timedelta(hours=24), until=NOW)
        return dash.Overview(
            readings_window=window,
            alerts_window=window,
            active_patients=0,
            readings=0,
            pre_breach_alerts=0,
            breaches=0,
            below_54_readings=0,
            open_handoffs=0,
            claimed_handoffs=0,
            delivery=dash.DeliveryCounts(
                total=0,
                queued=ZERO,
                sent=ZERO,
                opened=ZERO,
                failed=ZERO,
                expired=ZERO,
                suppressed=ZERO,
                delivered=ZERO,
            ),
            last_batch_run=None,
        )

    def list_patients(
        self, _s: object, practitioner_id: uuid.UUID, **kw: object
    ) -> dash.PatientPage:
        self.calls["patients"] = {"practitioner_id": practitioner_id, **kw}
        return dash.PatientPage(items=[], next_cursor=None)

    def patient_detail(
        self, _s: object, practitioner_id: uuid.UUID, user_id: uuid.UUID, **kw: object
    ) -> dash.PatientDetail | None:
        self.calls["detail"] = {"practitioner_id": practitioner_id, "user_id": user_id, **kw}
        if user_id != self.mine:
            return None
        return dash.PatientDetail(
            user_id=user_id,
            first_name="Asha",
            age_band="50-59",
            diabetes_status="diabetes",
            window=dash.TimeWindow(since=NOW - timedelta(days=30), until=NOW),
            pre_breach_band=0.07,
            readings=[],
            thresholds=[],
            risk=None,
            advisories=[],
        )

    def list_alerts(self, _s: object, practitioner_id: uuid.UUID, **kw: object) -> dash.AlertPage:
        self.calls["alerts"] = {"practitioner_id": practitioner_id, **kw}
        return dash.AlertPage(items=[], next_cursor=None)


@pytest.fixture
def clinic(monkeypatch: pytest.MonkeyPatch) -> Clinic:
    c = Clinic()
    monkeypatch.setattr(chat, "find_practitioner_by_auth_user", c.find_practitioner)
    for name in ("overview", "list_patients", "patient_detail", "list_alerts"):
        monkeypatch.setattr(dashboard, name, getattr(c, name))
    return c


@pytest.fixture
def client(clinic: Clinic) -> Iterator[tuple[TestClient, dict[str, AuthUser]]]:
    app = main.app
    login = {"who": DOCTOR}
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_session_factory] = lambda: FakeSession
    app.dependency_overrides[current_auth_user] = lambda: login["who"]
    yield TestClient(app), login
    app.dependency_overrides.clear()


def _urls(clinic: Clinic) -> list[str]:
    return [
        "/api/dashboard/overview",
        "/api/dashboard/patients",
        f"/api/dashboard/patients/{clinic.mine}",
        "/api/dashboard/alerts",
    ]


def test_without_a_login_every_route_is_401(clinic: Clinic) -> None:
    app = main.app
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    try:
        client = TestClient(app)
        for url in _urls(clinic):
            assert client.get(url).status_code == 401, url
    finally:
        app.dependency_overrides.clear()
    assert clinic.calls == {}


def test_non_practitioners_get_403(
    client: tuple[TestClient, dict[str, AuthUser]], clinic: Clinic
) -> None:
    http, login = client
    login["who"] = PATIENT
    for url in _urls(clinic):
        assert http.get(url).status_code == 403, url
    assert clinic.calls == {}


def test_unknown_or_foreign_patient_is_404(
    client: tuple[TestClient, dict[str, AuthUser]], clinic: Clinic
) -> None:
    http, _ = client
    response = http.get(f"/api/dashboard/patients/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"] == dashboard.PATIENT_NOT_FOUND
    assert http.get(f"/api/dashboard/patients/{clinic.mine}").status_code == 200


def test_settings_and_filters_reach_the_repository(
    client: tuple[TestClient, dict[str, AuthUser]], clinic: Clinic
) -> None:
    http, _ = client
    assert http.get("/api/dashboard/overview").status_code == 200
    assert clinic.calls["overview"]["timezone_name"] == "Asia/Kolkata"
    assert clinic.calls["overview"]["since"] is None

    cursor = dash.PatientCursor(id=uuid.uuid4())
    response = http.get(
        "/api/dashboard/patients",
        params={
            "limit": 7,
            "cursor": cursor.encode(),
            "band_state": "pre_breach_low",
            "breach_7d": "true",
            "live_handoff": "false",
        },
    )
    assert response.status_code == 200
    call = clinic.calls["patients"]
    assert call["practitioner_id"] == clinic.doctor.id
    assert (call["limit"], call["cursor"], call["band"]) == (7, cursor, 0.07)
    assert (call["band_state"], call["breach_7d"], call["live_handoff"]) == (
        "pre_breach_low",
        True,
        False,
    )

    assert http.get(f"/api/dashboard/patients/{clinic.mine}?days=14").status_code == 200
    assert (clinic.calls["detail"]["days"], clinic.calls["detail"]["band"]) == (14, 0.07)

    alert_cursor = dash.AlertCursor(measured_at=NOW, id=uuid.uuid4())
    response = http.get(
        "/api/dashboard/alerts",
        params={
            "cursor": alert_cursor.encode(),
            "state": "breach_high",
            "escalated": "true",
            "delivery_status": "failed",
            "since": "2026-11-16T00:00:00+05:30",
        },
    )
    assert response.status_code == 200
    call = clinic.calls["alerts"]
    assert (call["cursor"], call["state"], call["escalated"], call["delivery_status"]) == (
        alert_cursor,
        "breach_high",
        True,
        "failed",
    )


@pytest.mark.parametrize(
    "url",
    [
        "/api/dashboard/patients?limit=0",
        f"/api/dashboard/patients?limit={dash.MAX_PAGE_SIZE + 1}",
        "/api/dashboard/patients?cursor=not-a-cursor",
        "/api/dashboard/patients?band_state=sideways",
        "/api/dashboard/alerts?state=normal",
        "/api/dashboard/alerts?delivery_status=delivered",
        "/api/dashboard/alerts?cursor=bm90IGpzb24",
        "/api/dashboard/alerts?since=2026-11-16T00:00:00",
        "/api/dashboard/overview?since=2026-11-17T00:00:00Z&until=2026-11-16T00:00:00Z",
    ],
)
def test_bad_parameters_are_422(client: tuple[TestClient, dict[str, AuthUser]], url: str) -> None:
    http, _ = client
    assert http.get(url).status_code == 422, url


def test_detail_days_are_bounded(
    client: tuple[TestClient, dict[str, AuthUser]], clinic: Clinic
) -> None:
    http, _ = client
    for days in (0, dash.MAX_DETAIL_DAYS + 1):
        assert http.get(f"/api/dashboard/patients/{clinic.mine}?days={days}").status_code == 422


def test_a_patient_cursor_is_not_an_alert_cursor() -> None:
    token = dash.PatientCursor(id=uuid.uuid4()).encode()
    with pytest.raises(ValueError, match="invalid cursor"):
        dash.AlertCursor.decode(token)


@pytest.mark.parametrize(
    ("age", "band"),
    [(None, None), (18, "<20"), (19, "<20"), (20, "20-29"), (64, "60-69"), (65, "60-69"),
     (79, "70-79"), (80, "80+"), (101, "80+")],
)  # fmt: skip
def test_age_band(age: int | None, band: str | None) -> None:
    assert dash.age_band(age) == band


def test_start_of_day_is_local_midnight() -> None:
    # 01:00 UTC on 17 Nov is 06:30 IST, so "today" started at 18:30 UTC on 16 Nov.
    moment = datetime(2026, 11, 17, 1, 0, tzinfo=UTC)
    start = dash.start_of_day(moment, "Asia/Kolkata")
    assert start.astimezone(UTC) == datetime(2026, 11, 16, 18, 30, tzinfo=UTC)


def test_dashboard_timezone_must_be_a_real_zone() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        Settings(
            _env_file=None,
            supabase_url="https://abc.supabase.co",
            supabase_service_role_key="srk",
            database_url="postgresql://u:p@localhost:5432/db",
            dashboard_timezone="Mars/Olympus",
        )


def test_routes_are_read_only() -> None:
    methods = {
        method for route in dashboard.router.routes for method in getattr(route, "methods", set())
    }
    assert methods == {"GET"}
