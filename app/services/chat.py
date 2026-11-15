"""Two-way help chat: route each patient message (R4) and describe the turn for audit (R5).

Every message (typed, or transcribed from voice) goes through :meth:`ChatEngine.handle`,
which tries these routes **in order** and stops at the first that applies:

a. **Red flag** (:func:`detect_red_flag`): chest pain, trouble breathing, confusion,
   fainting/unconsciousness, seizure, self-harm words, or a glucose value below 54 or
   above 250 mg/dL. Reply with a **fixed** emergency template and open (or reuse) a
   handoff so the practitioner is told at once. Never sent to an LLM: no model is even
   constructed on this path.
b. **Reading report** (:func:`parse_reading`): "fasting 132", "sugar after lunch 190".
   The value and context are stored as a reading (source ``web``), classified against
   the patient's personal thresholds right away, and an advisory is queued when the
   reading is pre-breach or breach (the dispatcher delivers it).
c. **Practitioner request** ("talk to a doctor", or the advisory page button): open the
   patient's one live handoff and notify the assigned practitioner.
d. While a handoff is live, anything else is **relayed** to the practitioner.
e. **Question**: TF-IDF retrieval over approved ``kb_entries`` (:class:`KbIndexCache`).
   Below ``CHAT_KB_MIN_SIMILARITY`` the chat says it does not know and offers
   practitioner connect; otherwise it answers with the best entry and cites its title.
   With an Anthropic key, :class:`ClaudeRephraser` may rephrase the answer using only
   the retrieved entries; without one (or on any failure) the entry text is returned
   as written. Everything works without the key.

The engine talks to the database through the :class:`ChatStore` Protocol
(``app.db.chat_repository.SqlChatStore``), so routing is tested with fakes. It never
commits and never sends notifications: the API does both after the turn is decided.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Final, Literal, Protocol

import numpy as np
import sklearn
from pydantic import BaseModel, ConfigDict
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

from app.core.clinical_limits import HYPO_LEVEL_2_MG_DL, LEVEL_2_HYPERGLYCAEMIA_MG_DL
from app.db.models import HandoffStatus, HandoffTrigger, ReadingContext, TurnIntent
from app.services.thresholds import BandState

logger = logging.getLogger(__name__)

RedFlag = Literal[
    "self_harm", "breathing", "chest_pain", "consciousness", "confusion", "very_low", "very_high"
]
# Audit labels (conversation_turns.model) for the non-LLM routes.
RULES_MODEL: Final = "rules"
RULES_MODEL_VERSION: Final = "chat-rules-1"
TFIDF_MODEL: Final = "tfidf"
RELAY_MODEL: Final = "handoff_relay"

# R4 emergency line. 112 is India's single emergency number (ERSS), which the demo
# cohort (India, IST) uses; change it together with DEMO_TZ for another country.
EMERGENCY_NUMBER: Final = "112"
# Tele-MANAS, the Government of India's free 24x7 mental-health helpline.
MENTAL_HEALTH_HELPLINE: Final = "Tele-MANAS on 14416"

# Sanity range for a typed glucose value, not a clinical limit: home meters read
# roughly 20-600 mg/dL and show LO/HI outside it, so other numbers ("30 minutes",
# "2 hours", "1000 steps") are not readings. Same range as scripts/seed_demo.py.
_METER_MIN_MG_DL: Final = Decimal(20)
_METER_MAX_MG_DL: Final = Decimal(600)

# Retrieval: how many entries the rephraser may see, and the cut-off for "close
# enough to the best hit to include" (relative to the best score).
MAX_CONTEXT_ENTRIES: Final = 3
_SECOND_HIT_RATIO: Final = 0.6
# Replies are read on a phone and maybe spoken: keep them short.
MAX_REPLY_CHARS: Final = 1200


# ---------------------------------------------------------------------------
# Text normalisation and red flags
# ---------------------------------------------------------------------------


# Curly quotes and backticks typed by phone keyboards ("can\u2019t") -> ASCII apostrophe.
_QUOTES: Final = str.maketrans({0x2019: "'", 0x2018: "'", 0x60: "'"})


def normalise(text: str) -> str:
    """Lower-case, straighten quotes and collapse whitespace.

    Args:
        text: Raw message.

    Returns:
        The normalised text.
    """
    straight = text.translate(_QUOTES)
    return " ".join(straight.lower().split())


# Ordered: self-harm first so its template (helpline) wins over generic symptoms.
_RED_FLAG_PATTERNS: Final[tuple[tuple[RedFlag, re.Pattern[str]], ...]] = (
    (
        "self_harm",
        re.compile(
            r"\b(suicid\w*|kill (?:my ?self|me)|end (?:my|it) (?:life|all)|self[- ]?harm"
            r"|hurt(?:ing)? my ?self|want to die|don'?t want to (?:live|be alive)"
            r"|better off dead|overdose)\b"
        ),
    ),
    (
        "breathing",
        re.compile(
            r"\b(can'?t|cannot|can not|unable to|hard to|struggling to|trouble|difficulty)"
            r" (?:breath\w*)|short(?:ness)? of breath|breathless|not breathing\b"
        ),
    ),
    (
        "chest_pain",
        # A few words may sit between ("chest feels tight", "chest is very heavy").
        re.compile(
            r"\bchest\b(?:\s+\S+){0,3}?\s+(?:pain\w*|tight\w*|pressure|hurt\w*|heav\w*|"
            r"discomfort|ache\w*)\b|heart attack"
        ),
    ),
    (
        "consciousness",
        re.compile(
            r"\b(faint(?:ed|ing)?|pass(?:ed|ing)? out|black(?:ed)? out|unconscious|unresponsive"
            r"|collaps\w*|seizure|fitting|convuls\w*|won'?t wake up|not waking up)\b"
        ),
    ),
    ("confusion", re.compile(r"\b(confus\w*|disoriented|can'?t think straight)\b")),
)

# A trailing full stop is punctuation, not a decimal point: Whisper transcripts end
# sentences with one ("My fasting sugar is 132.").
_NUMBER: Final = re.compile(r"(?<![\d.])(\d{2,3}(?:\.\d)?)(?!\d|\.\d)")
# Units that mean a number is not a glucose value ("30 min walk", "2 hours", "70 kg").
_NOT_GLUCOSE_UNIT: Final = re.compile(
    r"^\s*(?:min\w*|hr\w*|hour\w*|sec\w*|day\w*|week\w*|month\w*|year\w*|yrs?|kg|kgs|kilo\w*"
    r"|lb\w*|steps?|cm|km|%|percent|bpm|calorie\w*|kcal|g\b|grams?|ml|units?|mmol\w*)"
)
# Words that make a number a glucose report.
_GLUCOSE_CUE: Final = re.compile(
    r"\b(sugar|glucose|bg|bgl|blood|reading|level|mg|mgdl|mg/dl|fasting|fbs|ppbs|rbs|pp|"
    r"after (?:food|meal|eating|lunch|dinner|breakfast)|post[- ]?meal|before|empty stomach|"
    r"meter|checked|tested|measured|morning)\b"
)
_QUESTION_START: Final = re.compile(
    r"^(what|why|how|when|where|which|who|is|are|can|could|should|would|will|do|does|did)\b"
)


def _numbers(text: str) -> list[tuple[Decimal, int]]:
    """Find candidate glucose numbers with their end offsets.

    Args:
        text: Normalised text.

    Returns:
        ``(value, end index)`` for each 2-3 digit number not followed by a non-glucose
        unit and inside the meter range.
    """
    found: list[tuple[Decimal, int]] = []
    for match in _NUMBER.finditer(text):
        if _NOT_GLUCOSE_UNIT.match(text[match.end() :]):
            continue
        try:
            value = Decimal(match.group(1))
        except InvalidOperation:  # pragma: no cover - the regex only matches digits
            continue
        if _METER_MIN_MG_DL <= value <= _METER_MAX_MG_DL:
            found.append((value, match.end()))
    return found


def glucose_value(text: str) -> Decimal | None:
    """Return the one glucose value a message mentions, if unambiguous.

    A bare number ("132") counts, as does a number with a glucose cue ("sugar 132",
    "fasting 132"). Messages with several candidate numbers return ``None``.

    Args:
        text: Raw or normalised message.

    Returns:
        The value in mg/dL, or ``None``.
    """
    norm = normalise(text)
    if "mmol" in norm:
        # mmol/L values need a unit conversion the patient did not confirm; ask instead.
        return None
    numbers = _numbers(norm)
    if len(numbers) != 1:
        return None
    value, _ = numbers[0]
    bare = re.fullmatch(r"\d{2,3}(?:\.\d)?\s*(?:mg\s*/?\s*dl)?[.!]?", norm) is not None
    if bare or _GLUCOSE_CUE.search(norm):
        return value
    return None


def detect_red_flag(text: str) -> RedFlag | None:
    """Spot messages that must get the emergency template (route a).

    Args:
        text: Raw message (typed or transcribed).

    Returns:
        The first matching red flag, or ``None``.
    """
    norm = normalise(text)
    for flag, pattern in _RED_FLAG_PATTERNS:
        if pattern.search(norm):
            return flag
    value = glucose_value(norm)
    if value is not None:
        # ADA-6 Table 6.4 / IHSG: < 54 mg/dL is Level 2 (clinically significant)
        # hypoglycaemia; every such reading escalates (clinical_limits).
        if value < Decimal(str(HYPO_LEVEL_2_MG_DL)):
            return "very_low"
        # ADA-6 Table 6.2 / TIR: > 250 mg/dL is Level 2 hyperglycaemia ("very high").
        if value > Decimal(str(LEVEL_2_HYPERGLYCAEMIA_MG_DL)):
            return "very_high"
    return None


def emergency_reply(
    flag: RedFlag, *, value_mg_dl: Decimal | None, practitioner_notified: bool
) -> str:
    """Fixed emergency template for a red flag (never LLM text).

    Args:
        flag: The red flag.
        value_mg_dl: Reported glucose, for the very_low/very_high templates.
        practitioner_notified: Whether a practitioner was assigned and notified.

    Returns:
        The reply.
    """
    if practitioner_notified:
        team = "A practitioner from your care team has been notified and will reply here."
    else:
        team = (
            "No practitioner is linked to your account yet, so please also call your "
            "doctor directly."
        )
    value = f"{value_mg_dl.normalize():f} mg/dL" if value_mg_dl is not None else "this reading"
    if flag == "self_harm":
        return (
            "I'm really sorry you're going through this. You deserve support right now. "
            f"If you might act on these thoughts, call {EMERGENCY_NUMBER} now. You can also "
            f"call {MENTAL_HEALTH_HELPLINE}, free, any time of day or night. "
            f"If you can, stay with someone you trust. {team}"
        )
    if flag == "very_low":
        # ADA-6 hypoglycaemia treatment: glucose (any carbohydrate containing glucose)
        # is the preferred treatment for a conscious person with hypoglycaemia.
        return (
            f"Your glucose of {value} is very low. If you are awake and able to swallow, "
            "have something sugary now, such as fruit juice or a regular (not diet) soft "
            "drink, and check again in 15 minutes. If you feel confused, drowsy or faint, "
            f"or cannot swallow, call {EMERGENCY_NUMBER} now. Do not drive. {team}"
        )
    if flag == "very_high":
        return (
            f"Your glucose of {value} is very high. If you are vomiting, breathing fast, "
            f"very drowsy or confused, call {EMERGENCY_NUMBER} now or go to the nearest "
            "hospital. Otherwise contact your doctor today and check your glucose again "
            f"soon. {team}"
        )
    return (
        "This could be an emergency. If you have chest pain, trouble breathing, "
        f"confusion, or have fainted, call {EMERGENCY_NUMBER} now or go to the nearest "
        "hospital. Do not wait for a reply in this chat. If someone is with you, ask them "
        f"to help. {team}"
    )


# ---------------------------------------------------------------------------
# Reading reports
# ---------------------------------------------------------------------------


class ParsedReading(BaseModel):
    """A glucose value the patient reported in chat.

    Attributes:
        value_mg_dl: Glucose in mg/dL.
        context: Which limits apply (``random`` when no timing was given).
    """

    model_config = ConfigDict(frozen=True)

    value_mg_dl: Decimal
    context: ReadingContext


_POST_MEAL: Final = re.compile(
    r"\b(after (?:food|meal|meals|eating|lunch|dinner|breakfast|having food)|post[- ]?meal|"
    r"post[- ]?prandial|pp|ppbs|\d (?:hours?|hrs?) after)\b|\bafter\b"
)
_FASTING: Final = re.compile(
    r"\b(fasting|fasted|fbs|empty stomach|before (?:breakfast|food|eating|meal)|"
    r"on waking|woke up|first thing)\b"
)


def parse_reading(text: str) -> ParsedReading | None:
    """Parse a reading report (route b).

    "fasting 132" -> 132 fasting; "132 after food" and "sugar after lunch 190" ->
    post-meal; "my sugar is 150" -> random. Questions ("is 132 fasting high?") are not
    reports: nothing is stored for a hypothetical value.

    Args:
        text: Raw message.

    Returns:
        The reading, or ``None`` if the message is not a reading report.
    """
    norm = normalise(text)
    if norm.endswith("?") or _QUESTION_START.match(norm):
        return None
    value = glucose_value(norm)
    if value is None:
        return None
    context: ReadingContext = "random"
    # Post-meal before fasting: "before lunch ... 2 hours after" style mixes are rare,
    # and "after" is the more specific marker.
    if _POST_MEAL.search(norm):
        context = "post_meal"
    elif _FASTING.search(norm):
        context = "fasting"
    return ParsedReading(value_mg_dl=value, context=context)


@dataclass(frozen=True)
class ReadingOutcome:
    """What storing and classifying a chat reading did.

    Attributes:
        reading_id: New ``readings.id``.
        state: Band state, or ``None`` when the patient has no thresholds yet.
        threshold_mg_dl: The limit the state refers to (``None`` when normal/unknown).
        upper_mg_dl: The upper limit for this context, if any applies.
        low_mg_dl: The lower limit, if known.
        advisory_queued: An advisory was queued for the dispatcher.
        escalated: The reading needs a practitioner (any breach, or < 54 mg/dL).
    """

    reading_id: uuid.UUID
    state: BandState | None
    threshold_mg_dl: Decimal | None = None
    upper_mg_dl: Decimal | None = None
    low_mg_dl: Decimal | None = None
    advisory_queued: bool = False
    escalated: bool = False


_CONTEXT_LABEL: Final[dict[ReadingContext, str]] = {
    "fasting": "fasting reading",
    "post_meal": "after-meal reading",
    "random": "reading",
}


def _mg(value: Decimal) -> str:
    """Format mg/dL without a trailing ``.0``.

    Args:
        value: Value.

    Returns:
        E.g. ``"132"`` or ``"48.5"``.
    """
    return f"{value.normalize():f}"


def reading_reply(reading: ParsedReading, outcome: ReadingOutcome) -> str:
    """Reply to a stored reading with its status against the personal limits.

    Args:
        reading: What the patient reported.
        outcome: Classification result.

    Returns:
        The reply text.
    """
    saved = f"Saved your {_CONTEXT_LABEL[reading.context]}: {_mg(reading.value_mg_dl)} mg/dL."
    if outcome.state is None:
        return (
            f"{saved} Your personal limits are not set up yet, so I can't compare it. "
            "Your care team can see it."
        )
    limit = _mg(outcome.threshold_mg_dl) if outcome.threshold_mg_dl is not None else "?"
    status = {
        "normal": "That is within your personal range",
        "pre_breach_high": f"That is close to your personal upper limit of {limit} mg/dL",
        "breach_high": f"That is at or above your personal upper limit of {limit} mg/dL",
        "pre_breach_low": f"That is close to your personal lower limit of {limit} mg/dL",
        "breach_low": f"That is at or below your personal lower limit of {limit} mg/dL",
    }[outcome.state]
    if outcome.state == "normal":
        bounds = []
        if outcome.low_mg_dl is not None:
            bounds.append(f"lower limit {_mg(outcome.low_mg_dl)}")
        if outcome.upper_mg_dl is not None:
            bounds.append(f"upper limit {_mg(outcome.upper_mg_dl)}")
        if bounds:
            status += f" ({', '.join(bounds)} mg/dL)"
    parts = [saved, f"{status}."]
    if outcome.state != "normal":
        if outcome.advisory_queued:
            parts.append("I'm sending you an advisory with what to do next.")
        else:
            parts.append("You had the same alert recently, so check the advice you received.")
        if outcome.escalated:
            parts.append("This reading is flagged for your care team.")
    if reading.context == "random":
        parts.append("Tell me if it was fasting or after a meal and I can compare it fully.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Practitioner requests and greetings
# ---------------------------------------------------------------------------

_PRACTITIONER_REQUEST: Final = re.compile(
    r"\b(talk|speak|chat|connect|consult)(?:ing)? (?:me )?(?:to|with)\b.*\b"
    r"(doctor|dr|nurse|practitioner|physician|clinician|care team|someone|human|person|"
    r"real person)\b"
    r"|\b(call|contact|message|need|want|see) (?:a |my |the )?"
    r"(doctor|nurse|practitioner|physician|clinician)\b"
    r"|\b(human|real person|real doctor)\b"
)
_GREETING: Final = re.compile(
    r"^(hi|hello|hey|hii+|namaste|good (?:morning|afternoon|evening)|thanks?|thank you|"
    r"ok(?:ay)?|bye)\b[ !.]*$"
)


# "When should I see a doctor?" asks for information (the KB answers it); "can I talk
# to a doctor?" asks for a person.
_INFO_QUESTION: Final = re.compile(r"^(when|what|why|how|which|where|who)\b")


def is_practitioner_request(text: str) -> bool:
    """Spot "talk to a doctor" style requests (route c).

    Args:
        text: Raw message.

    Returns:
        ``True`` if the patient asks for a human (not a when/what/how question
        about doctors, which the knowledge base answers).
    """
    norm = normalise(text)
    if _INFO_QUESTION.match(norm):
        return False
    return _PRACTITIONER_REQUEST.search(norm) is not None


# ---------------------------------------------------------------------------
# Knowledge base retrieval (TF-IDF)
# ---------------------------------------------------------------------------


class KbDoc(BaseModel):
    """One approved knowledge-base entry as retrieval sees it.

    Attributes:
        id: ``kb_entries.id``.
        entry_key: Logical entry.
        title: Title (cited in replies).
        body: Approved text.
        source_citation: Where the content comes from.
        kb_version: Release it was approved into.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    entry_key: str
    title: str
    body: str
    source_citation: str | None
    kb_version: int


