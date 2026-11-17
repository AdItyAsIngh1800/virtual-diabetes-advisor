"""Tests for the Audit tab summary (R5): ``answer_summary``, ``kb_status_counts`` and the route.

The repository tests are live (``RUN_DB_TESTS=1``) inside one rolled-back transaction.
Test turns are dated two days ahead of the database clock and the window is narrowed to
them, so real rows never enter the counts (even for the admin, who sees everyone).
The route tests at the end use fakes and always run.
"""

from __future__ import annotations

import os
import uuid
import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Connection, func, update
from sqlalchemy.orm import Session

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from app import main
from app.api import audit, chat
from app.api.auth import current_auth_user
from app.core.config import Settings, get_settings, load_settings
from app.core.rates import CountRate
from app.db.audit_repository import AnswerSummary, _RawCounts, add_review, answer_summary
from app.db.chat_repository import db_now, save_turn
from app.db.kb_repository import (
    SEED_PRACTITIONER_NAME,
    KbStatusCounts,
    kb_status_counts,
)
from app.db.models import KbEntry, Practitioner, TurnIntent
from app.db.session import get_session_factory, make_engine
from app.services.chat import ADVICE_GUARD_REPLY, RELAY_MODEL, RULES_MODEL, TFIDF_MODEL
from app.services.supabase_auth import AuthUser
from tests.test_audit_repository import _patient, _practitioner

live = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)


# --- CountRate ---------------------------------------------------------------------------


def test_count_rate_never_divides_by_zero() -> None:
    assert CountRate.of(0, 0).model_dump() == {"count": 0, "denominator": 0, "rate": None}
    assert CountRate.of(1, 4).rate == 0.25


# --- repository (live) -------------------------------------------------------------------


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


def _turn(
    session: Session,
    patient: uuid.UUID,
    at: datetime,
    *,
    reply: str = "A1C is your 3-month average.",
    intent: TurnIntent = "question",
    model: str = TFIDF_MODEL,
    kb_ids: list[uuid.UUID] | None = None,
    kb_version: int | None = None,
    escalated: bool = False,
) -> uuid.UUID:
    return save_turn(
        session,
        user_id=patient,
        direction="inbound",
        raw_text="question",
        transcript=None,
        intent=intent,
        response_text=reply,
        model=model,
        model_version="v",
        latency_ms=5,
        escalated=escalated,
        kb_ids_used=kb_ids,
        kb_version=kb_version,
        retrieval_confidence=0.4 if kb_ids else 0.05,
        created_at=at,
    ).id


class Clinic:
    """Two practitioners and an admin; a mix of turns in a future window."""

    def __init__(self, session: Session) -> None:
        self.start = db_now(session) + timedelta(days=2)
        self.end = self.start + timedelta(hours=1)
        self.doctor = _practitioner(session)
        self.other_doctor = _practitioner(session)
        self.admin = _practitioner(session, role="admin")
        self.patient = _patient(session, practitioner_id=self.doctor, name="Asha")
        self.other_patient = _patient(session, practitioner_id=self.other_doctor, name="Ravi")
        kb = [uuid.uuid4()]  # kb_ids_used is not a foreign key; any id marks "answered"
        t = self.start
        s = timedelta(seconds=1)
        # Doctor's patient: 6 advisor replies.
        self.v1_a = _turn(session, self.patient, t, kb_ids=kb, kb_version=1)
        self.v1_b = _turn(session, self.patient, t + s, kb_ids=kb, kb_version=1)
        self.v2_a = _turn(session, self.patient, t + 2 * s, kb_ids=kb, kb_version=2)
        self.unknown = _turn(session, self.patient, t + 3 * s, reply="I don't know", kb_version=2)
        self.guard = _turn(
            session, self.patient, t + 4 * s, reply=ADVICE_GUARD_REPLY, model=RULES_MODEL
        )
        self.red_flag = _turn(
            session,
            self.patient,
            t + 5 * s,
            reply="Call 112 now.",
            intent="symptom_red_flag",
            model=RULES_MODEL,
            escalated=True,
        )
        # Not advisor replies: a relay acknowledgement (excluded like the review list).
        _turn(session, self.patient, t + 6 * s, reply="Sent.", model=RELAY_MODEL)
        # Outside the window.
        _turn(session, self.patient, self.end, kb_ids=kb, kb_version=1)
        _turn(session, self.patient, t - s, kb_ids=kb, kb_version=1)
        # The other doctor's patient: 2 replies, one reviewed unsafe.
        self.foreign = _turn(session, self.other_patient, t, kb_ids=kb, kb_version=1)
        _turn(session, self.other_patient, t + s, reply="I don't know", kb_version=1)
        add_review(
            session, turn_id=self.foreign, reviewer_id=self.other_doctor, verdict="unsafe", note="x"
        )

    def summary(self, session: Session, who: uuid.UUID) -> AnswerSummary:
        return answer_summary(session, who, since=self.start, until=self.end)


