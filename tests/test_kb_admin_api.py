"""Tests for the practitioner knowledge-base API and the KB upgrade wiring (R5).

Fakes only, no database or network: the repository functions are swapped for an
in-memory knowledge base that follows the same lifecycle rules. The live rules (and the
database constraints behind them) are covered by ``tests/test_kb_repository.py``.
"""

from __future__ import annotations

import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from apscheduler.triggers.cron import CronTrigger

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from app import main
from app.api import admin, chat, kb_admin
from app.api.auth import current_auth_user
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    BatchAlreadyRunningError,
    KbApprovalIncompleteError,
    KbEntryNotFoundError,
    KbEntryStateError,
)
from app.db.kb_repository import KbEntryView, KbListing
from app.db.models import KbStatus, Practitioner
from app.db.session import get_session_factory
from app.jobs import kb_upgrade
from app.jobs.kb_upgrade import KbUpgradeSummary
from app.services.supabase_auth import AuthUser

ADMIN_KEY = "admin-secret"
SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="srk",
    database_url="postgresql://u:p@localhost:5432/db",
    admin_api_key=ADMIN_KEY,
)
DOCTOR_LOGIN = AuthUser(id=uuid.uuid4(), email="doctor@example.com")
PATIENT_LOGIN = AuthUser(id=uuid.uuid4(), email="patient@example.com")
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
BASE = "/api/practitioner/kb"


