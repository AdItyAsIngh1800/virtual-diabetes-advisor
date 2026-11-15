"""Advisory planning (R3): which classified readings become queued advisories.

The batch job (R1) classifies every new reading with
:func:`app.services.thresholds.classify_reading`; this module decides which of those
results become ``advisories`` rows, and writes their fixed-template text. It is pure
(no I/O, no clock), so de-duplication and cooldown are tested without a database.

Rules, applied to readings oldest first:

1. ``normal`` never creates an advisory.
2. **Idempotency (R1):** a reading that already has an advisory for the same limit is
   skipped. Mirrors the ``advisories_reading_limit_key`` unique constraint.
3. **Cooldown (R3):** no two advisories for the same user, limit and state whose
   readings were *measured* less than ``ADVISORY_COOLDOWN_HOURS`` apart. Advisories
   that ``expired`` unsent do not count (the patient never saw them). Measured time,
   not insert time: the batch job may process a day of readings in one run, and a
   window re-run must reach the same decisions. The check is symmetric, so a reading
   reported late is suppressed by an advisory for a later reading too.
4. **Level 2 hypoglycaemia bypasses the cooldown:** every reading below 54 mg/dL
   (``clinical_limits.HYPO_LEVEL_2_MG_DL``, ADA 2026 Table 6.4) gets its own escalated
   advisory - each one is a clinically significant event a practitioner must see.
   Idempotency still applies, and these advisories still count toward the cooldown
   of later, less severe lows in the same state.

Nothing here sends a message: rows are created with ``delivery_status = 'queued'`` and
``app.jobs.dispatch_advisories`` delivers them. This module also builds the text the
dispatcher needs: :func:`spoken_text` for TTS and :func:`notification_text` for the push.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.core.clinical_limits import GlucoseLimitKind
from app.db.models import AdvisoryState, DeliveryStatus, Language, ReadingContext
from app.services.thresholds import BandResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Message templates (web-push-voice-advisory skill: fixed templates, never free LLM
# text for alerts; no dosing advice, no diagnosis). Every template states the reading,
# the patient's personal limit, the direction (high / low), ONE safe general action
# and "Contact your doctor if ...". Breaches add the care-team / urgent-help line;
# readings below 54 mg/dL add the emergency line instead. Low-glucose *treatment*
# (e.g. grams of fast carbohydrate) is deliberately absent: clinical_limits.py has no
# cited source for it, so lows point to the doctor and to emergency help.
# ---------------------------------------------------------------------------

# Only English templates exist so far. Tamil and Hindi versions need a reviewed
# clinical translation before they may be sent, so ta/hi users get English for now and
# the advisory row records 'en' (the language actually used, which TTS will speak).
_TEMPLATE_LANGUAGES: Final[frozenset[Language]] = frozenset({"en"})
_FALLBACK_LANGUAGE: Final[Language] = "en"

_CONTEXT_WORDS: Final[Mapping[ReadingContext, str]] = MappingProxyType(
    {"fasting": "fasting", "post_meal": "after-meal", "random": ""}
)

_TEMPLATES_EN: Final[Mapping[AdvisoryState, str]] = MappingProxyType(
    {
        "pre_breach_high": (
            "Your {context}glucose is {value} mg/dL, running high: close to your personal "
            "upper limit of {threshold} mg/dL. Keep to your usual meals and activity, and "
            "check again at your next planned time. Contact your doctor if your readings "
            "keep rising."
        ),
        "breach_high": (
            "Your {context}glucose is {value} mg/dL. That is high, at or above your "
            "personal upper limit of {threshold} mg/dL. Check again at your next planned "
            "time. Contact your doctor if it stays high."
        ),
        "pre_breach_low": (
            "Your {context}glucose is {value} mg/dL, running low: close to your personal "
            "lower limit of {threshold} mg/dL. Have your next meal or snack on time, and "
            "check again soon. Contact your doctor if your readings keep falling or you "
            "feel unwell."
        ),
        "breach_low": (
            "Your {context}glucose is {value} mg/dL. That is low, at or below your "
            "personal lower limit of {threshold} mg/dL. Do not drive, and check again "
            "soon. Contact your doctor if it stays low."
        ),
    }
)
# Every breach is escalated (advisories.escalated, DB CHECK), so the dashboard shows
# it to the care team (R6); tell the patient, and where to go if they feel very unwell.
_BREACH_LINE_EN: Final = (
    " This reading is flagged for your care team. If you feel very unwell, get urgent help."
)
# Replaces the breach line when the reading is below 54 mg/dL (ADA Level 2
# hypoglycaemia, see clinical_limits.HYPO_LEVEL_2_MG_DL): urgent, so name emergency
# services explicitly.
_URGENT_LOW_EN: Final = (
    " This very low reading is flagged for your care team. If you feel confused or "
    "faint, call emergency services now."
)

# Words an advisory must never contain: no dosing or medication instructions
# (web-push-voice-advisory skill). Checked by tests over every template combination.
DOSING_WORDS: Final[frozenset[str]] = frozenset(
    {
        "dose",
        "dosage",
        "insulin",
        "units",
        "tablet",
        "tablets",
        "pill",
        "pills",
        "medication",
        "medicine",
        "metformin",
        "inject",
        "injection",
        "grams",
    }
)

# Spoken length budget (skill rule: under ~25 s). Fitted to gTTS's Indian-English
# voice (tld co.in) on these templates, Nov 2026: 150 words per minute when a number
# counts as the words it is read out as ("185.5" -> "one hundred eighty-five point
# five"). Fit error was within +/-1.5 s; the longest variants (breach_high and
# below-54, after-meal, decimal values) measured 23.6 s and 23.5 s.
SPOKEN_WORDS_PER_MINUTE: Final = 150
MAX_SPOKEN_SECONDS: Final = 25
_NUMBER: Final = re.compile(r"^\d+(?:\.\d+)?[.,:]?$")
# "123" -> one hundred twenty-three (4 words); a decimal adds "point" plus a digit.
_WORDS_PER_INTEGER: Final = 4
_WORDS_PER_DECIMAL: Final = 2

# Push notification headings per state (the body carries reading and limit only).
_TITLES_EN: Final[Mapping[AdvisoryState, str]] = MappingProxyType(
    {
        "pre_breach_high": "Glucose close to your upper limit",
        "breach_high": "High glucose reading",
        "pre_breach_low": "Glucose close to your lower limit",
        "breach_low": "Low glucose reading",
    }
)
_URGENT_TITLE_EN: Final = "Very low glucose reading"
_LIMIT_WORD: Final[Mapping[AdvisoryState, str]] = MappingProxyType(
    {
        "pre_breach_high": "upper",
        "breach_high": "upper",
        "pre_breach_low": "lower",
        "breach_low": "lower",
    }
)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluatedReading:
    """A new reading and its band classification.

    Attributes:
        reading_id: ``readings.id``.
        measured_at: When it was measured (timezone-aware).
        context: ``fasting``, ``post_meal`` or ``random``.
        value_mg_dl: The reading.
        band: Result of :func:`app.services.thresholds.classify_reading`.
    """

    reading_id: uuid.UUID
    measured_at: datetime
    context: ReadingContext
    value_mg_dl: Decimal
    band: BandResult


class PriorAdvisory(BaseModel):
    """An existing advisory, as needed for idempotency and cooldown checks.

    Attributes:
        reading_id: Reading it was raised for.
        limit_kind: Limit it refers to.
        state: Band state it was raised for.
        reading_measured_at: When that reading was measured.
        delivery_status: Where its notification got to. ``expired`` advisories were
            never shown to the patient, so they do not start a cooldown.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    reading_id: uuid.UUID
    limit_kind: GlucoseLimitKind
    state: AdvisoryState
    reading_measured_at: AwareDatetime
    delivery_status: DeliveryStatus = "queued"


