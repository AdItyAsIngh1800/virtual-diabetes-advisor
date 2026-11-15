"""Database access for the WhatsApp webhook (R3 delivery status, R4 inbound turns).

Every function takes an open :class:`~sqlalchemy.orm.Session`; the caller owns the
transaction (one commit per webhook item, so one bad item cannot undo the others).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Advisory, ConversationTurn, Profile, TurnChannel


def find_user_id_by_phone(session: Session, phone_e164: str) -> uuid.UUID | None:
    """Look up the user that owns a phone number.

    Args:
        session: Open session.
        phone_e164: Compact E.164 number.

    Returns:
        The user id, or ``None`` if no profile has this number.
    """
    return session.scalar(select(Profile.user_id).where(Profile.phone_e164 == phone_e164))


def insert_inbound_turn(
    session: Session,
    *,
    user_id: uuid.UUID,
    wamid: str,
    channel: TurnChannel,
    raw_text: str | None,
) -> bool:
    """Append an inbound WhatsApp message to the audit log, once per ``wamid``.

    Meta retries a webhook until it gets a 200 and can deliver the same message
    more than once, so the insert is ``ON CONFLICT (wamid) DO NOTHING``: the unique
    index on ``conversation_turns.wamid`` is the de-duplication record, and it stays
    correct when two workers process the same retry at once.

    Args:
        session: Open session.
        user_id: Sender.
        wamid: Inbound message id from the webhook.
        channel: ``whatsapp_text`` or ``whatsapp_voice``.
        raw_text: Message text, or ``None`` for audio (transcribed in Phase 7).

    Returns:
        ``True`` if the turn was stored, ``False`` if this ``wamid`` was seen before.
    """
    stmt = (
        pg_insert(ConversationTurn)
        .values(
            user_id=user_id,
            wamid=wamid,
            channel=channel,
            direction="inbound",
            raw_text=raw_text,
        )
        .on_conflict_do_nothing(index_elements=[ConversationTurn.wamid])
        .returning(ConversationTurn.id)
    )
    return session.execute(stmt).scalar_one_or_none() is not None


def touch_last_inbound(session: Session, user_id: uuid.UUID, at: datetime) -> None:
    """Move ``profiles.last_inbound_at`` forward to ``at`` (never backwards).

    The value opens the 24 h free-form window (R3). Webhooks can arrive out of
    order, so ``GREATEST`` keeps the latest message time.

    Args:
        session: Open session.
        user_id: Sender.
        at: Time WhatsApp says the message was sent (timezone-aware).
    """
    session.execute(
        update(Profile)
        .where(Profile.user_id == user_id)
        .values(last_inbound_at=func.greatest(func.coalesce(Profile.last_inbound_at, at), at))
    )


def lock_advisory_by_wamid(session: Session, wamid: str) -> Advisory | None:
    """Load and row-lock the advisory whose text or voice message has this ``wamid``.

    ``FOR UPDATE`` serialises concurrent status webhooks for the same advisory
    (``delivered`` and ``read`` often arrive within milliseconds).

    Args:
        session: Open session.
        wamid: Message id from a status webhook.

    Returns:
        The advisory, or ``None`` if the message is not an advisory (e.g. a chat reply).
    """
    return session.scalar(
        select(Advisory)
        .where(or_(Advisory.wamid == wamid, Advisory.audio_wamid == wamid))
        .with_for_update()
    )