def _rate(cr: CountRate) -> tuple[int, int, object]:
    return cr.count, cr.denominator, cr.rate


@live
def test_counts_and_rates_with_the_latest_review_winning(factory: Callable[[], Session]) -> None:
    with factory() as session:
        c = Clinic(session)
        # v1_a: incorrect, then corrected to correct -> current verdict correct.
        add_review(session, turn_id=c.v1_a, reviewer_id=c.doctor, verdict="incorrect", note="no")
        add_review(session, turn_id=c.v1_a, reviewer_id=c.doctor, verdict="correct", note=None)
        # v2_a: correct, then needs_kb_update -> current verdict needs_kb_update.
        add_review(session, turn_id=c.v2_a, reviewer_id=c.doctor, verdict="correct", note=None)
        add_review(
            session, turn_id=c.v2_a, reviewer_id=c.doctor, verdict="needs_kb_update", note="add"
        )
        add_review(session, turn_id=c.guard, reviewer_id=c.doctor, verdict="unsafe", note="hm")

        t = c.summary(session, c.doctor).totals
        assert t.total == 6
        assert _rate(t.reviewed) == (3, 6, 0.5)
        assert _rate(t.unreviewed) == (3, 6, 0.5)
        # Verdict rates are out of the reviewed answers; each turn counted once.
        assert _rate(t.verdicts.correct) == (1, 3, pytest.approx(1 / 3))
        assert _rate(t.verdicts.incorrect) == (0, 3, 0.0)
        assert _rate(t.verdicts.needs_kb_update) == (1, 3, pytest.approx(1 / 3))
        assert _rate(t.verdicts.unsafe) == (1, 3, pytest.approx(1 / 3))
        assert _rate(t.escalated) == (1, 6, pytest.approx(1 / 6))
        assert _rate(t.unanswered) == (1, 6, pytest.approx(1 / 6))
        assert _rate(t.blocked_by_guard) == (1, 6, pytest.approx(1 / 6))


@live
def test_grouped_by_kb_version(factory: Callable[[], Session]) -> None:
    with factory() as session:
        c = Clinic(session)
        add_review(session, turn_id=c.v1_b, reviewer_id=c.doctor, verdict="incorrect", note="no")
        groups = {g.kb_version: g.counts for g in c.summary(session, c.doctor).by_kb_version}
        assert list(groups) == [1, 2, None]  # oldest first, "no KB" last
        assert groups[1].total == 2 and _rate(groups[1].verdicts.incorrect) == (1, 1, 1.0)
        assert groups[2].total == 2 and _rate(groups[2].unanswered) == (1, 2, 0.5)
        assert groups[None].total == 2
        assert _rate(groups[None].blocked_by_guard) == (1, 2, 0.5)
        # Unreviewed version: the verdict denominator is 0, so the rate is null.
        assert _rate(groups[2].verdicts.correct) == (0, 0, None)


@live
def test_practitioner_cannot_count_another_practitioners_patients(
    factory: Callable[[], Session],
) -> None:
    with factory() as session:
        c = Clinic(session)
        mine = c.summary(session, c.doctor).totals
        theirs = c.summary(session, c.other_doctor).totals
        assert mine.total == 6 and mine.verdicts.unsafe.count == 0
        assert theirs.total == 2 and theirs.verdicts.unsafe.count == 1
        # An inactive practitioner counts nothing, like the review list.
        session.execute(
            update(Practitioner).where(Practitioner.id == c.doctor).values(is_active=False)
        )
        assert c.summary(session, c.doctor).totals.total == 0


@live
def test_admin_counts_every_patient(factory: Callable[[], Session]) -> None:
    with factory() as session:
        c = Clinic(session)
        t = c.summary(session, c.admin).totals
        assert t.total == 8
        assert t.unanswered.count == 2
        assert t.verdicts.unsafe.count == 1


@live
def test_empty_window_gives_null_rates(factory: Callable[[], Session]) -> None:
    with factory() as session:
        c = Clinic(session)
        far = c.end + timedelta(days=30)
        result = answer_summary(session, c.doctor, since=far, until=far + timedelta(hours=1))
        assert result.by_kb_version == []
        t = result.totals
        assert t.total == 0
        for cr in (t.reviewed, t.unreviewed, t.escalated, t.unanswered, t.blocked_by_guard):
            assert _rate(cr) == (0, 0, None)
        assert _rate(t.verdicts.correct) == (0, 0, None)
        with pytest.raises(ValueError, match="before"):
            answer_summary(session, c.doctor, since=far, until=far)


def _approved(session: Session, approver: uuid.UUID) -> None:
    version = (session.scalar(func.max(KbEntry.kb_version).select()) or 0) + 1
    session.add(
        KbEntry(
            id=uuid.uuid4(),
            entry_key=f"audit-test-{uuid.uuid4()}",
            title="t",
            body="b",
            status="approved",
            kb_version=version,
            source_citation="ADA",
            approved_by=approver,
            approved_at=db_now(session),
        )
    )
    session.flush()


