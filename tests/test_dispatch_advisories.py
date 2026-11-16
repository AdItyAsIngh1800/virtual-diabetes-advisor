"""Tests for the advisory dispatcher (R3) with mocked TTS, storage, push and Realtime.

The in-memory :class:`FakeStore` mirrors the SQL store's conditional updates, so the
rules (expiry, guard, cooldown, below-54 bypass, at-most-once) are tested everywhere.
The ``live`` tests (``RUN_DB_TESTS=1``) run the same scenarios against Supabase with
:class:`SqlDispatchStore`, inside one rolled-back transaction.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import Connection, create_engine, func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings, load_settings
from app.core.exceptions import AudioStorageError, NotificationDeliveryError, SpeechError
from app.db.advisory_repository import (
    QueuedAdvisory,
    SqlDispatchStore,
    advisory_for_view,
    mark_opened,
)
from app.db.models import Advisory, Profile, Reading, User
from app.db.session import sqlalchemy_url
from app.jobs import dispatch_advisories as job
from app.jobs.dispatch_advisories import (
    DispatchDeps,
    build_notification,
    dispatch_advisories,
    push_ttl_seconds,
)
from app.services.notifications import DeliveryReport, Notification, Recipient, check_recipient
from app.services.realtime import HttpRealtimePublisher, advisory_topic

NOW = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)
SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="srk",
    database_url="postgresql://u:p@localhost:5432/db",
    advisory_max_age_minutes=60,
    advisory_cooldown_hours=24,
)

live = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class Row:
    advisory: QueuedAdvisory
    status: str = "queued"
    claimed_at: datetime | None = None
    sent_at: datetime | None = None
    attempts: int = 0
    error_code: str | None = None
    error: str | None = None
    audio_path: str | None = None
    message_id: str | None = None


@dataclass
class FakeStore:
    rows: dict[uuid.UUID, Row] = field(default_factory=dict)
    lose_claims: bool = False  # simulate an overlapping run winning every claim

    def add(self, advisory: QueuedAdvisory, **row: Any) -> QueuedAdvisory:
        self.rows[advisory.id] = Row(advisory, **row)
        return advisory

    def _open(self, advisory_id: uuid.UUID) -> Row | None:
        row = self.rows[advisory_id]
        return row if row.status == "queued" and row.claimed_at is None else None

    def fail_stale_claims(self, *, claimed_before: datetime, at: datetime) -> int:
        stale = [
            r
            for r in self.rows.values()
            if r.status == "queued" and r.claimed_at and r.claimed_at < claimed_before
        ]
        for r in stale:
            r.status, r.error_code = "failed", "dispatch_interrupted"
        return len(stale)

    def queued(self, user_ids: Collection[uuid.UUID] | None) -> list[QueuedAdvisory]:
        rows = [
            r.advisory
            for r in self.rows.values()
            if r.status == "queued"
            and r.claimed_at is None
            and (user_ids is None or r.advisory.user_id in user_ids)
        ]
        return sorted(rows, key=lambda a: (a.measured_at, a.id))

    def _set(self, advisory_id: uuid.UUID, **values: Any) -> bool:
        row = self._open(advisory_id)
        if row is None:
            return False
        for key, value in values.items():
            setattr(row, key, value)
        return True

    def expire(self, advisory_id: uuid.UUID) -> bool:
        return self._set(advisory_id, status="expired")

    def suppress(self, advisory_id: uuid.UUID) -> bool:
        return self._set(advisory_id, status="suppressed")

    def block(self, advisory_id: uuid.UUID, at: datetime, reason: str) -> bool:
        return self._set(advisory_id, status="failed", error_code="recipient_blocked", error=reason)

    def claim(self, advisory_id: uuid.UUID, at: datetime) -> bool:
        if self.lose_claims:
            return False
        row = self._open(advisory_id)
        if row is None:
            return False
        row.claimed_at, row.attempts = at, row.attempts + 1
        return True

    def last_push_at(
        self, *, user_id: uuid.UUID, limit_kind: str, state: str, exclude_id: uuid.UUID
    ) -> datetime | None:
        times = [
            r.sent_at
            for r in self.rows.values()
            if r.sent_at
            and r.advisory.id != exclude_id
            and (r.advisory.user_id, r.advisory.limit_kind, r.advisory.state)
            == (user_id, limit_kind, state)
        ]
        return max(times, default=None)

    def record_sent(
        self,
        advisory_id: uuid.UUID,
        *,
        at: datetime,
        message_id: str | None,
        audio_path: str | None,
    ) -> None:
        row = self.rows[advisory_id]
        row.status, row.sent_at, row.message_id, row.audio_path = "sent", at, message_id, audio_path

    def record_failed(
        self, advisory_id: uuid.UUID, *, at: datetime, code: str, error: str, audio_path: str | None
    ) -> None:
        row = self.rows[advisory_id]
        row.status, row.error_code, row.error, row.audio_path = "failed", code, error, audio_path


class FakeChannel:
    """Web Push stand-in that runs the real recipient guard."""

    name = "webpush"

    def __init__(
        self, *, devices: int = 1, accept: bool = True, raise_exc: Exception | None = None
    ):
        self.devices, self.accept, self.raise_exc = devices, accept, raise_exc
        self.sent: list[tuple[Recipient, Notification]] = []

    def send(self, recipient: Recipient, notification: Notification) -> DeliveryReport:
        check_recipient(recipient)
        if self.raise_exc:
            raise self.raise_exc
        self.sent.append((recipient, notification))
        report = DeliveryReport(channel="webpush", attempted=self.devices)
        if self.devices and self.accept:
            report.sent = self.devices
            report.message_ids.append(f"https://push.example/m/{uuid.uuid4()}")
        elif self.devices:
            report.failed = self.devices
            report.errors.append("HTTP 503")
        return report


class FakeSynth:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.texts: list[str] = []

    def synthesize(self, text: str, language: str) -> bytes:
        if self.fail:
            raise SpeechError("gTTS failed (gTTSError)")
        self.texts.append(text)
        return b"ID3"


class FakeAudioStore:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.uploads: dict[str, bytes] = {}

    def upload(self, path: str, audio: bytes) -> None:
        if self.fail:
            raise AudioStorageError("storage upload answered HTTP 500")
        self.uploads[path] = audio

    def signed_url(self, path: str, expires_in_s: int) -> str:
        return f"https://signed/{path}?ttl={expires_in_s}"


class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[dict[str, Any]] = []

    def publish(self, *, auth_user_id: uuid.UUID, advisory_id: uuid.UUID, state: str) -> None:
        if self.fail:
            raise RuntimeError("realtime down")
        self.published.append({"auth": auth_user_id, "id": advisory_id, "state": state})


@dataclass
class World:
    store: FakeStore = field(default_factory=FakeStore)
    channel: FakeChannel = field(default_factory=FakeChannel)
    synth: FakeSynth = field(default_factory=FakeSynth)
    audio: FakeAudioStore = field(default_factory=FakeAudioStore)
    publisher: FakePublisher = field(default_factory=FakePublisher)

    def deps(self) -> DispatchDeps:
        return DispatchDeps(self.store, self.channel, self.synth, self.audio, self.publisher)  # type: ignore[arg-type]

    def run(self, clock: datetime = NOW, settings: Settings = SETTINGS) -> job.DispatchSummary:
        return dispatch_advisories(self.deps(), settings=settings, clock=lambda: clock)


PATIENT = uuid.uuid4()
LOGIN = uuid.uuid4()


def advisory(
    *,
    minutes_old: float = 5,
    state: str = "pre_breach_high",
    value: str = "126.0",
    limit_kind: str = "fasting_high",
    threshold: str = "130.0",
    user_id: uuid.UUID = PATIENT,
    auth_user_id: uuid.UUID | None = LOGIN,
    is_demo: bool = True,
) -> QueuedAdvisory:
    return QueuedAdvisory(
        id=uuid.uuid4(),
        user_id=user_id,
        auth_user_id=auth_user_id,
        is_demo=is_demo,
        limit_kind=limit_kind,
        state=state,
        reading_value_mg_dl=Decimal(value),
        threshold_mg_dl=Decimal(threshold),
        language="en",
        message_text=f"Your fasting glucose is {value} mg/dL, running high.",
        context="fasting",
        measured_at=NOW - timedelta(minutes=minutes_old),
    )


@pytest.fixture
def world() -> World:
    return World()


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def test_fresh_advisory_gets_audio_push_and_realtime(world: World) -> None:
    a = world.store.add(advisory())
    summary = world.run()
    assert (summary.sent, summary.sent_ids) == (1, [a.id])
    row = world.store.rows[a.id]
    assert row.status == "sent" and row.sent_at == NOW and row.attempts == 1
    assert row.message_id is not None and row.message_id.startswith("https://push.example/")
    # Voice: units dropped for speech, stored under the patient's folder.
    assert world.synth.texts == ["Your fasting glucose is 126.0, running high."]
    assert row.audio_path == f"{PATIENT}/{a.id}.mp3"
    assert world.audio.uploads == {row.audio_path: b"ID3"}
    # Push: opens the advisory page; reading and limit only.
    ((_, note),) = world.channel.sent
    assert note.url == f"/app/advisory/{a.id}"
    assert note.advisory_id == a.id
    assert note.urgency == "normal"
    assert note.title == "Glucose close to your upper limit"
    assert "126 mg/dL" in note.body and "130 mg/dL" in note.body
    assert world.publisher.published == [{"auth": LOGIN, "id": a.id, "state": "pre_breach_high"}]


def test_reading_older_than_max_age_expires_and_is_never_sent(world: World) -> None:
    old = world.store.add(advisory(minutes_old=61))
    edge = world.store.add(advisory(minutes_old=60, state="breach_high", value="135"))
    summary = world.run()
    assert world.store.rows[old.id].status == "expired"
    assert world.store.rows[edge.id].status == "sent"  # exactly at the limit is fresh
    assert (summary.expired, summary.sent) == (1, 1)
    assert [n.advisory_id for _, n in world.channel.sent] == [edge.id]
    assert world.synth.texts and len(world.synth.texts) == 1


def test_cooldown_suppresses_same_limit_and_state(world: World) -> None:
    first = world.store.add(advisory(minutes_old=10))
    world.run()
    second = world.store.add(advisory(minutes_old=2))
    summary = world.run(clock=NOW + timedelta(hours=23))
    # 23 h later the second one is stale anyway; use a fresh reading at that time.
    assert summary.expired == 1
    third = world.store.add(advisory(minutes_old=-23 * 60 + 1))
    summary = world.run(clock=NOW + timedelta(hours=23))
    assert world.store.rows[third.id].status == "suppressed"
    assert summary.suppressed == 1
    assert [n.advisory_id for _, n in world.channel.sent] == [first.id]
    assert world.store.rows[second.id].status == "expired"


def test_cooldown_ends_after_n_hours_and_is_per_state(world: World) -> None:
    world.store.add(advisory())
    world.run()
    other_state = world.store.add(advisory(state="breach_high", value="133"))
    world.run()
    assert world.store.rows[other_state.id].status == "sent"
    # Reading taken just before NOW + 24 h; dispatched exactly 24 h after the first push.
    after = world.store.add(advisory(minutes_old=-24 * 60 + 1))
    world.run(clock=NOW + timedelta(hours=24))
    assert world.store.rows[after.id].status == "sent"


def test_below_54_bypasses_the_cooldown(world: World) -> None:
    for minutes in (30, 20, 10):
        world.store.add(
            advisory(
                minutes_old=minutes,
                state="breach_low",
                value="49.0",
                limit_kind="low",
                threshold="70.0",
            )
        )
    summary = world.run()
    assert summary.sent == 3 and summary.suppressed == 0
    assert {n.urgency for _, n in world.channel.sent} == {"high"}
    assert {n.title for _, n in world.channel.sent} == {"Very low glucose reading"}


def test_ordinary_low_is_still_cooled_down(world: World) -> None:
    world.store.add(advisory(minutes_old=30, state="breach_low", value="65", limit_kind="low"))
    world.store.add(advisory(minutes_old=10, state="breach_low", value="66", limit_kind="low"))
    summary = world.run()
    assert (summary.sent, summary.suppressed) == (1, 1)


def test_breach_gets_high_urgency_pre_breach_normal(world: World) -> None:
    world.store.add(advisory(state="breach_high", value="140"))
    world.store.add(advisory(state="pre_breach_low", value="72", limit_kind="low", threshold="70"))
    world.run()
    urgencies = {n.title: n.urgency for _, n in world.channel.sent}
    assert urgencies == {
        "High glucose reading": "high",
        "Glucose close to your lower limit": "normal",
    }


def test_guard_blocks_unlinked_demo_patient_before_any_work(world: World) -> None:
    blocked = world.store.add(advisory(auth_user_id=None, is_demo=True))
    real = world.store.add(advisory(auth_user_id=None, is_demo=False, user_id=uuid.uuid4()))
    summary = world.run()
    row = world.store.rows[blocked.id]
    assert (row.status, row.error_code) == ("failed", "recipient_blocked")
    assert row.attempts == 0  # never claimed
    assert summary.blocked == 1
    # Nothing was synthesised, stored, pushed or broadcast for the blocked one.
    assert [n.advisory_id for _, n in world.channel.sent] == [real.id]
    assert len(world.synth.texts) == 1
    assert world.publisher.published == []  # the real patient has no login either


def test_dispatch_run_twice_sends_once(world: World) -> None:
    a = world.store.add(advisory())
    first = world.run()
    second = world.run()
    assert (first.sent, second.sent, second.considered) == (1, 0, 0)
    assert len(world.channel.sent) == 1
    assert world.store.rows[a.id].attempts == 1


def test_overlapping_run_that_loses_the_claim_sends_nothing(world: World) -> None:
    world.store.add(advisory())
    world.store.lose_claims = True
    summary = world.run()
    assert (summary.skipped, summary.sent) == (1, 0)
    assert world.channel.sent == [] and world.synth.texts == []


def test_stale_claim_is_failed_not_resent(world: World) -> None:
    a = world.store.add(advisory(), claimed_at=NOW - timedelta(minutes=11), attempts=1)
    fresh_claim = world.store.add(advisory(), claimed_at=NOW - timedelta(minutes=1), attempts=1)
    summary = world.run()
    assert summary.stale_claims_failed == 1
    assert world.store.rows[a.id].error_code == "dispatch_interrupted"
    assert world.store.rows[fresh_claim.id].status == "queued"  # still being sent elsewhere
    assert world.channel.sent == []


def test_tts_or_storage_failure_still_sends_without_audio() -> None:
    for w in (World(synth=FakeSynth(fail=True)), World(audio=FakeAudioStore(fail=True))):
        a = w.store.add(advisory())
        summary = w.run()
        assert summary.sent == 1 and summary.audio_missing == 1
        assert w.store.rows[a.id].audio_path is None


@pytest.mark.parametrize(
    ("channel", "code", "error"),
    [
        (FakeChannel(devices=0), "no_subscription", "not enabled alerts"),
        (FakeChannel(accept=False), "push_failed", "HTTP 503"),
        (
            FakeChannel(raise_exc=NotificationDeliveryError("signing failed")),
            "push_error",
            "signing",
        ),
        (FakeChannel(raise_exc=RuntimeError("bug")), "internal_error", "RuntimeError"),
    ],
)
def test_failed_push_is_recorded_with_the_error(
    channel: FakeChannel, code: str, error: str
) -> None:
    w = World(channel=channel)
    a = w.store.add(advisory())
    summary = w.run()
    row = w.store.rows[a.id]
    assert (row.status, row.error_code, summary.failed) == ("failed", code, 1)
    assert row.error is not None and error in row.error
    assert row.audio_path is not None  # the page can still play it
    # The open app still hears about it over Realtime.
    assert [p["id"] for p in w.publisher.published] == [a.id]


def test_realtime_failure_does_not_fail_the_advisory() -> None:
    w = World(publisher=FakePublisher(fail=True))
    a = w.store.add(advisory())
    assert w.run().sent == 1
    assert w.store.rows[a.id].status == "sent"


def test_user_filter(world: World) -> None:
    mine = world.store.add(advisory())
    other = world.store.add(advisory(user_id=uuid.uuid4()))
    summary = dispatch_advisories(
        world.deps(), settings=SETTINGS, clock=lambda: NOW, user_ids=[PATIENT]
    )
    assert summary.sent_ids == [mine.id]
    assert world.store.rows[other.id].status == "queued"


def test_push_ttl_is_the_time_left_until_expiry() -> None:
    assert push_ttl_seconds(NOW - timedelta(minutes=10), NOW, SETTINGS) == 50 * 60
    assert push_ttl_seconds(NOW - timedelta(minutes=59, seconds=50), NOW, SETTINGS) == 60
    assert push_ttl_seconds(NOW + timedelta(minutes=5), NOW, SETTINGS) == 3600
    note = build_notification(advisory(minutes_old=30), ttl_seconds=1800)
    assert note.ttl_seconds == 1800


def test_run_dispatch_needs_vapid_and_skips_when_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.exceptions import ConfigurationError  # noqa: PLC0415

    # Explicit: module-level SETTINGS is built at import time, before conftest clears
    # exported shell variables, so a shell that exports .env would supply real keys.
    no_vapid = SETTINGS.model_copy(
        update={"vapid_public_key": None, "vapid_private_key": None, "vapid_subject": None}
    )
    with pytest.raises(ConfigurationError, match="VAPID"):
        job.run_dispatch(settings=no_vapid, session_factory=lambda: None)  # type: ignore[arg-type,return-value]
    job._RUN_LOCK.acquire()
    try:
        assert job.run_dispatch(settings=SETTINGS, session_factory=lambda: None) is None  # type: ignore[arg-type,return-value]
    finally:
        job._RUN_LOCK.release()


def test_scheduled_dispatch_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.exceptions import ConfigurationError  # noqa: PLC0415

    def unconfigured(**_k: Any) -> None:
        raise ConfigurationError("VAPID_PUBLIC_KEY required")

    def broken(**_k: Any) -> None:
        raise RuntimeError("db down")

    for fake in (unconfigured, broken):
        monkeypatch.setattr(job, "run_dispatch", fake)
        job.scheduled_dispatch()


def _publisher(handler: Callable[[httpx.Request], httpx.Response]) -> HttpRealtimePublisher:
    return HttpRealtimePublisher(
        SETTINGS, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_realtime_publisher_sends_private_minimal_broadcast() -> None:
    requests: list[httpx.Request] = []

    def accept(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202)

    a_id = uuid.uuid4()
    _publisher(accept).publish(auth_user_id=LOGIN, advisory_id=a_id, state="breach_high")
    (request,) = requests
    assert str(request.url) == "https://abc.supabase.co/realtime/v1/api/broadcast"
    assert request.headers["apikey"] == "srk"
    (message,) = json.loads(request.content)["messages"]
    assert message == {
        "topic": f"advisories:{LOGIN}",
        "event": "advisory",
        "payload": {
            "advisory_id": str(a_id),
            "state": "breach_high",
            "url": f"/app/advisory/{a_id}",
        },
        "private": True,
    }
    assert advisory_topic(LOGIN) == message["topic"]


def test_realtime_publisher_raises_on_failure() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    for handler, match in ((refuse, "HTTP 401"), (down, "ConnectError")):
        with pytest.raises(NotificationDeliveryError, match=match):
            _publisher(handler).publish(
                auth_user_id=LOGIN, advisory_id=uuid.uuid4(), state="breach_low"
            )


# ---------------------------------------------------------------------------
# Live database (opt-in)
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[Connection]:
    url = sqlalchemy_url(load_settings().database_url.get_secret_value())
    engine = create_engine(url, connect_args={"prepare_threshold": None})
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


def db_clock(conn: Connection) -> datetime:
    moment = conn.scalar(select(func.clock_timestamp()))
    assert isinstance(moment, datetime)
    return moment


def add_advisory(
    factory: Callable[[], Session],
    user_id: uuid.UUID,
    measured_at: datetime,
    *,
    state: str = "pre_breach_high",
    value: str = "126.0",
    limit_kind: str = "fasting_high",
) -> uuid.UUID:
    with factory() as session:
        reading = Reading(
            user_id=user_id,
            context="fasting",
            value_mg_dl=Decimal(value),
            measured_at=measured_at,
            source="api",
        )
        threshold_id = session.execute(
            text(
                "insert into public.thresholds_history (user_id, limit_kind, direction,"
                " diabetes_status, base_mg_dl, unclamped_mg_dl, hard_min_mg_dl,"
                " hard_max_mg_dl, threshold_mg_dl, model_version, reason, source_citation)"
                " values (:u, :k, :d, 'diabetes', :v, :v, 54, 250, :v, 'test', 'initial', 't')"
                " returning id"
            ),
            {
                "u": user_id,
                "k": limit_kind,
                "d": "upper" if limit_kind == "fasting_high" else "lower",
                "v": 130 if limit_kind == "fasting_high" else 70,
            },
        ).scalar_one()
        session.add(reading)
        session.flush()
        row = Advisory(
            user_id=user_id,
            reading_id=reading.id,
            threshold_id=threshold_id,
            limit_kind=limit_kind,
            state=state,
            reading_value_mg_dl=Decimal(value),
            threshold_mg_dl=Decimal(130 if limit_kind == "fasting_high" else 70),
            language="en",
            message_text=f"Your fasting glucose is {value} mg/dL.",
            escalated=state.startswith("breach_"),
        )
        session.add(row)
        session.commit()
        return row.id


def add_user(
    conn: Connection, factory: Callable[[], Session], *, linked: bool = True
) -> tuple[uuid.UUID, uuid.UUID | None]:

    login: uuid.UUID | None = None
    if linked:
        login = uuid.uuid4()
        conn.execute(
            text("insert into auth.users (id, email) values (:id, :email)"),
            {"id": login, "email": f"dispatch-{login}@example.invalid"},
        )
    with factory() as session:
        user = User(id=uuid.uuid4(), role="patient", is_demo=True, auth_user_id=login)
        session.add(user)
        session.flush()
        session.add(Profile(user_id=user.id, age_years=58, sex=0, diabetes_status="diabetes"))
        session.commit()
        return user.id, login


def status_of(factory: Callable[[], Session], advisory_id: uuid.UUID) -> Advisory:
    with factory() as session:
        row = session.get(Advisory, advisory_id)
        assert row is not None
        session.expunge(row)
        return row


@live
def test_live_dispatch_twice_pushes_once_and_respects_rules(
    conn: Connection, factory: Callable[[], Session]
) -> None:
    now = db_clock(conn)
    user, _login = add_user(conn, factory)
    blocked_user, _ = add_user(conn, factory, linked=False)
    fresh = add_advisory(factory, user, now - timedelta(minutes=5))
    stale = add_advisory(
        factory, user, now - timedelta(minutes=90), state="breach_high", value="140"
    )
    hypo1 = add_advisory(
        factory, user, now - timedelta(minutes=20), state="breach_low", value="50", limit_kind="low"
    )
    hypo2 = add_advisory(
        factory, user, now - timedelta(minutes=10), state="breach_low", value="49", limit_kind="low"
    )
    blocked = add_advisory(factory, blocked_user, now - timedelta(minutes=5))

    w = World(store=SqlDispatchStore(factory))  # type: ignore[arg-type]
    users = [user, blocked_user]
    first = dispatch_advisories(w.deps(), settings=SETTINGS, clock=lambda: now, user_ids=users)
    second = dispatch_advisories(w.deps(), settings=SETTINGS, clock=lambda: now, user_ids=users)

    assert (first.sent, first.expired, first.blocked) == (3, 1, 1)
    assert (second.considered, second.sent) == (0, 0)
    assert len(w.channel.sent) == 3
    sent = status_of(factory, fresh)
    assert (sent.delivery_status, sent.attempt_count) == ("sent", 1)
    assert sent.sent_at is not None and sent.dispatch_claimed_at is not None
    assert sent.audio_path == f"{user}/{fresh}.mp3"
    assert sent.message_id is not None
    assert status_of(factory, stale).delivery_status == "expired"
    # Both Level 2 hypos went out despite sharing limit and state (cooldown bypass).
    assert {status_of(factory, h).delivery_status for h in (hypo1, hypo2)} == {"sent"}
    row = status_of(factory, blocked)
    assert (row.delivery_status, row.delivery_error_code) == ("failed", "recipient_blocked")
    assert row.failed_at is not None and row.attempt_count == 0


@live
def test_live_cooldown_suppresses_after_a_push(
    conn: Connection, factory: Callable[[], Session]
) -> None:
    now = db_clock(conn)
    user, _ = add_user(conn, factory)
    first = add_advisory(factory, user, now - timedelta(minutes=30))
    w = World(store=SqlDispatchStore(factory))  # type: ignore[arg-type]
    dispatch_advisories(w.deps(), settings=SETTINGS, clock=lambda: now, user_ids=[user])
    second = add_advisory(factory, user, now - timedelta(minutes=1))
    summary = dispatch_advisories(
        w.deps(), settings=SETTINGS, clock=lambda: now + timedelta(minutes=1), user_ids=[user]
    )
    assert summary.suppressed == 1
    assert status_of(factory, first).delivery_status == "sent"
    assert status_of(factory, second).delivery_status == "suppressed"


@live
def test_live_failed_push_records_error_and_stale_claim(
    conn: Connection, factory: Callable[[], Session]
) -> None:
    now = db_clock(conn)
    user, _ = add_user(conn, factory)
    a = add_advisory(factory, user, now - timedelta(minutes=5))
    w = World(store=SqlDispatchStore(factory), channel=FakeChannel(devices=0))  # type: ignore[arg-type]
    dispatch_advisories(w.deps(), settings=SETTINGS, clock=lambda: now, user_ids=[user])
    row = status_of(factory, a)
    assert (row.delivery_status, row.delivery_error_code) == ("failed", "no_subscription")
    assert row.failed_at is not None

    stuck = add_advisory(
        factory, user, now - timedelta(minutes=3), state="breach_high", value="140"
    )
    with factory() as session:
        session.get(Advisory, stuck).dispatch_claimed_at = now - timedelta(minutes=30)  # type: ignore[union-attr]
        session.commit()
    summary = dispatch_advisories(w.deps(), settings=SETTINGS, clock=lambda: now, user_ids=[user])
    assert summary.stale_claims_failed == 1
    assert status_of(factory, stuck).delivery_error_code == "dispatch_interrupted"


@live
def test_live_opening_sets_opened_at_once(conn: Connection, factory: Callable[[], Session]) -> None:
    now = db_clock(conn)
    user, _ = add_user(conn, factory)
    a = add_advisory(factory, user, now - timedelta(minutes=5))
    w = World(store=SqlDispatchStore(factory))  # type: ignore[arg-type]
    dispatch_advisories(w.deps(), settings=SETTINGS, clock=lambda: now, user_ids=[user])
    with factory() as session:
        first = mark_opened(session, a, now + timedelta(minutes=1))
        again = mark_opened(session, a, now + timedelta(minutes=9))
        session.commit()
        view = advisory_for_view(session, a)
    assert first == again == now + timedelta(minutes=1)
    assert view is not None
    assert (view.delivery_status, view.context, view.user_id) == ("opened", "fasting", user)
    assert view.audio_path == f"{user}/{a}.mp3"
    with factory() as session:
        assert advisory_for_view(session, uuid.uuid4()) is None
