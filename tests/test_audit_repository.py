"""Live tests for app.db.audit_repository (R5 answer review) against Supabase.

Opt-in: ``RUN_DB_TESTS=1 uv run pytest tests/test_audit_repository.py``. Everything runs
in one outer transaction that is rolled back, so nothing is left behind.
"""

from __future__ import annotations

import os
import uuid
import warnings
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Connection, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import load_settings
from app.core.exceptions import (
    AuditLogImmutableError,
    ReviewNoteRequiredError,
    TurnNotReviewableError,
)
from app.db.audit_repository import (
    ReviewFilter,
    TurnCursor,
    TurnPage,
    add_review,
    is_visible,
    list_reviewable_turns,
)
from app.db.chat_repository import db_now, save_turn
from app.db.models import (
    ConversationTurn,
    KbEntry,
    PatientAssignment,
    Practitioner,
    Profile,
    ReviewVerdict,
    TurnIntent,
    TurnReview,
    User,
    UserRole,
)
from app.db.session import get_session_factory, make_engine
from app.services.chat import RELAY_MODEL, TFIDF_MODEL, UNKNOWN_REPLY
from app.services.supabase_auth import AuthUser

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)

CUTOFF = 0.21


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


def _practitioner(
    session: Session, *, role: UserRole = "practitioner", auth_user_id: uuid.UUID | None = None
) -> uuid.UUID:
    user = User(id=uuid.uuid4(), role=role, auth_user_id=auth_user_id)
    session.add(user)
    session.flush()
    practitioner = Practitioner(
        id=uuid.uuid4(), user_id=user.id, full_name="Dr Review", phone_e164=None
    )
    session.add(practitioner)
    session.flush()
    return practitioner.id


def _patient(session: Session, *, practitioner_id: uuid.UUID | None, name: str) -> uuid.UUID:
    user = User(id=uuid.uuid4(), role="patient")
    session.add(user)
    session.flush()
    # A unique, recognisable phone number: the tests check it never leaks.
    session.add(
        Profile(
            user_id=user.id,
            full_name=name,
            phone_e164=f"+9198{uuid.uuid4().int % 10**8:08d}",
            diabetes_status="diabetes",
        )
    )
    if practitioner_id is not None:
        session.add(PatientAssignment(practitioner_id=practitioner_id, patient_id=user.id))
    session.flush()
    return user.id


def _kb_entry(session: Session) -> uuid.UUID:
    # A draft is enough: the review list shows whatever entry a turn names.
    entry = KbEntry(
        id=uuid.uuid4(),
        entry_key=f"review-test-{uuid.uuid4()}",
        title="Low glucose signs",
        body="Shaky, sweaty, confused.",
        source_citation="ADA Standards of Care 2026",
    )
    session.add(entry)
    session.flush()
    return entry.id


def _bot_turn(
    session: Session,
    patient: uuid.UUID,
    at: datetime,
    *,
    text_in: str = "what is a1c",
    reply: str = "A1C is your 3-month average.",
    intent: TurnIntent = "question",
    model: str = TFIDF_MODEL,
    kb_ids: list[uuid.UUID] | None = None,
    confidence: float | None = None,
    escalated: bool = False,
    transcript: str | None = None,
) -> uuid.UUID:
    turn = save_turn(
        session,
        user_id=patient,
        direction="inbound",
        raw_text=None if transcript else text_in,
        transcript=transcript,
        intent=intent,
        response_text=reply,
        model=model,
        model_version="v",
        latency_ms=5,
        escalated=escalated,
        kb_ids_used=kb_ids,
        retrieval_confidence=confidence,
        created_at=at,
    )
    return turn.id


