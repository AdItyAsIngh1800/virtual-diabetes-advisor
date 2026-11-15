"""Tests for GET /api/advisories/{id} and the /app/advisory/{id} page (R3).

Route tests override auth and swap the repository functions for in-memory fakes, and
use a fake audio store; no database or Supabase Storage is touched.
"""

from __future__ import annotations

import uuid
import warnings
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from app import main
from app.api import advisories
from app.api.auth import current_auth_user
from app.core.config import Settings, get_settings
from app.core.exceptions import AudioStorageError
from app.db.advisory_repository import AdvisoryView
from app.db.models import User
from app.db.session import get_session_factory
from app.services.supabase_auth import AuthUser

SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="srk",
    database_url="postgresql://u:p@localhost:5432/db",
    advisory_audio_url_ttl_seconds=120,
)
LOGIN = AuthUser(id=uuid.uuid4(), email="me@example.com")
OTHER_LOGIN = AuthUser(id=uuid.uuid4(), email="other@example.com")
MEASURED = datetime(2026, 11, 15, 7, 0, tzinfo=UTC)


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
    def __init__(self) -> None:
        self.me = User(id=uuid.uuid4(), role="patient", auth_user_id=LOGIN.id)
        self.other = User(id=uuid.uuid4(), role="patient", auth_user_id=OTHER_LOGIN.id)
        self.views: dict[uuid.UUID, AdvisoryView] = {}
        self.opened: dict[uuid.UUID, datetime] = {}

    def add(self, owner: User, **overrides: Any) -> AdvisoryView:
        fields: dict[str, Any] = {
            "id": uuid.uuid4(),
            "user_id": owner.id,
            "state": "pre_breach_high",
            "limit_kind": "fasting_high",
            "reading_value_mg_dl": Decimal("126.0"),
            "threshold_mg_dl": Decimal("130.0"),
            "context": "fasting",
            "measured_at": MEASURED,
            "language": "en",
            "message_text": "Your fasting glucose is 126 mg/dL, running high.",
            "escalated": False,
            "delivery_status": "sent",
            "audio_path": f"{owner.id}/x.mp3",
            "opened_at": None,
        }
        view = AdvisoryView(**{**fields, **overrides})
        self.views[view.id] = view
        return view

    def find_patient_by_auth_user(self, _s: object, auth_user_id: uuid.UUID) -> User | None:
        return {LOGIN.id: self.me, OTHER_LOGIN.id: self.other}.get(auth_user_id)

    def advisory_for_view(self, _s: object, advisory_id: uuid.UUID) -> AdvisoryView | None:
        return self.views.get(advisory_id)

    def mark_opened(self, _s: object, advisory_id: uuid.UUID, at: datetime) -> datetime:
        return self.opened.setdefault(advisory_id, at)


class FakeAudioStore:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.signed: list[tuple[str, int]] = []

    def upload(self, path: str, audio: bytes) -> None:
        raise AssertionError("the API never uploads")

    def signed_url(self, path: str, expires_in_s: int) -> str:
        if self.fail:
            raise AudioStorageError("storage sign answered HTTP 400")
        self.signed.append((path, expires_in_s))
        return f"https://abc.supabase.co/storage/v1/object/sign/advisory-audio/{path}?token=t"


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> FakeRepo:
    fake = FakeRepo()
    for name in ("find_patient_by_auth_user", "advisory_for_view", "mark_opened"):
        monkeypatch.setattr(advisories, name, getattr(fake, name))
    return fake


def client_for(
    login: AuthUser | None = LOGIN, store: FakeAudioStore | None = None
) -> tuple[TestClient, FakeAudioStore]:
    store = store or FakeAudioStore()
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_session_factory] = lambda: FakeSession
    app.dependency_overrides[advisories.get_audio_store] = lambda: store
    if login is not None:
        app.dependency_overrides[current_auth_user] = lambda: login
    return TestClient(app), store


