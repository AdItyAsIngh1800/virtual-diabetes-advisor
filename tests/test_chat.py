"""Tests for app.services.chat: routing order, red flags, readings, KB retrieval (R4).

The engine runs against an in-memory store with the real seeded knowledge base
(scripts/kb_seed.py), so retrieval thresholds are tested on the content patients see.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.db.models import HandoffTrigger
from app.services.chat import (
    EMERGENCY_NUMBER,
    GREETING_REPLY,
    MENTAL_HEALTH_HELPLINE,
    RELAY_REPLY,
    UNKNOWN_REPLY,
    ChatEngine,
    ChatRequest,
    HandoffInfo,
    KbDoc,
    KbIndexCache,
    KbVersion,
    ParsedReading,
    ReadingOutcome,
    Rephrased,
    canonical_text,
    detect_red_flag,
    is_practitioner_request,
    numbers_supported,
    parse_reading,
    reading_reply,
)
from scripts.kb_seed import KB_SEED

SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="srk",
    database_url="postgresql://u:p@localhost:5432/db",
)
MIN_SIMILARITY = SETTINGS.chat_kb_min_similarity
USER = uuid.uuid4()
DOCS = [
    KbDoc(
        id=uuid.uuid4(),
        entry_key=f"seed:{e.key}",
        title=e.title,
        body=e.body,
        source_citation=e.source,
        kb_version=1,
    )
    for e in KB_SEED
]


def doc(key: str) -> KbDoc:
    return next(d for d in DOCS if d.entry_key == f"seed:{key}")


class FakeStore:
    """In-memory ChatStore."""

    def __init__(self, *, practitioner: bool = True) -> None:
        self.practitioner_id = uuid.uuid4() if practitioner else None
        self.live: HandoffInfo | None = None
        self.opened: list[HandoffTrigger] = []
        self.readings: list[ParsedReading] = []
        self.kb_loads = 0
        self.version = KbVersion(kb_version=1, entries=len(DOCS), last_change=None)
        self.outcome = ReadingOutcome(
            reading_id=uuid.uuid4(),
            state="pre_breach_high",
            threshold_mg_dl=Decimal("130.0"),
            upper_mg_dl=Decimal("130.0"),
            low_mg_dl=Decimal("72.0"),
            advisory_queued=True,
        )

    def kb_version(self) -> KbVersion:
        return self.version

    def approved_kb_entries(self) -> list[KbDoc]:
        self.kb_loads += 1
        return DOCS

    def live_handoff(self, user_id: uuid.UUID) -> HandoffInfo | None:
        return self.live

    def open_handoff(
        self,
        user_id: uuid.UUID,
        *,
        trigger: HandoffTrigger,
        reason: str,
        advisory_id: uuid.UUID | None,
    ) -> tuple[HandoffInfo, bool]:
        if self.live is not None:
            return self.live, False
        self.opened.append(trigger)
        self.live = HandoffInfo(uuid.uuid4(), "open", trigger, self.practitioner_id)
        return self.live, True

    def record_reading(
        self, user_id: uuid.UUID, reading: ParsedReading, *, message_id: str | None
    ) -> ReadingOutcome:
        self.readings.append(reading)
        return self.outcome


class SpyRephraser:
    """Records every call; never used for red flags or readings."""

    def __init__(self, answer: str | None = "Rephrased answer.") -> None:
        self.calls: list[str] = []
        self.answer = answer

    def rephrase(self, question: str, entries: Sequence[KbDoc]) -> Rephrased | None:
        self.calls.append(question)
        if self.answer is None:
            return None
        return Rephrased(text=self.answer, model="claude-opus-5-5")


def engine(
    store: FakeStore, rephraser: SpyRephraser | None = None, cache: KbIndexCache | None = None
) -> ChatEngine:
    return ChatEngine(
        store, cache or KbIndexCache(), min_similarity=MIN_SIMILARITY, rephraser=rephraser
    )


def ask(store: FakeStore, text: str, rephraser: SpyRephraser | None = None, **kw: object):  # type: ignore[no-untyped-def]
    return engine(store, rephraser).handle(ChatRequest(user_id=USER, text=text, **kw))  # type: ignore[arg-type]


# --- (a) red flags -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "flag"),
    [
        ("I have chest pain", "chest_pain"),
        ("my chest feels tight", "chest_pain"),
        ("I can't breathe", "breathing"),
        ("I can" + chr(0x2019) + "t breathe properly", "breathing"),  # curly apostrophe
        ("short of breath since morning", "breathing"),
        ("feeling very confused", "confusion"),
        ("my father fainted", "consciousness"),
        ("she passed out after lunch", "consciousness"),
        ("he is unconscious", "consciousness"),
        ("I want to kill myself", "self_harm"),
        ("thinking about suicide", "self_harm"),
        ("fasting 45", "very_low"),
        ("sugar 48 after lunch", "very_low"),
        ("my glucose is 320", "very_high"),
        ("380", "very_high"),
    ],
)
def test_red_flags_get_the_emergency_template_and_never_the_llm(message: str, flag: str) -> None:
    store, spy = FakeStore(), SpyRephraser()
    out = ask(store, message, spy)
    assert detect_red_flag(message) == flag
    assert out.intent == "symptom_red_flag"
    assert out.red_flag == flag
    assert out.escalated and out.urgent and out.notify_practitioner
    assert out.model == "rules"
    assert spy.calls == []  # never sent to an LLM
    assert store.opened == ["red_flag"]
    assert out.handoff is not None and out.handoff_created
    assert EMERGENCY_NUMBER in out.reply_text
    assert "has been notified" in out.reply_text


def test_self_harm_template_gives_the_helpline() -> None:
    out = ask(FakeStore(), "I don't want to live anymore")
    assert MENTAL_HEALTH_HELPLINE in out.reply_text


def test_red_flag_without_practitioner_says_to_call_directly() -> None:
    out = ask(FakeStore(practitioner=False), "chest pain")
    assert "No practitioner is linked" in out.reply_text


def test_red_flag_reading_is_still_stored() -> None:
    store = FakeStore()
    out = ask(store, "fasting 45")
    assert store.readings == [ParsedReading(value_mg_dl=Decimal(45), context="fasting")]
    assert out.reading is store.outcome
    assert "45 mg/dL is very low" in out.reply_text


def test_red_flag_inside_a_live_handoff_reuses_it() -> None:
    store = FakeStore()
    ask(store, "talk to a doctor")
    out = ask(store, "now I have chest pain")
    assert store.opened == ["practitioner_request"]  # no second handoff
    assert out.handoff == store.live and not out.handoff_created
    assert out.notify_practitioner and out.intent == "symptom_red_flag"


@pytest.mark.parametrize(
    "message",
    [
        "my chest x-ray was fine",
        "fasting 132",
        "I passed my driving test",
        "breathing exercises help",
    ],
)
def test_ordinary_messages_are_not_red_flags(message: str) -> None:
    assert detect_red_flag(message) is None


# --- (b) reading reports -------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "value", "context"),
    [
        ("fasting 132", "132", "fasting"),
        ("132 after food", "132", "post_meal"),
        ("sugar after lunch 190", "190", "post_meal"),
        ("Fasting sugar 98 mg/dl", "98", "fasting"),
        ("before breakfast 101", "101", "fasting"),
        ("pp 210", "210", "post_meal"),
        ("my sugar is 150", "150", "random"),
        ("145", "145", "random"),
        ("126.5 fasting", "126.5", "fasting"),
        # Whisper transcripts end with punctuation.
        ("My fasting sugar is 132.", "132", "fasting"),
        ("140.", "140", "random"),
        ("Sugar after lunch, 190!", "190", "post_meal"),
    ],
)
def test_parse_reading(message: str, value: str, context: str) -> None:
    assert parse_reading(message) == ParsedReading.model_validate(
        {"value_mg_dl": Decimal(value), "context": context}
    )


@pytest.mark.parametrize(
    "message",
    [
        "I walked for 30 minutes",
        "is fasting 132 high?",
        "what is a normal fasting level",
        "fasting 7.2 mmol",
        "fasting 132 and after lunch 190",
        "I weigh 70 kg",
        "how do I check my sugar",
    ],
)
def test_not_a_reading_report(message: str) -> None:
    assert parse_reading(message) is None


def test_fasting_and_after_food_contexts_differ() -> None:
    fasting = parse_reading("fasting 132")
    after = parse_reading("132 after food")
    assert fasting is not None and after is not None
    assert (fasting.context, after.context) == ("fasting", "post_meal")
    assert fasting.value_mg_dl == after.value_mg_dl == Decimal(132)


def test_reading_report_is_stored_and_answered_with_its_status() -> None:
    store, spy = FakeStore(), SpyRephraser()
    out = ask(store, "fasting 128", spy)
    assert out.intent == "reading_report"
    assert store.readings == [ParsedReading(value_mg_dl=Decimal(128), context="fasting")]
    assert "Saved your fasting reading: 128 mg/dL" in out.reply_text
    assert "close to your personal upper limit of 130 mg/dL" in out.reply_text
    assert "advisory" in out.reply_text
    assert spy.calls == [] and out.model == "rules"
    assert not out.notify_practitioner  # no live handoff


def test_reading_inside_a_live_handoff_is_shown_to_the_practitioner() -> None:
    store = FakeStore()
    ask(store, "talk to a doctor")
    out = ask(store, "fasting 128")
    assert out.intent == "reading_report"
    assert out.handoff == store.live and out.notify_practitioner


def test_reading_reply_variants() -> None:
    reading = ParsedReading(value_mg_dl=Decimal(110), context="fasting")
    rid = uuid.uuid4()
    normal = ReadingOutcome(
        reading_id=rid, state="normal", upper_mg_dl=Decimal("130.0"), low_mg_dl=Decimal("72.0")
    )
    assert "within your personal range (lower limit 72, upper limit 130 mg/dL)" in reading_reply(
        reading, normal
    )
    unknown = reading_reply(reading, ReadingOutcome(reading_id=rid, state=None))
    assert "not set up yet" in unknown
    breach = ReadingOutcome(
        reading_id=rid,
        state="breach_high",
        threshold_mg_dl=Decimal("130.0"),
        advisory_queued=False,
        escalated=True,
    )
    text = reading_reply(reading, breach)
    assert "at or above your personal upper limit of 130 mg/dL" in text
    assert "same alert recently" in text  # cooldown: no new advisory
    assert "flagged for your care team" in text
    random = ParsedReading(value_mg_dl=Decimal(110), context="random")
    assert "fasting or after a meal" in reading_reply(random, normal)


# --- (c) practitioner requests and (d) relay -------------------------------------------


@pytest.mark.parametrize(
    "message",
    ["talk to a doctor", "Can I speak with my doctor?", "I need a doctor", "connect me to a nurse"],
)
def test_practitioner_request_detection(message: str) -> None:
    assert is_practitioner_request(message)


def test_practitioner_request_opens_one_handoff_and_notifies() -> None:
    store, spy = FakeStore(), SpyRephraser()
    first = ask(store, "Can I talk to a doctor?", spy)
    assert first.intent == "practitioner_request"
    assert first.handoff_created and first.notify_practitioner and first.escalated
    assert "A practitioner has been notified" in first.reply_text
    second = ask(store, "talk to a doctor please")
    assert not second.handoff_created
    assert "already been notified" in second.reply_text
    assert store.opened == ["practitioner_request"]  # uniqueness: one live handoff
    assert spy.calls == []


def test_button_request_works_with_no_text() -> None:
    store = FakeStore()
    out = ask(store, "", practitioner_requested=True, advisory_id=uuid.uuid4())
    assert out.intent == "practitioner_request" and out.handoff_created


def test_practitioner_request_without_assigned_practitioner() -> None:
    out = ask(FakeStore(practitioner=False), "talk to a doctor")
    assert "No practitioner is linked" in out.reply_text
    assert not out.notify_practitioner


def test_messages_are_relayed_while_a_handoff_is_live() -> None:
    store, spy = FakeStore(), SpyRephraser()
    ask(store, "talk to a doctor")
    out = ask(store, "what is the 15-15 rule", spy)
    assert out.reply_text == RELAY_REPLY
    assert out.model == "handoff_relay" and out.notify_practitioner
    assert out.handoff == store.live
    assert spy.calls == []


# --- (e) questions -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "key"),
    [
        ("what is the 15-15 rule", "rule-15-15"),
        ("what are symptoms of hypoglycemia", "low-glucose-signs"),
        ("signs of high blood sugar", "high-glucose-signs"),
        ("how do I take care of my feet", "foot-care"),
        ("how much exercise should I do", "physical-activity"),
        ("when should I see a doctor", "when-to-see-doctor"),
        ("what does pre-breach mean", "pre-breach-alert"),
    ],
)
def test_questions_are_answered_from_the_matching_entry(question: str, key: str) -> None:
    out = ask(FakeStore(), question)
    entry = doc(key)
    assert out.intent == "question"
    assert out.kb_ids_used == [entry.id]
    assert out.reply_text.startswith(entry.body)
    assert f"Source: {entry.title}" in out.reply_text
    assert out.model == "tfidf" and out.model_version.startswith("sklearn-")
    assert out.kb_version == 1
    assert out.retrieval_confidence is not None
    assert out.retrieval_confidence >= MIN_SIMILARITY


def test_15_15_answer_cites_ada() -> None:
    out = ask(FakeStore(), "how does the 15-15 rule work")
    assert "ADA" in out.reply_text or "American Diabetes Association" in out.reply_text


@pytest.mark.parametrize(
    "question",
    [
        "what is the capital of France",
        "who won the cricket match",
        "tell me a joke",
        "how much insulin should I take",
        "is metformin safe",
    ],
)
def test_low_similarity_says_i_dont_know_and_offers_a_practitioner(question: str) -> None:
    spy = SpyRephraser()
    out = ask(FakeStore(), question, spy)
    assert out.reply_text == UNKNOWN_REPLY
    assert "talk to a doctor" in out.reply_text.lower()
    assert out.kb_ids_used == []
    assert out.retrieval_confidence is not None and out.retrieval_confidence < MIN_SIMILARITY
    assert spy.calls == []  # nothing to rephrase


def test_greeting_gets_help_text() -> None:
    out = ask(FakeStore(), "hello")
    assert out.reply_text == GREETING_REPLY and out.intent == "other"


def test_rephraser_output_is_used_and_audited() -> None:
    spy = SpyRephraser("Have 15 grams of fast sugar, then check again after 15 minutes.")
    out = ask(FakeStore(), "what is the 15-15 rule", spy)
    assert spy.calls == ["what is the 15-15 rule"]
    assert out.reply_text.startswith("Have 15 grams")
    assert "Source: Treating a low: the 15-15 rule" in out.reply_text
    assert out.model == out.model_version == "claude-opus-5-5"
    assert doc("rule-15-15").id in out.kb_ids_used


def test_rephraser_declining_falls_back_to_the_entry_text() -> None:
    out = ask(FakeStore(), "what is the 15-15 rule", SpyRephraser(answer=None))
    assert out.reply_text.startswith(doc("rule-15-15").body)
    assert out.model == "tfidf"


def test_kb_index_is_rebuilt_only_when_the_kb_version_changes() -> None:
    store, cache = FakeStore(), KbIndexCache()
    for _ in range(3):
        engine(store, cache=cache).handle(ChatRequest(user_id=USER, text="what is a1c"))
    assert store.kb_loads == 1
    store.version = KbVersion(kb_version=2, entries=len(DOCS), last_change=None)
    engine(store, cache=cache).handle(ChatRequest(user_id=USER, text="what is a1c"))
    assert store.kb_loads == 2


def test_empty_kb_answers_i_dont_know() -> None:
    store = FakeStore()
    store.approved_kb_entries = lambda: []  # type: ignore[method-assign]
    assert ask(store, "what is a1c").reply_text == UNKNOWN_REPLY


def test_empty_message_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        ask(FakeStore(), "   ")


def test_latency_is_measured() -> None:
    ticks = iter([10.0, 10.25])
    eng = ChatEngine(FakeStore(), KbIndexCache(), min_similarity=0.15, clock=lambda: next(ticks))
    assert eng.handle(ChatRequest(user_id=USER, text="hello")).latency_ms == 250


# --- helpers ----------------------------------------------------------------------------


def test_numbers_supported() -> None:
    entries = [doc("rule-15-15")]
    assert numbers_supported("Have 15 grams and check after 15 minutes.", entries)
    assert not numbers_supported("Take 20 units of insulin.", entries)


def test_canonical_text_folds_patient_words() -> None:
    assert canonical_text("Low sugar symptoms in my feet") == "hypoglycaemia low signs in my foot"


def test_every_seed_entry_has_a_source() -> None:
    assert len(KB_SEED) >= 15
    assert all(e.source for e in KB_SEED)
    rule = next(e for e in KB_SEED if e.key == "rule-15-15")
    assert "ADA" in rule.source and "15-15" in rule.source
