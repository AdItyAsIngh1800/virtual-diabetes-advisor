"""Supabase Realtime alerts for the open patient app (R3).

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


class HttpRealtimePublisher:
    """:class:`AdvisoryPublisher` using Supabase Realtime's Broadcast REST endpoint.

    Uses the service-role key, which may broadcast to private topics.

    Args:
        settings: Supplies the project URL and service-role key.
        http_client: Optional client (tests pass one with a mock transport).
    """

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None) -> None:
        self._url = f"{str(settings.supabase_url).rstrip('/')}/realtime/v1/api/broadcast"
        key = settings.supabase_service_role_key.get_secret_value()
        self._headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        self._client = http_client or httpx.Client(timeout=_TIMEOUT)

    def publish(
        self, *, auth_user_id: uuid.UUID, advisory_id: uuid.UUID, state: AdvisoryState
    ) -> None:
        """Broadcast on the patient's private topic.

        Args:
            auth_user_id: Patient's login (topic).
            advisory_id: Advisory.
            state: Band state.

        Raises:
            NotificationDeliveryError: On a network error or a non-2xx answer.
        """
        message = {
            "topic": advisory_topic(auth_user_id),
            "event": ADVISORY_EVENT,
            "payload": {
                "advisory_id": str(advisory_id),
                "state": state,
                "url": advisory_page_url(advisory_id),
            },
            "private": True,
        }
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
        logger.debug("advisory %s broadcast on realtime", advisory_id)
