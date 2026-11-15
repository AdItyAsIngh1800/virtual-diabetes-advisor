"""Database access for advisory delivery (R3): the dispatcher's queue and the advisory page.

Every state change is a *conditional* UPDATE (``WHERE delivery_status = 'queued' ...``)
that reports whether it applied, so two overlapping dispatcher runs can never both act
on one advisory. Callers own transactions; the dispatcher commits after each change.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection
from datetime import datetime
from decimal import Decimal
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import ColumnElement, case, func, select, update
from sqlalchemy.orm import Session

from app.core.clinical_limits import GlucoseLimitKind
from app.db.models import (
    Advisory,
    AdvisoryState,
    DeliveryStatus,
    Language,
    Reading,
    ReadingContext,
    User,
)

# advisories.delivery_error is shown on the dashboard; keep it short.
MAX_ERROR_TEXT: Final = 500


class QueuedAdvisory(BaseModel):
    """A queued advisory with what the dispatcher needs to deliver it.

    Attributes:
        id: ``advisories.id``.
        user_id: Patient.
        auth_user_id: Patient's Supabase login (Realtime topic), if linked.
        is_demo: ``users.is_demo`` (recipient guard).
        limit_kind: Limit approached or crossed.
        state: Band state.
        reading_value_mg_dl: The reading.
        threshold_mg_dl: The personal limit.
        language: Language of ``message_text``.
        message_text: Fixed-template advisory text.
        context: Reading context.
        measured_at: When the reading was taken.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    auth_user_id: uuid.UUID | None
    is_demo: bool
    limit_kind: GlucoseLimitKind
    state: AdvisoryState
    reading_value_mg_dl: Decimal
    threshold_mg_dl: Decimal
    language: Language
    message_text: str
    context: ReadingContext
    measured_at: AwareDatetime


