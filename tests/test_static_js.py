"""Tests for the pure helpers in the web app's ``common.js`` (run with Node).

Day separators in both chat views (patient ``/app`` and practitioner page): "Today",
"Yesterday", "16 Nov", grouped by *local* calendar day. The practitioner page's Review
tab (R5): verdict labels, the note rule, list URLs and card tags, kept in step with the
API. The Knowledge base tab (R5): the approval rule, draft origin lines, field limits
and the staff-only hint staying off patient pages. The Audit tab (R5): counts always
shown with their rates, the KB status line, the model card's metric order and the
limitations shown in full. Node tests are skipped where Node is
missing; the static checks always run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final, get_args

import pytest

from app.api.kb_admin import MAX_BODY_CHARS, MAX_CITATION_CHARS, MAX_TITLE_CHARS
from app.db.audit_repository import NOTE_REQUIRED, AnswerCounts, ReviewFilter
from app.db.models import RetrainDecision, ReviewVerdict
from app.ml.model_card import CardMetrics
from app.services.kb_upgrade import PRIORITY_LABEL

STATIC: Final = Path(__file__).resolve().parents[1] / "app" / "static" / "patient"
COMMON_JS: Final = STATIC / "common.js"
NODE: Final = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

# Loads common.js the way a browser would (it only needs `window` to attach to) and
# evaluates one expression against window.DA.
_RUNNER: Final = """
const fs = require("fs");
const vm = require("vm");
const context = { window: {}, console };
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
const DA = context.window.DA;
const out = vm.runInContext(process.argv[2], Object.assign(context, { DA }));
process.stdout.write(JSON.stringify(out));
"""


def run_js(expression: str) -> object:
    """Evaluate ``expression`` (with ``DA`` = ``window.DA``) in Node, India time.

    Args:
        expression: JavaScript expression.

    Returns:
        Its JSON-decoded value.
    """
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", _RUNNER, str(COMMON_JS), expression],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        # The demo cohort's timezone, so "local day" is deterministic.
        env={**os.environ, "TZ": "Asia/Kolkata"},
    )
    return json.loads(result.stdout)


NOW_JS: Final = 'new Date("2026-11-16T20:00:00+05:30")'


@needs_node
@pytest.mark.parametrize(
    ("iso", "label"),
    [
        ("2026-11-16T09:15:00+05:30", "Today"),
        ("2026-11-16T00:05:00+05:30", "Today"),
        ("2026-11-15T23:55:00+05:30", "Yesterday"),
        # 15 Nov 19:00 UTC is already 16 Nov 00:30 in India: local day, not UTC day.
        ("2026-11-15T19:00:00Z", "Today"),
        ("2026-11-14T10:00:00+05:30", "14 Nov"),
        ("2025-12-31T10:00:00+05:30", "31 Dec 2025"),
    ],
)
def test_day_label(iso: str, label: str) -> None:
    assert run_js(f'DA.dayLabel("{iso}", {NOW_JS})') == label


@needs_node
def test_with_days_inserts_one_separator_per_local_day() -> None:
    messages = [
        {"id": "a", "created_at": "2026-11-15T09:00:00+05:30"},  # the 15 Nov "Hello"
        {"id": "b", "created_at": "2026-11-15T09:01:00+05:30"},
        {"id": "c", "created_at": "2026-11-16T08:00:00+05:30"},
        {"id": "d", "created_at": "2026-11-16T20:00:00+05:30"},
    ]
    out = run_js(f"DA.withDays({json.dumps(messages)}, {NOW_JS})")
    assert out == [
        {"day": "Yesterday"},
        messages[0],
        messages[1],
        {"day": "Today"},
        messages[2],
        messages[3],
    ]


@needs_node
def test_with_days_on_an_empty_conversation() -> None:
    assert run_js(f"DA.withDays([], {NOW_JS})") == []


# --- Review tab (R5) -------------------------------------------------------------------

TURN: Final = {
    "escalated": False,
    "unanswered": False,
    "low_confidence": False,
    "retrieval_confidence": 0.42,
    "spoken": False,
    "current_review": None,
}


@needs_node
def test_verdicts_match_the_api() -> None:
    labels = run_js("DA.VERDICTS")
    assert isinstance(labels, dict)
    assert list(labels) == list(get_args(ReviewVerdict))


@needs_node
@pytest.mark.parametrize("verdict", get_args(ReviewVerdict))
def test_needs_note_matches_the_api(verdict: str) -> None:
    # The same rule as NOTE_REQUIRED, so the page never sends a review the API refuses.
    assert run_js(f'DA.needsNote("{verdict}")') is (verdict in NOTE_REQUIRED)


@needs_node
def test_review_query_builds_the_list_url() -> None:
    assert run_js('DA.reviewQuery("unreviewed")') == (
        "/api/practitioner/turns?filter=unreviewed&limit=25"
    )
    # URL-safe base64 cursors can end in "=" padding, which must be encoded.
    assert run_js('DA.reviewQuery("all", "eyJh=", 10)') == (
        "/api/practitioner/turns?filter=all&limit=10&cursor=eyJh%3D"
    )


def test_every_filter_button_is_a_filter_the_api_accepts() -> None:
    html = (STATIC / "practitioner.html").read_text(encoding="utf-8")
    buttons = re.findall(r'data-filter="([a-z_]+)"', html)
    assert sorted(buttons) == sorted(get_args(ReviewFilter))


def test_every_element_the_practitioner_script_uses_exists() -> None:
    html = (STATIC / "practitioner.html").read_text(encoding="utf-8")
    script = (STATIC / "practitioner.js").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([\w-]+)"', html))
    used = set(re.findall(r'\$\("([\w-]+)"\)', script))
    used |= {i for pair in re.findall(r'\["(tab-\w+)", "(\w+)"\]', script) for i in pair}
    assert {"tabs", "tab-review", "review", "turns", "more"} <= used
    assert used <= ids, f"missing in practitioner.html: {sorted(used - ids)}"


@needs_node
@pytest.mark.parametrize(
    ("changes", "tags"),
    [
        ({}, [("Unreviewed", False)]),
        (
            {"escalated": True, "spoken": True},
            [("Escalated", True), ("Voice", False), ("Unreviewed", False)],
        ),
        ({"unanswered": True}, [("Didn't know", False), ("Unreviewed", False)]),
        (
            {"low_confidence": True, "retrieval_confidence": 0.184},
            [("Low confidence 0.18", False), ("Unreviewed", False)],
        ),
        ({"current_review": {"verdict": "correct"}}, [("Correct", False)]),
        ({"current_review": {"verdict": "unsafe"}}, [("Unsafe", True)]),
        ({"current_review": {"verdict": "needs_kb_update"}}, [("Needs KB update", True)]),
    ],
)
def test_review_tags(changes: dict[str, object], tags: list[tuple[str, bool]]) -> None:
    turn = json.dumps({**TURN, **changes})
    out = run_js(f"DA.reviewTags({turn})")
    assert out == [{"text": text, "urgent": urgent} for text, urgent in tags]


# --- Knowledge base tab (R5) ------------------------------------------------------------


def _page() -> tuple[str, str]:
    return (
        (STATIC / "practitioner.html").read_text(encoding="utf-8"),
        (STATIC / "practitioner.js").read_text(encoding="utf-8"),
    )


@needs_node
@pytest.mark.parametrize(
    ("draft", "problem"),
    [
        ({"body": "Eat it in small portions.", "source_citation": "ICMR-NIN 2024"}, None),
        (
            {"body": "", "source_citation": None},
            "Add the entry text and a source citation before approving.",
        ),
        ({"body": "   ", "source_citation": "ADA"}, "Add the entry text before approving."),
        ({"body": "Text", "source_citation": "  "}, "Add a source citation before approving."),
        ({"body": "Text"}, "Add a source citation before approving."),
    ],
)
def test_kb_approve_problem_matches_the_api_rule(
    draft: dict[str, object], problem: str | None
) -> None:
    # The same rule as approve_draft: the page never sends an approval the API refuses.
    assert run_js(f"DA.kbApproveProblem({json.dumps(draft)})") == problem


@needs_node
@pytest.mark.parametrize(
    ("entry", "line"),
    [
        (
            {"origin": "proposed_from_review", "supersedes_id": None},
            "Proposed by the weekly knowledge-base review",
        ),
        (
            {
                "origin": "manual",
                "supersedes_id": "x",
                "supersedes_version": 3,
                "entry_key": "seed:a1c",
                "created_by_name": "Dr Demo",
            },
            "Edit of approved entry seed:a1c v3 by Dr Demo",
        ),
        ({"origin": "manual", "supersedes_id": None, "created_by_name": None}, "New entry"),
    ],
)
def test_kb_draft_origin(entry: dict[str, object], line: str) -> None:
    assert run_js(f"DA.kbDraftOrigin({json.dumps(entry)})") == line


def test_knowledge_base_tab_is_wired() -> None:
    html, script = _page()
    ids = set(re.findall(r'\bid="([\w-]+)"', html))
    used = set(re.findall(r'\$\("([\w-]+)"\)', script))
    tabs = re.findall(r'\["(tab-\w+)", "(\w+)"\]', script)
    assert ("tab-kb", "kb") in tabs
    assert {"kb-drafts", "kb-approved", "kb-drafts-empty", "kb-approved-empty"} <= used <= ids
    # Every tab button in the HTML is in the script's TABS list and vice versa.
    buttons = re.findall(r'role="tab" id="(tab-\w+)" aria-controls="(\w+)"', html)
    assert sorted(buttons) == sorted(tabs)
    # The script calls every KB route: list, save (PUT), edit, approve and reject.
    for route in ('/api/practitioner/kb"', "/approve`", "/reject`", "/edit`", '"PUT"'):
        assert route in script, route


def test_kb_field_limits_match_the_api() -> None:
    _, script = _page()
    limits = dict(re.findall(r"(title|body|citation)\.maxLength = (\d+);", script))
    assert limits == {
        "title": str(MAX_TITLE_CHARS),
        "body": str(MAX_BODY_CHARS),
        "citation": str(MAX_CITATION_CHARS),
    }


def test_staff_only_hint_never_reaches_patient_pages() -> None:
    # review_hint (the reviewer's note) is shown on the practitioner page only.
    for page in ("app.js", "index.html", "advisory.js", "advisory.html", "sw.js"):
        text = (STATIC / page).read_text(encoding="utf-8")
        assert "review_hint" not in text, page
        assert "/api/practitioner/kb" not in text, page
    _, script = _page()
    assert "entry.review_hint" in script


@needs_node
def test_priority_label_matches_the_planner() -> None:
    assert run_js("DA.KB_PRIORITY_LABEL") == PRIORITY_LABEL == "Marked unsafe by a reviewer"


@needs_node
@pytest.mark.parametrize(
    ("entry", "tags"),
    [
        (
            {"priority": True, "origin": "proposed_from_review", "supersedes_id": None},
            [("Marked unsafe by a reviewer", True), ("Proposed", False)],
        ),
        (
            {"priority": False, "origin": "proposed_from_review", "supersedes_id": None},
            [("Proposed", False)],
        ),
        ({"priority": False, "origin": "manual", "supersedes_id": "x"}, [("Edit", False)]),
        ({"priority": False, "origin": "manual", "supersedes_id": None}, []),
    ],
)
def test_kb_draft_tags(entry: dict[str, object], tags: list[tuple[str, bool]]) -> None:
    out = run_js(f"DA.kbDraftTags({json.dumps(entry)})")
    assert out == [{"text": text, "urgent": urgent} for text, urgent in tags]


@needs_node
def test_priority_drafts_are_shown_first_in_api_order() -> None:
    drafts = [
        {"id": "a", "priority": False},
        {"id": "b", "priority": True},
        {"id": "c", "priority": False},
        {"id": "d", "priority": True},
    ]
    out = run_js(f"DA.kbSortDrafts({json.dumps(drafts)}).map((d) => d.id)")
    assert out == ["b", "d", "a", "c"]


def test_priority_draft_card_is_highlighted() -> None:
    html, script = _page()
    assert ".turn.priority" in html
    assert 'entry.priority ? " priority" : ""' in script
    assert "DA.kbSortDrafts(listing.drafts)" in script
    assert "DA.kbDraftTags(entry)" in script


# --- Audit tab (R5) ---------------------------------------------------------------------


def test_audit_tab_is_wired() -> None:
    html, script = _page()
    ids = set(re.findall(r'\bid="([\w-]+)"', html))
    used = set(re.findall(r'\$\("([\w-]+)"\)', script))
    tabs = re.findall(r'\["(tab-\w+)", "(\w+)"\]', script)
    assert ("tab-audit", "audit") in tabs
    buttons = re.findall(r'role="tab" id="(tab-\w+)" aria-controls="(\w+)"', html)
    assert sorted(buttons) == sorted(tabs)
    assert (
        {
            "audit-summary-body",
            "audit-by-kb-body",
            "audit-kb-status",
            "audit-since",
            "audit-until",
            "model-metrics-body",
            "model-limitations",
            "model-flagged",
            "model-retrain",
            "model-error",
        }
        <= used
        <= ids
    )
    # The confusion matrix is a labelled 2x2 table filled cell by cell.
    assert {"cm-tn", "cm-fp", "cm-fn", "cm-tp"} <= ids
    assert 'for (const key of ["tn", "fp", "fn", "tp"])' in script
    for route in ("/api/practitioner/model-card", "DA.auditQuery("):
        assert route in script, route
    # Plain tables: no chart library on the page.
    for lib in ("chart.js", "chartjs", "d3", "plotly", "recharts", "<canvas"):
        assert lib not in html.lower(), lib


@needs_node
def test_model_metrics_put_precision_recall_f1_before_roc_auc() -> None:
    metrics = run_js("DA.MODEL_METRICS")
    assert isinstance(metrics, list)
    keys = [key for key, _ in metrics]
    assert keys[:3] == ["precision", "recall", "f1"]
    assert keys.index("roc_auc") > 2
    # Every metric the card returns is shown, in the API's own order.
    assert keys == list(CardMetrics.model_fields)


def test_limitations_are_shown_in_full() -> None:
    _, script = _page()
    # Each limitation is one list item with the API's text, never truncated or picked from.
    assert 'card.limitations.map((text) => el("li", "", text))' in script


@needs_node
@pytest.mark.parametrize(
    ("cr", "text"),
    [
        ({"count": 12, "denominator": 40, "rate": 0.3}, "12 of 40 (30.0%)"),
        ({"count": 0, "denominator": 0, "rate": None}, "0 of 0 (n/a)"),
    ],
)
def test_count_of_never_shows_a_rate_alone(cr: dict[str, object], text: str) -> None:
    assert run_js(f"DA.countOf({json.dumps(cr)})") == text


@needs_node
def test_audit_rows_cover_every_summary_measure() -> None:
    zero = {"count": 0, "denominator": 0, "rate": None}
    counts = {
        "total": 0,
        "reviewed": zero,
        "unreviewed": zero,
        "verdicts": dict.fromkeys(get_args(ReviewVerdict), zero),
        "escalated": zero,
        "unanswered": zero,
        "blocked_by_guard": zero,
    }
    rows = run_js(f"DA.auditRows({json.dumps(counts)}).map(([label]) => label)")
    assert isinstance(rows, list)
    # One row per CountRate in AnswerCounts (verdicts expanded).
    expected = len(AnswerCounts.model_fields) - 2 + len(get_args(ReviewVerdict))
    assert len(rows) == expected
    # Every per-KB-version column picks a {count, denominator, rate} out of the counts.
    picked = run_js(f"DA.AUDIT_KB_COLUMNS.map(([, pick]) => pick({json.dumps(counts)}))")
    assert isinstance(picked, list) and picked
    assert all(p == zero for p in picked)


@needs_node
def test_audit_query_sends_aware_timestamps_and_includes_the_last_day() -> None:
    assert run_js("DA.auditQuery(null, null)") == "/api/practitioner/audit/summary"
    # India time: 1 Nov 00:00 IST is 31 Oct 18:30 UTC; "to 17 Nov" ends at 18 Nov 00:00 IST.
    assert run_js('DA.auditQuery("2026-11-01", "2026-11-17")') == (
        "/api/practitioner/audit/summary?since=2026-10-31T18%3A30%3A00.000Z"
        "&until=2026-11-17T18%3A30%3A00.000Z"
    )
    # The exclusive end shown back as the last included day.
    assert run_js('DA.localDay("2026-11-17T18:30:00Z", 1)') == "2026-11-17"


@needs_node
@pytest.mark.parametrize(
    ("kb", "line"),
    [
        (
            {"approved": 16, "drafts": 0, "seed": 16, "priority": 0},
            "16 of 16 approved entries were approved by the seed account, not yet reviewed "
            "by a practitioner. 0 drafts awaiting approval.",
        ),
        (
            {"approved": 5, "drafts": 1, "seed": 0, "priority": 1},
            "All 5 approved entries were approved by a practitioner other than the seed "
            "account. 1 draft awaiting approval (1 from answers marked unsafe).",
        ),
        (
            {"approved": 0, "drafts": 2, "seed": 0, "priority": 0},
            "No approved entries yet. 2 drafts awaiting approval.",
        ),
    ],
)
def test_kb_status_line(kb: dict[str, int], line: str) -> None:
    payload = {
        "approved": kb["approved"],
        "drafts": kb["drafts"],
        "priority_drafts": {"count": kb["priority"]},
        "approved_by_seed_account": {"count": kb["seed"]},
    }
    assert run_js(f"DA.kbStatusLine({json.dumps(payload)})") == line


@needs_node
def test_retrain_line() -> None:
    assert run_js("DA.retrainLine(null)") == (
        "Last retrain: none yet. Retraining is manual (python -m app.jobs.retrain)."
    )
    run = {
        "status": "succeeded",
        "decision": "kept",
        "error": None,
        "report": {"new": {"version": "risk-2", "precision": 0.25, "recall": 0.8}},
    }
    assert run_js(f"DA.retrainLine({json.dumps(run)})") == (
        "Last retrain: current model kept; candidate risk-2: precision 0.250, recall 0.800."
    )
    failed = {"status": "failed", "decision": "kept", "error": "RuntimeError", "report": None}
    assert run_js(f"DA.retrainLine({json.dumps(failed)})") == (
        "Last retrain: current model kept, training failed (RuntimeError)."
    )
    # A label for every decision the job records, and nothing else.
    assert run_js("Object.keys(DA.RETRAIN_DECISIONS)") == list(get_args(RetrainDecision))
