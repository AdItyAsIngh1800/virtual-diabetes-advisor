"""Logging setup with phone-number masking.

Phone numbers are PII (E.164 profile numbers, numbers typed into chat). They must
never appear in full in logs, so every record passing through the handler installed
by :func:`configure_logging` is masked *after* formatting. Masking after formatting
covers ``%``-args, f-strings, ``extra`` fields rendered by the format string and
exception tracebacks alike.

Modules still log with ``logging.getLogger(__name__)``; they don't import anything
from here.
"""

from __future__ import annotations

import logging
import re
import sys

# Name of the handler we install, so repeated configure_logging() calls replace it
# instead of stacking duplicate handlers (uvicorn reload, pytest, notebooks).
_HANDLER_NAME = "diabetes-advisor-masked"

DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Two shapes of phone number reach our logs:
#   1. International form with a leading "+", optionally with spaces/dots/dashes/
#      parentheses between digits: "+91 98765 43210", "+1 (415) 555-2671".
#      8-15 digits total; 15 is the E.164 maximum (ITU-T E.164 section 6).
#   2. Bare digit runs without the "+" ("919876543210"), as people often type them:
#      10-15 consecutive digits. 10 is the shortest national number we expect.
# Shape 2 also hits other 10-15 digit IDs (Unix timestamps, numeric ids, the
# rare all-digit UUID segment). We
# accept that over-masking: hiding a harmless ID is better than leaking a number.
_PHONE_PATTERN = re.compile(
    r"(?<![\w+])\+\d(?:[ ().-]{0,2}\d){7,14}(?!\d)"  # +CC ... (8-15 digits)
    r"|(?<![\w.])\d{10,15}(?![\d])"  # bare wa_id-style run (10-15 digits)
)

# How many digits stay visible. Country code + last four is enough to tell users
# apart in a log line without being enough to dial them.
_VISIBLE_HEAD = 2
_VISIBLE_TAIL = 4
# Below this many digits, showing head + tail would reveal most of the number, so
# we fall back to showing only the last two.
_FULL_MASK_MIN_DIGITS = 10
_SHORT_VISIBLE_TAIL = 2
_MASK_CHAR = "*"


def mask_phone(number: str) -> str:
    """Mask the middle digits of a single phone number.

    Separators are dropped and a leading ``+`` is kept. Numbers with at least 10
    digits keep the first 2 and last 4 digits; shorter ones keep only the last 2;
    four digits or fewer are masked completely.

    Args:
        number: A phone number in any common format, e.g. ``"+91 98765 43210"``.

    Returns:
        The masked number, e.g. ``"+91******3210"``. Input with no digits is
        returned unchanged.

    Examples:
        >>> mask_phone("+919876543210")
        '+91******3210'
        >>> mask_phone("14155552671")
        '14*****2671'
    """
    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        return number
    prefix = "+" if number.lstrip().startswith("+") else ""
    n = len(digits)
    if n <= _VISIBLE_TAIL:
        return prefix + _MASK_CHAR * n
    if n < _FULL_MASK_MIN_DIGITS:
        return prefix + _MASK_CHAR * (n - _SHORT_VISIBLE_TAIL) + digits[-_SHORT_VISIBLE_TAIL:]
    hidden = n - _VISIBLE_HEAD - _VISIBLE_TAIL
    return prefix + digits[:_VISIBLE_HEAD] + _MASK_CHAR * hidden + digits[-_VISIBLE_TAIL:]


def mask_phone_numbers(text: str) -> str:
    """Mask every phone-number-like substring in free text.

    Args:
        text: Any string, typically a formatted log line.

    Returns:
        The text with each detected phone number replaced by :func:`mask_phone`.
    """
    return _PHONE_PATTERN.sub(lambda m: mask_phone(m.group(0)), text)


class PhoneMaskingFormatter(logging.Formatter):
    """A :class:`logging.Formatter` that masks phone numbers in the final output."""

    def format(self, record: logging.LogRecord) -> str:
        """Format the record normally, then mask phone numbers.

        Args:
            record: The log record to render.

        Returns:
            The rendered line, including any traceback, with phone numbers masked.
        """
        return mask_phone_numbers(super().format(record))


def configure_logging(level: int | str = logging.INFO, fmt: str = DEFAULT_FORMAT) -> None:
    """Install a masked stderr handler on the root logger.

    Safe to call more than once: the handler installed by a previous call is
    replaced, and handlers installed by others (e.g. pytest's caplog) are kept.

    Args:
        level: Root log level, as a ``logging`` constant or name such as ``"INFO"``.
        fmt: ``%``-style format string for each line.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(PhoneMaskingFormatter(fmt))
    root.addHandler(handler)
    root.setLevel(level)
