"""Tests for app.core.logging: phone masking and the masked handler."""

from __future__ import annotations

import io
import logging

import pytest

from app.core.logging import (
    PhoneMaskingFormatter,
    configure_logging,
    mask_phone,
    mask_phone_numbers,
)


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("+919876543210", "+91******3210"),  # India E.164, 12 digits
        ("919876543210", "91******3210"),  # bare digits, no "+"
        ("+14155552671", "+14*****2671"),  # US E.164, 11 digits
        ("+1 (415) 555-2671", "+14*****2671"),  # separators dropped
        ("9876543210", "98****3210"),  # exactly 10 digits
        ("123456789", "*******89"),  # 9 digits: short form, last 2 only
        ("12345", "***45"),
        ("1234", "****"),  # 4 or fewer: fully masked
        ("", ""),
        ("n/a", "n/a"),  # no digits: unchanged
    ],
)
def test_mask_phone(raw: str, masked: str) -> None:
    assert mask_phone(raw) == masked


@pytest.mark.parametrize(
    "number",
    ["+919876543210", "919876543210", "+91 98765 43210", "+1 (415) 555-2671", "+44 20 7946 0958"],
)
def test_mask_phone_numbers_hides_middle_digits(number: str) -> None:
    out = mask_phone_numbers(f"sent advisory to {number} ok")
    assert number not in out
    assert "*" in out
    assert out.startswith("sent advisory to ")
    assert out.endswith(" ok")


@pytest.mark.parametrize(
    "text",
    [
        "fasting reading 126 mg/dL above limit 130.0",
        "run on 2026-11-15 at 02:30:00",
        "probability 0.1234567890123",
        # Realistic UUID (hex letters). A segment that happens to be 10+ pure
        # digits *is* masked - accepted over-masking, see _PHONE_PATTERN.
        "user_id=550e8400-e29b-41d4-a716-44665544a0f0",
        "phone +12345",  # too short to be a phone number
    ],
)
def test_mask_phone_numbers_leaves_non_phone_text(text: str) -> None:
    assert mask_phone_numbers(text) == text


def test_mask_phone_numbers_multiple() -> None:
    out = mask_phone_numbers("from 919876543210 to +14155552671")
    assert out == "from 91******3210 to +14*****2671"


def _capture_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(PhoneMaskingFormatter("%(levelname)s %(message)s"))
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_formatter_masks_percent_args() -> None:
    logger, stream = _capture_logger("test.mask.args")
    logger.info("delivered to %s", "+919876543210")
    assert stream.getvalue().strip() == "INFO delivered to +91******3210"


def test_formatter_masks_tracebacks() -> None:
    logger, stream = _capture_logger("test.mask.exc")
    try:
        raise RuntimeError("send failed for 919876543210")
    except RuntimeError:
        logger.exception("delivery error")
    out = stream.getvalue()
    assert "919876543210" not in out
    assert "91******3210" in out
    assert "Traceback" in out


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    old_level = root.level
    try:
        configure_logging("DEBUG")
        configure_logging("WARNING")
        ours = [h for h in root.handlers if h.get_name() == "diabetes-advisor-masked"]
        assert len(ours) == 1
        assert isinstance(ours[0].formatter, PhoneMaskingFormatter)
        assert root.level == logging.WARNING
        assert all(h in root.handlers for h in before)  # others' handlers kept
    finally:
        root.handlers = before
        root.setLevel(old_level)
