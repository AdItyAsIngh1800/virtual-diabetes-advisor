"""Evaluate help-chat KB retrieval and choose ``CHAT_KB_MIN_SIMILARITY`` (R4/R5).

Runs labelled questions through what the chat does before answering a question: the
dosing/self-diagnosis guard (:func:`app.services.chat.is_medical_advice_request`),
then the same TF-IDF index, built from the seeded knowledge base
(:data:`scripts.kb_seed.KB_SEED`). Two question sets are kept apart:

* **Tuning set** (``tests/fixtures/kb_eval.csv``): used to tune synonyms and to
  choose the threshold.
* **Held-out set** (``tests/fixtures/kb_eval_holdout.csv``): written after tuning and
  never used for any choice. It is scored at the threshold already chosen, to show
  how the tuned retrieval does on questions it has not seen.

Metrics, for each threshold from 0.10 to 0.25:

* **Top-1 accuracy** (threshold-free): share of in-scope questions whose best hit is
  the expected entry (a guarded in-scope question counts as a miss).
* **Answer precision / recall**: an *answer* is any unguarded question scoring at or
  above the threshold. It is correct when the question is in scope and the best hit is
  the expected entry. Precision = correct / answered; recall = correct / in-scope.
* **Don't-know precision / recall**: a *don't know* is any question that is guarded
  or below the threshold. It is correct when the question is out of scope (off-topic,
  or dosing/diagnosis). Precision = correct don't-knows / don't-knows; recall =
  correct don't-knows / out-of-scope.
* **Accuracy**: (correct answers + correct don't-knows) / all questions.

Threshold choice (:func:`choose_threshold`): the best accuracy on the tuning set; ties
go to the middle of the tied run so the cut-off keeps a margin on both sides. The guard,
not the threshold, keeps dosing and diagnosis questions away from KB answers.

CSV columns: ``question``, ``expected_key`` (``kb_seed`` key; empty when out of scope),
``category`` (``in_scope``, ``off_topic`` or ``dosing``).

Usage::

    uv run python -m scripts.eval_retrieval [--csv PATH] [--holdout PATH]
"""

from __future__ import annotations

import argparse
import csv
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from app.services.chat import KbDoc, TfidfIndex, is_medical_advice_request
from scripts.kb_seed import KB_SEED, SEED_PREFIX, KbSeedEntry

_FIXTURES: Final = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
DEFAULT_CSV: Final = _FIXTURES / "kb_eval.csv"
HOLDOUT_CSV: Final = _FIXTURES / "kb_eval_holdout.csv"
# The range asked for in the course report: 0.10 to 0.25 in steps of 0.01.
THRESHOLDS: Final[tuple[float, ...]] = tuple(round(0.10 + i / 100, 2) for i in range(16))

Category = Literal["in_scope", "off_topic", "dosing"]


class EvalCase(BaseModel):
    """One labelled question (a CSV row).

    Attributes:
        question: What the patient asks.
        expected_key: ``kb_seed`` key of the entry that should answer, or ``None``.
        category: ``in_scope``, ``off_topic`` or ``dosing``.
    """

    model_config = ConfigDict(frozen=True)

    question: str
    expected_key: str | None
    category: Category

    @model_validator(mode="after")
    def _key_matches_category(self) -> Self:
        """Require a key for in-scope rows and none for out-of-scope rows.

        Returns:
            The validated case.

        Raises:
            ValueError: If the key and category disagree.
        """
        if (self.category == "in_scope") != (self.expected_key is not None):
            raise ValueError(f"expected_key and category disagree for {self.question!r}")
        return self


class CaseResult(BaseModel):
    """What the chat would do with one question.

    Attributes:
        case: The labelled question.
        guarded: The dosing/diagnosis guard answered it (no retrieval).
        top_key: Key of the best hit, or ``None`` when nothing matched or guarded.
        score: Best similarity (0 when nothing matched or guarded).
    """

    model_config = ConfigDict(frozen=True)

    case: EvalCase
    guarded: bool = False
    top_key: str | None
    score: float

    @property
    def top1_correct(self) -> bool:
        """Whether the best hit is the expected entry (in-scope rows only).

        Returns:
            ``True`` for an unguarded in-scope question whose best hit is expected.
        """
        return self.case.expected_key is not None and self.top_key == self.case.expected_key

    def answered(self, threshold: float) -> bool:
        """Whether the chat would answer from the KB at ``threshold``.

        Args:
            threshold: Minimum similarity (same rule as ``ChatEngine``).

        Returns:
            ``True`` if unguarded and the best score reaches the threshold.
        """
        return not self.guarded and self.score >= threshold


