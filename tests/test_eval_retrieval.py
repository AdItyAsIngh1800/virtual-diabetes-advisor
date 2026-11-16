"""Tests for scripts.eval_retrieval: metrics, threshold choice and the labelled KB set (R4/R5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from scripts.eval_retrieval import (
    DEFAULT_CSV,
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


def result(category: str, expected: str | None, top: str | None, score: float) -> CaseResult:
    case = EvalCase.model_validate(
        {"question": f"q {category} {score}", "expected_key": expected, "category": category}
    )
    return CaseResult(case=case, top_key=top, score=score)


# Two in scope (one right entry, one wrong), one off-topic, one dosing.
TOY = [
    result("in_scope", "a1c", "a1c", 0.30),
    result("in_scope", "alcohol", "driving", 0.20),
    result("off_topic", None, "alcohol", 0.14),
    result("dosing", None, "prediabetes", 0.18),
]


def row(threshold: float, accuracy: float, dosing: int) -> ThresholdRow:
    return ThresholdRow(
        threshold=threshold,
        accuracy=accuracy,
        answer_precision=1.0,
        answer_recall=1.0,
        dont_know_precision=1.0,
        dont_know_recall=1.0,
        dosing_answered=dosing,
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


def test_thresholds_cover_0_10_to_0_25() -> None:
    assert THRESHOLDS[0] == 0.10 and THRESHOLDS[-1] == 0.25 and len(THRESHOLDS) == 16
    assert [r.threshold for r in sweep(TOY)] == list(THRESHOLDS)


# --- threshold choice ----------------------------------------------------------------------


def test_choose_threshold_never_answers_dosing_even_for_better_accuracy() -> None:
    rows = [row(0.15, 0.95, dosing=1), row(0.19, 0.90, dosing=0), row(0.20, 0.85, dosing=0)]
    assert choose_threshold(rows) == 0.19


def test_choose_threshold_takes_the_middle_of_a_tie() -> None:
    rows = [row(t, 0.9, 0) for t in (0.17, 0.18, 0.19)] + [row(0.20, 0.8, 0)]
    assert choose_threshold(rows) == 0.18


def test_choose_threshold_falls_back_to_accuracy_when_nothing_is_safe() -> None:
    assert choose_threshold([row(0.10, 0.7, 2), row(0.11, 0.8, 1)]) == 0.11


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


def test_fixture_has_about_60_labelled_questions_including_the_reported_ones() -> None:
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


# --- the real KB ---------------------------------------------------------------------------


def test_config_default_is_the_threshold_the_evaluation_chooses() -> None:
    # Changing the KB, the synonyms or the fixture must re-run the evaluation and
    # update CHAT_KB_MIN_SIMILARITY with it.
    results = retrieve(load_cases(), build_index())
    assert choose_threshold(sweep(results)) == SETTINGS.chat_kb_min_similarity


def test_seed_kb_retrieval_quality_at_the_chosen_threshold() -> None:
    results = retrieve(load_cases(), build_index())
    chosen = metrics_at(results, SETTINGS.chat_kb_min_similarity)
    assert top1_accuracy(results) >= 0.9
    assert chosen.dosing_answered == 0
    assert chosen.answer_precision >= 0.9
    jokes = [r for r in results if r.case.question == "tell me a joke"]
    assert jokes[0].score < SETTINGS.chat_kb_min_similarity


def test_report_marks_the_chosen_threshold_and_lists_errors() -> None:
    report = format_report(TOY, sweep(TOY))
    # 0.19-0.25 all score 0.75 without answering the dosing question: middle is 0.22.
    assert "| 0.22 **(chosen)** |" in report
    assert "Chosen threshold: 0.22" in report
    assert "Top-1 accuracy (in scope, any threshold): 0.500" in report
    assert "expected alcohol): top driving at 0.200" in report  # declined at 0.22


def test_main_prints_the_table(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--csv", str(DEFAULT_CSV)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("KB retrieval evaluation: 62 questions")
    assert "| Threshold | Accuracy | Answer P | Answer R |" in out
