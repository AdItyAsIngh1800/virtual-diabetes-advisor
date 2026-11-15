"""Channel-neutral notification types, the recipient guard and dispatch rules (R3).

Advisories reach patients through a :class:`NotificationChannel`. Web Push
(:mod:`app.services.webpush`) is the only channel today; another (e.g. email) can be
added later by implementing the same Protocol, without touching callers.

Rules shared by every channel:

* **Recipient guard** (:func:`check_recipient`): demo patients are only notified when
  they are linked to the developer's own login (``seed_demo --my-email`` sets
  ``users.auth_user_id`` on exactly one demo patient). Real patients are notified
  normally; they can only have subscriptions after signing in themselves.
* **Payload**: small, and only the reading and the patient's own limit. Never names,
  phone numbers or anything about other patients (web-push-voice-advisory skill).
* **Urgency**: ``high`` for breaches and readings below 54 mg/dL, ``normal`` otherwise.
* **Freshness**: an advisory whose reading is older than ``ADVISORY_MAX_AGE_MINUTES``
  expires instead of being sent.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.clinical_limits import HYPO_LEVEL_2_MG_DL
from app.core.config import Settings
from app.core.exceptions import RecipientBlockedError
from app.db.models import AdvisoryState, NotificationChannelName, User

logger = logging.getLogger(__name__)

# Web Push urgency values (RFC 8030 section 5.3).
Urgency = Literal["very-low", "low", "normal", "high"]

# R3: one hour (skill rule). A push service holds an undelivered message at most this
# long; an hour-old glucose alert no longer helps the patient act on the reading.
DEFAULT_TTL_SECONDS: Final = 3600
# RFC 8030 allows longer TTLs, but nothing in this app is worth delivering a day late.
_MAX_TTL_SECONDS: Final = 24 * 3600
# Notification text limits: Android truncates long titles/bodies anyway, and a
# small payload stays well under the ~4 KB push-service limit after encryption.
_MAX_TITLE: Final = 80
_MAX_BODY: Final = 240


class Recipient(BaseModel):
    """Who a notification is for, with the facts the guard needs.

    Attributes:
        user_id: Patient id.
        is_demo: ``users.is_demo``.
        is_linked: The patient is linked to a Supabase login (``users.auth_user_id``).
    """

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    is_demo: bool
    is_linked: bool

    @classmethod
    def for_user(cls, user: User) -> Recipient:
        """Build the recipient from a ``users`` row.

        Args:
            user: The patient.

        Returns:
            The recipient.
        """
        return cls(user_id=user.id, is_demo=user.is_demo, is_linked=user.auth_user_id is not None)


class Notification(BaseModel):
    """One notification, independent of the channel that delivers it.

    Attributes:
        title: Short heading.
        body: One or two sentences: the reading and the patient's own limit only.
        url: Same-origin path the notification opens (never an external URL).
        advisory_id: Advisory this is about; also the notification ``tag``, so a repeat
            replaces the earlier notification instead of stacking.
        urgency: Push-service delivery priority.
        ttl_seconds: How long a push service may hold it for an offline device.
        tag: Notification tag for non-advisory messages (e.g. ``handoff-<id>``), so
            repeats about the same conversation replace each other. Advisories use
            their id instead and leave this ``None``.
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, max_length=_MAX_TITLE)
    body: str = Field(min_length=1, max_length=_MAX_BODY)
    url: str = "/app/"
    advisory_id: uuid.UUID | None = None
    urgency: Urgency = "normal"
    ttl_seconds: int = Field(default=DEFAULT_TTL_SECONDS, ge=0, le=_MAX_TTL_SECONDS)
    tag: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9:_-]+$")

    @field_validator("url")
    @classmethod
    def _same_origin_path(cls, value: str) -> str:
        """Allow only a path on this site, so a payload can never send a patient elsewhere.

        Args:
            value: The URL.

        Returns:
            The path.

        Raises:
            ValueError: If it is not a same-origin absolute path.
        """
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("url must be a same-origin path such as /app/")
        return value

    def payload(self) -> str:
        """Serialise the JSON the service worker reads (``{advisory_id, title, body, url}``).

        ``tag`` is added only when set (chat/handoff notifications, R4).

        Returns:
            Compact JSON text.
        """
        data: dict[str, str | None] = {
            "advisory_id": str(self.advisory_id) if self.advisory_id else None,
            "title": self.title,
            "body": self.body,
            "url": self.url,
        }
        if self.tag is not None:
            data["tag"] = self.tag
        return json.dumps(data, separators=(",", ":"))


