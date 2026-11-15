"""FastAPI dependency: the Supabase user behind ``Authorization: Bearer <token>``."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError
from app.services.supabase_auth import AuthUser, verify_access_token

_BEARER = "bearer "


async def current_auth_user(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> AuthUser:
    """Verify the request's Supabase access token.

    Args:
        settings: Application settings.
        authorization: ``Authorization`` header.

    Returns:
        The signed-in user.

    Raises:
        HTTPException: 401 if the header is missing or the token is rejected, 503 if
            Supabase Auth cannot be reached.
    """
    token = ""
    if authorization is not None and authorization.lower().startswith(_BEARER):
        token = authorization[len(_BEARER) :].strip()
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "sign in first",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await verify_access_token(token, settings)
    except AuthenticationError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, str(exc), headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    except ConnectionError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "auth service unavailable"
        ) from exc
