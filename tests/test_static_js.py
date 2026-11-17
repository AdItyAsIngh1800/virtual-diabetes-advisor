"""Tests for the pure helpers in the web app's ``common.js`` (run with Node).

Day separators in both chat views (patient ``/app`` and practitioner page): "Today",
"Yesterday", "16 Nov", grouped by *local* calendar day. The practitioner page's Review
tab (R5): verdict labels, the note rule, list URLs and card tags, kept in step with the
API. The Knowledge base tab (R5): the approval rule, draft origin lines, field limits
and the staff-only hint staying off patient pages. The Audit tab (R5): counts always
shown with their rates, the KB status line, the model card's metric order and the
limitations shown in full. The dashboard tabs (R6): tab order and default, the
coordinate maths of the glucose chart, the pre-breach band maths against the Python
band check, labels that never rely on colour alone, and no e-mail, phone number or
push endpoint ever rendered. Node tests are skipped where Node is missing; the static
checks always run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, get_args

import pytest

from app.api.kb_admin import MAX_BODY_CHARS, MAX_CITATION_CHARS, MAX_TITLE_CHARS
from app.core.clinical_limits import GlucoseLimitKind
from app.db.audit_repository import NOTE_REQUIRED, AnswerCounts, ReviewFilter
from app.db.models import AdvisoryState, DeliveryStatus, RetrainDecision, ReviewVerdict
from app.ml.model_card import CardMetrics
from app.services.kb_upgrade import PRIORITY_LABEL
from app.services.thresholds import BandState, ReadingPoint, classify_reading

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


# --- Dashboard tabs (R6) ----------------------------------------------------------------


def js(expression: str) -> Any:
    """Like :func:`run_js`, typed loosely: JSON from Node, its shape checked by the test.

    Args:
        expression: JavaScript expression.

    Returns:
        Its JSON-decoded value.
    """
    return run_js(expression)


DASHBOARD_TABS: Final = [
    ("tab-overview", "overview"),
    ("tab-patients", "patients"),
    ("tab-alerts", "alerts"),
    ("tab-inbox", "inbox"),
    ("tab-review", "review"),
    ("tab-kb", "kb"),
    ("tab-audit", "audit"),
]


def test_dashboard_tabs_come_first_and_overview_is_the_default() -> None:
    html, script = _page()
    buttons = re.findall(
        r'role="tab" id="(tab-\w+)" aria-controls="(\w+)" aria-selected="(\w+)"', html
    )
    assert [(b, p) for b, p, _ in buttons] == DASHBOARD_TABS
    assert [sel for _, _, sel in buttons] == ["true"] + ["false"] * 6
    assert re.findall(r'\["(tab-\w+)", "(\w+)"\]', script) == DASHBOARD_TABS
    assert 'let activeTab = "overview";' in script
    ids = set(re.findall(r'\bid="([\w-]+)"', html))
    used = set(re.findall(r'\$\("([\w-]+)"\)', script))
    for panel in ("overview", "patients", "alerts"):
        assert f'role="tabpanel" aria-labelledby="tab-{panel}"' in html
    dashboard_ids = {
        "ov-tiles",
        "ov-batch",
        "ov-delivery",
        "patient-rows",
        "patients-empty",
        "pf-band",
        "pf-breach",
        "pf-handoff",
        "pf-sort",
        "patient-detail",
        "pd-chart",
        "pd-legend",
        "pd-readings",
        "pd-why-body",
        "pd-why-notes",
        "pd-risk",
        "pd-advisories",
        "pd-days",
        "alert-rows",
        "alerts-more",
        "af-state",
        "af-escalated",
        "af-delivery",
        "af-since",
        "af-until",
    }
    assert dashboard_ids <= used <= ids
    for route in (
        "/api/dashboard/overview",
        "/api/dashboard/patients/",
        "D.patientsQuery(",
        "D.alertsQuery(",
    ):
        assert route in script, route


def test_dashboard_draws_its_chart_without_a_library() -> None:
    html, script = _page()
    assert "createElementNS(SVG, tag)" in script and "http://www.w3.org/2000/svg" in script
    for lib in ("chart.js", "chartjs", "d3", "plotly", "recharts", "<canvas", "vega"):
        assert lib not in html.lower(), lib
    assert html.count("<script src=") == 3  # supabase-js, common.js, practitioner.js


def test_risk_tier_carries_the_safety_label_and_model_card_link() -> None:
    html, script = _page()
    common = COMMON_JS.read_text(encoding="utf-8")
    label = "Survey-based diabetes risk (information only, does not change limits)"
    assert f'const RISK_LABEL = "{label}";' in common
    # Every place a risk tier is shown gets the label and a link to the model card.
    assert html.count("data-risk-label") == 2 and html.count("data-model-card") == 2
    assert 'id="model-card-panel"' in html
    assert 'showTab("audit")' in script and '$("model-card-panel")' in script


def test_filters_offer_exactly_the_api_values() -> None:
    html, _ = _page()

    def options(select_id: str) -> list[str]:
        block = html.split(f'id="{select_id}"', 1)[1].split("</select>", 1)[0]
        return [v for v in re.findall(r'<option value="([\w]*)"', block) if v]

    assert set(options("pf-band")) == set(get_args(BandState))
    assert set(options("af-state")) == set(get_args(AdvisoryState))
    assert set(options("af-delivery")) == set(get_args(DeliveryStatus))
    assert options("pf-breach") == options("af-escalated") == ["true", "false"]


@needs_node
def test_every_state_has_words_not_only_colour() -> None:
    labels = js("DA.BAND_LABELS")
    assert isinstance(labels, dict)
    assert set(labels) == set(get_args(BandState))
    # Above / below carry a direction glyph as well as words.
    for state in ("pre_breach_high", "breach_high"):
        assert labels[state].startswith("▲")
    for state in ("pre_breach_low", "breach_low"):
        assert labels[state].startswith("▼")
    assert js("DA.bandLabel(null)") == "No limit yet"
    assert js("Object.keys(DA.DELIVERY_LABELS)") == list(get_args(DeliveryStatus))


# Pre-breach band maths: JS bandState must agree with the Python band check.

LIMITS: Final = {"low": 70.0, "fasting_high": 130.0, "post_meal_high": 180.0}
BOUNDARY_READINGS: Final = [
    (value, context)
    for context, values in (
        ("fasting", [69.9, 70.0, 70.1, 73.4, 73.5, 73.6, 100.0, 123.4, 123.5, 129.9, 130.0, 130.1]),
        ("post_meal", [70.0, 73.5, 170.9, 171.0, 179.9, 180.0, 250.0]),
        ("random", [54.0, 70.0, 73.5, 73.6, 400.0]),
    )
    for value in values
]


def _python_state(value: float, context: str, band: float) -> str:
    reading = ReadingPoint.model_validate(
        {"value_mg_dl": str(value), "context": context, "measured_at": "2026-11-17T09:00:00Z"}
    )
    limits: dict[GlucoseLimitKind, Decimal] = {
        "low": Decimal("70"),
        "fasting_high": Decimal("130"),
        "post_meal_high": Decimal("180"),
    }
    assert {k: float(v) for k, v in limits.items()} == LIMITS
    return classify_reading(reading, limits, band=band).state


@needs_node
@pytest.mark.parametrize("band", [0.05, 0.1, 0.07])
def test_band_maths_match_the_python_band_check(band: float) -> None:
    expected = [_python_state(v, c, band) for v, c in BOUNDARY_READINGS]
    cases = json.dumps(BOUNDARY_READINGS)
    got = js(f"{cases}.map(([v, c]) => DA.bandState(v, c, {json.dumps(LIMITS)}, {band}))")
    assert got == expected


@needs_node
def test_band_state_is_null_when_a_needed_limit_is_missing() -> None:
    assert js('DA.bandState(100, "fasting", {low: 70}, 0.05)') is None
    assert js('DA.bandState(100, "random", {low: 70}, 0.05)') == "normal"


@needs_node
def test_band_edges() -> None:
    # Upper limit U: [0.95 U, U]; lower limit L: [L, 1.05 L].
    lo, hi = js('DA.bandEdges(130, "upper", 0.05)')
    assert (lo, hi) == (pytest.approx(123.5), 130)
    lo, hi = js('DA.bandEdges(70, "lower", 0.05)')
    assert (lo, hi) == (70, pytest.approx(73.5))


# Chart coordinate maths (pure functions, no DOM).


@needs_node
def test_scale_is_linear_and_flips_for_svg_y() -> None:
    assert js("DA.scale(5, 0, 10, 0, 100)") == 50
    assert js("DA.scale(0, 0, 10, 200, 0)") == 200  # value 0 at the bottom
    assert js("DA.scale(10, 0, 10, 200, 0)") == 0
    assert js("DA.scale(3, 3, 3, 0, 100)") == 50  # empty domain: middle, no NaN


@needs_node
@pytest.mark.parametrize(
    ("lo", "hi", "ticks"),
    [
        (40, 260, [50, 100, 150, 200, 250]),
        (60, 140, [60, 80, 100, 120, 140]),
        (0, 1, [0, 0.2, 0.4, 0.6, 0.8, 1]),
    ],
)
def test_nice_ticks(lo: float, hi: float, ticks: list[float]) -> None:
    assert js(f"DA.niceTicks({lo}, {hi}, 5)") == ticks


@needs_node
def test_y_domain_keeps_readings_limits_and_bands_in_view() -> None:
    lo, hi = js("DA.yDomain([50, 128], [70, 130, 180], 0.05)")
    assert lo <= 50 - 10 and hi >= 180 + 10
    assert lo % 10 == 0 and hi % 10 == 0 and lo > 0


DETAIL: Final = {
    "window": {"since": "2026-11-01T00:00:00Z", "until": "2026-11-11T00:00:00Z"},
    "pre_breach_band": 0.05,
    "readings": [
        {
            "id": "r1",
            "measured_at": "2026-11-02T00:00:00Z",
            "value_mg_dl": 135,
            "context": "fasting",
            "band_state": "breach_high",
            "limit_kind": "fasting_high",
            "before_first_limit": True,
        },
        {
            "id": "r2",
            "measured_at": "2026-11-06T00:00:00Z",
            "value_mg_dl": 100,
            "context": "post_meal",
            "band_state": "normal",
            "limit_kind": None,
            "before_first_limit": False,
        },
    ],
    "thresholds": [
        {
            "limit_kind": "fasting_high",
            "effective_from": "2026-11-03T00:00:00Z",
            "value_mg_dl": 130,
        },
        {
            "limit_kind": "fasting_high",
            "effective_from": "2026-11-08T00:00:00Z",
            "value_mg_dl": 120,
        },
        {"limit_kind": "low", "effective_from": "2026-10-01T00:00:00Z", "value_mg_dl": 70},
    ],
}


def _model(width: int = 1000, height: int = 300) -> Any:
    margin = "{top: 0, right: 0, bottom: 0, left: 0}"
    out = js(f"DA.chartModel({json.dumps(DETAIL)}, {width}, {height}, {margin})")
    assert isinstance(out, dict)
    return out


@needs_node
def test_step_segments_cover_the_window_and_dash_before_the_first_limit() -> None:
    day = 86_400_000
    t0 = js('Date.parse("2026-11-01T00:00:00Z")')
    assert isinstance(t0, int)
    steps = json.dumps(DETAIL["thresholds"])
    segments = js(f'DA.stepSegments({steps}, "fasting_high", {t0}, {t0 + 10 * day})')
    assert segments == [
        {"from": t0, "to": t0 + 2 * day, "value": 130, "beforeFirst": True},
        {"from": t0 + 2 * day, "to": t0 + 7 * day, "value": 130, "beforeFirst": False},
        {"from": t0 + 7 * day, "to": t0 + 10 * day, "value": 120, "beforeFirst": False},
    ]
    # A limit older than the window starts at its left edge, not before.
    low = js(f'DA.stepSegments({json.dumps(DETAIL["thresholds"])}, "low", {t0}, {t0 + day})')
    assert low == [{"from": t0, "to": t0 + day, "value": 70, "beforeFirst": False}]
    assert js(f'DA.stepSegments([], "low", {t0}, {t0 + day})') == []


@needs_node
def test_chart_model_coordinates() -> None:
    m = _model()
    v0, v1 = m["domain"]
    plot = m["plot"]
    assert plot == {"x0": 0, "x1": 1000, "y0": 0, "y1": 300}

    def y(v: float) -> float:
        return float(300 - (v - v0) / (v1 - v0) * 300)

    limits = {lim["kind"]: lim for lim in m["limits"]}
    assert set(limits) == {"fasting_high", "low"}  # post-meal has no limit: not drawn
    fasting = limits["fasting_high"]["segments"]
    # x: 10 days across 1000 px, so a day is 100 px.
    assert [(round(s["x0"]), round(s["x1"])) for s in fasting] == [
        (0, 200),
        (200, 700),
        (700, 1000),
    ]
    assert [s["beforeFirst"] for s in fasting] == [True, False, False]
    for s in fasting:
        assert s["y"] == pytest.approx(y(s["value"]))
        # The band of an upper limit is shaded just under the line: [0.95 U, U].
        assert s["bandTop"] == pytest.approx(y(s["value"]))
        assert s["bandBottom"] == pytest.approx(y(s["value"] * 0.95))
    low = limits["low"]["segments"][0]
    assert low["bandBottom"] == pytest.approx(y(70)) and low["bandTop"] == pytest.approx(y(73.5))

    p1, p2 = m["points"]
    assert (round(p1["x"]), round(p2["x"])) == (100, 500)
    assert p1["y"] == pytest.approx(y(135)) and p1["breach"] and p1["beforeFirst"]
    assert not p2["breach"] and p2["context"] == "post_meal"
    # Higher glucose is higher on screen.
    assert p1["y"] < p2["y"]
    for t in m["yTicks"]:
        assert v0 <= t["v"] <= v1 and t["y"] == pytest.approx(y(t["v"]))
    assert m["beforeFirst"] is True


@needs_node
def test_chart_scales_with_its_width() -> None:
    narrow, wide = _model(320, 260), _model(1280, 260)
    for a, b in zip(narrow["points"], wide["points"], strict=True):
        assert b["x"] == pytest.approx(a["x"] * 4)
        assert a["y"] == pytest.approx(b["y"])


@needs_node
def test_nearest_point() -> None:
    pts = json.dumps([{"x": 10, "y": 10}, {"x": 100, "y": 100}])
    assert js(f"DA.nearestPoint({pts}, 12, 9)") == 0
    assert js(f"DA.nearestPoint({pts}, 90, 95)") == 1
    assert js(f"DA.nearestPoint({pts}, 55, 55)") == -1  # nothing within reach


@needs_node
def test_why_limits_shows_every_component() -> None:
    step = {
        "limit_kind": "fasting_high",
        "effective_from": "2026-11-08T00:00:00Z",
        "base_mg_dl": 130,
        "demo_adj_mg_dl": 10,
        "risk_adj_mg_dl": 0,
        "trend_adj_mg_dl": 45,
        "unclamped_mg_dl": 185,
        "hard_min_mg_dl": 100,
        "hard_max_mg_dl": 180,
        "value_mg_dl": 180,
        "reason": "trend_revision",
        "source_citation": "ADA 2026 Table 6.3",
    }
    older = {**step, "effective_from": "2026-11-01T00:00:00Z", "value_mg_dl": 130}
    rows = js(f"DA.whyLimits({json.dumps([older, step])})")
    assert rows == [
        {
            "kind": "fasting_high",
            "label": "Fasting",
            "base": 130,
            "demo": 10,
            "risk": 0,
            "trend": 45,
            "sum": 185,
            "min": 100,
            "max": 180,
            "value": 180,
            "clamped": True,
            "citation": "ADA 2026 Table 6.3",
            "reason": "Trend revision",
            "since": "2026-11-08T00:00:00Z",
        }
    ]
    assert js("[DA.signed(5), DA.signed(-3.5), DA.signed(0)]") == ["+5", "-3.5", "0"]


# Privacy: nothing but a first name ever reaches the page.

LEAKY_PATIENT: Final = {
    "user_id": "6b1f0c3e-0000-4000-8000-000000000001",
    "first_name": "Asha",
    "full_name": "Asha Rao Kumar",
    "email": "asha.kumar@example.com",
    "phone_e164": "+919812345678",
    "endpoint": "https://push.example/secret",
    "age_band": "50-59",
    "diabetes_status": "diabetes",
    "risk": {
        "tier": "high",
        "score": 0.4,
        "flagged": True,
        "model_version": "risk-1",
        "assessed_at": "2026-11-17T09:00:00Z",
    },
    "limits": {"fasting_high": 130, "post_meal_high": 180, "low": 70},
    "last_reading": {
        "value_mg_dl": 123.5,
        "context": "fasting",
        "measured_at": "2026-11-17T09:00:00Z",
        "band_state": "pre_breach_high",
    },
    "alerts_7d": 2,
    "breach_7d": True,
    "live_handoff": "open",
}
LEAKY_ALERT: Final = {
    "id": "a1",
    "user_id": LEAKY_PATIENT["user_id"],
    "first_name": "Asha",
    "email": "asha.kumar@example.com",
    "phone_e164": "+919812345678",
    "measured_at": "2026-11-17T09:00:00Z",
    "created_at": "2026-11-17T09:05:00Z",
    "limit_kind": "fasting_high",
    "state": "pre_breach_high",
    "reading_value_mg_dl": 123.5,
    "threshold_mg_dl": 130,
    "escalated": False,
    "delivery_status": "opened",
}
SECRETS: Final = ("asha.kumar", "@", "+91", "Kumar", "push.example", "6b1f0c3e")


@needs_node
def test_no_email_or_phone_is_ever_rendered() -> None:
    cells = js(
        f"[DA.patientCells({json.dumps(LEAKY_PATIENT)}), DA.alertCells({json.dumps(LEAKY_ALERT)})]"
    )
    rendered = json.dumps(cells, ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in rendered, secret
    patient, alert = cells
    assert patient["name"] == alert["name"] == "Asha"
    assert patient["state"] == "▲ Near high limit" and patient["limits"] == "130 / 180 / 70"
    assert alert["reading"] == "123.5 vs 130"


def test_dashboard_code_never_reads_contact_fields() -> None:
    _, script = _page()
    dashboard = script.split("// --- Dashboard (R6)", 1)[1].split("// --- Page ---", 1)[0]
    common = COMMON_JS.read_text(encoding="utf-8").split("// --- Dashboard (R6", 1)[1]
    for code in (dashboard, common):
        for field in ("email", "phone", "endpoint", "full_name", "p256dh"):
            assert re.search(rf"\.{field}\b|\[\"{field}\"\]", code) is None, field


@needs_node
def test_sort_patients_keeps_missing_values_last() -> None:
    rows = [
        {
            "first_name": "Ravi",
            "age_band": None,
            "last_reading": None,
            "alerts_7d": 0,
            "risk": None,
        },
        {
            "first_name": "Asha",
            "age_band": "50-59",
            "alerts_7d": 3,
            "risk": {"tier": "low"},
            "last_reading": {
                "band_state": "breach_low",
                "measured_at": "2026-11-17T09:00:00Z",
                "value_mg_dl": 50,
            },
        },
        {
            "first_name": "Bina",
            "age_band": "20-29",
            "alerts_7d": 1,
            "risk": {"tier": "high"},
            "last_reading": {
                "band_state": "normal",
                "measured_at": "2026-11-17T10:00:00Z",
                "value_mg_dl": 110,
            },
        },
    ]
    data = json.dumps(rows)

    def order(key: str, descending: bool = False) -> list[str]:
        args = f"{data}, {json.dumps(key)}, {json.dumps(descending)}"
        out = js(f"DA.sortPatients({args}).map((r) => r.first_name)")
        assert isinstance(out, list)
        return out

    assert order("name") == ["Asha", "Bina", "Ravi"]
    assert order("name", True) == ["Ravi", "Bina", "Asha"]
    assert order("state") == ["Asha", "Bina", "Ravi"]  # most urgent first, no reading last
    assert order("state", True) == ["Bina", "Asha", "Ravi"]
    assert order("alerts") == ["Asha", "Bina", "Ravi"]
    assert order("risk") == ["Bina", "Asha", "Ravi"]
    assert order("last") == ["Bina", "Asha", "Ravi"]


@needs_node
def test_dashboard_urls() -> None:
    assert js('DA.patientsQuery({band_state: "", breach_7d: "true", live_handoff: ""})') == (
        "/api/dashboard/patients?limit=100&breach_7d=true"
    )
    url = js(
        'DA.alertsQuery({state: "breach_low", since: "2026-11-16", until: "2026-11-16"}, "abc=")'
    )
    # India time: the 16th runs from 18:30 UTC on the 15th to 18:30 UTC on the 16th.
    assert url == (
        "/api/dashboard/alerts?limit=25&cursor=abc%3D&state=breach_low"
        "&since=2026-11-15T18%3A30%3A00.000Z&until=2026-11-16T18%3A30%3A00.000Z"
    )


@needs_node
def test_overview_tiles_show_rates_with_counts() -> None:
    zero = {"count": 0, "denominator": 0, "rate": None}
    ov: dict[str, Any] = {
        "active_patients": 20,
        "readings": 40,
        "pre_breach_alerts": 6,
        "breaches": 4,
        "below_54_readings": 0,
        "open_handoffs": 0,
        "claimed_handoffs": 1,
        "delivery": {
            "delivered": {"count": 3, "denominator": 10, "rate": 0.3},
            **dict.fromkeys(["queued", "sent", "opened", "failed", "expired", "suppressed"], zero),
        },
    }
    tiles = js(f"DA.overviewTiles({json.dumps(ov)})")
    assert isinstance(tiles, list)
    delivery = next(t for t in tiles if t["key"] == "delivery")
    assert (delivery["value"], delivery["detail"]) == ("30%", "3 of 10 sent or opened")
    breach = next(t for t in tiles if t["key"] == "breach")
    assert breach["urgent"] is True
    ov["delivery"]["delivered"] = zero
    tiles = js(f"DA.overviewTiles({json.dumps(ov)})")
    assert isinstance(tiles, list)
    empty = next(t for t in tiles if t["key"] == "delivery")
    assert (empty["value"], empty["detail"]) == ("n/a", "0 of 0 sent or opened")
    assert js("DA.batchLine(null)") == "Last batch run: never."
