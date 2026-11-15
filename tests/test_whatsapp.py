"""Tests for the WhatsApp Cloud API client and the send guard (R3).

HTTP is mocked with ``httpx.MockTransport``: no request ever leaves the process.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import RecipientBlockedError, WhatsAppDeliveryError
from app.core.phones import from_wa_id, is_fictional_demo_phone, normalise_e164, to_wa_id
from app.db.models import Profile, User
from app.services.whatsapp import RATE_LIMIT_CODES, WhatsAppClient, parse_graph_error
from app.services.whatsapp_guard import Recipient, check_recipient

# Async tests run on asyncio via the anyio pytest plugin (installed with httpx).
pytestmark = pytest.mark.anyio

MY_PHONE = "+919876543210"
OTHER_PHONE = "+447700900123"
DEMO_FAKE = "+15550107"
TOKEN = "test-token-abc"
REQUIRED: dict[str, Any] = {
    "supabase_url": "https://abc.supabase.co",
    "supabase_service_role_key": "srk",
    "database_url": "postgresql://u:p@localhost:5432/db",
    "whatsapp_access_token": TOKEN,
    "whatsapp_phone_number_id": "1234567890",
    "whatsapp_verify_token": "v",
    "whatsapp_app_secret": "s",
    "graph_api_version": "v21.0",
}


def make_settings(**overrides: Any) -> Settings:
    base = {**REQUIRED, "allowed_test_numbers": [MY_PHONE], "whatsapp_test_mode": True}
    return Settings(_env_file=None, **{**base, **overrides})


def me(*, is_demo: bool = False) -> Recipient:
    return Recipient(phone_e164=MY_PHONE, is_demo=is_demo)


def ok_send(wamid: str = "wamid.OK") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "messaging_product": "whatsapp",
            "contacts": [{"input": MY_PHONE, "wa_id": to_wa_id(MY_PHONE)}],
            "messages": [{"id": wamid}],
        },
    )


def graph_error(status: int, code: int, message: str = "boom", **extra: Any) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"message": message, "type": "OAuthException", "code": code, **extra}},
    )


class Recorder:
    """MockTransport handler that replays queued responses and records requests."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def client_for() -> Callable[..., tuple[WhatsAppClient, Recorder, Sleeps]]:
    def build(
        *responses: httpx.Response | Exception, settings: Settings | None = None
    ) -> tuple[WhatsAppClient, Recorder, Sleeps]:
        recorder = Recorder(*responses)
        sleeps = Sleeps()
        http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
        client = WhatsAppClient(settings or make_settings(), http_client=http, sleep=sleeps)
        return client, recorder, sleeps

    return build


# --- phones ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phone", "fictional"),
    [
        ("+15550100", True),
        ("+15550199", True),
        ("+15550200", False),  # just outside the 0100-0199 fiction block
        ("+15550099", False),
        ("+155501000", False),  # longer number that merely starts the same
        (MY_PHONE, False),
    ],
)
def test_fictional_demo_block(phone: str, fictional: bool) -> None:
    assert is_fictional_demo_phone(phone) is fictional


def test_wa_id_round_trip() -> None:
    assert to_wa_id(MY_PHONE) == "919876543210"
    assert from_wa_id("919876543210") == MY_PHONE
    assert normalise_e164("+91 98765-43210") == MY_PHONE
    with pytest.raises(ValueError, match=r"E\.164") as excinfo:
        normalise_e164("44412 33321")
    assert "44412" not in str(excinfo.value)


# --- settings ----------------------------------------------------------------------


def test_allowed_numbers_parsed_from_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key.upper(), value)
    monkeypatch.setenv("ALLOWED_TEST_NUMBERS", f" {MY_PHONE} , +44 7700 900123,,{MY_PHONE}")
    settings = Settings(_env_file=None)
    assert settings.allowed_test_numbers == (MY_PHONE, OTHER_PHONE)
    assert settings.whatsapp_test_mode is True  # fails safe by default


def test_allowed_numbers_reject_non_e164() -> None:
    with pytest.raises(ValueError, match=r"E\.164"):
        make_settings(allowed_test_numbers="12345")


# --- guard -------------------------------------------------------------------------


def test_guard_blocks_demo_patient_on_fictional_number_even_outside_test_mode() -> None:
    settings = make_settings(whatsapp_test_mode=False)
    with pytest.raises(RecipientBlockedError, match="fictional"):
        check_recipient(Recipient(phone_e164=DEMO_FAKE, is_demo=True), settings)


def test_guard_allows_demo_patient_with_my_real_number() -> None:
    check_recipient(me(is_demo=True), make_settings())
    check_recipient(me(is_demo=True), make_settings(whatsapp_test_mode=False))