def view(entry_id: uuid.UUID, status: KbStatus, **changes: object) -> KbEntryView:
    data: dict[str, object] = {
        "id": entry_id,
        "entry_key": "seed:a1c",
        "title": "What is A1C?",
        "body": "",
        "language": "en",
        "status": status,
        "kb_version": None,
        "source_citation": None,
        "origin": "manual",
        "review_hint": None,
        "priority": False,
        "supersedes_id": None,
        "supersedes_version": None,
        "proposed_from_turn_id": None,
        "source_turn": None,
        "open_draft_id": None,
        "created_by_name": None,
        "approved_by_name": None,
        "approved_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return KbEntryView.model_validate({**data, **changes})


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


class Kb:
    """An approved entry, a draft that edits it, and a proposed draft."""

    def __init__(self) -> None:
        self.doctor = Practitioner(id=uuid.uuid4(), user_id=uuid.uuid4(), full_name="Dr Demo")
        self.approved = uuid.uuid4()
        self.edit_draft = uuid.uuid4()
        self.proposed = uuid.uuid4()
        self.rows: dict[uuid.UUID, KbEntryView] = {
            self.approved: view(
                self.approved,
                "approved",
                body="A1C is your 3-month average.",
                source_citation="ADA 2026",
                kb_version=4,
                approved_at=NOW,
            ),
            self.proposed: view(
                self.proposed,
                "draft",
                entry_key="review:x",
                title="can i eat jackfruit",
                origin="proposed_from_review",
                review_hint="Asked 3 times",
            ),
        }
        self.calls: list[tuple[str, uuid.UUID, uuid.UUID]] = []
        self.session = FakeSession()

    def find_practitioner(self, _s: object, auth_id: uuid.UUID) -> Practitioner | None:
        return self.doctor if auth_id == DOCTOR_LOGIN.id else None

    def _get(self, entry_id: uuid.UUID) -> KbEntryView:
        if entry_id not in self.rows:
            raise KbEntryNotFoundError("missing")
        return self.rows[entry_id]

    def list_kb(self, _s: object, practitioner_id: uuid.UUID) -> KbListing:
        self.calls.append(("list", practitioner_id, practitioner_id))
        rows = list(self.rows.values())
        return KbListing(
            drafts=[r for r in rows if r.status == "draft"],
            approved=[r for r in rows if r.status == "approved"],
        )

    def update_draft(
        self,
        _s: object,
        entry_id: uuid.UUID,
        practitioner_id: uuid.UUID,
        *,
        title: str,
        body: str,
        source_citation: str | None,
    ) -> KbEntryView:
        self.calls.append(("update", entry_id, practitioner_id))
        row = self._get(entry_id)
        if row.status != "draft":
            raise KbEntryStateError("only drafts can be edited; this entry is approved")
        self.rows[entry_id] = row.model_copy(
            update={"title": title, "body": body, "source_citation": source_citation}
        )
        return self.rows[entry_id]

    def start_edit(
        self, _s: object, entry_id: uuid.UUID, practitioner_id: uuid.UUID
    ) -> tuple[KbEntryView, bool]:
        self.calls.append(("edit", entry_id, practitioner_id))
        row = self._get(entry_id)
        if row.status != "approved":
            raise KbEntryStateError("only approved entries can be edited")
        if self.edit_draft in self.rows:
            return self.rows[self.edit_draft], False
        draft = row.model_copy(
            update={"id": self.edit_draft, "status": "draft", "supersedes_id": entry_id}
        )
        self.rows[self.edit_draft] = draft
        return draft, True

    def approve_draft(
        self, _s: object, entry_id: uuid.UUID, practitioner_id: uuid.UUID
    ) -> KbEntryView:
        self.calls.append(("approve", entry_id, practitioner_id))
        row = self._get(entry_id)
        if row.status != "draft":
            raise KbEntryStateError("only drafts can be approved")
        if not row.body.strip() or not (row.source_citation or "").strip():
            raise KbApprovalIncompleteError("write the entry text and cite its source")
        if row.supersedes_id is not None:
            old = self.rows[row.supersedes_id]
            self.rows[old.id] = old.model_copy(update={"status": "retired"})
        self.rows[entry_id] = row.model_copy(
            update={"status": "approved", "kb_version": 5, "review_hint": None}
        )
        return self.rows[entry_id]

    def reject_draft(
        self, _s: object, entry_id: uuid.UUID, practitioner_id: uuid.UUID
    ) -> KbEntryView:
        self.calls.append(("reject", entry_id, practitioner_id))
        row = self._get(entry_id)
        if row.status != "draft":
            raise KbEntryStateError("only drafts can be rejected")
        self.rows[entry_id] = row.model_copy(update={"status": "retired"})
        return self.rows[entry_id]


@pytest.fixture
def kb(monkeypatch: pytest.MonkeyPatch) -> Kb:
    k = Kb()
    monkeypatch.setattr(chat, "find_practitioner_by_auth_user", k.find_practitioner)
    for name in ("list_kb", "update_draft", "start_edit", "approve_draft", "reject_draft"):
        monkeypatch.setattr(kb_admin, name, getattr(k, name))
    return k


class Harness:
    def __init__(self, client: TestClient, login: dict[str, AuthUser]) -> None:
        self.client = client
        self.login = login

    def as_(self, who: AuthUser) -> None:
        self.login["who"] = who


@pytest.fixture
def harness(kb: Kb) -> Iterator[Harness]:
    app = main.app
    login = {"who": DOCTOR_LOGIN}
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_session_factory] = lambda: lambda: kb.session
    app.dependency_overrides[current_auth_user] = lambda: login["who"]
    yield Harness(TestClient(app), login)
    app.dependency_overrides.clear()


DRAFT_BODY = {"title": "Jackfruit", "body": "Fine in small portions.", "source_citation": "ICMR"}


def _every_route(kb: Kb) -> list[tuple[str, str, dict[str, str] | None]]:
    return [
        ("GET", BASE, None),
        ("PUT", f"{BASE}/{kb.proposed}", DRAFT_BODY),
        ("POST", f"{BASE}/{kb.approved}/edit", None),
        ("POST", f"{BASE}/{kb.proposed}/approve", None),
        ("POST", f"{BASE}/{kb.proposed}/reject", None),
    ]


# --- authentication and role ----------------------------------------------------------


def test_requests_without_a_login_are_401(kb: Kb) -> None:
    app = main.app
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    try:
        client = TestClient(app)
        for method, url, body in _every_route(kb):
            assert client.request(method, url, json=body).status_code == 401, url
    finally:
        app.dependency_overrides.clear()
    assert kb.calls == []


