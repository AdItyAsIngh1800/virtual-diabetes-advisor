"""Tests for app.services.supabase_auth and the current_auth_user dependency (mocked HTTP)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from app.api.auth import current_auth_user
from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.services.supabase_auth import AuthUser, verify_access_token

pytestmark = pytest.mark.anyio

REQUIRED: dict[str, Any] = {
    "supabase_url": "https://abc.supabase.co",
    "supabase_service_role_key": "service-key",
    "database_url": "postgresql://u:p@localhost:5432/db",
}
USER_ID = uuid.uuid4()


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED, **overrides})


def mock(response: httpx.Response | Exception, seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_valid_token_returns_the_user() -> None:
    seen: list[httpx.Request] = []
    body = {"id": str(USER_ID), "email": "me@example.com", "aud": "authenticated"}
    client = mock(httpx.Response(200, json=body), seen)
    user = await verify_access_token("tok", settings(supabase_anon_key="anon"), http_client=client)
    assert user == AuthUser(id=USER_ID, email="me@example.com")
    (request,) = seen
    assert str(request.url) == "https://abc.supabase.co/auth/v1/user"
    assert request.headers["Authorization"] == "Bearer tok"
    assert request.headers["apikey"] == "anon"  # publishable key preferred


async def test_service_key_is_the_apikey_fallback() -> None:
    seen: list[httpx.Request] = []
    client = mock(httpx.Response(200, json={"id": str(USER_ID)}), seen)
    await verify_access_token("tok", settings(), http_client=client)
    assert seen[0].headers["apikey"] == "service-key"


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_token(status: int) -> None:
    client = mock(httpx.Response(status, json={"msg": "invalid JWT"}), [])
    with pytest.raises(AuthenticationError, match="sign in again"):
        await verify_access_token("tok", settings(), http_client=client)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, json={"no": "id"}),
        httpx.ConnectError("down"),
    ],
)
async def test_supabase_problems_are_connection_errors(
    response: httpx.Response | Exception,
) -> None:
    with pytest.raises(ConnectionError):
        await verify_access_token("tok", settings(), http_client=mock(response, []))


async def test_dependency_maps_errors_to_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException  # noqa: PLC0415

    async def reject(*_a: Any, **_k: Any) -> AuthUser:
        raise AuthenticationError("expired")

    async def down(*_a: Any, **_k: Any) -> AuthUser:
        raise ConnectionError("down")

    for fake, status in ((reject, 401), (down, 503)):
        monkeypatch.setattr("app.api.auth.verify_access_token", fake)
        with pytest.raises(HTTPException) as info:
            await current_auth_user(settings(), "Bearer tok")
        assert info.value.status_code == status


async def test_owned_client_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []

    class Client(httpx.AsyncClient):
        async def aclose(self) -> None:
            closed.append(True)
            await super().aclose()

    def factory(**_kwargs: Any) -> httpx.AsyncClient:
        return Client(transport=httpx.MockTransport(lambda _r: httpx.Response(401)))

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    with pytest.raises(AuthenticationError):
        await verify_access_token("tok", settings())
    assert closed == [True]
