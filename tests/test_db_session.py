"""Tests for the database connect timeout and the "database unreachable" handling.

Includes a real connection attempt to an unreachable host: before the timeout existed,
this is exactly where the API, the scheduler and the test suite hung forever.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
import warnings
from typing import Any

import psycopg
import pytest
import sqlalchemy
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from app import main
from app.api.auth import current_auth_user
from app.core.config import Settings, get_settings, load_settings
from app.db import session as db_session
from app.db.session import (
    CONNECT_ARGS,
    DB_CONNECT_TIMEOUT_S,
    DB_STATEMENT_TIMEOUT_MS,
    DatabaseHealth,
    DatabaseStatus,
    database_port,
    describe_connection_error,
    get_database_health,
    get_session_factory,
    make_engine,
    open_connection,
    probe_database,
    set_statement_timeout,
    unreachable_message,
)
from app.services.supabase_auth import AuthUser

# 10.255.255.1 is a private address nothing answers on: packets are dropped, so a
# connection without a timeout waits forever (like a network that blocks port 5432).
BLACKHOLE_DSN = "postgresql://user:pw@10.255.255.1:5432/db"
# Nothing listens on port 1: the connection is refused at once.
REFUSED_DSN = "postgresql://user:pw@127.0.0.1:1/db"
UNREACHABLE = DatabaseStatus(reachable=False, detail=unreachable_message(5432))


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "supabase_url": "https://abc.supabase.co",
        "supabase_service_role_key": "srk",
        "database_url": "postgresql://u:p@localhost:5432/db",
        "supabase_anon_key": "anon",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def default_settings() -> Settings:
    """Zero-argument form for FastAPI dependency overrides.

    Overriding with ``settings`` itself would make FastAPI read its ``**overrides``
    as request parameters (422).
    """
    return settings()


# --- connect timeout everywhere --------------------------------------------------------


EXPECTED_CONNECT_ARGS = {
    "prepare_threshold": None,
    "connect_timeout": 10,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "options": "-c statement_timeout=30000",
}


def test_every_connection_gets_a_10_second_timeout() -> None:
    assert DB_CONNECT_TIMEOUT_S == 10
    assert CONNECT_ARGS["connect_timeout"] == 10
    assert CONNECT_ARGS["prepare_threshold"] is None


def test_every_connection_gets_keepalives_and_a_30_second_statement_timeout() -> None:
    # A silently dropped connection is detected in ~30 + 3 * 10 s instead of hanging.
    assert dict(CONNECT_ARGS) == EXPECTED_CONNECT_ARGS
    assert DB_STATEMENT_TIMEOUT_MS == 30_000


class FakeCursor:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def execute(self, sql: str) -> None:
        self.log.append(sql)

    def close(self) -> None:
        self.log.append("close")


class FakeDbapiConnection:
    def __init__(self) -> None:
        self.log: list[str] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.log)

    def commit(self) -> None:
        self.log.append("commit")


def test_statement_timeout_is_set_and_committed_on_each_new_connection() -> None:
    # The pooler drops the startup option, so the SET is what actually applies.
    conn = FakeDbapiConnection()
    set_statement_timeout(conn)  # type: ignore[arg-type]
    assert conn.log == ["SET statement_timeout = 30000", "close", "commit"]


def test_make_engine_registers_the_statement_timeout_hook() -> None:
    engine = make_engine("postgresql://u:p@localhost:5432/db")
    assert engine.url.drivername == "postgresql+psycopg"
    assert event.contains(engine, "connect", db_session._on_connect)


def test_engine_is_built_with_the_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    real_create_engine = sqlalchemy.create_engine

    def fake_create_engine(url: str, **kwargs: Any) -> object:
        seen.update(url=url, **kwargs)
        return real_create_engine(url, **kwargs)  # builds the engine; does not connect

    monkeypatch.setattr(db_session, "create_engine", fake_create_engine)
    monkeypatch.setattr(db_session, "get_settings", settings)
    db_session.get_engine.cache_clear()
    try:
        db_session.get_engine()
    finally:
        db_session.get_engine.cache_clear()
    assert seen["connect_args"] == EXPECTED_CONNECT_ARGS
    assert seen["pool_pre_ping"] is True
    assert seen["url"].startswith("postgresql+psycopg://")


# --- the probe -----------------------------------------------------------------------------


def test_unreachable_host_fails_fast_with_a_clear_message() -> None:
    started = time.monotonic()
    status = probe_database(BLACKHOLE_DSN, timeout_s=1)
    elapsed = time.monotonic() - started
    assert status == DatabaseStatus(
        reachable=False, detail="database unreachable (port 5432 blocked?)"
    )
    # Bounded by the timeout instead of hanging (some networks answer "unreachable"
    # even sooner).
    assert elapsed < 5


def test_refused_port_is_reported_as_unreachable() -> None:
    status = probe_database(REFUSED_DSN, timeout_s=1)
    assert status.reachable is False
    assert status.detail == "database unreachable (port 1 blocked?)"


def test_message_names_the_configured_port() -> None:
    assert database_port("postgresql://u:p@db.example.supabase.co:6543/postgres") == 6543
    assert database_port("postgres://u:p@db.example.supabase.co/postgres") == 5432
    assert unreachable_message(6543) == "database unreachable (port 6543 blocked?)"


def test_rejected_login_is_not_called_unreachable() -> None:
    rejected = psycopg.OperationalError('FATAL: password authentication failed for user "u"')
    assert "rejected" in describe_connection_error(rejected, 5432)
    timeout = psycopg.errors.ConnectionTimeout("connection timeout expired")
    assert describe_connection_error(timeout, 5432) == unreachable_message(5432)
    wrapped = OperationalError("select 1", {}, timeout)
    assert describe_connection_error(wrapped, 5432) == unreachable_message(5432)


def test_messages_never_echo_the_password() -> None:
    status = probe_database("postgresql://user:s3cret-pw@127.0.0.1:1/db", timeout_s=1)
    assert status.detail is not None and "s3cret" not in status.detail


def test_health_is_cached_for_the_ttl(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []
    now = [100.0]

    def probe(dsn: str) -> DatabaseStatus:
        calls.append(dsn)
        return UNREACHABLE

    health = DatabaseHealth("dsn", probe=probe, clock=lambda: now[0], ttl_s=30)
    with caplog.at_level(logging.ERROR, logger="app.db.session"):
        assert health.status() == UNREACHABLE
        now[0] += 29
        health.status()
        assert calls == ["dsn"]  # within the TTL: no second 10 s wait
        now[0] += 2
        health.status()
    assert calls == ["dsn", "dsn"]
    assert caplog.messages.count("database unreachable (port 5432 blocked?)") == 2


# --- API ---------------------------------------------------------------------------------


def client_with(health_status: DatabaseStatus) -> TestClient:
    app = main.create_app()  # lifespan not started (no `with`)
    app.dependency_overrides[get_settings] = default_settings
    app.dependency_overrides[get_database_health] = lambda: DatabaseHealth(
        "dsn", probe=lambda _dsn: health_status
    )
    return TestClient(app)


def test_app_config_reports_an_unreachable_database(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="app.db.session"):
        response = client_with(UNREACHABLE).get("/api/app-config")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unreachable (port 5432 blocked?)"}
    assert "database unreachable (port 5432 blocked?)" in caplog.messages


def test_app_config_works_when_the_database_answers() -> None:
    response = client_with(DatabaseStatus(reachable=True)).get("/api/app-config")
    assert response.status_code == 200
    assert response.json()["supabase_anon_key"] == "anon"


def test_any_route_that_cannot_reach_the_database_answers_503(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class DeadSession:
        def __enter__(self) -> DeadSession:
            raise OperationalError(
                "select 1", {}, psycopg.errors.ConnectionTimeout("connection timeout expired")
            )

        def __exit__(self, *_exc: object) -> None:
            return None

    app = main.create_app()
    app.dependency_overrides[get_settings] = default_settings
    app.dependency_overrides[get_session_factory] = lambda: DeadSession
    app.dependency_overrides[current_auth_user] = lambda: AuthUser(id=uuid.uuid4())
    with caplog.at_level(logging.ERROR, logger="app.main"):
        response = TestClient(app, raise_server_exceptions=False).get("/api/me")
    assert response.status_code == 503
    assert response.json()["detail"].startswith("database unreachable (port ")
    assert any("GET /api/me: database unreachable" in m for m in caplog.messages)


# --- startup ------------------------------------------------------------------------------


def test_startup_logs_an_unreachable_database_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(main, "get_settings", lambda: settings(batch_scheduler_enabled=False))
    monkeypatch.setattr(main, "configure_logging", lambda _level: None)
    monkeypatch.setattr(main, "database_status_at_startup", lambda _s: UNREACHABLE)
    with caplog.at_level(logging.INFO, logger="app.main"), TestClient(main.create_app()) as client:
        # The server still starts and serves the pages.
        assert client.get("/app/").status_code == 200
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(m.startswith("database unreachable (port 5432 blocked?)") for m in errors)


def test_startup_probe_against_an_unreachable_host_is_bounded() -> None:
    started = time.monotonic()
    status = main.database_status_at_startup(settings(database_url=REFUSED_DSN))
    assert status.reachable is False
    assert time.monotonic() - started < DB_CONNECT_TIMEOUT_S + 2


# --- live database (opt-in) ------------------------------------------------------------

live_db = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)


@live_db
def test_live_engine_connections_have_the_30_second_statement_timeout() -> None:
    engine = make_engine(load_settings().database_url.get_secret_value())
    try:
        with engine.connect() as connection:
            assert connection.execute(text("show statement_timeout")).scalar() == "30s"
            connection.rollback()  # the committed SET survives a rollback
            assert connection.execute(text("show statement_timeout")).scalar() == "30s"
    finally:
        engine.dispose()


@live_db
def test_live_raw_connections_have_the_30_second_statement_timeout() -> None:
    with open_connection(load_settings().database_url.get_secret_value()) as conn:
        row = conn.execute("show statement_timeout").fetchone()
        assert row is not None and row[0] == "30s"