class AdvisoryDraft(BaseModel):
    """An advisory to insert (``delivery_status`` defaults to ``queued`` in SQL).

    Attributes:
        reading_id: Reading that triggered it.
        limit_kind: Limit that was approached or crossed.
        state: Band state.
        reading_value_mg_dl: The reading.
        threshold_mg_dl: The personal threshold it was judged against.
        escalated: Practitioner must be told (every breach, and Level 2 hypo).
        language: Language of ``message_text``.
        message_text: Fixed-template advisory text.
        measured_at: When the reading was measured.
    """

    model_config = ConfigDict(frozen=True)

    reading_id: uuid.UUID
    limit_kind: GlucoseLimitKind
    state: AdvisoryState
    reading_value_mg_dl: Decimal = Field(gt=0)
    threshold_mg_dl: Decimal = Field(gt=0)
    escalated: bool
    language: Language
    message_text: str = Field(min_length=1)
    measured_at: AwareDatetime


@dataclass(frozen=True)
class AdvisoryPlan:
    """What :func:`plan_advisories` decided for one user's new readings.

    Attributes:
        drafts: Advisories to create, oldest reading first.
        duplicates: Non-normal results skipped because the advisory already exists.
        suppressed: Non-normal results skipped by the cooldown.
    """

    drafts: list[AdvisoryDraft]
    duplicates: int
    suppressed: int


