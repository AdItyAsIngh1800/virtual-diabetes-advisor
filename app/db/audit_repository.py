"""Database access for practitioner review of advisor answers (R5 audit loop).

A practitioner reads the advisor's replies to their patients and records a verdict on
each: ``correct``, ``incorrect``, ``unsafe`` or ``needs_kb_update``. Verdicts go into
the append-only ``turn_reviews`` table; changing your mind means adding a new review,
and the **latest review of a turn is its current verdict**. Nothing here issues an
UPDATE or DELETE on ``conversation_turns`` or ``turn_reviews`` (the database and the
ORM listeners in :mod:`app.db.models` would refuse one anyway).

Which turns are reviewable: advisor replies only. The chat stores a patient message
and the advisor's reply on one ``direction = 'inbound'`` row; ``outbound`` rows are
practitioner replies and system notices, which are not the advisor's answers. Relay
acknowledgements ("Sent to your practitioner") carry no advice and are left out.

Access (the backend bypasses RLS, so this mirrors ``accessible_patient_ids()`` in
``0002_rls.sql``): a practitioner sees turns of patients assigned to them while both
their login and practitioner row are active; an admin sees every patient. A turn
outside that set is treated exactly like a turn that does not exist.

:func:`answer_summary` counts the same rows as the review list (R5 Audit tab): totals,
current verdicts, escalations, "I don't know" replies and dosing/diagnosis guard
replies, overall and per ``kb_version``, each count with its rate.

Nothing here commits: the route owns the transaction.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Final, Literal, NamedTuple

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError
from sqlalchemy import ColumnElement, Select, and_, exists, func, select, tuple_, union
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, aliased

from app.core.exceptions import ReviewNoteRequiredError, TurnNotReviewableError
from app.core.rates import CountRate
from app.db.chat_repository import db_now
from app.db.models import (
    ConversationTurn,
    KbEntry,
    PatientAssignment,
    Practitioner,
    Profile,
    ReviewVerdict,
    TurnIntent,
    TurnReview,
    User,
)
from app.services.chat import RELAY_MODEL, RULES_MODEL, TFIDF_MODEL

# R5: list filters on the review tab.
#   all            - every advisor reply
#   unreviewed     - no review yet
#   escalated      - a practitioner was brought in on that turn
#   low_confidence - answered from the KB, but with similarity below the review cutoff
#   unanswered     - the "I don't know" reply (no KB entry was close enough)
ReviewFilter = Literal["all", "unreviewed", "escalated", "low_confidence", "unanswered"]

# R5: a verdict other than "correct" must say what is wrong, or the weekly KB upgrade
# job has nothing to draft from.
NOTE_REQUIRED: Final[frozenset[ReviewVerdict]] = frozenset(
    {"incorrect", "unsafe", "needs_kb_update"}
)
MAX_PAGE_SIZE: Final = 100


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class KbSource(BaseModel):
    """A knowledge-base entry an answer came from.

    Attributes:
        id: ``kb_entries.id``.
        entry_key: Stable key across versions.
        title: Entry title.
        kb_version: KB release of that entry.
        source_citation: Where the text comes from (e.g. ADA Standards of Care).
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    entry_key: str
    title: str
    kb_version: int | None
    source_citation: str | None


