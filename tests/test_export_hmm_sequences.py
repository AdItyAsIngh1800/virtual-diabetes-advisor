"""Tests for scripts/export_hmm_sequences.py (synthetic sequences for the PBL 2 notebook).

No database: the export uses the pure seed generator and threshold engine. The last
test guards the committed CSV, so the notebook never runs on stale data.
"""

from __future__ import annotations

import csv
from collections import Counter
from decimal import Decimal
from pathlib import Path

from scripts.export_hmm_sequences import OUT, SequenceRow, build_rows, export, patient_sequence
from scripts.seed_demo import DAYS, DEMO_PATIENTS, PatientSpec


def _spec(key: str) -> PatientSpec:
    return next(s for s in DEMO_PATIENTS if s.key == key)


def test_one_fasting_row_per_patient_and_day() -> None:
    rows = build_rows()
    assert len(rows) == len(DEMO_PATIENTS) * DAYS
    per_patient = Counter(r.patient_key for r in rows)
    assert set(per_patient) == {s.key for s in DEMO_PATIENTS}
    assert set(per_patient.values()) == {DAYS}
    for spec in DEMO_PATIENTS:
        days = [r.day for r in rows if r.patient_key == spec.key]
        assert days == list(range(DAYS))
    assert {r.context for r in rows} == {"fasting"}


def test_export_is_deterministic() -> None:
    assert build_rows() == build_rows()


def test_band_states_match_the_limits_in_the_row() -> None:
    # Re-derive each state from the exported value and limits (5 % band).
    band = Decimal("0.05")
    for row in build_rows():
        value, high, low = row.value_mg_dl, row.high_limit_mg_dl, row.low_limit_mg_dl
        if value <= low:
            expected = "breach_low"
        elif value >= high:
            expected = "breach_high"
        elif value <= low * (1 + band):
            expected = "pre_breach_low"
        elif value >= high * (1 - band):
            expected = "pre_breach_high"
        else:
            expected = "normal"
        assert row.band_state == expected, row


def test_scenarios_show_up_in_the_states() -> None:
    def states(key: str) -> Counter[str]:
        return Counter(r.band_state for r in patient_sequence(_spec(key)))

    for calm in ("aarav", "rahul", "ananya", "fatima"):
        assert states(calm) == {"normal": DAYS}, calm
    assert states("rohan")["breach_high"] > DAYS // 2
    assert states("meera")["pre_breach_high"] > 0
    assert states("kavya")["pre_breach_low"] > 0
    # All five band states occur somewhere, so all three notebook symbols do.
    assert {r.band_state for r in build_rows()} == {
        "normal",
        "pre_breach_high",
        "pre_breach_low",
        "breach_high",
        "breach_low",
    }


def test_export_writes_a_csv_that_round_trips(tmp_path: Path) -> None:
    out = export(tmp_path / "nested" / "seq.csv")
    with out.open(encoding="utf-8", newline="") as handle:
        parsed = [SequenceRow.model_validate(r) for r in csv.DictReader(handle)]
    assert parsed == build_rows()


def test_committed_csv_is_up_to_date() -> None:
    # The notebook reads the committed file; it must equal a fresh export.
    with OUT.open(encoding="utf-8", newline="") as handle:
        committed = [SequenceRow.model_validate(r) for r in csv.DictReader(handle)]
    assert committed == build_rows(), "re-run: uv run python -m scripts.export_hmm_sequences"
