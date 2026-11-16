"""Practitioner review of advisor answers (R5 audit loop).

Practitioner only (signed-in, linked to an active ``practitioners`` row; the same check
as the handoff inbox), and only for patients they may see:

* ``GET /api/practitioner/turns`` - advisor replies, newest first. Query parameters:
  ``filter`` (``all``, ``unreviewed``, ``escalated``, ``low_confidence``,
  ``unanswered``), ``limit`` (1-100) and ``cursor`` (``next_cursor`` of the previous
  page).
* ``POST /api/practitioner/turns/{turn_id}/review`` - record a verdict: ``correct``,
  ``incorrect``, ``unsafe`` or ``needs_kb_update``, with a note (required for all but
  ``correct``). Reviews are append-only: a second review of the same turn is added,
  and the latest one becomes the turn's current verdict.

Patients are shown by first name only; no phone number or push endpoint is returned.
A turn the practitioner may not see answers 404, the same as one that does not exist.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import current_auth_user
from app.api.chat import _require_practitioner
from app.core.config import Settings, get_settings
from app.core.exceptions import ReviewNoteRequiredError, TurnNotReviewableError
from app.db.audit_repository import (
    MAX_PAGE_SIZE,
    NOTE_REQUIRED,
    ReviewFilter,
    ReviewRecord,
    TurnCursor,
    TurnPage,
    add_review,
    list_reviewable_turns,
)
from app.db.models import ReviewVerdict
from app.db.session import get_session_factory
from app.services.supabase_auth import AuthUser

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audit"])

MAX_REVIEW_NOTE_CHARS: Final = 1000
DEFAULT_PAGE_SIZE: Final = 25
# A cursor is base64 JSON of a timestamp and a UUID (about 110 chars); anything much
# longer is not one of ours.
_MAX_CURSOR_CHARS: Final = 256
TURN_NOT_FOUND: Final = "turn not found"


class ReviewIn(BaseModel):
    """Body of ``POST /api/practitioner/turns/{turn_id}/review``.

    Attributes:
        verdict: ``correct``, ``incorrect``, ``unsafe`` or ``needs_kb_update``.
        note: What is wrong or what the KB should say; required unless ``correct``.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: ReviewVerdict
    note: str | None = Field(default=None, max_length=MAX_REVIEW_NOTE_CHARS)

    @model_validator(mode="after")
    def _note_when_required(self) -> ReviewIn:
        """Strip the note and require one for every verdict other than ``correct``.

        Returns:
            The validated body.

        Raises:
            ValueError: The verdict needs a note and the note is missing or blank
                (FastAPI answers 422).
        """
        self.note = (self.note or "").strip() or None
        if self.verdict in NOTE_REQUIRED and self.note is None:
            raise ValueError(f"a note is required when the verdict is {self.verdict!r}")
        return self


@router.get("/api/practitioner/turns")
def list_turns(  # noqa: PLR0917 - FastAPI injects each dependency as a parameter
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    settings: Annotated[Settings, Depends(get_settings)],
    review_filter: Annotated[ReviewFilter, Query(alias="filter")] = "all",
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=_MAX_CURSOR_CHARS)] = None,
) -> TurnPage:
    """List advisor replies to the practitioner's patients for review.

    Args:
        auth_user: Verified login.
        session_factory: Database sessions.
        settings: Settings (low-confidence cutoff).
        review_filter: Which turns.
        limit: Page size.
        cursor: ``next_cursor`` from the previous page.

    Returns:
        One page, newest first.

    Raises:
        HTTPException: 401 without a valid login, 403 without a practitioner login,
            422 for an unknown filter or a malformed cursor.
    """
    try:
        position = TurnCursor.decode(cursor) if cursor else None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid cursor") from exc
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        return list_reviewable_turns(
            session,
            practitioner.id,
            review_filter,
            limit,
            position,
            low_confidence_below=settings.review_low_confidence_below,
        )


@router.post("/api/practitioner/turns/{turn_id}/review", status_code=201)
def review_turn(
    turn_id: uuid.UUID,
    body: ReviewIn,
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> ReviewRecord:
    """Record a verdict on an advisor reply (append-only; the latest is current).

    Args:
        turn_id: Turn being reviewed.
        body: Verdict and note.
        auth_user: Verified login.
        session_factory: Database sessions.

    Returns:
        The stored review.

    Raises:
        HTTPException: 401 without a valid login, 403 without a practitioner login,
            404 if the turn does not exist, is not an advisor reply, or belongs to a
            patient this practitioner may not see, 422 for a bad body (including a
            missing note).
    """
    with session_factory() as session:
        reviewer_id = _require_practitioner(session, auth_user).id
        try:
            review = add_review(
                session,
                turn_id=turn_id,
                reviewer_id=reviewer_id,
                verdict=body.verdict,
                note=body.note,
            )
        except TurnNotReviewableError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, TURN_NOT_FOUND) from exc
        except ReviewNoteRequiredError as exc:  # pragma: no cover - ReviewIn checks first
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        session.commit()
    logger.info("practitioner %s reviewed turn %s: %s", reviewer_id, turn_id, body.verdict)
    return review