class ReviewRecord(BaseModel):
    """One stored review.

    Attributes:
        id: ``turn_reviews.id``.
        turn_id: Reviewed turn.
        verdict: The verdict.
        note: Reviewer's note.
        reviewer_id: ``practitioners.id`` of the reviewer.
        reviewer_name: Reviewer's display name.
        created_at: When it was recorded.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    turn_id: uuid.UUID
    verdict: ReviewVerdict
    note: str | None
    reviewer_id: uuid.UUID
    reviewer_name: str | None
    created_at: AwareDatetime


class ReviewableTurn(BaseModel):
    """One advisor reply in the review list.

    Only the patient's first name is included; never a phone number, push endpoint or
    other contact detail.

    Attributes:
        turn_id: ``conversation_turns.id``.
        created_at: When the patient wrote.
        patient_first_name: First word of ``profiles.full_name``, or ``None``.
        question: What the patient wrote or said.
        spoken: The patient used voice (``question`` is the transcript).
        answer: The advisor's reply.
        intent: Classified intent.
        model: ``rules``, ``tfidf`` or an LLM name.
        model_version: Version of that model.
        escalated: A practitioner was brought in.
        unanswered: The advisor said it did not know.
        low_confidence: Answered from the KB with similarity below the review cutoff.
        retrieval_confidence: Best KB similarity (questions only).
        kb_version: KB release that answered.
        sources: KB entries the answer came from.
        current_review: The latest review, or ``None`` if never reviewed.
        review_count: How many reviews the turn has (the history is kept).
    """

    turn_id: uuid.UUID
    created_at: AwareDatetime
    patient_first_name: str | None
    question: str | None
    spoken: bool
    answer: str
    intent: TurnIntent | None
    model: str | None
    model_version: str | None
    escalated: bool
    unanswered: bool
    low_confidence: bool
    retrieval_confidence: float | None
    kb_version: int | None
    sources: list[KbSource]
    current_review: ReviewRecord | None
    review_count: int


class TurnCursor(BaseModel):
    """Keyset position in the newest-first list: the last turn of a page.

    Attributes:
        created_at: That turn's ``created_at``.
        id: That turn's id (tie-breaker).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    created_at: AwareDatetime
    id: uuid.UUID

    def encode(self) -> str:
        """Serialise for a URL query parameter.

        Returns:
            URL-safe base64 of the JSON form.
        """
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, token: str) -> TurnCursor:
        """Parse a token made by :meth:`encode`.

        Args:
            token: The query parameter.

        Returns:
            The cursor.

        Raises:
            ValueError: The token is not a cursor this API issued.
        """
        try:
            raw = base64.urlsafe_b64decode(token.encode("ascii"))
            return cls.model_validate_json(raw)
        except (UnicodeEncodeError, binascii.Error, ValidationError) as exc:
            raise ValueError("invalid cursor") from exc


class TurnPage(BaseModel):
    """One page of the review list.

    Attributes:
        items: Turns, newest first.
        next_cursor: Pass back as ``cursor`` for the next page; ``None`` on the last.
    """

    items: list[ReviewableTurn]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# SQL building blocks
# ---------------------------------------------------------------------------


def visible_patient_ids(practitioner_id: uuid.UUID) -> Select[uuid.UUID]:
    """Patients a practitioner may see, as in ``app_private.accessible_patient_ids()``.

    Args:
        practitioner_id: ``practitioners.id`` of the reviewer.

    Returns:
        A ``SELECT`` of patient ids: assigned patients while the practitioner and
        their login are active, plus every patient when the login is an admin.
    """
    assigned = (
        select(PatientAssignment.patient_id)
        .join(Practitioner, Practitioner.id == PatientAssignment.practitioner_id)
        .join(User, User.id == Practitioner.user_id)
        .where(
            PatientAssignment.practitioner_id == practitioner_id,
            Practitioner.is_active,
            User.is_active,
        )
    )
    admin_login = aliased(User)
    patient = aliased(User)
    caller_is_admin = exists(
        select(1)
        .select_from(Practitioner)
        .join(admin_login, admin_login.id == Practitioner.user_id)
        .where(
            Practitioner.id == practitioner_id,
            admin_login.is_active,
            admin_login.role == "admin",
        )
    )
    everyone = select(patient.id).where(patient.role == "patient", caller_is_admin)
    return select(union(assigned, everyone).subquery().c[0])


def advisor_reply() -> ColumnElement[bool]:
    """SQL condition for a turn that carries an advisor reply (see module docstring).

    Returns:
        The condition.
    """
    return and_(
        ConversationTurn.direction == "inbound",
        ConversationTurn.response_text.is_not(None),
        ConversationTurn.author_practitioner_id.is_(None),
        ConversationTurn.model.is_distinct_from(RELAY_MODEL),
    )


def unanswered_reply() -> ColumnElement[bool]:
    """SQL condition for the "I don't know" reply.

    ``ChatEngine._question`` gives that reply, and only that reply, as a ``question``
    from the ``tfidf`` model with no KB entries used (greetings are intent ``other``
    and the dosing guard is model ``rules``). Matching on structure rather than on the
    reply text keeps older rows matching if the wording changes.

    Returns:
        The condition.
    """
    return and_(
        ConversationTurn.intent == "question",
        ConversationTurn.model == TFIDF_MODEL,
        func.cardinality(ConversationTurn.kb_ids_used) == 0,
    )


def guard_reply() -> ColumnElement[bool]:
    """SQL condition for a question blocked by the dosing/diagnosis guard.

    ``ChatEngine._question`` answers a dosing or diagnosis question with the fixed
    ``ADVICE_GUARD_REPLY`` from the ``rules`` model and intent ``question``; no other
    advisor reply is a ``rules`` question (relay acknowledgements are excluded by
    :func:`advisor_reply`). Matched on structure, like :func:`unanswered_reply`.

    Returns:
        The condition.
    """
    return and_(ConversationTurn.intent == "question", ConversationTurn.model == RULES_MODEL)


