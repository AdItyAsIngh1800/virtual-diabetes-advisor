"""Tests for the chat and practitioner API (R4) with fakes; no database or network.

Repository functions are swapped for in-memory fakes; the chat engine runs for real
with the seeded knowledge base and no Anthropic key (the LLM is optional).
"""

from __future__ import annotations

import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from app import main
from app.api import chat
from app.api.advisories import get_audio_store
from app.api.auth import current_auth_user
from app.core.config import Settings, get_settings
from app.core.exceptions import SpeechError
from app.db.chat_repository import Contact, HandoffSummary
from app.db.models import ConversationTurn, Handoff, Practitioner, User
from app.db.session import get_session_factory
from app.services.chat import HandoffInfo, KbIndexCache, ParsedReading
from app.services.supabase_auth import AuthUser
from tests.test_chat import DOCS, FakeStore

SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="srk",
    database_url="postgresql://u:p@localhost:5432/db",
    # Explicit, so an exported ANTHROPIC_API_KEY cannot turn the LLM on in these tests.
    anthropic_api_key=None,
)
PATIENT_LOGIN = AuthUser(id=uuid.uuid4(), email="patient@example.com")
DOCTOR_LOGIN = AuthUser(id=uuid.uuid4(), email="doctor@example.com")
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


class FakeSession:
    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class FakeTranscriber:
    def __init__(self, text: str = "fasting 132", error: bool = False) -> None:
        self.text = text
        self.error = error
        self.calls: list[tuple[bytes, str]] = []

    def transcribe(self, audio: bytes, language: str) -> str:
        self.calls.append((audio, language))
        if self.error:
            raise SpeechError("bad audio")
        return self.text


class FakeSynth:
    def synthesize(self, text: str, language: str) -> bytes:
        return b"ID3" + text.encode()


class FakeAudioStore:
    def __init__(self) -> None:
        self.uploads: dict[str, bytes] = {}

    def upload(self, path: str, audio: bytes) -> None:
        self.uploads[path] = audio

    def signed_url(self, path: str, expires_in_s: int) -> str:
        return (
            f"https://abc.supabase.co/storage/v1/object/sign/advisory-audio/{path}?t={expires_in_s}"
        )


class FakeNotifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, uuid.UUID]] = []

    def practitioner_alert(
        self, practitioner: Contact, *, handoff_id: uuid.UUID, kind: str, urgent: bool
    ) -> None:
        self.events.append((f"practitioner:{kind}:{urgent}", handoff_id))

    def patient_reply(self, patient: Contact, *, handoff_id: uuid.UUID) -> None:
        self.events.append(("patient:reply", handoff_id))

    def patient_ping(self, patient: Contact, *, handoff_id: uuid.UUID) -> None:
        self.events.append(("patient:ping", handoff_id))


