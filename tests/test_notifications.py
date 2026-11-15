"""Tests for app.services.notifications: guard, payload, urgency and expiry (R3)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.exceptions import RecipientBlockedError
from app.db.models import User
from app.services.notifications import (
    DeliveryReport,
    Notification,
    Recipient,
    check_recipient,
    is_expired,
    urgency_for,
)

REQUIRED: dict[str, Any] = {
    "supabase_url": "https://abc.supabase.co",
    "supabase_service_role_key": "srk",
    "database_url": "postgresql://u:p@localhost:5432/db",
}
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def recipient(*, is_demo: bool, is_linked: bool) -> Recipient:
    return Recipient(user_id=uuid.uuid4(), is_demo=is_demo, is_linked=is_linked)


def test_guard_blocks_unlinked_demo_patient() -> None:
    with pytest.raises(RecipientBlockedError, match="not linked"):
        check_recipient(recipient(is_demo=True, is_linked=False))


@pytest.mark.parametrize(("is_demo", "is_linked"), [(True, True), (False, True), (False, False)])
def test_guard_allows_linked_demo_and_real_patients(is_demo: bool, is_linked: bool) -> None:
    check_recipient(recipient(is_demo=is_demo, is_linked=is_linked))


def test_recipient_from_user_row() -> None:
    user = User(id=uuid.uuid4(), role="patient", is_demo=True, auth_user_id=uuid.uuid4())
    assert Recipient.for_user(user) == Recipient(user_id=user.id, is_demo=True, is_linked=True)
    user.auth_user_id = None
    assert Recipient.for_user(user).is_linked is False


def test_payload_is_small_json_with_only_the_four_fields() -> None:
    advisory_id = uuid.uuid4()
    note = Notification(
        title="Glucose alert",
        body="Fasting 128 mg/dL, close to your limit of 130 mg/dL.",
        url=f"/app/?advisory={advisory_id}",
        advisory_id=advisory_id,
    )
    assert json.loads(note.payload()) == {
        "advisory_id": str(advisory_id),
        "title": "Glucose alert",
        "body": "Fasting 128 mg/dL, close to your limit of 130 mg/dL.",
        "url": f"/app/?advisory={advisory_id}",
    }
    assert (note.ttl_seconds, note.urgency) == (3600, "normal")


@pytest.mark.parametrize("url", ["https://evil.example/app", "//evil.example/app", "app/"])
def test_notification_url_must_be_same_origin_path(url: str) -> None:
    with pytest.raises(ValidationError, match="same-origin"):
        Notification(title="t", body="b", url=url)


@pytest.mark.parametrize(
    ("field", "value"), [("title", "x" * 81), ("body", "x" * 241), ("ttl_seconds", 86401)]
)
def test_notification_limits(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Notification.model_validate({"title": "t", "body": "b", field: value})


@pytest.mark.parametrize(
    ("state", "reading", "expected"),
    [
        ("breach_high", Decimal("260"), "high"),
        ("breach_low", Decimal("65"), "high"),
        ("pre_breach_low", Decimal("53.9"), "high"),  # Level 2 hypoglycaemia
        ("pre_breach_low", Decimal("54.0"), "normal"),  # exactly 54 is not < 54
        ("pre_breach_high", Decimal("125"), "normal"),
    ],
)
def test_urgency(state: Any, reading: Decimal, expected: str) -> None:
    assert urgency_for(state, reading) == expected


@pytest.mark.parametrize(
    ("age", "expired"),
    [
        (timedelta(minutes=59), False),
        (timedelta(minutes=60), False),  # exactly the limit is still fresh
        (timedelta(minutes=60, seconds=1), True),
    ],
)
def test_expiry_boundary(age: timedelta, expired: bool) -> None:
    settings = Settings(_env_file=None, **REQUIRED)
    assert is_expired(NOW - age, NOW, settings) is expired


def test_expiry_follows_setting() -> None:
    settings = Settings(_env_file=None, **REQUIRED, advisory_max_age_minutes=15)
    assert is_expired(NOW - timedelta(minutes=16), NOW, settings)


def test_delivery_report_delivered() -> None:
    report = DeliveryReport(channel="webpush")
    assert not report.delivered
    report.sent = 1
    assert report.delivered
