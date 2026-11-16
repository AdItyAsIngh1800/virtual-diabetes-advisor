"""Tests for the answer-review API (R5) with fakes; no database or network.

The repository functions are swapped for an in-memory fake that applies the same
access rule (patients assigned to the practitioner only); the live rules are covered
by ``tests/test_audit_repository.py``.
"""

from __future__ import annotations

import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from app import main
from app.api import audit, chat
from app.api.auth import current_auth_user
from app.core.config import Settings, get_settings
from app.core.exceptions import ReviewNoteRequiredError, TurnNotReviewableError
from app.db.audit_repository import (
    NOTE_REQUIRED,
    ReviewableTurn,
    ReviewFilter,
    ReviewRecord,
    TurnCursor,
    TurnPage,
)
from app.db.models import Practitioner, ReviewVerdict
from app.db.session import get_session_factory
from app.services.supabase_auth import AuthUser

SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="srk",
    database_url="postgresql://u:p@localhost:5432/db",
    review_low_confidence_below=0.25,
)
DOCTOR_LOGIN = AuthUser(id=uuid.uuid4(), email="doctor@example.com")
OTHER_DOCTOR_LOGIN = AuthUser(id=uuid.uuid4(), email="other@example.com")
PATIENT_LOGIN = AuthUser(id=uuid.uuid4(), email="patient@example.com")
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


class Clinic:
    """Two practitioners; ``mine`` belongs to the first, ``theirs`` to the second."""

    def __init__(self) -> None:
        self.doctor = Practitioner(id=uuid.uuid4(), user_id=uuid.uuid4(), full_name="Dr Demo")
        self.other = Practitioner(id=uuid.uuid4(), user_id=uuid.uuid4(), full_name="Dr Other")
        self.mine = uuid.uuid4()
        self.theirs = uuid.uuid4()
        self.owner = {self.mine: self.doctor.id, self.theirs: self.other.id}
        self.reviews: list[ReviewRecord] = []
        self.list_calls: list[tuple[uuid.UUID, str, int, TurnCursor | None, float]] = []
        self.session = FakeSession()

    def find_practitioner(self, _s: object, auth_id: uuid.UUID) -> Practitioner | None:
        return {DOCTOR_LOGIN.id: self.doctor, OTHER_DOCTOR_LOGIN.id: self.other}.get(auth_id)

    def list_reviewable_turns(
        self,
        _s: object,
        practitioner_id: uuid.UUID,
        review_filter: ReviewFilter = "all",
        limit: int = 25,
        cursor: TurnCursor | None = None,
        *,
        low_confidence_below: float,
    ) -> TurnPage:
        self.list_calls.append(
            (practitioner_id, review_filter, limit, cursor, low_confidence_below)
        )
        items = [
            ReviewableTurn(
                turn_id=turn_id,
                created_at=NOW,
                patient_first_name="Asha",
                question="what is a1c",
                spoken=False,
                answer="A1C is your 3-month average.",
                intent="question",
                model="tfidf",
                model_version="sklearn-1.7",
                escalated=False,
                unanswered=False,
                low_confidence=False,
                retrieval_confidence=0.4,
                kb_version=3,
                sources=[],
                current_review=None,
                review_count=0,
            )
            for turn_id, owner in self.owner.items()
            if owner == practitioner_id
        ]
        return TurnPage(items=items, next_cursor=None)

    def add_review(
        self,
        _s: object,
        *,
        turn_id: uuid.UUID,
        reviewer_id: uuid.UUID,
        verdict: ReviewVerdict,
        note: str | None,
    ) -> ReviewRecord:
        # Same order and rules as the real repository.
        if verdict in NOTE_REQUIRED and not (note or "").strip():
            raise ReviewNoteRequiredError("note required")
        if self.owner.get(turn_id) != reviewer_id:
            raise TurnNotReviewableError("not yours")
        review = ReviewRecord(
            id=uuid.uuid4(),
            turn_id=turn_id,
            verdict=verdict,
            note=note,
            reviewer_id=reviewer_id,
            reviewer_name="Dr Demo",
            created_at=NOW,
        )
        self.reviews.append(review)
        return review


@pytest.fixture
def clinic(monkeypatch: pytest.MonkeyPatch) -> Clinic:
    c = Clinic()
    # _require_practitioner (shared with the handoff inbox) looks the login up here.
    monkeypatch.setattr(chat, "find_practitioner_by_auth_user", c.find_practitioner)
    monkeypatch.setattr(audit, "list_reviewable_turns", c.list_reviewable_turns)
    monkeypatch.setattr(audit, "add_review", c.add_review)
    return c


class Harness:
    def __init__(self, client: TestClient, login: dict[str, AuthUser]) -> None:
        self.client = client
        self.login = login

    def as_(self, who: AuthUser) -> None:
        self.login["who"] = who


@pytest.fixture
def harness(clinic: Clinic) -> Iterator[Harness]:
    app = main.app
    login = {"who": DOCTOR_LOGIN}
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_session_factory] = lambda: lambda: clinic.session
    app.dependency_overrides[current_auth_user] = lambda: login["who"]
    yield Harness(TestClient(app), login)
    app.dependency_overrides.clear()


def _review_url(turn_id: uuid.UUID) -> str:
    return f"/api/practitioner/turns/{turn_id}/review"


# --- authentication and role --------------------------------------------------------


def test_requests_without_a_login_are_401(clinic: Clinic) -> None:
    app = main.app
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    try:
        client = TestClient(app)
        assert client.get("/api/practitioner/turns").status_code == 401
        response = client.post(_review_url(clinic.mine), json={"verdict": "correct"})
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()
    assert clinic.reviews == [] and clinic.list_calls == []