class World:
    """In-memory stand-in for the chat repository."""

    def __init__(self) -> None:
        self.patient = User(id=uuid.uuid4(), role="patient", auth_user_id=PATIENT_LOGIN.id)
        self.doctor = Practitioner(
            id=uuid.uuid4(), user_id=uuid.uuid4(), full_name="Dr Demo", is_active=True
        )
        self.doctor_linked = True
        self.assigned = True
        self.store = FakeStore()
        self.store.practitioner_id = self.doctor.id
        self.turns: list[ConversationTurn] = []
        self.handoff_rows: dict[uuid.UUID, Handoff] = {}
        self.claimed: list[uuid.UUID] = []
        self.resolved: list[tuple[uuid.UUID, str | None]] = []
        self.dispatched: list[uuid.UUID] = []

    # patient side
    def find_patient_by_auth_user(self, _s: object, auth_id: uuid.UUID) -> User | None:
        return self.patient if auth_id == PATIENT_LOGIN.id else None

    def find_practitioner_by_auth_user(self, _s: object, auth_id: uuid.UUID) -> Practitioner | None:
        return self.doctor if self.doctor_linked and auth_id == DOCTOR_LOGIN.id else None

    def turn_by_message_id(
        self, _s: object, user_id: uuid.UUID, message_id: str
    ) -> ConversationTurn | None:
        return next((t for t in self.turns if t.message_id == message_id), None)

    def save_turn(self, _s: object, **fields: Any) -> ConversationTurn:
        fields.pop("created_at", None)
        turn = ConversationTurn(
            id=uuid.uuid4(),
            channel="web",
            created_at=NOW,
            **{**fields, "kb_ids_used": fields.get("kb_ids_used") or []},
        )
        self.turns.append(turn)
        return turn

    def live_row(self, _s: object, user_id: uuid.UUID) -> Handoff | None:
        live = self.store.live
        if live is None or live.status == "resolved":
            return None
        return self.row_for(live)

    def row_for(self, live: HandoffInfo) -> Handoff:
        if live.id not in self.handoff_rows:
            self.handoff_rows[live.id] = Handoff(
                id=live.id,
                user_id=self.patient.id,
                practitioner_id=live.practitioner_id,
                trigger=live.trigger,
                status=live.status,
                opened_at=NOW,
            )
        return self.handoff_rows[live.id]

    def recent_turns(self, _s: object, user_id: uuid.UUID, **_kw: Any) -> list[ConversationTurn]:
        return [t for t in self.turns if t.user_id == user_id]

    # practitioner side
    def handoff_for_practitioner(
        self, _s: object, handoff_id: uuid.UUID, practitioner_id: uuid.UUID
    ) -> Handoff | None:
        row = self.handoff_rows.get(handoff_id)
        # Mirrors the SQL: only patients assigned to this practitioner.
        if row is None or not self.assigned or practitioner_id != self.doctor.id:
            return None
        return row

    def practitioner_handoffs(self, _s: object, practitioner_id: uuid.UUID) -> list[HandoffSummary]:
        if not self.assigned:
            return []
        return [
            HandoffSummary(
                id=h.id,
                patient_id=h.user_id,
                patient_name="Meera",
                status=h.status,
                trigger=h.trigger,
                reason=None,
                opened_at=NOW,
                assigned_at=h.assigned_at,
                resolved_at=h.resolved_at,
                last_message_at=None,
            )
            for h in self.handoff_rows.values()
        ]

    def claim_handoff(
        self, _s: object, handoff: Handoff, practitioner_id: uuid.UUID, at: datetime
    ) -> None:
        if handoff.status == "open":
            handoff.status = "assigned"
            handoff.assigned_at = at
        self.claimed.append(handoff.id)

    def resolve_handoff(
        self,
        _s: object,
        handoff: Handoff,
        practitioner_id: uuid.UUID,
        *,
        note: str | None,
        at: datetime,
    ) -> None:
        handoff.status = "resolved"
        handoff.resolved_at = at
        self.resolved.append((handoff.id, note))
        self.store.live = None

    def contact(self, _s: object, some_id: uuid.UUID) -> Contact:
        return Contact(user_id=some_id, auth_user_id=uuid.uuid4(), is_demo=False)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    for name in (
        "find_patient_by_auth_user",
        "find_practitioner_by_auth_user",
        "turn_by_message_id",
        "save_turn",
        "recent_turns",
        "handoff_for_practitioner",
        "practitioner_handoffs",
        "claim_handoff",
        "resolve_handoff",
    ):
        monkeypatch.setattr(chat, name, getattr(w, name))
    monkeypatch.setattr(chat, "_live_handoff_row", w.live_row)
    monkeypatch.setattr(chat, "user_contact", w.contact)
    monkeypatch.setattr(chat, "practitioner_contact", w.contact)
    monkeypatch.setattr(chat, "_valid_advisory_id", lambda _s, a, _u: a)
    monkeypatch.setattr(chat, "SqlChatStore", lambda _session, _settings: w.store)
    monkeypatch.setattr(chat, "dispatch_for_user", w.dispatched.append)
    return w


class Harness:
    def __init__(self, client: TestClient, transcriber: FakeTranscriber) -> None:
        self.client = client
        self.transcriber = transcriber
        self.audio_store = FakeAudioStore()
        self.notifier = FakeNotifier()


