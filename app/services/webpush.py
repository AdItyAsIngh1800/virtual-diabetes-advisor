"""Web Push delivery with ``pywebpush`` (R3), behind :class:`NotificationChannel`.

:class:`WebPushChannel` sends one :class:`~app.services.notifications.Notification` to
every active browser subscription of a patient:

* runs the recipient guard first (no I/O for a blocked demo patient);
* sets ``TTL`` and ``Urgency`` from the notification (RFC 8030);
* revokes a subscription the push service answers with 404/410 (gone for good);
* retries 5xx / 429 / connection errors with exponential backoff, at most
  ``max_attempts`` attempts per device (default 3), honouring ``Retry-After``;
* never logs keys or full endpoints (an endpoint plus its keys lets anyone push).

The HTTP call goes through a :class:`PushSender`; :class:`PywebpushSender` is the real
one, tests pass a fake. ``pywebpush`` is synchronous (``requests``), which fits the
batch job's worker thread; call it via ``asyncio.to_thread`` from async code.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final, NamedTuple, Protocol
from urllib.parse import urlsplit

import requests
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from app.core.config import VapidConfig
from app.core.exceptions import NotificationDeliveryError
from app.db.models import NotificationChannelName
from app.db.push_repository import StoredSubscription
from app.services.notifications import (
    DeliveryReport,
    Notification,
    Recipient,
    Urgency,
    check_recipient,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS: Final = 3
# Backoff 0.5 s, 1 s, 2 s ... capped at 8 s, plus up to 0.5 s jitter so devices that
# hit the same push-service rate limit do not retry in lockstep.
DEFAULT_BACKOFF_BASE_S: Final = 0.5
DEFAULT_MAX_BACKOFF_S: Final = 8.0
# Never block the batch worker longer than this for a server-requested Retry-After.
_MAX_RETRY_AFTER_S: Final = 30.0
# Push services answer within a second or two; a hung connection must not stall a run.
DEFAULT_TIMEOUT_S: Final = 10.0

# RFC 8030 section 5: 201 Created is the normal answer; some services answer 200/202.
_ACCEPTED: Final = frozenset({200, 201, 202})
# 404 Not Found / 410 Gone: the subscription expired or the user revoked permission.
_GONE: Final = frozenset({404, 410})
_TOO_MANY_REQUESTS: Final = 429
_SERVER_ERROR: Final = 500


class PushResponse(NamedTuple):
    """The parts of a push-service response the channel acts on.

    Attributes:
        status_code: HTTP status.
        location: ``Location`` header (the push-service message id), if any.
        retry_after: Seconds from a numeric ``Retry-After`` header, if any.
    """

    status_code: int
    location: str | None = None
    retry_after: float | None = None


class PushSender(Protocol):
    """Encrypts and POSTs one message to one subscription."""

    def __call__(
        self, subscription: StoredSubscription, *, data: str, ttl: int, urgency: Urgency
    ) -> PushResponse:
        """Send one message.

        Args:
            subscription: Target browser subscription.
            data: Plain-text payload (encrypted by the sender).
            ttl: ``TTL`` header, seconds.
            urgency: ``Urgency`` header.

        Returns:
            The push service's answer (any status; the channel interprets it).

        Raises:
            requests.ConnectionError: If the push service cannot be reached.
            requests.Timeout: If it does not answer in time.
            NotificationDeliveryError: If the message cannot be built or sent.
        """
        ...


class SubscriptionStore(Protocol):
    """Where the channel reads and updates subscriptions."""

    def active_for_user(self, user_id: uuid.UUID) -> list[StoredSubscription]:
        """Return the patient's active subscriptions.

        Args:
            user_id: Patient id.

        Returns:
            Subscriptions that are not revoked.
        """
        ...

    def revoke(self, subscription_id: uuid.UUID, at: datetime) -> None:
        """Mark a subscription dead.

        Args:
            subscription_id: Subscription id.
            at: When.
        """
        ...

    def mark_success(self, subscription_id: uuid.UUID, at: datetime) -> None:
        """Record an accepted push.

        Args:
            subscription_id: Subscription id.
            at: When.
        """
        ...


def mask_endpoint(endpoint: str) -> str:
    """Shorten an endpoint for logs: push-service host plus the last 6 characters.

    Args:
        endpoint: Full push-service URL.

    Returns:
        E.g. ``fcm.googleapis.com/...a1b2c3``.
    """
    return f"{urlsplit(endpoint).netloc}/...{endpoint[-6:]}"


def _retry_after_seconds(raw: str | None) -> float | None:
    """Parse a numeric ``Retry-After`` header, capped at 30 s.

    Args:
        raw: Header value.

    Returns:
        Seconds, or ``None`` when missing or not a number (HTTP-date form is ignored).
    """
    if raw is None:
        return None
    try:
        return min(max(float(raw), 0.0), _MAX_RETRY_AFTER_S)
    except ValueError:
        return None


class PywebpushSender:
    """The real :class:`PushSender`: ``pywebpush`` with VAPID signing.

    Args:
        vapid: Keys and subject from :meth:`Settings.require_vapid`.
        timeout_s: Per-request timeout.
        session: Optional ``requests.Session`` (connection reuse; tests pass a fake).

    Raises:
        ValueError: If the private key cannot be loaded.
    """

    def __init__(
        self,
        vapid: VapidConfig,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        session: requests.Session | None = None,
    ) -> None:
        # Parse the key once; pywebpush would otherwise re-parse it on every send.
        self._vapid: Vapid = Vapid.from_string(private_key=vapid.private_key.get_secret_value())
        self._subject = vapid.subject
        self._timeout_s = timeout_s
        self._session = session

    def __call__(
        self, subscription: StoredSubscription, *, data: str, ttl: int, urgency: Urgency
    ) -> PushResponse:
        """Encrypt (aes128gcm), sign (VAPID) and POST one message.

        Args:
            subscription: Target browser subscription.
            data: Plain-text payload.
            ttl: ``TTL`` header, seconds.
            urgency: ``Urgency`` header.

        Returns:
            The push service's answer, including 4xx/5xx statuses.

        Raises:
            NotificationDeliveryError: If pywebpush fails without an HTTP response.
        """
        info = {
            "endpoint": subscription.endpoint,
            "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
        }
        try:
            # pywebpush fills in "aud" and "exp"; pass a fresh dict because it mutates it.
            response: requests.Response = webpush(
                info,
                data=data,
                vapid_private_key=self._vapid,
                vapid_claims={"sub": self._subject},
                ttl=ttl,
                headers={"Urgency": urgency},
                timeout=self._timeout_s,
                requests_session=self._session,
            )
        except WebPushException as exc:
            # Raised for any status > 202; the response is what the channel needs.
            if exc.response is None:
                raise NotificationDeliveryError("web push failed before a response") from exc
            response = exc.response
        return PushResponse(
            status_code=int(response.status_code),
            location=response.headers.get("Location"),
            retry_after=_retry_after_seconds(response.headers.get("Retry-After")),
        )


class WebPushChannel:
    """Web Push implementation of :class:`~app.services.notifications.NotificationChannel`.

    Args:
        store: Subscription storage.
        sender: Performs the HTTP call (:class:`PywebpushSender` in production).
        sleep: Sleep between retries (tests pass a recorder).
        clock: Current time, for revoke/success timestamps.
        max_attempts: Attempts per device, including the first (>= 1).
        backoff_base_s: First backoff delay; doubles each retry.
        max_backoff_s: Upper bound of one backoff delay.

    Raises:
        ValueError: If ``max_attempts`` is below 1.
    """

    name: NotificationChannelName = "webpush"

    def __init__(
        self,
        store: SubscriptionStore,
        sender: PushSender,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._store = store
        self._sender = sender
        self._sleep = sleep
        self._clock = clock
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s
        self._max_backoff_s = max_backoff_s

    def send(self, recipient: Recipient, notification: Notification) -> DeliveryReport:
        """Push ``notification`` to every active subscription of ``recipient``.

        Args:
            recipient: Target patient.
            notification: What to show.

        Returns:
            Per-device outcome counts; ``delivered`` is ``False`` when no device
            accepted it (including a patient with no subscriptions).

        Raises:
            RecipientBlockedError: If the guard blocks the recipient.
        """
        # R3 safety guard, before reading subscriptions or touching the network.
        check_recipient(recipient)
        report = DeliveryReport(channel=self.name)
        subscriptions = self._store.active_for_user(recipient.user_id)
        if not subscriptions:
            logger.info("user %s has no active push subscription", recipient.user_id)
            report.errors.append("no active push subscription")
            return report
        data = notification.payload()
        for subscription in subscriptions:
            report.attempted += 1
            self._send_one(subscription, data, notification, report)
        logger.info(
            "web push to user %s: %d sent, %d revoked, %d failed",
            recipient.user_id,
            report.sent,
            report.revoked,
            report.failed,
        )
        return report

    def _send_one(
        self,
        subscription: StoredSubscription,
        data: str,
        notification: Notification,
        report: DeliveryReport,
    ) -> None:
        """Deliver to one device with retries, updating ``report`` and the store.

        Args:
            subscription: Device.
            data: Payload.
            notification: Supplies TTL and urgency.
            report: Updated in place.
        """
        where = mask_endpoint(subscription.endpoint)
        for attempt in range(1, self._max_attempts + 1):
            retry_after: float | None = None
            try:
                response = self._sender(
                    subscription,
                    data=data,
                    ttl=notification.ttl_seconds,
                    urgency=notification.urgency,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                error, retryable = f"{type(exc).__name__}", True
            except (NotificationDeliveryError, ValueError) as exc:
                # Malformed subscription keys or a signing problem: retrying cannot help.
                error, retryable = f"{type(exc).__name__}: {exc}", False
            else:
                status = response.status_code
                if status in _ACCEPTED:
                    report.sent += 1
                    if response.location:
                        report.message_ids.append(response.location)
                    self._store.mark_success(subscription.id, self._clock())
                    return
                if status in _GONE:
                    report.revoked += 1
                    self._store.revoke(subscription.id, self._clock())
                    logger.info("push subscription %s gone (HTTP %d); revoked", where, status)
                    return
                error = f"HTTP {status}"
                retryable = status == _TOO_MANY_REQUESTS or status >= _SERVER_ERROR
                retry_after = response.retry_after

            if not retryable or attempt == self._max_attempts:
                report.failed += 1
                report.errors.append(error)
                logger.warning(
                    "web push to %s failed: %s (attempt %d/%d)",
                    where,
                    error,
                    attempt,
                    self._max_attempts,
                )
                return
            delay = self._backoff_delay(attempt, retry_after)
            logger.warning(
                "web push to %s: %s (attempt %d/%d); retrying in %.1fs",
                where,
                error,
                attempt,
                self._max_attempts,
                delay,
            )
            self._sleep(delay)

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        """Seconds to wait before the next attempt.

        Args:
            attempt: The attempt that just failed (1-based).
            retry_after: Server-requested wait, if any.

        Returns:
            ``Retry-After`` when given, else exponential backoff plus jitter.
        """
        if retry_after is not None:
            return retry_after
        exponential = min(self._backoff_base_s * 2.0 ** (attempt - 1), self._max_backoff_s)
        # Jitter only spreads retries out; it is not security-sensitive.
        return exponential + random.uniform(0, self._backoff_base_s)  # noqa: S311
