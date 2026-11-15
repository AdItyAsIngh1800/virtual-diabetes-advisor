"""Safety guard run before every outbound WhatsApp message (R3 delivery, R4 replies).

Two rules, checked before any HTTP request leaves the process:

1. **Demo patients** (``users.is_demo``) receive nothing while their number is still
   one of the made-up ``+1 555 0100-0199`` seed numbers. The one demo patient that
   ``scripts.seed_demo --my-phone`` gives your real number is the only demo patient
   that can receive anything.
2. **Test number**: while ``WHATSAPP_TEST_MODE`` is on (the Meta app sends from its
   free test number), only numbers in ``ALLOWED_TEST_NUMBERS`` can receive messages. An
   empty allowlist blocks everything (fails closed).

:class:`app.services.whatsapp.WhatsAppClient` accepts a :class:`Recipient`, never a
bare number, and calls :func:`check_recipient` itself, so no send path can skip it.
"""

from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.core.exceptions import RecipientBlockedError
from app.core.logging import mask_phone
from app.core.phones import E164_PATTERN, is_fictional_demo_phone
from app.db.models import Profile, User

logger = logging.getLogger(__name__)


class Recipient(BaseModel):
    """Who a message goes to, with the facts the guard needs.

    Attributes:
        phone_e164: Compact E.164 number, e.g. ``+919876543210``.
        is_demo: ``users.is_demo`` of the patient; ``False`` for an operator number
            typed on the command line (``scripts.whatsapp_ping``).
        user_id: Patient id, for log lines (never the number). ``None`` for operators.
    """

    model_config = ConfigDict(frozen=True)

    phone_e164: str = Field(pattern=E164_PATTERN)
    is_demo: bool
    user_id: uuid.UUID | None = None

    @classmethod
    def for_patient(cls, user: User, profile: Profile) -> Recipient:
        """Build the recipient for a patient from their database rows.

        Args:
            user: The ``users`` row (supplies ``is_demo``).
            profile: The matching ``profiles`` row (supplies the number).

        Returns:
            The recipient.

        Raises:
            ValueError: If the profile belongs to another user.
        """
        if profile.user_id != user.id:
            raise ValueError("profile does not belong to user")
        return cls(phone_e164=profile.phone_e164, is_demo=user.is_demo, user_id=user.id)

    @property
    def masked(self) -> str:
        """Masked number for log lines, e.g. ``+91******3210``.

        Returns:
            The number with its middle digits hidden.
        """
        return mask_phone(self.phone_e164)


def check_recipient(recipient: Recipient, settings: Settings) -> None:
    """Raise unless ``recipient`` may receive a WhatsApp message right now.

    Args:
        recipient: Target of the message.
        settings: Supplies ``whatsapp_test_mode`` and ``allowed_test_numbers``.

    Raises:
        RecipientBlockedError: If the demo rule or the test-number allowlist blocks
            the send. The message contains the masked number only.
    """
    if recipient.is_demo and is_fictional_demo_phone(recipient.phone_e164):
        reason = "demo patient has a fictional number (only the --my-phone patient is real)"
        _block(recipient, reason)
    if settings.whatsapp_test_mode and recipient.phone_e164 not in settings.allowed_test_numbers:
        reason = (
            "WHATSAPP_TEST_MODE is on and the number is not in ALLOWED_TEST_NUMBERS"
            if settings.allowed_test_numbers
            else "WHATSAPP_TEST_MODE is on and ALLOWED_TEST_NUMBERS is empty"
        )
        _block(recipient, reason)


def _block(recipient: Recipient, reason: str) -> None:
    """Log and raise a guard block.

    Args:
        recipient: The blocked recipient.
        reason: Why it was blocked (no PII).

    Raises:
        RecipientBlockedError: Always.
    """
    logger.warning(
        "whatsapp send blocked for %s (user %s): %s", recipient.masked, recipient.user_id, reason
    )
    raise RecipientBlockedError(f"send to {recipient.masked} blocked: {reason}")
