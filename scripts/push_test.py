"""Send a test Web Push notification to your own subscribed device(s).

Usage::

    uv run python -m scripts.push_test                          # the linked demo patient
    uv run python -m scripts.push_test --email you@example.com  # the patient of a login
    uv run python -m scripts.push_test --urgency high --title "Test" --body "Hello"

Without ``--email`` it targets the one demo patient linked by
``seed_demo --my-email``. The recipient guard still applies, and every device the
patient enabled alerts on gets the notification. Exit code 1 if none accepted it.
"""

from __future__ import annotations

import argparse
import logging
from typing import get_args

from sqlalchemy import select

from app.core.config import get_settings
from app.core.exceptions import ConfigurationError, NotificationDeliveryError
from app.core.logging import configure_logging
from app.db.auth_users import find_auth_user_id_by_email
from app.db.models import User
from app.db.push_repository import SqlSubscriptionStore, find_patient_by_auth_user
from app.db.session import get_session_factory
from app.services.notifications import DeliveryReport, Notification, Recipient, Urgency
from app.services.webpush import PywebpushSender, WebPushChannel

logger = logging.getLogger(__name__)

DEFAULT_TITLE = "Diabetes Advisor test"
DEFAULT_BODY = "Alerts work on this device. Tap to open the app."


def find_recipient(email: str | None) -> Recipient:
    """Resolve the patient to notify.

    Args:
        email: Login email, or ``None`` for the demo patient linked to a login.

    Returns:
        The recipient.

    Raises:
        ConfigurationError: If no matching patient exists (with the fix to apply).
    """
    with get_session_factory()() as session:
        if email is not None:
            auth_id = find_auth_user_id_by_email(session, email)
            if auth_id is None:
                raise ConfigurationError("no Supabase login uses that email")
            user = find_patient_by_auth_user(session, auth_id)
        else:
            linked = session.scalars(
                select(User).where(User.is_demo, User.auth_user_id.is_not(None))
            ).all()
            if len(linked) > 1:
                raise ConfigurationError("several demo patients are linked; pass --email")
            user = linked[0] if linked else None
        if user is None:
            raise ConfigurationError(
                "no patient is linked to your login: run "
                "uv run python -m scripts.seed_demo --my-email <your email>"
            )
        return Recipient.for_user(user)


def send_test(recipient: Recipient, notification: Notification) -> DeliveryReport:
    """Push one notification with the real Web Push channel.

    Args:
        recipient: Target patient.
        notification: What to show.

    Returns:
        The delivery report.

    Raises:
        ConfigurationError: If the VAPID keys are missing.
        RecipientBlockedError: If the guard blocks the recipient.
    """
    settings = get_settings()
    channel = WebPushChannel(
        SqlSubscriptionStore(get_session_factory()), PywebpushSender(settings.require_vapid())
    )
    return channel.send(recipient, notification)


def main(argv: list[str] | None = None) -> int:
    """Send the test notification and report per-device results.

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        0 if at least one device accepted it, else 1.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--email", help="login email of the patient (default: linked demo patient)")
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--body", default=DEFAULT_BODY)
    parser.add_argument("--urgency", choices=get_args(Urgency), default="normal")
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)

    try:
        recipient = find_recipient(args.email)
        report = send_test(
            recipient,
            Notification(title=args.title, body=args.body, url="/app/", urgency=args.urgency),
        )
    except (ConfigurationError, NotificationDeliveryError) as exc:
        logger.error("push test failed: %s", exc)
        return 1
    logger.info(
        "push test to user %s: %d device(s) tried, %d sent, %d revoked, %d failed%s",
        recipient.user_id,
        report.attempted,
        report.sent,
        report.revoked,
        report.failed,
        f" ({'; '.join(report.errors)})" if report.errors else "",
    )
    if not report.delivered:
        logger.error("no device accepted the push: open /app on your phone and press Enable alerts")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