class Clinic:
    """Two practitioners, each with a patient, and a mix of turns."""

    def __init__(self, session: Session) -> None:
        self.now = db_now(session) + timedelta(days=1)  # newer than any real row
        self.doctor = _practitioner(session)
        self.other_doctor = _practitioner(session)
        self.patient = _patient(session, practitioner_id=self.doctor, name="Asha Rao Kumar")
        self.other_patient = _patient(session, practitioner_id=self.other_doctor, name="Ravi K")
        self.kb = _kb_entry(session)
        t = self.now
        self.answered = _bot_turn(session, self.patient, t, kb_ids=[self.kb], confidence=0.42)
        self.shaky = _bot_turn(
            session,
            self.patient,
            t + timedelta(seconds=1),
            kb_ids=[self.kb],
            confidence=0.18,
            transcript="why do i feel shaky",
        )
        self.unknown = _bot_turn(
            session,
            self.patient,
            t + timedelta(seconds=2),
            text_in="can i eat jackfruit",
            reply=UNKNOWN_REPLY,
            confidence=0.05,
        )
        self.red_flag = _bot_turn(
            session,
            self.patient,
            t + timedelta(seconds=3),
            text_in="chest pain",
            reply="Call 112 now.",
            intent="symptom_red_flag",
            model="rules",
            escalated=True,
        )
        # Not advisor answers: a relay ack, a practitioner reply and a system notice.
        self.relay = _bot_turn(
            session,
            self.patient,
            t + timedelta(seconds=4),
            reply="Sent to your practitioner.",
            model=RELAY_MODEL,
        )
        self.practitioner_reply = save_turn(
            session,
            user_id=self.patient,
            direction="outbound",
            raw_text=None,
            transcript=None,
            intent=None,
            response_text="Please recheck.",
            model="practitioner",
            model_version=None,
            latency_ms=None,
            escalated=False,
            author_practitioner_id=self.doctor,
            created_at=t + timedelta(seconds=5),
        ).id
        self.foreign = _bot_turn(session, self.other_patient, t + timedelta(seconds=6))


def _ids(page: TurnPage) -> list[uuid.UUID]:
    return [item.turn_id for item in page.items]


def test_patient_id_limits_the_list_to_one_patient(factory: Callable[[], Session]) -> None:
    with factory() as session:
        c = Clinic(session)
        second = _patient(session, practitioner_id=c.doctor, name="Meena Iyer")
        theirs = _bot_turn(session, second, c.now + timedelta(seconds=30))
        everyone = list_reviewable_turns(session, c.doctor, low_confidence_below=CUTOFF)
        assert theirs in {t.turn_id for t in everyone.items}
        asha = list_reviewable_turns(
            session, c.doctor, low_confidence_below=CUTOFF, patient_id=c.patient
        )
        assert asha.items and theirs not in {t.turn_id for t in asha.items}
        assert {t.patient_first_name for t in asha.items} == {"Asha"}
        meena = list_reviewable_turns(
            session, c.doctor, low_confidence_below=CUTOFF, patient_id=second
        )
        assert [t.turn_id for t in meena.items] == [theirs]
        # Another practitioner's patient: not visible, and filtered out even if asked.
        assert is_visible(session, c.doctor, c.patient)
        assert not is_visible(session, c.doctor, c.other_patient)
        assert not is_visible(session, c.doctor, uuid.uuid4())
        hidden = list_reviewable_turns(
            session, c.doctor, low_confidence_below=CUTOFF, patient_id=c.other_patient
        )
        assert hidden.items == []


def test_lists_only_advisor_replies_of_my_patients_newest_first(
    factory: Callable[[], Session],
) -> None:
    with factory() as session:
        c = Clinic(session)
        page = list_reviewable_turns(session, c.doctor, low_confidence_below=CUTOFF)
        assert _ids(page) == [c.red_flag, c.unknown, c.shaky, c.answered]
        assert page.next_cursor is None
        first = page.items[-1]
        assert first.patient_first_name == "Asha"
        assert first.question == "what is a1c" and not first.spoken
        assert first.answer == "A1C is your 3-month average."
        assert [s.id for s in first.sources] == [c.kb]
        assert first.sources[0].source_citation == "ADA Standards of Care 2026"
        assert first.current_review is None and first.review_count == 0
        voice = page.items[2]
        assert voice.spoken and voice.question == "why do i feel shaky"
        # Nothing about the patient beyond a first name.
        dumped = page.model_dump_json()
        assert "+9198" not in dumped and "Kumar" not in dumped and "phone" not in dumped
        other = list_reviewable_turns(session, c.other_doctor, low_confidence_below=CUTOFF)
        assert _ids(other) == [c.foreign]


