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
    ADVICE_GUARD_REPLY,
    EMERGENCY_NUMBER,
    GREETING_REPLY,
    HYPERGLYCAEMIC_CRISIS_SIGNS,
    MENTAL_HEALTH_HELPLINE,
    RED_FLAG_LEVELS,
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
    is_medical_advice_request,
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
            # Like SqlChatStore: an open handoff follows the *current* practitioner.
            if self.live.status == "open" and self.live.practitioner_id != self.practitioner_id:
                just_linked = self.live.practitioner_id is None
                self.live = HandoffInfo(
                    self.live.id,
                    "open",
                    self.live.trigger,
                    self.practitioner_id,
                    practitioner_just_linked=just_linked and self.practitioner_id is not None,
                )
                return self.live, False
            self.live = HandoffInfo(
                self.live.id, self.live.status, self.live.trigger, self.live.practitioner_id
            )
            return self.live, False
        self.opened.append(trigger)
        self.live = HandoffInfo(uuid.uuid4(), "open", trigger, self.practitioner_id)
        return self.live, True

    def claim(self) -> None:
        """A practitioner claims the live handoff (``open -> assigned``)."""
        assert self.live is not None
        self.live = HandoffInfo(self.live.id, "assigned", self.live.trigger, self.practitioner_id)

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


def first_sentence(text: str) -> str:
    return text.split(". ", 1)[0]


EMERGENCY_CASES = [
    ("I have chest pain", "chest_pain"),
    ("my chest feels tight", "chest_pain"),
    ("I can't breathe", "breathing"),
    ("I can" + chr(0x2019) + "t breathe properly", "breathing"),  # curly apostrophe
    ("short of breath since morning", "breathing"),
    ("feeling very confused", "confusion"),
    ("my father fainted", "consciousness"),
    ("she passed out after lunch", "consciousness"),
    ("he is unconscious", "consciousness"),
    ("he had a seizure", "consciousness"),
    ("I want to kill myself", "self_harm"),
    ("thinking about suicide", "self_harm"),
    ("260 and vomiting", "high_with_symptoms"),
    ("sugar 300, breathing fast", "high_with_symptoms"),
    ("my glucose is 320 and I feel confused", "high_with_symptoms"),
    ("threw up twice, reading 410", "high_with_symptoms"),
    ("sugar 290 and very drowsy", "high_with_symptoms"),
    ("glucose 310 with stomach pain", "high_with_symptoms"),
    # Found in testing (Nov 2026): self-harm phrasings that were missed.
    ("i feel like killing myself", "self_harm"),
    ("thinking of ending my life", "self_harm"),
    ("no reason to live", "self_harm"),
    ("i don't want to live", "self_harm"),
    ("end it all", "self_harm"),
    ("I just want to end it all", "self_harm"),
    ("everyone would be better off dead without me", "self_harm"),
    ("I have been hurting myself", "self_harm"),
    ("nothing left to live for", "self_harm"),
    # Found in testing: emergency symptoms that were missed.
    ("breathing problem", "breathing"),
    ("I have a problem breathing", "breathing"),
    ("heart pain", "chest_pain"),
    ("pain in my chest", "chest_pain"),
    # Drowsiness counts as confusion when it describes someone's state now.
    ("feeling very drowsy", "confusion"),
    ("I feel very drowsy", "confusion"),
    ("she is drowsy and confused", "confusion"),
    ("my father is very drowsy since morning", "confusion"),
]
URGENT_CASES = [
    ("fasting 45", "very_low"),
    ("sugar 48 after lunch", "very_low"),
    ("260", "very_high"),
    ("my glucose is 320", "very_high"),
    ("380", "very_high"),
    # "random" marks a reading report (found in testing).
    ("random 300", "very_high"),
    ("300 random", "very_high"),
]


@pytest.mark.parametrize(("message", "flag"), EMERGENCY_CASES + URGENT_CASES)
def test_red_flags_hand_off_at_once_and_never_reach_the_llm(message: str, flag: str) -> None:
    store, spy = FakeStore(), SpyRephraser()
    out = ask(store, message, spy)
    assert detect_red_flag(message) == flag
    assert out.intent == "symptom_red_flag"
    assert out.red_flag == flag
    assert out.red_flag_level == RED_FLAG_LEVELS[flag]
    # Both levels: immediate handoff, high-priority practitioner push, fixed text.
    assert out.escalated and out.urgent and out.notify_practitioner
    assert out.model == "rules"
    assert spy.calls == []  # never sent to an LLM
    assert store.opened == ["red_flag"]
    assert out.handoff is not None and out.handoff_created
    assert EMERGENCY_NUMBER in out.reply_text
    assert "has been notified" in out.reply_text


