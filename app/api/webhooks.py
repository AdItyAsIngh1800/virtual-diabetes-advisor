"""WhatsApp Cloud API webhook routes (R3 delivery status, R4 inbound transport).

* ``GET /webhooks/whatsapp`` - the one-off verification handshake Meta runs when you
  save the callback URL in the app dashboard.
* ``POST /webhooks/whatsapp`` - every event. The ``X-Hub-Signature-256`` HMAC is
  checked against the raw body (403 on mismatch), the body is parsed, and the work is
  queued as a background task so Meta gets its 200 straight away. Meta retries
  anything that is not a fast 2xx, so slow work in the request would cause duplicate
  deliveries.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Final

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import get_session_factory
from app.services.whatsapp_webhook import (
    SIGNATURE_HEADER,
    WebhookPayload,
    process_webhook,
    verify_signature,
)

logger = logging.getLogger(__name__)

WEBHOOK_PATH: Final = "/webhooks/whatsapp"

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def webhook_callback_url(settings: Settings) -> str:
    """Return the callback URL to paste into the Meta app dashboard.

    Args:
        settings: Application settings.

    Returns:
        ``{PUBLIC_BASE_URL}/webhooks/whatsapp``.

    Raises:
        ConfigurationError: If ``PUBLIC_BASE_URL`` is not set.
    """
    return settings.require_public_base_url() + WEBHOOK_PATH


@router.get("/whatsapp", response_class=PlainTextResponse)
def verify_webhook(
    settings: Annotated[Settings, Depends(get_settings)],
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> str:
    """Answer Meta's subscription handshake.

    Args:
        settings: Supplies ``WHATSAPP_VERIFY_TOKEN``.
        mode: ``hub.mode``; must be ``subscribe``.
        verify_token: ``hub.verify_token``; must equal the configured token.
        challenge: ``hub.challenge``; echoed back on success.

    Returns:
        The challenge, as plain text.

    Raises:
        HTTPException: 403 if the mode or token is wrong or the challenge is missing.
    """
    expected = settings.whatsapp_verify_token.get_secret_value().encode()
    token_ok = verify_token is not None and hmac.compare_digest(verify_token.encode(), expected)
    if mode != "subscribe" or not token_ok or challenge is None:
        logger.warning("whatsapp webhook verification rejected (mode=%s)", mode)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "verification failed")
    logger.info("whatsapp webhook verified by Meta")
    return challenge


@router.post("/whatsapp", status_code=status.HTTP_200_OK)
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> Response:
    """Verify, parse and queue one webhook delivery.

    A body that is signed correctly but does not match the expected shape is logged
    and acknowledged with 200: Meta would otherwise retry it forever.

    Args:
        request: The raw request (the body is read as bytes for the HMAC).
        background_tasks: Runs :func:`process_webhook` after the response is sent.
        settings: Supplies ``WHATSAPP_APP_SECRET``.
        session_factory: Database sessions for the background task.

    Returns:
        An empty 200 response.

    Raises:
        HTTPException: 403 if ``X-Hub-Signature-256`` is missing or does not match.
    """
    raw_body = await request.body()
    secret = settings.whatsapp_app_secret.get_secret_value()
    if not verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER), secret):
        logger.warning("whatsapp webhook rejected: missing or invalid %s", SIGNATURE_HEADER)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid signature")
    try:
        payload = WebhookPayload.model_validate_json(raw_body)
    except ValidationError as exc:
        # Log locations only: the input values may contain phone numbers and messages.
        locations = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        logger.warning("whatsapp webhook body not understood, ignored: %s", locations)
        return Response(status_code=status.HTTP_200_OK)
    background_tasks.add_task(process_webhook, payload, session_factory)
    return Response(status_code=status.HTTP_200_OK)