class KbVersion(BaseModel):
    """Fingerprint of the approved KB: changes whenever an entry is approved or retired.

    Attributes:
        kb_version: Highest approved ``kb_version`` (stored on each turn).
        entries: Number of approved rows.
        last_change: Latest ``approved_at`` / ``updated_at`` among approved rows.
    """

    model_config = ConfigDict(frozen=True)

    kb_version: int | None
    entries: int
    last_change: datetime | None


class KbSource(Protocol):
    """Where approved entries come from (``SqlChatStore`` in production)."""

    def kb_version(self) -> KbVersion:
        """Return the current fingerprint of the approved KB.

        Returns:
            The fingerprint.
        """
        ...

    def approved_kb_entries(self) -> list[KbDoc]:
        """Return every approved entry.

        Returns:
            Approved entries.
        """
        ...


@dataclass(frozen=True)
class KbHit:
    """One retrieval result.

    Attributes:
        doc: The entry.
        score: Cosine similarity in [0, 1].
    """

    doc: KbDoc
    score: float


# Domain synonyms folded onto one canonical word before TF-IDF, for documents and
# questions alike. Patients say "sugar", "hypo", "feet", "symptoms"; the approved text
# says "glucose", "hypoglycaemia", "foot", "signs". TF-IDF only matches equal tokens,
# so without this an in-scope question can score 0. Multi-word phrases come first.
_SYNONYM_PHRASES: Final[tuple[tuple[str, str], ...]] = (
    ("blood sugar", "glucose"),
    ("low sugar", "hypoglycaemia low"),
    ("low glucose", "hypoglycaemia low"),
    ("sugar low", "hypoglycaemia low"),
    ("high sugar", "hyperglycaemia high"),
    ("high glucose", "hyperglycaemia high"),
    ("sugar high", "hyperglycaemia high"),
    ("15 15", "15-15"),
)
_SYNONYM_WORDS: Final[dict[str, str]] = {
    "sugar": "glucose",
    "sugars": "glucose",
    "bg": "glucose",
    "hypo": "hypoglycaemia",
    "hypos": "hypoglycaemia",
    "hypoglycemia": "hypoglycaemia",
    "hypoglycemic": "hypoglycaemia",
    "hyper": "hyperglycaemia",
    "hyperglycemia": "hyperglycaemia",
    "feet": "foot",
    "toes": "foot",
    "toe": "foot",
    "symptoms": "signs",
    "symptom": "signs",
    "sign": "signs",
    "treat": "treating",
    "treatment": "treating",
    "eat": "eating",
    "food": "eating",
    "foods": "eating",
    "diet": "eating",
    "exercise": "activity",
    "exercises": "activity",
    "workout": "activity",
    "walk": "activity",
    "walking": "activity",
    "active": "activity",
    "physician": "doctor",
    "dr": "doctor",
    "see": "contact",
    "visit": "contact",
    "beer": "alcohol",
    "wine": "alcohol",
    "liquor": "alcohol",
    "ill": "sick",
    "illness": "sick",
    "fever": "sick",
    "cold": "sick",
    "flu": "sick",
    "test": "checking",
    "testing": "checking",
    "check": "checking",
    "measure": "checking",
    "hba1c": "a1c",
    "drive": "driving",
    "tired": "tiredness",
    "means": "mean",
    "meaning": "mean",
    "limit": "limits",
    "target": "limits",
    "targets": "limits",
    "range": "limits",
}
_WORD: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def canonical_text(text: str) -> str:
    """Lower-case text with domain synonyms folded together (TF-IDF preprocessor).

    Args:
        text: Document or question.

    Returns:
        Normalised text, e.g. "low sugar symptoms" -> "hypoglycaemia low glucose... signs".
    """
    norm = f" {normalise(text)} "
    for phrase, canon in _SYNONYM_PHRASES:
        norm = norm.replace(f" {phrase} ", f" {canon} ")
    return " ".join(_SYNONYM_WORDS.get(w, w) for w in _WORD.findall(norm))