@pytest.mark.parametrize(("message", "flag"), [c for c in EMERGENCY_CASES if c[1] != "self_harm"])
def test_emergency_replies_lead_with_112(message: str, flag: str) -> None:
    out = ask(FakeStore(), message)
    assert out.red_flag_level == "emergency"
    assert first_sentence(out.reply_text).startswith(f"Call {EMERGENCY_NUMBER} now")


@pytest.mark.parametrize(("message", "flag"), URGENT_CASES)
def test_urgent_replies_keep_112_out_of_the_first_line(message: str, flag: str) -> None:
    out = ask(FakeStore(), message)
    assert out.red_flag_level == "urgent"
    assert EMERGENCY_NUMBER not in first_sentence(out.reply_text)
    assert EMERGENCY_NUMBER in out.reply_text  # still there, for when it gets worse


def test_260_alone_is_urgent_but_260_and_vomiting_is_emergency() -> None:
    assert detect_red_flag("260") == "very_high"
    assert RED_FLAG_LEVELS["very_high"] == "urgent"
    assert detect_red_flag("260 and vomiting") == "high_with_symptoms"
    assert RED_FLAG_LEVELS["high_with_symptoms"] == "emergency"


def test_very_high_alone_gives_ketone_and_care_team_guidance() -> None:
    reply = ask(FakeStore(), "260").reply_text
    assert reply.startswith("Your glucose of 260 mg/dL is very high.")
    # ADA-6 Section 6 text after Rec 6.22: ketones with symptoms or likely triggers,
    # especially above 200 mg/dL; then that text's crisis signs.
    assert "Check your ketones" in reply
    assert "symptoms or a likely trigger" in reply and "above 200 mg/dL" in reply
    assert "care team today" in reply
    assert f"call {EMERGENCY_NUMBER}) if you notice signs of a crisis: " in reply
    assert reply.count(HYPERGLYCAEMIC_CRISIS_SIGNS) == 1


def test_crisis_signs_are_the_ada_section_6_list() -> None:
    assert HYPERGLYCAEMIC_CRISIS_SIGNS == (
        "passing a lot of urine, intense thirst, weight loss, vomiting, dehydration, "
        "confusion or drowsiness"
    )


def test_high_with_symptoms_names_the_emergency_symptoms() -> None:
    reply = ask(FakeStore(), "260 and vomiting").reply_text
    assert "260 mg/dL with symptoms like vomiting, confusion, drowsiness" in reply
    assert "ketoacidosis" in reply


def test_dehydration_with_a_very_high_reading_is_an_emergency() -> None:
    assert detect_red_flag("sugar 320 and I am dehydrated") == "high_with_symptoms"


def test_very_low_gives_ada_treatment_then_when_to_call_112() -> None:
    reply = ask(FakeStore(), "fasting 45").reply_text
    assert reply.startswith("Your glucose of 45 mg/dL is very low.")
    # ADA-6 Rec 6.15 + Section 6 text: 15 g, glucose preferred, recheck at 15 min,
    # repeat; meal or snack once above 70 (ADA 15-15 rule page).
    assert "treat it now with 15 grams of fast-acting carbohydrate." in reply
    assert "Glucose tablets or gel are best" in reply
    assert "Check again in 15 minutes and repeat if it is still low." in reply
    assert "Once you are back above 70 mg/dL, eat a meal or snack." in reply
    assert "15 to 20" not in reply and "about 15" not in reply
    assert f"Call {EMERGENCY_NUMBER} if you cannot treat it" in reply
    assert "confused" in reply


def test_high_with_symptoms_is_stored_as_a_reading() -> None:
    store = FakeStore()
    out = ask(store, "260 and vomiting")
    assert store.readings == [ParsedReading(value_mg_dl=Decimal(260), context="random")]
    assert out.reading is store.outcome
    assert "260 mg/dL" in out.reply_text and "ketoacidosis" in out.reply_text


@pytest.mark.parametrize(
    "message",
    ["vomiting since this morning", "180 and vomiting", "I vomited 2 times"],
)
def test_vomiting_without_a_very_high_reading_is_not_a_red_flag(message: str) -> None:
    assert detect_red_flag(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "I don't want to live anymore",
        "i feel like killing myself",
        "thinking of ending my life",
        "no reason to live",
        "end it all",
    ],
)
def test_self_harm_template_gives_the_helpline(message: str) -> None:
    out = ask(FakeStore(), message)
    assert out.red_flag == "self_harm"
    assert MENTAL_HEALTH_HELPLINE in out.reply_text
    assert "Tele-MANAS on 14416" in out.reply_text