class ThresholdRow(BaseModel):
    """Metrics at one threshold.

    Attributes:
        threshold: Minimum similarity to answer.
        accuracy: Correct answers plus correct don't-knows, over all questions.
        answer_precision: Correct answers / answered (1.0 when nothing answered).
        answer_recall: Correct answers / in-scope questions.
        dont_know_precision: Out-of-scope don't-knows / don't-knows (1.0 when none).
        dont_know_recall: Out-of-scope don't-knows / out-of-scope questions.
        dosing_answered: Dosing/diagnosis questions that would get a KB answer.
    """

    model_config = ConfigDict(frozen=True)

    threshold: float
    accuracy: float
    answer_precision: float
    answer_recall: float
    dont_know_precision: float
    dont_know_recall: float
    dosing_answered: int


def load_cases(path: Path = DEFAULT_CSV) -> list[EvalCase]:
    """Read and validate the labelled questions.

    Args:
        path: CSV with ``question``, ``expected_key`` and ``category`` columns.

    Returns:
        The cases, in file order.

    Raises:
        ValueError: If a row is invalid or names an entry that is not in the seed KB.
    """
    known = {e.key for e in KB_SEED}
    with path.open(newline="", encoding="utf-8") as handle:
        cases = [
            EvalCase.model_validate(
                {
                    "question": row["question"],
                    "expected_key": row["expected_key"] or None,
                    "category": row["category"],
                }
            )
            for row in csv.DictReader(handle)
        ]
    unknown = sorted({c.expected_key for c in cases if c.expected_key} - known)
    if unknown:
        raise ValueError(f"unknown KB keys in {path}: {unknown}")
    return cases


def build_index(entries: Sequence[KbSeedEntry] = KB_SEED) -> TfidfIndex:
    """Build the chat's TF-IDF index over the seed entries.

    Args:
        entries: Knowledge-base entries.

    Returns:
        The index (``entry_key`` = ``seed:<key>``, as in the database).
    """
    docs = [
        KbDoc(
            id=uuid.uuid4(),
            entry_key=SEED_PREFIX + e.key,
            title=e.title,
            body=e.body,
            source_citation=e.source,
            kb_version=1,
        )
        for e in entries
    ]
    return TfidfIndex(docs)


def retrieve(cases: Sequence[EvalCase], index: TfidfIndex) -> list[CaseResult]:
    """Run each question through the guard, then the index.

    Args:
        cases: Labelled questions.
        index: Retrieval index.

    Returns:
        One result per case, in order.
    """
    results = []
    for case in cases:
        if is_medical_advice_request(case.question):
            results.append(CaseResult(case=case, guarded=True, top_key=None, score=0.0))
            continue
        hits = index.search(case.question, k=1)
        top_key = hits[0].doc.entry_key.removeprefix(SEED_PREFIX) if hits else None
        results.append(CaseResult(case=case, top_key=top_key, score=hits[0].score if hits else 0.0))
    return results


def top1_accuracy(results: Sequence[CaseResult]) -> float:
    """Share of in-scope questions whose best hit is the expected entry.

    Args:
        results: Retrieval results.

    Returns:
        Accuracy in [0, 1] (0 when there are no in-scope questions).
    """
    in_scope = [r for r in results if r.case.category == "in_scope"]
    return sum(r.top1_correct for r in in_scope) / len(in_scope) if in_scope else 0.0


def _ratio(numerator: int, denominator: int) -> float:
    """Divide, treating an empty denominator as perfect (nothing to get wrong).

    Args:
        numerator: Count of correct items.
        denominator: Count of items.

    Returns:
        The ratio, or 1.0 when ``denominator`` is 0.
    """
    return numerator / denominator if denominator else 1.0


def metrics_at(results: Sequence[CaseResult], threshold: float) -> ThresholdRow:
    """Compute answer / don't-know metrics at one threshold.

    Args:
        results: Retrieval results.
        threshold: Minimum similarity to answer (same rule as ``ChatEngine``).

    Returns:
        The metrics.
    """
    answered = [r for r in results if r.answered(threshold)]
    declined = [r for r in results if not r.answered(threshold)]
    in_scope = sum(r.case.category == "in_scope" for r in results)
    out_scope = len(results) - in_scope
    correct_answers = sum(r.top1_correct for r in answered)
    correct_declines = sum(r.case.category != "in_scope" for r in declined)
    return ThresholdRow(
        threshold=threshold,
        accuracy=_ratio(correct_answers + correct_declines, len(results)),
        answer_precision=_ratio(correct_answers, len(answered)),
        answer_recall=_ratio(correct_answers, in_scope),
        dont_know_precision=_ratio(correct_declines, len(declined)),
        dont_know_recall=_ratio(correct_declines, out_scope),
        dosing_answered=sum(r.case.category == "dosing" for r in answered),
    )


def sweep(
    results: Sequence[CaseResult], thresholds: Sequence[float] = THRESHOLDS
) -> list[ThresholdRow]:
    """Metrics at every threshold.

    Args:
        results: Retrieval results.
        thresholds: Thresholds to try.

    Returns:
        One row per threshold, in the given order.
    """
    return [metrics_at(results, t) for t in thresholds]


