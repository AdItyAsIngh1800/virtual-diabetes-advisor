"""Typed application settings, loaded from environment variables and ``.env``.

This is the only module allowed to read secrets. Everything else calls
:func:`get_settings` and receives a validated, immutable :class:`Settings`.
Secrets are wrapped in :class:`pydantic.SecretStr` so they print as ``**********``
if a settings object is ever logged or shown in a traceback.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.exceptions import ConfigurationError

# Repo root: app/core/config.py -> parents[2]. Anchoring the path means the app finds
# .env no matter which directory uvicorn / pytest / the batch job is started from.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

# Meta's Graph API host; only the version segment is configurable (GRAPH_API_VERSION).
GRAPH_API_HOST = "https://graph.facebook.com"

# Meta writes versions as "v21.0"; accept "21.0" too so either .env style works.
_GRAPH_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)$")

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """All runtime configuration for the diabetes advisor.

    Field names map case-insensitively to environment variables
    (``supabase_url`` <- ``SUPABASE_URL``). Real environment variables take
    precedence over values in ``.env``.

    Attributes:
        supabase_url: Supabase project URL.
        supabase_service_role_key: Server-side Supabase key (bypasses RLS - backend only).
        database_url: Postgres connection string used by SQLAlchemy / psycopg.
        whatsapp_access_token: WhatsApp Cloud API bearer token.
        whatsapp_phone_number_id: Sender phone-number ID from the Meta app dashboard.
        whatsapp_verify_token: Shared token Meta echoes during webhook verification.
        whatsapp_app_secret: App secret used to verify ``X-Hub-Signature-256`` on webhooks.
        graph_api_version: Graph API version, normalised to ``vMAJOR.MINOR``.
        public_base_url: Public HTTPS URL of this API (webhook callback / media links).
            Optional until Phase 5 webhook setup; read it via
            :meth:`require_public_base_url` there.
        anthropic_api_key: API key for the Claude-backed help chat.
        pre_breach_band: Fraction of a limit that defines the pre-breach band (R3).
        advisory_cooldown_hours: Minimum hours between repeat advisories (R3 de-dup).
        log_level: Root log level.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Unknown keys in .env (e.g. tooling vars) are ignored rather than crashing startup.
        extra="ignore",
        frozen=True,
    )

    # --- Supabase -------------------------------------------------------------
    supabase_url: HttpUrl
    supabase_service_role_key: SecretStr
    # SecretStr, not a DSN type: the connection string embeds the DB password.
    database_url: SecretStr

    # --- WhatsApp Cloud API (R3 delivery, R4 transport) -------------------------
    whatsapp_access_token: SecretStr
    whatsapp_phone_number_id: str = Field(pattern=r"^\d+$")
    whatsapp_verify_token: SecretStr
    whatsapp_app_secret: SecretStr
    graph_api_version: str

    # --- Public URL + LLM ------------------------------------------------------
    # Optional: only the Phase 5 webhook/media setup needs a public URL, so local
    # dev, tests and batch jobs must start without one.
    public_base_url: HttpUrl | None = None
    anthropic_api_key: SecretStr

    # --- Threshold engine / advisory behaviour ---------------------------------
    # R3: pre-breach band. 0.05 => warn when a reading is within 5% of a limit
    # (0.95*U <= x < U, or L < x <= 1.05*L). Product choice from the project brief,
    # not a clinical constant. Capped below 0.5 so the two bands of a limit pair can
    # never swallow the whole target range.
    pre_breach_band: float = Field(default=0.05, gt=0.0, lt=0.5)
    # R3: one advisory per user per metric per state per N hours. 24 h chosen by the
    # team to avoid alert fatigue; a *breach* still escalates to the practitioner.
    advisory_cooldown_hours: int = Field(default=24, ge=1, le=24 * 7)

    log_level: LogLevel = "INFO"

    @field_validator("graph_api_version")
    @classmethod
    def _normalise_graph_version(cls, value: str) -> str:
        """Normalise ``21.0`` / ``v21.0`` to ``v21.0``.

        Args:
            value: Raw ``GRAPH_API_VERSION`` value.

        Returns:
            The version with a leading ``v``.

        Raises:
            ValueError: If the value is not of the form ``[v]MAJOR.MINOR``.
        """
        match = _GRAPH_VERSION_PATTERN.match(value.strip())
        if match is None:
            raise ValueError("GRAPH_API_VERSION must look like 'v21.0' or '21.0'")
        return f"v{match.group(1)}.{match.group(2)}"

    @field_validator("public_base_url", mode="before")
    @classmethod
    def _blank_url_is_unset(cls, value: object) -> object:
        """Treat a blank ``PUBLIC_BASE_URL=`` line in ``.env`` as not set.

        Args:
            value: Raw value from the environment.

        Returns:
            ``None`` for an empty or whitespace-only string, otherwise ``value``.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def graph_api_base_url(self) -> str:
        """Versioned Graph API base URL, e.g. ``https://graph.facebook.com/v21.0``.

        Returns:
            The base URL without a trailing slash.
        """
        return f"{GRAPH_API_HOST}/{self.graph_api_version}"

    def require_public_base_url(self) -> str:
        """Return the public base URL, failing loudly if it is not configured.

        Call this from code that genuinely needs a public URL (Phase 5 webhook
        registration, media links) so the error points at the missing variable.

        Returns:
            The URL without a trailing slash, e.g. ``https://advisor.example.com``.

        Raises:
            ConfigurationError: If ``PUBLIC_BASE_URL`` is not set.
        """
        if self.public_base_url is None:
            raise ConfigurationError(
                "PUBLIC_BASE_URL is required for WhatsApp webhook setup but is not set"
            )
        return str(self.public_base_url).rstrip("/")


def load_settings(env_file: Path | None = ENV_FILE) -> Settings:
    """Build a :class:`Settings`, turning validation failures into a safe error.

    Pydantic's ``ValidationError`` text includes the rejected *input values*, which
    here are API keys and passwords. We re-raise with variable names only and drop
    the original exception (``from None``) so secrets cannot reach logs or tracebacks.

    Args:
        env_file: ``.env`` path to read, or ``None`` to use only real environment
            variables (useful in tests and containers).

    Returns:
        The validated settings.

    Raises:
        ConfigurationError: If any variable is missing or invalid.
    """
    try:
        # Required fields come from the environment, which static checkers can't see;
        # mypy's pydantic plugin understands this, pyright does not.
        return Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue]
    except ValidationError as exc:
        problems = sorted(
            {
                # err["msg"] describes the rule ("input is empty"); it never echoes the
                # input, unlike str(exc).
                f"{'.'.join(str(p) for p in err['loc']).upper()} ({err['msg']})"
                for err in exc.errors()
            }
        )
        raise ConfigurationError("Invalid or missing settings: " + ", ".join(problems)) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loading them once.

    Use as a FastAPI dependency (``Depends(get_settings)``); tests can override it
    or call ``get_settings.cache_clear()``.

    Returns:
        The cached :class:`Settings` instance.

    Raises:
        ConfigurationError: If any variable is missing or invalid.
    """
    return load_settings()