def test_non_practitioners_get_403(harness: Harness, kb: Kb) -> None:
    harness.as_(PATIENT_LOGIN)
    for method, url, body in _every_route(kb):
        assert harness.client.request(method, url, json=body).status_code == 403, url
    assert kb.calls == [] and kb.session.commits == 0


# --- listing and editing ------------------------------------------------------------------


def test_list_returns_drafts_and_approved_entries(harness: Harness, kb: Kb) -> None:
    response = harness.client.get(BASE)
    assert response.status_code == 200
    body = response.json()
    assert [d["id"] for d in body["drafts"]] == [str(kb.proposed)]
    assert [a["id"] for a in body["approved"]] == [str(kb.approved)]
    assert body["drafts"][0]["review_hint"] == "Asked 3 times"
    assert body["drafts"][0]["priority"] is False
    assert kb.calls == [("list", kb.doctor.id, kb.doctor.id)]


def test_put_saves_a_draft(harness: Harness, kb: Kb) -> None:
    response = harness.client.put(
        f"{BASE}/{kb.proposed}", json={**DRAFT_BODY, "title": "  Jackfruit  "}
    )
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Jackfruit"  # stripped by the body model
    assert kb.calls == [("update", kb.proposed, kb.doctor.id)]
    assert kb.session.commits == 1


def test_put_on_an_approved_entry_is_409(harness: Harness, kb: Kb) -> None:
    response = harness.client.put(f"{BASE}/{kb.approved}", json=DRAFT_BODY)
    assert response.status_code == 409
    assert kb.rows[kb.approved].title == "What is A1C?"
    assert kb.session.commits == 0


@pytest.mark.parametrize(
    "body",
    [
        {"title": "", "body": "x"},
        {"title": "   ", "body": "x"},
        {"title": "ok", "body": "x" * 6001},
        {"title": "ok", "source_citation": "c" * 501},
        {"title": "ok", "status": "approved"},  # status is never set through PUT
        {"title": "ok", "review_hint": "x"},
    ],
)
def test_put_rejects_bad_bodies(harness: Harness, kb: Kb, body: dict[str, str]) -> None:
    assert harness.client.put(f"{BASE}/{kb.proposed}", json=body).status_code == 422
    assert kb.calls == []


def test_unknown_entry_is_404(harness: Harness) -> None:
    missing = uuid.uuid4()
    assert harness.client.put(f"{BASE}/{missing}", json=DRAFT_BODY).status_code == 404
    for action in ("edit", "approve", "reject"):
        assert harness.client.post(f"{BASE}/{missing}/{action}").status_code == 404


def test_edit_creates_a_superseding_draft_then_returns_the_same_one(
    harness: Harness, kb: Kb
) -> None:
    first = harness.client.post(f"{BASE}/{kb.approved}/edit")
    assert first.status_code == 201
    assert first.json()["supersedes_id"] == str(kb.approved)
    assert first.json()["status"] == "draft"
    again = harness.client.post(f"{BASE}/{kb.approved}/edit")
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert kb.rows[kb.approved].status == "approved"


def test_edit_of_a_draft_is_409(harness: Harness, kb: Kb) -> None:
    assert harness.client.post(f"{BASE}/{kb.proposed}/edit").status_code == 409


# --- approve and reject -------------------------------------------------------------------


def test_approve_with_an_empty_body_or_citation_is_422(harness: Harness, kb: Kb) -> None:
    response = harness.client.post(f"{BASE}/{kb.proposed}/approve")
    assert response.status_code == 422
    assert "cite its source" in response.json()["detail"]
    harness.client.put(f"{BASE}/{kb.proposed}", json={"title": "t", "body": "text"})
    assert harness.client.post(f"{BASE}/{kb.proposed}/approve").status_code == 422
    assert kb.rows[kb.proposed].status == "draft"


def test_approve_records_the_signed_in_practitioner(harness: Harness, kb: Kb) -> None:
    harness.client.put(f"{BASE}/{kb.proposed}", json=DRAFT_BODY)
    response = harness.client.post(f"{BASE}/{kb.proposed}/approve")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    assert ("approve", kb.proposed, kb.doctor.id) in kb.calls