# sklearn's English stop words, minus any the synonym map rewrites ("see" -> "contact"),
# so stop-word removal and synonym folding never disagree.
_STOP_WORDS: Final[frozenset[str]] = frozenset(
    w for w in ENGLISH_STOP_WORDS if canonical_text(w) == w
)


class TfidfIndex:
    """TF-IDF index over title + body of approved entries (scikit-learn).

    Args:
        docs: Approved entries.
    """

    def __init__(self, docs: Sequence[KbDoc]) -> None:
        self.docs: tuple[KbDoc, ...] = tuple(docs)
        # Word unigrams + bigrams catch phrases ("foot care", "15-15 rule");
        # sublinear tf stops one repeated word dominating a long entry. The
        # preprocessor folds synonyms so patient wording meets the approved wording.
        self._vectorizer = TfidfVectorizer(
            preprocessor=canonical_text,
            ngram_range=(1, 2),
            sublinear_tf=True,
            stop_words=sorted(_STOP_WORDS),
            # Keep hyphenated tokens ("15-15", "pre-breach"); a regex, not a secret.
            token_pattern=r"(?u)\b[\w-]{2,}\b",  # noqa: S106
        )
        # Sparse document-term matrix (scipy, untyped); None for an empty KB.
        self._matrix: object | None = None
        if self.docs:
            # The title is repeated so a question naming the topic finds its entry.
            corpus = [f"{d.title}. {d.title}. {d.body}" for d in self.docs]
            self._matrix = self._vectorizer.fit_transform(corpus)

    def search(self, query: str, k: int = MAX_CONTEXT_ENTRIES) -> list[KbHit]:
        """Return the ``k`` most similar entries, best first.

        Args:
            query: The question.
            k: Maximum hits.

        Returns:
            Hits with a positive score.
        """
        if self._matrix is None or not query.strip():
            return []
        query_vec = self._vectorizer.transform([query])
        # Rows are L2-normalised by TfidfVectorizer, so the linear kernel is the cosine.
        scores = np.asarray(linear_kernel(self._matrix, query_vec), dtype=float).ravel()
        order = np.argsort(-scores)[:k]
        return [KbHit(self.docs[i], float(scores[i])) for i in order if scores[i] > 0]


