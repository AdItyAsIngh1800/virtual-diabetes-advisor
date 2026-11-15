"""Phone-number rules shared by the app, the send guard and the demo seed script.

Numbers are stored as E.164 (``+`` then 7-15 digits, no separators). WhatsApp sends
and receives the same number *without* the ``+`` (its ``wa_id``); the helpers here
convert between the two.

Demo patients get numbers from the NANP fictional block ``+1 555 0100-0199``. No
WhatsApp message may ever be addressed to one (R3 safety guard); only the patient
that ``seed_demo --my-phone`` gives a real number can receive demo advisories.
"""

from __future__ import annotations

import re
from typing import Final

# Same rule as the profiles.phone_e164 CHECK (0001_core_schema.sql): ITU-T E.164 caps a
# number at 15 digits; 7 is the shortest we accept (small national numbering plans).
E164_PATTERN: Final = r"^\+[1-9][0-9]{6,14}$"
_E164: Final = re.compile(E164_PATTERN)
# Separators people type when copying a number: spaces, dashes, dots, parentheses.
_SEPARATORS: Final = re.compile(r"[\s\-().]")

# Fake numbers: +1 555 0100 ... +1 555 0199 (NANP reserves 555-0100..0199 for fiction).
DEMO_PHONE_PREFIX: Final = "+1555"
DEMO_PHONE_FIRST: Final = 100
DEMO_PHONE_LAST: Final = 199
# "+1555" + four digits.
_DEMO_PHONE_LENGTH: Final = len(DEMO_PHONE_PREFIX) + 4


def normalise_e164(raw: str) -> str:
    """Strip separators from a phone number and check it is E.164.

    Args:
        raw: A number such as ``"+91 98765-43210"``.

    Returns:
        The compact form, e.g. ``"+919876543210"``.

    Raises:
        ValueError: If the result is not E.164. The message omits the number (PII).
    """
    compact = _SEPARATORS.sub("", raw)
    if not _E164.match(compact):
        raise ValueError("phone number must be E.164, e.g. +919876543210")
    return compact


def is_fictional_demo_phone(phone_e164: str) -> bool:
    """Tell whether a number belongs to the fake demo block ``+1 555 0100-0199``.

    Args:
        phone_e164: Compact E.164 number.

    Returns:
        ``True`` for a generated demo number, ``False`` for anything else.
    """
    if len(phone_e164) != _DEMO_PHONE_LENGTH or not phone_e164.startswith(DEMO_PHONE_PREFIX):
        return False
    suffix = phone_e164[len(DEMO_PHONE_PREFIX) :]
    return suffix.isdigit() and DEMO_PHONE_FIRST <= int(suffix) <= DEMO_PHONE_LAST


def to_wa_id(phone_e164: str) -> str:
    """Convert an E.164 number to WhatsApp's ``wa_id`` form (digits, no ``+``).

    Args:
        phone_e164: Compact E.164 number.

    Returns:
        The number without the leading ``+``.
    """
    return phone_e164.removeprefix("+")


def from_wa_id(wa_id: str) -> str:
    """Convert a WhatsApp ``wa_id`` (digits only) to E.164.

    Args:
        wa_id: Sender id from a webhook, e.g. ``"919876543210"``.

    Returns:
        The number with a leading ``+``.
    """
    return "+" + wa_id.removeprefix("+")
