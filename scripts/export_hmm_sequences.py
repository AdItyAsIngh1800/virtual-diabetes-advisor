"""Export the synthetic demo cohort's daily fasting readings for the PBL 2 HMM notebook.

``notebooks/02_pbl2_unsupervised_and_hmm.ipynb`` (Part B) fits a hidden Markov model to
one glucose sequence per demo patient. It runs on Colab, where the repo's database is
not available, so this script writes the sequences to a small committed CSV instead:
``data/synthetic/demo_glucose_sequences.csv``.

The values come from the same pure code the demo seed uses (``scripts.seed_demo``) and
are classified by the real threshold engine, exactly as ``tests/test_seed_demo.py``
simulates one batch run: no database, no risk model, fixed ``NOW`` and seed, so every
run writes the same file. The data is **synthetic**; the notebook only demonstrates a
method on it and the app never reads this file.

Run with ``uv run python -m scripts.export_hmm_sequences``.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from app.core.clinical_limits import DiabetesStatus, GlucoseLimitKind
from app.ml.predict import RiskAssessment
from app.services.thresholds import (
    ALL_LIMIT_KINDS,
    TREND_CONTEXTS,
    BandState,
    ReadingPoint,
    ThresholdInputs,
    analyse_trend,
    classify_reading,
    plan_threshold_update,
)
from scripts.seed_demo import DAYS, DEMO_PATIENTS, DEMO_TZ, PatientSpec, build_risk_profile
from scripts.seed_demo import generate_readings as generate_demo_readings

logger = logging.getLogger(__name__)

OUT: Final = (
    Path(__file__).resolve().parents[1] / "data" / "synthetic" / "demo_glucose_sequences.csv"
)
# Fixed clock (same as tests/test_seed_demo.py): 23:30 IST, so all 30 days are complete
# and the export never changes with the day it is run.
NOW: Final = datetime(2026, 11, 15, 18, 0, tzinfo=UTC)
# Pre-breach band fraction, passed explicitly so a local .env cannot change the export
# (the engine's default PRE_BREACH_BAND, threshold-engine skill: 5 % of the limit).
BAND: Final = 0.05
# Placeholder model output: the risk tier does not move any glucose limit, so a fixed
# assessment at the spec's target tier gives the same limits as the trained model would.
_PLACEHOLDER_PROBABILITY: Final = 0.5
_PLACEHOLDER_MODEL_VERSION: Final = "export-hmm-sequences"


class SequenceRow(BaseModel):
    """One day of one synthetic patient's fasting sequence (one CSV row).

    Attributes:
        patient_key: Demo patient key (``DEMO_PATIENTS[i].key``).
        scenario: What the generator was told to produce for this patient.
        diabetes_status: Limit set the threshold engine used.
        day: Day index, 0 (oldest) to ``DAYS - 1`` (today).
        context: Always ``fasting`` (one reading per day).
        value_mg_dl: Synthetic glucose value.
        high_limit_mg_dl: The patient's ``fasting_high`` threshold.
        low_limit_mg_dl: The patient's ``low`` threshold.
        band_state: ``classify_reading`` result for this value.
    """

    model_config = ConfigDict(frozen=True)

    patient_key: str
    scenario: str
    diabetes_status: DiabetesStatus
    day: int = Field(ge=0, lt=DAYS)
    context: str
    value_mg_dl: Decimal = Field(gt=0)
    high_limit_mg_dl: Decimal = Field(gt=0)
    low_limit_mg_dl: Decimal = Field(gt=0)
    band_state: BandState


def patient_sequence(spec: PatientSpec, *, now: datetime = NOW) -> list[SequenceRow]:
    """Build one patient's daily fasting sequence with its band states.

    Thresholds are computed once, at ``now``, from all 30 days of readings (one batch
    run, as in ``tests/test_seed_demo.py``); every fasting reading is then classified
    against them.

    Args:
        spec: The demo patient.
        now: Clock for generation and trend analysis (timezone-aware).

    Returns:
        One row per day that has a fasting reading, oldest first.
    """
    profile, _ = build_risk_profile(spec, None)
    inputs = ThresholdInputs(
        diabetes_status=spec.diabetes_status,
        age_years=spec.age,
        sex=profile.sex,
        high_bp=profile.high_bp,
        high_chol=profile.high_chol,
        heart_disease_or_attack=profile.heart_disease_or_attack,
        stroke=profile.stroke,
        diff_walk=profile.diff_walk,
    )
    risk = RiskAssessment(
        probability=_PLACEHOLDER_PROBABILITY,
        tier=spec.target_tier,
        flagged=True,
        model_version=_PLACEHOLDER_MODEL_VERSION,
    )
    points = [
        ReadingPoint(value_mg_dl=r.value_mg_dl, context=r.context, measured_at=r.measured_at)
        for r in generate_demo_readings(spec, now=now)
    ]
    thresholds: dict[GlucoseLimitKind, Decimal] = {
        kind: plan_threshold_update(
            limit_kind=kind,
            inputs=inputs,
            risk=risk,
            trend=analyse_trend([p for p in points if p.context in TREND_CONTEXTS[kind]], now),
            previous=None,
        ).threshold.threshold_mg_dl
        for kind in ALL_LIMIT_KINDS
    }
    today = now.astimezone(DEMO_TZ).date()
    rows: list[SequenceRow] = []
    for point in points:
        if point.context != "fasting":
            continue
        day = DAYS - 1 - (today - point.measured_at.astimezone(DEMO_TZ).date()).days
        rows.append(
            SequenceRow(
                patient_key=spec.key,
                scenario=spec.scenario,
                diabetes_status=spec.diabetes_status,
                day=day,
                context=point.context,
                value_mg_dl=point.value_mg_dl,
                high_limit_mg_dl=thresholds["fasting_high"],
                low_limit_mg_dl=thresholds["low"],
                band_state=classify_reading(point, thresholds, band=BAND).state,
            )
        )
    return rows


def build_rows(
    patients: Sequence[PatientSpec] = DEMO_PATIENTS, *, now: datetime = NOW
) -> list[SequenceRow]:
    """Build the sequences of every demo patient.

    Args:
        patients: Patients to export (default: the whole demo cohort).
        now: Clock for generation and trend analysis.

    Returns:
        Rows grouped by patient (cohort order), each patient's days in order.
    """
    return [row for spec in patients for row in patient_sequence(spec, now=now)]


def export(out: Path = OUT, *, now: datetime = NOW) -> Path:
    """Write every demo patient's fasting sequence to a CSV file.

    Args:
        out: Destination file; its folder is created if missing.
        now: Clock for generation and trend analysis.

    Returns:
        The path written.
    """
    rows = build_rows(now=now)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SequenceRow.model_fields))
        writer.writeheader()
        writer.writerows(row.model_dump(mode="json") for row in rows)
    logger.info("wrote %d rows for %d patients to %s", len(rows), len(DEMO_PATIENTS), out)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    export()
