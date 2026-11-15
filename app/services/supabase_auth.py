"""Verify Supabase Auth sessions from the patient web app (R3/R4 API auth).

The browser signs in with Supabase Auth and sends ``Authorization: Bearer <access
token>``. Instead of holding the project's JWT secret, the backend asks Supabase Auth
itself (``GET /auth/v1/user``), which validates signature, expiry and revocation and
returns the user. One extra request per call is fine at this project's scale.
"""

from __future__ import annotations

import logging
import uuid
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import Settings
from app.core.exceptions import AuthenticationError

logger = logging.getLogger(__name__)

_TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)
_HTTP_OK: Final = 200
# 401/403: Supabase rejected the token (expired, malformed, signed out).
_REJECTED: Final = frozenset({401, 403})


class AuthUser(BaseModel):
    """The signed-in Supabase user.

    Attributes:
        id: ``auth.users.id`` (matches ``users.auth_user_id``).
        email: Login email, if any. PII: do not log it.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    email: str | None = None


async def verify_access_token(
    token: str, settings: Settings, *, http_client: httpx.AsyncClient | None = None
) -> AuthUser:
    """Resolve a Supabase access token to its user.

    Args:
        token: The bearer token from the browser.
        settings: Supplies the Supabase URL and an API key for the ``apikey`` header.
        http_client: Optional client (tests pass one with a mock transport).

    Returns:
        The authenticated user.

    Raises:
        AuthenticationError: If Supabase rejects the token.
        ConnectionError: If Supabase Auth cannot be reached or answers unexpectedly.
    """
    # Supabase requires a project key on every Auth request; the publishable key is
    # enough for /user, the service key is the fallback before SUPABASE_ANON_KEY is set.
    api_key = settings.supabase_anon_key or settings.supabase_service_role_key.get_secret_value()
    url = f"{str(settings.supabase_url).rstrip('/')}/auth/v1/user"
    headers = {"apikey": api_key, "Authorization": f"Bearer {token}"}
    client = http_client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ConnectionError(f"Supabase Auth unreachable ({type(exc).__name__})") from exc
    finally:
        if http_client is None:
            await client.aclose()
    if response.status_code in _REJECTED:
        raise AuthenticationError("session is invalid or expired; sign in again")
    if response.status_code != _HTTP_OK:
        logger.error("Supabase Auth /user answered HTTP %d", response.status_code)
        raise ConnectionError(f"Supabase Auth answered HTTP {response.status_code}")
    try:
        return AuthUser.model_validate_json(response.content)
    except ValidationError as exc:
        raise ConnectionError("Supabase Auth returned an unexpected user body") from exc