def test_approving_an_edit_retires_the_old_entry(harness: Harness, kb: Kb) -> None:
    draft = harness.client.post(f"{BASE}/{kb.approved}/edit").json()
    assert harness.client.post(f"{BASE}/{draft['id']}/approve").status_code == 200
    assert kb.rows[kb.approved].status == "retired"


def test_approve_twice_is_409(harness: Harness, kb: Kb) -> None:
    harness.client.put(f"{BASE}/{kb.proposed}", json=DRAFT_BODY)
    assert harness.client.post(f"{BASE}/{kb.proposed}/approve").status_code == 200
    assert harness.client.post(f"{BASE}/{kb.proposed}/approve").status_code == 409


def test_reject_retires_a_draft_and_refuses_an_approved_entry(harness: Harness, kb: Kb) -> None:
    assert harness.client.post(f"{BASE}/{kb.proposed}/reject").json()["status"] == "retired"
    assert harness.client.post(f"{BASE}/{kb.approved}/reject").status_code == 409
    assert kb.rows[kb.approved].status == "approved"


# --- admin endpoint and scheduler ----------------------------------------------------------


def _summary() -> KbUpgradeSummary:
    return KbUpgradeSummary(
        run_id=uuid.uuid4(),
        status="succeeded",
        triggered_by="admin_api",
        window_start=NOW,
        window_end=NOW,
        candidates=3,
        drafts_proposed=2,
        skipped=1,
        promoted=1,
        draft_ids=[uuid.uuid4(), uuid.uuid4()],
        error=None,
    )


@pytest.fixture
def admin_client() -> Iterator[TestClient]:
    main.app.dependency_overrides[get_settings] = lambda: SETTINGS
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_admin_kb_upgrade_run(admin_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_run(_factory: object, *, triggered_by: str) -> KbUpgradeSummary:
        calls.append(triggered_by)
        return _summary()

    monkeypatch.setattr(admin, "get_session_factory", lambda: None)
    monkeypatch.setattr(admin, "run_kb_upgrade", fake_run)
    assert admin_client.post("/admin/kb-upgrade-run").status_code == 401
    response = admin_client.post("/admin/kb-upgrade-run", headers={"X-Admin-Api-Key": ADMIN_KEY})
    assert response.status_code == 200
    assert response.json()["drafts_proposed"] == 2
    assert calls == ["admin_api"]


def test_admin_kb_upgrade_run_while_running_is_409(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(_factory: object, *, triggered_by: str) -> KbUpgradeSummary:
        raise BatchAlreadyRunningError(f"a kb_upgrade run is already in progress ({triggered_by})")

    monkeypatch.setattr(admin, "get_session_factory", lambda: None)
    monkeypatch.setattr(admin, "run_kb_upgrade", busy)
    response = admin_client.post("/admin/kb-upgrade-run", headers={"X-Admin-Api-Key": ADMIN_KEY})
    assert response.status_code == 409


def _settings(**changes: object) -> Settings:
    return SETTINGS.model_copy(update=changes)


def test_scheduler_runs_the_kb_upgrade_weekly() -> None:
    job = main.build_scheduler(_settings()).get_job(main.KB_UPGRADE_JOB_ID)
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["day_of_week"], fields["hour"], fields["minute"]) == ("mon", "2", "30")
    assert (job.max_instances, job.coalesce) == (1, True)


def test_scheduler_skips_the_kb_upgrade_when_disabled() -> None:
    scheduler = main.build_scheduler(_settings(kb_upgrade_enabled=False))
    assert scheduler.get_job(main.KB_UPGRADE_JOB_ID) is None
    assert scheduler.get_job(main.BATCH_JOB_ID) is not None


@pytest.mark.parametrize("error", [BatchAlreadyRunningError("busy"), RuntimeError("database down")])
def test_scheduled_kb_upgrade_never_raises(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def boom(*_a: object, **_k: object) -> KbUpgradeSummary:
        raise error

    monkeypatch.setattr(kb_upgrade, "run_kb_upgrade", boom)
    monkeypatch.setattr("app.db.session.get_session_factory", lambda: None)
    kb_upgrade.scheduled_kb_upgrade()
