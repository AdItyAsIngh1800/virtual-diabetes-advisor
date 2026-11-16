"""Resolve a test patient's stale handoffs so the chat starts fresh (R4 testing aid).

Usage::

    uv run python -m scripts.resolve_handoffs --patient-email you@example.com
    uv run python -m scripts.resolve_handoffs --patient-email you@example.com --include-assigned
    uv run python -m scripts.resolve_handoffs --patient-email you@example.com --dry-run

Finds the patient linked to that login (``seed_demo --my-email``) and resolves their
live handoffs: by default only ``open`` ones (never claimed); with
``--include-assigned`` also those a practitioner claimed. Each gets ``resolved_at`` and
a resolution note saying this script closed it. The append-only ``conversation_turns``
are never touched: the conversation history and its audit stay exactly as they were.
"""

from __future__ import annotations

import argparse
import logging
import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import ConfigurationError
from app.core.logging import configure_logging
from app.db.auth_users import find_auth_user_id_by_email
from app.db.chat_repository import (
    LIVE_STATUSES,
    TEST_RESOLUTION_NOTE,
    db_now,
    resolve_stale_handoffs,
)
from app.db.models import Handoff, HandoffStatus, User
from scripts.seed_demo import SeedError, validate_my_email

logger = logging.getLogger(__name__)


class ResolveError(Exception):
    """Raised when the patient cannot be found (with the fix in the message)."""


def find_patient_id(session: Session, email: str) -> uuid.UUID:
    """Return the patient linked to the login with this email.

    Args:
        session: Open session.
        email: Validated login email.

    Returns:
        ``users.id`` of the patient.

    Raises:
        ResolveError: If no login has that email or it is not linked to a patient.
    """
    auth_user_id = find_auth_user_id_by_email(session, email)
    if auth_user_id is None:
        raise ResolveError("no Supabase login uses that email")
    patient_id = session.scalar(
        select(User.id).where(User.auth_user_id == auth_user_id, User.role == "patient")
    )
    if patient_id is None:
        raise ResolveError(
            "that login is not linked to a patient "
            "(run: uv run python -m scripts.seed_demo --my-email <email>)"
        )
    return patient_id


def resolve_for_email(
    session_factory: Callable[[], Session],
    email: str,
    *,
    include_assigned: bool = False,
    dry_run: bool = False,
    note: str = TEST_RESOLUTION_NOTE,
) -> list[uuid.UUID]:
    """Resolve the stale handoffs of the patient behind ``email``.

    Args:
        session_factory: Database sessions.
        email: Validated patient login email.
        include_assigned: Also resolve claimed handoffs.
        dry_run: Only report what would be resolved.
        note: Resolution note.

    Returns:
        Ids of the handoffs resolved (or that would be, for a dry run).
    """
    statuses: tuple[HandoffStatus, ...] = LIVE_STATUSES if include_assigned else ("open",)
    with session_factory() as session:
        patient_id = find_patient_id(session, email)
        if dry_run:
            return list(
                session.scalars(
                    select(Handoff.id).where(
                        Handoff.user_id == patient_id, Handoff.status.in_(statuses)
                    )
                )
            )
        resolved = resolve_stale_handoffs(
            session,
            patient_id,
            at=db_now(session),
            note=note,
            include_assigned=include_assigned,
        )
        session.commit()
    return resolved


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        Process exit code (1 if the patient cannot be found).
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--patient-email", required=True, help="the patient's login email")
    parser.add_argument(
        "--include-assigned", action="store_true", help="also resolve claimed handoffs"
    )
    parser.add_argument("--dry-run", action="store_true", help="only list what would change")
    parser.add_argument("--note", default=TEST_RESOLUTION_NOTE, help="resolution note")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    from app.db.session import get_session_factory  # noqa: PLC0415 - CLI-only import

    try:
        email = validate_my_email(args.patient_email, "--patient-email")
        ids = resolve_for_email(
            get_session_factory(),
            email,
            include_assigned=args.include_assigned,
            dry_run=args.dry_run,
            note=args.note,
        )
    except (ResolveError, SeedError, ConfigurationError) as exc:
        logger.error("resolve handoffs failed: %s", exc)
        return 1
    verb = "would resolve" if args.dry_run else "resolved"
    # A CLI report, so stdout rather than the log.
    print(f"{verb} {len(ids)} handoff(s): {', '.join(map(str, ids)) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