class KbIndexCache:
    """Process-wide TF-IDF index, rebuilt only when the approved KB changes.

    Thread-safe: requests run in a thread pool.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._version: KbVersion | None = None
        self._index: TfidfIndex = TfidfIndex(())

    def get(self, source: KbSource) -> tuple[TfidfIndex, KbVersion]:
        """Return the index for the current KB version, rebuilding it if stale.

        Args:
            source: Supplies the version fingerprint and, when stale, the entries.

        Returns:
            The index and the version it was built from.
        """
        version = source.kb_version()
        with self._lock:
            if version != self._version:
                docs = source.approved_kb_entries()
                self._index = TfidfIndex(docs)
                self._version = version
                logger.info(
                    "KB index rebuilt: %d approved entries, kb_version %s",
                    len(docs),
                    version.kb_version,
                )
            return self._index, version


# ---------------------------------------------------------------------------
# Optional LLM rephrasing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rephrased:
    """A rephrased answer.

    Attributes:
        text: Answer text (without the source line, which the engine adds).
        model: Model that wrote it.
    """

    text: str
    model: str


class Rephraser(Protocol):
    """Rewrites retrieved KB text as a direct answer to the question."""

    def rephrase(self, question: str, entries: Sequence[KbDoc]) -> Rephrased | None:
        """Answer ``question`` using only ``entries``.

        Args:
            question: The patient's question.
            entries: Retrieved approved entries (the only allowed context).

        Returns:
            The answer, or ``None`` to fall back to the entry text.
        """
        ...


_NUMBER_IN_TEXT: Final = re.compile(r"\d+(?:\.\d+)?")


def numbers_supported(answer: str, entries: Sequence[KbDoc]) -> bool:
    """Check every number in an LLM answer also appears in the context.

    A number the context does not contain (a dose, a target, a time) is the most
    dangerous kind of invented content, so such answers are discarded.

    Args:
        answer: LLM output.
        entries: Context it was given.

    Returns:
        ``True`` if each number in ``answer`` occurs in some entry.
    """
    context = " ".join(f"{e.title} {e.body}" for e in entries)
    allowed = set(_NUMBER_IN_TEXT.findall(context))
    return all(n in allowed for n in _NUMBER_IN_TEXT.findall(answer))


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HandoffInfo:
    """A live (or just resolved) handoff.

    Attributes:
        id: ``handoffs.id``.
        status: ``open``, ``assigned`` or ``resolved``.
        trigger: Why it was opened.
        practitioner_id: Assigned practitioner, if the patient has one.
    """

    id: uuid.UUID
    status: HandoffStatus
    trigger: HandoffTrigger
    practitioner_id: uuid.UUID | None


class ChatStore(KbSource, Protocol):
    """Database operations the engine needs, inside the caller's transaction."""

    def live_handoff(self, user_id: uuid.UUID) -> HandoffInfo | None:
        """Return the patient's open/assigned handoff.

        Args:
            user_id: Patient.

        Returns:
            The handoff, or ``None``.
        """
        ...

    def open_handoff(
        self,
        user_id: uuid.UUID,
        *,
        trigger: HandoffTrigger,
        reason: str,
        advisory_id: uuid.UUID | None,
    ) -> tuple[HandoffInfo, bool]:
        """Open a handoff unless one is live (one live handoff per patient).

        Args:
            user_id: Patient.
            trigger: Why.
            reason: Short reason for the practitioner (no free patient text).
            advisory_id: Advisory the request came from, if any.

        Returns:
            The live handoff and whether it was created now.
        """
        ...

    def record_reading(
        self, user_id: uuid.UUID, reading: ParsedReading, *, message_id: str | None
    ) -> ReadingOutcome:
        """Store a reading, classify it and queue an advisory if needed.

        Args:
            user_id: Patient.
            reading: Parsed value and context.
            message_id: Client message id (de-duplicates retries).

        Returns:
            What happened.
        """
        ...


