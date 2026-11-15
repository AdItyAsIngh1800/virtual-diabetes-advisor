"""Database engine and session factory for the backend and batch jobs.

One engine per process, built lazily from ``DATABASE_URL`` (via
:func:`app.core.config.get_settings`, the only place secrets are read). Sessions never
autocommit: callers own their transactions (the batch job commits once per user, R1).
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# Supabase hands out postgres:// or postgresql:// DSNs; SQLAlchemy needs the driver
# named explicitly to use psycopg 3 (the only Postgres driver in pyproject).
_DRIVER_PREFIXES = ("postgresql+psycopg://", "postgresql://", "postgres://")


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


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use.

    Returns:
        A pooled engine. ``pool_pre_ping`` drops connections Supabase closed while
        idle between batch runs; ``prepare_threshold=None`` disables server-side
        prepared statements, which Supabase's transaction pooler does not support.

    Raises:
        ConfigurationError: If settings are missing or invalid.
    """
    url = sqlalchemy_url(get_settings().database_url.get_secret_value())
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"prepare_threshold": None},
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Return the session factory bound to :func:`get_engine`.

    Returns:
        A ``sessionmaker`` whose sessions keep loaded attributes after commit, so
        results can be read once the transaction is closed.
    """
    return sessionmaker(get_engine(), expire_on_commit=False)
