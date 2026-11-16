"""Live tests for app.db.chat_repository and the KB seed against Supabase (R4/R5).

Opt-in: ``RUN_DB_TESTS=1 uv run pytest tests/test_chat_repository.py``. Everything runs
in one outer transaction that is rolled back, so nothing is left behind.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Connection, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import load_settings
from app.db.chat_repository import (
    SqlChatStore,
    handoff_for_practitioner,
    practitioner_handoffs,
    resolve_handoff,
    save_turn,
)
from app.db.models import (
    Advisory,
    Handoff,
    KbEntry,
    PatientAssignment,
    Practitioner,
    Profile,
    Reading,
    User,
)
from app.db.session import make_engine
from app.services.chat import ParsedReading
from scripts.kb_seed import KB_SEED, SEED_PREFIX, seed_kb

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)


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


def _practitioner(session: Session) -> uuid.UUID:
    user = User(id=uuid.uuid4(), role="practitioner")
    session.add(user)
    session.flush()
    practitioner = Practitioner(id=uuid.uuid4(), user_id=user.id, full_name="Dr Test")
    session.add(practitioner)
    session.flush()
    return practitioner.id


def _patient(session: Session, *, practitioner_id: uuid.UUID | None = None) -> uuid.UUID:
    user = User(id=uuid.uuid4(), role="patient")
    session.add(user)
    session.flush()
    session.add(
        Profile(user_id=user.id, full_name="Test Patient", age_years=45, diabetes_status="diabetes")
    )
    if practitioner_id is not None:
        session.add(PatientAssignment(practitioner_id=practitioner_id, patient_id=user.id))
    session.flush()
    return user.id


def test_one_live_handoff_per_patient(factory: Callable[[], Session]) -> None:
    settings = load_settings()
    with factory() as session:
        doctor = _practitioner(session)
        patient = _patient(session, practitioner_id=doctor)
        store = SqlChatStore(session, settings)
        first, created = store.open_handoff(
            patient, trigger="practitioner_request", reason="r", advisory_id=None
        )
        again, created_again = store.open_handoff(
            patient, trigger="red_flag", reason="r", advisory_id=None
        )
        assert created and not created_again
        assert again.id == first.id and again.trigger == "practitioner_request"
        assert first.practitioner_id == doctor  # assigned practitioner is attached
        live = session.scalar(
            select(func.count())
            .select_from(Handoff)
            .where(Handoff.user_id == patient, Handoff.status.in_(("open", "assigned")))
        )
        assert live == 1
        # The partial unique index itself refuses a second live row.
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(Handoff(user_id=patient, trigger="red_flag", status="open"))
            session.flush()
        # Once resolved, a new handoff may open.
        row = session.get(Handoff, first.id)
        assert row is not None
        resolve_handoff(session, row, doctor, note="done", at=datetime.now(UTC))
        assert row.status == "resolved" and row.practitioner_id == doctor
        _, created_after = store.open_handoff(
            patient, trigger="practitioner_request", reason="r", advisory_id=None
        )
        assert created_after


def test_practitioner_sees_only_assigned_patients(factory: Callable[[], Session]) -> None:
    settings = load_settings()
    with factory() as session:
        mine, other = _practitioner(session), _practitioner(session)
        patient = _patient(session, practitioner_id=mine)
        handoff, _ = SqlChatStore(session, settings).open_handoff(
            patient, trigger="practitioner_request", reason="r", advisory_id=None
        )
        assert handoff_for_practitioner(session, handoff.id, mine) is not None
        assert handoff_for_practitioner(session, handoff.id, other) is None
        assert [h.id for h in practitioner_handoffs(session, mine)] == [handoff.id]
        assert practitioner_handoffs(session, other) == []


def test_chat_reading_is_stored_classified_and_queues_an_advisory(
    factory: Callable[[], Session],
) -> None:
    settings = load_settings()
    with factory() as session:
        patient = _patient(session)
        store = SqlChatStore(session, settings)
        outcome = store.record_reading(
            patient,
            ParsedReading(value_mg_dl=Decimal("170"), context="fasting"),
            message_id=f"test-{uuid.uuid4()}",
        )
        reading = session.get(Reading, outcome.reading_id)
        assert reading is not None and reading.source == "web"
        # 170 fasting is above every possible fasting limit (hard max 180 only for
        # complex older adults; this patient is 45): a breach, queued for dispatch.
        assert outcome.state == "breach_high" and outcome.escalated
        assert outcome.advisory_queued
        advisory = session.scalar(select(Advisory).where(Advisory.reading_id == reading.id))
        assert advisory is not None
        assert advisory.delivery_status == "queued" and advisory.batch_run_id is None
        normal = store.record_reading(
            patient, ParsedReading(value_mg_dl=Decimal("100"), context="fasting"), message_id=None
        )
        assert normal.state == "normal" and not normal.advisory_queued


def test_reading_without_diabetes_status_is_saved_but_not_judged(
    factory: Callable[[], Session],
) -> None:
    with factory() as session:
        user = User(id=uuid.uuid4(), role="patient")
        session.add(user)
        session.flush()
        outcome = SqlChatStore(session, load_settings()).record_reading(
            user.id, ParsedReading(value_mg_dl=Decimal("150"), context="random"), message_id=None
        )
        assert outcome.state is None
        assert session.get(Reading, outcome.reading_id) is not None


def test_turn_author_only_on_outbound(factory: Callable[[], Session]) -> None:
    with factory() as session:
        doctor = _practitioner(session)
        patient = _patient(session, practitioner_id=doctor)
        reply = save_turn(
            session,
            user_id=patient,
            direction="outbound",
            raw_text=None,
            transcript=None,
            intent=None,
            response_text="Please recheck.",
            model="practitioner",
            model_version=None,
            latency_ms=None,
            escalated=False,
            author_practitioner_id=doctor,
        )
        assert reply.channel == "web"
        with pytest.raises(IntegrityError), session.begin_nested():
            save_turn(
                session,
                user_id=patient,
                direction="inbound",
                raw_text="hi",
                transcript=None,
                intent="other",
                response_text="hello",
                model="rules",
                model_version="x",
                latency_ms=1,
                escalated=False,
                author_practitioner_id=doctor,
            )


def test_kb_seed_is_approved_versioned_and_idempotent(factory: Callable[[], Session]) -> None:
    settings = load_settings()
    now = datetime.now(UTC)
    with factory() as session:
        doctor = _practitioner(session)
        first = seed_kb(session, approver_id=doctor, now=now)
        second = seed_kb(session, approver_id=doctor, now=now)
        assert second.inserted == 0 and second.kb_version is None
        rows = list(
            session.scalars(
                select(KbEntry).where(
                    KbEntry.status == "approved", KbEntry.entry_key.startswith(SEED_PREFIX)
                )
            )
        )
        assert len(rows) == len(KB_SEED)
        assert all(r.approved_by is not None and r.source_citation for r in rows)
        store = SqlChatStore(session, settings)
        assert store.kb_version().kb_version is not None
        assert len(store.approved_kb_entries()) >= len(KB_SEED)
        if first.inserted:  # a fresh database: this run approved them
            assert all(r.approved_by == doctor for r in rows)


def _auth_login(session: Session) -> tuple[uuid.UUID, str]:
    from sqlalchemy import text  # noqa: PLC0415

    uid = uuid.uuid4()
    email = f"chat-test-{uid}@example.invalid"
    session.execute(
        text(
            "insert into auth.users (id, aud, role, email) "
            "values (:id, 'authenticated', 'authenticated', :email)"
        ),
        {"id": uid, "email": email},
    )
    return uid, email


def test_link_practitioner_moves_a_login_off_a_demo_patient(
    factory: Callable[[], Session],
) -> None:
    from scripts.seed_demo import assign_patients, link_practitioner  # noqa: PLC0415

    with factory() as session:
        login, email = _auth_login(session)
        demo = User(id=uuid.uuid4(), role="patient", is_demo=True, auth_user_id=login)
        session.add(demo)
        session.flush()
        practitioner_id = link_practitioner(session, email)
        assert link_practitioner(session, email) == practitioner_id  # idempotent
        session.refresh(demo)
        assert demo.auth_user_id is None
        practitioner = session.get(Practitioner, practitioner_id)
        assert practitioner is not None
        user = session.get(User, practitioner.user_id)
        assert user is not None
        assert (user.role, user.is_demo, user.auth_user_id) == ("practitioner", False, login)
        patient = _patient(session)
        assign_patients(session, practitioner_id, [patient])
        assign_patients(session, practitioner_id, [patient])  # idempotent
        assert (
            session.scalar(
                select(func.count())
                .select_from(PatientAssignment)
                .where(PatientAssignment.practitioner_id == practitioner_id)
            )
            == 1
        )


def test_link_practitioner_refuses_a_real_patient_login(factory: Callable[[], Session]) -> None:
    from scripts.seed_demo import SeedError, link_practitioner  # noqa: PLC0415

    with factory() as session:
        login, email = _auth_login(session)
        session.add(User(id=uuid.uuid4(), role="patient", is_demo=False, auth_user_id=login))
        session.flush()
        with pytest.raises(SeedError, match="real patient"):
            link_practitioner(session, email)
