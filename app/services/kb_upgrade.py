"""Knowledge-base upgrade planning (R5): which questions deserve a new draft entry.

Pure functions: no database, no network, no LLM. :func:`plan_drafts` takes what the
weekly job loaded (reviewed answers, repeated "I don't know" questions, the drafts
that already exist) and returns the drafts to insert.

What it proposes, and what it never does:

* A draft's **title is the patient's question** (whitespace tidied) and its **body is
  empty**. The practitioner writes the answer, cites a source and approves it; the
  system never writes medical content and never publishes anything.
* The reason for the draft goes in ``review_hint`` (the reviewer's note, or how often
  the question was asked). It is staff-only and never served to patients.

Sources, in order (a question is proposed once per run):

1. Advisor replies whose **latest** review is ``unsafe``: **priority** drafts, listed
   first and highlighted on the Knowledge base tab ("Marked unsafe by a reviewer").
2. Advisor replies whose latest review is ``incorrect`` or ``needs_kb_update``.
3. Questions the advisor could not answer, asked at least ``min_repeats`` times in the
   lookback window. The latest ask is the draft's source turn.

Skipped: a question with no text (the "Talk to a doctor" button), a question whose
normalised text matches an open draft, and a turn that already produced a draft
(``kb_entries_one_proposal_per_turn_idx`` backs this up), so re-runs add nothing.
One exception, so an unsafe answer can never hide behind an ordinary draft: when an
unsafe review matches an open draft (same turn or same question) that is not yet a
priority, that draft is **promoted** to priority instead of being skipped.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Collection, Sequence
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# R5: the verdicts that mean "the knowledge base should say something else". An unsafe
# answer may have harmed a patient, so its draft is a priority.
DRAFT_VERDICTS: Final[frozenset[str]] = frozenset({"unsafe", "incorrect", "needs_kb_update"})
PRIORITY_VERDICT: Final = "unsafe"
# Shown on a priority draft (and first in its hint).
PRIORITY_LABEL: Final = "Marked unsafe by a reviewer"

# Drafts the job proposes use this entry_key prefix plus the source turn id: stable,
# unique, and recognisable in the KB list.
PROPOSED_KEY_PREFIX: Final = "review:"
_VERDICT_LABEL: Final = {
    "unsafe": "Unsafe",
    "incorrect": "Incorrect",
    "needs_kb_update": "Needs KB update",
}
_NOT_WORD = re.compile(r"[^\w]+")
# A "repeated" question has been asked at least twice.
MIN_REPEATS_FLOOR: Final = 2

DraftVerdict = Literal["unsafe", "incorrect", "needs_kb_update"]


class ReviewedQuestion(BaseModel):
    """An advisor reply whose latest review asks for a KB change.

    Attributes:
        turn_id: The reviewed turn.
        question: What the patient wrote or said (``None`` for the help button).
        verdict: The latest verdict.
        note: The reviewer's note.
        reviewed_at: When that review was recorded.
    """

    model_config = ConfigDict(frozen=True)

    turn_id: uuid.UUID
    question: str | None
    verdict: DraftVerdict
    note: str | None
    reviewed_at: AwareDatetime


class UnansweredQuestion(BaseModel):
    """One "I don't know" reply.

    Attributes:
        turn_id: The turn.
        question: What the patient wrote or said.
        asked_at: When.
    """

    model_config = ConfigDict(frozen=True)

    turn_id: uuid.UUID
    question: str | None
    asked_at: AwareDatetime


class DraftProposal(BaseModel):
    """A draft ``kb_entries`` row to insert (status ``draft``, empty body).

    Attributes:
        entry_key: ``review:<turn id>``.
        title: The patient's question.
        review_hint: Why it was proposed (staff only).
        proposed_from_turn_id: The source turn.
        reason: ``review`` or ``unanswered``.
        priority: The answer was marked unsafe; the draft is listed first.
    """

    model_config = ConfigDict(frozen=True)

    entry_key: str
    title: str = Field(min_length=1)
    review_hint: str
    proposed_from_turn_id: uuid.UUID
    reason: Literal["review", "unanswered"]
    priority: bool = False


class OpenDraft(BaseModel):
    """A draft awaiting approval, as the planner needs it.

    Attributes:
        id: ``kb_entries.id``.
        title: Current title (the practitioner may have edited it).
        proposed_from_turn_id: Source turn of a proposed draft.
        priority: Already a priority draft.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    title: str
    proposed_from_turn_id: uuid.UUID | None = None
    priority: bool = False


class DraftPlan(BaseModel):
    """Result of :func:`plan_drafts`.

    Attributes:
        proposals: Drafts to insert, reviewed questions first.
        candidates: Distinct questions considered (reviewed turns plus repeated
            unanswered questions).
        skipped: Candidates not proposed (open draft, already proposed or no text).
        promote_draft_ids: Open drafts to mark priority (an unsafe review matched
            them); not counted as skipped.
    """

    proposals: list[DraftProposal]
    candidates: int
    skipped: int
    promote_draft_ids: list[uuid.UUID] = Field(default_factory=list)


def tidy_question(text: str | None) -> str:
    """Collapse whitespace, as the question will appear as a draft title.

    Args:
        text: Raw question.

    Returns:
        The question on one line, or ``""``.
    """
    return " ".join((text or "").split())


