"""Tests for app.services.handoff_notify and the chat Realtime topics (R4)."""

from __future__ import annotations

import json
import uuid
from typing import Literal

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import NotificationDeliveryError, RecipientBlockedError
from app.db.chat_repository import Contact
from app.services.handoff_notify import PATIENT_CHAT_PAGE, PRACTITIONER_PAGE, HandoffNotifier
from app.services.notifications import DeliveryReport, Notification, Recipient
from app.services.realtime import HttpRealtimePublisher, chat_topic, handoff_topic

HANDOFF = uuid.uuid4()


class FakeChannel:
    name: Literal["webpush"] = "webpush"

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[Recipient, Notification]] = []
        self.error = error

    def send(self, recipient: Recipient, notification: Notification) -> DeliveryReport:
        if self.error is not None:
            raise self.error
        self.sent.append((recipient, notification))
        return DeliveryReport(channel="webpush", attempted=1, sent=1)


class FakeBroadcaster:
    def __init__(self, fail: bool = False) -> None:
        self.messages: list[tuple[str, str, dict[str, str]]] = []
        self.fail = fail

    def broadcast(self, *, topic: str, event: str, payload: dict[str, str]) -> None:
        if self.fail:
            raise NotificationDeliveryError("realtime down")
        self.messages.append((topic, event, payload))


def contact(*, linked: bool = True, demo: bool = False) -> Contact:
    return Contact(
        user_id=uuid.uuid4(), auth_user_id=uuid.uuid4() if linked else None, is_demo=demo
    )


def test_red_flag_alert_is_urgent_and_reveals_nothing() -> None:
    channel, rt = FakeChannel(), FakeBroadcaster()
    doctor = contact()
    HandoffNotifier(channel, rt).practitioner_alert(
        doctor, handoff_id=HANDOFF, kind="red_flag", urgent=True
    )
    assert doctor.auth_user_id is not None
    assert rt.messages == [
        (
            handoff_topic(doctor.auth_user_id),
            "handoff",
            {"handoff_id": str(HANDOFF), "kind": "red_flag"},
        )
    ]
    ((recipient, note),) = channel.sent
    assert recipient.user_id == doctor.user_id
    assert note.urgency == "high" and note.url == PRACTITIONER_PAGE
    assert note.tag == f"handoff-{HANDOFF}"
    payload = json.loads(note.payload())
    assert payload["tag"] == f"handoff-{HANDOFF}"
    # No names, readings or message text on a lock screen.
    assert not any(ch.isdigit() for ch in note.title + note.body)


def test_patient_reply_notifies_the_patient() -> None:
    channel, rt = FakeChannel(), FakeBroadcaster()
    patient = contact()
    HandoffNotifier(channel, rt).patient_reply(patient, handoff_id=HANDOFF)
    assert patient.auth_user_id is not None
    assert rt.messages[0][0] == chat_topic(patient.auth_user_id)
    assert channel.sent[0][1].url == PATIENT_CHAT_PAGE


def test_ping_is_realtime_only() -> None:
    channel, rt = FakeChannel(), FakeBroadcaster()
    HandoffNotifier(channel, rt).patient_ping(contact(), handoff_id=HANDOFF)
    assert len(rt.messages) == 1 and channel.sent == []


@pytest.mark.parametrize(
    "error", [RecipientBlockedError("demo"), NotificationDeliveryError("push down")]
)
def test_failures_are_logged_not_raised(error: Exception) -> None:
    notifier = HandoffNotifier(FakeChannel(error), FakeBroadcaster(fail=True))
    notifier.practitioner_alert(contact(), handoff_id=HANDOFF, kind="new", urgent=False)
    notifier.patient_reply(contact(linked=False, demo=True), handoff_id=HANDOFF)


def test_broadcast_posts_a_private_message() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    settings = Settings(
        _env_file=None,
        supabase_url="https://abc.supabase.co",
        supabase_service_role_key="srk",
        database_url="postgresql://u:p@localhost:5432/db",
    )
    publisher = HttpRealtimePublisher(
        settings, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    publisher.broadcast(topic="chat:abc", event="message", payload={"handoff_id": "x"})
    body = json.loads(seen[0].content)
    assert body == {
        "messages": [
            {
                "topic": "chat:abc",
                "event": "message",
                "payload": {"handoff_id": "x"},
                "private": True,
            }
        ]
    }


def test_urgent_reading_alert_is_high_priority_with_its_own_title() -> None:
    channel = FakeChannel()
    HandoffNotifier(channel, FakeBroadcaster()).practitioner_alert(
        contact(), handoff_id=HANDOFF, kind="urgent_reading", urgent=True
    )
    ((_, note),) = channel.sent
    assert note.urgency == "high"
    assert note.title == "Urgent: very high or low reading"
    assert not any(ch.isdigit() for ch in note.title + note.body)
