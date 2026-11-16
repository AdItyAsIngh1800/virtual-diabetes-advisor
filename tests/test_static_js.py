"""Tests for the pure helpers in the web app's ``common.js`` (run with Node).

Day separators in both chat views (patient ``/app`` and practitioner page): "Today",
"Yesterday", "16 Nov", grouped by *local* calendar day. Skipped where Node is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

COMMON_JS: Final = Path(__file__).resolve().parents[1] / "app" / "static" / "patient" / "common.js"
NODE: Final = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

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


def test_with_days_on_an_empty_conversation() -> None:
    assert run_js(f"DA.withDays([], {NOW_JS})") == []