@pytest.mark.parametrize(
    ("review_filter", "expected"),
    [
        ("escalated", ["red_flag"]),
        ("low_confidence", ["shaky"]),
        ("unanswered", ["unknown"]),
    ],
)
def test_filters(
    factory: Callable[[], Session], review_filter: ReviewFilter, expected: list[str]
) -> None:
    with factory() as session:
        c = Clinic(session)
        page = list_reviewable_turns(
            session,
            c.doctor,
            review_filter,
            low_confidence_below=CUTOFF,
        )
        assert _ids(page) == [getattr(c, name) for name in expected]
        flags = {i.turn_id: (i.unanswered, i.low_confidence) for i in page.items}
        assert flags.get(c.unknown, (True, False)) == (True, False)
        assert flags.get(c.shaky, (False, True)) == (False, True)


def test_low_confidence_cutoff_is_exclusive(factory: Callable[[], Session]) -> None:
    with factory() as session:
        c = Clinic(session)
        at_cutoff = list_reviewable_turns(
            session, c.doctor, "low_confidence", low_confidence_below=0.18
        )
        assert _ids(at_cutoff) == []  # 0.18 is not below 0.18
        just_above = list_reviewable_turns(
            session, c.doctor, "low_confidence", low_confidence_below=0.1801
        )
        assert _ids(just_above) == [c.shaky]


def test_latest_review_is_the_current_verdict(factory: Callable[[], Session]) -> None:
    with factory() as session:
        c = Clinic(session)
        first = add_review(
            session, turn_id=c.shaky, reviewer_id=c.doctor, verdict="correct", note=None
        )
        second = add_review(
            session,
            turn_id=c.shaky,
            reviewer_id=c.doctor,
            verdict="needs_kb_update",
            note="  Should mention checking glucose first.  ",
        )
        assert second.created_at > first.created_at
        assert second.note == "Should mention checking glucose first."
        assert second.reviewer_name == "Dr Review"
        page = list_reviewable_turns(session, c.doctor, low_confidence_below=CUTOFF)
        item = next(i for i in page.items if i.turn_id == c.shaky)
        assert item.current_review is not None
        assert item.current_review.id == second.id
        assert item.current_review.verdict == "needs_kb_update"
        assert item.review_count == 2
        unreviewed = list_reviewable_turns(
            session, c.doctor, "unreviewed", low_confidence_below=CUTOFF
        )
        assert _ids(unreviewed) == [c.red_flag, c.unknown, c.answered]


def test_reviews_are_append_only(factory: Callable[[], Session]) -> None:
    with factory() as session:
        c = Clinic(session)
        review = add_review(
            session, turn_id=c.answered, reviewer_id=c.doctor, verdict="correct", note=None
        )
        row = session.get(TurnReview, review.id)
        assert row is not None
        row.verdict = "unsafe"
        with pytest.raises(AuditLogImmutableError):
            session.flush()
        session.rollback()
    # The database trigger refuses it too, for every role (0003).
    with factory() as session:
        c = Clinic(session)
        review = add_review(
            session, turn_id=c.answered, reviewer_id=c.doctor, verdict="correct", note=None
        )
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(update(TurnReview).where(TurnReview.id == review.id).values(note="x"))


def test_cannot_review_another_practitioners_patient(factory: Callable[[], Session]) -> None:
    with factory() as session:
        c = Clinic(session)
        with pytest.raises(TurnNotReviewableError):
            add_review(
                session, turn_id=c.foreign, reviewer_id=c.doctor, verdict="correct", note=None
            )
        # Practitioner replies, relay acks and unknown ids are not reviewable either.
        for turn_id in (c.practitioner_reply, c.relay, uuid.uuid4()):
            with pytest.raises(TurnNotReviewableError):
                add_review(
                    session, turn_id=turn_id, reviewer_id=c.doctor, verdict="correct", note=None
                )


def test_unassigned_or_inactive_practitioner_sees_nothing(factory: Callable[[], Session]) -> None:
    with factory() as session:
        c = Clinic(session)
        row = session.get(Practitioner, c.doctor)
        assert row is not None
        row.is_active = False
        session.flush()
        assert list_reviewable_turns(session, c.doctor, low_confidence_below=CUTOFF).items == []
        with pytest.raises(TurnNotReviewableError):
            add_review(
                session, turn_id=c.answered, reviewer_id=c.doctor, verdict="correct", note=None
            )


@pytest.mark.parametrize("verdict", ["incorrect", "unsafe", "needs_kb_update"])
@pytest.mark.parametrize("note", [None, "", "   "])
def test_note_required_unless_correct(
    factory: Callable[[], Session], verdict: ReviewVerdict, note: str | None
) -> None:
    with factory() as session:
        c = Clinic(session)
        with pytest.raises(ReviewNoteRequiredError):
            add_review(
                session,
                turn_id=c.answered,
                reviewer_id=c.doctor,
                verdict=verdict,
                note=note,
            )