def test_non_practitioners_get_403(harness: Harness, clinic: Clinic) -> None:
    harness.as_(PATIENT_LOGIN)
    assert harness.client.get("/api/practitioner/turns").status_code == 403
    response = harness.client.post(_review_url(clinic.mine), json={"verdict": "correct"})
    assert response.status_code == 403
    assert clinic.reviews == [] and clinic.list_calls == []


# --- listing ------------------------------------------------------------------------------


def test_list_passes_filter_page_and_cutoff(harness: Harness, clinic: Clinic) -> None:
    cursor = TurnCursor(created_at=NOW, id=uuid.uuid4())
    response = harness.client.get(
        "/api/practitioner/turns",
        params={"filter": "low_confidence", "limit": 10, "cursor": cursor.encode()},
    )
    assert response.status_code == 200
    assert clinic.list_calls == [(clinic.doctor.id, "low_confidence", 10, cursor, 0.25)]
    assert [i["turn_id"] for i in response.json()["items"]] == [str(clinic.mine)]


def test_list_defaults(harness: Harness, clinic: Clinic) -> None:
    assert harness.client.get("/api/practitioner/turns").status_code == 200
    assert clinic.list_calls == [(clinic.doctor.id, "all", 25, None, 0.25)]


@pytest.mark.parametrize(
    "params",
    [
        {"filter": "everything"},
        {"limit": 0},
        {"limit": 101},
        {"cursor": "not-a-cursor"},
        {"cursor": "x" * 300},
    ],
)
def test_bad_list_parameters_are_422(harness: Harness, params: dict[str, str | int]) -> None:
    assert harness.client.get("/api/practitioner/turns", params=params).status_code == 422


def test_list_shows_first_name_only_and_no_contact_details(harness: Harness) -> None:
    item = harness.client.get("/api/practitioner/turns").json()["items"][0]
    assert item["patient_first_name"] == "Asha"
    for key in ("phone", "phone_e164", "endpoint", "p256dh", "auth", "full_name", "email"):
        assert key not in item
    # The response models themselves have no such field, whatever the data holds.
    fields = set(ReviewableTurn.model_fields) | set(ReviewRecord.model_fields)
    assert not {f for f in fields if "phone" in f or "endpoint" in f or "email" in f}


def test_each_practitioner_lists_only_their_patients(harness: Harness, clinic: Clinic) -> None:
    harness.as_(OTHER_DOCTOR_LOGIN)
    items = harness.client.get("/api/practitioner/turns").json()["items"]
    assert [i["turn_id"] for i in items] == [str(clinic.theirs)]


# --- reviewing -----------------------------------------------------------------------------


def test_correct_needs_no_note(harness: Harness, clinic: Clinic) -> None:
    response = harness.client.post(_review_url(clinic.mine), json={"verdict": "correct"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["verdict"] == "correct" and body["note"] is None
    assert body["turn_id"] == str(clinic.mine)
    assert clinic.session.commits == 1


def test_note_is_stripped(harness: Harness, clinic: Clinic) -> None:
    response = harness.client.post(
        _review_url(clinic.mine), json={"verdict": "unsafe", "note": "  Gave a dose.  "}
    )
    assert response.status_code == 201
    assert clinic.reviews[0].note == "Gave a dose."


@pytest.mark.parametrize("verdict", ["incorrect", "unsafe", "needs_kb_update"])
@pytest.mark.parametrize("note", [None, "", "   "])
def test_missing_note_is_422(
    harness: Harness, clinic: Clinic, verdict: str, note: str | None
) -> None:
    body: dict[str, object] = {"verdict": verdict}
    if note is not None:
        body["note"] = note
    response = harness.client.post(_review_url(clinic.mine), json=body)
    assert response.status_code == 422
    assert "note is required" in response.text
    assert clinic.reviews == []


@pytest.mark.parametrize(
    "body",
    [
        {"verdict": "wrong"},
        {},
        {"verdict": "correct", "note": "x" * 1001},
        {"verdict": "correct", "extra": 1},
    ],
)
def test_bad_review_bodies_are_422(
    harness: Harness, clinic: Clinic, body: dict[str, object]
) -> None:
    assert harness.client.post(_review_url(clinic.mine), json=body).status_code == 422
    assert clinic.reviews == []


def test_cannot_review_another_practitioners_patient(harness: Harness, clinic: Clinic) -> None:
    response = harness.client.post(
        _review_url(clinic.theirs), json={"verdict": "unsafe", "note": "Wrong advice"}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "turn not found"
    assert clinic.reviews == [] and clinic.session.commits == 0


def test_unknown_turn_is_404_like_a_foreign_one(harness: Harness, clinic: Clinic) -> None:
    response = harness.client.post(_review_url(uuid.uuid4()), json={"verdict": "correct"})
    assert response.status_code == 404
    assert response.json()["detail"] == "turn not found"


def test_reviews_can_be_added_again(harness: Harness, clinic: Clinic) -> None:
    for verdict, note in (("correct", None), ("needs_kb_update", "Mention ADA 2026 targets.")):
        response = harness.client.post(
            _review_url(clinic.mine), json={"verdict": verdict, "note": note}
        )
        assert response.status_code == 201
    assert [r.verdict for r in clinic.reviews] == ["correct", "needs_kb_update"]


def test_routes_are_mounted() -> None:
    paths = set(main.app.openapi()["paths"])
    assert {"/api/practitioner/turns", "/api/practitioner/turns/{turn_id}/review"} <= paths
