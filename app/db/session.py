"""Database engine, session factory and reachability check for the backend and jobs.

One engine per process, built lazily from ``DATABASE_URL`` (via
:func:`app.core.config.get_settings`, the only place secrets are read). Sessions never
autocommit: callers own their transactions (the batch job commits once per user, R1).

Every connection gives up after :data:`DB_CONNECT_TIMEOUT_S` seconds. Without a timeout,
libpq waits for the TCP handshake forever: on a network that silently drops traffic
to port 5432 (some campus and office Wi-Fi), the API, the scheduler and the test suite
hang instead of failing. :class:`DatabaseHealth` turns that failure into one clear
message, "database unreachable (port 5432 blocked?)", for startup logs and the API.

Once connected, two more limits stop a stalled connection from hanging forever:

* **TCP keepalives** (:data:`CONNECT_ARGS`): a connection whose peer silently vanished
  (a pooler that dropped it, Wi-Fi that stopped forwarding) is detected after about
  a minute instead of blocking a read forever.
* **statement_timeout** (:data:`DB_STATEMENT_TIMEOUT_MS`): the server cancels any
  statement, including one stuck waiting on a lock, after 30 s. It is sent as a libpq
  startup option *and* set with ``SET`` on every new connection, because Supabase's
  pooler ignores startup options. The ``SET`` is session-level, which suits the
  session-mode pooler (port 5432) and direct connections this project uses.

Build engines with :func:`make_engine` and raw connections with :func:`open_connection`
so all of the above applies everywhere, tests included.
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
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Supabase hands out postgres:// or postgresql:// DSNs; SQLAlchemy needs the driver
# named explicitly to use psycopg 3 (the only Postgres driver in pyproject).
_DRIVER_PREFIXES = ("postgresql+psycopg://", "postgresql://", "postgres://")

# 10 s: a healthy Supabase connection opens in well under a second, even over mobile
# data; ten times that is a dead network, not a slow one.
DB_CONNECT_TIMEOUT_S: Final = 10
# TCP keepalives: after 30 s of silence send a probe every 10 s and give up after 3
# unanswered ones, so a silently dropped connection fails in about a minute instead
# of hanging a test run (seen twice, Nov 2026) or a request forever.
DB_KEEPALIVE_IDLE_S: Final = 30
DB_KEEPALIVE_INTERVAL_S: Final = 10
DB_KEEPALIVE_COUNT: Final = 3
# 30 s: the app's slowest statement (the batch update's per-user query) takes well
# under a second; anything near 30 s is stuck, typically waiting on a lock.
DB_STATEMENT_TIMEOUT_MS: Final = 30_000
_SET_STATEMENT_TIMEOUT: Final = f"SET statement_timeout = {DB_STATEMENT_TIMEOUT_MS}"
# libpq connection parameters for every connection.
LIBPQ_PARAMS: Final[MappingProxyType[str, str | int]] = MappingProxyType(
    {
        "connect_timeout": DB_CONNECT_TIMEOUT_S,
        "keepalives": 1,
        "keepalives_idle": DB_KEEPALIVE_IDLE_S,
        "keepalives_interval": DB_KEEPALIVE_INTERVAL_S,
        "keepalives_count": DB_KEEPALIVE_COUNT,
        # Honoured by Postgres itself; Supabase's pooler drops it, hence the SET too.
        "options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
    }
)
# psycopg connect() keyword arguments for SQLAlchemy engines (connect_args).
# prepare_threshold=None disables server-side prepared statements, which Supabase's
# transaction pooler does not support.
CONNECT_ARGS: Final = MappingProxyType({"prepare_threshold": None, **LIBPQ_PARAMS})
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


def conninfo(dsn: str, **overrides: str | int) -> str:
    """Build a libpq conninfo string carrying :data:`LIBPQ_PARAMS`.

    Args:
        dsn: Any Postgres URL accepted by :func:`sqlalchemy_url`.
        **overrides: Parameters to replace (e.g. a shorter ``connect_timeout``).

    Returns:
        The conninfo string (contains the password: never log it).
    """
    return make_conninfo(libpq_url(dsn), **{**LIBPQ_PARAMS, **overrides})


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
            conninfo(dsn, connect_timeout=timeout_s), prepare_threshold=None
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


def set_statement_timeout(dbapi_connection: DBAPIConnection) -> None:
    """Apply :data:`DB_STATEMENT_TIMEOUT_MS` to a new connection and commit it.

    Committed so the setting survives the caller's first rollback.

    Args:
        dbapi_connection: A freshly opened DB-API (psycopg) connection.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(_SET_STATEMENT_TIMEOUT)
    finally:
        cursor.close()
    dbapi_connection.commit()


def _on_connect(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    """SQLAlchemy ``connect`` event: set the statement timeout on each new connection.

    Args:
        dbapi_connection: The new psycopg connection.
        _record: Pool bookkeeping (unused).
    """
    set_statement_timeout(dbapi_connection)


def make_engine(dsn: str, *, pool_pre_ping: bool = False) -> Engine:
    """Create an engine with the connect timeout, keepalives and statement timeout.

    Args:
        dsn: Any Postgres URL accepted by :func:`sqlalchemy_url`.
        pool_pre_ping: Test pooled connections before use (long-lived processes).

    Returns:
        The engine.
    """
    engine = create_engine(
        sqlalchemy_url(dsn), pool_pre_ping=pool_pre_ping, connect_args=dict(CONNECT_ARGS)
    )
    event.listen(engine, "connect", _on_connect)
    return engine


def open_connection(dsn: str, *, autocommit: bool = False) -> psycopg.Connection[TupleRow]:
    """Open a raw psycopg connection with the same limits as :func:`make_engine`.

    Args:
        dsn: Any Postgres URL accepted by :func:`sqlalchemy_url`.
        autocommit: psycopg autocommit mode.

    Returns:
        An open connection (use it as a context manager).
    """
    conn = psycopg.connect(conninfo(dsn), autocommit=autocommit, prepare_threshold=None)
    try:
        conn.execute(_SET_STATEMENT_TIMEOUT)
        if not autocommit:
            conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use.

    Returns:
        A pooled engine from :func:`make_engine`. ``pool_pre_ping`` drops connections
        Supabase closed while idle between batch runs.

    Raises:
        ConfigurationError: If settings are missing or invalid.
    """
    return make_engine(get_settings().database_url.get_secret_value(), pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Return the session factory bound to :func:`get_engine`.

    Returns:
        A ``sessionmaker`` whose sessions keep loaded attributes after commit, so
        results can be read once the transaction is closed.
    """
    return sessionmaker(get_engine(), expire_on_commit=False)