# ---------------------------------------------------------------------------
# Message text
# ---------------------------------------------------------------------------


def _format_mg_dl(value: Decimal) -> str:
    """Format a glucose value for speech: ``123.0 -> "123"``, ``123.5 -> "123.5"``.

    Args:
        value: Value in mg/dL.

    Returns:
        The value without a trailing ``.0``.
    """
    text = f"{value:.1f}"
    return text.removesuffix(".0")


def advisory_language(preferred: Language) -> Language:
    """Pick the language an advisory is written in.

    Args:
        preferred: The user's profile language.

    Returns:
        ``preferred`` if a template exists for it, otherwise English.
    """
    return preferred if preferred in _TEMPLATE_LANGUAGES else _FALLBACK_LANGUAGE


def render_message(
    *,
    state: AdvisoryState,
    context: ReadingContext,
    value_mg_dl: Decimal,
    threshold_mg_dl: Decimal,
    urgent_low: bool,
    language: Language,
) -> str:
    """Fill the fixed advisory template for one reading.

    Args:
        state: Band state.
        context: Reading context, named in the message.
        value_mg_dl: The reading.
        threshold_mg_dl: The personal threshold.
        urgent_low: Reading is Level 2 hypoglycaemia (< 54 mg/dL).
        language: Must be a language with templates (see :func:`advisory_language`).

    Returns:
        The message text.

    Raises:
        ValueError: If no template exists for ``language``.
    """
    if language not in _TEMPLATE_LANGUAGES:
        raise ValueError(f"no advisory templates for language {language!r}")
    context_word = _CONTEXT_WORDS[context]
    text = _TEMPLATES_EN[state].format(
        context=f"{context_word} " if context_word else "",
        value=_format_mg_dl(value_mg_dl),
        threshold=_format_mg_dl(threshold_mg_dl),
    )
    if urgent_low:
        return text + _URGENT_LOW_EN
    if state.startswith("breach_"):
        return text + _BREACH_LINE_EN
    return text


def spoken_text(message_text: str) -> str:
    """Turn stored advisory text into what TTS should say.

    "mg/dL" is dropped: TTS reads it letter by letter ("m g slash d l"), which costs
    seconds and helps nobody; the numbers and "limit" carry the meaning.

    Args:
        message_text: ``advisories.message_text``.

    Returns:
        The text to synthesise.
    """
    return message_text.replace(" mg/dL", "")


def estimated_speech_seconds(text: str) -> float:
    """Estimate how long gTTS takes to speak ``text`` (numbers count as spoken words).

    Args:
        text: Text to be spoken.

    Returns:
        Seconds.
    """
    words = 0
    for token in text.split():
        if _NUMBER.match(token):
            words += _WORDS_PER_INTEGER + (_WORDS_PER_DECIMAL if "." in token.rstrip(".,:") else 0)
        else:
            words += 1
    return words * 60.0 / SPOKEN_WORDS_PER_MINUTE


