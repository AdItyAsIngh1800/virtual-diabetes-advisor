"""Database access for the knowledge-base upgrade loop (R5, human in the loop).

Two callers:

* the weekly job (:mod:`app.jobs.kb_upgrade`) loads its inputs with
  :func:`load_upgrade_inputs` and stores the planned drafts with :func:`insert_drafts`;
* the practitioner KB routes (:mod:`app.api.kb_admin`) list, edit, approve and reject.

Lifecycle of a ``kb_entries`` row (the database enforces it, see 0001 and 0009)::

    draft --approve--> approved --superseded by a newer approval--> retired
    draft --reject---> retired

* Only **drafts** are edited. An approved row is never changed in place: "Edit" makes
  a new draft that supersedes it (same ``entry_key``), and approving that draft retires
  the old row in the same transaction.
* Approval needs a non-empty body and a source citation, records the signed-in
  practitioner and the time, assigns the next ``kb_version`` and clears the staff-only
  ``review_hint``. Nothing here approves on its own: only :func:`approve_draft`, called
  from a practitioner's request, ever sets ``status = 'approved'``.

The knowledge base is shared by all staff, so every practitioner sees every draft. The
conversation a draft came from is patient data, though: its question and the advisor's
answer are only included for a practitioner who may see that patient (the same rule as
the review tab, :func:`app.db.audit_repository.visible_patient_ids`).

Nothing here commits: the route or job owns the transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import (
    KbApprovalIncompleteError,
    KbEntryNotFoundError,
    KbEntryStateError,
)
from app.db.audit_repository import (
    advisor_reply,
    latest_reviews,
    unanswered_reply,
    visible_patient_ids,
)
from app.db.chat_repository import db_now
from app.db.models import (
    ConversationTurn,
    KbEntry,
    KbOrigin,
    KbStatus,
    Language,
    Practitioner,
)
from app.services.kb_upgrade import (
    DRAFT_VERDICTS,
    DraftProposal,
    OpenDraft,
    ReviewedQuestion,
    UnansweredQuestion,
)

# Serialises approvals so two practitioners approving at once cannot be given the same
# kb_version. Transaction-scoped, so it works through Supabase's transaction pooler.
_KB_VERSION_LOCK: Final = "kb_entries.kb_version"


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class UpgradeInputs(BaseModel):
    """Everything :func:`app.services.kb_upgrade.plan_drafts` needs.

    Attributes:
        reviewed: Advisor replies whose latest review asks for a KB change.
        unanswered: "I don't know" replies since the lookback start.
        open_drafts: Drafts awaiting approval.
        proposed_turn_ids: Turns that already produced a draft (any status).
    """

    reviewed: list[ReviewedQuestion]
    unanswered: list[UnansweredQuestion]
    open_drafts: list[OpenDraft]
    proposed_turn_ids: list[uuid.UUID]


class SourceTurn(BaseModel):
    """The conversation turn a draft was proposed from.

    Attributes:
        turn_id: ``conversation_turns.id``.
        asked_at: When the patient asked.
        question: What the patient wrote or said.
        answer: What the advisor replied.
    """

    model_config = ConfigDict(frozen=True)

    turn_id: uuid.UUID
    asked_at: AwareDatetime
    question: str | None
    answer: str | None


class KbEntryView(BaseModel):
    """One knowledge-base row as the practitioner page shows it.

    Attributes:
        id: ``kb_entries.id``.
        entry_key: Stable key across versions.
        title: Title (for a proposed draft, the patient's question).
        body: Patient-facing text (empty until a practitioner writes it).
        language: Entry language.
        status: ``draft``, ``approved`` or ``retired``.
        kb_version: KB release (approved and retired rows).
        source_citation: Where the text comes from.
        origin: ``manual`` or ``proposed_from_review``.
        review_hint: Why the job proposed it (staff only; drafts only).
        priority: Proposed from an answer marked unsafe; listed first.
        supersedes_id: The approved row this draft replaces.
        supersedes_version: That row's ``kb_version``.
        proposed_from_turn_id: The reviewed turn a proposed draft came from.
        source_turn: That turn, when the viewer may see its patient; else ``None``.
        open_draft_id: For an approved row, the draft currently editing it.
        created_by_name: Practitioner who started the draft (``None`` for the job).
        approved_by_name: Practitioner who approved it.
        approved_at: When it was approved.
        created_at: When the row was created.
        updated_at: Last change.
    """

    id: uuid.UUID
    entry_key: str
    title: str
    body: str
    language: Language
    status: KbStatus
    kb_version: int | None
    source_citation: str | None
    origin: KbOrigin
    review_hint: str | None
    priority: bool
    supersedes_id: uuid.UUID | None
    supersedes_version: int | None
    proposed_from_turn_id: uuid.UUID | None
    source_turn: SourceTurn | None
    open_draft_id: uuid.UUID | None
    created_by_name: str | None
    approved_by_name: str | None
    approved_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class KbListing(BaseModel):
    """The practitioner KB tab.

    Attributes:
        drafts: Drafts awaiting approval, newest first.
        approved: Entries being served now, by title.
    """

    drafts: list[KbEntryView]
    approved: list[KbEntryView]


# ---------------------------------------------------------------------------
# Weekly job
# ---------------------------------------------------------------------------


def _question_of(turn: ConversationTurn) -> str | None:
    """Return what the patient wrote or said on a turn.

    Args:
        turn: The turn.

    Returns:
        The transcript for voice, else the typed text.
    """
    return turn.transcript if turn.transcript is not None else turn.raw_text


def load_upgrade_inputs(session: Session, *, since: AwareDatetime) -> UpgradeInputs:
    """Load the job's inputs across all patients (the job acts for the whole clinic).

    Args:
        session: Open session.
        since: Start of the lookback window for unanswered questions. Reviewed
            answers are not windowed: a review asking for a KB change stays a
            candidate until a draft has been proposed from it.

    Returns:
        Reviewed and unanswered questions plus the existing drafts.
    """
    latest = latest_reviews().subquery()
    reviewed_rows = session.execute(
        select(ConversationTurn, latest.c.verdict, latest.c.note, latest.c.reviewed_at)
        .join(latest, latest.c.turn_id == ConversationTurn.id)
        .where(advisor_reply(), latest.c.verdict.in_(DRAFT_VERDICTS))
    ).all()
    unanswered_rows = session.scalars(
        select(ConversationTurn).where(
            advisor_reply(), unanswered_reply(), ConversationTurn.created_at >= since
        )
    ).all()
    open_rows = session.execute(
        select(KbEntry.id, KbEntry.title, KbEntry.proposed_from_turn_id, KbEntry.priority).where(
            KbEntry.status == "draft"
        )
    ).all()
    proposed = session.scalars(
        select(KbEntry.proposed_from_turn_id).where(KbEntry.proposed_from_turn_id.is_not(None))
    ).all()
    return UpgradeInputs(
        reviewed=[
            ReviewedQuestion(
                turn_id=turn.id,
                question=_question_of(turn),
                verdict=verdict,
                note=note,
                reviewed_at=reviewed_at,
            )
            for turn, verdict, note, reviewed_at in reviewed_rows
        ],
        unanswered=[
            UnansweredQuestion(turn_id=t.id, question=_question_of(t), asked_at=t.created_at)
            for t in unanswered_rows
        ],
        open_drafts=[
            OpenDraft(id=i, title=t, proposed_from_turn_id=turn, priority=prio)
            for i, t, turn, prio in open_rows
        ],
        proposed_turn_ids=[t for t in proposed if t is not None],
    )


def insert_drafts(session: Session, proposals: Sequence[DraftProposal]) -> list[uuid.UUID]:
    """Insert the planned drafts: status ``draft``, empty body, no citation.

    Args:
        session: Open session (caller commits).
        proposals: From :func:`app.services.kb_upgrade.plan_drafts`.

    Returns:
        The new row ids, in order.
    """
    rows = [
        KbEntry(
            id=uuid.uuid4(),
            entry_key=p.entry_key,
            title=p.title,
            # R5: the system never writes medical content; a practitioner writes this.
            body="",
            language="en",
            status="draft",
            origin="proposed_from_review",
            review_hint=p.review_hint,
            priority=p.priority,
            proposed_from_turn_id=p.proposed_from_turn_id,
        )
        for p in proposals
    ]
    session.add_all(rows)
    session.flush()
    return [r.id for r in rows]


def promote_drafts(session: Session, draft_ids: Sequence[uuid.UUID]) -> int:
    """Mark open drafts as priority (an unsafe review matched them).

    Only the flag changes: the title, body and hint stay as the practitioner left them.

    Args:
        session: Open session (caller commits).
        draft_ids: From :attr:`app.services.kb_upgrade.DraftPlan.promote_draft_ids`.

    Returns:
        How many drafts were promoted (a draft approved or rejected meanwhile is not).
    """
    if not draft_ids:
        return 0
    result = session.execute(
        update(KbEntry)
        .where(KbEntry.id.in_(draft_ids), KbEntry.status == "draft", KbEntry.priority.is_(False))
        .values(priority=True)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Practitioner views
# ---------------------------------------------------------------------------


def _names(session: Session, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    """Look up practitioner display names in one query.

    Args:
        session: Open session.
        ids: ``practitioners.id`` values (``None`` ignored).

    Returns:
        Names by id.
    """
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = session.execute(
        select(Practitioner.id, Practitioner.full_name).where(Practitioner.id.in_(wanted))
    ).all()
    return {pid: name for pid, name in rows if name}


def _views(
    session: Session, rows: Sequence[KbEntry], practitioner_id: uuid.UUID
) -> list[KbEntryView]:
    """Convert rows, resolving names, superseded versions, open drafts and source turns.

    Args:
        session: Open session.
        rows: ``kb_entries`` rows.
        practitioner_id: Viewer; source turns of patients they cannot see are left out.

    Returns:
        Views in the order of ``rows``.
    """
    names = _names(session, [x for r in rows for x in (r.created_by, r.approved_by)])
    superseded_ids = {r.supersedes_id for r in rows if r.supersedes_id is not None}
    versions: dict[uuid.UUID, int | None] = (
        {
            entry_id: version
            for entry_id, version in session.execute(
                select(KbEntry.id, KbEntry.kb_version).where(KbEntry.id.in_(superseded_ids))
            )
        }
        if superseded_ids
        else {}
    )
    approved_ids = {r.id for r in rows if r.status == "approved"}
    open_drafts: dict[uuid.UUID | None, uuid.UUID] = (
        {
            superseded: draft_id
            for superseded, draft_id in session.execute(
                select(KbEntry.supersedes_id, KbEntry.id).where(
                    KbEntry.status == "draft", KbEntry.supersedes_id.in_(approved_ids)
                )
            )
        }
        if approved_ids
        else {}
    )
    turn_ids = {r.proposed_from_turn_id for r in rows if r.proposed_from_turn_id is not None}
    turns = (
        {
            t.id: SourceTurn(
                turn_id=t.id,
                asked_at=t.created_at,
                question=_question_of(t),
                answer=t.response_text,
            )
            for t in session.scalars(
                select(ConversationTurn).where(
                    ConversationTurn.id.in_(turn_ids),
                    ConversationTurn.user_id.in_(visible_patient_ids(practitioner_id)),
                )
            )
        }
        if turn_ids
        else {}
    )
    return [
        KbEntryView(
            id=r.id,
            entry_key=r.entry_key,
            title=r.title,
            body=r.body,
            language=r.language,
            status=r.status,
            kb_version=r.kb_version,
            source_citation=r.source_citation,
            origin=r.origin,
            review_hint=r.review_hint,
            priority=r.priority,
            supersedes_id=r.supersedes_id,
            supersedes_version=versions.get(r.supersedes_id) if r.supersedes_id else None,
            proposed_from_turn_id=r.proposed_from_turn_id,
            source_turn=turns.get(r.proposed_from_turn_id) if r.proposed_from_turn_id else None,
            open_draft_id=open_drafts.get(r.id),
            created_by_name=names.get(r.created_by) if r.created_by else None,
            approved_by_name=names.get(r.approved_by) if r.approved_by else None,
            approved_at=r.approved_at,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


def list_kb(session: Session, practitioner_id: uuid.UUID) -> KbListing:
    """List drafts awaiting approval and the entries being served.

    Args:
        session: Open session.
        practitioner_id: Viewer (``practitioners.id``).

    Returns:
        Drafts (priority first, then newest); approved entries by title.
    """
    drafts = session.scalars(
        select(KbEntry)
        .where(KbEntry.status == "draft")
        # Answers marked unsafe first (R5), then newest first.
        .order_by(KbEntry.priority.desc(), KbEntry.created_at.desc(), KbEntry.id)
    ).all()
    approved = session.scalars(
        select(KbEntry)
        .where(KbEntry.status == "approved")
        .order_by(func.lower(KbEntry.title), KbEntry.id)
    ).all()
    return KbListing(
        drafts=_views(session, drafts, practitioner_id),
        approved=_views(session, approved, practitioner_id),
    )


def _locked(session: Session, entry_id: uuid.UUID) -> KbEntry:
    """Load a row ``FOR UPDATE`` (concurrent edits/approvals queue behind it).

    Args:
        session: Open session.
        entry_id: ``kb_entries.id``.

    Returns:
        The row.

    Raises:
        KbEntryNotFoundError: No such row.
    """
    row = session.scalar(
        select(KbEntry)
        .where(KbEntry.id == entry_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise KbEntryNotFoundError(f"kb entry {entry_id} not found")
    return row


def _require_draft(row: KbEntry, action: str) -> None:
    """Refuse an action on anything but a draft.

    Args:
        row: The row.
        action: Verb for the message.

    Raises:
        KbEntryStateError: The row is approved or retired.
    """
    if row.status != "draft":
        raise KbEntryStateError(f"only drafts can be {action}; this entry is {row.status}")


def _clean(text: str | None) -> str | None:
    """Strip text; blank becomes ``None``.

    Args:
        text: Input.

    Returns:
        Stripped text or ``None``.
    """
    stripped = (text or "").strip()
    return stripped or None


def update_draft(
    session: Session,
    entry_id: uuid.UUID,
    practitioner_id: uuid.UUID,
    *,
    title: str,
    body: str,
    source_citation: str | None,
) -> KbEntryView:
    """Save a practitioner's work on a draft (only drafts may be edited).

    Args:
        session: Open session (caller commits).
        entry_id: The draft.
        practitioner_id: Editor (for the returned view's visibility rules).
        title: New title.
        body: New body (may stay empty while drafting).
        source_citation: New citation, or ``None``.

    Returns:
        The updated draft.

    Raises:
        KbEntryNotFoundError: No such entry.
        KbEntryStateError: The entry is not a draft.
    """
    row = _locked(session, entry_id)
    _require_draft(row, "edited")
    row.title = title.strip()
    row.body = body.strip()
    row.source_citation = _clean(source_citation)
    session.flush()
    return _views(session, [row], practitioner_id)[0]


def start_edit(
    session: Session, entry_id: uuid.UUID, practitioner_id: uuid.UUID
) -> tuple[KbEntryView, bool]:
    """Open a draft that supersedes an approved entry (the approved row is untouched).

    The draft starts as a copy of the approved text, so re-approving an entry
    unchanged is: Edit, read it, Approve. If a draft for the entry is already open,
    that one is returned instead of a second.

    Args:
        session: Open session (caller commits).
        entry_id: The approved entry.
        practitioner_id: Who is editing (``created_by``).

    Returns:
        The draft, and whether it was created by this call.

    Raises:
        KbEntryNotFoundError: No such entry.
        KbEntryStateError: The entry is not approved.
    """
    current = _locked(session, entry_id)
    if current.status != "approved":
        raise KbEntryStateError(
            f"only approved entries can be edited; this one is {current.status}"
        )
    existing = session.scalar(
        select(KbEntry).where(KbEntry.status == "draft", KbEntry.supersedes_id == entry_id)
    )
    if existing is not None:
        return _views(session, [existing], practitioner_id)[0], False
    draft = KbEntry(
        id=uuid.uuid4(),
        entry_key=current.entry_key,
        title=current.title,
        body=current.body,
        language=current.language,
        status="draft",
        source_citation=current.source_citation,
        origin="manual",
        supersedes_id=current.id,
        created_by=practitioner_id,
    )
    try:
        # Savepoint: losing the race on kb_entries_one_draft_per_superseded_idx only
        # undoes this insert; the winner's draft is returned instead.
        with session.begin_nested():
            session.add(draft)
    except IntegrityError:
        existing = session.scalar(
            select(KbEntry).where(KbEntry.status == "draft", KbEntry.supersedes_id == entry_id)
        )
        if existing is None:
            raise
        return _views(session, [existing], practitioner_id)[0], False
    return _views(session, [draft], practitioner_id)[0], True


def approve_draft(session: Session, entry_id: uuid.UUID, practitioner_id: uuid.UUID) -> KbEntryView:
    """Publish a draft: the only path by which an entry becomes ``approved``.

    Args:
        session: Open session (caller commits).
        entry_id: The draft.
        practitioner_id: Signed-in practitioner, recorded as ``approved_by``.

    Returns:
        The approved entry.

    Raises:
        KbEntryNotFoundError: No such entry.
        KbEntryStateError: The entry is not a draft, the entry it supersedes is no
            longer the approved version, or another approved entry has its key.
        KbApprovalIncompleteError: The body or the source citation is empty.
    """
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext(_KB_VERSION_LOCK))))
    draft = _locked(session, entry_id)
    _require_draft(draft, "approved")
    if not draft.body.strip() or not (draft.source_citation or "").strip():
        raise KbApprovalIncompleteError("write the entry text and cite its source before approving")
    old: KbEntry | None = None
    if draft.supersedes_id is not None:
        old = _locked(session, draft.supersedes_id)
        if old.status != "approved":
            raise KbEntryStateError(
                "the entry this draft edits is no longer the approved version; "
                "reject this draft and edit the current one"
            )
    clash = session.scalar(
        select(KbEntry.id).where(
            KbEntry.status == "approved",
            KbEntry.entry_key == draft.entry_key,
            KbEntry.language == draft.language,
            KbEntry.id != (old.id if old is not None else draft.id),
        )
    )
    if clash is not None:
        raise KbEntryStateError(f"another approved entry already uses the key {draft.entry_key!r}")
    version = (session.scalar(select(func.max(KbEntry.kb_version))) or 0) + 1
    # Read the clock before changing any row: a query after the change would autoflush
    # a half-approved row, which the kb_entries CHECK constraints refuse.
    approved_at = db_now(session)
    if old is not None:
        # Retire first: kb_entries_one_approved_per_key_idx allows one approved row.
        old.status = "retired"
        session.flush()
    draft.status = "approved"
    draft.kb_version = version
    draft.approved_by = practitioner_id
    draft.approved_at = approved_at
    draft.body = draft.body.strip()
    draft.source_citation = (draft.source_citation or "").strip()
    # Staff-only working note; the database refuses it on a patient-visible row.
    draft.review_hint = None
    draft.priority = False
    session.flush()
    return _views(session, [draft], practitioner_id)[0]


def reject_draft(session: Session, entry_id: uuid.UUID, practitioner_id: uuid.UUID) -> KbEntryView:
    """Discard a draft (it is retired, never deleted, and never served).

    Args:
        session: Open session (caller commits).
        entry_id: The draft.
        practitioner_id: Who rejected it (for the returned view).

    Returns:
        The retired row.

    Raises:
        KbEntryNotFoundError: No such entry.
        KbEntryStateError: The entry is not a draft.
    """
    row = _locked(session, entry_id)
    _require_draft(row, "rejected")
    row.status = "retired"
    session.flush()
    return _views(session, [row], practitioner_id)[0]
