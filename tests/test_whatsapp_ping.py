"""Tests for scripts.whatsapp_ping: target selection and the guard (no network)."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import ConfigurationError
from scripts import whatsapp_ping
from scripts.whatsapp_ping import resolve_target

MY_PHONE = "+919876543210"
REQUIRED: dict[str, Any] = {
    "supabase_url": "https://abc.supabase.co",
    "supabase_service_role_key": "srk",
    "database_url": "postgresql://u:p@localhost:5432/db",
    "whatsapp_access_token": "t",
    "whatsapp_phone_number_id": "123",
    "whatsapp_verify_token": "v",
    "whatsapp_app_secret": "s",
    "graph_api_version": "v21.0",
}


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED, **overrides})


def test_defaults_to_the_single_allowlisted_number() -> None:
    assert resolve_target(None, make_settings(allowed_test_numbers=[MY_PHONE])) == MY_PHONE


@pytest.mark.parametrize("numbers", [[], [MY_PHONE, "+447700900123"]])
def test_needs_to_flag_unless_exactly_one_allowlisted(numbers: list[str]) -> None:
    with pytest.raises(ConfigurationError, match="--to"):
        resolve_target(None, make_settings(allowed_test_numbers=numbers))


def test_explicit_number_is_normalised_and_validated() -> None:
    assert resolve_target("+91 98765 43210", make_settings()) == MY_PHONE
    with pytest.raises(ConfigurationError, match="--to"):
        resolve_target("98765", make_settings())


def test_blocked_number_exits_1_without_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(allowed_test_numbers=[MY_PHONE])
    monkeypatch.setattr(whatsapp_ping, "get_settings", lambda: settings)
    # The guard raises before any HTTP request, so no network is touched.
    assert whatsapp_ping.main(["--to", "+447700900123"]) == 1


def test_webhook_url_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(public_base_url="https://abc.ngrok-free.app")
    monkeypatch.setattr(whatsapp_ping, "get_settings", lambda: settings)
    assert whatsapp_ping.main(["--webhook-url"]) == 0
    assert capsys.readouterr().out.strip() == "https://abc.ngrok-free.app/webhooks/whatsapp"
    monkeypatch.setattr(whatsapp_ping, "get_settings", make_settings)
    assert whatsapp_ping.main(["--webhook-url"]) == 1  # PUBLIC_BASE_URL not set
