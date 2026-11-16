"""Tests for the FastAPI app: admin key protection, /admin/batch-run and the scheduler."""

from __future__ import annotations

import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

with warnings.catch_warnings():
    # Starlette's TestClient warns that httpx is deprecated in favour of httpx2.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from apscheduler.triggers.interval import IntervalTrigger

from app import main
from app.api import admin
from app.core.config import Settings, get_settings
from app.core.exceptions import BatchAlreadyRunningError, ConfigurationError
from app.jobs.batch_update import BatchRunSummary
from app.jobs.dispatch_advisories import DispatchSummary

KEY = "test-admin-key-0123456789"
REQUIRED = {
    "supabase_url": "https://abc.supabase.co",
    "supabase_service_role_key": "srk",
    "database_url": "postgresql://u:p@localhost:5432/db",
}
SUMMARY = BatchRunSummary(
    run_id=uuid.uuid4(),
    status="succeeded",
    triggered_by="admin_api",
    window_start=None,
    window_end=datetime(2026, 11, 15, tzinfo=UTC),
    model_version="test",
    users_processed=2,
    users_skipped=0,
    users_failed=0,
    failed_user_ids=[],
    risk_rescored=2,
    readings_evaluated=10,
    thresholds_written=6,
    advisories_created=3,
    advisories_by_state={"pre_breach_high": 2, "breach_low": 1},
)


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED, **overrides})


@pytest.fixture(autouse=True)
def _no_real_database_check_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifespan tests start the app; keep its startup database check offline."""
    from app.db.session import DatabaseStatus  # noqa: PLC0415

    monkeypatch.setattr(
        main, "database_status_at_startup", lambda _s: DatabaseStatus(reachable=True)
    )


@pytest.fixture
def client_with(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Return a factory: client_with(settings) -> TestClient (lifespan not started)."""
    calls: list[dict[str, Any]] = []

    def fake_run(*_args: Any, **kwargs: Any) -> BatchRunSummary:
        calls.append(kwargs)
        return SUMMARY

    monkeypatch.setattr(admin, "run_batch_update", fake_run)
    monkeypatch.setattr(admin, "scheduled_dispatch", lambda: calls.append({"dispatch": True}))
    monkeypatch.setattr(admin, "get_session_factory", lambda: None)
    monkeypatch.setattr(admin, "load_risk_scorer", lambda: None)
    app = main.create_app()

    def build(settings: Settings) -> TestClient:
        app.dependency_overrides[get_settings] = lambda: settings
        client = TestClient(app)
        client.calls = calls  # type: ignore[attr-defined]  # test-only attribute
        return client

    yield build
    app.dependency_overrides.clear()


def test_admin_disabled_without_configured_key(client_with: Any) -> None:
    client = client_with(make_settings())
    response = client.post("/admin/batch-run", headers={"X-Admin-Api-Key": KEY})
    assert response.status_code == 503
    assert client.calls == []


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Api-Key": "wrong"}, {"X-Admin-Api-Key": ""}])
def test_admin_rejects_missing_or_wrong_key(client_with: Any, headers: dict[str, str]) -> None:
    client = client_with(make_settings(admin_api_key=KEY))
    response = client.post("/admin/batch-run", headers=headers)
    assert response.status_code == 401
    assert KEY not in response.text
    assert client.calls == []


def test_batch_run_with_valid_key(client_with: Any) -> None:
    client = client_with(make_settings(admin_api_key=KEY))
    response = client.post("/admin/batch-run", headers={"X-Admin-Api-Key": KEY})
    assert response.status_code == 200
    body = response.json()
    assert body["advisories_created"] == 3
    assert body["advisories_by_state"] == {"pre_breach_high": 2, "breach_low": 1}
    # The batch run is followed by a dispatch of the new advisories (R3).
    assert client.calls == [{"scorer": None, "triggered_by": "admin_api"}, {"dispatch": True}]


