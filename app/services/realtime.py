"""Supabase Realtime alerts for the open patient and practitioner pages (R3, R4).

Web Push reaches a phone with the app closed; Realtime makes an open app react at
once (banner + link) without polling. The dispatcher publishes one Broadcast message
per delivered or attempted advisory through Realtime's REST endpoint
(``POST /realtime/v1/api/broadcast``) on the **private** topic
``advisories:<auth user id>``. Migration 0007's policy on ``realtime.messages`` lets a
signed-in patient join their own topic only.

Why REST and not ``realtime.send()`` in SQL: ``realtime.send`` writes into a partition
of ``realtime.messages`` that the Realtime server creates only while it is in use, and
it turns any failure into a ``WARNING`` - a message can vanish silently. The REST call
goes straight to the Realtime server and fails loudly.

The payload is deliberately minimal (advisory id, state, page URL): the page then
fetches the advisory through ``GET /api/advisories/{id}``, which checks ownership.

Phase 7 (R4) adds two more private topics (policy in migration 0008):
``chat:<auth uid>`` (a practitioner replied to this patient) and
``handoffs:<auth uid>`` (a handoff for this practitioner was opened or updated). Their
payloads carry only the handoff id; the pages re-fetch through the API.
"""

from __future__ import annotations

import logging
import uuid
from typing import Final, Protocol

import httpx

from app.core.config import Settings
from app.core.exceptions import NotificationDeliveryError
from app.db.models import AdvisoryState

logger = logging.getLogger(__name__)

ADVISORY_EVENT: Final = "advisory"
CHAT_EVENT: Final = "message"
HANDOFF_EVENT: Final = "handoff"
_TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)
# Realtime answers 202 Accepted for a queued broadcast.
_ACCEPTED: Final = frozenset({200, 202})


def advisory_topic(auth_user_id: uuid.UUID) -> str:
    """Realtime topic a signed-in patient listens on.

    Args:
        auth_user_id: ``auth.users.id`` of the patient's login.

    Returns:
        ``"advisories:<auth_user_id>"`` (must match the 0007 RLS policy).
    """
    return f"advisories:{auth_user_id}"


def advisory_page_url(advisory_id: uuid.UUID) -> str:
    """Same-origin path of the advisory page.

    Args:
        advisory_id: Advisory.

    Returns:
        ``"/app/advisory/<id>"``.
    """
    return f"/app/advisory/{advisory_id}"


class AdvisoryPublisher(Protocol):
    """Announces a new advisory to the patient's open app."""

    def publish(
        self, *, auth_user_id: uuid.UUID, advisory_id: uuid.UUID, state: AdvisoryState
    ) -> None:
        """Broadcast that an advisory is ready.

        Args:
            auth_user_id: Patient's login (topic).
            advisory_id: Advisory.
            state: Band state (lets the page pick a colour before fetching).

        Raises:
            NotificationDeliveryError: If Realtime does not accept the message.
        """
        ...


class Broadcaster(Protocol):
    """Sends one Broadcast message on a private Realtime topic (R3 alerts, R4 chat)."""

    def broadcast(self, *, topic: str, event: str, payload: dict[str, str]) -> None:
        """Broadcast ``payload`` on ``topic``.

        Args:
            topic: Private topic, e.g. ``"chat:<auth uid>"``.
            event: Event name the page listens for.
            payload: Ids only; never health details or names.

        Raises:
            NotificationDeliveryError: If Realtime does not accept the message.
        """
        ...


def chat_topic(auth_user_id: uuid.UUID) -> str:
    """Realtime topic a signed-in patient's chat page listens on (R4).

    Args:
        auth_user_id: ``auth.users.id`` of the patient's login.

    Returns:
        ``"chat:<auth_user_id>"`` (must match the 0008 RLS policy).
    """
    return f"chat:{auth_user_id}"


def handoff_topic(auth_user_id: uuid.UUID) -> str:
    """Realtime topic a signed-in practitioner's page listens on (R4).

    Args:
        auth_user_id: ``auth.users.id`` of the practitioner's login.

    Returns:
        ``"handoffs:<auth_user_id>"`` (must match the 0008 RLS policy).
    """
    return f"handoffs:{auth_user_id}"


class HttpRealtimePublisher:
    """:class:`AdvisoryPublisher` and :class:`Broadcaster` over Realtime's REST API.

    Posts to Supabase Realtime's Broadcast REST endpoint with the service-role key,
    which may broadcast to private topics.

    Args:
        settings: Supplies the project URL and service-role key.
        http_client: Optional client (tests pass one with a mock transport).
    """

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None) -> None:
        self._url = f"{str(settings.supabase_url).rstrip('/')}/realtime/v1/api/broadcast"
        key = settings.supabase_service_role_key.get_secret_value()
        self._headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        self._client = http_client or httpx.Client(timeout=_TIMEOUT)

    def broadcast(self, *, topic: str, event: str, payload: dict[str, str]) -> None:
        """Broadcast one message on a private topic.

        Args:
            topic: Private topic.
            event: Event name.
            payload: Small JSON payload (ids only).

        Raises:
            NotificationDeliveryError: On a network error or a non-2xx answer.
        """
        message = {"topic": topic, "event": event, "payload": payload, "private": True}
        try:
            response = self._client.post(
                self._url, json={"messages": [message]}, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise NotificationDeliveryError(
                f"realtime broadcast failed ({type(exc).__name__})", retryable=True
            ) from exc
        if response.status_code not in _ACCEPTED:
            raise NotificationDeliveryError(
                f"realtime broadcast answered HTTP {response.status_code}",
                status_code=response.status_code,
            )

    def publish(
        self, *, auth_user_id: uuid.UUID, advisory_id: uuid.UUID, state: AdvisoryState
    ) -> None:
        """Broadcast on the patient's private advisory topic.

        Args:
            auth_user_id: Patient's login (topic).
            advisory_id: Advisory.
            state: Band state.

        Raises:
            NotificationDeliveryError: On a network error or a non-2xx answer.
        """
        self.broadcast(
            topic=advisory_topic(auth_user_id),
            event=ADVISORY_EVENT,
            payload={
                "advisory_id": str(advisory_id),
                "state": state,
                "url": advisory_page_url(advisory_id),
            },
        )
        logger.debug("advisory %s broadcast on realtime", advisory_id)
