"""Tests for app.core.config: loading, defaults, validation and secret safety."""

from __future__ import annotations

import pytest

from app.core import config
from app.core.config import Settings, load_settings
from app.core.exceptions import ConfigurationError

SECRET = "sk-test-super-secret-value"

REQUIRED_ENV = {
    "SUPABASE_URL": "https://abc.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": SECRET,
    "DATABASE_URL": "postgresql://postgres:pw@db.abc.supabase.co:5432/postgres",
    "WHATSAPP_ACCESS_TOKEN": "wa-token",
    "WHATSAPP_PHONE_NUMBER_ID": "123456789012345",
    "WHATSAPP_VERIFY_TOKEN": "verify-me",
    "WHATSAPP_APP_SECRET": "app-secret",
    "GRAPH_API_VERSION": "v21.0",
}
OPTIONAL_ENV = (
    "WHATSAPP_TEST_MODE",
    "ALLOWED_TEST_NUMBERS",
    "PUBLIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "PRE_BREACH_BAND",
    "ADVISORY_COOLDOWN_HOURS",
    "ADMIN_API_KEY",
    "BATCH_INTERVAL_MINUTES",
    "BATCH_SCHEDULER_ENABLED",
    "LOG_LEVEL",
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Set every required variable and clear optional ones; never touch the real .env."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    for key in OPTIONAL_ENV:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_loads_every_variable(env: pytest.MonkeyPatch) -> None:
    s = load_settings(env_file=None)
    assert str(s.supabase_url).startswith("https://abc.supabase.co")
    assert s.supabase_service_role_key.get_secret_value() == SECRET
    assert s.database_url.get_secret_value().startswith("postgresql://")
    assert s.whatsapp_access_token.get_secret_value() == "wa-token"
    assert s.whatsapp_phone_number_id == "123456789012345"
    assert s.whatsapp_verify_token.get_secret_value() == "verify-me"
    assert s.whatsapp_app_secret.get_secret_value() == "app-secret"
    assert s.graph_api_version == "v21.0"
    assert s.public_base_url is None
    assert s.anthropic_api_key is None


def test_every_settings_field_is_covered_by_this_test() -> None:
    """Fails if a field is added to Settings without updating REQUIRED/OPTIONAL_ENV."""
    expected = {k.lower() for k in REQUIRED_ENV} | {k.lower() for k in OPTIONAL_ENV}
    assert set(Settings.model_fields) == expected


def test_defaults(env: pytest.MonkeyPatch) -> None:
    s = load_settings(env_file=None)
    assert s.pre_breach_band == 0.05
    assert s.advisory_cooldown_hours == 24
    assert s.batch_interval_minutes == 15
    assert s.batch_scheduler_enabled is True
    assert s.admin_api_key is None
    assert s.log_level == "INFO"
    assert s.whatsapp_test_mode is True
    assert s.allowed_test_numbers == ()


def test_defaults_can_be_overridden(env: pytest.MonkeyPatch) -> None:
    env.setenv("PRE_BREACH_BAND", "0.1")
    env.setenv("ADVISORY_COOLDOWN_HOURS", "6")
    s = load_settings(env_file=None)
    assert s.pre_breach_band == 0.1
    assert s.advisory_cooldown_hours == 6


@pytest.mark.parametrize("band", ["0", "-0.05", "0.5", "1"])
def test_pre_breach_band_bounds(env: pytest.MonkeyPatch, band: str) -> None:
    env.setenv("PRE_BREACH_BAND", band)
    with pytest.raises(ConfigurationError, match="PRE_BREACH_BAND"):
        load_settings(env_file=None)


@pytest.mark.parametrize("hours", ["0", "-1", "169"])
def test_cooldown_bounds(env: pytest.MonkeyPatch, hours: str) -> None:
    env.setenv("ADVISORY_COOLDOWN_HOURS", hours)
    with pytest.raises(ConfigurationError, match="ADVISORY_COOLDOWN_HOURS"):
        load_settings(env_file=None)


@pytest.mark.parametrize(
    ("raw", "expected"), [("21.0", "v21.0"), ("v22.0", "v22.0"), (" v23.1 ", "v23.1")]
)
def test_graph_version_normalised(env: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    env.setenv("GRAPH_API_VERSION", raw)
    s = load_settings(env_file=None)
    assert s.graph_api_version == expected
    assert s.graph_api_base_url == f"https://graph.facebook.com/{expected}"


@pytest.mark.parametrize("raw", ["latest", "v21", "21.0.1", ""])
def test_graph_version_rejected(env: pytest.MonkeyPatch, raw: str) -> None:
    env.setenv("GRAPH_API_VERSION", raw)
    with pytest.raises(ConfigurationError, match="GRAPH_API_VERSION"):
        load_settings(env_file=None)


def test_phone_number_id_must_be_digits(env: pytest.MonkeyPatch) -> None:
    env.setenv("WHATSAPP_PHONE_NUMBER_ID", "abc123")
    with pytest.raises(ConfigurationError, match="WHATSAPP_PHONE_NUMBER_ID"):
        load_settings(env_file=None)


@pytest.mark.parametrize("required", sorted(REQUIRED_ENV))
def test_each_required_variable_is_enforced(env: pytest.MonkeyPatch, required: str) -> None:
    """Supabase, WhatsApp and Anthropic settings stay mandatory."""
    env.delenv(required)
    with pytest.raises(ConfigurationError, match=required):
        load_settings(env_file=None)


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_public_base_url_optional(env: pytest.MonkeyPatch, blank: str | None) -> None:
    if blank is not None:
        env.setenv("PUBLIC_BASE_URL", blank)  # e.g. "PUBLIC_BASE_URL=" left empty in .env
    s = load_settings(env_file=None)
    assert s.public_base_url is None
    with pytest.raises(ConfigurationError, match="PUBLIC_BASE_URL"):
        s.require_public_base_url()


def test_require_public_base_url_strips_trailing_slash(env: pytest.MonkeyPatch) -> None:
    env.setenv("PUBLIC_BASE_URL", "https://advisor.example.com/")
    s = load_settings(env_file=None)
    assert s.require_public_base_url() == "https://advisor.example.com"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_anthropic_api_key_optional(env: pytest.MonkeyPatch, blank: str | None) -> None:
    if blank is not None:
        env.setenv("ANTHROPIC_API_KEY", blank)  # e.g. "ANTHROPIC_API_KEY=" left empty in .env
    s = load_settings(env_file=None)
    assert s.anthropic_api_key is None
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        s.require_anthropic_api_key()


def test_require_anthropic_api_key_returns_wrapped_secret(env: pytest.MonkeyPatch) -> None:
    env.setenv("ANTHROPIC_API_KEY", SECRET)
    s = load_settings(env_file=None)
    key = s.require_anthropic_api_key()
    assert key.get_secret_value() == SECRET
    assert SECRET not in repr(s)
    assert SECRET not in str(key)


def test_public_base_url_still_validated_when_set(env: pytest.MonkeyPatch) -> None:
    env.setenv("PUBLIC_BASE_URL", "not a url")
    with pytest.raises(ConfigurationError, match="PUBLIC_BASE_URL"):
        load_settings(env_file=None)


def test_missing_variable_named_without_leaking_secrets(env: pytest.MonkeyPatch) -> None:
    env.delenv("WHATSAPP_ACCESS_TOKEN")
    env.setenv("PUBLIC_BASE_URL", "not a url")
    with pytest.raises(ConfigurationError) as info:
        load_settings(env_file=None)
    message = str(info.value)
    assert "WHATSAPP_ACCESS_TOKEN" in message
    assert "PUBLIC_BASE_URL" in message
    assert SECRET not in message
    # The pydantic error (which embeds input values) must not be chained.
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_secrets_hidden_in_repr(env: pytest.MonkeyPatch) -> None:
    s = load_settings(env_file=None)
    assert SECRET not in repr(s)
    assert SECRET not in str(s)


def test_settings_are_immutable(env: pytest.MonkeyPatch) -> None:
    s = load_settings(env_file=None)
    with pytest.raises(ValueError, match="frozen"):
        s.pre_breach_band = 0.2  # type: ignore[misc]


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch, env: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    stub = load_settings(env_file=None)

    def fake_load() -> Settings:
        calls.append(1)
        return stub

    monkeypatch.setattr(config, "load_settings", fake_load)
    config.get_settings.cache_clear()
    try:
        assert config.get_settings() is config.get_settings() is stub
        assert len(calls) == 1
    finally:
        config.get_settings.cache_clear()


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_admin_api_key_is_unset(env: pytest.MonkeyPatch, blank: str) -> None:
    env.setenv("ADMIN_API_KEY", blank)
    s = load_settings(env_file=None)
    with pytest.raises(ConfigurationError, match="ADMIN_API_KEY"):
        s.require_admin_api_key()


def test_require_admin_api_key_returns_wrapped_secret(env: pytest.MonkeyPatch) -> None:
    env.setenv("ADMIN_API_KEY", SECRET)
    s = load_settings(env_file=None)
    assert s.require_admin_api_key().get_secret_value() == SECRET
    assert SECRET not in repr(s)


@pytest.mark.parametrize("minutes", ["0", "1441"])
def test_batch_interval_bounds(env: pytest.MonkeyPatch, minutes: str) -> None:
    env.setenv("BATCH_INTERVAL_MINUTES", minutes)
    with pytest.raises(ConfigurationError, match="BATCH_INTERVAL_MINUTES"):
        load_settings(env_file=None)
