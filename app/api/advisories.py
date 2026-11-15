"""Advisory API for the patient web app (R3).

``GET /api/advisories/{id}`` returns one advisory - reading, personal limit, advice
text - plus a short-lived signed URL of its voice recording. Only the patient who owns
the advisory may read it (403 for anyone else). Opening it is what "opened" means:
the first successful read sets ``opened_at`` (and ``sent`` becomes ``opened``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AwareDatetime, BaseModel
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import current_auth_user
from app.api.push import NOT_LINKED
from app.core.clinical_limits import GlucoseLimitKind
from app.core.config import Settings, get_settings
from app.core.exceptions import AudioStorageError
from app.db.advisory_repository import advisory_for_view, mark_opened
from app.db.models import AdvisoryState, DeliveryStatus, Language, ReadingContext
from app.db.push_repository import find_patient_by_auth_user
from app.db.session import get_session_factory
from app.jobs.dispatch_advisories import is_urgent_low
from app.services.advisory import spoken_text
from app.services.supabase_auth import AuthUser
from app.services.voice import AudioStore, SupabaseAudioStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/advisories", tags=["advisories"])

NOT_YOURS: Final = "this advisory belongs to another patient"


class AdvisoryOut(BaseModel):
    """One advisory as the advisory page shows it.

    Attributes:
        id: Advisory id.
        state: Band state.
        direction: ``high`` or ``low``.
        limit_kind: Limit approached or crossed.
        reading_value_mg_dl: The reading.
        threshold_mg_dl: The patient's personal limit.
        context: Reading context.
        measured_at: When the reading was taken.
        urgent: Reading below 54 mg/dL (show the emergency styling).
        escalated: Flagged for the care team.
        language: Language of the text (BCP-47 base, for speechSynthesis).
        message_text: Advice to show.
        spoken_text: What the voice says (fallback for speechSynthesis).
        audio_url: Signed mp3 URL, or ``None`` when no recording exists.
        audio_url_expires_in_s: Lifetime of ``audio_url``.
        delivery_status: Delivery state after this read.
        opened_at: First time the patient opened it.
    """

    id: uuid.UUID
    state: AdvisoryState
    direction: Literal["high", "low"]
    limit_kind: GlucoseLimitKind
    reading_value_mg_dl: Decimal
    threshold_mg_dl: Decimal
    context: ReadingContext
    measured_at: AwareDatetime
    urgent: bool
    escalated: bool
    language: Language
    message_text: str
    spoken_text: str
    audio_url: str | None
    audio_url_expires_in_s: int | None
    delivery_status: DeliveryStatus
    opened_at: AwareDatetime


def get_audio_store(settings: Annotated[Settings, Depends(get_settings)]) -> AudioStore:
    """FastAPI dependency: the Supabase audio store (tests override it).

    Args:
        settings: Application settings.

    Returns:
        The store.
    """
    return SupabaseAudioStore(settings)


@router.get("/{advisory_id}")
def get_advisory(
    advisory_id: uuid.UUID,
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    audio_store: Annotated[AudioStore, Depends(get_audio_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AdvisoryOut:
    """Return the signed-in patient's advisory and record that they opened it.

    Args:
        advisory_id: Advisory to show.
        auth_user: Verified Supabase user.
        session_factory: Database sessions.
        audio_store: Signs the voice recording URL.
        settings: Supplies the signed-URL lifetime.

    Returns:
        The advisory with a short-lived audio URL (``None`` if there is no recording
        or signing failed; the page then uses the browser's speech synthesis).

    Raises:
        HTTPException: 401 without a session, 403 if the login is not linked or the
            advisory belongs to another patient, 404 if it does not exist.
    """
    with session_factory() as session:
        patient = find_patient_by_auth_user(session, auth_user.id)
        if patient is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_LINKED)
        view = advisory_for_view(session, advisory_id)
        if view is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "advisory not found")
        if view.user_id != patient.id:
            logger.warning("user %s refused advisory %s (not theirs)", patient.id, advisory_id)
            raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_YOURS)
        opened_at = mark_opened(session, advisory_id, datetime.now(UTC))
        session.commit()

    audio_url: str | None = None
    ttl = settings.advisory_audio_url_ttl_seconds
    if view.audio_path is not None:
        try:
            audio_url = audio_store.signed_url(view.audio_path, ttl)
        except AudioStorageError as exc:
            logger.warning("advisory %s: could not sign audio (%s)", advisory_id, exc)
    return AdvisoryOut(
        id=view.id,
        state=view.state,
        direction="high" if view.state.endswith("_high") else "low",
        limit_kind=view.limit_kind,
        reading_value_mg_dl=view.reading_value_mg_dl,
        threshold_mg_dl=view.threshold_mg_dl,
        context=view.context,
        measured_at=view.measured_at,
        urgent=is_urgent_low(view.reading_value_mg_dl),
        escalated=view.escalated,
        language=view.language,
        message_text=view.message_text,
        spoken_text=spoken_text(view.message_text),
        audio_url=audio_url,
        audio_url_expires_in_s=ttl if audio_url else None,
        delivery_status="opened" if view.delivery_status == "sent" else view.delivery_status,
        opened_at=opened_at,
    )