def test_red_flag_without_practitioner_says_to_call_directly() -> None:
    out = ask(FakeStore(practitioner=False), "chest pain")
    assert "No practitioner is linked" in out.reply_text


def test_red_flag_reading_is_still_stored() -> None:
    store = FakeStore()
    out = ask(store, "fasting 45")
    assert store.readings == [ParsedReading(value_mg_dl=Decimal(45), context="fasting")]
    assert out.reading is store.outcome
    assert "45 mg/dL is very low" in out.reply_text
    assert out.red_flag_level == "urgent"


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
        # Everyday phrases the broader self-harm list must not catch.
        "my feet are killing me",
        # Questions about drowsiness are not a report of it.
        "does metformin make you drowsy",
        "can low sugar make you drowsy?",
        "can I take it all at once",
        "I cut myself while chopping vegetables",
        "random 180",
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
        # "random" is a reading keyword (found in testing).
        ("random 300", "300", "random"),
        ("random 180", "180", "random"),
        ("300 random", "300", "random"),
        ("RBS 210", "210", "random"),
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
        # Medicine doses are not glucose readings (found while adding the dosing guard).
        "I take 500 mg metformin",
        "metformin 500 mg twice a day",
        "500 mg of metformin",
    ],
)
def test_not_a_reading_report(message: str) -> None:
    assert parse_reading(message) is None


def test_random_300_is_stored_and_flagged_very_high() -> None:
    store = FakeStore()
    out = ask(store, "random 300")
    assert store.readings == [ParsedReading(value_mg_dl=Decimal(300), context="random")]
    assert out.reading is store.outcome
    assert out.red_flag == "very_high" and out.red_flag_level == "urgent"
    assert out.reply_text.startswith("Your glucose of 300 mg/dL is very high.")


def test_random_180_is_stored_as_a_random_reading() -> None:
    store = FakeStore()
    out = ask(store, "random 180")
    assert out.intent == "reading_report"
    assert store.readings == [ParsedReading(value_mg_dl=Decimal(180), context="random")]


def test_a_metformin_dose_is_not_a_very_high_reading() -> None:
    assert detect_red_flag("I take 500 mg metformin, is that ok") is None
    assert parse_reading("my sugar is 132 mg") == ParsedReading(
        value_mg_dl=Decimal(132), context="random"
    )


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


def test_no_relay_before_claim_the_chat_keeps_answering() -> None:
    store, spy = FakeStore(), SpyRephraser()
    ask(store, "talk to a doctor")
    assert store.live is not None and store.live.status == "open"
    out = ask(store, "what is the 15-15 rule", spy)
    # Answered from the KB (and rephrased), not relayed.
    assert spy.calls == ["what is the 15-15 rule"]
    assert "Source: Treating low glucose (hypoglycaemia): the 15-15 rule" in out.reply_text
    assert doc("rule-15-15").id in out.kb_ids_used
    assert out.intent == "question" and not out.relayed
    # Attached to the open handoff so the practitioner sees it (refresh, no push).
    assert out.handoff == store.live and out.notify_practitioner


def test_relay_after_claim() -> None:
    store, spy = FakeStore(), SpyRephraser()
    ask(store, "talk to a doctor")
    store.claim()
    out = ask(store, "what is the 15-15 rule", spy)
    assert out.reply_text == RELAY_REPLY
    assert out.model == "handoff_relay" and out.notify_practitioner and out.relayed
    assert out.handoff == store.live
    assert spy.calls == []


def test_red_flags_and_readings_are_processed_during_a_claimed_handoff() -> None:
    store = FakeStore()
    ask(store, "talk to a doctor")
    store.claim()
    flag = ask(store, "I have chest pain")
    assert flag.intent == "symptom_red_flag" and flag.reply_text.startswith("Call 112 now")
    assert flag.handoff == store.live and not flag.handoff_created
    reading = ask(store, "fasting 132")
    assert reading.intent == "reading_report"
    assert store.readings[-1] == ParsedReading(value_mg_dl=Decimal(132), context="fasting")
    assert reading.reading is store.outcome and reading.relayed


def test_talk_to_a_doctor_checks_the_current_assignment() -> None:
    # Opened while nobody was linked...
    store = FakeStore(practitioner=False)
    first = ask(store, "talk to a doctor")
    assert "No practitioner is linked" in first.reply_text and not first.notify_practitioner
    # ...then a practitioner is linked: the next request routes the same handoff.
    store.practitioner_id = uuid.uuid4()
    second = ask(store, "talk to a doctor")
    assert store.opened == ["practitioner_request"]  # same handoff, not a new one
    assert second.handoff is not None
    assert second.handoff.practitioner_id == store.practitioner_id
    assert second.handoff.practitioner_just_linked and second.notify_practitioner
    assert "A practitioner has been notified" in second.reply_text


