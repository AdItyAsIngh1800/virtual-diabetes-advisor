"""Tests for app/services/kb_upgrade.py: which questions become draft KB entries (R5).

Pure functions, no database. The live job is covered by tests/test_kb_repository.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.kb_upgrade import (
    PRIORITY_LABEL,
    PROPOSED_KEY_PREFIX,
    DraftPlan,
    OpenDraft,
    ReviewedQuestion,
    UnansweredQuestion,
    plan_drafts,
    question_key,
    tidy_question,
)

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def reviewed(
    question: str | None = "Can I eat jackfruit?",
    *,
    verdict: str = "needs_kb_update",
    note: str | None = "Add jackfruit to the diet entry.",
    at: datetime = T0,
) -> ReviewedQuestion:
    return ReviewedQuestion.model_validate(
        {
            "turn_id": uuid.uuid4(),
            "question": question,
            "verdict": verdict,
            "note": note,
            "reviewed_at": at,
        }
    )


def asked(question: str | None, at: datetime = T0) -> UnansweredQuestion:
    return UnansweredQuestion(turn_id=uuid.uuid4(), question=question, asked_at=at)


def plan(
    rev: list[ReviewedQuestion] | None = None,
    unk: list[UnansweredQuestion] | None = None,
    *,
    open_titles: list[str] | None = None,
    open_drafts: list[OpenDraft] | None = None,
    proposed: list[uuid.UUID] | None = None,
    min_repeats: int = 2,
) -> DraftPlan:
    drafts = [OpenDraft(id=uuid.uuid4(), title=t) for t in open_titles or []]
    return plan_drafts(
        rev or [],
        unk or [],
        open_drafts=drafts + (open_drafts or []),
        proposed_turn_ids=proposed or [],
        min_repeats=min_repeats,
    )


# --- normalisation ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Can I eat jackfruit?", "can i eat  jackfruit"),
        ("What is HbA1c", "what is hba1c?!"),
        ("Café  au lait?", "cafe au lait"),
        ("  roti\nor rice ", "Roti or rice"),
    ],
)
def test_question_key_groups_trivial_variants(a: str, b: str) -> None:
    assert question_key(a) == question_key(b) != ""


def test_question_key_keeps_different_wording_apart() -> None:
    assert question_key("can i eat mango") != question_key("can i eat mangoes")


@pytest.mark.parametrize("text", [None, "", "   ", "?!"])
def test_question_key_of_nothing_is_empty(text: str | None) -> None:
    assert question_key(text) == ""


def test_tidy_question_collapses_whitespace_only() -> None:
    assert tidy_question("  Can I eat\n jackfruit? ") == "Can I eat jackfruit?"
    assert tidy_question(None) == ""


# --- reviewed answers ----------------------------------------------------------------


def test_reviewed_answer_becomes_an_empty_draft_titled_with_the_question() -> None:
    item = reviewed("  Can I eat\njackfruit? ")
    result = plan([item])
    assert result.candidates == 1 and result.skipped == 0
    (draft,) = result.proposals
    assert draft.title == "Can I eat jackfruit?"
    assert draft.proposed_from_turn_id == item.turn_id
    assert draft.entry_key == f"{PROPOSED_KEY_PREFIX}{item.turn_id}"
    assert draft.reason == "review"
    # The reviewer's note goes in the hint, never in patient-facing text.
    assert "Add jackfruit to the diet entry." in draft.review_hint
    assert '"Needs KB update"' in draft.review_hint
    assert "2026-11-02" in draft.review_hint
    # DraftProposal has no body at all: the system never writes medical content.
    assert "body" not in type(draft).model_fields


def test_incorrect_verdict_is_labelled() -> None:
    (draft,) = plan([reviewed(verdict="incorrect", note="Wrong target")]).proposals
    assert draft.review_hint.startswith('Reviewer marked the answer "Incorrect"')


def test_missing_note_is_said_plainly() -> None:
    (draft,) = plan([reviewed(note="  ")]).proposals
    assert draft.review_hint.endswith("(no note)")


def test_correct_verdict_is_not_a_candidate() -> None:
    with pytest.raises(ValueError, match="verdict"):
        reviewed(verdict="correct")


def test_question_without_text_is_skipped() -> None:
    result = plan([reviewed(None), reviewed("  ")])
    assert result.proposals == [] and (result.candidates, result.skipped) == (2, 2)


def test_same_question_reviewed_twice_gives_one_draft_from_the_oldest_review() -> None:
    first = reviewed("Is jaggery OK?", at=T0)
    second = reviewed("is jaggery ok", at=T0 + timedelta(days=1))
    result = plan([second, first])
    assert [d.proposed_from_turn_id for d in result.proposals] == [first.turn_id]
    assert (result.candidates, result.skipped) == (2, 1)


# --- repeated unanswered questions ---------------------------------------------------


def test_repeated_unanswered_question_becomes_a_draft_from_the_latest_ask() -> None:
    asks = [
        asked("Can I eat jackfruit?", T0),
        asked("can i eat jackfruit", T0 + timedelta(days=3)),
        asked("Can I eat JACKFRUIT", T0 + timedelta(days=1)),
    ]
    (draft,) = plan(unk=asks).proposals
    assert draft.proposed_from_turn_id == asks[1].turn_id
    assert draft.title == "can i eat jackfruit"
    assert draft.reason == "unanswered"
    assert draft.review_hint.startswith("Asked 3 times between 2026-11-02 and 2026-11-05")


def test_repeats_on_one_day_say_so() -> None:
    (draft,) = plan(unk=[asked("ragi?", T0), asked("Ragi", T0 + timedelta(hours=2))]).proposals
    assert draft.review_hint.startswith("Asked 2 times on 2026-11-02")


@pytest.mark.parametrize(("asks", "min_repeats", "drafts"), [(1, 2, 0), (2, 2, 1), (2, 3, 0)])
def test_min_repeats_boundary(asks: int, min_repeats: int, drafts: int) -> None:
    unk = [asked("is millet good", T0 + timedelta(hours=i)) for i in range(asks)]
    result = plan(unk=unk, min_repeats=min_repeats)
    assert len(result.proposals) == drafts
    # A one-off ask is not a candidate at all.
    assert result.candidates == drafts


def test_min_repeats_below_two_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        plan(min_repeats=1)


def test_most_asked_question_comes_first() -> None:
    unk = [asked("ragi", T0), asked("ragi", T0)] + [asked("millet", T0)] * 3
    result = plan(unk=unk)
    assert [d.title for d in result.proposals] == ["millet", "ragi"]


def test_reviewed_question_wins_over_the_same_unanswered_question() -> None:
    rev = reviewed("Can I eat jackfruit?")
    unk = [asked("can i eat jackfruit"), asked("can i eat jackfruit")]
    result = plan([rev], unk)
    assert [d.reason for d in result.proposals] == ["review"]
    assert (result.candidates, result.skipped) == (2, 1)


# --- idempotency -------------------------------------------------------------------------


def test_question_with_an_open_draft_is_skipped() -> None:
    result = plan([reviewed("Can I eat jackfruit?")], open_titles=["can I eat jackfruit"])
    assert result.proposals == [] and result.skipped == 1


def test_turn_that_already_produced_a_draft_is_skipped() -> None:
    item = reviewed()
    result = plan([item], proposed=[item.turn_id])
    assert result.proposals == [] and result.skipped == 1


def test_feeding_a_plan_back_in_proposes_nothing() -> None:
    rev = [reviewed("Is jaggery OK?"), reviewed("What is a normal A1C?")]
    unk = [asked("ragi"), asked("ragi")]
    first = plan(rev, unk)
    assert len(first.proposals) == 3
    again = plan(
        rev,
        unk,
        open_titles=[d.title for d in first.proposals],
        proposed=[d.proposed_from_turn_id for d in first.proposals],
    )
    assert again.proposals == [] and again.skipped == again.candidates == 3


def test_rejected_draft_is_not_reproposed_from_the_same_turn() -> None:
    # After a rejection the draft is no longer open, but its turn is still "proposed".
    item = reviewed()
    assert plan([item], proposed=[item.turn_id]).proposals == []


# --- unsafe answers: priority drafts ---------------------------------------------------


def test_unsafe_answer_becomes_a_priority_draft() -> None:
    item = reviewed("Can I double my insulin?", verdict="unsafe", note="Told them to double it.")
    result = plan([item, reviewed("What is A1C?")])
    unsafe, normal = result.proposals
    assert unsafe.priority and unsafe.proposed_from_turn_id == item.turn_id
    assert unsafe.review_hint.startswith(f'{PRIORITY_LABEL}. Reviewer marked the answer "Unsafe"')
    assert "Told them to double it." in unsafe.review_hint
    assert not normal.priority
    assert PRIORITY_LABEL == "Marked unsafe by a reviewer"


def test_only_unsafe_reviews_are_priority() -> None:
    result = plan(
        [reviewed("a", verdict="incorrect"), reviewed("b")],
        [asked("c"), asked("c")],
    )
    assert [d.priority for d in result.proposals] == [False, False, False]


def test_unsafe_review_is_planned_before_older_reviews_of_the_same_question() -> None:
    older = reviewed("Is jaggery OK?", verdict="needs_kb_update", at=T0)
    unsafe = reviewed("is jaggery ok", verdict="unsafe", at=T0 + timedelta(days=2))
    (draft,) = plan([older, unsafe]).proposals
    assert draft.priority and draft.proposed_from_turn_id == unsafe.turn_id


def test_unsafe_review_promotes_a_matching_open_draft_instead_of_skipping() -> None:
    existing = OpenDraft(id=uuid.uuid4(), title="Can I double my insulin?")
    result = plan([reviewed("can i double my insulin", verdict="unsafe")], open_drafts=[existing])
    assert result.proposals == []
    assert result.promote_draft_ids == [existing.id]
    assert result.skipped == 0 and result.candidates == 1


def test_unsafe_re_review_promotes_the_draft_from_the_same_turn() -> None:
    # The practitioner already retitled the draft, so only the turn links them.
    item = reviewed("can i double my insulin", verdict="unsafe")
    existing = OpenDraft(id=uuid.uuid4(), title="Insulin doses", proposed_from_turn_id=item.turn_id)
    result = plan([item], open_drafts=[existing], proposed=[item.turn_id])
    assert result.promote_draft_ids == [existing.id] and result.proposals == []


def test_an_existing_priority_draft_is_not_promoted_again() -> None:
    existing = OpenDraft(id=uuid.uuid4(), title="Insulin doses", priority=True)
    result = plan([reviewed("insulin doses", verdict="unsafe")], open_drafts=[existing])
    assert result.promote_draft_ids == [] and result.proposals == [] and result.skipped == 1


def test_non_unsafe_review_never_promotes() -> None:
    existing = OpenDraft(id=uuid.uuid4(), title="Insulin doses")
    result = plan([reviewed("insulin doses", verdict="incorrect")], open_drafts=[existing])
    assert result.promote_draft_ids == [] and result.skipped == 1


def test_unsafe_turn_whose_draft_was_closed_is_not_proposed_again() -> None:
    item = reviewed("Can I double my insulin?", verdict="unsafe")
    result = plan([item], proposed=[item.turn_id])
    assert result.proposals == [] and result.promote_draft_ids == [] and result.skipped == 1