@dataclass(frozen=True)
class ChatRequest:
    """One incoming patient message.

    Attributes:
        user_id: Patient.
        text: Typed text, or the transcript of a voice message.
        spoken: The patient spoke (reply also as audio).
        practitioner_requested: The "Talk to a doctor" button was used.
        advisory_id: Advisory the button was pressed on.
        message_id: Client-generated id.
    """

    user_id: uuid.UUID
    text: str
    spoken: bool = False
    practitioner_requested: bool = False
    advisory_id: uuid.UUID | None = None
    message_id: str | None = None


@dataclass
class ChatOutcome:
    """The decided turn: the reply plus everything the audit row needs.

    Attributes:
        intent: Classified intent.
        reply_text: What to show (and speak).
        model: ``rules``, ``tfidf``, ``handoff_relay`` or the LLM name.
        model_version: Version label of that model.
        escalated: A practitioner was brought in (red flag, request, breach reading).
        kb_ids_used: Entries the answer came from.
        kb_version: KB release that answered (questions only).
        retrieval_confidence: Best similarity (questions only).
        handoff: Live handoff this turn belongs to.
        handoff_created: The handoff was opened by this turn.
        notify_practitioner: Tell the practitioner (push + Realtime) after commit.
        urgent: The notification is urgent (red flag).
        red_flag: The red flag, if any.
        reading: Reading outcome, if a reading was stored.
        latency_ms: Time spent deciding.
    """

    intent: TurnIntent
    reply_text: str
    model: str
    model_version: str
    escalated: bool = False
    kb_ids_used: list[uuid.UUID] = field(default_factory=list)
    kb_version: int | None = None
    retrieval_confidence: float | None = None
    handoff: HandoffInfo | None = None
    handoff_created: bool = False
    notify_practitioner: bool = False
    urgent: bool = False
    red_flag: RedFlag | None = None
    reading: ReadingOutcome | None = None
    latency_ms: int = 0


