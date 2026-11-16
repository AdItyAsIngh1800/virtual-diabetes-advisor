"""Notify people about practitioner-connect activity (R4): Web Push + Realtime.

* A new handoff, a red flag or a new patient message in a live handoff ->
  the assigned **practitioner**: Realtime ``handoffs:<auth uid>`` (open page refreshes)
  and a Web Push that opens ``/app/practitioner``.
* A practitioner reply -> the **patient**: Realtime ``chat:<auth uid>`` and a Web Push
  that opens the chat at ``/app/``.

Payloads never contain names, readings or message text: just "you have a new
message" and the handoff id, so a lock-screen notification reveals nothing. Pushes about
one handoff share the tag ``handoff-<id>`` and replace each other instead of piling up.

Called after the turn is committed (FastAPI background task), so a slow push service
never delays the chat reply; every failure is logged and swallowed here.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Final

from app.core.exceptions import NotificationDeliveryError
from app.db.chat_repository import Contact
from app.services.notifications import Notification, NotificationChannel, Recipient
from app.services.realtime import (
    CHAT_EVENT,
    HANDOFF_EVENT,
    Broadcaster,
    chat_topic,
    handoff_topic,
)

logger = logging.getLogger(__name__)

PRACTITIONER_PAGE: Final = "/app/practitioner"
PATIENT_CHAT_PAGE: Final = "/app/"
# A chat ping is only useful while the conversation is live; 15 minutes covers a phone
# that is briefly offline without delivering stale nudges hours later.
CHAT_PUSH_TTL_SECONDS: Final = 15 * 60


def _tag(handoff_id: uuid.UUID) -> str:
    """Notification tag shared by every push about one handoff.

    Args:
        handoff_id: Handoff.

    Returns:
        ``"handoff-<id>"``.
    """
    return f"handoff-{handoff_id}"


@dataclass(frozen=True)
class HandoffNotifier:
    """Sends the push and the Realtime ping for handoff events.

    Attributes:
        channel: Web Push channel.
        broadcaster: Realtime broadcaster.
    """

    channel: NotificationChannel
    broadcaster: Broadcaster

    def practitioner_alert(
        self, practitioner: Contact, *, handoff_id: uuid.UUID, kind: str, urgent: bool
    ) -> None:
        """Tell the practitioner a handoff needs them.

        Args:
            practitioner: Practitioner's contact.
            handoff_id: Handoff.
            kind: ``new``, ``red_flag`` (emergency), ``urgent_reading`` or ``message``.
            urgent: Urgent push (red flags).
        """
        if practitioner.auth_user_id is not None:
            self._broadcast(
                handoff_topic(practitioner.auth_user_id),
                HANDOFF_EVENT,
                {"handoff_id": str(handoff_id), "kind": kind},
            )
        title, body = {
            "red_flag": (
                "Emergency: patient needs help",
                "A patient reported a red flag. Open now.",
            ),
            "urgent_reading": (
                "Urgent: very high or low reading",
                "A patient reported a reading that needs you today. Open now.",
            ),
            "new": ("New patient request", "A patient asked to talk to you."),
        }.get(kind, ("New patient message", "A patient sent a new message."))
        self._push(
            practitioner,
            Notification(
                title=title,
                body=body,
                url=PRACTITIONER_PAGE,
                urgency="high" if urgent else "normal",
                ttl_seconds=CHAT_PUSH_TTL_SECONDS,
                tag=_tag(handoff_id),
            ),
        )

    def patient_reply(self, patient: Contact, *, handoff_id: uuid.UUID) -> None:
        """Tell the patient their practitioner replied.

        Args:
            patient: Patient's contact.
            handoff_id: Handoff.
        """
        if patient.auth_user_id is not None:
            self._broadcast(
                chat_topic(patient.auth_user_id), CHAT_EVENT, {"handoff_id": str(handoff_id)}
            )
        self._push(
            patient,
            Notification(
                title="Your practitioner replied",
                body="Open the app to read the reply.",
                url=PATIENT_CHAT_PAGE,
                ttl_seconds=CHAT_PUSH_TTL_SECONDS,
                tag=_tag(handoff_id),
            ),
        )

    def patient_ping(self, patient: Contact, *, handoff_id: uuid.UUID) -> None:
        """Refresh the patient's open chat (e.g. the handoff was resolved); no push.

        Args:
            patient: Patient's contact.
            handoff_id: Handoff.
        """
        if patient.auth_user_id is not None:
            self._broadcast(
                chat_topic(patient.auth_user_id), CHAT_EVENT, {"handoff_id": str(handoff_id)}
            )

    def _broadcast(self, topic: str, event: str, payload: dict[str, str]) -> None:
        """Broadcast, logging instead of raising.

        Args:
            topic: Private topic.
            event: Event name.
            payload: Ids only.
        """
        try:
            self.broadcaster.broadcast(topic=topic, event=event, payload=payload)
        except NotificationDeliveryError as exc:
            logger.warning(
                "realtime %s on %s failed: %s", event, topic.split(":", maxsplit=1)[0], exc
            )

    def _push(self, contact: Contact, notification: Notification) -> None:
        """Push to every device of ``contact``, logging instead of raising.

        Args:
            contact: Recipient.
            notification: What to show.
        """
        recipient = Recipient(
            user_id=contact.user_id,
            is_demo=contact.is_demo,
            is_linked=contact.auth_user_id is not None,
        )
        try:
            report = self.channel.send(recipient, notification)
        except NotificationDeliveryError as exc:
            # Includes RecipientBlockedError (unlinked demo user).
            logger.info("push to user %s not sent: %s", contact.user_id, exc)
            return
        if not report.delivered:
            logger.info("push to user %s not delivered: %s", contact.user_id, report.errors)
