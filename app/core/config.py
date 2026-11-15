"""Typed application settings, loaded from environment variables and ``.env``.

This is the only module allowed to read secrets. Everything else calls
:func:`get_settings` and receives a validated, immutable :class:`Settings`.
Secrets are wrapped in :class:`pydantic.SecretStr` so they print as ``**********``
if a settings object is ever logged or shown in a traceback.
"""

from __future__ import annotations

import base64
import binascii
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import Field, HttpUrl, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.exceptions import ConfigurationError

# Repo root: app/core/config.py -> parents[2]. Anchoring the path means the app finds
# .env no matter which directory uvicorn / pytest / the batch job is started from.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

# VAPID keys are P-256 (RFC 8292). The browser's applicationServerKey is the public
# key as an uncompressed point: 0x04 || X (32 bytes) || Y (32 bytes) = 65 bytes. The
# private key is the raw 32-byte scalar, which is what scripts/gen_vapid_keys.py prints.
_VAPID_PUBLIC_LEN = 65
_VAPID_UNCOMPRESSED_PREFIX = 0x04
_VAPID_PRIVATE_LEN = 32

# VAPID "sub" claim (RFC 8292 section 2.1). py_vapid, which pywebpush signs with, only
# accepts ``mailto:user@domain`` or a bare ``https://host`` - no path, port or trailing
# slash - and raises VapidException at *send* time otherwise. This mirrors its rule
# (py_vapid._check_sub) but anchors both alternatives, so a bad value fails at startup
# instead of inside the dispatcher.
VAPID_SUBJECT_PATTERN = (
    r"^(?:mailto:[^@\s]+@(?:localhost|[%\w-]+(?:\.[%\w-]+)+)"
    r"|https://(?:localhost|[\w-]+\.[\w.-]+))$"
)

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
        supabase_anon_key: Publishable (anon) key the patient web app uses to sign in
            with Supabase Auth. Public by design; optional until ``/app`` is used.
        vapid_public_key: Web Push VAPID public key (base64url, uncompressed P-256 point).
        vapid_private_key: Web Push VAPID private key (base64url, raw 32 bytes).
        vapid_subject: VAPID contact, ``mailto:user@domain`` or ``https://host`` with no
            path or port (RFC 8292, as py_vapid enforces it).
            The three VAPID settings are optional until a push is sent; read them via
            :meth:`require_vapid`.
        advisory_max_age_minutes: Advisories whose reading is older than this are
            expired instead of notified (R3).
        dispatch_interval_minutes: How often the advisory dispatcher runs on its own
            schedule (it also runs right after every batch update).
        advisory_audio_bucket: Private Supabase Storage bucket for voice advisories.
        advisory_audio_url_ttl_seconds: Lifetime of a signed voice-advisory URL.
        public_base_url: Public HTTPS URL of this API (the ngrok URL the phone opens).
            Optional; read it via :meth:`require_public_base_url` where it is needed.
        anthropic_api_key: API key for the Claude-backed help chat. Optional until
            Phase 7; read it via :meth:`require_anthropic_api_key` there.
        pre_breach_band: Fraction of a limit that defines the pre-breach band (R3).
        advisory_cooldown_hours: Minimum hours between repeat advisories (R3 de-dup).
        admin_api_key: Shared secret for ``POST /admin/batch-run`` (sent in the
            ``X-Admin-Api-Key`` header). Optional: without it the admin endpoint
            refuses every request; read it via :meth:`require_admin_api_key`.
        batch_interval_minutes: How often APScheduler runs the batch update (R1).
        batch_scheduler_enabled: Start the in-process scheduler with the API. Turn
            off when another process (or only the admin endpoint) drives the job.
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

    # Publishable key: shipped to the browser, so a plain str, not a SecretStr.
    supabase_anon_key: str | None = None

    # --- Web Push (R3 delivery) -------------------------------------------------
    # Optional so tests, the batch job and the API start before keys are generated
    # (scripts/gen_vapid_keys.py); sending a push calls require_vapid() and fails
    # loudly if any is missing.
    vapid_public_key: str | None = None
    vapid_private_key: SecretStr | None = None
    vapid_subject: str | None = None
    # R3: an alert about a reading more than an hour old no longer helps the patient
    # act on it (team choice, matching the 1 h push TTL); such advisories expire.
    advisory_max_age_minutes: int = Field(default=60, ge=5, le=24 * 60)
    # R3: queued advisories wait at most this long for the dispatcher when no batch run
    # happens (e.g. a reading arrives between runs). 2 min keeps alerts timely; the cap
    # matches the expiry window so a typo cannot hold alerts until they expire.
    dispatch_interval_minutes: int = Field(default=2, ge=1, le=60)
    # Private bucket (created by migration 0007): audio is only reachable through a
    # signed URL handed to the patient who owns the advisory.
    advisory_audio_bucket: str = Field(default="advisory-audio", pattern=r"^[a-z0-9][a-z0-9-]*$")
    # 5 min: long enough to press Play, short enough that a leaked link soon dies.
    advisory_audio_url_ttl_seconds: int = Field(default=300, ge=30, le=3600)

    # --- Public URL + LLM ------------------------------------------------------
    # Optional: only phone testing over ngrok needs a public URL, so local dev,
    # tests and batch jobs must start without one.
    public_base_url: HttpUrl | None = None
    # Optional: only the Phase 7 help chat calls Claude; read it via
    # require_anthropic_api_key() there.
    anthropic_api_key: SecretStr | None = None

    # --- Threshold engine / advisory behaviour ---------------------------------
    # R3: pre-breach band. 0.05 => warn when a reading is within 5% of a limit
    # (0.95*U <= x < U, or L < x <= 1.05*L). Product choice from the project brief,
    # not a clinical constant. Capped below 0.5 so the two bands of a limit pair can
    # never swallow the whole target range.
    pre_breach_band: float = Field(default=0.05, gt=0.0, lt=0.5)
    # R3: one advisory per user per metric per state per N hours. 24 h chosen by the
    # team to avoid alert fatigue; a *breach* still escalates to the practitioner, and
    # Level 2 hypoglycaemia (< 54 mg/dL) bypasses the cooldown entirely.
    advisory_cooldown_hours: int = Field(default=24, ge=1, le=24 * 7)

    # --- Batch job / admin API (R1) ---------------------------------------------
    # Optional so tests, scripts and the batch job start without it; the admin
    # endpoint fails closed (503) when it is missing.
    admin_api_key: SecretStr | None = None
    # R1: 15 min keeps advisories close to real time while each run stays small; the
    # upper bound (one day) stops a typo from silently disabling alerts for weeks.
    batch_interval_minutes: int = Field(default=15, ge=1, le=24 * 60)
    batch_scheduler_enabled: bool = True

    log_level: LogLevel = "INFO"

    @field_validator("vapid_public_key")
    @classmethod
    def _check_vapid_public_key(cls, value: str | None) -> str | None:
        """Require a base64url, uncompressed P-256 point (what browsers expect).

        Args:
            value: Raw ``VAPID_PUBLIC_KEY``.

        Returns:
            The key, stripped.

        Raises:
            ValueError: If it does not decode to 65 bytes starting with ``0x04``.
        """
        if value is None:
            return None
        raw = _b64url_decode(value.strip())
        if raw is None or len(raw) != _VAPID_PUBLIC_LEN or raw[0] != _VAPID_UNCOMPRESSED_PREFIX:
            raise ValueError("VAPID_PUBLIC_KEY must be a base64url uncompressed P-256 point")
        return value.strip()

    @field_validator("vapid_private_key")
    @classmethod
    def _check_vapid_private_key(cls, value: SecretStr | None) -> SecretStr | None:
        """Require a base64url raw 32-byte P-256 private key.

        Args:
            value: Raw ``VAPID_PRIVATE_KEY``.

        Returns:
            The key, stripped and still wrapped.

        Raises:
            ValueError: If it does not decode to 32 bytes. The message omits the value.
        """
        if value is None:
            return None
        secret = value.get_secret_value().strip()
        raw = _b64url_decode(secret)
        if raw is None or len(raw) != _VAPID_PRIVATE_LEN:
            raise ValueError("VAPID_PRIVATE_KEY must be a base64url raw 32-byte key")
        return SecretStr(secret)

    @field_validator("vapid_subject")
    @classmethod
    def _check_vapid_subject(cls, value: str | None) -> str | None:
        """Require a subject py_vapid will sign with (see ``VAPID_SUBJECT_PATTERN``).

        Args:
            value: Raw ``VAPID_SUBJECT``.

        Returns:
            The subject, stripped.

        Raises:
            ValueError: If it is not ``mailto:user@domain`` or ``https://host``.
        """
        if value is None:
            return None
        subject = value.strip()
        if re.fullmatch(VAPID_SUBJECT_PATTERN, subject, re.IGNORECASE) is None:
            raise ValueError(
                "VAPID_SUBJECT must be mailto:user@domain or https://host (no path or port)"
            )
        return subject

    @field_validator(
        "public_base_url",
        "anthropic_api_key",
        "admin_api_key",
        "supabase_anon_key",
        "vapid_public_key",
        "vapid_private_key",
        "vapid_subject",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat a blank optional line in ``.env`` (e.g. ``PUBLIC_BASE_URL=``) as not set.

        Args:
            value: Raw value from the environment.

        Returns:
            ``None`` for an empty or whitespace-only string, otherwise ``value``.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def require_public_base_url(self) -> str:
        """Return the public base URL, failing loudly if it is not configured.

        Call this from code that genuinely needs a public URL (links printed for the
        phone over ngrok) so the error points at the missing variable.

        Returns:
            The URL without a trailing slash, e.g. ``https://advisor.example.com``.

        Raises:
            ConfigurationError: If ``PUBLIC_BASE_URL`` is not set.
        """
        if self.public_base_url is None:
            raise ConfigurationError(
                "PUBLIC_BASE_URL is required (run scripts/go_live.sh) but is not set"
            )
        return str(self.public_base_url).rstrip("/")

    def require_anthropic_api_key(self) -> SecretStr:
        """Return the Anthropic API key, failing loudly if it is not configured.

        Call this from the Phase 7 help chat when building the Claude client. The
        key stays wrapped; unwrap it with ``get_secret_value()`` only at that point.

        Returns:
            The key as a :class:`pydantic.SecretStr`.

        Raises:
            ConfigurationError: If ``ANTHROPIC_API_KEY`` is not set.
        """
        if self.anthropic_api_key is None:
            raise ConfigurationError(
                "ANTHROPIC_API_KEY is required for the help chat but is not set"
            )
        return self.anthropic_api_key

    def require_admin_api_key(self) -> SecretStr:
        """Return the admin API key, failing loudly if it is not configured.

        Returns:
            The key as a :class:`pydantic.SecretStr`.

        Raises:
            ConfigurationError: If ``ADMIN_API_KEY`` is not set.
        """
        if self.admin_api_key is None:
            raise ConfigurationError("ADMIN_API_KEY is required for /admin routes but is not set")
        return self.admin_api_key

    def require_vapid(self) -> VapidConfig:
        """Return the VAPID key pair and subject, failing loudly if any is missing.

        Returns:
            The VAPID configuration; the private key stays wrapped.

        Raises:
            ConfigurationError: Naming every missing ``VAPID_*`` variable.
        """
        missing = [
            name
            for name, value in (
                ("VAPID_PUBLIC_KEY", self.vapid_public_key),
                ("VAPID_PRIVATE_KEY", self.vapid_private_key),
                ("VAPID_SUBJECT", self.vapid_subject),
            )
            if value is None
        ]
        if (
            missing
            or self.vapid_public_key is None
            or self.vapid_private_key is None
            or self.vapid_subject is None
        ):
            raise ConfigurationError(
                f"{', '.join(missing)} required for Web Push but not set "
                "(generate keys with: uv run python -m scripts.gen_vapid_keys)"
            )
        return VapidConfig(self.vapid_public_key, self.vapid_private_key, self.vapid_subject)

    def require_supabase_anon_key(self) -> str:
        """Return the publishable key the web app signs in with.

        Returns:
            The key.

        Raises:
            ConfigurationError: If ``SUPABASE_ANON_KEY`` is not set.
        """
        if self.supabase_anon_key is None:
            raise ConfigurationError("SUPABASE_ANON_KEY is required for /app but is not set")
        return self.supabase_anon_key


class VapidConfig(NamedTuple):
    """VAPID settings needed to sign a Web Push request (RFC 8292).

    Attributes:
        public_key: Base64url uncompressed P-256 point (browser applicationServerKey).
        private_key: Base64url raw 32-byte private key, still wrapped.
        subject: ``mailto:`` or ``https:`` contact sent to push services.
    """

    public_key: str
    private_key: SecretStr
    subject: str


def _b64url_decode(value: str) -> bytes | None:
    """Decode base64url with or without padding.

    Args:
        value: Encoded text.

    Returns:
        The bytes, or ``None`` if the text is not valid base64url.
    """
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None


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
