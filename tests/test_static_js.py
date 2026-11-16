"""Tests for the pure helpers in the web app's ``common.js`` (run with Node).

Day separators in both chat views (patient ``/app`` and practitioner page): "Today",
"Yesterday", "16 Nov", grouped by *local* calendar day. The practitioner page's Review
tab (R5): verdict labels, the note rule, list URLs and card tags, kept in step with the
API. The Knowledge base tab (R5): the approval rule, draft origin lines, field limits
and the staff-only hint staying off patient pages. Node tests are skipped where Node is
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
from app.db.audit_repository import NOTE_REQUIRED, ReviewFilter
from app.db.models import ReviewVerdict
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