def test_dispatch_run_with_valid_key(client_with: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_with(make_settings(admin_api_key=KEY))
    monkeypatch.setattr(admin, "run_dispatch", lambda: DispatchSummary(sent=1, expired=2))
    response = client.post("/admin/dispatch-run", headers={"X-Admin-Api-Key": KEY})
    assert response.status_code == 200
    assert (response.json()["sent"], response.json()["expired"]) == (1, 2)
    assert client.post("/admin/dispatch-run").status_code == 401


def test_dispatch_run_errors(client_with: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_with(make_settings(admin_api_key=KEY))
    headers = {"X-Admin-Api-Key": KEY}

    def unconfigured() -> DispatchSummary:
        raise ConfigurationError("VAPID_PUBLIC_KEY required")

    monkeypatch.setattr(admin, "run_dispatch", unconfigured)
    assert client.post("/admin/dispatch-run", headers=headers).status_code == 503
    monkeypatch.setattr(admin, "run_dispatch", lambda: None)
    assert client.post("/admin/dispatch-run", headers=headers).status_code == 409


def test_batch_run_conflict_when_already_running(
    client_with: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(*_args: Any, **_kwargs: Any) -> BatchRunSummary:
        raise BatchAlreadyRunningError("a batch_update run is already in progress")

    client = client_with(make_settings(admin_api_key=KEY))
    monkeypatch.setattr(admin, "run_batch_update", busy)
    response = client.post("/admin/batch-run", headers={"X-Admin-Api-Key": KEY})
    assert response.status_code == 409


def test_get_is_not_allowed(client_with: Any) -> None:
    client = client_with(make_settings(admin_api_key=KEY))
    assert client.get("/admin/batch-run", headers={"X-Admin-Api-Key": KEY}).status_code == 405


@pytest.mark.parametrize("minutes", [15, 5])
def test_scheduler_interval_comes_from_settings(minutes: int) -> None:
    scheduler = main.build_scheduler(make_settings(batch_interval_minutes=minutes))
    job = scheduler.get_job(main.BATCH_JOB_ID)
    assert job is not None
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval.total_seconds() == minutes * 60
    assert job.max_instances == 1
    assert job.coalesce is True


def test_default_interval_is_15_minutes() -> None:
    assert make_settings().batch_interval_minutes == 15


def test_dispatch_job_runs_on_its_own_interval() -> None:
    scheduler = main.build_scheduler(make_settings(dispatch_interval_minutes=3))
    job = scheduler.get_job(main.DISPATCH_JOB_ID)
    assert job is not None
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval.total_seconds() == 180
    assert (job.max_instances, job.coalesce) == (1, True)


def test_scheduled_batch_run_is_followed_by_a_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(main, "get_session_factory", lambda: None)
    monkeypatch.setattr(main, "load_risk_scorer", lambda: None)
    monkeypatch.setattr(main, "run_batch_update", lambda *_a, **_k: order.append("batch"))
    monkeypatch.setattr(main, "scheduled_dispatch", lambda: order.append("dispatch"))
    main.scheduled_batch_run()
    assert order == ["batch", "dispatch"]


def test_scheduled_run_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "scheduled_dispatch", lambda: None)
    monkeypatch.setattr(main, "get_session_factory", lambda: None)
    monkeypatch.setattr(main, "load_risk_scorer", lambda: None)

    def busy(*_a: Any, **_k: Any) -> None:
        raise BatchAlreadyRunningError("busy")

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("database down")

    for fake in (busy, broken):
        monkeypatch.setattr(main, "run_batch_update", fake)
        main.scheduled_batch_run()  # must not raise


def test_lifespan_starts_and_stops_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []

    class FakeScheduler:
        def start(self) -> None:
            started.append(True)

        def shutdown(self, wait: bool) -> None:
            started.append(wait)

    monkeypatch.setattr(main, "get_settings", make_settings)
    monkeypatch.setattr(main, "build_scheduler", lambda _s: FakeScheduler())
    with TestClient(main.create_app()):
        assert started == [True]
    assert started == [True, False]


def test_lifespan_respects_scheduler_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "get_settings", lambda: make_settings(batch_scheduler_enabled=False))

    def fail(_s: Settings) -> None:
        raise AssertionError("scheduler must not be built")

    monkeypatch.setattr(main, "build_scheduler", fail)
    with TestClient(main.create_app()):
        pass
