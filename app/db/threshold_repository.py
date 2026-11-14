"""SQL persistence for personal thresholds (R2).

The only module that reads or writes ``thresholds_history`` and ``current_thresholds``.
It implements :class:`app.services.thresholds.ThresholdRepository`, so the threshold
maths never imports SQLAlchemy sessions and is tested without a database.

The repository never commits: the caller owns the transaction, so the batch job (R1)
can write every user's thresholds and advisories atomically.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ColumnClause, Select, TableClause, column, select, table
from sqlalchemy.orm import Session

from app.core.clinical_limits import GlucoseLimitKind
from app.db.models import Reading, ThresholdHistory
from app.services.thresholds import ReadingPoint, StoredThreshold, ThresholdComputation


def _view_columns() -> list[ColumnClause[object]]:
    """Return lightweight column handles for every ``thresholds_history`` column.

    Returns:
        One typed column per table column, in table order.
    """
    return [column(c.name, c.type) for c in ThresholdHistory.__table__.columns]


# Read-only handle on the current_thresholds view (0001_core_schema.sql). The view is
# ``select distinct on (user_id, limit_kind) * from thresholds_history``, so it has the
# table's columns. A lightweight ``table()`` is not registered on Base.metadata, so the
# ORM never tries to create, alter or write it.
CURRENT_THRESHOLDS: TableClause = table("current_thresholds", *_view_columns())


def current_thresholds_query(user_id: uuid.UUID) -> Select[tuple[object, ...]]:
    """Build the query for one user's current thresholds.

    Args:
        user_id: The user.

    Returns:
        A SELECT on the ``current_thresholds`` view filtered to ``user_id``.
    """
    return select(*CURRENT_THRESHOLDS.columns).where(CURRENT_THRESHOLDS.c.user_id == user_id)


class SqlThresholdRepository:
    """Threshold persistence on a SQLAlchemy session.

    Args:
        session: Open session; the caller commits or rolls back.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def current_thresholds(self, user_id: uuid.UUID) -> dict[GlucoseLimitKind, StoredThreshold]:
        """Read the user's current thresholds through the ``current_thresholds`` view.

        Args:
            user_id: The user.

        Returns:
            Latest stored threshold per limit kind; kinds never computed are absent.
        """
        rows = self._session.execute(current_thresholds_query(user_id)).mappings()
        # DB rows cross a boundary: validate them into the domain model.
        stored = (StoredThreshold.model_validate(dict(row)) for row in rows)
        return {t.limit_kind: t for t in stored}

    def readings_since(self, user_id: uuid.UUID, since: datetime) -> list[ReadingPoint]:
        """Return the user's readings measured strictly after ``since``.

        Args:
            user_id: The user.
            since: Exclusive lower bound on ``measured_at``.

        Returns:
            Readings, oldest first.
        """
        stmt = (
            select(Reading)
            .where(Reading.user_id == user_id, Reading.measured_at > since)
            .order_by(Reading.measured_at)
        )
        return [ReadingPoint.model_validate(r) for r in self._session.scalars(stmt)]

    def save_threshold(
        self,
        user_id: uuid.UUID,
        computation: ThresholdComputation,
        *,
        effective_from: datetime,
        batch_run_id: uuid.UUID | None = None,
    ) -> StoredThreshold:
        """Append one computed threshold, with all its components, to the history.

        Args:
            user_id: The user.
            computation: Threshold, components, clamp bounds, model version, citation.
            effective_from: When it takes effect.
            batch_run_id: Batch run writing it, if any.

        Returns:
            The stored row (flushed, not committed).
        """
        row = ThresholdHistory(
            # Client-side id: no round trip needed to learn it (schema convention).
            id=uuid.uuid4(),
            user_id=user_id,
            effective_from=effective_from,
            batch_run_id=batch_run_id,
            **computation.model_dump(),
        )
        self._session.add(row)
        self._session.flush()
        return StoredThreshold.model_validate(row)