def test_talk_to_a_doctor_after_claim_says_the_practitioner_is_here() -> None:
    store = FakeStore()
    ask(store, "talk to a doctor")
    store.claim()
    out = ask(store, "talk to a doctor")
    assert "already in this chat" in out.reply_text


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
        # Found in testing (Nov 2026): these went to the wrong entry or "don't know".
        ("what should i do if my sugar is low", "rule-15-15"),
        ("how do i treat a hypo", "rule-15-15"),
        ("is rice okay for diabetics", "healthy-eating"),
        ("can I eat roti or chapati", "healthy-eating"),
        ("is jaggery better than sugar", "healthy-eating"),
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


GUARDED = [
    "how much insulin should I take",
    "is metformin safe",
    "can I stop taking metformin",
    "should I increase my insulin",
    "should i double my dose if i miss one",
    "can i reduce my glimepiride if my sugar is normal",
    "I take 500 mg metformin, is that ok",
    "do I have type 1 or type 2",
    "do i have type 1 or type 2 diabetes",
    "am I diabetic",
    "am i pre-diabetic",
    "is my diabetes type 1",
    "can you diagnose my kidney problem",
]


@pytest.mark.parametrize("question", GUARDED)
def test_dosing_and_self_diagnosis_get_the_fixed_practitioner_reply(question: str) -> None:
    store, spy = FakeStore(), SpyRephraser()
    out = ask(store, question, spy)
    assert is_medical_advice_request(question)
    assert out.reply_text == ADVICE_GUARD_REPLY
    assert "ask your practitioner" in out.reply_text
    assert "talk to a doctor" in out.reply_text.lower()  # the connect offer
    assert (out.intent, out.model) == ("question", "rules")
    assert out.kb_ids_used == [] and out.retrieval_confidence is None
    assert spy.calls == []  # never sent to an LLM
    assert store.kb_loads == 0  # decided before retrieval
    assert store.opened == []  # an offer, not a handoff


@pytest.mark.parametrize(
    "question",
    [
        "what should i do if my sugar is low",
        "should i take 15 grams of sugar when i am low",
        "how much exercise should i do",
        "can i drink alcohol",
        "what is prediabetes",
        "is it safe to drive if my sugar is low",
        "my doctor said i have borderline diabetes",
    ],
)
def test_ordinary_questions_are_not_guarded(question: str) -> None:
    assert not is_medical_advice_request(question)


def test_guard_does_not_override_a_red_flag() -> None:
    out = ask(FakeStore(), "I took too much insulin and I feel very drowsy")
    assert out.intent == "symptom_red_flag"


def test_greeting_gets_help_text() -> None:
    out = ask(FakeStore(), "hello")
    assert out.reply_text == GREETING_REPLY and out.intent == "other"


def test_rephraser_output_is_used_and_audited() -> None:
    spy = SpyRephraser("Have 15 grams of fast sugar, then check again after 15 minutes.")
    out = ask(FakeStore(), "what is the 15-15 rule", spy)
    assert spy.calls == ["what is the 15-15 rule"]
    assert out.reply_text.startswith("Have 15 grams")
    assert "Source: Treating low glucose (hypoglycaemia): the 15-15 rule" in out.reply_text
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
    assert "Recommendation 6.15" in rule.source
    assert "Glucose is the preferred treatment for the conscious individual" in rule.source
    assert "For most individuals, 15 g carbohydrates should be ingested" in rule.source
    assert "'The 15-15 Rule', diabetes.org" in rule.source


def test_15_15_entry_matches_ada_rec_6_15() -> None:
    body = next(e for e in KB_SEED if e.key == "rule-15-15").body
    assert "have 15 grams of fast-acting carbohydrate" in body
    assert "Glucose tablets or gel are best" in body
    assert "Check again after 15 minutes" in body and "repeat" in body
    assert "Once you are back above 70 mg/dL, eat a meal or snack" in body
    assert "15 to 20" not in body and "about 15" not in body


def test_healthy_eating_entry_covers_indian_foods_with_a_source() -> None:
    entry = next(e for e in KB_SEED if e.key == "healthy-eating")
    for food in ("rice", "roti", "chapati", "idli", "dosa", "jaggery", "mango", "fruit"):
        assert food in entry.body.lower()
    assert "Dietary Guidelines for Indians" in entry.source
