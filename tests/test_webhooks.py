"""Tests for the WhatsApp webhook: handshake, signature, parsing and processing (R3/R4).

Most tests swap the repository functions for an in-memory fake (``FakeStore``), so they
run without a database. The ``live`` tests (``RUN_DB_TESTS=1``) run the same flow
against Supabase inside one outer transaction that is rolled back at the end.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
import warnings
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import Connection, create_engine, select
from sqlalchemy.orm import Session

with warnings.catch_warnings():
    # Starlette's TestClient warns that httpx is deprecated in favour of httpx2.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from app import main
from app.api import webhooks
from app.core.config import Settings, get_settings, load_settings
from app.core.exceptions import ConfigurationError
from app.db.models import (
    Advisory,
    ConversationTurn,
    DeliveryStatus,
    Profile,
    Reading,
    ThresholdHistory,
    User,
)
from app.db.session import get_session_factory, sqlalchemy_url
from app.services import whatsapp_webhook
from app.services.whatsapp_webhook import (
    InboundMessage,
    StatusUpdate,
    WebhookPayload,
    WebhookResult,
    apply_status,
    message_turn,
    process_webhook,
    verify_signature,
)

SECRET = "app-secret-for-tests"
VERIFY = "verify-token-for-tests"
SENDER_WA_ID = "919876543210"
SENDER = "+" + SENDER_WA_ID
T0 = 1_763_200_000  # 2025-11-15T09:46:40Z, a fixed Unix time for payloads
REQUIRED: dict[str, Any] = {
    "supabase_url": "https://abc.supabase.co",
    "supabase_service_role_key": "srk",
    "database_url": "postgresql://u:p@localhost:5432/db",
    "whatsapp_access_token": "t",
    "whatsapp_phone_number_id": "123",
    "whatsapp_verify_token": VERIFY,
    "whatsapp_app_secret": SECRET,
    "graph_api_version": "v21.0",
}

live = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED, **overrides})


# --- payload builders --------------------------------------------------------------


def envelope(
    *, messages: Sequence[dict[str, Any]] = (), statuses: Sequence[dict[str, Any]] = ()
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": "123"},
    }
    if messages:
        value["contacts"] = [{"profile": {"name": "Asha"}, "wa_id": SENDER_WA_ID}]
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = list(statuses)
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA_ID", "changes": [{"field": "messages", "value": value}]}],
    }


def text_msg(
    wamid: str, body: str = "hello", *, ts: int = T0, sender: str = SENDER_WA_ID
) -> dict[str, Any]:
    return {
        "from": sender,
        "id": wamid,
        "timestamp": str(ts),
        "type": "text",
        "text": {"body": body},
    }


def audio_msg(wamid: str, *, ts: int = T0) -> dict[str, Any]:
    return {
        "from": SENDER_WA_ID,
        "id": wamid,
        "timestamp": str(ts),
        "type": "audio",
        "audio": {"id": "MEDIA_1", "mime_type": "audio/ogg; codecs=opus", "voice": True},
    }


def status(wamid: str, state: str, *, ts: int = T0, code: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": wamid,
        "status": state,
        "timestamp": str(ts),
        "recipient_id": SENDER_WA_ID,
    }
    if code is not None:
        item["errors"] = [
            {"code": code, "title": "Re-engagement message", "message": "window closed"}
        ]
    return item


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def parse(data: dict[str, Any]) -> WebhookPayload:
    return WebhookPayload.model_validate(data)


def at(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


# --- in-memory stand-ins for the repository ----------------------------------------


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeStore:
    """Mimics the DB: phone -> user, unique wamid on turns, GREATEST on last_inbound_at."""

    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.phones = {SENDER: self.user_id}
        self.turns: dict[str, tuple[str, str | None]] = {}
        self.last_inbound: datetime | None = None
        self.advisories: list[Advisory] = []
        self.sessions: list[FakeSession] = []
        self.fail_wamids: set[str] = set()
        # process_webhook only uses the context-manager, commit and rollback parts.
        self.as_factory = cast(Callable[[], Session], self.factory)

    def factory(self) -> FakeSession:
        session = FakeSession()
        self.sessions.append(session)
        return session

    def find_user_id_by_phone(self, _session: object, phone: str) -> uuid.UUID | None:
        return self.phones.get(phone)

    def insert_inbound_turn(
        self,
        _session: object,
        *,
        user_id: uuid.UUID,
        wamid: str,
        channel: str,
        raw_text: str | None,
    ) -> bool:
        if wamid in self.fail_wamids:
            raise RuntimeError("db down")
        if wamid in self.turns:
            return False
        self.turns[wamid] = (channel, raw_text)
        return True

    def touch_last_inbound(self, _session: object, _user_id: uuid.UUID, when: datetime) -> None:
        self.last_inbound = when if self.last_inbound is None else max(self.last_inbound, when)

    def lock_advisory_by_wamid(self, _session: object, wamid: str) -> Advisory | None:
        return next((a for a in self.advisories if wamid in (a.wamid, a.audio_wamid)), None)

    def add_advisory(self, wamid: str, *, delivery_status: DeliveryStatus = "queued") -> Advisory:
        advisory = Advisory(id=uuid.uuid4(), wamid=wamid, delivery_status=delivery_status)
        self.advisories.append(advisory)
        return advisory


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake = FakeStore()
    for name in (
        "find_user_id_by_phone",
        "insert_inbound_turn",
        "touch_last_inbound",
        "lock_advisory_by_wamid",
    ):
        monkeypatch.setattr(whatsapp_webhook, name, getattr(fake, name))
    return fake


@pytest.fixture
def client(store: FakeStore) -> Iterator[TestClient]:
    app = main.create_app()  # lifespan (scheduler) is not started without `with`
    settings = make_settings()  # not passed directly: FastAPI would read **overrides as params
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_factory] = lambda: store.factory
    yield TestClient(app)
    app.dependency_overrides.clear()


def post(client: TestClient, data: dict[str, Any] | bytes, signature: str | None = "auto") -> Any:
    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    headers = {"Content-Type": "application/json"}
    if signature == "auto":
        headers["X-Hub-Signature-256"] = sign(body)
    elif signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/webhooks/whatsapp", content=body, headers=headers)


# --- GET handshake -----------------------------------------------------------------


def test_verify_handshake_echoes_challenge(client: TestClient) -> None:
    response = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1158201444"},
    )
    assert response.status_code == 200
    assert response.text == "1158201444"
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize(
    "params",
    [
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
        {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY, "hub.challenge": "1"},
        {"hub.mode": "subscribe", "hub.challenge": "1"},
        {"hub.mode": "subscribe", "hub.verify_token": VERIFY},
        {},
    ],
)
def test_verify_handshake_rejects_bad_requests(client: TestClient, params: dict[str, str]) -> None:
    response = client.get("/webhooks/whatsapp", params=params)
    assert response.status_code == 403
    assert VERIFY not in response.text


def test_webhook_callback_url_requires_public_base_url() -> None:
    url = webhooks.webhook_callback_url(
        make_settings(public_base_url="https://abc.ngrok-free.app/")
    )
    assert url == "https://abc.ngrok-free.app/webhooks/whatsapp"
    with pytest.raises(ConfigurationError, match="PUBLIC_BASE_URL"):
        webhooks.webhook_callback_url(make_settings())


# --- POST signature ----------------------------------------------------------------


@pytest.mark.parametrize(
    "signature",
    [
        None,  # header missing
        "",
        "sha256=" + "0" * 64,  # well-formed, wrong
        sign(b"some other body"),  # valid for a different body (tampered payload)
        sign(b"x", secret="not-the-app-secret").replace("x", ""),
        "sha1=" + "0" * 40,  # wrong algorithm prefix
    ],
)
def test_bad_signature_is_rejected_and_nothing_processed(
    client: TestClient, store: FakeStore, signature: str | None
) -> None:
    response = post(client, envelope(messages=[text_msg("wamid.M1")]), signature=signature)
    assert response.status_code == 403
    assert store.sessions == []
    assert store.turns == {}


def test_signature_is_checked_on_raw_bytes() -> None:
    # Same JSON, different whitespace: the signature of one does not verify the other.
    compact = b'{"object":"whatsapp_business_account","entry":[]}'
    spaced = b'{"object": "whatsapp_business_account", "entry": []}'
    assert verify_signature(compact, sign(compact), SECRET)
    assert not verify_signature(spaced, sign(compact), SECRET)
    assert verify_signature(compact, sign(compact).upper().replace("SHA256=", "sha256="), SECRET)


def test_valid_signature_returns_200_with_empty_body(client: TestClient, store: FakeStore) -> None:
    response = post(client, envelope(messages=[text_msg("wamid.M1", "fasting 132")]))
    assert response.status_code == 200
    assert response.content == b""
    assert store.turns == {"wamid.M1": ("whatsapp_text", "fasting 132")}


def test_signed_but_unparseable_body_is_acknowledged_and_ignored(
    client: TestClient, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(webhooks, "process_webhook", lambda *a: calls.append(a))
    assert post(client, b'{"entry": "not a list"}').status_code == 200
    assert post(client, b"not json at all").status_code == 200
    assert calls == []


# --- inbound messages --------------------------------------------------------------


def test_duplicate_message_id_is_stored_once(client: TestClient, store: FakeStore) -> None:
    payload = envelope(messages=[text_msg("wamid.DUP", "fasting 140")])
    assert post(client, payload).status_code == 200
    assert post(client, payload).status_code == 200  # Meta retry of the same delivery
    assert list(store.turns) == ["wamid.DUP"]
    # The retry rolled back and did not commit anything.
    assert [s.commits for s in store.sessions] == [1, 0]
    assert store.sessions[1].rollbacks == 1


def test_duplicate_inside_one_payload_is_processed_once(store: FakeStore) -> None:
    payload = parse(envelope(messages=[text_msg("wamid.X"), text_msg("wamid.X")]))
    result = process_webhook(payload, store.as_factory)
    assert (result.messages_stored, result.messages_duplicate) == (1, 1)
    assert len(store.sessions) == 1


def test_inbound_updates_last_inbound_at_monotonically(store: FakeStore) -> None:
    process_webhook(
        parse(envelope(messages=[text_msg("wamid.new", ts=T0 + 600)])), store.as_factory
    )
    process_webhook(parse(envelope(messages=[text_msg("wamid.old", ts=T0)])), store.as_factory)
    assert store.last_inbound == at(T0 + 600)


def test_audio_is_stored_as_voice_turn_without_text(store: FakeStore) -> None:
    result = process_webhook(parse(envelope(messages=[audio_msg("wamid.V")])), store.as_factory)
    assert result.messages_stored == 1
    assert store.turns["wamid.V"] == ("whatsapp_voice", None)
    assert store.last_inbound == at(T0)


def test_unknown_sender_is_logged_not_stored(
    store: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    payload = parse(envelope(messages=[text_msg("wamid.U", sender="447700900123")]))
    result = process_webhook(payload, store.as_factory)
    assert result.messages_unknown_sender == 1
    assert store.turns == {}
    assert "447700900123" not in caplog.text
    assert "0123" in caplog.text


def test_unhandled_type_still_opens_the_window(store: FakeStore) -> None:
    image = {
        "from": SENDER_WA_ID,
        "id": "wamid.I",
        "timestamp": str(T0),
        "type": "image",
        "image": {"id": "m"},
    }
    result = process_webhook(parse(envelope(messages=[image])), store.as_factory)
    assert result.messages_not_stored == 1
    assert store.turns == {}
    assert store.last_inbound == at(T0)


def test_one_failing_message_does_not_stop_the_rest(store: FakeStore) -> None:
    store.fail_wamids.add("wamid.BAD")
    payload = parse(envelope(messages=[text_msg("wamid.BAD"), text_msg("wamid.GOOD")]))
    result = process_webhook(payload, store.as_factory)
    assert (result.errors, result.messages_stored) == (1, 1)
    assert list(store.turns) == ["wamid.GOOD"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"type": "text", "text": {"body": "hi"}}, ("whatsapp_text", "hi")),
        (
            {"type": "button", "button": {"text": "Hear advisory", "payload": "p"}},
            ("whatsapp_text", "Hear advisory"),
        ),
        (
            {
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "b1", "title": "Yes"},
                },
            },
            ("whatsapp_text", "Yes"),
        ),
        ({"type": "audio", "audio": {"id": "m1"}}, ("whatsapp_voice", None)),
        ({"type": "sticker", "sticker": {"id": "s"}}, None),
    ],
)
def test_message_turn_by_type(
    message: dict[str, Any], expected: tuple[str, str | None] | None
) -> None:
    parsed = InboundMessage.model_validate(
        {"from": SENDER_WA_ID, "id": "w", "timestamp": str(T0), **message}
    )
    assert message_turn(parsed) == expected


def test_payload_parsing_reads_unix_timestamps_as_utc() -> None:
    payload = parse(envelope(messages=[text_msg("wamid.T")], statuses=[status("wamid.S", "sent")]))
    (message,) = payload.messages()
    assert message.timestamp == at(T0)
    assert message.timestamp.utcoffset() is not None
    assert payload.statuses()[0].timestamp == at(T0)


def test_other_objects_and_fields_are_ignored() -> None:
    data = envelope(messages=[text_msg("wamid.T")])
    assert parse({**data, "object": "page"}).messages() == []
    data["entry"][0]["changes"][0]["field"] = "account_update"
    assert parse(data).messages() == []


# --- statuses -> advisory ----------------------------------------------------------


def test_status_webhooks_advance_the_advisory(client: TestClient, store: FakeStore) -> None:
    advisory = store.add_advisory("wamid.ADV")
    for state, offset in (("sent", 0), ("delivered", 5), ("read", 60)):
        assert (
            post(
                client, envelope(statuses=[status("wamid.ADV", state, ts=T0 + offset)])
            ).status_code
            == 200
        )
    assert advisory.delivery_status == "read"
    assert (advisory.sent_at, advisory.delivered_at, advisory.read_at) == (
        at(T0),
        at(T0 + 5),
        at(T0 + 60),
    )
    assert advisory.failed_at is None


def test_out_of_order_status_never_downgrades() -> None:
    advisory = Advisory(wamid="w", delivery_status="queued")
    assert apply_status(advisory, StatusUpdate(id="w", status="read", timestamp=at(T0 + 60)))
    assert apply_status(advisory, StatusUpdate(id="w", status="delivered", timestamp=at(T0 + 5)))
    assert advisory.delivery_status == "read"
    assert advisory.delivered_at == at(T0 + 5)  # still recorded
    # A repeat of a status already applied changes nothing.
    assert not apply_status(advisory, StatusUpdate(id="w", status="read", timestamp=at(T0 + 90)))
    assert advisory.read_at == at(T0 + 60)


def test_failed_status_marks_advisory_failed_with_error(store: FakeStore) -> None:
    advisory = store.add_advisory("wamid.F", delivery_status="sent")
    payload = parse(envelope(statuses=[status("wamid.F", "failed", ts=T0 + 3, code=131047)]))
    result = process_webhook(payload, store.as_factory)
    assert result.statuses_applied == 1
    assert advisory.delivery_status == "failed"
    assert advisory.failed_at == at(T0 + 3)
    assert (advisory.delivery_error_code, advisory.delivery_error) == ("131047", "window closed")


def test_failure_after_delivery_records_error_but_keeps_status() -> None:
    advisory = Advisory(wamid="w", delivery_status="delivered")
    assert apply_status(advisory, StatusUpdate.model_validate(status("w", "failed", code=131000)))
    assert advisory.delivery_status == "delivered"
    assert advisory.failed_at is None
    assert advisory.delivery_error_code == "131000"


def test_failed_is_final_for_its_wamid() -> None:
    advisory = Advisory(wamid="w", delivery_status="failed")
    apply_status(advisory, StatusUpdate(id="w", status="delivered", timestamp=at(T0)))
    assert advisory.delivery_status == "failed"


def test_voice_note_status_does_not_drive_delivery_status() -> None:
    advisory = Advisory(wamid="text", audio_wamid="voice", delivery_status="sent")
    assert not apply_status(advisory, StatusUpdate(id="voice", status="read", timestamp=at(T0)))
    assert advisory.delivery_status == "sent"
    assert apply_status(
        advisory, StatusUpdate.model_validate(status("voice", "failed", code=131053))
    )
    assert advisory.delivery_status == "sent"
    assert advisory.delivery_error == "voice note failed: window closed"


def test_unmatched_and_untracked_statuses_are_ignored(store: FakeStore) -> None:
    store.add_advisory("wamid.A")
    payload = parse(
        envelope(statuses=[status("wamid.chat-reply", "delivered"), status("wamid.A", "deleted")])
    )
    result = process_webhook(payload, store.as_factory)
    assert (result.statuses_applied, result.statuses_ignored) == (0, 2)
    assert all(s.commits == 0 for s in store.sessions)


def test_webhook_result_defaults() -> None:
    assert WebhookResult().errors == 0


# --- live database round trips (opt-in) --------------------------------------------


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


def add_patient(factory: Callable[[], Session], wa_id: str) -> uuid.UUID:
    with factory() as session:
        user = User(id=uuid.uuid4(), role="patient")
        session.add(user)
        session.flush()
        session.add(Profile(user_id=user.id, phone_e164="+" + wa_id, diabetes_status="diabetes"))
        session.commit()
        return user.id


def add_advisory(factory: Callable[[], Session], user_id: uuid.UUID, wamid: str) -> uuid.UUID:
    with factory() as session:
        reading = Reading(
            user_id=user_id,
            context="fasting",
            value_mg_dl=Decimal("128.0"),
            measured_at=at(T0),
            source="api",
        )
        threshold = ThresholdHistory(
            user_id=user_id,
            limit_kind="fasting_high",
            direction="upper",
            diabetes_status="diabetes",
            base_mg_dl=Decimal("130.0"),
            unclamped_mg_dl=Decimal("130.0"),
            hard_min_mg_dl=Decimal("100.0"),
            hard_max_mg_dl=Decimal("180.0"),
            threshold_mg_dl=Decimal("130.0"),
            model_version="test",
            reason="initial",
            source_citation="test",
        )
        session.add_all([reading, threshold])
        session.flush()
        advisory = Advisory(
            user_id=user_id,
            reading_id=reading.id,
            threshold_id=threshold.id,
            limit_kind="fasting_high",
            state="pre_breach_high",
            reading_value_mg_dl=Decimal("128.0"),
            threshold_mg_dl=Decimal("130.0"),
            language="en",
            message_text="test advisory",
            delivery_channel="free_form",
            wamid=wamid,
        )
        session.add(advisory)
        session.commit()
        return advisory.id


def unique_wa_id() -> str:
    return f"4479{uuid.uuid4().int % 10**8:08d}"


@live
def test_live_duplicate_message_stored_once_and_window_opened(
    factory: Callable[[], Session],
) -> None:
    wa_id = unique_wa_id()
    user_id = add_patient(factory, wa_id)
    wamid = f"wamid.live-{uuid.uuid4()}"
    payload = parse(envelope(messages=[text_msg(wamid, "fasting 128", sender=wa_id, ts=T0 + 60)]))
    first = process_webhook(payload, factory)
    second = process_webhook(payload, factory)
    older = parse(envelope(messages=[text_msg(f"{wamid}-old", sender=wa_id, ts=T0)]))
    process_webhook(older, factory)

    assert (first.messages_stored, second.messages_duplicate) == (1, 1)
    with factory() as session:
        turns = session.scalars(
            select(ConversationTurn).where(ConversationTurn.user_id == user_id)
        ).all()
        assert sorted(t.wamid or "" for t in turns) == sorted([wamid, f"{wamid}-old"])
        stored = next(t for t in turns if t.wamid == wamid)
        assert (stored.direction, stored.channel, stored.raw_text) == (
            "inbound",
            "whatsapp_text",
            "fasting 128",
        )
        last = session.scalar(select(Profile.last_inbound_at).where(Profile.user_id == user_id))
        assert last == at(T0 + 60)  # the older message did not move it back


@live
def test_live_statuses_update_advisory(factory: Callable[[], Session]) -> None:
    wa_id = unique_wa_id()
    user_id = add_patient(factory, wa_id)
    ok_wamid, bad_wamid = f"wamid.ok-{uuid.uuid4()}", f"wamid.bad-{uuid.uuid4()}"
    ok_id = add_advisory(factory, user_id, ok_wamid)
    bad_id = add_advisory(factory, user_id, bad_wamid)

    payload = parse(
        envelope(
            statuses=[
                status(ok_wamid, "sent", ts=T0),
                status(ok_wamid, "read", ts=T0 + 60),
                status(ok_wamid, "delivered", ts=T0 + 5),
                status(bad_wamid, "failed", ts=T0 + 2, code=131047),
            ]
        )
    )
    result = process_webhook(payload, factory)
    assert (result.statuses_applied, result.errors) == (4, 0)
    with factory() as session:
        ok = session.get(Advisory, ok_id)
        bad = session.get(Advisory, bad_id)
        assert ok is not None and bad is not None
        assert ok.delivery_status == "read"
        assert (ok.sent_at, ok.delivered_at, ok.read_at) == (at(T0), at(T0 + 5), at(T0 + 60))
        assert bad.delivery_status == "failed"
        assert bad.failed_at == at(T0 + 2)
        assert bad.delivery_error_code == "131047"