UNKNOWN_REPLY: Final = (
    "I don't know the answer to that from the approved information I have. "
    'Would you like to talk to a practitioner? Tap "Talk to a doctor" or type '
    '"talk to a doctor".'
)
GREETING_REPLY: Final = (
    'Hello! You can send me a glucose reading, like "fasting 132" or "190 after lunch", '
    'ask a question about diabetes, or type "talk to a doctor".'
)
RELAY_REPLY: Final = "Sent to your practitioner. They will reply here."


class ChatEngine:
    """Routes one message and decides the reply (see the module docstring).

    Args:
        store: Database operations (caller's transaction).
        kb_cache: Shared TF-IDF index cache.
        min_similarity: ``CHAT_KB_MIN_SIMILARITY``.
        rephraser: Optional LLM rephraser; ``None`` returns entry text as written.
        clock: Monotonic clock in seconds (tests pass a fake).
    """

    def __init__(
        self,
        store: ChatStore,
        kb_cache: KbIndexCache,
        *,
        min_similarity: float,
        rephraser: Rephraser | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._kb_cache = kb_cache
        self._min_similarity = min_similarity
        self._rephraser = rephraser
        self._clock = clock

    def handle(self, request: ChatRequest) -> ChatOutcome:
        """Route one message.

        Args:
            request: The message.

        Returns:
            The decided turn (not yet stored).

        Raises:
            ValueError: If the message is empty and no practitioner was requested.
        """
        started = self._clock()
        text = request.text.strip()
        if not text and not request.practitioner_requested:
            raise ValueError("empty message")
        outcome = self._route(request, text)
        outcome.latency_ms = max(0, round((self._clock() - started) * 1000))
        logger.info(
            "chat turn for user %s: intent %s via %s (escalated=%s, %d ms)",
            request.user_id,
            outcome.intent,
            outcome.model,
            outcome.escalated,
            outcome.latency_ms,
        )
        return outcome

    def _route(self, request: ChatRequest, text: str) -> ChatOutcome:
        """Apply routes a-e in order.

        Args:
            request: The message.
            text: Stripped text.

        Returns:
            The decided turn.
        """
        # (a) Red flags first, on every message, even inside a handoff.
        flag = detect_red_flag(text) if text else None
        if flag is not None:
            return self._red_flag(request, text, flag)
        live = self._store.live_handoff(request.user_id)
        # (b) Reading reports are stored and judged even while a handoff is live; the
        # turn is then attached to the handoff so the practitioner sees it.
        reading = parse_reading(text) if text else None
        if reading is not None:
            outcome = self._store.record_reading(
                request.user_id, reading, message_id=request.message_id
            )
            return ChatOutcome(
                intent="reading_report",
                reply_text=reading_reply(reading, outcome),
                model=RULES_MODEL,
                model_version=RULES_MODEL_VERSION,
                escalated=outcome.escalated,
                handoff=live,
                notify_practitioner=live is not None,
                reading=outcome,
            )
        # (c) Practitioner connect.
        if request.practitioner_requested or is_practitioner_request(text):
            return self._practitioner_request(request, live)
        # (d) Relay while a handoff is live.
        if live is not None:
            return ChatOutcome(
                intent="other" if _GREETING.match(normalise(text)) else "question",
                reply_text=RELAY_REPLY,
                model=RELAY_MODEL,
                model_version=RULES_MODEL_VERSION,
                handoff=live,
                notify_practitioner=True,
            )
        # (e) Questions answered from the approved KB.
        return self._question(text)

    def _red_flag(self, request: ChatRequest, text: str, flag: RedFlag) -> ChatOutcome:
        """Route (a): fixed template + immediate handoff (no LLM).

        Args:
            request: The message.
            text: Stripped text.
            flag: Detected red flag.

        Returns:
            The decided turn.
        """
        reading_outcome: ReadingOutcome | None = None
        reading = parse_reading(text)
        if reading is not None:
            # "fasting 45" is both a red flag and a reading: keep the data point so the
            # threshold engine and the care team see it.
            reading_outcome = self._store.record_reading(
                request.user_id, reading, message_id=request.message_id
            )
        handoff, created = self._store.open_handoff(
            request.user_id,
            trigger="red_flag",
            reason=f"red flag in chat: {flag.replace('_', ' ')}",
            advisory_id=request.advisory_id,
        )
        return ChatOutcome(
            intent="symptom_red_flag",
            reply_text=emergency_reply(
                flag,
                value_mg_dl=glucose_value(text),
                practitioner_notified=handoff.practitioner_id is not None,
            ),
            model=RULES_MODEL,
            model_version=RULES_MODEL_VERSION,
            escalated=True,
            handoff=handoff,
            handoff_created=created,
            notify_practitioner=True,
            urgent=True,
            red_flag=flag,
            reading=reading_outcome,
        )

    def _practitioner_request(self, request: ChatRequest, live: HandoffInfo | None) -> ChatOutcome:
        """Route (c): open (or reuse) the handoff and notify the practitioner.

        Args:
            request: The message.
            live: Already-live handoff, if any.

        Returns:
            The decided turn.
        """
        handoff, created = (
            (live, False)
            if live is not None
            else self._store.open_handoff(
                request.user_id,
                trigger="practitioner_request",
                reason="patient asked to talk to a practitioner",
                advisory_id=request.advisory_id,
            )
        )
        if handoff.practitioner_id is None:
            reply = (
                "No practitioner is linked to your account yet. Your request is saved for "
                "the care team; please also contact your doctor directly."
            )
        elif created:
            reply = (
                "A practitioner has been notified. They will reply here; anything you "
                "write now goes to them."
            )
        else:
            reply = (
                "A practitioner has already been notified and will reply here. Anything "
                "you write now goes to them."
            )
        return ChatOutcome(
            intent="practitioner_request",
            reply_text=reply,
            model=RULES_MODEL,
            model_version=RULES_MODEL_VERSION,
            escalated=True,
            handoff=handoff,
            handoff_created=created,
            notify_practitioner=handoff.practitioner_id is not None,
        )

    def _question(self, text: str) -> ChatOutcome:
        """Route (e): answer from the approved KB, or say "I don't know".

        Args:
            text: The question.

        Returns:
            The decided turn.
        """
        index, version = self._kb_cache.get(self._store)
        hits = index.search(text)
        best = hits[0].score if hits else 0.0
        tfidf_version = f"sklearn-{sklearn.__version__}"
        if best < self._min_similarity:
            greeting = _GREETING.match(normalise(text)) is not None
            return ChatOutcome(
                intent="other" if greeting else "question",
                reply_text=GREETING_REPLY if greeting else UNKNOWN_REPLY,
                model=TFIDF_MODEL,
                model_version=tfidf_version,
                kb_version=version.kb_version,
                retrieval_confidence=round(best, 4),
            )
        context = [
            h.doc
            for h in hits
            if h.score >= self._min_similarity and h.score >= best * _SECOND_HIT_RATIO
        ]
        top = context[0]
        answer = top.body
        model, model_version = TFIDF_MODEL, tfidf_version
        used = [top]
        if self._rephraser is not None:
            rephrased = self._rephraser.rephrase(text, context)
            if rephrased is not None:
                answer = rephrased.text
                model, model_version = rephrased.model, rephrased.model
                used = context
        source = f"\n\nSource: {top.title}"
        if top.source_citation:
            source += f" ({top.source_citation})"
        if len(answer) > MAX_REPLY_CHARS:
            answer = answer[: MAX_REPLY_CHARS - 1].rsplit(" ", 1)[0] + "…"
        return ChatOutcome(
            intent="question",
            reply_text=answer + source,
            model=model,
            model_version=model_version,
            kb_ids_used=[d.id for d in used],
            kb_version=version.kb_version,
            retrieval_confidence=round(best, 4),
        )
