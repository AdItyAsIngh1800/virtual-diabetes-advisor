"""Live tests for the knowledge-base upgrade loop (R5) against Supabase.

Covers the weekly job (app.jobs.kb_upgrade), the draft lifecycle in
app.db.kb_repository, the database guards added in 0009, and the practitioner KB API
end to end.

Opt-in: ``RUN_DB_TESTS=1 uv run pytest tests/test_kb_repository.py``. Everything runs in
one outer transaction that is rolled back, so nothing is left behind. Questions carry a
random token so rows already in the database never collide with the test's.
"""

from __future__ import annotations

import os
import uuid
import warnings
from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Connection, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, load_settings
from app.core.exceptions import (
    KbApprovalIncompleteError,
    KbEntryNotFoundError,
    KbEntryStateError,
)
from app.db.audit_repository import add_review
from app.db.chat_repository import SqlChatStore, db_now
from app.db.kb_repository import (
    approve_draft,
    list_kb,
    reject_draft,
    start_edit,
    update_draft,
)
from app.db.models import BatchRun, KbEntry, PatientAssignment
from app.db.session import get_session_factory, make_engine
from app.jobs.kb_upgrade import KB_UPGRADE_JOB, RULESET_VERSION, run_kb_upgrade
from app.services.chat import UNKNOWN_REPLY
from app.services.kb_upgrade import PROPOSED_KEY_PREFIX
from app.services.supabase_auth import AuthUser
from tests.test_audit_repository import _auth_login, _bot_turn, _patient, _practitioner

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)


@pytest.fixture
def settings() -> Settings:
    return load_settings()


@pytest.fixture
def conn(settings: Settings) -> Iterator[Connection]:
    engine = make_engine(settings.database_url.get_secret_value())
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


def _approved_entry(session: Session, approver: uuid.UUID, *, body: str = "Old text.") -> KbEntry:
    version = (session.scalar(select(func.max(KbEntry.kb_version))) or 0) + 1
    entry = KbEntry(
        id=uuid.uuid4(),
        entry_key=f"kb-test:{uuid.uuid4()}",
        title="What is A1C?",
        body=body,
        language="en",
        status="approved",
        kb_version=version,
        source_citation="ADA Standards of Care 2026, section 6",
        origin="manual",
        approved_by=approver,
        approved_at=db_now(session),
    )
    session.add(entry)
    session.flush()
    return entry


def _draft(session: Session, **fields: object) -> KbEntry:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "entry_key": f"kb-test:{uuid.uuid4()}",
        "title": "Can I eat ragi?",
        "body": "",
        "status": "draft",
        "origin": "manual",
    }
    values.update(fields)
    entry = KbEntry(**values)
    session.add(entry)
    session.flush()
    return entry


def _served_ids(session: Session, settings: Settings) -> set[uuid.UUID]:
    return {doc.id for doc in SqlChatStore(session, settings).approved_kb_entries()}