def test_guard_blocks_number_outside_allowlist_in_test_mode() -> None:
    with pytest.raises(RecipientBlockedError, match="not in ALLOWED_TEST_NUMBERS"):
        check_recipient(Recipient(phone_e164=OTHER_PHONE, is_demo=False), make_settings())


def test_guard_fails_closed_with_empty_allowlist() -> None:
    with pytest.raises(RecipientBlockedError, match="empty"):
        check_recipient(me(), make_settings(allowed_test_numbers=[]))


def test_guard_allows_any_real_number_outside_test_mode() -> None:
    check_recipient(
        Recipient(phone_e164=OTHER_PHONE, is_demo=False), make_settings(whatsapp_test_mode=False)
    )


def test_guard_error_and_log_hide_the_number(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING), pytest.raises(RecipientBlockedError) as excinfo:
        check_recipient(Recipient(phone_e164=OTHER_PHONE, is_demo=False), make_settings())
    assert OTHER_PHONE not in str(excinfo.value)
    assert OTHER_PHONE not in caplog.text
    assert "0123" in caplog.text  # last four digits stay visible


def test_recipient_for_patient_uses_is_demo_and_checks_ownership() -> None:
    user = User(id=uuid.uuid4(), role="patient", is_demo=True)
    recipient = Recipient.for_patient(user, Profile(user_id=user.id, phone_e164=MY_PHONE))
    assert recipient == Recipient(phone_e164=MY_PHONE, is_demo=True, user_id=user.id)
    with pytest.raises(ValueError, match="does not belong"):
        Recipient.for_patient(user, Profile(user_id=uuid.uuid4(), phone_e164=MY_PHONE))


async def test_blocked_send_makes_no_http_request(client_for: Callable[..., Any]) -> None:
    client, recorder, _ = client_for(ok_send())
    for recipient in (
        Recipient(phone_e164=DEMO_FAKE, is_demo=True),
        Recipient(phone_e164=OTHER_PHONE, is_demo=False),
    ):
        with pytest.raises(RecipientBlockedError):
            await client.send_text(recipient, "hi")
        with pytest.raises(RecipientBlockedError):
            await client.send_audio(recipient, "media-1")
        with pytest.raises(RecipientBlockedError):
            await client.send_template(recipient, "hello_world", language_code="en_US")
    assert recorder.requests == []


# --- client: payloads --------------------------------------------------------------


async def test_send_text_request_shape(client_for: Callable[..., Any]) -> None:
    client, recorder, _ = client_for(ok_send("wamid.T1"))
    result = await client.send_text(me(), "Hello")
    assert result.wamid == "wamid.T1"
    (request,) = recorder.requests
    assert str(request.url) == "https://graph.facebook.com/v21.0/1234567890/messages"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "919876543210",
        "type": "text",
        "text": {"body": "Hello", "preview_url": False},
    }


async def test_send_template_with_params_and_quick_reply(client_for: Callable[..., Any]) -> None:
    client, recorder, _ = client_for(ok_send())
    await client.send_template(
        me(),
        "glucose_advisory",
        language_code="en",
        body_params=["Asha", "182", "180"],
        quick_reply_payloads=["hear_advisory:42"],
    )
    template = json.loads(recorder.requests[0].content)["template"]
    assert template["name"] == "glucose_advisory"
    assert template["language"] == {"code": "en"}
    body, button = template["components"]
    assert [p["text"] for p in body["parameters"]] == ["Asha", "182", "180"]
    assert button["sub_type"] == "quick_reply"
    assert button["index"] == "0"
    assert button["parameters"] == [{"type": "payload", "payload": "hear_advisory:42"}]


async def test_template_without_params_has_no_components(client_for: Callable[..., Any]) -> None:
    client, recorder, _ = client_for(ok_send())
    await client.send_template(me(), "hello_world", language_code="en_US")
    assert "components" not in json.loads(recorder.requests[0].content)["template"]


async def test_upload_media_then_send_audio(client_for: Callable[..., Any]) -> None:
    client, recorder, _ = client_for(
        httpx.Response(200, json={"id": "media-9"}), ok_send("wamid.A")
    )
    media_id = await client.upload_media(b"OggS-fake-bytes")
    result = await client.send_audio(me(), media_id)
    assert (media_id, result.wamid) == ("media-9", "wamid.A")
    upload, send = recorder.requests
    assert str(upload.url).endswith("/1234567890/media")
    assert upload.headers["Content-Type"].startswith("multipart/form-data")
    assert b'name="messaging_product"' in upload.content
    assert b"audio/ogg" in upload.content
    assert b"OggS-fake-bytes" in upload.content
    assert json.loads(send.content)["audio"] == {"id": "media-9"}


