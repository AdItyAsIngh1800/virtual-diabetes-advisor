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

* ``GET /api/practitioner/audit/summary`` - counts over the same advisor replies as
  the review list, in ``[since, until)`` (default: the last 30 days): totals, current
  verdicts, escalated, "I don't know" and dosing/diagnosis-guard replies, overall and
  per ``kb_version``, plus the knowledge-base status. Every rate comes with its count
  and denominator (``null`` when the denominator is 0).
* ``GET /api/practitioner/model-card`` - the risk model's test-set metrics (precision,
  recall and F1 before ROC-AUC), labelled confusion matrix, FP / (FP + TP), decision
  threshold, tier cut-offs, top features, fixed limitations and the last five retrain
  runs. Read from ``models/risk_model.meta.json``; an invalid file answers 500.

Patients are shown by first name only; no phone number or push endpoint is returned.
A turn the practitioner may not see answers 404, the same as one that does not exist.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import current_auth_user
from app.api.chat import _require_practitioner
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ModelMetadataError,
    ModelNotTrainedError,
    ReviewNoteRequiredError,
    TurnNotReviewableError,
)
from app.db.audit_repository import (
    MAX_PAGE_SIZE,
    NOTE_REQUIRED,
    AnswerCounts,
    KbVersionCounts,
    ReviewFilter,
    ReviewRecord,
    TurnCursor,
    TurnPage,
    add_review,
    answer_summary,
    is_visible,
    list_reviewable_turns,
)
from app.db.batch_repository import SqlBatchRepository
from app.db.chat_repository import db_now
from app.db.kb_repository import KbStatusCounts, kb_status_counts
from app.db.models import ReviewVerdict
from app.db.session import get_session_factory
from app.jobs.retrain import RECENT_RUNS, RETRAIN_JOB, RetrainRunView, run_view
from app.ml.artifacts import DEFAULT_MODEL_DIR, load_metadata
from app.ml.model_card import ModelCard, build_model_card
from app.services.supabase_auth import AuthUser

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audit"])

MAX_REVIEW_NOTE_CHARS: Final = 1000
DEFAULT_PAGE_SIZE: Final = 25
# A cursor is base64 JSON of a timestamp and a UUID (about 110 chars); anything much
# longer is not one of ours.
_MAX_CURSOR_CHARS: Final = 256
TURN_NOT_FOUND: Final = "turn not found"
DEFAULT_SUMMARY_DAYS: Final = 30
MODEL_CARD_UNAVAILABLE: Final = "the model card is unavailable: the model metadata is invalid"


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
    patient_id: uuid.UUID | None = None,
) -> TurnPage:
    """List advisor replies to the practitioner's patients for review.

    Args:
        auth_user: Verified login.
        session_factory: Database sessions.
        settings: Settings (low-confidence cutoff).
        review_filter: Which turns.
        limit: Page size.
        cursor: ``next_cursor`` from the previous page.
        patient_id: Only this patient's replies (the patient detail's Conversation
            tab).

    Returns:
        One page, newest first.

    Raises:
        HTTPException: 401 without a valid login, 403 without a practitioner login,
            404 if ``patient_id`` is not a patient this practitioner may see, 422 for
            an unknown filter or a malformed cursor.
    """
    try:
        position = TurnCursor.decode(cursor) if cursor else None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid cursor") from exc
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        if patient_id is not None and not is_visible(session, practitioner.id, patient_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "patient not found")
        return list_reviewable_turns(
            session,
            practitioner.id,
            review_filter,
            limit,
            position,
            low_confidence_below=settings.review_low_confidence_below,
            patient_id=patient_id,
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


# ---------------------------------------------------------------------------
# Audit tab (R5)
# ---------------------------------------------------------------------------


class AuditSummary(BaseModel):
    """Body of ``GET /api/practitioner/audit/summary``.

    Attributes:
        since: Inclusive start of the window.
        until: Exclusive end.
        totals: Advisor replies the caller may see, in the window.
        by_kb_version: The same counts per KB release, to compare versions.
        kb_status: Approved entries, drafts and who approved them (clinic-wide).
    """

    since: AwareDatetime
    until: AwareDatetime
    totals: AnswerCounts
    by_kb_version: list[KbVersionCounts]
    kb_status: KbStatusCounts


class ModelCardOut(ModelCard):
    """Body of ``GET /api/practitioner/model-card``: the card plus recent retrains.

    Attributes:
        recent_retrains: The last :data:`app.jobs.retrain.RECENT_RUNS` retrain runs,
            newest first.
    """

    recent_retrains: list[RetrainRunView]


def live_model_dir() -> Path:
    """Where the live model is (a dependency so tests can point elsewhere).

    Returns:
        ``models/``.
    """
    return DEFAULT_MODEL_DIR


@router.get("/api/practitioner/audit/summary")
def audit_summary(
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
) -> AuditSummary:
    """Count advisor replies to the caller's patients, overall and per KB version.

    Args:
        auth_user: Verified login.
        session_factory: Database sessions.
        since: Inclusive start (default: 30 days before ``until``).
        until: Exclusive end (default: now, on the database clock).

    Returns:
        Counts with rates, and the KB status.

    Raises:
        HTTPException: 401 without a valid login, 403 without a practitioner login,
            422 for a naive timestamp or ``since`` not before ``until``.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        end = until or db_now(session)
        start = since or end - timedelta(days=DEFAULT_SUMMARY_DAYS)
        try:
            answers = answer_summary(session, practitioner.id, since=start, until=end)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        return AuditSummary(
            since=start,
            until=end,
            totals=answers.totals,
            by_kb_version=answers.by_kb_version,
            kb_status=kb_status_counts(session),
        )


@router.get("/api/practitioner/model-card")
def model_card(
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    models: Annotated[Path, Depends(live_model_dir)],
) -> ModelCardOut:
    """Describe the live risk model honestly (R5).

    Args:
        auth_user: Verified login.
        session_factory: Database sessions.
        models: Folder of the live model.

    Returns:
        The card and the last retrain runs.

    Raises:
        HTTPException: 401 without a valid login, 403 without a practitioner login,
            503 when no model is installed, 500 when its metadata is invalid (logged).
    """
    with session_factory() as session:
        _require_practitioner(session, auth_user)
        runs = SqlBatchRepository(session).recent_runs(RETRAIN_JOB, RECENT_RUNS)
        recent = [run_view(r) for r in runs]
    try:
        metadata = load_metadata(models)
    except ModelNotTrainedError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "no risk model is installed"
        ) from exc
    except ModelMetadataError as exc:
        logger.exception("model card: invalid risk model metadata")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, MODEL_CARD_UNAVAILABLE) from exc
    card = build_model_card(metadata)
    return ModelCardOut(**card.model_dump(), recent_retrains=recent)