class AdvisoryView(BaseModel):
    """An advisory as the patient's advisory page shows it.

    Attributes:
        id: Advisory id.
        user_id: Owner (checked against the signed-in patient).
        state: Band state.
        limit_kind: Limit approached or crossed.
        reading_value_mg_dl: The reading.
        threshold_mg_dl: The personal limit.
        context: Reading context.
        measured_at: When the reading was taken.
        language: Language of the text.
        message_text: Advisory text.
        escalated: Flagged for the care team.
        delivery_status: Delivery state.
        audio_path: Voice mp3 in storage, if generated.
        opened_at: First time the patient opened it.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    state: AdvisoryState
    limit_kind: GlucoseLimitKind
    reading_value_mg_dl: Decimal
    threshold_mg_dl: Decimal
    context: ReadingContext
    measured_at: AwareDatetime
    language: Language
    message_text: str
    escalated: bool
    delivery_status: DeliveryStatus
    audio_path: str | None
    opened_at: AwareDatetime | None


def _queued_and_unclaimed(advisory_id: uuid.UUID) -> tuple[ColumnElement[bool], ...]:
    """WHERE clauses that make a state change apply at most once.

    Args:
        advisory_id: Advisory.

    Returns:
        Conditions for ``update(Advisory).where(...)``.
    """
    return (
        Advisory.id == advisory_id,
        Advisory.delivery_status == "queued",
        Advisory.dispatch_claimed_at.is_(None),
    )


def queued_advisories(
    session: Session, *, user_ids: Collection[uuid.UUID] | None = None, limit: int = 500
) -> list[QueuedAdvisory]:
    """Return queued, unclaimed advisories, oldest reading first.

    Args:
        session: Open session.
        user_ids: Only these patients (tests, the demo trigger), or everyone.
        limit: Upper bound per run; the rest wait for the next run.

    Returns:
        The work list.
    """
    stmt = (
        select(
            Advisory.id,
            Advisory.user_id,
            User.auth_user_id,
            User.is_demo,
            Advisory.limit_kind,
            Advisory.state,
            Advisory.reading_value_mg_dl,
            Advisory.threshold_mg_dl,
            Advisory.language,
            Advisory.message_text,
            Reading.context,
            Reading.measured_at,
        )
        .join(Reading, Reading.id == Advisory.reading_id)
        .join(User, User.id == Advisory.user_id)
        .where(Advisory.delivery_status == "queued", Advisory.dispatch_claimed_at.is_(None))
        .order_by(Reading.measured_at, Advisory.id)
        .limit(limit)
    )
    if user_ids is not None:
        stmt = stmt.where(Advisory.user_id.in_(list(user_ids)))
    return [QueuedAdvisory.model_validate(row) for row in session.execute(stmt)]


def _apply(session: Session, advisory_id: uuid.UUID, **values: object) -> bool:
    """Run a queued-and-unclaimed conditional UPDATE.

    Args:
        session: Open session.
        advisory_id: Advisory.
        **values: Columns to set.

    Returns:
        ``True`` if the row was still queued and unclaimed (the change applied).
    """
    result = session.execute(
        update(Advisory)
        .where(*_queued_and_unclaimed(advisory_id))
        .values(**values)
        .returning(Advisory.id)
    )
    return result.first() is not None


def mark_expired(session: Session, advisory_id: uuid.UUID) -> bool:
    """Expire a queued advisory whose reading is too old to notify about.

    Args:
        session: Open session.
        advisory_id: Advisory.

    Returns:
        ``True`` if it was still queued.
    """
    return _apply(session, advisory_id, delivery_status="expired")


def mark_suppressed(session: Session, advisory_id: uuid.UUID) -> bool:
    """Suppress a queued advisory: the same alert was pushed within the cooldown.

    Args:
        session: Open session.
        advisory_id: Advisory.

    Returns:
        ``True`` if it was still queued.
    """
    return _apply(session, advisory_id, delivery_status="suppressed")


def mark_blocked(session: Session, advisory_id: uuid.UUID, at: datetime, reason: str) -> bool:
    """Fail a queued advisory the recipient guard refused (nothing was sent).

    Args:
        session: Open session.
        advisory_id: Advisory.
        at: Time of the decision.
        reason: Guard message (ids only, no PII).

    Returns:
        ``True`` if it was still queued.
    """
    return _apply(
        session,
        advisory_id,
        delivery_status="failed",
        failed_at=at,
        delivery_error_code="recipient_blocked",
        delivery_error=reason[:MAX_ERROR_TEXT],
    )


def claim(session: Session, advisory_id: uuid.UUID, at: datetime) -> bool:
    """Claim an advisory for sending; only one dispatcher run can win.

    Args:
        session: Open session (commit before sending, so others see the claim).
        advisory_id: Advisory.
        at: Claim time.

    Returns:
        ``True`` if this caller now owns the advisory.
    """
    return _apply(
        session, advisory_id, dispatch_claimed_at=at, attempt_count=Advisory.attempt_count + 1
    )


def last_push_at(
    session: Session,
    *,
    user_id: uuid.UUID,
    limit_kind: GlucoseLimitKind,
    state: AdvisoryState,
    exclude_id: uuid.UUID,
) -> datetime | None:
    """When the patient was last pushed an advisory for this limit and state.

    Args:
        session: Open session.
        user_id: Patient.
        limit_kind: Limit.
        state: Band state.
        exclude_id: The advisory being considered.

    Returns:
        Latest ``sent_at`` (sent or opened advisories), or ``None``.
    """
    latest = session.scalar(
        select(func.max(Advisory.sent_at)).where(
            Advisory.user_id == user_id,
            Advisory.limit_kind == limit_kind,
            Advisory.state == state,
            Advisory.sent_at.is_not(None),
            Advisory.id != exclude_id,
        )
    )
    return latest if isinstance(latest, datetime) else None


def record_sent(
    session: Session,
    advisory_id: uuid.UUID,
    *,
    at: datetime,
    message_id: str | None,
    audio_path: str | None,
) -> None:
    """Record an accepted push on a claimed advisory.

    Args:
        session: Open session.
        advisory_id: Advisory (claimed by this caller).
        at: Send time.
        message_id: Push-service message id of the first accepted device, if any.
        audio_path: Voice mp3 path, if generated.
    """
    session.execute(
        update(Advisory)
        .where(Advisory.id == advisory_id, Advisory.delivery_status == "queued")
        .values(
            delivery_status="sent",
            sent_at=at,
            message_id=message_id,
            audio_path=audio_path,
            delivery_error_code=None,
            delivery_error=None,
        )
    )


def record_failed(
    session: Session,
    advisory_id: uuid.UUID,
    *,
    at: datetime,
    code: str,
    error: str,
    audio_path: str | None,
) -> None:
    """Record a failed push on a claimed advisory.

    Args:
        session: Open session.
        advisory_id: Advisory (claimed by this caller).
        at: Failure time.
        code: Short machine code (``no_subscription``, ``push_failed`` ...).
        error: Human-readable reason, without endpoints or keys.
        audio_path: Voice mp3 path, if generated (the page can still play it).
    """
    session.execute(
        update(Advisory)
        .where(Advisory.id == advisory_id, Advisory.delivery_status == "queued")
        .values(
            delivery_status="failed",
            failed_at=at,
            delivery_error_code=code,
            delivery_error=error[:MAX_ERROR_TEXT],
            audio_path=audio_path,
        )
    )


def fail_stale_claims(session: Session, *, claimed_before: datetime, at: datetime) -> int:
    """Fail advisories a crashed dispatcher run claimed but never finished.

    Re-sending could push the same alert twice, so they are failed, not retried.

    Args:
        session: Open session.
        claimed_before: Claims older than this are stale.
        at: Failure time.

    Returns:
        Number of advisories failed.
    """
    result = session.execute(
        update(Advisory)
        .where(
            Advisory.delivery_status == "queued",
            Advisory.dispatch_claimed_at.is_not(None),
            Advisory.dispatch_claimed_at < claimed_before,
        )
        .values(
            delivery_status="failed",
            failed_at=at,
            delivery_error_code="dispatch_interrupted",
            delivery_error="dispatcher stopped mid-send; not retried to avoid a duplicate push",
        )
        .returning(Advisory.id)
    )
    return len(result.all())


def advisory_for_view(session: Session, advisory_id: uuid.UUID) -> AdvisoryView | None:
    """Load one advisory with its reading context, for the advisory page.

    Args:
        session: Open session.
        advisory_id: Advisory.

    Returns:
        The advisory, or ``None`` if it does not exist.
    """
    row = session.execute(
        select(
            Advisory.id,
            Advisory.user_id,
            Advisory.state,
            Advisory.limit_kind,
            Advisory.reading_value_mg_dl,
            Advisory.threshold_mg_dl,
            Reading.context,
            Reading.measured_at,
            Advisory.language,
            Advisory.message_text,
            Advisory.escalated,
            Advisory.delivery_status,
            Advisory.audio_path,
            Advisory.opened_at,
        )
        .join(Reading, Reading.id == Advisory.reading_id)
        .where(Advisory.id == advisory_id)
    ).first()
    return AdvisoryView.model_validate(row) if row is not None else None


def mark_opened(session: Session, advisory_id: uuid.UUID, at: datetime) -> datetime:
    """Record that the patient opened the advisory (first time wins).

    ``sent`` becomes ``opened``; other states keep their status (an expired or failed
    advisory stays so in the record) but still get ``opened_at``.

    Args:
        session: Open session (caller commits).
        advisory_id: Advisory.
        at: When the page was opened.

    Returns:
        The stored ``opened_at``.
    """
    opened = session.execute(
        update(Advisory)
        .where(Advisory.id == advisory_id)
        .values(
            opened_at=func.coalesce(Advisory.opened_at, at),
            delivery_status=case(
                (Advisory.delivery_status == "sent", "opened"), else_=Advisory.delivery_status
            ),
        )
        .returning(Advisory.opened_at)
    ).scalar_one()
    if not isinstance(opened, datetime):  # pragma: no cover - coalesce never yields NULL
        raise TypeError("opened_at was not stored")
    return opened


class SqlDispatchStore:
    """The dispatcher's view of ``advisories``: one short committed transaction per call.

    Committing each step matters: the claim must be visible to an overlapping run
    *before* the push goes out, and a crash mid-run must leave finished advisories
    recorded.

    Args:
        session_factory: Creates sessions.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def fail_stale_claims(self, *, claimed_before: datetime, at: datetime) -> int:
        """See :func:`fail_stale_claims`.

        Args:
            claimed_before: Claims older than this are stale.
            at: Failure time.

        Returns:
            Number of advisories failed.
        """
        with self._session_factory() as session:
            count = fail_stale_claims(session, claimed_before=claimed_before, at=at)
            session.commit()
            return count

    def queued(self, user_ids: Collection[uuid.UUID] | None) -> list[QueuedAdvisory]:
        """See :func:`queued_advisories`.

        Args:
            user_ids: Only these patients, or everyone.

        Returns:
            The work list.
        """
        with self._session_factory() as session:
            return queued_advisories(session, user_ids=user_ids)

    def expire(self, advisory_id: uuid.UUID) -> bool:
        """See :func:`mark_expired`.

        Args:
            advisory_id: Advisory.

        Returns:
            ``True`` if it applied.
        """
        return self._commit(mark_expired, advisory_id)

    def suppress(self, advisory_id: uuid.UUID) -> bool:
        """See :func:`mark_suppressed`.

        Args:
            advisory_id: Advisory.

        Returns:
            ``True`` if it applied.
        """
        return self._commit(mark_suppressed, advisory_id)

    def block(self, advisory_id: uuid.UUID, at: datetime, reason: str) -> bool:
        """See :func:`mark_blocked`.

        Args:
            advisory_id: Advisory.
            at: Decision time.
            reason: Guard message.

        Returns:
            ``True`` if it applied.
        """
        with self._session_factory() as session:
            applied = mark_blocked(session, advisory_id, at, reason)
            session.commit()
            return applied

    def claim(self, advisory_id: uuid.UUID, at: datetime) -> bool:
        """See :func:`claim`.

        Args:
            advisory_id: Advisory.
            at: Claim time.

        Returns:
            ``True`` if this caller owns it now.
        """
        with self._session_factory() as session:
            won = claim(session, advisory_id, at)
            session.commit()
            return won

    def last_push_at(
        self,
        *,
        user_id: uuid.UUID,
        limit_kind: GlucoseLimitKind,
        state: AdvisoryState,
        exclude_id: uuid.UUID,
    ) -> datetime | None:
        """See :func:`last_push_at`.

        Args:
            user_id: Patient.
            limit_kind: Limit.
            state: Band state.
            exclude_id: Advisory being considered.

        Returns:
            Latest push time, or ``None``.
        """
        with self._session_factory() as session:
            return last_push_at(
                session, user_id=user_id, limit_kind=limit_kind, state=state, exclude_id=exclude_id
            )

    def record_sent(
        self,
        advisory_id: uuid.UUID,
        *,
        at: datetime,
        message_id: str | None,
        audio_path: str | None,
    ) -> None:
        """See :func:`record_sent`.

        Args:
            advisory_id: Advisory.
            at: Send time.
            message_id: Push-service message id.
            audio_path: Voice mp3 path.
        """
        with self._session_factory() as session:
            record_sent(session, advisory_id, at=at, message_id=message_id, audio_path=audio_path)
            session.commit()

    def record_failed(
        self,
        advisory_id: uuid.UUID,
        *,
        at: datetime,
        code: str,
        error: str,
        audio_path: str | None,
    ) -> None:
        """See :func:`record_failed`.

        Args:
            advisory_id: Advisory.
            at: Failure time.
            code: Machine code.
            error: Reason.
            audio_path: Voice mp3 path.
        """
        with self._session_factory() as session:
            record_failed(
                session, advisory_id, at=at, code=code, error=error, audio_path=audio_path
            )
            session.commit()

    def _commit(self, change: Callable[[Session, uuid.UUID], bool], advisory_id: uuid.UUID) -> bool:
        """Apply one conditional change and commit it.

        Args:
            change: Repository function.
            advisory_id: Advisory.

        Returns:
            Whether it applied.
        """
        with self._session_factory() as session:
            applied = change(session, advisory_id)
            session.commit()
            return applied