@live
def test_kb_status_separates_seed_account_approvals(factory: Callable[[], Session]) -> None:
    with factory() as session:
        before = kb_status_counts(session)
        seed = _practitioner(session)
        session.execute(
            update(Practitioner)
            .where(Practitioner.id == seed)
            .values(full_name=SEED_PRACTITIONER_NAME)
        )
        reviewer = _practitioner(session)
        _approved(session, seed)
        _approved(session, seed)
        _approved(session, reviewer)
        session.add(
            KbEntry(
                id=uuid.uuid4(),
                entry_key=f"audit-test-{uuid.uuid4()}",
                title="q",
                body="",
                priority=True,
            )
        )
        session.flush()
        after = kb_status_counts(session)
        assert after.approved == before.approved + 3
        assert after.drafts == before.drafts + 1
        assert after.priority_drafts.count == before.priority_drafts.count + 1
        assert after.approved_by_seed_account.count == before.approved_by_seed_account.count + 2
        assert after.approved_by_others.count == before.approved_by_others.count + 1
        assert after.approved_by_seed_account.denominator == after.approved
        assert (
            after.approved_by_seed_account.count + after.approved_by_others.count == after.approved
        )


# --- route (fakes) -----------------------------------------------------------------------

SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="srk",
    database_url="postgresql://u:p@localhost:5432/db",
)
DOCTOR_LOGIN = AuthUser(id=uuid.uuid4(), email="doctor@example.com")
PATIENT_LOGIN = AuthUser(id=uuid.uuid4(), email="patient@example.com")
DOCTOR = Practitioner(id=uuid.uuid4(), user_id=uuid.uuid4(), full_name="Dr Demo")
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
URL = "/api/practitioner/audit/summary"
EMPTY_KB = KbStatusCounts(
    approved=0,
    drafts=0,
    priority_drafts=CountRate.of(0, 0),
    approved_by_seed_account=CountRate.of(0, 0),
    approved_by_others=CountRate.of(0, 0),
)


class FakeSession:
    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[uuid.UUID, datetime, datetime]]]:
    seen: list[tuple[uuid.UUID, datetime, datetime]] = []

    def fake(_s: object, who: uuid.UUID, *, since: datetime, until: datetime) -> AnswerSummary:
        seen.append((who, since, until))
        if since >= until:  # the repository's rule, which the route turns into 422
            raise ValueError("since must be before until")
        return AnswerSummary(totals=_RawCounts().with_rates(), by_kb_version=[])

    monkeypatch.setattr(
        chat,
        "find_practitioner_by_auth_user",
        lambda _s, auth_id: DOCTOR if auth_id == DOCTOR_LOGIN.id else None,
    )
    monkeypatch.setattr(audit, "answer_summary", fake)
    monkeypatch.setattr(audit, "kb_status_counts", lambda _s: EMPTY_KB)
    monkeypatch.setattr(audit, "db_now", lambda _s: NOW)
    app = main.app
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_session_factory] = lambda: FakeSession
    app.dependency_overrides[current_auth_user] = lambda: DOCTOR_LOGIN
    yield seen
    app.dependency_overrides.clear()


def test_default_window_is_the_last_30_days(
    calls: list[tuple[uuid.UUID, datetime, datetime]],
) -> None:
    response = TestClient(main.app).get(URL)
    assert response.status_code == 200, response.text
    assert calls == [(DOCTOR.id, NOW - timedelta(days=30), NOW)]
    body = response.json()
    assert body["totals"]["total"] == 0
    # Empty data: null rates, never a division error, and never a rate without counts.
    assert body["totals"]["reviewed"] == {"count": 0, "denominator": 0, "rate": None}
    assert body["kb_status"]["approved_by_seed_account"] == {
        "count": 0,
        "denominator": 0,
        "rate": None,
    }


def test_explicit_window(calls: list[tuple[uuid.UUID, datetime, datetime]]) -> None:
    response = TestClient(main.app).get(
        URL, params={"since": "2026-11-01T00:00:00+05:30", "until": "2026-11-10T00:00:00Z"}
    )
    assert response.status_code == 200, response.text
    _, since, until = calls[0]
    assert since == datetime(2026, 10, 31, 18, 30, tzinfo=UTC)
    assert until == datetime(2026, 11, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    "params",
    [
        {"since": "2026-11-10T00:00:00Z", "until": "2026-11-01T00:00:00Z"},  # reversed
        {"since": "2026-11-01T00:00:00"},  # no timezone
        {"since": "yesterday"},
    ],
)
def test_bad_window_is_422(
    calls: list[tuple[uuid.UUID, datetime, datetime]], params: dict[str, str]
) -> None:
    assert TestClient(main.app).get(URL, params=params).status_code == 422


def test_summary_is_practitioner_only(calls: list[tuple[uuid.UUID, datetime, datetime]]) -> None:
    main.app.dependency_overrides[current_auth_user] = lambda: PATIENT_LOGIN
    assert TestClient(main.app).get(URL).status_code == 403
    assert calls == []