@pytest.fixture
def harness(world: World) -> Iterator[Harness]:
    app = main.create_app()
    transcriber = FakeTranscriber()
    h = Harness(TestClient(app), transcriber)
    login = {"who": PATIENT_LOGIN}
    h.login = login  # type: ignore[attr-defined]
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_session_factory] = lambda: FakeSession
    app.dependency_overrides[current_auth_user] = lambda: login["who"]
    app.dependency_overrides[chat.get_transcriber] = lambda: transcriber
    app.dependency_overrides[chat.get_synthesizer] = FakeSynth
    app.dependency_overrides[get_audio_store] = lambda: h.audio_store
    app.dependency_overrides[chat.get_notifier] = lambda: h.notifier
    app.dependency_overrides[chat.get_kb_cache] = KbIndexCache  # fresh index per test
    yield h
    app.dependency_overrides.clear()


def as_doctor(h: Harness) -> None:
    h.login["who"] = DOCTOR_LOGIN  # type: ignore[attr-defined]


# --- patient chat ---------------------------------------------------------------------


def test_chat_works_without_an_anthropic_key(harness: Harness, world: World) -> None:
    assert SETTINGS.anthropic_api_key is None  # get_rephraser is NOT overridden
    response = harness.client.post("/api/chat", data={"text": "what is the 15-15 rule"})
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "question"
    assert "Source: Treating low glucose (hypoglycaemia): the 15-15 rule" in body["reply"]
    assert body["audio_url"] is None  # typed, so no spoken reply
    (turn,) = world.turns
    assert (turn.direction, turn.raw_text, turn.transcript) == (
        "inbound",
        "what is the 15-15 rule",
        None,
    )
    assert (
        turn.model == "tfidf" and turn.model_version and turn.model_version.startswith("sklearn-")
    )
    assert turn.kb_ids_used == [next(d.id for d in DOCS if d.entry_key == "seed:rule-15-15")]
    assert turn.kb_version == 1
    assert turn.latency_ms is not None and turn.latency_ms >= 0
    assert turn.escalated is False


def test_voice_message_is_transcribed_and_answered_with_audio(
    harness: Harness, world: World
) -> None:
    audio = b"\x1aE\xdf\xa3fake-webm-bytes"
    response = harness.client.post(
        "/api/chat",
        files={"audio": ("voice.webm", audio, "audio/webm;codecs=opus")},
        data={"message_id": "msg-voice-0001"},
    )
    assert response.status_code == 200
    body = response.json()
    assert harness.transcriber.calls == [(audio, "en")]
    assert body["transcript"] == "fasting 132"
    assert body["intent"] == "reading_report"
    assert world.store.readings == [ParsedReading(value_mg_dl=Decimal(132), context="fasting")]
    # Spoken reply: stored privately under chat/<user>/<turn>.mp3, served by signed URL.
    (path,) = harness.audio_store.uploads
    assert path == f"chat/{world.patient.id}/{body['turn_id']}.mp3"
    assert body["audio_url"].startswith("https://abc.supabase.co/storage/v1/object/sign/")
    assert body["audio_url_expires_in_s"] == SETTINGS.advisory_audio_url_ttl_seconds
    (turn,) = world.turns
    assert turn.transcript == "fasting 132" and turn.raw_text is None
    assert turn.message_id == "msg-voice-0001"
    assert world.dispatched == [world.patient.id]  # advisory queued -> dispatch now


@pytest.mark.parametrize(
    ("files", "status"),
    [
        ({"audio": ("x.txt", b"hello", "text/plain")}, 415),
        ({"audio": ("v.webm", b"", "audio/webm")}, 422),
    ],
)
def test_bad_audio_is_refused(harness: Harness, files: dict[str, Any], status: int) -> None:
    assert harness.client.post("/api/chat", files=files).status_code == status


def test_too_long_recording_is_refused(harness: Harness) -> None:
    big = b"0" * (SETTINGS.chat_audio_max_bytes + 1)
    response = harness.client.post("/api/chat", files={"audio": ("v.webm", big, "audio/webm")})
    assert response.status_code == 413


