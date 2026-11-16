"""Tests for app.services.chat_llm: optional Claude rephrasing with a fake client (R4)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import httpx2
import pytest

from app.core.config import Settings
from app.services.chat import KbDoc
from app.services.chat_llm import SYSTEM_PROMPT, ClaudeRephraser, build_rephraser

ENTRY = KbDoc(
    id=uuid.uuid4(),
    entry_key="seed:rule-15-15",
    title="Treating a low: the 15-15 rule",
    body="Have 15 grams of fast-acting carbohydrate, then check again after 15 minutes.",
    source_citation="ADA",
    kb_version=1,
)


def settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        supabase_url="https://abc.supabase.co",
        supabase_service_role_key="srk",
        database_url="postgresql://u:p@localhost:5432/db",
        **overrides,
    )


class FakeMessages:
    def __init__(self, response: object = None, exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> object:
        self.kwargs = kwargs
        if self.exc is not None:
            raise self.exc
        return self.response


def rephraser(messages: FakeMessages) -> ClaudeRephraser:
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return ClaudeRephraser("key", "claude-opus-5-5", client=cast(anthropic.Anthropic, client))


def reply(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        model="claude-opus-5-5",
    )


def test_no_api_key_means_no_rephraser() -> None:
    assert build_rephraser(settings()) is None


def test_api_key_builds_a_rephraser() -> None:
    assert isinstance(build_rephraser(settings(anthropic_api_key="sk-test")), ClaudeRephraser)


def test_rephrases_using_only_the_retrieved_entries() -> None:
    fake = FakeMessages(reply("Have 15 grams of quick sugar and check again in 15 minutes."))
    out = rephraser(fake).rephrase("what is the 15-15 rule", [ENTRY])
    assert out is not None
    assert out.text.startswith("Have 15 grams")
    assert out.model == "claude-opus-5-5"
    kw = fake.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["system"] == SYSTEM_PROMPT
    assert kw["fallbacks"] == "default"
    user = kw["messages"][0]["content"]
    assert ENTRY.body in user and "what is the 15-15 rule" in user


def test_system_prompt_forbids_diagnosis_dosing_and_outside_knowledge() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "never diagnose" in lowered
    assert "dose" in lowered and "insulin" in lowered
    assert "only" in lowered and "reference entries" in lowered


@pytest.mark.parametrize(
    "response",
    [
        reply("NO_ANSWER"),
        reply("", stop_reason="end_turn"),
        reply("Sorry.", stop_reason="refusal"),
        # 20 units appears nowhere in the context: an invented dose is discarded.
        reply("Take 20 units of insulin."),
    ],
)
def test_unusable_answers_fall_back(response: SimpleNamespace) -> None:
    assert rephraser(FakeMessages(response)).rephrase("q", [ENTRY]) is None


def test_api_errors_fall_back() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    down = anthropic.APIConnectionError(request=request)
    assert rephraser(FakeMessages(exc=down)).rephrase("q", [ENTRY]) is None
    busy = anthropic.APIStatusError(
        "overloaded", response=httpx2.Response(529, request=request), body=None
    )
    assert rephraser(FakeMessages(exc=busy)).rephrase("q", [ENTRY]) is None


def test_no_entries_means_no_call() -> None:
    fake = FakeMessages(reply("x"))
    assert rephraser(fake).rephrase("q", []) is None
    assert fake.kwargs == {}


def test_rephrasing_is_off_even_with_an_api_key() -> None:
    from app.api.chat import get_rephraser  # noqa: PLC0415

    assert get_rephraser(settings(anthropic_api_key="sk-test")) is None


def test_rephrasing_needs_the_explicit_flag_and_a_key() -> None:
    from app.api.chat import get_rephraser  # noqa: PLC0415

    on = settings(anthropic_api_key="sk-test", chat_llm_rephrase_enabled=True)
    assert isinstance(get_rephraser(on), ClaudeRephraser)
    assert get_rephraser(settings(chat_llm_rephrase_enabled=True)) is None
