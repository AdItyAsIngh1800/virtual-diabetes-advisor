"""WhatsApp webhook: signature check, payload models and background processing (R3/R4).

Meta POSTs ``entry[].changes[].value`` objects carrying ``messages[]`` (inbound
messages) and ``statuses[]`` (delivery receipts for messages we sent). The route in
:mod:`app.api.webhooks` checks the signature, parses the body into
:class:`WebhookPayload`, answers 200 at once and hands the payload to
:func:`process_webhook` as a background task.

Processing:

* **Inbound message** -> look up the sender by number, append a ``conversation_turns``
  row (de-duplicated by ``wamid``: Meta re-delivers on retries) and move
  ``profiles.last_inbound_at`` forward, which opens the 24 h free-form window (R3).
  Text and audio are only stored for now; Phase 7 adds replies and transcription.
* **Status** -> find the advisory by ``wamid`` / ``audio_wamid`` and advance its
  delivery state ``queued -> sent -> delivered -> read``, or mark it ``failed``.

Each item commits on its own, so one failing item cannot undo the rest.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.logging import mask_phone
from app.core.phones import from_wa_id
from app.db.models import Advisory, DeliveryStatus, TurnChannel
from app.db.whatsapp_repository import (
    find_user_id_by_phone,
    insert_inbound_turn,
    lock_advisory_by_wamid,
    touch_last_inbound,
)

logger = logging.getLogger(__name__)

SIGNATURE_HEADER: Final = "X-Hub-Signature-256"
_SIGNATURE_PREFIX: Final = "sha256="
# The only webhook object / field this app subscribes to in the Meta dashboard.
WHATSAPP_OBJECT: Final = "whatsapp_business_account"
MESSAGES_FIELD: Final = "messages"

DeliveryEvent = Literal["sent", "delivered", "read", "failed"]
_DELIVERY_EVENTS: Final = frozenset(get_args(DeliveryEvent))
# Delivery only moves forward. Webhooks can arrive out of order ("read" before
# "delivered"); the rank stops a late "delivered" from downgrading a "read" advisory.
_PROGRESS_RANK: Final[dict[DeliveryStatus, int]] = {
    "queued": 0,
    "sent": 1,
    "delivered": 2,
    "read": 3,
}
# Status event -> (delivery_status it moves to, timestamp column it fills).
_PROGRESS_EVENTS: Final[dict[str, tuple[DeliveryStatus, str]]] = {
    "sent": ("sent", "sent_at"),
    "delivered": ("delivered", "delivered_at"),
    "read": ("read", "read_at"),
}


# --- Signature ---------------------------------------------------------------------


def verify_signature(raw_body: bytes, header_value: str | None, app_secret: str) -> bool:
    """Check ``X-Hub-Signature-256`` against the raw request body.

    Meta signs the exact bytes it sends: ``sha256=`` + hex HMAC-SHA256 keyed with the
    app secret. The HMAC must be computed over the raw body, before JSON parsing
    (re-serialised JSON would not match byte for byte).

    Args:
        raw_body: Request body exactly as received.
        header_value: The header value, or ``None`` if absent.
        app_secret: ``WHATSAPP_APP_SECRET``.

    Returns:
        ``True`` only for a present, well-formed, matching signature.
    """
    if not header_value or not header_value.startswith(_SIGNATURE_PREFIX):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    received = header_value.removeprefix(_SIGNATURE_PREFIX).strip().lower()
    # Constant-time comparison so response timing cannot leak how many bytes matched.
    return hmac.compare_digest(expected.encode(), received.encode())


# --- Payload models (unknown fields are ignored: Meta adds fields over time) --------


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)


class TextContent(_Frozen):
    """``text`` part of an inbound text message."""

    body: str


class AudioContent(_Frozen):
    """``audio`` part of an inbound audio / voice-note message."""

    id: str
    mime_type: str | None = None
    voice: bool | None = None


class ButtonContent(_Frozen):
    """``button`` part: the user tapped a template quick-reply button."""

    text: str | None = None
    payload: str | None = None


class InteractiveReply(_Frozen):
    """Chosen reply button or list row."""

    id: str
    title: str


class InteractiveContent(_Frozen):
    """``interactive`` part: reply to an interactive message we sent."""

    type: str
    button_reply: InteractiveReply | None = None
    list_reply: InteractiveReply | None = None


class InboundMessage(_Frozen):
    """One entry of ``value.messages[]``.

    Attributes:
        id: The message ``wamid`` (de-duplication key).
        from_: Sender ``wa_id`` (digits, no ``+``). PII: log it masked.
        timestamp: When the user sent it (WhatsApp sends Unix seconds).
        type: ``text``, ``audio``, ``button``, ``interactive``, ``image`` ...
    """

    id: str
    from_: str = Field(alias="from")
    timestamp: datetime
    type: str
    text: TextContent | None = None
    audio: AudioContent | None = None
    button: ButtonContent | None = None
    interactive: InteractiveContent | None = None


class StatusErrorDetail(_Frozen):
    """Why a message failed, e.g. code 131047 (24 h window closed)."""

    code: int | None = None
    title: str | None = None
    message: str | None = None


class StatusUpdate(_Frozen):
    """One entry of ``value.statuses[]``: delivery receipt for a message we sent.

    ``status`` stays a plain string: Meta also sends values we do not track
    (``deleted``, ``warning``), which are skipped rather than rejecting the payload.
    """

    id: str
    status: str
    timestamp: datetime
    recipient_id: str | None = None
    errors: list[StatusErrorDetail] = Field(default_factory=list)


class ChangeValue(_Frozen):
    """``entry[].changes[].value`` for the ``messages`` field."""

    messaging_product: str | None = None
    messages: list[InboundMessage] = Field(default_factory=list)
    statuses: list[StatusUpdate] = Field(default_factory=list)


class Change(_Frozen):
    """One ``entry[].changes[]`` item."""

    field: str
    value: ChangeValue


class Entry(_Frozen):
    """One ``entry[]`` item (one WhatsApp Business Account)."""

    id: str
    changes: list[Change] = Field(default_factory=list)


class WebhookPayload(_Frozen):
    """Top-level webhook body."""

    object: str
    entry: list[Entry] = Field(default_factory=list)

    def messages(self) -> list[InboundMessage]:
        """Return every inbound message in the payload.

        Returns:
            Messages from ``messages`` changes of the WhatsApp object, in order.
        """
        return [m for value in self._values() for m in value.messages]

    def statuses(self) -> list[StatusUpdate]:
        """Return every delivery status in the payload.

        Returns:
            Statuses from ``messages`` changes of the WhatsApp object, in order.
        """
        return [s for value in self._values() for s in value.statuses]

    def _values(self) -> list[ChangeValue]:
        if self.object != WHATSAPP_OBJECT:
            return []
        return [c.value for e in self.entry for c in e.changes if c.field == MESSAGES_FIELD]


# --- Processing ---------------------------------------------------------------------


@dataclass
class WebhookResult:
    """Counts from one :func:`process_webhook` call (logged; asserted in tests).

    Attributes:
        messages_stored: New inbound messages written to ``conversation_turns``.
        messages_duplicate: Messages skipped because their ``wamid`` was seen before.
        messages_unknown_sender: Messages from numbers with no profile (logged only).
        messages_not_stored: Message types not stored yet (image, location ...); they
            still update ``last_inbound_at``.
        statuses_applied: Statuses that changed an advisory.
        statuses_ignored: Statuses for non-advisory messages, untracked values, or
            ones that changed nothing (e.g. a late ``delivered`` after ``read``).
        errors: Items that raised; each is logged with its traceback.
    """

    messages_stored: int = 0
    messages_duplicate: int = 0
    messages_unknown_sender: int = 0
    messages_not_stored: int = 0
    statuses_applied: int = 0
    statuses_ignored: int = 0
    errors: int = 0


def message_turn(message: InboundMessage) -> tuple[TurnChannel, str | None] | None:
    """Decide how an inbound message is stored in ``conversation_turns``.

    Args:
        message: Parsed inbound message.

    Returns:
        ``(channel, raw_text)``, or ``None`` for types not stored yet. Audio is stored
        with ``raw_text=None``; Phase 7 downloads and transcribes it.
    """
    if message.type == "text" and message.text is not None:
        return "whatsapp_text", message.text.body
    if message.type == "button" and message.button is not None:
        return "whatsapp_text", message.button.text or message.button.payload
    if message.type == "interactive" and message.interactive is not None:
        reply = message.interactive.button_reply or message.interactive.list_reply
        return "whatsapp_text", reply.title if reply else None
    if message.type == "audio" and message.audio is not None:
        return "whatsapp_voice", None
    return None


def apply_status(advisory: Advisory, update: StatusUpdate) -> bool:
    """Apply one delivery status to an advisory row (R3 delivery tracking).

    Rules:

    * The text/template message (``advisory.wamid``) drives ``delivery_status``.
      ``sent``/``delivered``/``read`` only ever move it forward; each also fills its
      own timestamp if still empty, so a late ``delivered`` after ``read`` still
      records ``delivered_at``.
    * ``failed`` marks the advisory failed (with ``failed_at`` and the Graph error)
      unless it was already delivered or read; then only the error is recorded.
      ``failed`` is final for that ``wamid``: a re-send gets a new ``wamid``.
    * The voice note (``advisory.audio_wamid``) never changes the status (the text
      carries the alert); its failure is recorded in ``delivery_error``.

    Args:
        advisory: The row to update (locked by the caller).
        update: The status from the webhook.

    Returns:
        ``True`` if any column changed.
    """
    event = update.status
    if event not in _DELIVERY_EVENTS:
        return False
    is_voice_note = update.id == advisory.audio_wamid and update.id != advisory.wamid
    detail = update.errors[0] if update.errors else None

    if event == "failed":
        code = str(detail.code) if detail and detail.code is not None else None
        reason = (detail.message or detail.title) if detail else None
        message = reason or "failed without error details"
        advisory.delivery_error_code = code
        if is_voice_note:
            advisory.delivery_error = f"voice note failed: {message}"
            return True
        advisory.delivery_error = message
        if advisory.delivery_status in ("queued", "sent"):
            advisory.delivery_status = "failed"
            advisory.failed_at = update.timestamp
        return True

    if is_voice_note:
        return False
    changed = False
    new, stamp_column = _PROGRESS_EVENTS[event]
    if getattr(advisory, stamp_column) is None:
        setattr(advisory, stamp_column, update.timestamp)
        changed = True
    current = advisory.delivery_status
    if current != "failed" and _PROGRESS_RANK[new] > _PROGRESS_RANK[current]:
        advisory.delivery_status = new
        changed = True
    return changed


def process_webhook(
    payload: WebhookPayload, session_factory: Callable[[], Session]
) -> WebhookResult:
    """Store inbound messages and apply delivery statuses (background task).

    Args:
        payload: Verified, parsed webhook body.
        session_factory: Creates one session per item.

    Returns:
        Counts of what happened.
    """
    result = WebhookResult()
    seen: set[str] = set()
    for message in payload.messages():
        if message.id in seen:  # the same message twice in one delivery
            result.messages_duplicate += 1
            continue
        seen.add(message.id)
        try:
            _process_message(message, session_factory, result)
        except Exception:
            result.errors += 1
            logger.exception("failed to process inbound whatsapp message %s", message.id)
    for status in payload.statuses():
        try:
            _process_status(status, session_factory, result)
        except Exception:
            result.errors += 1
            logger.exception("failed to apply whatsapp status %s for %s", status.status, status.id)
    logger.info("whatsapp webhook processed: %s", result)
    return result


def _process_message(
    message: InboundMessage, session_factory: Callable[[], Session], result: WebhookResult
) -> None:
    """Store one inbound message and bump ``last_inbound_at``.

    Args:
        message: The message.
        session_factory: Session source.
        result: Counters to update.
    """
    phone = from_wa_id(message.from_)
    with session_factory() as session:
        user_id = find_user_id_by_phone(session, phone)
        if user_id is None:
            result.messages_unknown_sender += 1
            logger.info(
                "whatsapp %s message %s from unknown number %s ignored",
                message.type,
                message.id,
                mask_phone(phone),
            )
            return
        turn = message_turn(message)
        if turn is None:
            result.messages_not_stored += 1
            logger.info(
                "whatsapp %s message %s from user %s not stored (type not handled yet)",
                message.type,
                message.id,
                user_id,
            )
        else:
            channel, raw_text = turn
            if not insert_inbound_turn(
                session, user_id=user_id, wamid=message.id, channel=channel, raw_text=raw_text
            ):
                result.messages_duplicate += 1
                logger.info("whatsapp message %s already processed; skipped", message.id)
                session.rollback()
                return
            result.messages_stored += 1
            if message.audio is not None:
                # Phase 7 transcribes: GET /{media_id} -> download -> faster-whisper.
                logger.info(
                    "whatsapp voice note %s from user %s stored (media %s)",
                    message.id,
                    user_id,
                    message.audio.id,
                )
            else:
                logger.info(
                    "whatsapp %s message %s from user %s stored", message.type, message.id, user_id
                )
        # Any inbound message opens the 24 h free-form window (R3).
        touch_last_inbound(session, user_id, message.timestamp)
        session.commit()


def _process_status(
    status: StatusUpdate, session_factory: Callable[[], Session], result: WebhookResult
) -> None:
    """Apply one delivery status to its advisory, if it belongs to one.

    Args:
        status: The status.
        session_factory: Session source.
        result: Counters to update.
    """
    with session_factory() as session:
        advisory = lock_advisory_by_wamid(session, status.id)
        if advisory is None or not apply_status(advisory, status):
            result.statuses_ignored += 1
            session.rollback()
            return
        session.commit()
        result.statuses_applied += 1
        log = logger.warning if status.status == "failed" else logger.info
        log(
            "advisory %s: whatsapp %s -> delivery_status=%s%s",
            advisory.id,
            status.status,
            advisory.delivery_status,
            f" ({advisory.delivery_error})" if status.status == "failed" else "",
        )