def test_untranscribable_audio_is_a_422(harness: Harness) -> None:
    harness.transcriber.error = True
    response = harness.client.post("/api/chat", files={"audio": ("v.ogg", b"OggS", "audio/ogg")})
    assert response.status_code == 422
    harness.transcriber.error = False
    harness.transcriber.text = ""
    response = harness.client.post("/api/chat", files={"audio": ("v.ogg", b"OggS", "audio/ogg")})
    assert response.status_code == 422


def test_spoken_reply_survives_tts_failure(harness: Harness, world: World) -> None:
    class BrokenSynth:
        def synthesize(self, text: str, language: str) -> bytes:
            raise SpeechError("gTTS down")

    harness.client.app.dependency_overrides[chat.get_synthesizer] = BrokenSynth  # type: ignore[attr-defined]
    response = harness.client.post("/api/chat", files={"audio": ("v.webm", b"\x1aE", "audio/webm")})
    assert response.status_code == 200
    assert response.json()["audio_url"] is None  # page falls back to speechSynthesis


def test_red_flag_escalates_and_alerts_the_practitioner(harness: Harness, world: World) -> None:
    response = harness.client.post("/api/chat", data={"text": "I have chest pain"})
    body = response.json()
    assert body["intent"] == "symptom_red_flag" and body["escalated"] is True
    assert body["handoff"]["status"] == "open" and body["handoff"]["has_practitioner"]
    assert harness.notifier.events == [
        ("practitioner:red_flag:True", uuid.UUID(body["handoff"]["id"]))
    ]
    assert world.turns[0].escalated is True and world.turns[0].model == "rules"


def test_urgent_reading_alerts_the_practitioner_with_its_own_kind(
    harness: Harness, world: World
) -> None:
    body = harness.client.post("/api/chat", data={"text": "260"}).json()
    assert body["intent"] == "symptom_red_flag" and body["escalated"] is True
    assert harness.notifier.events == [
        ("practitioner:urgent_reading:True", uuid.UUID(body["handoff"]["id"]))
    ]
    assert world.turns[0].model == "rules"


def test_talk_to_a_doctor_button(harness: Harness, world: World) -> None:
    advisory = uuid.uuid4()
    response = harness.client.post(
        "/api/chat", data={"request_practitioner": "true", "advisory_id": str(advisory)}
    )
    body = response.json()
    assert body["intent"] == "practitioner_request"
    assert "A practitioner has been notified" in body["reply"]
    assert harness.notifier.events[0][0] == "practitioner:new:False"
    assert world.turns[0].advisory_id == advisory


def test_duplicate_message_id_returns_the_stored_reply(harness: Harness, world: World) -> None:
    first = harness.client.post("/api/chat", data={"text": "hello", "message_id": "dup-00000001"})
    again = harness.client.post("/api/chat", data={"text": "hello", "message_id": "dup-00000001"})
    assert again.json()["duplicate"] is True
    assert again.json()["turn_id"] == first.json()["turn_id"]
    assert len(world.turns) == 1


def test_empty_message_is_a_422(harness: Harness) -> None:
    assert harness.client.post("/api/chat", data={"text": "  "}).status_code == 422


def test_unlinked_login_is_refused(harness: Harness) -> None:
    harness.login["who"] = AuthUser(id=uuid.uuid4())  # type: ignore[attr-defined]
    assert harness.client.post("/api/chat", data={"text": "hi"}).status_code == 403
    assert harness.client.get("/api/chat/messages").status_code == 403
    assert harness.client.get("/api/me").status_code == 403


def test_messages_show_patient_bot_and_practitioner_bubbles(harness: Harness, world: World) -> None:
    harness.client.post("/api/chat", data={"text": "talk to a doctor"})
    handoff_id = world.store.live.id if world.store.live else None
    as_doctor(harness)
    assert handoff_id is not None
    harness.client.post(
        f"/api/practitioner/handoffs/{handoff_id}/messages", json={"text": "Hello, how are you?"}
    )
    harness.login["who"] = PATIENT_LOGIN  # type: ignore[attr-defined]
    data = harness.client.get("/api/chat/messages").json()
    assert [m["role"] for m in data["messages"]] == ["patient", "assistant", "practitioner"]
    assert data["messages"][2]["text"] == "Hello, how are you?"
    assert data["handoff"]["status"] == "assigned"