async def test_success_log_masks_the_number(
    client_for: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    client, _, _ = client_for(ok_send())
    with caplog.at_level(logging.INFO):
        await client.send_text(me(), "Hello")
    assert "wamid.OK" in caplog.text
    assert MY_PHONE not in caplog.text
    assert to_wa_id(MY_PHONE) not in caplog.text


# --- client: errors and retries ----------------------------------------------------


async def test_graph_error_is_parsed_and_not_retried(client_for: Callable[..., Any]) -> None:
    client, recorder, sleeps = client_for(
        graph_error(
            400,
            131030,
            "Recipient phone number not in allowed list",
            error_subcode=2655007,
            fbtrace_id="AbC123",
            error_data={"details": "Add the number in the app dashboard"},
        )
    )
    with pytest.raises(WhatsAppDeliveryError) as excinfo:
        await client.send_text(me(), "Hello")
    err = excinfo.value
    assert (err.http_status, err.code, err.subcode, err.fbtrace_id) == (
        400,
        131030,
        2655007,
        "AbC123",
    )
    assert err.retryable is False
    assert "not in allowed list" in str(err)
    assert "Add the number" in str(err)
    assert len(recorder.requests) == 1
    assert sleeps.delays == []


async def test_5xx_is_retried_then_succeeds(client_for: Callable[..., Any]) -> None:
    client, recorder, sleeps = client_for(
        graph_error(500, 2, "Service temporarily unavailable"),
        httpx.Response(503, text="<html>bad gateway</html>"),
        ok_send("wamid.R"),
    )
    result = await client.send_text(me(), "Hello")
    assert result.wamid == "wamid.R"
    assert len(recorder.requests) == 3
    assert len(sleeps.delays) == 2
    # Exponential: ~0.5 s then ~1 s, each plus < 0.5 s jitter.
    assert 0.5 <= sleeps.delays[0] < 1.0
    assert 1.0 <= sleeps.delays[1] < 1.5


async def test_gives_up_after_three_attempts(client_for: Callable[..., Any]) -> None:
    client, recorder, sleeps = client_for(*(graph_error(502, 1) for _ in range(3)))
    with pytest.raises(WhatsAppDeliveryError) as excinfo:
        await client.send_text(me(), "Hello")
    assert excinfo.value.retryable is True
    assert excinfo.value.http_status == 502
    assert len(recorder.requests) == 3
    assert len(sleeps.delays) == 2


@pytest.mark.parametrize("code", sorted(RATE_LIMIT_CODES))
async def test_rate_limit_codes_are_retried(client_for: Callable[..., Any], code: int) -> None:
    client, recorder, _ = client_for(graph_error(400, code, "rate limit hit"), ok_send())
    await client.send_text(me(), "Hello")
    assert len(recorder.requests) == 2


async def test_retry_after_header_is_honoured(client_for: Callable[..., Any]) -> None:
    limited = httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": 130429}})
    client, _, sleeps = client_for(limited, ok_send())
    await client.send_text(me(), "Hello")
    assert sleeps.delays == [7.0]


async def test_connect_error_is_retried(client_for: Callable[..., Any]) -> None:
    client, recorder, _ = client_for(httpx.ConnectError("dns failure"), ok_send())
    await client.send_text(me(), "Hello")
    assert len(recorder.requests) == 2


async def test_read_timeout_is_not_retried(client_for: Callable[..., Any]) -> None:
    client, recorder, sleeps = client_for(httpx.ReadTimeout("slow"), ok_send())
    with pytest.raises(WhatsAppDeliveryError, match="duplicate"):
        await client.send_text(me(), "Hello")
    assert len(recorder.requests) == 1
    assert sleeps.delays == []


async def test_malformed_success_body_raises(client_for: Callable[..., Any]) -> None:
    client, _, _ = client_for(httpx.Response(200, json={"messages": []}))
    with pytest.raises(WhatsAppDeliveryError, match="unexpected response"):
        await client.send_text(me(), "Hello")


def test_parse_graph_error_without_json_body() -> None:
    err = parse_graph_error(httpx.Response(504, text="Gateway Timeout"), "send text")
    assert err.retryable is True
    assert err.code is None
    assert "HTTP 504" in str(err)


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        WhatsAppClient(make_settings(), max_attempts=0)


async def test_owned_http_client_is_closed() -> None:
    async with WhatsAppClient(make_settings()) as client:
        http = client._http
    assert http.is_closed
