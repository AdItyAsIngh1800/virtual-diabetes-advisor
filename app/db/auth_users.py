"""Read-only lookups in Supabase's ``auth.users`` (the web-app logins).

The backend connects as the database owner, which can read the ``auth`` schema. Only
the id is ever selected: emails are PII and stay in Supabase Auth.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session


def find_auth_user_id_by_email(session: Session, email: str) -> uuid.UUID | None:
    """Return the id of the Supabase login with this email.

    Args:
        session: Open session.
        email: Login email (matched case-insensitively, as Supabase Auth does).

    Returns:
        ``auth.users.id``, or ``None`` if nobody signed up with that email.
    """
    # SSO users may share an email with an email/password user; only the latter can
    # sign in to /app, so prefer it (is_sso_user = false sorts first).
    row = session.execute(
        text(
            "select id from auth.users where lower(email) = lower(:email) "
            "order by is_sso_user, created_at limit 1"
        ),
        {"email": email.strip()},
    ).first()
    return row[0] if row is not None else None