def latest_reviews() -> Select[
    uuid.UUID, uuid.UUID, ReviewVerdict, str | None, uuid.UUID, datetime, int
]:
    """Latest review of every reviewed turn, with the turn's review count.

    Returns:
        ``DISTINCT ON (turn_id)`` select, newest review per turn.
    """
    return (
        select(
            TurnReview.turn_id,
            TurnReview.id.label("review_id"),
            TurnReview.verdict,
            TurnReview.note,
            TurnReview.reviewer_id,
            TurnReview.created_at.label("reviewed_at"),
            func.count().over(partition_by=TurnReview.turn_id).label("review_count"),
        )
        .ext(distinct_on(TurnReview.turn_id))
        .order_by(TurnReview.turn_id, TurnReview.created_at.desc(), TurnReview.id.desc())
    )


def first_name(full_name: str | None) -> str | None:
    """Return the first word of a name.

    Args:
        full_name: ``profiles.full_name``.

    Returns:
        The first name, or ``None`` when no name is stored.
    """
    parts = (full_name or "").split()
    return parts[0] if parts else None


def _sources(session: Session, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, KbSource]:
    """Load the KB entries named by a page of turns in one query.

    Args:
        session: Open session.
        ids: ``kb_ids_used`` of every turn on the page.

    Returns:
        Entries by id (entries since deleted are simply missing).
    """
    if not ids:
        return {}
    rows = session.scalars(select(KbEntry).where(KbEntry.id.in_(set(ids))))
    return {
        r.id: KbSource(
            id=r.id,
            entry_key=r.entry_key,
            title=r.title,
            kb_version=r.kb_version,
            source_citation=r.source_citation,
        )
        for r in rows
    }


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def list_reviewable_turns(
    session: Session,
    practitioner_id: uuid.UUID,
    review_filter: ReviewFilter = "all",
    limit: int = 25,
    cursor: TurnCursor | None = None,
    *,
    low_confidence_below: float,
) -> TurnPage:
    """List advisor replies to the practitioner's patients, newest first.

    Args:
        session: Open session.
        practitioner_id: Reviewer (``practitioners.id``).
        review_filter: Which turns (see :data:`ReviewFilter`).
        limit: Page size, 1 to :data:`MAX_PAGE_SIZE`.
        cursor: Continue after this position (from the previous page).
        low_confidence_below: Similarity cutoff of the ``low_confidence`` filter
            (``Settings.review_low_confidence_below``).

    Returns:
        One page and the cursor of the next.

    Raises:
        ValueError: ``limit`` is out of range.
    """
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    latest = latest_reviews().subquery()
    reviewer = aliased(Practitioner)
    stmt = (
        select(
            ConversationTurn,
            Profile.full_name,
            latest.c.review_id,
            latest.c.verdict,
            latest.c.note,
            latest.c.reviewer_id,
            latest.c.reviewed_at,
            latest.c.review_count,
            reviewer.full_name,
        )
        .outerjoin(Profile, Profile.user_id == ConversationTurn.user_id)
        .outerjoin(latest, latest.c.turn_id == ConversationTurn.id)
        .outerjoin(reviewer, reviewer.id == latest.c.reviewer_id)
        .where(
            advisor_reply(),
            ConversationTurn.user_id.in_(visible_patient_ids(practitioner_id)),
        )
    )
    if review_filter == "unreviewed":
        stmt = stmt.where(latest.c.review_id.is_(None))
    elif review_filter == "escalated":
        stmt = stmt.where(ConversationTurn.escalated)
    elif review_filter == "low_confidence":
        stmt = stmt.where(
            func.cardinality(ConversationTurn.kb_ids_used) > 0,
            ConversationTurn.retrieval_confidence < low_confidence_below,
        )
    elif review_filter == "unanswered":
        stmt = stmt.where(unanswered_reply())
    if cursor is not None:
        stmt = stmt.where(
            tuple_(ConversationTurn.created_at, ConversationTurn.id)
            < tuple_(cursor.created_at, cursor.id)
        )
    # One extra row tells whether another page exists.
    rows = session.execute(
        stmt.order_by(ConversationTurn.created_at.desc(), ConversationTurn.id.desc()).limit(
            limit + 1
        )
    ).all()
    page, more = rows[:limit], len(rows) > limit
    sources = _sources(session, [kb_id for row in page for kb_id in row[0].kb_ids_used])

    items: list[ReviewableTurn] = []
    for turn, full_name, review_id, verdict, note, reviewer_id, reviewed_at, count, rname in page:
        current = (
            ReviewRecord(
                id=review_id,
                turn_id=turn.id,
                verdict=verdict,
                note=note,
                reviewer_id=reviewer_id,
                reviewer_name=rname,
                created_at=reviewed_at,
            )
            if review_id is not None
            else None
        )
        items.append(
            ReviewableTurn(
                turn_id=turn.id,
                created_at=turn.created_at,
                patient_first_name=first_name(full_name),
                question=turn.transcript if turn.transcript is not None else turn.raw_text,
                spoken=turn.transcript is not None,
                answer=turn.response_text or "",
                intent=turn.intent,
                model=turn.model,
                model_version=turn.model_version,
                escalated=turn.escalated,
                unanswered=(
                    turn.intent == "question" and turn.model == TFIDF_MODEL and not turn.kb_ids_used
                ),
                low_confidence=(
                    bool(turn.kb_ids_used)
                    and turn.retrieval_confidence is not None
                    and turn.retrieval_confidence < Decimal(str(low_confidence_below))
                ),
                retrieval_confidence=(
                    float(turn.retrieval_confidence)
                    if turn.retrieval_confidence is not None
                    else None
                ),
                kb_version=turn.kb_version,
                sources=[sources[k] for k in turn.kb_ids_used if k in sources],
                current_review=current,
                review_count=int(count or 0),
            )
        )
    next_cursor = (
        TurnCursor(created_at=page[-1][0].created_at, id=page[-1][0].id).encode() if more else None
    )
    return TurnPage(items=items, next_cursor=next_cursor)