def test_owner_gets_advisory_with_short_lived_audio_url(repo: FakeRepo) -> None:
    view = repo.add(repo.me)
    client, store = client_for()
    response = client.get(f"/api/advisories/{view.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(view.id)
    assert (body["direction"], body["urgent"], body["context"]) == ("high", False, "fasting")
    assert Decimal(body["reading_value_mg_dl"]) == Decimal("126.0")
    assert Decimal(body["threshold_mg_dl"]) == Decimal("130.0")
    assert body["message_text"].startswith("Your fasting glucose is 126 mg/dL")
    assert body["spoken_text"] == "Your fasting glucose is 126, running high."
    assert body["audio_url"].startswith("https://abc.supabase.co/storage/v1/object/sign/")
    assert body["audio_url_expires_in_s"] == 120
    assert store.signed == [(view.audio_path, 120)]


def test_other_patients_advisory_is_forbidden(repo: FakeRepo) -> None:
    theirs = repo.add(repo.other)
    client, store = client_for()
    response = client.get(f"/api/advisories/{theirs.id}")
    assert response.status_code == 403
    assert "another patient" in response.json()["detail"]
    assert "126" not in response.text
    assert store.signed == []
    assert repo.opened == {}  # not marked opened


def test_unlinked_login_unknown_id_and_no_session(repo: FakeRepo) -> None:
    view = repo.add(repo.me)
    stranger, _ = client_for(AuthUser(id=uuid.uuid4()))
    assert stranger.get(f"/api/advisories/{view.id}").status_code == 403
    client, _ = client_for()
    assert client.get(f"/api/advisories/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/advisories/not-a-uuid").status_code == 422
    anonymous, _ = client_for(login=None)
    assert anonymous.get(f"/api/advisories/{view.id}").status_code == 401


def test_viewing_sets_opened_at_once(repo: FakeRepo) -> None:
    view = repo.add(repo.me)
    client, _ = client_for()
    before = datetime.now(UTC)
    first = client.get(f"/api/advisories/{view.id}").json()
    second = client.get(f"/api/advisories/{view.id}").json()
    opened = datetime.fromisoformat(first["opened_at"])
    assert before <= opened <= datetime.now(UTC) + timedelta(seconds=1)
    assert second["opened_at"] == first["opened_at"]
    assert first["delivery_status"] == "opened"
    assert repo.opened == {view.id: opened}


def test_missing_or_unsignable_audio_returns_null_url(repo: FakeRepo) -> None:
    no_audio = repo.add(repo.me, audio_path=None)
    client, _ = client_for()
    body = client.get(f"/api/advisories/{no_audio.id}").json()
    assert (body["audio_url"], body["audio_url_expires_in_s"]) == (None, None)

    broken = repo.add(repo.me)
    client, _ = client_for(store=FakeAudioStore(fail=True))
    response = client.get(f"/api/advisories/{broken.id}")
    assert response.status_code == 200
    assert response.json()["audio_url"] is None


def test_below_54_is_urgent_low(repo: FakeRepo) -> None:
    view = repo.add(
        repo.me,
        state="breach_low",
        limit_kind="low",
        reading_value_mg_dl=Decimal("49.0"),
        threshold_mg_dl=Decimal("70.0"),
        escalated=True,
        delivery_status="failed",
    )
    client, _ = client_for()
    body = client.get(f"/api/advisories/{view.id}").json()
    assert (body["direction"], body["urgent"], body["escalated"]) == ("low", True, True)
    assert body["delivery_status"] == "failed"  # opening does not rewrite a failure


# --- the page ------------------------------------------------------------------------


def test_advisory_page_is_served_for_a_uuid(repo: FakeRepo) -> None:
    client, _ = client_for()
    response = client.get(f"/app/advisory/{uuid.uuid4()}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    for marker in ('id="play"', "Talk to a doctor", "/app/advisory.js"):
        assert marker in response.text
    assert 'id="doctor" type="button" disabled' in response.text  # Phase 7
    assert client.get("/app/advisory/not-a-uuid").status_code == 422


def test_advisory_script_falls_back_to_speech_synthesis(repo: FakeRepo) -> None:
    client, _ = client_for()
    script = client.get("/app/advisory.js")
    assert script.status_code == 200
    for marker in ("speechSynthesis", "SpeechSynthesisUtterance", "/api/advisories/", "en-IN"):
        assert marker in script.text


def test_service_worker_opens_the_advisory_page(repo: FakeRepo) -> None:
    client, _ = client_for()
    sw = client.get("/app/sw.js").text
    assert "/app/advisory/${data.advisory_id}" in sw
    assert "notificationclick" in sw


def test_app_listens_on_private_realtime_topic(repo: FakeRepo) -> None:
    client, _ = client_for()
    app_js = client.get("/app/app.js").text
    assert "advisories:${session.user.id}" in app_js
    assert "private: true" in app_js
