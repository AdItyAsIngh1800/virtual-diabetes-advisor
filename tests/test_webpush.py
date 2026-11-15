"""Tests for app.services.webpush (R3): retries, revocation, guard, real encryption.

HTTP is mocked throughout. The channel tests use a scripted fake sender; the
``PywebpushSender`` tests run real pywebpush encryption and VAPID signing against a
fake ``requests`` session, then decrypt the captured body like a browser would.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import http_ece
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import SecretStr

from app.core.config import VapidConfig
from app.core.exceptions import NotificationDeliveryError, RecipientBlockedError
from app.db.push_repository import StoredSubscription
from app.services.notifications import Notification, Recipient, Urgency
from app.services.webpush import (
    PushResponse,
    PywebpushSender,
    WebPushChannel,
    mask_endpoint,
)
from scripts.gen_vapid_keys import generate_vapid_keys

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
ME = Recipient(user_id=uuid.uuid4(), is_demo=True, is_linked=True)
NOTE = Notification(title="Glucose alert", body="Reading 128 mg/dL, limit 130 mg/dL.")


def sub(name: str = "a") -> StoredSubscription:
    return StoredSubscription(
        id=uuid.uuid4(),
        endpoint=f"https://fcm.googleapis.com/fcm/send/secret-token-{name}-XYZ123",
        p256dh="p" * 87,
        auth="a" * 22,
    )


class FakeStore:
    def __init__(self, *subs: StoredSubscription) -> None:
        self.subs = list(subs)
        self.revoked: list[uuid.UUID] = []
        self.succeeded: list[uuid.UUID] = []
        self.reads = 0

    def active_for_user(self, user_id: uuid.UUID) -> list[StoredSubscription]:
        self.reads += 1
        return [s for s in self.subs if s.id not in self.revoked]

    def revoke(self, subscription_id: uuid.UUID, at: datetime) -> None:
        self.revoked.append(subscription_id)

    def mark_success(self, subscription_id: uuid.UUID, at: datetime) -> None:
        self.succeeded.append(subscription_id)


Outcome = PushResponse | Exception


class FakeSender:
    """Replays scripted outcomes per endpoint and records each call."""

    def __init__(self, script: dict[str, list[Outcome]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, int, Urgency, str]] = []

    def __call__(
        self, subscription: StoredSubscription, *, data: str, ttl: int, urgency: Urgency
    ) -> PushResponse:
        self.calls.append((subscription.endpoint, ttl, urgency, data))
        outcome = self.script[subscription.endpoint].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def channel(
    store: FakeStore, script: dict[str, list[Outcome]]
) -> tuple[WebPushChannel, FakeSender, list[float]]:
    sender = FakeSender(script)
    sleeps: list[float] = []
    return (
        WebPushChannel(store, sender, sleep=sleeps.append, clock=lambda: NOW),
        sender,
        sleeps,
    )


# --- guard and empty cases ----------------------------------------------------------


def test_blocked_recipient_reads_nothing_and_sends_nothing() -> None:
    store = FakeStore(sub())
    ch, sender, _ = channel(store, {})
    with pytest.raises(RecipientBlockedError):
        ch.send(Recipient(user_id=uuid.uuid4(), is_demo=True, is_linked=False), NOTE)
    assert store.reads == 0
    assert sender.calls == []


def test_no_subscriptions_is_not_delivered() -> None:
    ch, _, _ = channel(FakeStore(), {})
    report = ch.send(ME, NOTE)
    assert (report.delivered, report.attempted) == (False, 0)
    assert report.errors == ["no active push subscription"]


# --- success, ttl, urgency ----------------------------------------------------------


def test_accepted_push_passes_ttl_urgency_and_payload() -> None:
    s = sub()
    store = FakeStore(s)
    ch, sender, sleeps = channel(store, {s.endpoint: [PushResponse(201, "https://fcm/msg/1")]})
    note = Notification(title="Low", body="Reading 52 mg/dL.", urgency="high", ttl_seconds=600)
    report = ch.send(ME, note)
    assert (report.sent, report.failed, report.revoked, report.delivered) == (1, 0, 0, True)
    assert report.message_ids == ["https://fcm/msg/1"]
    assert store.succeeded == [s.id]
    ((_, ttl, urgency, data),) = sender.calls
    assert (ttl, urgency) == (600, "high")
    assert json.loads(data)["body"] == "Reading 52 mg/dL."
    assert sleeps == []


# --- revocation ---------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 410])
def test_gone_subscription_is_revoked_without_retry(status: int) -> None:
    s = sub()
    store = FakeStore(s)
    ch, sender, sleeps = channel(store, {s.endpoint: [PushResponse(status)]})
    report = ch.send(ME, NOTE)
    assert (report.revoked, report.delivered) == (1, False)
    assert store.revoked == [s.id]
    assert len(sender.calls) == 1
    assert sleeps == []


def test_one_dead_device_does_not_stop_the_others() -> None:
    dead, alive = sub("dead"), sub("alive")
    store = FakeStore(dead, alive)
    ch, _, _ = channel(
        store, {dead.endpoint: [PushResponse(410)], alive.endpoint: [PushResponse(201)]}
    )
    report = ch.send(ME, NOTE)
    assert (report.attempted, report.sent, report.revoked) == (2, 1, 1)
    assert report.delivered


# --- retries ------------------------------------------------------------------------


def test_5xx_is_retried_with_backoff_then_succeeds() -> None:
    s = sub()
    ch, sender, sleeps = channel(
        FakeStore(s), {s.endpoint: [PushResponse(500), PushResponse(503), PushResponse(201)]}
    )
    assert ch.send(ME, NOTE).sent == 1
    assert len(sender.calls) == 3
    assert 0.5 <= sleeps[0] < 1.0
    assert 1.0 <= sleeps[1] < 1.5


def test_gives_up_after_three_attempts() -> None:
    s = sub()
    store = FakeStore(s)
    ch, sender, sleeps = channel(store, {s.endpoint: [PushResponse(502)] * 3})
    report = ch.send(ME, NOTE)
    assert (report.failed, report.delivered) == (1, False)
    assert report.errors == ["HTTP 502"]
    assert (len(sender.calls), len(sleeps)) == (3, 2)
    assert store.revoked == []


def test_429_honours_retry_after() -> None:
    s = sub()
    ch, _, sleeps = channel(
        FakeStore(s), {s.endpoint: [PushResponse(429, retry_after=7.0), PushResponse(201)]}
    )
    assert ch.send(ME, NOTE).sent == 1
    assert sleeps == [7.0]


@pytest.mark.parametrize("exc", [requests.ConnectionError("dns"), requests.Timeout("slow")])
def test_network_errors_are_retried(exc: Exception) -> None:
    s = sub()
    ch, sender, _ = channel(FakeStore(s), {s.endpoint: [exc, PushResponse(201)]})
    assert ch.send(ME, NOTE).sent == 1
    assert len(sender.calls) == 2


@pytest.mark.parametrize(
    "outcome",
    [PushResponse(400), PushResponse(403), PushResponse(413), ValueError("bad p256dh")],
)
def test_permanent_failures_are_not_retried(outcome: Outcome) -> None:
    s = sub()
    store = FakeStore(s)
    ch, sender, sleeps = channel(store, {s.endpoint: [outcome]})
    report = ch.send(ME, NOTE)
    assert (report.failed, len(sender.calls), sleeps, store.revoked) == (1, 1, [], [])


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        WebPushChannel(FakeStore(), FakeSender({}), max_attempts=0)


def test_logs_never_contain_the_full_endpoint(caplog: pytest.LogCaptureFixture) -> None:
    s = sub()
    ch, _, _ = channel(FakeStore(s), {s.endpoint: [PushResponse(500)] * 3})
    with caplog.at_level(logging.DEBUG):
        ch.send(ME, NOTE)
    assert "secret-token" not in caplog.text
    assert mask_endpoint(s.endpoint) in caplog.text
    assert mask_endpoint(s.endpoint) == "fcm.googleapis.com/...XYZ123"


# --- PywebpushSender: real encryption + VAPID, fake HTTP ------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Browser:
    """A fake browser subscription with real keys, so payloads can be decrypted."""

    def __init__(self) -> None:
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        point = self.private_key.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )
        self.subscription = StoredSubscription(
            id=uuid.uuid4(),
            endpoint="https://fcm.googleapis.com/fcm/send/abc123",
            p256dh=_b64url(point),
            auth=_b64url(self.auth),
        )

    def decrypt(self, body: bytes) -> str:
        plain: bytes = http_ece.decrypt(
            body, private_key=self.private_key, auth_secret=self.auth, version="aes128gcm"
        )
        return plain.decode()


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.reason = "fake"
        self.text = ""


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.posts: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.posts.append({"url": url, **kwargs})
        return self.response


def vapid() -> VapidConfig:
    keys = generate_vapid_keys()
    return VapidConfig(keys.public_key, SecretStr(keys.private_key), "mailto:team6@example.com")


def test_real_sender_encrypts_signs_and_sets_headers() -> None:
    browser, config = Browser(), vapid()
    session = FakeSession(FakeResponse(201, {"Location": "https://fcm/msg/9"}))
    send: Callable[..., PushResponse] = PywebpushSender(config, session=session)  # type: ignore[arg-type]
    response = send(browser.subscription, data=NOTE.payload(), ttl=3600, urgency="high")

    assert response == PushResponse(201, "https://fcm/msg/9", None)
    (post,) = session.posts
    assert post["url"] == browser.subscription.endpoint
    headers = {k.lower(): str(v) for k, v in post["headers"].items()}
    assert headers["ttl"] == "3600"
    assert headers["urgency"] == "high"
    assert headers["content-encoding"] == "aes128gcm"
    # VAPID (RFC 8292): "vapid t=<JWT>, k=<our public key>".
    assert headers["authorization"].startswith("vapid t=")
    assert f"k={config.public_key}" in headers["authorization"]
    # Only the browser's private key can read the payload.
    assert json.loads(browser.decrypt(post["data"])) == json.loads(NOTE.payload())


def test_real_sender_returns_error_statuses_instead_of_raising() -> None:
    browser = Browser()
    session = FakeSession(FakeResponse(410))
    send = PywebpushSender(vapid(), session=session)  # type: ignore[arg-type]
    assert send(browser.subscription, data="{}", ttl=60, urgency="normal").status_code == 410


def test_real_sender_parses_retry_after() -> None:
    browser = Browser()
    session = FakeSession(FakeResponse(429, {"Retry-After": "120"}))
    send = PywebpushSender(vapid(), session=session)  # type: ignore[arg-type]
    # Capped at 30 s so one rate limit cannot stall the batch worker.
    assert send(browser.subscription, data="{}", ttl=60, urgency="normal").retry_after == 30.0


def test_real_sender_rejects_malformed_subscription_keys() -> None:
    session = FakeSession(FakeResponse(201))
    send = PywebpushSender(vapid(), session=session)  # type: ignore[arg-type]
    with pytest.raises((ValueError, NotificationDeliveryError)):
        send(sub(), data="{}", ttl=60, urgency="normal")
    assert session.posts == []