def add_review(
    session: Session,
    *,
    turn_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    verdict: ReviewVerdict,
    note: str | None,
) -> ReviewRecord:
    """Append a review of an advisor reply (INSERT only; reviews are never edited).

    The access check lives here, not only in the route, so no caller can review a turn
    of a patient the reviewer cannot see.

    Args:
        session: Open session (caller commits).
        turn_id: Turn being reviewed.
        reviewer_id: ``practitioners.id`` of the reviewer.
        verdict: The verdict.
        note: Explanation; required for every verdict in :data:`NOTE_REQUIRED`.

    Returns:
        The stored review, which is now the turn's current verdict.

    Raises:
        ReviewNoteRequiredError: The verdict needs a note and none was given.
        TurnNotReviewableError: The turn does not exist, is not an advisor reply, or
            its patient is not visible to the reviewer.
    """
    clean_note = note.strip() if note is not None else ""
    if verdict in NOTE_REQUIRED and not clean_note:
        raise ReviewNoteRequiredError(f"a note is required for the verdict {verdict!r}")
    found = session.scalar(
        select(ConversationTurn.id).where(
            ConversationTurn.id == turn_id,
            advisor_reply(),
            ConversationTurn.user_id.in_(visible_patient_ids(reviewer_id)),
        )
    )
    if found is None:
        raise TurnNotReviewableError(f"turn {turn_id} is not reviewable by {reviewer_id}")
    review = TurnReview(
        id=uuid.uuid4(),
        turn_id=turn_id,
        reviewer_id=reviewer_id,
        verdict=verdict,
        note=clean_note or None,
        # clock_timestamp(), not now(): two reviews in one transaction must still be
        # ordered, because the latest one is the current verdict.
        created_at=db_now(session),
    )
    session.add(review)
    session.flush()
    return ReviewRecord(
        id=review.id,
        turn_id=review.turn_id,
        verdict=review.verdict,
        note=review.note,
        reviewer_id=review.reviewer_id,
        reviewer_name=session.scalar(
            select(Practitioner.full_name).where(Practitioner.id == reviewer_id)
        ),
        created_at=review.created_at,
    )


# ---------------------------------------------------------------------------
# Audit summary (R5 Audit tab)
# ---------------------------------------------------------------------------


class VerdictCounts(BaseModel):
    """Current verdicts (latest review per turn), each out of the reviewed answers.

    Attributes:
        correct: Marked correct.
        incorrect: Marked incorrect.
        unsafe: Marked unsafe.
        needs_kb_update: Marked as needing a KB change.
    """

    correct: CountRate
    incorrect: CountRate
    unsafe: CountRate
    needs_kb_update: CountRate


class AnswerCounts(BaseModel):
    """Counts over a set of advisor replies; every rate comes with its counts.

    Attributes:
        total: Advisor replies.
        reviewed: Replies with at least one review, out of ``total``.
        unreviewed: Replies with none, out of ``total``.
        verdicts: Current verdicts, out of ``reviewed``.
        escalated: Replies that brought a practitioner in, out of ``total``.
        unanswered: "I don't know" replies, out of ``total``.
        blocked_by_guard: Dosing/diagnosis questions refused, out of ``total``.
    """

    total: int
    reviewed: CountRate
    unreviewed: CountRate
    verdicts: VerdictCounts
    escalated: CountRate
    unanswered: CountRate
    blocked_by_guard: CountRate


