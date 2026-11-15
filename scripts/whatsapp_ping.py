"""Send one WhatsApp message to your own number to check the token and phone-number id.

Usage::

    uv run python -m scripts.whatsapp_ping                         # text to your number
    uv run python -m scripts.whatsapp_ping --to +919876543210      # explicit number
    uv run python -m scripts.whatsapp_ping --template hello_world  # outside the 24 h window
    uv run python -m scripts.whatsapp_ping --webhook-url           # print the callback URL

Without ``--to`` the message goes to the single number in ``ALLOWED_TEST_NUMBERS``.
The send guard still applies: in test mode the number must be on that allowlist.

A plain text only *delivers* within 24 h of your last message to the business number;
Meta still accepts it (you get a ``wamid``) but the status webhook later says
``failed`` (131047). If you have not messaged the test number today, use
``--template hello_world`` (pre-approved on every test number, language ``en_US``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.api.webhooks import webhook_callback_url
from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError, WhatsAppDeliveryError
from app.core.logging import configure_logging
from app.core.phones import normalise_e164
from app.services.whatsapp import SendResult, WhatsAppClient
from app.services.whatsapp_guard import Recipient

logger = logging.getLogger(__name__)

DEFAULT_TEXT = "Diabetes advisor ping: the WhatsApp Cloud API token works."


def resolve_target(explicit: str | None, settings: Settings) -> str:
    """Pick the number to ping.

    Args:
        explicit: ``--to`` value, if given.
        settings: Supplies ``ALLOWED_TEST_NUMBERS``.

    Returns:
        A compact E.164 number.

    Raises:
        ConfigurationError: If ``--to`` is missing and the allowlist does not hold
            exactly one number, or ``--to`` is not E.164.
    """
    if explicit:
        try:
            return normalise_e164(explicit)
        except ValueError as exc:
            raise ConfigurationError(f"--to: {exc}") from None
    if len(settings.allowed_test_numbers) == 1:
        return settings.allowed_test_numbers[0]
    raise ConfigurationError(
        "pass --to +<number>: ALLOWED_TEST_NUMBERS must hold exactly one number to default to it"
    )


async def ping(
    settings: Settings, recipient: Recipient, *, text: str, template: str | None, language: str
) -> SendResult:
    """Send the ping message.

    Args:
        settings: Application settings.
        recipient: Target (an operator number, so ``is_demo=False``).
        text: Body for a free-form text.
        template: Template name; when set, a template is sent instead of text.
        language: Template language code.

    Returns:
        The accepted message's ``wamid``.

    Raises:
        WhatsAppDeliveryError: If the guard blocks the number or Graph rejects the call.
    """
    async with WhatsAppClient(settings) as client:
        if template:
            return await client.send_template(recipient, template, language_code=language)
        return await client.send_text(recipient, text)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, send one message and report the result.

    Args:
        argv: Command-line arguments (``None`` reads ``sys.argv``).

    Returns:
        Process exit code: 0 on success, 1 on failure.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--to", help="E.164 number; default: the only ALLOWED_TEST_NUMBERS entry")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="free-form text to send")
    parser.add_argument("--template", help="send this approved template instead, e.g. hello_world")
    parser.add_argument("--language", default="en_US", help="template language code")
    parser.add_argument(
        "--webhook-url", action="store_true", help="print the webhook callback URL and exit"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        if args.webhook_url:
            print(webhook_callback_url(settings))
            return 0
        recipient = Recipient(phone_e164=resolve_target(args.to, settings), is_demo=False)
        result = asyncio.run(
            ping(
                settings,
                recipient,
                text=args.text,
                template=args.template,
                language=args.language,
            )
        )
    except (ConfigurationError, WhatsAppDeliveryError) as exc:
        logger.error("ping failed: %s", exc)
        return 1
    kind = f"template {args.template!r}" if args.template else "text"
    logger.info("ping %s accepted for %s: wamid=%s", kind, recipient.masked, result.wamid)
    if not args.template:
        logger.info(
            "not on your phone? the 24 h window is probably closed: message the test "
            "number first, or re-run with --template hello_world"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