class Clinic:
    """Two practitioners and their patients, with turns the job should and should not use."""

    def __init__(self, session: Session) -> None:
        token = uuid.uuid4().hex[:10]
        t = db_now(session) + timedelta(days=1)  # newer than any real row
        self.doctor = _practitioner(session)
        self.other_doctor = _practitioner(session)
        self.patient = _patient(session, practitioner_id=self.doctor, name="Asha Rao")
        self.other_patient = _patient(session, practitioner_id=self.other_doctor, name="Ravi K")
        kb = _draft(session, title="Low glucose signs", body="Shaky, sweaty.").id
        self.kb_used = kb

        self.question = f"Can I eat jackfruit {token}?"
        self.wrong = _bot_turn(
            session, self.patient, t, text_in=self.question, kb_ids=[kb], confidence=0.3
        )
        add_review(
            session,
            turn_id=self.wrong,
            reviewer_id=self.doctor,
            verdict="needs_kb_update",
            note="Add jackfruit to the diet entry.",
        )
        # Marked incorrect, then corrected: the latest review ("correct") wins.
        self.corrected = _bot_turn(
            session,
            self.patient,
            t + timedelta(seconds=1),
            text_in=f"is rice ok {token}",
            kb_ids=[kb],
            confidence=0.3,
        )
        add_review(
            session,
            turn_id=self.corrected,
            reviewer_id=self.doctor,
            verdict="incorrect",
            note="Wrong entry.",
        )
        add_review(
            session, turn_id=self.corrected, reviewer_id=self.doctor, verdict="correct", note=None
        )
        self.unsafe_question = f"double my dose {token}"
        self.unsafe = _bot_turn(
            session,
            self.patient,
            t + timedelta(seconds=2),
            text_in=self.unsafe_question,
            kb_ids=[kb],
            confidence=0.3,
        )
        add_review(
            session, turn_id=self.unsafe, reviewer_id=self.doctor, verdict="unsafe", note="No."
        )
        # Asked twice (two patients, different spelling): a repeated gap in the KB.
        self.repeat_question = f"Is ragi good {token}"
        self.unknown_first = _bot_turn(
            session,
            self.patient,
            t + timedelta(seconds=3),
            text_in=f"is RAGI good {token}?",
            reply=UNKNOWN_REPLY,
        )
        self.unknown_latest = _bot_turn(
            session,
            self.other_patient,
            t + timedelta(seconds=4),
            text_in=self.repeat_question,
            reply=UNKNOWN_REPLY,
        )
        # Asked once only: not proposed.
        self.unknown_once = _bot_turn(
            session,
            self.patient,
            t + timedelta(seconds=5),
            text_in=f"what about kodo millet {token}",
            reply=UNKNOWN_REPLY,
        )
        self.my_turns = [
            self.wrong,
            self.corrected,
            self.unsafe,
            self.unknown_first,
            self.unknown_latest,
            self.unknown_once,
        ]

    def drafts(self, session: Session) -> dict[uuid.UUID, KbEntry]:
        rows = session.scalars(
            select(KbEntry).where(KbEntry.proposed_from_turn_id.in_(self.my_turns))
        ).all()
        return {r.proposed_from_turn_id: r for r in rows if r.proposed_from_turn_id}


# --- the weekly job -------------------------------------------------------------------


def test_job_proposes_empty_drafts_and_is_idempotent(
    factory: Callable[[], Session], settings: Settings
) -> None:
    with factory() as session:
        c = Clinic(session)
        session.commit()

    first = run_kb_upgrade(factory, triggered_by="manual", settings=settings)
    assert first.status == "succeeded" and first.error is None
    assert first.drafts_proposed >= 3  # other rows in the database may add more

    with factory() as session:
        drafts = c.drafts(session)
        # Reviewed needs_kb_update and unsafe, and the repeated question from its latest
        # ask; never the corrected answer or the one-off question.
        assert set(drafts) == {c.wrong, c.unsafe, c.unknown_latest}
        # Only the unsafe one is a priority, and it says why.
        assert {t for t, d in drafts.items() if d.priority} == {c.unsafe}
        unsafe = drafts[c.unsafe]
        assert unsafe.title == c.unsafe_question and unsafe.body == ""
        assert unsafe.review_hint is not None
        assert unsafe.review_hint.startswith("Marked unsafe by a reviewer.")
        reviewed = drafts[c.wrong]
        # The draft links back to its reviewed turn.
        assert reviewed.proposed_from_turn_id == c.wrong
        assert reviewed.entry_key == f"{PROPOSED_KEY_PREFIX}{c.wrong}"
        assert reviewed.title == c.question
        # The system writes no medical content and publishes nothing.
        assert reviewed.body == ""
        assert reviewed.source_citation is None
        assert (reviewed.status, reviewed.origin) == ("draft", "proposed_from_review")
        assert (reviewed.approved_by, reviewed.approved_at, reviewed.kb_version) == (
            None,
            None,
            None,
        )
        assert reviewed.created_by is None
        assert reviewed.review_hint is not None
        assert "Add jackfruit to the diet entry." in reviewed.review_hint
        repeated = drafts[c.unknown_latest]
        assert repeated.title == c.repeat_question
        assert repeated.review_hint is not None
        assert repeated.review_hint.startswith("Asked 2 times")

        run = session.get(BatchRun, first.run_id)
        assert run is not None
        assert (run.job, run.status, run.triggered_by) == (KB_UPGRADE_JOB, "succeeded", "manual")
        assert run.kb_drafts_proposed == first.drafts_proposed
        assert run.kb_candidates == first.candidates >= first.drafts_proposed
        assert run.model_version == RULESET_VERSION
        assert run.finished_at is not None and run.error is None

    second = run_kb_upgrade(factory, triggered_by="manual", settings=settings)
    assert second.status == "succeeded"
    assert second.drafts_proposed == 0
    with factory() as session:
        assert len(c.drafts(session)) == 3
        run = session.get(BatchRun, second.run_id)
        assert run is not None and run.kb_drafts_proposed == 0


