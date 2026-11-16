"""Evaluate help-chat KB retrieval and choose ``CHAT_KB_MIN_SIMILARITY`` (R4/R5).

Runs every question in a labelled CSV (default ``tests/fixtures/kb_eval.csv``) through
the same TF-IDF index the chat uses, built from the seeded knowledge base
(:data:`scripts.kb_seed.KB_SEED`), and reports for each threshold from 0.10 to 0.25:

* **Top-1 accuracy** (threshold-free): share of in-scope questions whose best hit is
  the expected entry.
* **Answer precision / recall**: an *answer* is any question scoring at or above the
  threshold. It is correct when the question is in scope and the best hit is the
  expected entry. Precision = correct / answered; recall = correct / in-scope.
* **Don't-know precision / recall**: a *don't know* is any question below the
  threshold. It is correct when the question is out of scope (off-topic, or a
  dosing/diagnosis question the KB must never answer). Precision = correct
  don't-knows / don't-knows; recall = correct don't-knows / out-of-scope.
* **Accuracy**: (correct answers + correct don't-knows) / all questions.

Threshold choice (:func:`choose_threshold`): never answer a dosing/diagnosis question
(safety first), then the best accuracy; ties go to the middle of the tied run so the
cut-off keeps a margin on both sides.

CSV columns: ``question``, ``expected_key`` (``kb_seed`` key; empty when out of scope),
``category`` (``in_scope``, ``off_topic`` or ``dosing``).

Usage::

    uv run python -m scripts.eval_retrieval [--csv PATH]
"""

from __future__ import annotations

import argparse
import csv
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from app.services.chat import KbDoc, TfidfIndex
from scripts.kb_seed import KB_SEED, SEED_PREFIX, KbSeedEntry

DEFAULT_CSV: Final = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "kb_eval.csv"
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
    """Retrieval result for one question.

    Attributes:
        case: The labelled question.
        top_key: Key of the best hit, or ``None`` when nothing matched.
        score: Best similarity (0 when nothing matched).
    """

    model_config = ConfigDict(frozen=True)

    case: EvalCase
    top_key: str | None
    score: float

    @property
    def top1_correct(self) -> bool:
        """Whether the best hit is the expected entry (in-scope rows only).

        Returns:
            ``True`` for an in-scope question whose best hit is the expected entry.
        """
        return self.case.expected_key is not None and self.top_key == self.case.expected_key


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
    """Run each question through the index.

    Args:
        cases: Labelled questions.
        index: Retrieval index.

    Returns:
        One result per case, in order.
    """
    results = []
    for case in cases:
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
    answered = [r for r in results if r.score >= threshold]
    declined = [r for r in results if r.score < threshold]
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
    """Pick the threshold: no dosing answers, then best accuracy, then the middle of ties.

    Args:
        rows: Sweep rows, sorted by threshold.

    Returns:
        The chosen threshold.

    Raises:
        ValueError: If ``rows`` is empty.
    """
    if not rows:
        raise ValueError("no thresholds to choose from")
    # Safety first: a dosing/diagnosis question must get "I don't know", never KB text.
    safe = [r for r in rows if r.dosing_answered == 0] or list(rows)
    best = max(r.accuracy for r in safe)
    tied = [r.threshold for r in safe if r.accuracy == best]
    # Middle of the tied run: the furthest from flipping a question either way.
    return tied[(len(tied) - 1) // 2]


def format_report(results: Sequence[CaseResult], rows: Sequence[ThresholdRow]) -> str:
    """Render the results as Markdown for the course report.

    Args:
        results: Retrieval results.
        rows: Sweep rows.

    Returns:
        Markdown text: summary, threshold table and the questions it gets wrong.
    """
    chosen = choose_threshold(rows)
    in_scope = sum(r.case.category == "in_scope" for r in results)
    lines = [
        f"KB retrieval evaluation: {len(results)} questions ({in_scope} in scope, "
        f"{len(results) - in_scope} off-topic or dosing), {len(KB_SEED)} KB entries.",
        f"Top-1 accuracy (in scope, any threshold): {top1_accuracy(results):.3f}",
        "",
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
    lines += ["", f"Chosen threshold: {chosen:.2f}", "", f"Errors at {chosen:.2f}:"]
    errors = [
        r
        for r in results
        if (r.case.category == "in_scope" and not (r.score >= chosen and r.top1_correct))
        or (r.case.category != "in_scope" and r.score >= chosen)
    ]
    for r in errors:
        lines.append(
            f"- {r.case.question!r} ({r.case.category}, expected "
            f"{r.case.expected_key or 'dont-know'}): top {r.top_key} at {r.score:.3f}"
        )
    if not errors:
        lines.append("- none")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the evaluation and print the Markdown report.

    Args:
        argv: Command-line arguments (``None`` reads ``sys.argv``).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="labelled questions")
    args = parser.parse_args(argv)
    results = retrieve(load_cases(args.csv), build_index())
    # A CLI report for the user to paste, so stdout rather than the log.
    print(format_report(results, sweep(results)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