def notification_text(
    *,
    state: AdvisoryState,
    context: ReadingContext,
    value_mg_dl: Decimal,
    threshold_mg_dl: Decimal,
    urgent_low: bool,
) -> tuple[str, str]:
    """Build the push notification's title and body.

    Only the reading and the patient's own limit appear (skill rule): a lock screen is
    visible to anyone nearby.

    Args:
        state: Band state.
        context: Reading context.
        value_mg_dl: The reading.
        threshold_mg_dl: The personal limit.
        urgent_low: Reading is below 54 mg/dL.

    Returns:
        ``(title, body)``.
    """
    title = _URGENT_TITLE_EN if urgent_low else _TITLES_EN[state]
    context_word = _CONTEXT_WORDS[context]
    reading = f"{context_word.capitalize()} reading" if context_word else "Reading"
    body = (
        f"{reading} {_format_mg_dl(value_mg_dl)} mg/dL, your {_LIMIT_WORD[state]} limit "
        f"{_format_mg_dl(threshold_mg_dl)} mg/dL. Tap to open your advice."
    )
    return title, body


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_advisories(
    evaluated: Sequence[EvaluatedReading],
    prior: Sequence[PriorAdvisory],
    *,
    cooldown: timedelta,
    preferred_language: Language,
) -> AdvisoryPlan:
    """Decide which classified readings become advisories (rules in the module doc).

    Args:
        evaluated: The user's new readings with their band results, any order.
        prior: The user's existing advisories whose readings were measured within
            ``cooldown`` of any new reading (more is harmless).
        cooldown: ``ADVISORY_COOLDOWN_HOURS`` as a timedelta.
        preferred_language: The user's profile language.

    Returns:
        Drafts to insert plus counts of duplicates and cooldown suppressions.

    Raises:
        ValueError: If ``cooldown`` is not positive, or a non-normal band result has
            no limit or threshold (a bug in the classifier).
    """
    if cooldown <= timedelta(0):
        raise ValueError(f"cooldown must be positive, got {cooldown}")
    language = advisory_language(preferred_language)
    existing: set[tuple[uuid.UUID, GlucoseLimitKind]] = {
        (p.reading_id, p.limit_kind) for p in prior
    }
    issued: defaultdict[tuple[GlucoseLimitKind, AdvisoryState], list[datetime]] = defaultdict(list)
    for p in prior:
        # An expired advisory was never sent (its reading was too old by the time the
        # dispatcher saw it), so it must not silence a fresh alert. It still counts
        # for idempotency above: its reading never gets a second advisory.
        if p.delivery_status != "expired":
            issued[(p.limit_kind, p.state)].append(p.reading_measured_at)

    drafts: list[AdvisoryDraft] = []
    duplicates = suppressed = 0
    # Oldest first (reading id breaks ties) so the outcome never depends on the order
    # the database returned rows in - required for window re-runs to be identical.
    for ev in sorted(evaluated, key=lambda e: (e.measured_at, e.reading_id)):
        band = ev.band
        if band.state == "normal":
            continue
        if band.limit_kind is None or band.threshold_mg_dl is None:
            raise ValueError(f"band result {band.state} has no limit/threshold")
        state: AdvisoryState = band.state
        if (ev.reading_id, band.limit_kind) in existing:
            duplicates += 1
            continue
        # R3 cooldown: one advisory per user per limit per state per N hours, except
        # Level 2 hypoglycaemia (< 54 mg/dL), which is never suppressed (rule 4).
        earlier = issued[(band.limit_kind, state)]
        if not band.escalate and any(abs(ev.measured_at - t) < cooldown for t in earlier):
            suppressed += 1
            continue
        drafts.append(
            AdvisoryDraft(
                reading_id=ev.reading_id,
                limit_kind=band.limit_kind,
                state=state,
                reading_value_mg_dl=ev.value_mg_dl,
                threshold_mg_dl=band.threshold_mg_dl,
                escalated=band.needs_practitioner,
                language=language,
                message_text=render_message(
                    state=state,
                    context=ev.context,
                    value_mg_dl=ev.value_mg_dl,
                    threshold_mg_dl=band.threshold_mg_dl,
                    urgent_low=band.escalate,
                    language=language,
                ),
                measured_at=ev.measured_at,
            )
        )
        earlier.append(ev.measured_at)
        existing.add((ev.reading_id, band.limit_kind))
    if duplicates or suppressed:
        logger.debug(
            "advisory plan: %d new, %d duplicate, %d within cooldown",
            len(drafts),
            duplicates,
            suppressed,
        )
    return AdvisoryPlan(drafts=drafts, duplicates=duplicates, suppressed=suppressed)