def test_rejected_draft_is_not_proposed_again(
    factory: Callable[[], Session], settings: Settings
) -> None:
    with factory() as session:
        c = Clinic(session)
        session.commit()
    run_kb_upgrade(factory, settings=settings)
    with factory() as session:
        draft = c.drafts(session)[c.wrong]
        assert reject_draft(session, draft.id, c.doctor).status == "retired"
        session.commit()
    run_kb_upgrade(factory, settings=settings)
    with factory() as session:
        assert c.drafts(session)[c.wrong].status == "retired"
        count = session.scalar(select(func.count()).where(KbEntry.proposed_from_turn_id == c.wrong))
        assert count == 1


def test_job_skips_a_question_that_already_has_an_open_draft(
    factory: Callable[[], Session], settings: Settings
) -> None:
    with factory() as session:
        c = Clinic(session)
        _draft(session, title=c.question.upper())  # a practitioner already started one
        session.commit()
    run_kb_upgrade(factory, settings=settings)
    with factory() as session:
        assert set(c.drafts(session)) == {c.unsafe, c.unknown_latest}


def test_unsafe_review_promotes_an_open_draft_and_priority_drafts_list_first(
    factory: Callable[[], Session], settings: Settings
) -> None:
    with factory() as session:
        c = Clinic(session)
        # A practitioner already opened an ordinary draft for the unsafe question.
        existing_id = _draft(session, title=c.unsafe_question.upper()).id
        session.commit()
    summary = run_kb_upgrade(factory, settings=settings)
    assert summary.promoted >= 1
    with factory() as session:
        promoted = session.get(KbEntry, existing_id)
        assert promoted is not None and promoted.priority and promoted.status == "draft"
        # Promoted, not duplicated: no second draft from the unsafe turn.
        assert c.unsafe not in c.drafts(session)

        listing = list_kb(session, c.doctor).drafts
        flags = [d.priority for d in listing]
        assert flags == sorted(flags, reverse=True)  # every priority draft comes first
        assert listing[0].priority

        # Approving clears the flag: priority only means "waiting, and urgent".
        update_draft(
            session,
            existing_id,
            c.doctor,
            title="Never change your own dose",
            body="Do not change your insulin dose without your doctor.",
            source_citation="ADA Standards of Care 2026, section 9",
        )
        approved = approve_draft(session, existing_id, c.doctor)
        assert approved.status == "approved" and approved.priority is False

    again = run_kb_upgrade(factory, settings=settings)
    assert again.drafts_proposed == 0 and again.promoted == 0


# --- serving: drafts never reach patients ---------------------------------------------


def test_a_draft_is_never_served(factory: Callable[[], Session], settings: Settings) -> None:
    with factory() as session:
        c = Clinic(session)
        session.commit()
    run_kb_upgrade(factory, settings=settings)
    with factory() as session:
        draft = c.drafts(session)[c.wrong]
        served = _served_ids(session, settings)
        assert draft.id not in served and c.kb_used not in served
        # Saving text into a draft still does not serve it.
        update_draft(
            session,
            draft.id,
            c.doctor,
            title="Jackfruit",
            body="Small portions of ripe jackfruit are fine.",
            source_citation="ICMR-NIN 2024",
        )
        assert draft.id not in _served_ids(session, settings)
        # Approval is what serves it.
        approved = approve_draft(session, draft.id, c.doctor)
        assert approved.status == "approved"
        assert draft.id in _served_ids(session, settings)


# --- approval -------------------------------------------------------------------------


def test_approve_is_refused_without_body_or_citation(factory: Callable[[], Session]) -> None:
    with factory() as session:
        doctor = _practitioner(session)
        draft = _draft(session)
        with pytest.raises(KbApprovalIncompleteError):
            approve_draft(session, draft.id, doctor)
        update_draft(
            session, draft.id, doctor, title="Ragi", body="Ragi is a millet.", source_citation="  "
        )
        with pytest.raises(KbApprovalIncompleteError):
            approve_draft(session, draft.id, doctor)
        update_draft(session, draft.id, doctor, title="Ragi", body="   ", source_citation="ICMR")
        with pytest.raises(KbApprovalIncompleteError):
            approve_draft(session, draft.id, doctor)
        session.refresh(draft)
        assert (draft.status, draft.kb_version, draft.approved_by) == ("draft", None, None)