def question_key(text: str | None) -> str:
    """Normalise a question so trivially different wordings group together.

    Case, accents, punctuation and spacing are ignored: "Can I eat jackfruit?" and
    "can i eat  jackfruit" are one question. Wording is not (no stemming or
    synonyms): grouping must stay predictable for the practitioner.

    Args:
        text: Raw question.

    Returns:
        The key, or ``""`` for no text.
    """
    folded = unicodedata.normalize("NFKD", text or "").casefold()
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(_NOT_WORD.sub(" ", stripped).split())


def _review_hint(item: ReviewedQuestion) -> str:
    """Hint for a draft proposed from a review.

    Args:
        item: The reviewed question.

    Returns:
        E.g. ``Reviewer marked the answer "Needs KB update" (2026-11-16): <note>``.
    """
    label = _VERDICT_LABEL[item.verdict]
    note = (item.note or "").strip() or "(no note)"
    hint = f'Reviewer marked the answer "{label}" ({item.reviewed_at.date().isoformat()}): {note}'
    return f"{PRIORITY_LABEL}. {hint}" if item.verdict == PRIORITY_VERDICT else hint


def _unanswered_hint(group: Sequence[UnansweredQuestion]) -> str:
    """Hint for a draft proposed from a repeated unanswered question.

    Args:
        group: Every ask of the question, oldest first.

    Returns:
        E.g. ``Asked 3 times between 2026-11-02 and 2026-11-15; the advisor had no
        matching entry and said it did not know.``
    """
    first, last = group[0].asked_at.date(), group[-1].asked_at.date()
    span = f"on {first.isoformat()}" if first == last else f"between {first} and {last}"
    return (
        f"Asked {len(group)} times {span}; the advisor had no matching entry "
        "and said it did not know."
    )


def plan_drafts(
    reviewed: Sequence[ReviewedQuestion],
    unanswered: Sequence[UnansweredQuestion],
    *,
    open_drafts: Sequence[OpenDraft],
    proposed_turn_ids: Collection[uuid.UUID],
    min_repeats: int,
) -> DraftPlan:
    """Decide which draft entries to propose or promote (R5 weekly job).

    Args:
        reviewed: Advisor replies whose latest review is in :data:`DRAFT_VERDICTS`.
        unanswered: "I don't know" replies in the lookback window.
        open_drafts: Every draft awaiting approval.
        proposed_turn_ids: Turns that already produced a draft (any status).
        min_repeats: Asks needed before an unanswered question is proposed.

    Returns:
        The drafts to insert, the drafts to promote, and the counts for ``batch_runs``.

    Raises:
        ValueError: ``min_repeats`` is below :data:`MIN_REPEATS_FLOOR`.
    """
    if min_repeats < MIN_REPEATS_FLOOR:
        raise ValueError("min_repeats must be at least 2 (a repeat needs two asks)")
    open_by_key = {question_key(d.title): d for d in open_drafts if question_key(d.title)}
    open_by_turn = {d.proposed_from_turn_id: d for d in open_drafts if d.proposed_from_turn_id}
    taken = set(open_by_key)
    already = set(proposed_turn_ids)
    promoted: list[uuid.UUID] = []
    proposals: list[DraftProposal] = []
    candidates = skipped = 0

    def consider(turn_id: uuid.UUID, question: str | None, *, priority: bool) -> bool:
        """Count a candidate; return whether to propose it (may promote instead)."""
        nonlocal candidates, skipped
        candidates += 1
        key = question_key(question)
        match = open_by_turn.get(turn_id) or open_by_key.get(key)
        if priority and match is not None and not match.priority:
            if match.id not in promoted:
                promoted.append(match.id)
            taken.add(key)
            return False
        if not key or key in taken or turn_id in already:
            skipped += 1
            return False
        taken.add(key)
        return True

    # Unsafe first, then oldest review first: the earliest complaint about a question
    # gets the draft, and an unsafe one always claims it.
    for item in sorted(
        reviewed,
        key=lambda r: (r.verdict != PRIORITY_VERDICT, r.reviewed_at, str(r.turn_id)),
    ):
        priority = item.verdict == PRIORITY_VERDICT
        if not consider(item.turn_id, item.question, priority=priority):
            continue
        proposals.append(
            DraftProposal(
                entry_key=f"{PROPOSED_KEY_PREFIX}{item.turn_id}",
                title=tidy_question(item.question),
                review_hint=_review_hint(item),
                proposed_from_turn_id=item.turn_id,
                reason="review",
                priority=priority,
            )
        )

    groups: dict[str, list[UnansweredQuestion]] = defaultdict(list)
    for ask in unanswered:
        key = question_key(ask.question)
        if key:
            groups[key].append(ask)
    repeated = [
        sorted(group, key=lambda a: (a.asked_at, str(a.turn_id)))
        for group in groups.values()
        if len(group) >= min_repeats
    ]
    # Most-asked first, then the most recent: the biggest gaps lead the list.
    repeated.sort(key=lambda g: (-len(g), -g[-1].asked_at.timestamp()))
    for group in repeated:
        latest = group[-1]
        if not consider(latest.turn_id, latest.question, priority=False):
            continue
        proposals.append(
            DraftProposal(
                entry_key=f"{PROPOSED_KEY_PREFIX}{latest.turn_id}",
                title=tidy_question(latest.question),
                review_hint=_unanswered_hint(group),
                proposed_from_turn_id=latest.turn_id,
                reason="unanswered",
            )
        )
    return DraftPlan(
        proposals=proposals,
        candidates=candidates,
        skipped=skipped,
        promote_draft_ids=promoted,
    )
