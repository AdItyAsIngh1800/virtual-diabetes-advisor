"""Web Push subscription routes for the patient web app (R3).

* ``GET /api/push/public-key`` - the VAPID public key (``applicationServerKey``).
* ``POST /api/push/subscribe`` - store this browser's subscription for the signed-in
  patient.
* ``DELETE /api/push/subscribe`` - revoke it ("turn alerts off").

Subscribe/unsubscribe need a Supabase session whose login is linked to a patient
(``users.auth_user_id``); a patient can only ever touch their own subscriptions.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import current_auth_user
from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.db.models import User
from app.db.push_repository import (
    find_patient_by_auth_user,
    revoke_subscription,
    upsert_subscription,
)
from app.db.session import get_session_factory
from app.services.supabase_auth import AuthUser
from app.services.webpush import mask_endpoint

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/push", tags=["push"])

# Browsers emit unpadded base64url; real endpoints are a few hundred characters.
_B64URL: Final = r"^[A-Za-z0-9_-]+={0,2}$"
_MAX_ENDPOINT: Final = 2048
# Kept as a plain https string, not HttpUrl: normalising it could change the text
# and break the exact match DELETE needs.
_ENDPOINT_PATTERN: Final = r"^https://\S+$"
_MAX_USER_AGENT: Final = 512
NOT_LINKED: Final = (
    "this login is not linked to a patient "
    "(run: uv run python -m scripts.seed_demo --my-email <your email>)"
)


class SubscriptionKeys(BaseModel):
    """``keys`` of ``PushSubscription.toJSON()``."""

    # P-256 public key: 65 bytes -> 87 base64url characters; auth secret: 16 -> 22.
    p256dh: str = Field(min_length=80, max_length=100, pattern=_B64URL)
    auth: str = Field(min_length=16, max_length=32, pattern=_B64URL)


class SubscriptionIn(BaseModel):
    """Body of ``POST /api/push/subscribe`` (``PushSubscription.toJSON()``).

    ``expirationTime`` is accepted and ignored: Chrome and Firefox send ``null``, and
    an expired subscription is detected anyway when the push service answers 404/410.
    """

    model_config = ConfigDict(populate_by_name=True)

    endpoint: str = Field(max_length=_MAX_ENDPOINT, pattern=_ENDPOINT_PATTERN)
    keys: SubscriptionKeys
    expiration_time: int | None = Field(default=None, alias="expirationTime")


class UnsubscribeIn(BaseModel):
    """Body of ``DELETE /api/push/subscribe``."""

    endpoint: str = Field(max_length=_MAX_ENDPOINT, pattern=_ENDPOINT_PATTERN)


class PublicKeyOut(BaseModel):
    """VAPID public key for ``pushManager.subscribe``."""

    public_key: str


class SubscribeOut(BaseModel):
    """Result of a subscribe call."""

    subscription_id: uuid.UUID


def _linked_patient(session: Session, auth_user: AuthUser) -> User:
    """Return the patient linked to the login, or refuse.

    Args:
        session: Open session.
        auth_user: Verified Supabase user.

    Returns:
        The patient.

    Raises:
        HTTPException: 403 if the login has no linked active patient.
    """
    patient = find_patient_by_auth_user(session, auth_user.id)
    if patient is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_LINKED)
    return patient


@router.get("/public-key")
def public_key(settings: Annotated[Settings, Depends(get_settings)]) -> PublicKeyOut:
    """Return the VAPID public key.

    Args:
        settings: Application settings.

    Returns:
        The key.

    Raises:
        HTTPException: 503 if the VAPID keys are not configured.
    """
    try:
        return PublicKeyOut(public_key=settings.require_vapid().public_key)
    except ConfigurationError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "web push is not configured"
        ) from exc


@router.post("/subscribe", status_code=status.HTTP_201_CREATED)
def subscribe(
    body: SubscriptionIn,
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    user_agent: Annotated[str | None, Header()] = None,
) -> SubscribeOut:
    """Save this browser's push subscription for the signed-in patient.

    Args:
        body: The browser's subscription.
        auth_user: Verified Supabase user.
        session_factory: Database sessions.
        user_agent: Browser user agent (stored for the dashboard, truncated).

    Returns:
        The subscription id.

    Raises:
        HTTPException: 401 without a valid session, 403 if the login is not linked.
    """
    with session_factory() as session:
        patient = _linked_patient(session, auth_user)
        subscription_id = upsert_subscription(
            session,
            user_id=patient.id,
            endpoint=body.endpoint,
            p256dh=body.keys.p256dh,
            auth=body.keys.auth,
            user_agent=user_agent[:_MAX_USER_AGENT] if user_agent else None,
        )
        session.commit()
    logger.info("push subscription %s saved for user %s", mask_endpoint(body.endpoint), patient.id)
    return SubscribeOut(subscription_id=subscription_id)


@router.delete("/subscribe", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe(
    body: UnsubscribeIn,
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> Response:
    """Revoke this browser's subscription. Idempotent: unknown endpoints also get 204.

    Args:
        body: The endpoint to revoke.
        auth_user: Verified Supabase user.
        session_factory: Database sessions.

    Returns:
        An empty 204 response.

    Raises:
        HTTPException: 401 without a valid session, 403 if the login is not linked.
    """
    with session_factory() as session:
        patient = _linked_patient(session, auth_user)
        revoked = revoke_subscription(
            session, user_id=patient.id, endpoint=body.endpoint, at=datetime.now(UTC)
        )
        session.commit()
    if revoked:
        logger.info(
            "push subscription %s revoked by user %s", mask_endpoint(body.endpoint), patient.id
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