def test_database_refuses_an_approved_row_without_text_or_with_a_hint(
    factory: Callable[[], Session],
) -> None:
    with factory() as session:
        doctor = _practitioner(session)
        now = db_now(session)
        bad_rows: list[dict[str, str | None]] = [
            {"body": "Text", "source_citation": None},
            {"body": " ", "source_citation": "ADA"},
            {"body": "Text", "source_citation": "ADA", "review_hint": "reviewer note"},
        ]
        for extra in bad_rows:
            with pytest.raises(DBAPIError), session.begin_nested():
                _draft(
                    session,
                    status="approved",
                    kb_version=999_999,
                    approved_by=doctor,
                    approved_at=now,
                    **extra,
                )


def test_approving_a_superseding_draft_retires_the_old_row_and_bumps_kb_version(
    factory: Callable[[], Session], settings: Settings
) -> None:
    with factory() as session:
        doctor = _practitioner(session)
        reviewer = _practitioner(session)
        old = _approved_entry(session, doctor)
        draft, created = start_edit(session, old.id, reviewer)
        assert created and draft.status == "draft"
        assert (draft.supersedes_id, draft.supersedes_version) == (old.id, old.kb_version)
        assert (draft.entry_key, draft.body) == (old.entry_key, old.body)
        # Edit twice: the open draft is returned, not a second one.
        again, created_again = start_edit(session, old.id, reviewer)
        assert again.id == draft.id and not created_again
        listed = {e.id: e for e in list_kb(session, reviewer).approved}
        assert listed[old.id].open_draft_id == draft.id

        update_draft(
            session,
            draft.id,
            reviewer,
            title="What is A1C?",
            body="New text.",
            source_citation="ADA Standards of Care 2026, section 6",
        )
        latest = session.scalar(select(func.max(KbEntry.kb_version))) or 0
        approved = approve_draft(session, draft.id, reviewer)
        assert approved.status == "approved"
        assert approved.kb_version == latest + 1
        assert approved.approved_by_name == "Dr Review"
        assert approved.approved_at is not None and approved.review_hint is None
        session.refresh(old)
        assert old.status == "retired"
        assert old.body == "Old text." and old.kb_version == latest  # never edited in place
        served = _served_ids(session, settings)
        assert draft.id in served and old.id not in served
        row = session.get(KbEntry, draft.id)
        assert row is not None and row.approved_by == reviewer


def test_draft_of_a_no_longer_approved_entry_cannot_be_approved(
    factory: Callable[[], Session],
) -> None:
    with factory() as session:
        doctor = _practitioner(session)
        old = _approved_entry(session, doctor)
        first, _ = start_edit(session, old.id, doctor)
        update_draft(session, first.id, doctor, title="A1C", body="v2", source_citation="ADA")
        approve_draft(session, first.id, doctor)
        # A stale draft (made before the approval) that still points at the old row.
        stale = _draft(
            session,
            entry_key=old.entry_key,
            body="v2b",
            source_citation="ADA",
            supersedes_id=old.id,
        )
        with pytest.raises(KbEntryStateError, match="no longer the approved version"):
            approve_draft(session, stale.id, doctor)
        with pytest.raises(KbEntryStateError):
            start_edit(session, old.id, doctor)  # old is retired now


# --- only drafts are edited -----------------------------------------------------------


def test_only_drafts_may_be_edited_approved_or_rejected(factory: Callable[[], Session]) -> None:
    with factory() as session:
        doctor = _practitioner(session)
        entry = _approved_entry(session, doctor)
        with pytest.raises(KbEntryStateError):
            update_draft(session, entry.id, doctor, title="x", body="y", source_citation="z")
        with pytest.raises(KbEntryStateError):
            approve_draft(session, entry.id, doctor)
        with pytest.raises(KbEntryStateError):
            reject_draft(session, entry.id, doctor)
        draft = _draft(session)
        reject_draft(session, draft.id, doctor)
        with pytest.raises(KbEntryStateError):
            update_draft(session, draft.id, doctor, title="x", body="y", source_citation="z")
        with pytest.raises(KbEntryNotFoundError):
            update_draft(session, uuid.uuid4(), doctor, title="x", body="y", source_citation="z")
        session.refresh(entry)
        assert (entry.status, entry.body) == ("approved", "Old text.")