class KbVersionCounts(BaseModel):
    """Counts of the replies one KB release gave.

    Attributes:
        kb_version: ``conversation_turns.kb_version``; ``None`` for replies that used
            no KB (readings, red flags, the guard, handoff requests).
        counts: The counts.
    """

    kb_version: int | None
    counts: AnswerCounts


class AnswerSummary(BaseModel):
    """Advisor replies in a window, overall and per KB version.

    Attributes:
        totals: All replies in the window.
        by_kb_version: The same counts per ``kb_version``, oldest first, then ``None``.
    """

    totals: AnswerCounts
    by_kb_version: list[KbVersionCounts]


class _RawCounts(NamedTuple):
    """Plain counts of one group, before rates are attached."""

    total: int = 0
    reviewed: int = 0
    correct: int = 0
    incorrect: int = 0
    unsafe: int = 0
    needs_kb_update: int = 0
    escalated: int = 0
    unanswered: int = 0
    blocked: int = 0

    def with_rates(self) -> AnswerCounts:
        """Attach denominators and rates.

        Returns:
            The counts as :class:`AnswerCounts`.
        """
        return AnswerCounts(
            total=self.total,
            reviewed=CountRate.of(self.reviewed, self.total),
            unreviewed=CountRate.of(self.total - self.reviewed, self.total),
            verdicts=VerdictCounts(
                correct=CountRate.of(self.correct, self.reviewed),
                incorrect=CountRate.of(self.incorrect, self.reviewed),
                unsafe=CountRate.of(self.unsafe, self.reviewed),
                needs_kb_update=CountRate.of(self.needs_kb_update, self.reviewed),
            ),
            escalated=CountRate.of(self.escalated, self.total),
            unanswered=CountRate.of(self.unanswered, self.total),
            blocked_by_guard=CountRate.of(self.blocked, self.total),
        )


def answer_summary(
    session: Session,
    practitioner_id: uuid.UUID,
    *,
    since: datetime,
    until: datetime,
) -> AnswerSummary:
    """Count the advisor replies a practitioner may see in ``[since, until)``.

    The rows are exactly the review list's (:func:`advisor_reply` of a patient in
    :func:`visible_patient_ids`), so the numbers match what the Review tab shows. The
    verdict of a turn is its latest review, as everywhere else.

    Args:
        session: Open session.
        practitioner_id: Viewer (``practitioners.id``); an admin counts every patient.
        since: Inclusive start, on ``conversation_turns.created_at``.
        until: Exclusive end.

    Returns:
        Totals and per-KB-version counts, with rates.

    Raises:
        ValueError: ``since`` is not before ``until``.
    """
    if since >= until:
        raise ValueError("since must be before until")
    latest = latest_reviews().subquery()

    def counted(condition: ColumnElement[bool]) -> ColumnElement[int]:
        return func.count().filter(condition)

    stmt = (
        select(
            ConversationTurn.kb_version,
            func.count(),
            counted(latest.c.review_id.is_not(None)),
            counted(latest.c.verdict == "correct"),
            counted(latest.c.verdict == "incorrect"),
            counted(latest.c.verdict == "unsafe"),
            counted(latest.c.verdict == "needs_kb_update"),
            counted(ConversationTurn.escalated.is_(True)),
            counted(unanswered_reply()),
            counted(guard_reply()),
        )
        .outerjoin(latest, latest.c.turn_id == ConversationTurn.id)
        .where(
            advisor_reply(),
            ConversationTurn.user_id.in_(visible_patient_ids(practitioner_id)),
            ConversationTurn.created_at >= since,
            ConversationTurn.created_at < until,
        )
        .group_by(ConversationTurn.kb_version)
        .order_by(ConversationTurn.kb_version.asc().nulls_last())
    )
    groups = [
        (version, _RawCounts(*(int(n) for n in counts)))
        for version, *counts in session.execute(stmt).all()
    ]
    # Counts are additive across KB versions; rates are not, so they are recomputed.
    totals = _RawCounts(*(sum(column) for column in zip(*(c for _, c in groups), strict=True)))
    return AnswerSummary(
        totals=totals.with_rates(),
        by_kb_version=[
            KbVersionCounts(kb_version=version, counts=counts.with_rates())
            for version, counts in groups
        ],
    )
