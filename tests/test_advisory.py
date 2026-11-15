"""Tests for advisory planning: idempotency, cooldown boundaries and message templates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.advisory import (
    AdvisoryDraft,
    AdvisoryPlan,
    EvaluatedReading,
    PriorAdvisory,
    advisory_language,
    plan_advisories,
    render_message,
)
from app.services.thresholds import BandResult

T0 = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
COOLDOWN = timedelta(hours=24)
PRE_HIGH = BandResult("pre_breach_high", "fasting_high", Decimal("130.0"), escalate=False)
BREACH_HIGH = BandResult("breach_high", "fasting_high", Decimal("130.0"), escalate=False)
PRE_LOW = BandResult("pre_breach_low", "low", Decimal("70.0"), escalate=False)
NORMAL = BandResult("normal", None, None, escalate=False)


def ev(
    hours: float, band: BandResult = PRE_HIGH, value: str = "125.0", context: str = "fasting"
) -> EvaluatedReading:
    return EvaluatedReading(
        reading_id=uuid.uuid4(),
        measured_at=T0 + timedelta(hours=hours),
        context=context,  # type: ignore[arg-type]
        value_mg_dl=Decimal(value),
        band=band,
    )


def as_prior(draft: AdvisoryDraft) -> PriorAdvisory:
    return PriorAdvisory(
        reading_id=draft.reading_id,
        limit_kind=draft.limit_kind,
        state=draft.state,
        reading_measured_at=draft.measured_at,
    )


def plan(
    evaluated: list[EvaluatedReading], prior: list[PriorAdvisory] | None = None
) -> AdvisoryPlan:
    return plan_advisories(evaluated, prior or [], cooldown=COOLDOWN, preferred_language="en")


def test_normal_readings_create_nothing() -> None:
    assert plan([ev(0, NORMAL), ev(30, NORMAL)]).drafts == []


# --- idempotency (R1) ------------------------------------------------------------


def test_replanning_with_own_output_creates_no_duplicates() -> None:
    readings = [ev(h) for h in (0, 30, 60)] + [ev(10, PRE_LOW, "72.0")]
    first = plan(readings)
    assert len(first.drafts) == 4
    second = plan(readings, [as_prior(d) for d in first.drafts])
    assert second.drafts == []
    assert second.duplicates == 4


def test_existing_advisory_for_other_limit_does_not_block_reading() -> None:
    reading = ev(0, PRE_HIGH)
    prior = PriorAdvisory(
        reading_id=reading.reading_id,
        limit_kind="low",
        state="pre_breach_low",
        reading_measured_at=reading.measured_at,
    )
    assert len(plan([reading], [prior]).drafts) == 1


def test_outcome_does_not_depend_on_input_order() -> None:
    readings = [ev(h) for h in (0, 5, 23, 24, 30, 47, 49)]
    forward = plan(readings)
    backward = plan(list(reversed(readings)))
    assert [d.reading_id for d in forward.drafts] == [d.reading_id for d in backward.drafts]


# --- cooldown (R3) -----------------------------------------------------------------


def test_cooldown_suppresses_same_state_within_window() -> None:
    result = plan([ev(0), ev(6), ev(23.99)])
    assert len(result.drafts) == 1
    assert result.suppressed == 2


def test_cooldown_boundary_exactly_n_hours_apart_is_allowed() -> None:
    result = plan([ev(0), ev(24)])
    assert len(result.drafts) == 2


def test_cooldown_counts_from_last_issued_not_last_seen() -> None:
    # 0 issued, 20 suppressed, 30 issued (30 h after 0), 40 suppressed (10 h after 30).
    result = plan([ev(0), ev(20), ev(30), ev(40)])
    assert [d.measured_at for d in result.drafts] == [T0, T0 + timedelta(hours=30)]


def test_cooldown_is_per_state() -> None:
    result = plan([ev(0, PRE_HIGH), ev(1, BREACH_HIGH, "131.0")])
    assert {d.state for d in result.drafts} == {"pre_breach_high", "breach_high"}


def test_cooldown_is_per_limit_kind() -> None:
    post_meal = BandResult("pre_breach_high", "post_meal_high", Decimal("180.0"), escalate=False)
    result = plan([ev(0, PRE_HIGH), ev(1, post_meal, "175.0", "post_meal")])
    assert len(result.drafts) == 2


def test_prior_advisory_suppresses_new_reading_within_cooldown() -> None:
    first = plan([ev(0)])
    later = plan([ev(12)], [as_prior(first.drafts[0])])
    assert later.drafts == []
    assert later.suppressed == 1


def test_late_reported_earlier_reading_is_suppressed_too() -> None:
    # Advisory exists for a reading at +10 h; a reading measured at 0 h arrives later.
    existing = plan([ev(10)])
    late = plan([ev(0)], [as_prior(existing.drafts[0])])
    assert late.drafts == []


def test_non_positive_cooldown_rejected() -> None:
    with pytest.raises(ValueError, match="cooldown"):
        plan_advisories([ev(0)], [], cooldown=timedelta(0), preferred_language="en")


def test_non_normal_band_without_limit_is_a_bug() -> None:
    broken = BandResult("breach_high", None, None, escalate=False)
    with pytest.raises(ValueError, match="no limit"):
        plan([ev(0, broken)])


# --- escalation and text -----------------------------------------------------------


def test_breach_is_escalated_pre_breach_is_not() -> None:
    result = plan([ev(0, PRE_HIGH), ev(1, BREACH_HIGH, "135.0")])
    escalated = {d.state: d.escalated for d in result.drafts}
    assert escalated == {"pre_breach_high": False, "breach_high": True}


def test_level_2_hypo_message_points_to_emergency_help() -> None:
    hypo = BandResult("breach_low", "low", Decimal("70.0"), escalate=True)
    draft = plan([ev(0, hypo, "48.5")]).drafts[0]
    assert draft.escalated
    assert "48.5 mg/dL" in draft.message_text
    assert "emergency" in draft.message_text


def test_message_has_values_and_no_dosing_words() -> None:
    text = render_message(
        state="pre_breach_high",
        context="post_meal",
        value_mg_dl=Decimal("172.0"),
        threshold_mg_dl=Decimal("180.0"),
        urgent_low=False,
        language="en",
    )
    assert "after-meal glucose reading of 172 mg/dL" in text
    assert "180 mg/dL" in text
    for banned in ("insulin", "dose", "units", "tablet", "medication"):
        assert banned not in text.lower()


def test_random_context_reads_naturally() -> None:
    text = render_message(
        state="breach_low",
        context="random",
        value_mg_dl=Decimal("68.4"),
        threshold_mg_dl=Decimal("70.0"),
        urgent_low=False,
        language="en",
    )
    assert text.startswith("Your glucose reading of 68.4 mg/dL")


def test_messages_stay_short_enough_to_speak_in_25_seconds() -> None:
    # ~150 spoken words per minute -> 25 s is about 60 words.
    for state in ("pre_breach_high", "breach_high", "pre_breach_low", "breach_low"):
        text = render_message(
            state=state,
            context="fasting",
            value_mg_dl=Decimal("50.0"),
            threshold_mg_dl=Decimal("70.0"),
            urgent_low=True,
            language="en",
        )
        assert len(text.split()) <= 70


def test_untranslated_languages_fall_back_to_english() -> None:
    assert advisory_language("ta") == "en"
    assert advisory_language("hi") == "en"
    assert advisory_language("en") == "en"
    draft = plan_advisories([ev(0)], [], cooldown=COOLDOWN, preferred_language="ta").drafts[0]
    assert draft.language == "en"
    with pytest.raises(ValueError, match="no advisory templates"):
        render_message(
            state="breach_low",
            context="fasting",
            value_mg_dl=Decimal("60"),
            threshold_mg_dl=Decimal("70"),
            urgent_low=False,
            language="hi",
        )


# --- Level 2 hypoglycaemia bypasses the cooldown -------------------------------------

LEVEL_2 = BandResult("breach_low", "low", Decimal("70.0"), escalate=True)
BREACH_LOW = BandResult("breach_low", "low", Decimal("70.0"), escalate=False)


def test_every_level_2_hypo_creates_an_escalated_advisory() -> None:
    readings = [ev(0, LEVEL_2, "52.0"), ev(1, LEVEL_2, "49.0"), ev(1.5, LEVEL_2, "53.9")]
    result = plan(readings)
    assert len(result.drafts) == 3
    assert result.suppressed == 0
    assert all(d.escalated and d.state == "breach_low" for d in result.drafts)
    assert all("emergency" in d.message_text for d in result.drafts)


def test_level_2_hypo_not_suppressed_by_prior_advisory() -> None:
    first = plan([ev(0, BREACH_LOW, "65.0")])
    later = plan([ev(2, LEVEL_2, "50.0")], [as_prior(first.drafts[0])])
    assert len(later.drafts) == 1
    assert later.drafts[0].escalated


def test_level_2_hypo_still_respects_idempotency() -> None:
    readings = [ev(0, LEVEL_2, "50.0"), ev(1, LEVEL_2, "48.0")]
    first = plan(readings)
    second = plan(readings, [as_prior(d) for d in first.drafts])
    assert second.drafts == []
    assert second.duplicates == 2


def test_ordinary_low_after_level_2_is_still_cooled_down() -> None:
    # 54 mg/dL is not Level 2 (strictly < 54), so it follows the normal cooldown.
    result = plan([ev(0, LEVEL_2, "50.0"), ev(3, BREACH_LOW, "54.0"), ev(4, BREACH_LOW, "66.0")])
    assert [d.reading_value_mg_dl for d in result.drafts] == [Decimal("50.0")]
    assert result.suppressed == 2