def test_database_freezes_approved_and_retired_rows(factory: Callable[[], Session]) -> None:
    with factory() as session:
        doctor = _practitioner(session)
        entry = _approved_entry(session, doctor)
        retired = _draft(session)
        reject_draft(session, retired.id, doctor)
        refused = [
            (entry.id, {"body": "Edited in place."}),
            (entry.id, {"status": "draft"}),
            (retired.id, {"status": "draft"}),
            (retired.id, {"title": "changed"}),
        ]
        for entry_id, values in refused:
            with pytest.raises(DBAPIError, match="never edited in place"), session.begin_nested():
                session.execute(update(KbEntry).where(KbEntry.id == entry_id).values(**values))
        # Retiring an approved row is the one allowed change.
        session.execute(update(KbEntry).where(KbEntry.id == entry.id).values(status="retired"))


# --- what each practitioner sees ------------------------------------------------------


def test_source_turn_is_shown_only_to_practitioners_who_see_the_patient(
    factory: Callable[[], Session], settings: Settings
) -> None:
    with factory() as session:
        c = Clinic(session)
        session.commit()
    run_kb_upgrade(factory, settings=settings)
    with factory() as session:
        draft_id = c.drafts(session)[c.wrong].id
        mine = {d.id: d for d in list_kb(session, c.doctor).drafts}[draft_id]
        assert mine.source_turn is not None
        assert mine.source_turn.turn_id == c.wrong
        assert mine.source_turn.question == c.question
        assert mine.source_turn.answer == "A1C is your 3-month average."
        theirs = {d.id: d for d in list_kb(session, c.other_doctor).drafts}[draft_id]
        # The KB is shared, but the conversation is the patient's.
        assert theirs.source_turn is None
        assert theirs.proposed_from_turn_id == c.wrong
        assert theirs.review_hint == mine.review_hint
        assert "+9198" not in theirs.model_dump_json()


# --- API end to end -------------------------------------------------------------------


def test_api_end_to_end_on_the_database(conn: Connection, factory: Callable[[], Session]) -> None:
    from app import main  # noqa: PLC0415
    from app.api.auth import current_auth_user  # noqa: PLC0415

    with factory() as session:
        login = _auth_login(session)
        patient_login = _auth_login(session)
        doctor = _practitioner(session, auth_user_id=login)
        patient = _patient(session, practitioner_id=None, name="Asha")
        session.add(PatientAssignment(practitioner_id=doctor, patient_id=patient))
        entry = _approved_entry(session, doctor)
        old_id, old_version = entry.id, entry.kb_version
        session.commit()
    assert old_version is not None

    app = main.app
    api_factory = sessionmaker(
        bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    who = {"id": login}
    app.dependency_overrides[get_session_factory] = lambda: api_factory
    app.dependency_overrides[current_auth_user] = lambda: AuthUser(
        id=who["id"], email="doctor@example.invalid"
    )
    base = "/api/practitioner/kb"
    try:
        client = TestClient(app)
        listing = client.get(base)
        assert listing.status_code == 200
        assert str(old_id) in {e["id"] for e in listing.json()["approved"]}

        edit = client.post(f"{base}/{old_id}/edit")
        assert edit.status_code == 201, edit.text
        draft_id = edit.json()["id"]
        assert client.post(f"{base}/{old_id}/edit").status_code == 200
        assert client.put(f"{base}/{old_id}", json={"title": "x"}).status_code == 409

        saved = client.put(
            f"{base}/{draft_id}",
            json={"title": "What is A1C?", "body": "Text.", "source_citation": ""},
        )
        assert saved.status_code == 200 and saved.json()["source_citation"] is None
        assert client.post(f"{base}/{draft_id}/approve").status_code == 422
        client.put(
            f"{base}/{draft_id}",
            json={"title": "What is A1C?", "body": "Text.", "source_citation": "ADA 2026"},
        )

        # A signed-in non-practitioner is refused and changes nothing.
        who["id"] = patient_login
        assert client.post(f"{base}/{draft_id}/approve").status_code == 403
        assert client.get(base).status_code == 403
        who["id"] = login

        approved = client.post(f"{base}/{draft_id}/approve")
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["status"] == "approved" and body["kb_version"] > old_version
        assert client.post(f"{base}/{draft_id}/approve").status_code == 409
    finally:
        app.dependency_overrides.clear()

    with factory() as session:
        new = session.get(KbEntry, uuid.UUID(draft_id))
        assert new is not None and new.approved_by == doctor
        retired = session.get(KbEntry, old_id)
        assert retired is not None and retired.status == "retired"
