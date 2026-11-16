"""Database engine, session factory and reachability check for the backend and jobs.

One engine per process, built lazily from ``DATABASE_URL`` (via
:func:`app.core.config.get_settings`, the only place secrets are read). Sessions never
autocommit: callers own their transactions (the batch job commits once per user, R1).

Every connection gives up after :data:`DB_CONNECT_TIMEOUT_S` seconds. Without a timeout,
libpq waits for the TCP handshake forever: on a network that silently drops traffic
to port 5432 (some campus and office Wi-Fi), the API, the scheduler and the test suite
hang instead of failing. :class:`DatabaseHealth` turns that failure into one clear
message, "database unreachable (port 5432 blocked?)", for startup logs and the API.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from types import MappingProxyType
from typing import Final

import psycopg
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Supabase hands out postgres:// or postgresql:// DSNs; SQLAlchemy needs the driver
# named explicitly to use psycopg 3 (the only Postgres driver in pyproject).
_DRIVER_PREFIXES = ("postgresql+psycopg://", "postgresql://", "postgres://")

# 10 s: a healthy Supabase connection opens in well under a second, even over mobile
# data; ten times that is a dead network, not a slow one.
DB_CONNECT_TIMEOUT_S: Final = 10
# psycopg connect() keyword arguments for every connection (engine and test fixtures).
# prepare_threshold=None disables server-side prepared statements, which Supabase's
# transaction pooler does not support.
CONNECT_ARGS: Final = MappingProxyType(
    {"prepare_threshold": None, "connect_timeout": DB_CONNECT_TIMEOUT_S}
)
_DEFAULT_PG_PORT: Final = 5432
# Re-check reachability at most this often, so a broken network costs one timeout per
# half minute rather than one per request.
HEALTH_TTL_S: Final = 30.0
# libpq messages that mean the server answered but refused the login or database:
# the network is fine, the configuration is not.
_REJECTED_MARKERS: Final = (
    "authentication failed",
    "no pg_hba.conf entry",
    "does not exist",
    "password",
)


def sqlalchemy_url(dsn: str) -> str:
    """Rewrite a libpq DSN so SQLAlchemy uses the psycopg 3 driver.

    Args:
        dsn: ``postgres://``, ``postgresql://`` or ``postgresql+psycopg://`` URL.

    Returns:
        The same URL with the ``postgresql+psycopg://`` scheme.

    Raises:
        ValueError: If the URL is not a Postgres URL. The message omits the URL,
            which contains the database password.
    """
    for prefix in _DRIVER_PREFIXES:
        if dsn.startswith(prefix):
            return "postgresql+psycopg://" + dsn.removeprefix(prefix)
    raise ValueError("DATABASE_URL must be a postgres:// or postgresql:// URL")


def libpq_url(dsn: str) -> str:
    """Rewrite a DSN for psycopg's own ``connect`` (plain ``postgresql://``).

    Args:
        dsn: Any Postgres URL accepted by :func:`sqlalchemy_url`.

    Returns:
        The URL with the ``postgresql://`` scheme.
    """
    return "postgresql://" + sqlalchemy_url(dsn).removeprefix("postgresql+psycopg://")


def database_port(dsn: str) -> int:
    """Port the DSN connects to (5432 direct, 6543 for Supabase's pooler).

    Args:
        dsn: Postgres URL.

    Returns:
        The port, defaulting to 5432.
    """
    return make_url(sqlalchemy_url(dsn)).port or _DEFAULT_PG_PORT


def unreachable_message(port: int) -> str:
    """The one message used for a database that cannot be reached.

    Args:
        port: Database port.

    Returns:
        E.g. ``"database unreachable (port 5432 blocked?)"``.
    """
    return f"database unreachable (port {port} blocked?)"


def describe_connection_error(exc: BaseException, port: int) -> str:
    """Explain a failed connection without echoing the DSN (it holds the password).

    Args:
        exc: The error (psycopg, or SQLAlchemy wrapping psycopg).
        port: Database port, for the message.

    Returns:
        :func:`unreachable_message` for timeouts, refusals and DNS/network failures;
        a credentials hint when the server answered and rejected the login.
    """
    original = getattr(exc, "orig", None) or exc
    text = str(original).lower()
    if not isinstance(original, psycopg.errors.ConnectionTimeout) and any(
        marker in text for marker in _REJECTED_MARKERS
    ):
        return "database rejected the connection (check DATABASE_URL user, password, name)"
    return unreachable_message(port)


class DatabaseStatus(BaseModel):
    """Result of one reachability check.

    Attributes:
        reachable: A connection opened and ``select 1`` answered.
        detail: Why not, when unreachable (safe to log and to show users).
    """

    model_config = ConfigDict(frozen=True)

    reachable: bool
    detail: str | None = None


def probe_database(dsn: str, *, timeout_s: int = DB_CONNECT_TIMEOUT_S) -> DatabaseStatus:
    """Open one connection with a timeout and run ``select 1``.

    Args:
        dsn: Postgres URL.
        timeout_s: Connect timeout in seconds.

    Returns:
        The status; never raises for connection failures.
    """
    port = database_port(dsn)
    try:
        with psycopg.connect(
            libpq_url(dsn), connect_timeout=timeout_s, prepare_threshold=None
        ) as conn:
            conn.execute("select 1")
    except psycopg.OperationalError as exc:
        detail = describe_connection_error(exc, port)
        logger.debug("database probe failed (%s)", type(exc).__name__)
        return DatabaseStatus(reachable=False, detail=detail)
    return DatabaseStatus(reachable=True)


class DatabaseHealth:
    """Cached, thread-safe reachability check (one probe per :data:`HEALTH_TTL_S`).

    Args:
        dsn: Postgres URL.
        probe: Check to run (tests pass a fake).
        clock: Monotonic clock in seconds.
        ttl_s: How long a result is reused.
    """

    def __init__(
        self,
        dsn: str,
        *,
        probe: Callable[[str], DatabaseStatus] = probe_database,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = HEALTH_TTL_S,
    ) -> None:
        self._dsn = dsn
        self._probe = probe
        self._clock = clock
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._status: DatabaseStatus | None = None
        self._checked_at = 0.0

    def status(self) -> DatabaseStatus:
        """Return the cached status, probing again once it is older than the TTL.

        Concurrent callers wait for one probe rather than each starting their own.

        Returns:
            The status.
        """
        with self._lock:
            now = self._clock()
            if self._status is None or now - self._checked_at >= self._ttl_s:
                self._status = self._probe(self._dsn)
                self._checked_at = self._clock()
                if not self._status.reachable:
                    logger.error("%s", self._status.detail)
            return self._status


def build_database_health(settings: Settings) -> DatabaseHealth:
    """Create the reachability check for the configured database.

    Args:
        settings: Supplies ``DATABASE_URL``.

    Returns:
        The check.
    """
    return DatabaseHealth(settings.database_url.get_secret_value())


@lru_cache(maxsize=1)
def get_database_health() -> DatabaseHealth:
    """Return the process-wide reachability check (FastAPI dependency).

    Returns:
        The shared, cached check.
    """
    return build_database_health(get_settings())


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use.

    Returns:
        A pooled engine. ``pool_pre_ping`` drops connections Supabase closed while
        idle between batch runs; :data:`CONNECT_ARGS` sets the connect timeout and
        disables prepared statements.

    Raises:
        ConfigurationError: If settings are missing or invalid.
    """
    url = sqlalchemy_url(get_settings().database_url.get_secret_value())
    return create_engine(url, pool_pre_ping=True, connect_args=dict(CONNECT_ARGS))


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Return the session factory bound to :func:`get_engine`.

    Returns:
        A ``sessionmaker`` whose sessions keep loaded attributes after commit, so
        results can be read once the transaction is closed.
    """
    return sessionmaker(get_engine(), expire_on_commit=False)
