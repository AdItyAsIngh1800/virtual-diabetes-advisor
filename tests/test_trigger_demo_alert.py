"""Tests for scripts/trigger_demo_alert.py (no database: pure helpers and fakes)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.core.clinical_limits import GlucoseLimitKind
from app.core.exceptions import BatchAlreadyRunningError
from app.services.thresholds import ReadingPoint, classify_reading
from scripts import trigger_demo_alert as script
from scripts.trigger_demo_alert import DemoAlertError, reading_value_for

LIMITS: dict[GlucoseLimitKind, Decimal] = {
    "fasting_high": Decimal("130.0"),
    "low": Decimal("70.0"),
}
EXPECTED = {
    "pre_breach_high": ("pre_breach_high", False),
    "breach_high": ("breach_high", False),
    "below_54": ("breach_low", True),
}


@pytest.mark.parametrize("level", script.LEVELS)
@pytest.mark.parametrize("upper", ["130.0", "126.3", "140.0", "117.0"])
def test_each_level_classifies_as_requested(level: script.Level, upper: str) -> None:
    limits: dict[GlucoseLimitKind, Decimal] = {**LIMITS, "fasting_high": Decimal(upper)}
    value = reading_value_for(level, limits, 0.05)
    result = classify_reading(
        ReadingPoint(value_mg_dl=value, context="fasting", measured_at=datetime.now(UTC)),
        limits,
        band=0.05,
    )
    assert (result.state, result.escalate) == EXPECTED[level]


def test_values_for_meeras_default_limit() -> None:
    assert reading_value_for("pre_breach_high", LIMITS, 0.05) == Decimal("126.8")
    assert reading_value_for("breach_high", LIMITS, 0.05) == Decimal("140.0")
    assert reading_value_for("below_54", {}, 0.05) == Decimal("50.0")


def test_high_levels_need_a_fasting_limit() -> None:
    with pytest.raises(DemoAlertError, match="batch_update"):
        reading_value_for("pre_breach_high", {"low": Decimal("70")}, 0.05)


def test_batch_is_retried_while_another_run_holds_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []
    waits: list[float] = []

    def busy_twice(*_a: Any, **_k: Any) -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise BatchAlreadyRunningError("busy")
        return "summary"

    monkeypatch.setattr(script, "run_batch_update", busy_twice)
    monkeypatch.setattr(script, "load_risk_scorer", lambda: None)
    assert script.run_batch_with_retry(lambda: None, sleep=waits.append) == "summary"  # type: ignore[arg-type,return-value,comparison-overlap]
    assert len(waits) == 2

    def always_busy(*_a: Any, **_k: Any) -> None:
        raise BatchAlreadyRunningError("busy")

    monkeypatch.setattr(script, "run_batch_update", always_busy)
    with pytest.raises(DemoAlertError, match="kept running"):
        script.run_batch_with_retry(lambda: None, sleep=lambda _s: None)  # type: ignore[arg-type,return-value]


def test_main_reports_setup_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_patient(*_a: Any, **_k: Any) -> int:
        raise DemoAlertError("no demo patient is linked")

    monkeypatch.setattr(script, "trigger", no_patient)
    monkeypatch.setattr("app.db.session.get_session_factory", lambda: None)
    assert script.main(["--level", "below_54"]) == 1
    with pytest.raises(SystemExit):
        script.main(["--level", "nonsense"])