def test_me_tells_patient_from_practitioner(harness: Harness, world: World) -> None:
    assert harness.client.get("/api/me").json()["role"] == "patient"
    as_doctor(harness)
    assert harness.client.get("/api/me").json() == {
        "role": "practitioner",
        "user_id": str(world.doctor.user_id),
        "name": "Dr Demo",
    }


# --- practitioner relay ------------------------------------------------------------------


def _open_handoff(h: Harness, world: World) -> uuid.UUID:
    h.client.post("/api/chat", data={"text": "talk to a doctor"})
    assert world.store.live is not None
    world.row_for(world.store.live)
    as_doctor(h)
    return world.store.live.id


def test_practitioner_reply_assigns_relays_and_notifies(harness: Harness, world: World) -> None:
    handoff_id = _open_handoff(harness, world)
    response = harness.client.post(
        f"/api/practitioner/handoffs/{handoff_id}/messages", json={"text": "Please recheck now."}
    )
    assert response.status_code == 201
    assert response.json()["role"] == "practitioner"
    reply = world.turns[-1]
    assert (reply.direction, reply.author_practitioner_id, reply.handoff_id) == (
        "outbound",
        world.doctor.id,
        handoff_id,
    )
    assert world.handoff_rows[handoff_id].status == "assigned"
    assert ("patient:reply", handoff_id) in harness.notifier.events
    # The patient's next message is relayed to the practitioner.
    harness.login["who"] = PATIENT_LOGIN  # type: ignore[attr-defined]
    relayed = harness.client.post("/api/chat", data={"text": "ok I will"}).json()
    assert relayed["reply"].startswith("Sent to your practitioner")
    assert harness.notifier.events[-1][0] == "practitioner:message:False"


def test_practitioner_sees_only_assigned_patients(harness: Harness, world: World) -> None:
    handoff_id = _open_handoff(harness, world)
    world.assigned = False
    assert harness.client.get(f"/api/practitioner/handoffs/{handoff_id}").status_code == 404
    reply = harness.client.post(
        f"/api/practitioner/handoffs/{handoff_id}/messages", json={"text": "hi"}
    )
    assert reply.status_code == 404
    assert (
        harness.client.post(f"/api/practitioner/handoffs/{handoff_id}/resolve", json={}).status_code
        == 404
    )
    assert harness.client.get("/api/practitioner/handoffs").json() == []


def test_patient_login_cannot_use_practitioner_routes(harness: Harness, world: World) -> None:
    handoff_id = _open_handoff(harness, world)
    harness.login["who"] = PATIENT_LOGIN  # type: ignore[attr-defined]
    assert harness.client.get("/api/practitioner/handoffs").status_code == 403
    response = harness.client.post(
        f"/api/practitioner/handoffs/{handoff_id}/messages", json={"text": "hi"}
    )
    assert response.status_code == 403


def test_resolve_then_no_more_replies(harness: Harness, world: World) -> None:
    handoff_id = _open_handoff(harness, world)
    detail = harness.client.get(f"/api/practitioner/handoffs/{handoff_id}").json()
    assert detail["handoff"]["status"] == "open"
    assert [m["role"] for m in detail["messages"]] == ["patient", "assistant"]
    resolved = harness.client.post(
        f"/api/practitioner/handoffs/{handoff_id}/resolve", json={"note": "Advised recheck"}
    )
    assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"
    assert world.resolved == [(handoff_id, "Advised recheck")]
    assert ("patient:ping", handoff_id) in harness.notifier.events
    again = harness.client.post(
        f"/api/practitioner/handoffs/{handoff_id}/messages", json={"text": "hi"}
    )
    assert again.status_code == 409
