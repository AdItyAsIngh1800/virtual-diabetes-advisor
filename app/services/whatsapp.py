"""Async client for the Meta WhatsApp Cloud API (R3 advisory delivery, R4 replies).

Calls the Graph API directly with ``httpx`` (no Twilio). Base URL and version come
from settings (``GRAPH_API_VERSION``), never from inline strings.

Every ``send_*`` method takes a :class:`~app.services.whatsapp_guard.Recipient` and
runs :func:`~app.services.whatsapp_guard.check_recipient` before any HTTP request, so
demo patients on made-up numbers and numbers outside the test allowlist can never be
messaged.

Failures raise :class:`~app.core.exceptions.WhatsAppDeliveryError` with the Graph
``error.code`` / ``error_subcode`` parsed out. Transient failures (HTTP 5xx, 429, Graph
rate-limit codes, connection errors before the request was sent) are retried with
exponential backoff, up to ``max_attempts`` attempts in total (default 3).

Phone numbers are PII: log lines use :func:`~app.core.logging.mask_phone` only.

Usage::

    async with WhatsAppClient(get_settings()) as wa:
        result = await wa.send_text(Recipient.for_patient(user, profile), "Hello")
        advisory.wamid = result.wamid
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import TracebackType
from typing import Final, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.core.exceptions import WhatsAppDeliveryError
from app.core.phones import to_wa_id
from app.services.whatsapp_guard import Recipient, check_recipient

logger = logging.getLogger(__name__)

# Graph error codes that mean "slow down", per Meta's WhatsApp Cloud API error-code
# reference: 4 = app-level API call limit, 80007 = WABA rate limit, 130429 = Cloud API
# throughput limit, 131056 = too many messages to the same recipient (pair rate limit).
RATE_LIMIT_CODES: Final = frozenset({4, 80007, 130429, 131056})
# HTTP 429 Too Many Requests; any 5xx is a server-side failure worth retrying.
_HTTP_TOO_MANY_REQUESTS: Final = 429
_HTTP_SERVER_ERROR: Final = 500

DEFAULT_MAX_ATTEMPTS: Final = 3
# Backoff: 0.5 s, 1 s, 2 s ... capped at 8 s, plus up to 0.5 s of jitter so parallel
# workers hitting the same rate limit do not retry in lockstep.
DEFAULT_BACKOFF_BASE_S: Final = 0.5
DEFAULT_MAX_BACKOFF_S: Final = 8.0
# Never wait longer than this for a server-supplied Retry-After; the caller (batch job)
# is better off marking the advisory for a later run than blocking a worker.
_MAX_RETRY_AFTER_S: Final = 30.0
# Graph usually answers in < 1 s; media uploads of a ~25 s voice note are ~100 KB.
_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)

# Failures where httpx guarantees the request never reached Meta, so retrying cannot
# send a message twice. Read/write timeouts are *not* here: Meta may already have
# accepted the message, and a retry could deliver a duplicate alert.
_SAFE_TO_RETRY_TRANSPORT: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

SleepFn = Callable[[float], Awaitable[None]]


class _GraphErrorData(BaseModel):
    details: str | None = None


class _GraphError(BaseModel):
    """``error`` object of a Graph API error response."""

    message: str = "unknown error"
    type: str | None = None
    code: int | None = None
    error_subcode: int | None = None
    fbtrace_id: str | None = None
    error_data: _GraphErrorData | None = None


class _GraphErrorEnvelope(BaseModel):
    error: _GraphError


class _SentMessage(BaseModel):
    id: str


class _SendResponse(BaseModel):
    """Body of a successful ``POST /{phone_number_id}/messages``."""

    messages: list[_SentMessage] = Field(min_length=1)


class _MediaUploadResponse(BaseModel):
    """Body of a successful ``POST /{phone_number_id}/media``."""

    id: str


class SendResult(BaseModel):
    """Outcome of an accepted send.

    Attributes:
        wamid: WhatsApp message id. Store it (``advisories.wamid`` / ``audio_wamid``):
            delivery status webhooks refer to the message by this id.
    """

    model_config = ConfigDict(frozen=True)

    wamid: str


def parse_graph_error(response: httpx.Response, operation: str) -> WhatsAppDeliveryError:
    """Turn a failed Graph API response into a :class:`WhatsAppDeliveryError`.

    Args:
        response: A non-2xx response.
        operation: Short label for the message, e.g. ``"send text"``.

    Returns:
        The error (not raised), with ``retryable`` set for 5xx, 429 and rate limits.
    """
    status_code = response.status_code
    try:
        graph = _GraphErrorEnvelope.model_validate_json(response.content).error
    except ValidationError:
        # Proxies and Meta's edge sometimes answer with HTML; keep the status only.
        graph = _GraphError(message=f"HTTP {status_code} without a Graph error body")
    retryable = (
        status_code == _HTTP_TOO_MANY_REQUESTS
        or status_code >= _HTTP_SERVER_ERROR
        or graph.code in RATE_LIMIT_CODES
    )
    detail = (
        f" ({graph.error_data.details})" if graph.error_data and graph.error_data.details else ""
    )
    code = graph.code if graph.code is not None else "?"
    subcode = f"/{graph.error_subcode}" if graph.error_subcode is not None else ""
    return WhatsAppDeliveryError(
        f"{operation} failed: Graph error {code}{subcode} (HTTP {status_code}): "
        f"{graph.message}{detail}",
        http_status=status_code,
        code=graph.code,
        subcode=graph.error_subcode,
        fbtrace_id=graph.fbtrace_id,
        retryable=retryable,
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read a numeric ``Retry-After`` header, capped at 30 s.

    Args:
        response: The failed response.

    Returns:
        Seconds to wait, or ``None`` if the header is missing or not a number.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_S)


class WhatsAppClient:
    """Async WhatsApp Cloud API client with retries and the send guard built in.

    Use as an async context manager, or call :meth:`aclose` when done. If you pass
    your own ``http_client`` (tests use ``httpx.MockTransport``), you own its lifetime.

    Args:
        settings: Supplies the token, phone-number id, Graph version and guard config.
        http_client: Optional pre-built ``httpx.AsyncClient``.
        sleep: Awaitable sleep used between retries (tests pass a no-op).
        max_attempts: Total attempts per call, including the first (>= 1).
        backoff_base_s: First backoff delay in seconds; doubles each retry.
        max_backoff_s: Upper bound for one backoff delay.

    Raises:
        ValueError: If ``max_attempts`` is below 1.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: SleepFn = asyncio.sleep,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._settings = settings
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=_TIMEOUT)
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s
        self._max_backoff_s = max_backoff_s
        self._base_url = f"{settings.graph_api_base_url}/{settings.whatsapp_phone_number_id}"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_http:
            await self._http.aclose()

    # --- Public API --------------------------------------------------------------

    async def send_text(
        self, recipient: Recipient, body: str, *, preview_url: bool = False
    ) -> SendResult:
        """Send a free-form text message.

        Free-form messages only deliver inside the 24 h customer-service window
        (``profiles.last_inbound_at``); outside it Meta accepts the request but the
        status webhook later reports ``failed`` with code 131047. Use
        :meth:`send_template` then.

        Args:
            recipient: Target; checked by the send guard first.
            body: Message text (WhatsApp limit: 4096 characters).
            preview_url: Let WhatsApp render a link preview.

        Returns:
            The accepted message's ``wamid``.

        Raises:
            RecipientBlockedError: If the guard blocks the recipient.
            WhatsAppDeliveryError: If Graph rejects the message or stays unreachable.
        """
        return await self._send(
            recipient, "text", {"text": {"body": body, "preview_url": preview_url}}
        )

    async def send_template(
        self,
        recipient: Recipient,
        name: str,
        *,
        language_code: str,
        body_params: Sequence[str] = (),
        quick_reply_payloads: Sequence[str] = (),
    ) -> SendResult:
        """Send an approved message template (works outside the 24 h window).

        Args:
            recipient: Target; checked by the send guard first.
            name: Template name as approved in WhatsApp Manager, e.g. ``"hello_world"``.
            language_code: Template language exactly as approved, e.g. ``"en_US"``.
            body_params: Values for ``{{1}}``, ``{{2}}`` ... in the template body.
            quick_reply_payloads: Payload per quick-reply button, in button order. The
                payload comes back in the inbound ``button`` message when tapped.

        Returns:
            The accepted message's ``wamid``.

        Raises:
            RecipientBlockedError: If the guard blocks the recipient.
            WhatsAppDeliveryError: If Graph rejects the message or stays unreachable.
        """
        components: list[dict[str, object]] = []
        if body_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": value} for value in body_params],
                }
            )
        for index, payload in enumerate(quick_reply_payloads):
            components.append(
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": str(index),
                    "parameters": [{"type": "payload", "payload": payload}],
                }
            )
        template: dict[str, object] = {"name": name, "language": {"code": language_code}}
        if components:
            template["components"] = components
        return await self._send(recipient, "template", {"template": template})

    async def upload_media(
        self, content: bytes, *, mime_type: str = "audio/ogg", filename: str = "advisory.ogg"
    ) -> str:
        """Upload media (e.g. an OGG/Opus voice note) and return its media id.

        Uploading addresses no recipient, so the send guard does not apply here; it
        applies when the media id is sent with :meth:`send_audio`.

        Args:
            content: File bytes.
            mime_type: MIME type; ``audio/ogg`` (Opus codec) plays as a voice note.
            filename: Name reported to Meta.

        Returns:
            The media id, valid for 30 days.

        Raises:
            WhatsAppDeliveryError: If the upload fails or the response is malformed.
        """
        response = await self._request(
            "upload media",
            "media",
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (filename, content, mime_type)},
        )
        try:
            media_id = _MediaUploadResponse.model_validate_json(response.content).id
        except ValidationError as exc:
            raise WhatsAppDeliveryError(
                "upload media: unexpected response body", http_status=response.status_code
            ) from exc
        logger.info("whatsapp media uploaded: %s (%d bytes)", media_id, len(content))
        return media_id

    async def send_audio(self, recipient: Recipient, media_id: str) -> SendResult:
        """Send an uploaded audio file (a voice note when it is OGG/Opus).

        Like text, audio only delivers inside the 24 h window; templates cannot carry
        audio. Send a text alongside so the advisory is readable if audio fails.

        Args:
            recipient: Target; checked by the send guard first.
            media_id: Id returned by :meth:`upload_media`.

        Returns:
            The accepted message's ``wamid`` (store as ``advisories.audio_wamid``).

        Raises:
            RecipientBlockedError: If the guard blocks the recipient.
            WhatsAppDeliveryError: If Graph rejects the message or stays unreachable.
        """
        return await self._send(recipient, "audio", {"audio": {"id": media_id}})

    # --- Internals -------------------------------------------------------------------

    async def _send(
        self, recipient: Recipient, kind: str, content: Mapping[str, object]
    ) -> SendResult:
        """Guard, then POST one message and parse its ``wamid``.

        Args:
            recipient: Target.
            kind: Graph message ``type`` (``text``, ``template``, ``audio``).
            content: The type-specific part of the payload.

        Returns:
            The accepted message's ``wamid``.

        Raises:
            RecipientBlockedError: If the guard blocks the recipient.
            WhatsAppDeliveryError: If the request fails or the response is malformed.
        """
        # R3 safety guard: before any network I/O, for every message type.
        check_recipient(recipient, self._settings)
        payload: dict[str, object] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_wa_id(recipient.phone_e164),
            "type": kind,
            **content,
        }
        operation = f"send {kind} to {recipient.masked}"
        response = await self._request(operation, "messages", json=payload)
        try:
            wamid = _SendResponse.model_validate_json(response.content).messages[0].id
        except ValidationError as exc:
            raise WhatsAppDeliveryError(
                f"{operation}: unexpected response body", http_status=response.status_code
            ) from exc
        logger.info("whatsapp %s accepted for %s: %s", kind, recipient.masked, wamid)
        return SendResult(wamid=wamid)

    async def _request(
        self,
        operation: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        data: Mapping[str, str] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        """POST to ``/{phone_number_id}/{path}``, retrying transient failures.

        Args:
            operation: Label for errors and logs (contains masked numbers only).
            path: ``messages`` or ``media``.
            json: JSON body.
            data: Form fields (multipart upload).
            files: Multipart files.

        Returns:
            The successful (2xx) response.

        Raises:
            WhatsAppDeliveryError: On a non-retryable failure, or when the last
                attempt fails.
        """
        url = f"{self._base_url}/{path}"
        token = self._settings.whatsapp_access_token.get_secret_value()
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(1, self._max_attempts + 1):
            retry_after: float | None = None
            try:
                response = await self._http.post(
                    url, headers=headers, json=json, data=data, files=files
                )
            except _SAFE_TO_RETRY_TRANSPORT as exc:
                error = WhatsAppDeliveryError(
                    f"{operation} failed: Graph API unreachable ({type(exc).__name__})",
                    retryable=True,
                )
                error.__cause__ = exc
            except httpx.HTTPError as exc:
                # The request may have reached Meta (read timeout, dropped connection):
                # retrying could deliver the same alert twice, so stop here.
                raise WhatsAppDeliveryError(
                    f"{operation} failed after the request was sent ({type(exc).__name__}); "
                    "not retried to avoid a duplicate message",
                ) from exc
            else:
                if response.is_success:
                    return response
                error = parse_graph_error(response, operation)
                retry_after = _retry_after_seconds(response)

            if not error.retryable or attempt == self._max_attempts:
                logger.error("whatsapp %s (attempt %d/%d)", error, attempt, self._max_attempts)
                raise error
            delay = self._backoff_delay(attempt, retry_after)
            logger.warning(
                "whatsapp %s (attempt %d/%d); retrying in %.1fs",
                error,
                attempt,
                self._max_attempts,
                delay,
            )
            await self._sleep(delay)
        # range() above always runs at least once and every path returns or raises.
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        """Seconds to wait before attempt ``attempt + 1``.

        Args:
            attempt: The attempt that just failed (1-based).
            retry_after: Server-requested wait from ``Retry-After``, if any.

        Returns:
            ``Retry-After`` when given, else exponential backoff plus jitter.
        """
        if retry_after is not None:
            return retry_after
        exponential = min(self._backoff_base_s * 2.0 ** (attempt - 1), self._max_backoff_s)
        # Jitter only spreads out retries; it is not security-sensitive.
        return exponential + random.uniform(0, self._backoff_base_s)  # noqa: S311
