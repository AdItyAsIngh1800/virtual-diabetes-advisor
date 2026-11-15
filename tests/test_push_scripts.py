"""Tests for scripts.gen_vapid_keys and scripts.push_test (no network, no database)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from py_vapid import Vapid

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, RecipientBlockedError
from app.services.notifications import DeliveryReport, Notification, Recipient
from scripts import gen_vapid_keys, push_test
from scripts.gen_vapid_keys import generate_vapid_keys, public_key_for

REQUIRED: dict[str, Any] = {
    "supabase_url": "https://abc.supabase.co",
    "supabase_service_role_key": "srk",
    "database_url": "postgresql://u:p@localhost:5432/db",
}


def test_generated_keys_are_accepted_by_settings_and_py_vapid() -> None:
    keys = generate_vapid_keys()
    settings = Settings(
        _env_file=None,
        **REQUIRED,
        vapid_public_key=keys.public_key,
        vapid_private_key=keys.private_key,
        vapid_subject="mailto:team6@example.com",
    )
    assert settings.require_vapid().public_key == keys.public_key
    assert public_key_for(keys.private_key) == keys.public_key
    Vapid.from_string(private_key=keys.private_key)  # what pywebpush will do
    assert keys != generate_vapid_keys()


def test_main_prints_env_lines(capsys: pytest.CaptureFixture[str]) -> None:
    assert gen_vapid_keys.main(["--subject", "mailto:me@example.com"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("=", 1)[0] for line in lines] == [
        "VAPID_PUBLIC_KEY",
        "VAPID_PRIVATE_KEY",
        "VAPID_SUBJECT",
    ]
    assert lines[2] == "VAPID_SUBJECT=mailto:me@example.com"


@pytest.mark.parametrize("matching", [True, False])
def test_check_detects_mismatched_keys(monkeypatch: pytest.MonkeyPatch, matching: bool) -> None:
    keys, other = generate_vapid_keys(), generate_vapid_keys()
    settings = Settings(
        _env_file=None,
        **REQUIRED,
        vapid_public_key=keys.public_key if matching else other.public_key,
        vapid_private_key=keys.private_key,
        vapid_subject="mailto:me@example.com",
    )
    monkeypatch.setattr(gen_vapid_keys, "get_settings", lambda: settings)
    assert gen_vapid_keys.main(["--check"]) == (0 if matching else 1)


def test_check_without_keys_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gen_vapid_keys, "get_settings", lambda: Settings(_env_file=None, **REQUIRED)
    )
    assert gen_vapid_keys.main(["--check"]) == 1


# --- push_test ----------------------------------------------------------------------


ME = Recipient(user_id=uuid.uuid4(), is_demo=True, is_linked=True)


@pytest.fixture
def quiet_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push_test, "get_settings", lambda: Settings(_env_file=None, **REQUIRED))


@pytest.mark.parametrize(("sent", "code"), [(1, 0), (0, 1)])
def test_push_test_exit_code_follows_delivery(
    monkeypatch: pytest.MonkeyPatch, quiet_settings: None, sent: int, code: int
) -> None:
    sent_notes: list[Notification] = []

    def fake_send(recipient: Recipient, note: Notification) -> DeliveryReport:
        sent_notes.append(note)
        return DeliveryReport(channel="webpush", attempted=1, sent=sent, failed=1 - sent)

    monkeypatch.setattr(push_test, "find_recipient", lambda email: ME)
    monkeypatch.setattr(push_test, "send_test", fake_send)
    assert push_test.main(["--urgency", "high", "--title", "Hi"]) == code
    (note,) = sent_notes
    assert (note.title, note.urgency, note.url) == ("Hi", "high", "/app/")


@pytest.mark.parametrize(
    "error",
    [ConfigurationError("no patient is linked"), RecipientBlockedError("not linked")],
)
def test_push_test_reports_setup_problems(
    monkeypatch: pytest.MonkeyPatch, quiet_settings: None, error: Exception
) -> None:
    def fail(_email: str | None) -> Recipient:
        raise error

    monkeypatch.setattr(push_test, "find_recipient", fail)
    assert push_test.main([]) == 1


@pytest.mark.parametrize("subject", ["https://example.com/contact", "you@example.com"])
def test_gen_vapid_keys_rejects_subjects_py_vapid_refuses(subject: str) -> None:
    with pytest.raises(SystemExit):
        gen_vapid_keys.main(["--subject", subject])