class DeliveryReport(BaseModel):
    """What one :meth:`NotificationChannel.send` did across the patient's devices.

    Attributes:
        channel: Channel name, stored in ``advisories.channel``.
        attempted: Active subscriptions (devices) tried.
        sent: Devices the push service accepted the message for.
        revoked: Subscriptions found dead (404/410) and revoked.
        failed: Devices that failed for any other reason.
        message_ids: Push-service message ids (``Location`` header) of accepted sends.
        errors: Short error descriptions, without endpoints or keys.
    """

    channel: NotificationChannelName
    attempted: int = 0
    sent: int = 0
    revoked: int = 0
    failed: int = 0
    message_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def delivered(self) -> bool:
        """At least one device accepted the message.

        Returns:
            ``True`` when ``sent > 0``.
        """
        return self.sent > 0


class NotificationChannel(Protocol):
    """A way to reach a patient (Web Push today).

    Implementations must call :func:`check_recipient` before any network I/O and
    must never raise for a single device failing: they report it in the
    :class:`DeliveryReport` instead.
    """

    name: NotificationChannelName

    def send(self, recipient: Recipient, notification: Notification) -> DeliveryReport:
        """Deliver ``notification`` to every active device of ``recipient``.

        Args:
            recipient: Target patient.
            notification: What to show.

        Returns:
            Per-device outcome counts.

        Raises:
            RecipientBlockedError: If the guard blocks the recipient.
        """
        ...


def check_recipient(recipient: Recipient) -> None:
    """Raise unless ``recipient`` may be notified (R3 safety guard).

    Args:
        recipient: Target patient.

    Raises:
        RecipientBlockedError: For a demo patient that is not the one linked to the
            developer's login.
    """
    if recipient.is_demo and not recipient.is_linked:
        logger.warning("notification to demo user %s blocked: not linked", recipient.user_id)
        raise RecipientBlockedError(
            f"demo user {recipient.user_id} is not linked to your login "
            "(only the seed_demo --my-email patient receives notifications)"
        )


def urgency_for(state: AdvisoryState, reading_mg_dl: Decimal | float) -> Urgency:
    """Pick the push urgency for an advisory.

    Args:
        state: Band state of the advisory.
        reading_mg_dl: The reading that raised it.

    Returns:
        ``high`` for a breach or a reading below 54 mg/dL, else ``normal``.
    """
    # Breaches escalate to a practitioner, and < 54 mg/dL is ADA Level 2
    # hypoglycaemia (clinical_limits.HYPO_LEVEL_2_MG_DL): deliver those immediately
    # even on a phone in battery saver.
    if state.startswith("breach_") or float(reading_mg_dl) < HYPO_LEVEL_2_MG_DL:
        return "high"
    return "normal"


def is_expired(measured_at: datetime, now: datetime, settings: Settings) -> bool:
    """Tell whether an advisory's reading is too old to notify about.

    Args:
        measured_at: When the reading was taken (timezone-aware).
        now: Current time (timezone-aware).
        settings: Supplies ``ADVISORY_MAX_AGE_MINUTES``.

    Returns:
        ``True`` when the reading is older than the limit (exactly at it is fresh).
    """
    return now - measured_at > timedelta(minutes=settings.advisory_max_age_minutes)
