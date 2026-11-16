"""Tests for /api/push, /api/app-config and the /app static patient page (R3).

Route tests override auth and swap the repository functions for an in-memory fake.
The ``live`` tests (``RUN_DB_TESTS=1``) exercise the real repository and store inside
one rolled-back transaction on Supabase.
"""

from __future__ import annotations

import os
import uuid
import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Connection, select
from sqlalchemy.orm import Session

with warnings.catch_warnings():
    # Starlette's TestClient warns that httpx is deprecated in favour of httpx2.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from app import main
from app.api import push
from app.api.auth import current_auth_user
from app.core.config import Settings, get_settings, load_settings
from app.db.models import PushSubscription, User
from app.db.push_repository import (
    SqlSubscriptionStore,
    find_patient_by_auth_user,
    revoke_subscription,
    upsert_subscription,
)
from app.db.session import get_session_factory, make_engine
from app.services.supabase_auth import AuthUser
from scripts.gen_vapid_keys import generate_vapid_keys

REQUIRED: dict[str, Any] = {
    "supabase_url": "https://abc.supabase.co",
    "supabase_service_role_key": "srk",
    "database_url": "postgresql://u:p@localhost:5432/db",
}
KEYS = generate_vapid_keys()
VAPID: dict[str, Any] = {
    "vapid_public_key": KEYS.public_key,
    "vapid_private_key": KEYS.private_key,
    "vapid_subject": "mailto:team6@example.com",
}
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc:def-123"
BODY = {
    "endpoint": ENDPOINT,
    "expirationTime": None,
    "keys": {"p256dh": "B" + "x" * 86, "auth": "y" * 22},
}
LOGIN = AuthUser(id=uuid.uuid4(), email="me@example.com")

live = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


class FakeRepo:
    """In-memory stand-in for app.db.push_repository (endpoint -> user, revoked)."""

    def __init__(self) -> None:
        self.patient = User(id=uuid.uuid4(), role="patient", auth_user_id=LOGIN.id)
        self.practitioner: User | None = None
        self.linked = True
        self.rows: dict[str, dict[str, Any]] = {}

    def find_patient_by_auth_user(self, _session: object, auth_user_id: uuid.UUID) -> User | None:
        return self.patient if self.linked and auth_user_id == LOGIN.id else None

    def find_staff_by_auth_user(self, _session: object, auth_user_id: uuid.UUID) -> User | None:
        if self.practitioner is not None and auth_user_id == self.practitioner.auth_user_id:
            return self.practitioner
        return None

    def upsert_subscription(self, _session: object, **row: Any) -> uuid.UUID:
        existing = self.rows.get(row["endpoint"])
        sub_id = existing["id"] if existing else uuid.uuid4()
        self.rows[row["endpoint"]] = {**row, "id": sub_id, "revoked": False}
        return sub_id

    def revoke_subscription(
        self, _session: object, *, user_id: uuid.UUID, endpoint: str, at: datetime
    ) -> bool:
        row = self.rows.get(endpoint)
        if row is None or row["user_id"] != user_id or row["revoked"]:
            return False
        row["revoked"] = True
        return True


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> FakeRepo:
    fake = FakeRepo()
    for name in (
        "find_patient_by_auth_user",
        "find_staff_by_auth_user",
        "upsert_subscription",
        "revoke_subscription",
    ):
        monkeypatch.setattr(push, name, getattr(fake, name))
    return fake


def client_for(settings: Settings, *, signed_in: bool = True) -> TestClient:
    app = main.create_app()  # lifespan (scheduler) is not started without `with`
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_factory] = lambda: FakeSession
    if signed_in:
        app.dependency_overrides[current_auth_user] = lambda: LOGIN
    return TestClient(app)


@pytest.fixture
def client(repo: FakeRepo) -> TestClient:
    return client_for(Settings(_env_file=None, **REQUIRED, **VAPID))


# --- public key / app config --------------------------------------------------------


