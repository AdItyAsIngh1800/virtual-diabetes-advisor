"""Practitioner knowledge-base routes (R5, human in the loop).

Practitioner only (signed-in, linked to an active ``practitioners`` row; the same
``_require_practitioner`` check as the handoff inbox and the review tab):

* ``GET /api/practitioner/kb`` - drafts awaiting approval and the approved entries.
* ``PUT /api/practitioner/kb/{id}`` - save a **draft's** title, body and citation.
* ``POST /api/practitioner/kb/{id}/edit`` - open a draft that supersedes an approved
  entry (201 when created, 200 when one was already open). The approved row is not
  touched; it keeps being served until the draft is approved.
* ``POST /api/practitioner/kb/{id}/approve`` - publish a draft. Needs a body and a
  source citation; records the signed-in practitioner and time, assigns the next
  ``kb_version`` and retires the entry it supersedes.
* ``POST /api/practitioner/kb/{id}/reject`` - retire a draft unpublished.

Approved rows are never edited in place, and only drafts may be edited (409
otherwise). The staff-only ``review_hint`` is returned here and nowhere patient-facing.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import current_auth_user
from app.api.chat import MAX_TEXT_CHARS, _require_practitioner
from app.core.exceptions import (
    KbApprovalIncompleteError,
    KbEntryNotFoundError,
    KbEntryStateError,
)
from app.db.kb_repository import (
    KbEntryView,
    KbListing,
    approve_draft,
    list_kb,
    reject_draft,
    start_edit,
    update_draft,
)
from app.db.session import get_session_factory
from app.services.supabase_auth import AuthUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/practitioner/kb", tags=["knowledge base"])

# A proposed draft's title is the patient's question, so it may be as long as a chat
# message.
MAX_TITLE_CHARS: Final = MAX_TEXT_CHARS
# The longest seed entry is under 1,000 characters; 6,000 leaves room for a detailed
# entry while keeping a reply short enough to read on a phone or to hear.
MAX_BODY_CHARS: Final = 6000
MAX_CITATION_CHARS: Final = 500
ENTRY_NOT_FOUND: Final = "knowledge-base entry not found"

Factory = Annotated[sessionmaker[Session], Depends(get_session_factory)]
Login = Annotated[AuthUser, Depends(current_auth_user)]


class KbDraftIn(BaseModel):
    """Body of ``PUT /api/practitioner/kb/{id}``.

    Attributes:
        title: Entry title (shown to patients once approved).
        body: Entry text; may stay empty while drafting, required to approve.
        source_citation: Where the text comes from; required to approve.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    body: str = Field(default="", max_length=MAX_BODY_CHARS)
    source_citation: str | None = Field(default=None, max_length=MAX_CITATION_CHARS)


def _http(exc: Exception) -> HTTPException:
    """Map a KB repository error to its HTTP answer.

    Args:
        exc: A :class:`KbEntryNotFoundError`, :class:`KbEntryStateError` or
            :class:`KbApprovalIncompleteError`.

    Returns:
        404, 409 or 422 respectively.
    """
    if isinstance(exc, KbEntryNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, ENTRY_NOT_FOUND)
    if isinstance(exc, KbApprovalIncompleteError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


_KB_ERRORS: Final = (KbEntryNotFoundError, KbEntryStateError, KbApprovalIncompleteError)


@router.get("")
def get_kb(auth_user: Login, session_factory: Factory) -> KbListing:
    """List drafts awaiting approval and the entries being served.

    Args:
        auth_user: Verified login.
        session_factory: Database sessions.

    Returns:
        Drafts (newest first) and approved entries (by title).

    Raises:
        HTTPException: 401 without a valid login, 403 without a practitioner login.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        return list_kb(session, practitioner.id)


@router.put("/{entry_id}")
def put_draft(
    entry_id: uuid.UUID, body: KbDraftIn, auth_user: Login, session_factory: Factory
) -> KbEntryView:
    """Save a draft (only drafts may be edited).

    Args:
        entry_id: The draft.
        body: New title, body and citation.
        auth_user: Verified login.
        session_factory: Database sessions.

    Returns:
        The saved draft.

    Raises:
        HTTPException: 401/403 as above, 404 for an unknown id, 409 when the entry
            is approved or retired, 422 for a bad body.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        try:
            view = update_draft(
                session,
                entry_id,
                practitioner.id,
                title=body.title,
                body=body.body,
                source_citation=body.source_citation,
            )
        except _KB_ERRORS as exc:
            raise _http(exc) from exc
        session.commit()
    logger.info("practitioner %s saved kb draft %s", practitioner.id, entry_id)
    return view


@router.post("/{entry_id}/edit", status_code=status.HTTP_201_CREATED)
def edit_approved(
    entry_id: uuid.UUID, auth_user: Login, session_factory: Factory, response: Response
) -> KbEntryView:
    """Open a draft that supersedes an approved entry, starting from its text.

    Args:
        entry_id: The approved entry.
        auth_user: Verified login.
        session_factory: Database sessions.
        response: Set to 200 when a draft was already open.

    Returns:
        The draft (``supersedes_id`` = ``entry_id``).

    Raises:
        HTTPException: 401/403 as above, 404 for an unknown id, 409 when the entry
            is not approved.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        try:
            view, created = start_edit(session, entry_id, practitioner.id)
        except _KB_ERRORS as exc:
            raise _http(exc) from exc
        session.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    logger.info(
        "practitioner %s editing kb entry %s as draft %s", practitioner.id, entry_id, view.id
    )
    return view


@router.post("/{entry_id}/approve")
def approve(entry_id: uuid.UUID, auth_user: Login, session_factory: Factory) -> KbEntryView:
    """Approve a draft: it is served to patients from the next question on.

    Args:
        entry_id: The draft.
        auth_user: Verified login (recorded as ``approved_by``).
        session_factory: Database sessions.

    Returns:
        The approved entry with its new ``kb_version``.

    Raises:
        HTTPException: 401/403 as above, 404 for an unknown id, 409 when the entry
            is not a draft or its superseded entry changed, 422 without a body or a
            source citation.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        try:
            view = approve_draft(session, entry_id, practitioner.id)
        except _KB_ERRORS as exc:
            raise _http(exc) from exc
        session.commit()
    logger.info(
        "practitioner %s approved kb entry %s as kb_version %s",
        practitioner.id,
        entry_id,
        view.kb_version,
    )
    return view


@router.post("/{entry_id}/reject")
def reject(entry_id: uuid.UUID, auth_user: Login, session_factory: Factory) -> KbEntryView:
    """Reject a draft (retired, kept for the record, never served).

    Args:
        entry_id: The draft.
        auth_user: Verified login.
        session_factory: Database sessions.

    Returns:
        The retired row.

    Raises:
        HTTPException: 401/403 as above, 404 for an unknown id, 409 when the entry
            is not a draft.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        try:
            view = reject_draft(session, entry_id, practitioner.id)
        except _KB_ERRORS as exc:
            raise _http(exc) from exc
        session.commit()
    logger.info("practitioner %s rejected kb draft %s", practitioner.id, entry_id)
    return view
