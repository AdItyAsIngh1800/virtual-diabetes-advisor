"""Tests for scripts.eval_retrieval: metrics, threshold choice and the labelled KB sets (R4/R5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from scripts.eval_retrieval import (
    DEFAULT_CSV,
    HOLDOUT_CSV,
    THRESHOLDS,
    CaseResult,
    EvalCase,
    ThresholdRow,
    build_index,
    choose_threshold,
    format_report,
    load_cases,
    main,
    metrics_at,
    retrieve,
    sweep,
    top1_accuracy,
)

SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="srk",
    database_url="postgresql://u:p@localhost:5432/db",
)


def result(
    category: str, expected: str | None, top: str | None, score: float, *, guarded: bool = False
) -> CaseResult:
    case = EvalCase.model_validate(
        {"question": f"q {category} {score}", "expected_key": expected, "category": category}
    )
    return CaseResult(case=case, top_key=top, score=score, guarded=guarded)


# Two in scope (one right entry, one wrong), one off-topic, one dosing.
TOY = [
    result("in_scope", "a1c", "a1c", 0.30),
    result("in_scope", "alcohol", "driving", 0.20),
    result("off_topic", None, "alcohol", 0.14),
    result("dosing", None, "prediabetes", 0.18),
]


def row(threshold: float, accuracy: float, dont_know_recall: float = 1.0) -> ThresholdRow:
    return ThresholdRow(
        threshold=threshold,
        accuracy=accuracy,
        answer_precision=1.0,
        answer_recall=1.0,
        dont_know_precision=1.0,
        dont_know_recall=dont_know_recall,
        dosing_answered=0,
    )


# --- metrics ---------------------------------------------------------------------------


def test_top1_accuracy_counts_in_scope_rows_only() -> None:
    assert top1_accuracy(TOY) == 0.5
    assert top1_accuracy([]) == 0.0


def test_metrics_at_a_low_threshold_answers_everything() -> None:
    m = metrics_at(TOY, 0.10)
    assert m.answer_precision == 0.25  # 1 correct of 4 answered
    assert m.answer_recall == 0.5  # 1 of 2 in scope
    assert m.dont_know_precision == 1.0  # nothing declined: nothing wrong
    assert m.dont_know_recall == 0.0
    assert m.dosing_answered == 1
    assert m.accuracy == 0.25


def test_metrics_at_a_middle_threshold() -> None:
    m = metrics_at(TOY, 0.19)
    # Answered: a1c (right), alcohol->driving (wrong). Declined: off-topic, dosing.
    assert m.answer_precision == 0.5
    assert m.answer_recall == 0.5
    assert m.dont_know_precision == 1.0
    assert m.dont_know_recall == 1.0
    assert m.dosing_answered == 0
    assert m.accuracy == 0.75


def test_metrics_score_equal_to_threshold_is_answered() -> None:
    # Same rule as ChatEngine: below the threshold is "don't know".
    assert metrics_at(TOY, 0.18).dosing_answered == 1
    assert metrics_at(TOY, 0.181).dosing_answered == 0


def test_metrics_at_a_high_threshold_declines_everything() -> None:
    m = metrics_at(TOY, 0.50)
    assert m.answer_precision == 1.0 and m.answer_recall == 0.0
    assert m.dont_know_precision == 0.5 and m.dont_know_recall == 1.0


def test_guarded_questions_are_dont_knows_at_every_threshold() -> None:
    guarded_dosing = result("dosing", None, None, 0.0, guarded=True)
    guarded_in_scope = result("in_scope", "a1c", None, 0.0, guarded=True)
    for threshold in THRESHOLDS:
        m = metrics_at([guarded_dosing, guarded_in_scope], threshold)
        assert m.dosing_answered == 0
        assert m.dont_know_recall == 1.0  # the dosing question: correct don't-know
        assert m.answer_recall == 0.0  # the over-guarded in-scope question: a miss


def test_thresholds_cover_0_10_to_0_25() -> None:
    assert THRESHOLDS[0] == 0.10 and THRESHOLDS[-1] == 0.25 and len(THRESHOLDS) == 16
    assert [r.threshold for r in sweep(TOY)] == list(THRESHOLDS)


# --- threshold choice ----------------------------------------------------------------------


def test_choose_threshold_takes_the_best_accuracy() -> None:
    rows = [row(0.15, 0.95), row(0.19, 0.90), row(0.20, 0.85)]
    assert choose_threshold(rows) == 0.15


def test_choose_threshold_takes_the_middle_of_a_tie() -> None:
    rows = [row(t, 0.9) for t in (0.17, 0.18, 0.19)] + [row(0.20, 0.8)]
    assert choose_threshold(rows) == 0.18


def test_choose_threshold_breaks_ties_by_dont_know_recall_then_rounds_up() -> None:
    # Same accuracy at 0.10-0.13 and 0.15-0.16; only 0.15-0.16 declines every
    # off-topic question, and the middle of two rounds toward the safer, higher one.
    rows = [row(t, 0.935, 0.9) for t in (0.10, 0.11, 0.12, 0.13)]
    rows += [row(0.14, 0.919, 0.9), row(0.15, 0.935), row(0.16, 0.935), row(0.17, 0.9)]
    assert choose_threshold(rows) == 0.16


def test_choose_threshold_needs_rows() -> None:
    with pytest.raises(ValueError, match="no thresholds"):
        choose_threshold([])


# --- CSV loading -------------------------------------------------------------------------


def test_key_and_category_must_agree() -> None:
    with pytest.raises(ValueError, match="disagree"):
        EvalCase.model_validate({"question": "q", "expected_key": None, "category": "in_scope"})
    with pytest.raises(ValueError, match="disagree"):
        EvalCase.model_validate({"question": "q", "expected_key": "a1c", "category": "dosing"})


def test_unknown_kb_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("question,expected_key,category\nwhat?,no-such-entry,in_scope\n")
    with pytest.raises(ValueError, match="no-such-entry"):
        load_cases(path)


def test_tuning_set_has_about_60_labelled_questions_including_the_reported_ones() -> None:
    cases = load_cases()
    in_scope = [c for c in cases if c.category == "in_scope"]
    out_scope = [c for c in cases if c.category != "in_scope"]
    assert len(cases) >= 60 and len(in_scope) >= 40 and len(out_scope) >= 20
    assert {c.category for c in out_scope} == {"off_topic", "dosing"}
    labelled = {c.question: c.expected_key for c in cases}
    assert labelled["what should i do if my sugar is low"] == "rule-15-15"
    assert labelled["how do i treat a hypo"] == "rule-15-15"
    assert labelled["is rice okay for diabetics"] == "healthy-eating"
    assert labelled["tell me a joke"] is None
    assert len(labelled) == len(cases)  # no duplicate questions


def test_holdout_set_is_new_questions_only() -> None:
    holdout = load_cases(HOLDOUT_CSV)
    in_scope = [c for c in holdout if c.category == "in_scope"]
    assert len(holdout) == 25 and len(in_scope) == 17
    assert {c.category for c in holdout if c.category != "in_scope"} == {"off_topic", "dosing"}
    tuning = {c.question for c in load_cases()}
    assert not tuning & {c.question for c in holdout}


# --- the real KB ---------------------------------------------------------------------------


def tuning_results() -> list[CaseResult]:
    return retrieve(load_cases(), build_index())


def test_config_default_is_the_threshold_the_tuning_set_chooses() -> None:
    # Changing the KB, the synonyms, the guard or the tuning set must re-run the
    # evaluation and update CHAT_KB_MIN_SIMILARITY with it.
    assert choose_threshold(sweep(tuning_results())) == SETTINGS.chat_kb_min_similarity


def test_seed_kb_retrieval_quality_at_the_chosen_threshold() -> None:
    results = tuning_results()
    chosen = metrics_at(results, SETTINGS.chat_kb_min_similarity)
    assert top1_accuracy(results) >= 0.9
    assert chosen.dosing_answered == 0
    assert chosen.answer_precision >= 0.9
    joke = next(r for r in results if r.case.question == "tell me a joke")
    assert not joke.guarded and joke.score < SETTINGS.chat_kb_min_similarity


def test_every_dosing_question_is_guarded_in_both_sets() -> None:
    for path in (DEFAULT_CSV, HOLDOUT_CSV):
        for r in retrieve(load_cases(path), build_index()):
            assert r.guarded == (r.case.category == "dosing"), r.case.question


def test_holdout_cannot_change_the_chosen_threshold() -> None:
    tuning = tuning_results()
    holdout = retrieve(load_cases(HOLDOUT_CSV), build_index())
    flipped = [r.model_copy(update={"score": 1.0 - r.score}) for r in holdout]
    first_lines = {format_report(tuning, h).splitlines()[2] for h in (holdout, flipped, [])}
    assert first_lines == {
        f"Chosen threshold: {SETTINGS.chat_kb_min_similarity:.2f} (best accuracy on the "
        "tuning set; ties: higher don't-know recall, then the middle)."
    }


def test_report_shows_both_sets_and_marks_the_choice() -> None:
    report = format_report(TOY, TOY[:2])
    # 0.19-0.25 all score 0.75 and decline every out-of-scope question: middle is 0.22.
    assert "Chosen threshold: 0.22" in report
    assert report.count("| 0.22 **(chosen)** |") == 2
    assert "## Tuning set (used to choose the threshold)" in report
    assert "## Held-out set (not used for any choice)" in report
    assert "expected alcohol): top driving at 0.200" in report  # declined at 0.22


def test_report_names_guarded_errors() -> None:
    over_guarded = [result("in_scope", "a1c", None, 0.0, guarded=True)]
    report = format_report(TOY, over_guarded)
    assert "1 stopped by the dosing/diagnosis guard" in report
    assert "expected a1c): guarded" in report


def test_main_prints_both_tables(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--csv", str(DEFAULT_CSV), "--holdout", str(HOLDOUT_CSV)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# KB retrieval evaluation")
    assert "62 questions (42 in scope, 20 off-topic or dosing)" in out
    assert "25 questions (17 in scope, 8 off-topic or dosing)" in out
    assert out.count("| Threshold | Accuracy | Answer P | Answer R |") == 2
