"""Tests for app.core.config: loading, defaults, validation and secret safety."""

from __future__ import annotations

import pytest

from app.core import config
from app.core.config import Settings, load_settings
from app.core.exceptions import ConfigurationError
from scripts.gen_vapid_keys import generate_vapid_keys

SECRET = "sk-test-super-secret-value"

REQUIRED_ENV = {
    "SUPABASE_URL": "https://abc.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": SECRET,
    "DATABASE_URL": "postgresql://postgres:pw@db.abc.supabase.co:5432/postgres",
}
OPTIONAL_ENV = (
    "SUPABASE_ANON_KEY",
    "VAPID_PUBLIC_KEY",
    "VAPID_PRIVATE_KEY",
    "VAPID_SUBJECT",
    "ADVISORY_MAX_AGE_MINUTES",
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
    assert s.advisory_max_age_minutes == 60
    assert (s.vapid_public_key, s.vapid_private_key, s.vapid_subject) == (None, None, None)
    assert s.supabase_anon_key is None


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


def _with_vapid(env: pytest.MonkeyPatch) -> tuple[str, str]:
    keys = generate_vapid_keys()
    env.setenv("VAPID_PUBLIC_KEY", keys.public_key)
    env.setenv("VAPID_PRIVATE_KEY", keys.private_key)
    env.setenv("VAPID_SUBJECT", "mailto:team6@example.com")
    return keys


def test_vapid_settings_load_and_hide_the_private_key(env: pytest.MonkeyPatch) -> None:
    public, private = _with_vapid(env)
    vapid = load_settings(env_file=None).require_vapid()
    assert vapid.public_key == public
    assert vapid.private_key.get_secret_value() == private
    assert vapid.subject == "mailto:team6@example.com"
    assert private not in repr(load_settings(env_file=None))


def test_require_vapid_names_every_missing_variable(env: pytest.MonkeyPatch) -> None:
    env.setenv("VAPID_SUBJECT", "mailto:team6@example.com")
    with pytest.raises(ConfigurationError, match="VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY"):
        load_settings(env_file=None).require_vapid()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("VAPID_PUBLIC_KEY", "not-base64!"),
        ("VAPID_PUBLIC_KEY", "AAAA"),  # valid base64url, wrong length
        ("VAPID_PRIVATE_KEY", "c2hvcnQ"),  # 5 bytes, not 32
        ("VAPID_SUBJECT", "team6@example.com"),  # missing mailto:
        ("VAPID_SUBJECT", "http://example.com"),  # must be https
    ],
)
def test_vapid_values_are_validated(env: pytest.MonkeyPatch, variable: str, value: str) -> None:
    _with_vapid(env)
    env.setenv(variable, value)
    with pytest.raises(ConfigurationError, match=variable) as info:
        load_settings(env_file=None)
    assert value not in str(info.value)


@pytest.mark.parametrize("minutes", ["4", "1441"])
def test_advisory_max_age_bounds(env: pytest.MonkeyPatch, minutes: str) -> None:
    env.setenv("ADVISORY_MAX_AGE_MINUTES", minutes)
    with pytest.raises(ConfigurationError, match="ADVISORY_MAX_AGE_MINUTES"):
        load_settings(env_file=None)


def test_supabase_anon_key_optional(env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError, match="SUPABASE_ANON_KEY"):
        load_settings(env_file=None).require_supabase_anon_key()
    env.setenv("SUPABASE_ANON_KEY", "sb_publishable_abc")
    assert load_settings(env_file=None).require_supabase_anon_key() == "sb_publishable_abc"


@pytest.mark.parametrize("required", sorted(REQUIRED_ENV))
def test_each_required_variable_is_enforced(env: pytest.MonkeyPatch, required: str) -> None:
    """Supabase settings stay mandatory."""
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
    env.delenv("DATABASE_URL")
    env.setenv("PUBLIC_BASE_URL", "not a url")
    with pytest.raises(ConfigurationError) as info:
        load_settings(env_file=None)
    message = str(info.value)
    assert "DATABASE_URL" in message
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