def choose_threshold(rows: Sequence[ThresholdRow]) -> float:
    """Pick the threshold with the best accuracy.

    Ties: first the higher don't-know recall (answering an off-topic question is the
    worse error), then the middle of what is left, rounding toward the higher (safer)
    threshold. Call it with tuning-set rows only: the held-out set must not influence
    the choice.

    Args:
        rows: Sweep rows, sorted by threshold.

    Returns:
        The chosen threshold.

    Raises:
        ValueError: If ``rows`` is empty.
    """
    if not rows:
        raise ValueError("no thresholds to choose from")
    best = max(r.accuracy for r in rows)
    tied = [r for r in rows if r.accuracy == best]
    safest = max(r.dont_know_recall for r in tied)
    candidates = [r.threshold for r in tied if r.dont_know_recall == safest]
    # Middle: the furthest from flipping a question either way.
    return candidates[len(candidates) // 2]


def _table(rows: Sequence[ThresholdRow], chosen: float) -> list[str]:
    """Render sweep rows as a Markdown table.

    Args:
        rows: Sweep rows.
        chosen: Threshold to mark.

    Returns:
        Table lines.
    """
    lines = [
        "| Threshold | Accuracy | Answer P | Answer R | Don't-know P | Don't-know R "
        "| Dosing answered |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        mark = " **(chosen)**" if row.threshold == chosen else ""
        lines.append(
            f"| {row.threshold:.2f}{mark} | {row.accuracy:.3f} | {row.answer_precision:.3f} "
            f"| {row.answer_recall:.3f} | {row.dont_know_precision:.3f} "
            f"| {row.dont_know_recall:.3f} | {row.dosing_answered} |"
        )
    return lines


def _section(title: str, results: Sequence[CaseResult], chosen: float) -> list[str]:
    """Summary, threshold table and errors for one question set.

    Args:
        title: Section heading.
        results: Retrieval results for the set.
        chosen: The threshold chosen on the tuning set.

    Returns:
        Markdown lines.
    """
    in_scope = sum(r.case.category == "in_scope" for r in results)
    guarded = sum(r.guarded for r in results)
    lines = [
        f"## {title}",
        "",
        f"{len(results)} questions ({in_scope} in scope, {len(results) - in_scope} "
        f"off-topic or dosing); {guarded} stopped by the dosing/diagnosis guard.",
        f"Top-1 accuracy (in scope, any threshold): {top1_accuracy(results):.3f}",
        "",
        *_table(sweep(results), chosen),
        "",
        f"Errors at {chosen:.2f}:",
    ]
    errors = [
        r
        for r in results
        if (r.case.category == "in_scope" and not (r.answered(chosen) and r.top1_correct))
        or (r.case.category != "in_scope" and r.answered(chosen))
    ]
    for r in errors:
        where = "guarded" if r.guarded else f"top {r.top_key} at {r.score:.3f}"
        lines.append(
            f"- {r.case.question!r} ({r.case.category}, expected "
            f"{r.case.expected_key or 'dont-know'}): {where}"
        )
    if not errors:
        lines.append("- none")
    return lines


def format_report(tuning: Sequence[CaseResult], holdout: Sequence[CaseResult]) -> str:
    """Render both sets as Markdown for the course report.

    The threshold is chosen from ``tuning`` alone; ``holdout`` is only scored.

    Args:
        tuning: Results on the tuning set.
        holdout: Results on the held-out set.

    Returns:
        Markdown text.
    """
    chosen = choose_threshold(sweep(tuning))
    held = metrics_at(holdout, chosen)
    lines = [
        f"# KB retrieval evaluation ({len(KB_SEED)} KB entries)",
        "",
        f"Chosen threshold: {chosen:.2f} (best accuracy on the tuning set; ties: higher "
        "don't-know recall, then the middle).",
        f"Held-out at {chosen:.2f}: accuracy {held.accuracy:.3f}, answer precision "
        f"{held.answer_precision:.3f}, answer recall {held.answer_recall:.3f}, "
        f"dosing answered {held.dosing_answered}.",
        "",
        *_section("Tuning set (used to choose the threshold)", tuning, chosen),
        "",
        *_section("Held-out set (not used for any choice)", holdout, chosen),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the evaluation and print the Markdown report.

    Args:
        argv: Command-line arguments (``None`` reads ``sys.argv``).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="tuning questions")
    parser.add_argument("--holdout", type=Path, default=HOLDOUT_CSV, help="held-out questions")
    args = parser.parse_args(argv)
    index = build_index()
    report = format_report(
        retrieve(load_cases(args.csv), index), retrieve(load_cases(args.holdout), index)
    )
    # A CLI report for the user to paste, so stdout rather than the log.
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
