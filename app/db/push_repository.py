"""Database access for Web Push subscriptions (R3).

:class:`SqlSubscriptionStore` is what :class:`app.services.webpush.WebPushChannel`
uses while sending; the module functions serve the ``/api/push`` routes. Callers own
transactions, except the store, which commits each revoke/success on its own so a
dead subscription is recorded even if a later device fails.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import PushSubscription, User


class StoredSubscription(BaseModel):
    """An active subscription, as the sender needs it.

    Attributes:
        id: ``push_subscriptions.id``.
        endpoint: Push-service URL (secret: never log it in full).
        p256dh: Browser public key (base64url).
        auth: Browser auth secret (base64url).
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    endpoint: str
    p256dh: str
    auth: str


class SqlSubscriptionStore:
    """Subscription storage backed by ``push_subscriptions``.

    Args:
        session_factory: Creates one short session per call.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def active_for_user(self, user_id: uuid.UUID) -> list[StoredSubscription]:
        """Return the patient's subscriptions that are not revoked, oldest first.

        Args:
            user_id: Patient.

        Returns:
            Active subscriptions.
        """
        with self._session_factory() as session:
            rows = session.scalars(
                select(PushSubscription)
                .where(PushSubscription.user_id == user_id, PushSubscription.revoked_at.is_(None))
                .order_by(PushSubscription.created_at)
            )
            return [
                StoredSubscription(id=r.id, endpoint=r.endpoint, p256dh=r.p256dh, auth=r.auth)
                for r in rows
            ]

    def revoke(self, subscription_id: uuid.UUID, at: datetime) -> None:
        """Mark a subscription dead (the push service answered 404/410).

        Args:
            subscription_id: Subscription to revoke.
            at: Time of the failed push.
        """
        with self._session_factory() as session:
            session.execute(
                update(PushSubscription)
                .where(
                    PushSubscription.id == subscription_id, PushSubscription.revoked_at.is_(None)
                )
                .values(revoked_at=at)
            )
            session.commit()

    def mark_success(self, subscription_id: uuid.UUID, at: datetime) -> None:
        """Record that a push to this subscription was accepted.

        Args:
            subscription_id: Subscription.
            at: Time of the accepted push.
        """
        with self._session_factory() as session:
            session.execute(
                update(PushSubscription)
                .where(PushSubscription.id == subscription_id)
                .values(last_success_at=at)
            )
            session.commit()


def find_patient_by_auth_user(session: Session, auth_user_id: uuid.UUID) -> User | None:
    """Return the active patient linked to a Supabase login.

    Args:
        session: Open session.
        auth_user_id: ``auth.users.id`` from a verified session.

    Returns:
        The patient, or ``None`` if the login is not linked to an active patient.
    """
    return session.scalar(
        select(User).where(
            User.auth_user_id == auth_user_id, User.role == "patient", User.is_active
        )
    )


def find_staff_by_auth_user(session: Session, auth_user_id: uuid.UUID) -> User | None:
    """Return the active practitioner/admin user linked to a Supabase login.

    Practitioners subscribe to Web Push to hear about new handoffs (R4).

    Args:
        session: Open session.
        auth_user_id: ``auth.users.id`` from a verified session.

    Returns:
        The user, or ``None``.
    """
    return session.scalar(
        select(User).where(
            User.auth_user_id == auth_user_id,
            User.role.in_(("practitioner", "admin")),
            User.is_active,
        )
    )


def upsert_subscription(
    session: Session,
    *,
    user_id: uuid.UUID,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None,
) -> uuid.UUID:
    """Store a browser subscription, or refresh it if the endpoint is already known.

    An endpoint identifies one browser profile on one device. If a different patient
    signs in on that browser and enables alerts, the row moves to them: the device
    should alert whoever is signed in now. Re-subscribing also clears ``revoked_at``.

    Args:
        session: Open session (caller commits).
        user_id: Patient enabling alerts.
        endpoint: Push-service URL.
        p256dh: Browser public key.
        auth: Browser auth secret.
        user_agent: Browser user agent, for the dashboard (optional).

    Returns:
        The subscription id.
    """
    stmt = (
        pg_insert(PushSubscription)
        .values(user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth, user_agent=user_agent)
        .on_conflict_do_update(
            index_elements=[PushSubscription.endpoint],
            set_={
                "user_id": user_id,
                "p256dh": p256dh,
                "auth": auth,
                "user_agent": user_agent,
                "revoked_at": None,
            },
        )
        .returning(PushSubscription.id)
    )
    return session.execute(stmt).scalar_one()


def revoke_subscription(
    session: Session, *, user_id: uuid.UUID, endpoint: str, at: datetime
) -> bool:
    """Revoke the patient's own subscription for ``endpoint`` (alerts turned off).

    Args:
        session: Open session (caller commits).
        user_id: Patient (a patient can never revoke someone else's device).
        endpoint: Push-service URL from the browser.
        at: Revocation time.

    Returns:
        ``True`` if an active subscription was revoked.
    """
    result = session.execute(
        update(PushSubscription)
        .where(
            PushSubscription.user_id == user_id,
            PushSubscription.endpoint == endpoint,
            PushSubscription.revoked_at.is_(None),
        )
        .values(revoked_at=at)
        .returning(PushSubscription.id)
    )
    return result.first() is not None