def test_public_key(client: TestClient) -> None:
    response = client.get("/api/push/public-key")
    assert response.status_code == 200
    assert response.json() == {"public_key": KEYS.public_key}
    assert KEYS.private_key not in response.text


def test_public_key_unavailable_without_vapid() -> None:
    response = client_for(Settings(_env_file=None, **REQUIRED)).get("/api/push/public-key")
    assert response.status_code == 503


def test_app_config_returns_only_public_values() -> None:
    settings = Settings(_env_file=None, **REQUIRED, **VAPID, supabase_anon_key="sb_publishable_x")
    response = client_for(settings).get("/api/app-config")
    assert response.status_code == 200
    assert response.json() == {
        "supabase_url": "https://abc.supabase.co",
        "supabase_anon_key": "sb_publishable_x",
        "vapid_public_key": KEYS.public_key,
    }
    assert "srk" not in response.text
    assert KEYS.private_key not in response.text


def test_app_config_needs_anon_key() -> None:
    response = client_for(Settings(_env_file=None, **REQUIRED)).get("/api/app-config")
    assert response.status_code == 503
    assert "SUPABASE_ANON_KEY" in response.json()["detail"]


# --- subscribe / unsubscribe --------------------------------------------------------


@pytest.mark.parametrize("header", [None, "Token abc", "Bearer", "Bearer   "])
def test_subscribe_requires_a_bearer_token(repo: FakeRepo, header: str | None) -> None:
    # Rejected locally: none of these reach Supabase Auth (no network in this test).
    client = client_for(Settings(_env_file=None, **REQUIRED, **VAPID), signed_in=False)
    headers = {"Authorization": header} if header else {}
    response = client.post("/api/push/subscribe", json=BODY, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert repo.rows == {}


def test_subscribe_stores_and_resubscribe_updates(client: TestClient, repo: FakeRepo) -> None:
    first = client.post("/api/push/subscribe", json=BODY, headers={"User-Agent": "Chrome/131"})
    assert first.status_code == 201
    again = client.post("/api/push/subscribe", json=BODY)
    assert again.json() == first.json()  # same endpoint -> same row
    row = repo.rows[ENDPOINT]
    assert (row["user_id"], row["p256dh"], row["auth"]) == (
        repo.patient.id,
        BODY["keys"]["p256dh"],  # type: ignore[index]
        BODY["keys"]["auth"],  # type: ignore[index]
    )


def test_subscribe_refused_when_login_not_linked(client: TestClient, repo: FakeRepo) -> None:
    repo.linked = False
    response = client.post("/api/push/subscribe", json=BODY)
    assert response.status_code == 403
    assert "--my-email" in response.json()["detail"]
    assert repo.rows == {}


def test_practitioner_login_can_subscribe(client: TestClient, repo: FakeRepo) -> None:
    """R4: practitioners enable alerts to hear about new handoffs."""
    repo.linked = False
    repo.practitioner = User(id=uuid.uuid4(), role="practitioner", auth_user_id=LOGIN.id)
    response = client.post("/api/push/subscribe", json=BODY)
    assert response.status_code == 201
    (row,) = repo.rows.values()
    assert row["user_id"] == repo.practitioner.id


@pytest.mark.parametrize(
    "change",
    [
        {"endpoint": "http://fcm.googleapis.com/x"},  # not https
        {"endpoint": "https://" + "x" * 2050},
        {"keys": {"p256dh": "short", "auth": "y" * 22}},
        {"keys": {"p256dh": "B" + "x" * 86, "auth": "bad auth!"}},
        {"keys": {"p256dh": "B" + "x" * 86}},
    ],
)
def test_subscribe_validates_the_body(
    client: TestClient, repo: FakeRepo, change: dict[str, Any]
) -> None:
    assert client.post("/api/push/subscribe", json={**BODY, **change}).status_code == 422
    assert repo.rows == {}


def test_unsubscribe_revokes_and_is_idempotent(client: TestClient, repo: FakeRepo) -> None:
    client.post("/api/push/subscribe", json=BODY)
    for _ in range(2):
        response = client.request("DELETE", "/api/push/subscribe", json={"endpoint": ENDPOINT})
        assert response.status_code == 204
    assert repo.rows[ENDPOINT]["revoked"] is True


# --- /app static page ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "content_type", "marker"),
    [
        ("/app/", "text/html", "Enable alerts"),
        ("/app/common.js", "javascript", "pushManager.subscribe"),
        ("/app/app.js", "javascript", "MediaRecorder"),
        ("/app/practitioner", "text/html", "Practitioner inbox"),
        ("/app/practitioner.js", "javascript", "/api/practitioner/handoffs"),
        ("/app/sw.js", "javascript", "notificationclick"),
        ("/app/manifest.webmanifest", "application/manifest+json", '"start_url": "/app/"'),
    ],
)
def test_patient_app_files(client: TestClient, path: str, content_type: str, marker: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert marker in response.text


def test_patient_app_icons(client: TestClient) -> None:
    for size in (192, 512):
        response = client.get(f"/app/icon-{size}.png")
        assert response.status_code == 200
        assert response.content.startswith(b"\x89PNG")


# --- live repository round trip (opt-in) --------------------------------------------


@pytest.fixture
def conn() -> Iterator[Connection]:
    engine = make_engine(load_settings().database_url.get_secret_value())
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            yield connection
        finally:
            outer.rollback()
    engine.dispose()


@pytest.fixture
def factory(conn: Connection) -> Callable[[], Session]:
    return lambda: Session(bind=conn, join_transaction_mode="create_savepoint")


@live
def test_live_subscription_lifecycle(factory: Callable[[], Session]) -> None:
    patient, other = uuid.uuid4(), uuid.uuid4()
    endpoint = f"https://push.example.invalid/{uuid.uuid4()}"
    with factory() as session:
        session.add_all(
            [User(id=patient, role="patient"), User(id=other, role="patient", is_active=False)]
        )
        session.commit()
    keys = {"p256dh": "k" * 87, "auth": "a" * 22}
    with factory() as session:
        first = upsert_subscription(
            session, user_id=patient, endpoint=endpoint, user_agent="UA", **keys
        )
        session.commit()
    store = SqlSubscriptionStore(factory)
    (active,) = store.active_for_user(patient)
    assert (active.id, active.endpoint) == (first, endpoint)

    now = datetime.now(UTC)
    store.mark_success(first, now)
    store.revoke(first, now)
    assert store.active_for_user(patient) == []

    # Re-subscribing the same browser reuses the row and clears revoked_at.
    with factory() as session:
        again = upsert_subscription(
            session, user_id=patient, endpoint=endpoint, user_agent=None, **keys
        )
        session.commit()
    assert again == first
    with factory() as session:
        row = session.scalars(select(PushSubscription).where(PushSubscription.id == first)).one()
        assert row.revoked_at is None
        assert row.last_success_at is not None
        # A patient can only revoke their own device.
        assert not revoke_subscription(session, user_id=other, endpoint=endpoint, at=now)
        assert revoke_subscription(session, user_id=patient, endpoint=endpoint, at=now)
        session.commit()


@live
def test_live_find_patient_by_auth_user(conn: Connection, factory: Callable[[], Session]) -> None:
    from sqlalchemy import text  # noqa: PLC0415

    login, inactive_login = uuid.uuid4(), uuid.uuid4()
    for auth_id in (login, inactive_login):
        conn.execute(
            text("insert into auth.users (id, email) values (:id, :email)"),
            {"id": auth_id, "email": f"push-test-{auth_id}@example.invalid"},
        )
    patient = uuid.uuid4()
    with factory() as session:
        session.add_all(
            [
                User(id=patient, role="patient", auth_user_id=login),
                User(id=uuid.uuid4(), role="patient", auth_user_id=inactive_login, is_active=False),
            ]
        )
        session.commit()
    with factory() as session:
        found = find_patient_by_auth_user(session, login)
        assert found is not None and found.id == patient
        assert find_patient_by_auth_user(session, inactive_login) is None
        assert find_patient_by_auth_user(session, uuid.uuid4()) is None