def test_admin_sees_every_patient(factory: Callable[[], Session]) -> None:
    with factory() as session:
        c = Clinic(session)
        admin = _practitioner(session, role="admin")
        page = list_reviewable_turns(session, admin, limit=100, low_confidence_below=CUTOFF)
        assert {c.foreign, c.answered} <= set(_ids(page))
        review = add_review(
            session, turn_id=c.foreign, reviewer_id=admin, verdict="correct", note=None
        )
        assert review.turn_id == c.foreign


def test_cursor_pages_without_gaps_or_repeats_even_on_ties(
    factory: Callable[[], Session],
) -> None:
    with factory() as session:
        doctor = _practitioner(session)
        patient = _patient(session, practitioner_id=doctor, name="Meena")
        at = db_now(session) + timedelta(days=1)
        # Three turns share a timestamp: the id breaks the tie.
        expected = {
            _bot_turn(session, patient, at),
            _bot_turn(session, patient, at),
            _bot_turn(session, patient, at),
            _bot_turn(session, patient, at - timedelta(seconds=1)),
            _bot_turn(session, patient, at - timedelta(seconds=2)),
        }
        seen: list[uuid.UUID] = []
        cursor: TurnCursor | None = None
        pages = 0
        while True:
            page = list_reviewable_turns(
                session, doctor, limit=2, cursor=cursor, low_confidence_below=CUTOFF
            )
            seen += _ids(page)
            pages += 1
            if page.next_cursor is None:
                break
            # Round-trip through the opaque token the API hands out.
            cursor = TurnCursor.decode(page.next_cursor)
        assert pages == 3
        assert len(seen) == len(set(seen)) == 5 and set(seen) == expected


def test_page_size_is_bounded(factory: Callable[[], Session]) -> None:
    with factory() as session:
        doctor = _practitioner(session)
        for bad in (0, 101):
            with pytest.raises(ValueError, match="limit"):
                list_reviewable_turns(session, doctor, limit=bad, low_confidence_below=CUTOFF)


# --- API over the live database ---------------------------------------------------


def _auth_login(session: Session) -> uuid.UUID:
    uid = uuid.uuid4()
    session.execute(
        text(
            "insert into auth.users (id, aud, role, email) "
            "values (:id, 'authenticated', 'authenticated', :email)"
        ),
        {"id": uid, "email": f"review-test-{uid}@example.invalid"},
    )
    return uid


def test_api_end_to_end_on_the_database(conn: Connection, factory: Callable[[], Session]) -> None:
    from app import main  # noqa: PLC0415
    from app.api.auth import current_auth_user  # noqa: PLC0415

    with factory() as session:
        login = _auth_login(session)
        c = Clinic(session)
        doctor = _practitioner(session, auth_user_id=login)
        session.add(PatientAssignment(practitioner_id=doctor, patient_id=c.patient))
        session.commit()

    app = main.app
    # Like the production factory (expire_on_commit=False), on the rolled-back connection.
    api_factory = sessionmaker(
        bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    app.dependency_overrides[get_session_factory] = lambda: api_factory
    app.dependency_overrides[current_auth_user] = lambda: AuthUser(
        id=login, email="doctor@example.invalid"
    )
    try:
        client = TestClient(app)
        listed = client.get("/api/practitioner/turns", params={"filter": "unanswered"})
        assert listed.status_code == 200
        assert [i["turn_id"] for i in listed.json()["items"]] == [str(c.unknown)]
        ok = client.post(
            f"/api/practitioner/turns/{c.unknown}/review",
            json={"verdict": "needs_kb_update", "note": "Add jackfruit to the diet entry."},
        )
        assert ok.status_code == 201, ok.text
        assert ok.json()["verdict"] == "needs_kb_update"
        refused = client.post(
            f"/api/practitioner/turns/{c.foreign}/review", json={"verdict": "correct"}
        )
        assert refused.status_code == 404
    finally:
        app.dependency_overrides.clear()

    with factory() as session:
        stored = session.scalars(select(TurnReview).where(TurnReview.turn_id == c.unknown)).all()
        assert len(stored) == 1
        assert session.get(ConversationTurn, c.unknown) is not None
