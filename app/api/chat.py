"""Help chat and practitioner-connect API (R4), with every turn audited (R5).

Patient (signed-in, linked login):

* ``POST /api/chat`` - multipart form: ``text`` *or* ``audio`` (MediaRecorder webm/ogg,
  or mp4 from Safari; up to ``CHAT_AUDIO_MAX_BYTES``, about a minute), optional
  ``message_id`` (client id; a retry returns the stored reply), ``request_practitioner``
  and ``advisory_id`` (the advisory page's "Talk to a doctor" button). Audio is
  transcribed with faster-whisper; the reply comes back as text, plus a short-lived
  signed mp3 URL when the patient spoke.
* ``GET /api/chat/messages`` - the patient's recent conversation and live handoff;
  replies to voice messages carry a fresh signed mp3 URL, so Play survives a reload.

Practitioner (signed-in, linked to a ``practitioners`` row; assigned patients only):

* ``GET /api/practitioner/handoffs`` - live handoffs, then recently resolved.
* ``GET /api/practitioner/handoffs/{id}`` - one handoff and the patient's conversation.
* ``POST /api/practitioner/handoffs/{id}/claim`` - claim it (``open -> assigned``): from
  then on the patient's messages are relayed to this practitioner.
* ``POST /api/practitioner/handoffs/{id}/messages`` - reply (claims it if still open; the
  first reply also posts "<name> has joined the chat" to the patient).
* ``POST /api/practitioner/handoffs/{id}/resolve`` - close it (``-> resolved``).

``GET /api/me`` tells the web app which page a login belongs on.

Each patient message is one append-only ``conversation_turns`` row (channel ``web``)
with intent, KB ids used, model/model_version, latency and escalation; each
practitioner reply is an outbound row naming its author. Notifications go out after
the commit as background tasks.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Final, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.advisories import get_audio_store
from app.api.auth import current_auth_user
from app.api.push import NOT_LINKED
from app.core.config import Settings, get_settings
from app.core.exceptions import AudioStorageError, ConfigurationError, SpeechError
from app.db.chat_repository import (
    LIVE_STATUSES,
    HandoffSummary,
    SqlChatStore,
    assigned_practitioner_id,
    claim_handoff,
    db_now,
    find_practitioner_by_auth_user,
    handoff_for_practitioner,
    has_replied,
    practitioner_contact,
    practitioner_handoffs,
    practitioner_name,
    recent_turns,
    resolve_handoff,
    save_turn,
    turn_by_message_id,
    user_contact,
)
from app.db.models import (
    Advisory,
    ConversationTurn,
    Handoff,
    HandoffStatus,
    HandoffTrigger,
    Practitioner,
    TurnIntent,
    User,
)
from app.db.push_repository import SqlSubscriptionStore, find_patient_by_auth_user
from app.db.session import get_session_factory
from app.jobs.dispatch_advisories import run_dispatch
from app.services.chat import ChatEngine, ChatOutcome, ChatRequest, KbIndexCache, Rephraser
from app.services.chat_llm import build_rephraser
from app.services.handoff_notify import HandoffNotifier
from app.services.notifications import DeliveryReport, Notification, Recipient
from app.services.realtime import HttpRealtimePublisher
from app.services.supabase_auth import AuthUser
from app.services.voice import (
    AudioStore,
    GttsSynthesizer,
    SpeechSynthesizer,
    SpeechTranscriber,
    WhisperTranscriber,
)
from app.services.webpush import PywebpushSender, WebPushChannel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

MAX_TEXT_CHARS: Final = 2000
MAX_NOTE_CHARS: Final = 1000
_MESSAGE_ID_PATTERN: Final = r"^[A-Za-z0-9_-]{8,64}$"
# MediaRecorder output: webm/opus (Chrome, Firefox, Android), ogg/opus (Firefox),
# mp4/aac (Safari, iPhone). faster-whisper decodes all of them through PyAV.
_AUDIO_TYPES: Final = frozenset(
    {
        "audio/webm",
        "audio/ogg",
        "audio/mp4",
        "audio/mpeg",
        "audio/wav",
        "audio/x-wav",
        "audio/aac",
        "audio/x-m4a",
        "video/webm",  # some Android builds label audio-only webm as video
    }
)
# conversation_turns.model of a notice the system posts in the chat ("... has joined").
SYSTEM_MODEL: Final = "system"
NOT_PRACTITIONER: Final = (
    "this login is not linked to a practitioner "
    "(run: uv run python -m scripts.seed_demo --practitioner-email <email>)"
)


# ---------------------------------------------------------------------------
# Dependencies (tests override these)
# ---------------------------------------------------------------------------

_KB_CACHE: Final = KbIndexCache()


def get_kb_cache() -> KbIndexCache:
    """Return the process-wide TF-IDF index cache.

    Returns:
        The cache.
    """
    return _KB_CACHE


@lru_cache(maxsize=1)
def _shared_transcriber() -> WhisperTranscriber:
    """One faster-whisper model per process (loaded lazily on first use).

    Returns:
        The transcriber.
    """
    return WhisperTranscriber()


def get_transcriber() -> SpeechTranscriber:
    """FastAPI dependency: the speech-to-text engine.

    Returns:
        The shared faster-whisper transcriber.
    """
    return _shared_transcriber()


def get_synthesizer() -> SpeechSynthesizer:
    """FastAPI dependency: text-to-speech for spoken replies.

    Returns:
        gTTS.
    """
    return GttsSynthesizer()


def get_rephraser(settings: Annotated[Settings, Depends(get_settings)]) -> Rephraser | None:
    """FastAPI dependency: the optional Claude rephraser.

    Args:
        settings: Application settings.

    Returns:
        A rephraser only when ``CHAT_LLM_REPHRASE_ENABLED`` is true and
        ``ANTHROPIC_API_KEY`` is set, else ``None`` (knowledge-base text as written).
    """
    if not settings.chat_llm_rephrase_enabled:
        return None
    return build_rephraser(settings)


class _NoPushChannel:
    """Stand-in channel when VAPID keys are missing: Realtime still works."""

    name: Literal["webpush"] = "webpush"

    def send(self, recipient: Recipient, notification: Notification) -> DeliveryReport:
        """Report that nothing was sent.

        Args:
            recipient: Ignored.
            notification: Ignored.

        Returns:
            An undelivered report.
        """
        del recipient, notification
        return DeliveryReport(channel="webpush", errors=["web push is not configured"])


def get_notifier(
    settings: Annotated[Settings, Depends(get_settings)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> HandoffNotifier:
    """FastAPI dependency: Web Push + Realtime for handoff events.

    Args:
        settings: Application settings.
        session_factory: Database sessions (subscription lookups).

    Returns:
        The notifier (push disabled when VAPID keys are missing).
    """
    try:
        channel: WebPushChannel | _NoPushChannel = WebPushChannel(
            SqlSubscriptionStore(session_factory), PywebpushSender(settings.require_vapid())
        )
    except ConfigurationError:
        channel = _NoPushChannel()
    return HandoffNotifier(channel=channel, broadcaster=HttpRealtimePublisher(settings))


def dispatch_for_user(user_id: uuid.UUID) -> None:
    """Deliver advisories just queued by a chat reading (background task).

    The scheduler would pick them up within ``DISPATCH_INTERVAL_MINUTES`` anyway; this
    makes a reading reported in chat alert the patient at once.

    Args:
        user_id: Patient.
    """
    try:
        run_dispatch(user_ids=[user_id])
    except ConfigurationError as exc:
        logger.info("advisory dispatch after chat reading skipped: %s", exc)
    except Exception:
        logger.exception("advisory dispatch after chat reading failed")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class MeOut(BaseModel):
    """Which app a login belongs to.

    Attributes:
        role: ``patient`` or ``practitioner``.
        user_id: ``users.id``.
        name: Practitioner's display name (patients: ``None``).
    """

    role: Literal["patient", "practitioner"]
    user_id: uuid.UUID
    name: str | None = None


class HandoffOut(BaseModel):
    """A handoff as the patient sees it.

    Attributes:
        id: Handoff id.
        status: ``open``, ``assigned`` or ``resolved``.
        trigger: Why it was opened.
        has_practitioner: A practitioner is linked to the patient *now* (current
            assignment, not the handoff's state when it was opened).
        practitioner_name: Who joined the chat (``assigned`` handoffs only).
    """

    id: uuid.UUID
    status: HandoffStatus
    trigger: HandoffTrigger
    has_practitioner: bool
    practitioner_name: str | None = None


class ChatMessageOut(BaseModel):
    """One chat bubble.

    Attributes:
        id: Stable id (turn id + side).
        role: Who wrote it.
        text: Message text.
        spoken: The patient sent it as voice (text is the transcript).
        voice_reply: An advisor reply to a voice message: the page shows Play.
        audio_url: Signed mp3 of that reply (``None``: play with the browser voice).
        created_at: When.
        handoff_id: Handoff it belongs to.
    """

    id: str
    role: Literal["patient", "assistant", "practitioner", "system"]
    text: str
    spoken: bool = False
    voice_reply: bool = False
    audio_url: str | None = None
    created_at: AwareDatetime
    handoff_id: uuid.UUID | None = None


class ChatReplyOut(BaseModel):
    """Result of ``POST /api/chat``.

    Attributes:
        turn_id: Audited turn.
        transcript: What was heard, for voice messages.
        reply: Reply text.
        intent: Classified intent.
        escalated: A practitioner was brought in.
        handoff: Live handoff after this turn.
        audio_url: Signed mp3 of the spoken reply (voice messages only).
        audio_url_expires_in_s: Lifetime of ``audio_url``.
        duplicate: The message id was seen before; this is the stored reply.
    """

    turn_id: uuid.UUID
    transcript: str | None = None
    reply: str
    intent: TurnIntent | None
    escalated: bool
    handoff: HandoffOut | None = None
    audio_url: str | None = None
    audio_url_expires_in_s: int | None = None
    duplicate: bool = False


class ConversationOut(BaseModel):
    """The patient's recent conversation.

    Attributes:
        messages: Oldest first.
        handoff: Live handoff, if any.
    """

    messages: list[ChatMessageOut]
    handoff: HandoffOut | None


class HandoffDetailOut(BaseModel):
    """One handoff with the patient's conversation (practitioner view).

    Attributes:
        handoff: The handoff.
        messages: The patient's recent conversation, oldest first.
    """

    handoff: HandoffSummary
    messages: list[ChatMessageOut]


class PractitionerMessageIn(BaseModel):
    """Body of a practitioner reply."""

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)


class ResolveIn(BaseModel):
    """Body of a resolve call."""

    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def reply_audio_path(user_id: uuid.UUID, turn_id: uuid.UUID) -> str:
    """Object path of the spoken reply to a voice message.

    Args:
        user_id: Patient (folder).
        turn_id: Turn (file name).

    Returns:
        ``"chat/<user_id>/<turn_id>.mp3"``.
    """
    return f"chat/{user_id}/{turn_id}.mp3"


def _messages(
    turns: list[ConversationTurn], audio_urls: dict[str, str] | None = None
) -> list[ChatMessageOut]:
    """Turn audited rows into chat bubbles.

    Args:
        turns: Turns, oldest first.
        audio_urls: Signed URLs by :func:`reply_audio_path` (patient view only).

    Returns:
        Bubbles: inbound rows give the patient message and the bot reply; outbound
        rows give the practitioner's reply, or a system notice ("... has joined").
    """
    out: list[ChatMessageOut] = []
    for t in turns:
        if t.direction == "outbound":
            if t.response_text:
                out.append(
                    ChatMessageOut(
                        id=f"{t.id}:{'s' if t.model == SYSTEM_MODEL else 'p'}",
                        role="system" if t.model == SYSTEM_MODEL else "practitioner",
                        text=t.response_text,
                        created_at=t.created_at,
                        handoff_id=t.handoff_id,
                    )
                )
            continue
        said = t.transcript if t.transcript is not None else t.raw_text
        if said:
            out.append(
                ChatMessageOut(
                    id=f"{t.id}:u",
                    role="patient",
                    text=said,
                    spoken=t.transcript is not None,
                    created_at=t.created_at,
                    handoff_id=t.handoff_id,
                )
            )
        if t.response_text:
            voice = t.transcript is not None
            out.append(
                ChatMessageOut(
                    id=f"{t.id}:a",
                    role="assistant",
                    text=t.response_text,
                    voice_reply=voice,
                    audio_url=(
                        (audio_urls or {}).get(reply_audio_path(t.user_id, t.id)) if voice else None
                    ),
                    created_at=t.created_at,
                    handoff_id=t.handoff_id,
                )
            )
    return out


def _reply_audio_urls(
    turns: list[ConversationTurn], audio_store: AudioStore, ttl: int
) -> dict[str, str]:
    """Sign the spoken replies among ``turns`` in one Storage call.

    Args:
        turns: The patient's turns.
        audio_store: Private storage.
        ttl: URL lifetime.

    Returns:
        URLs by path; empty if signing fails (the page then uses the browser voice).
    """
    paths = [
        reply_audio_path(t.user_id, t.id)
        for t in turns
        if t.direction == "inbound" and t.transcript is not None and t.response_text
    ]
    if not paths:
        return {}
    try:
        return audio_store.signed_urls(paths, ttl)
    except AudioStorageError as exc:
        logger.warning("spoken replies not signed: %s", exc)
        return {}


def _handoff_out(session: Session, row: Handoff | None) -> HandoffOut | None:
    """Patient view of a handoff row.

    Args:
        session: Open session.
        row: The row, or ``None``.

    Returns:
        The view, or ``None``.
    """
    if row is None:
        return None
    return HandoffOut(
        id=row.id,
        status=row.status,
        trigger=row.trigger,
        has_practitioner=(
            row.practitioner_id is not None
            or assigned_practitioner_id(session, row.user_id) is not None
        ),
        practitioner_name=(
            practitioner_name(session, row.practitioner_id) if row.status == "assigned" else None
        ),
    )


def _live_handoff_row(session: Session, user_id: uuid.UUID) -> Handoff | None:
    """Return the patient's live handoff row.

    Args:
        session: Open session.
        user_id: Patient.

    Returns:
        The row, or ``None``.
    """
    return session.scalar(
        select(Handoff).where(Handoff.user_id == user_id, Handoff.status.in_(LIVE_STATUSES))
    )


def _require_patient(session: Session, auth_user: AuthUser) -> User:
    """Return the linked patient or refuse.

    Args:
        session: Open session.
        auth_user: Verified login.

    Returns:
        The patient.

    Raises:
        HTTPException: 403 if the login is not linked to a patient.
    """
    patient = find_patient_by_auth_user(session, auth_user.id)
    if patient is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_LINKED)
    return patient


def _require_practitioner(session: Session, auth_user: AuthUser) -> Practitioner:
    """Return the linked practitioner or refuse.

    Args:
        session: Open session.
        auth_user: Verified login.

    Returns:
        The practitioner.

    Raises:
        HTTPException: 403 if the login is not linked to an active practitioner.
    """
    practitioner = find_practitioner_by_auth_user(session, auth_user.id)
    if practitioner is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_PRACTITIONER)
    return practitioner


def _read_audio(audio: UploadFile, max_bytes: int) -> bytes:
    """Read and check an uploaded voice message.

    Args:
        audio: The upload.
        max_bytes: Size limit.

    Returns:
        The bytes.

    Raises:
        HTTPException: 415 for a non-audio type, 413 when too large, 422 when empty.
    """
    content_type = (audio.content_type or "").split(";")[0].strip().lower()
    if content_type not in _AUDIO_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"unsupported audio type {content_type!r}"
        )
    data = audio.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "voice message too long: keep it under about a minute",
        )
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "the recording is empty")
    return data


def _spoken_reply_url(
    *,
    text: str,
    user_id: uuid.UUID,
    turn_id: uuid.UUID,
    synthesizer: SpeechSynthesizer,
    audio_store: AudioStore,
    ttl: int,
) -> str | None:
    """Speak the reply into the private bucket and sign a short-lived URL.

    Args:
        text: Reply text.
        user_id: Patient (folder).
        turn_id: Turn (file name).
        synthesizer: TTS.
        audio_store: Private storage.
        ttl: URL lifetime.

    Returns:
        The URL, or ``None`` on failure (the page then uses speechSynthesis).
    """
    # The "Source: ..." line is for reading, not for listening.
    spoken = text.split("\n\nSource:", 1)[0]
    path = reply_audio_path(user_id, turn_id)
    try:
        audio_store.upload(path, synthesizer.synthesize(spoken, "en"))
        return audio_store.signed_url(path, ttl)
    except (SpeechError, AudioStorageError) as exc:
        logger.warning("spoken reply for turn %s unavailable: %s", turn_id, exc)
        return None


def _valid_advisory_id(
    session: Session, advisory_id: uuid.UUID | None, user_id: uuid.UUID
) -> uuid.UUID | None:
    """Keep an advisory id only if it is the patient's own.

    Args:
        session: Open session.
        advisory_id: Id from the form.
        user_id: Patient.

    Returns:
        The id, or ``None`` if missing or someone else's.
    """
    if advisory_id is None:
        return None
    owner = session.scalar(select(Advisory.user_id).where(Advisory.id == advisory_id))
    return advisory_id if owner == user_id else None


def _notify_after_turn(
    outcome: ChatOutcome,
    session_factory: sessionmaker[Session],
    notifier: HandoffNotifier,
) -> None:
    """Background task: alert the practitioner about this turn.

    Args:
        outcome: The decided turn.
        session_factory: Database sessions.
        notifier: Push + Realtime.
    """
    handoff = outcome.handoff
    if handoff is None or handoff.practitioner_id is None:
        return
    with session_factory() as session:
        contact = practitioner_contact(session, handoff.practitioner_id)
    if contact is None:
        return
    if outcome.red_flag_level == "emergency":
        kind = "red_flag"
    elif outcome.red_flag_level == "urgent":
        kind = "urgent_reading"
    elif (
        outcome.handoff_created
        or handoff.practitioner_just_linked
        or outcome.intent == "practitioner_request"
    ):
        kind = "new"
    elif outcome.relayed:
        kind = "message"
    else:
        # The chat answered this itself (handoff not claimed yet): refresh the
        # practitioner's page, but no push for every message.
        notifier.practitioner_refresh(contact, handoff_id=handoff.id)
        return
    notifier.practitioner_alert(contact, handoff_id=handoff.id, kind=kind, urgent=outcome.urgent)


# ---------------------------------------------------------------------------
# Routes: who am I
# ---------------------------------------------------------------------------


@router.get("/api/me")
def me(
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> MeOut:
    """Tell the web app whether this login is a patient or a practitioner.

    Args:
        auth_user: Verified login.
        session_factory: Database sessions.

    Returns:
        The role.

    Raises:
        HTTPException: 403 if the login is linked to neither.
    """
    with session_factory() as session:
        patient = find_patient_by_auth_user(session, auth_user.id)
        if patient is not None:
            return MeOut(role="patient", user_id=patient.id)
        practitioner = find_practitioner_by_auth_user(session, auth_user.id)
        if practitioner is not None:
            return MeOut(
                role="practitioner", user_id=practitioner.user_id, name=practitioner.full_name
            )
    raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_LINKED)


# ---------------------------------------------------------------------------
# Routes: patient chat
# ---------------------------------------------------------------------------


@router.post("/api/chat")
def post_chat(  # noqa: PLR0917 - FastAPI injects each dependency as a parameter
    background: BackgroundTasks,
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    settings: Annotated[Settings, Depends(get_settings)],
    kb_cache: Annotated[KbIndexCache, Depends(get_kb_cache)],
    rephraser: Annotated[Rephraser | None, Depends(get_rephraser)],
    transcriber: Annotated[SpeechTranscriber, Depends(get_transcriber)],
    synthesizer: Annotated[SpeechSynthesizer, Depends(get_synthesizer)],
    audio_store: Annotated[AudioStore, Depends(get_audio_store)],
    notifier: Annotated[HandoffNotifier, Depends(get_notifier)],
    text: Annotated[str | None, Form(max_length=MAX_TEXT_CHARS)] = None,
    audio: Annotated[UploadFile | None, File()] = None,
    message_id: Annotated[str | None, Form(pattern=_MESSAGE_ID_PATTERN)] = None,
    request_practitioner: Annotated[bool, Form()] = False,
    advisory_id: Annotated[uuid.UUID | None, Form()] = None,
) -> ChatReplyOut:
    """Handle one patient message (typed or spoken) and audit the turn.

    Args:
        background: Post-commit tasks (notifications, advisory dispatch).
        auth_user: Verified login.
        session_factory: Database sessions.
        settings: Settings.
        kb_cache: TF-IDF index cache.
        rephraser: Optional LLM rephraser.
        transcriber: Speech-to-text.
        synthesizer: Text-to-speech for spoken replies.
        audio_store: Private audio storage.
        notifier: Push + Realtime.
        text: Typed message.
        audio: Recorded voice message.
        message_id: Client-generated id (retries return the stored reply).
        request_practitioner: The "Talk to a doctor" button was used.
        advisory_id: Advisory the button was on.

    Returns:
        The reply.

    Raises:
        HTTPException: 401/403 without a linked patient login, 413/415/422 for bad
            audio, 422 for an empty message, 503 if transcription is unavailable.
    """
    with session_factory() as session:
        patient = _require_patient(session, auth_user)
        if message_id is not None:
            earlier = turn_by_message_id(session, patient.id, message_id)
            if earlier is not None:
                return ChatReplyOut(
                    turn_id=earlier.id,
                    transcript=earlier.transcript,
                    reply=earlier.response_text or "",
                    intent=earlier.intent,
                    escalated=earlier.escalated,
                    handoff=_handoff_out(session, _live_handoff_row(session, patient.id)),
                    duplicate=True,
                )

    # Transcribe outside any transaction: it can take a few seconds.
    transcript: str | None = None
    if audio is not None:
        data = _read_audio(audio, settings.chat_audio_max_bytes)
        try:
            transcript = transcriber.transcribe(data, "en")
        except SpeechError as exc:
            logger.warning("voice message from user %s not transcribed: %s", patient.id, exc)
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "sorry, I could not understand that recording; please try again or type",
            ) from exc
        if not transcript:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "I could not hear anything; please try again"
            )
    message = transcript if transcript is not None else (text or "").strip()
    if not message and not request_practitioner:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "send a message or a recording")

    with session_factory() as session:
        store = SqlChatStore(session, settings)
        own_advisory = _valid_advisory_id(session, advisory_id, patient.id)
        engine = ChatEngine(
            store, kb_cache, min_similarity=settings.chat_kb_min_similarity, rephraser=rephraser
        )
        outcome = engine.handle(
            ChatRequest(
                user_id=patient.id,
                text=message,
                spoken=transcript is not None,
                practitioner_requested=request_practitioner,
                advisory_id=own_advisory,
                message_id=message_id,
            )
        )
        turn = save_turn(
            session,
            user_id=patient.id,
            direction="inbound",
            raw_text=None if transcript is not None else (message or None),
            transcript=transcript,
            intent=outcome.intent,
            response_text=outcome.reply_text,
            model=outcome.model,
            model_version=outcome.model_version,
            latency_ms=outcome.latency_ms,
            escalated=outcome.escalated,
            kb_ids_used=outcome.kb_ids_used,
            kb_version=outcome.kb_version,
            retrieval_confidence=outcome.retrieval_confidence,
            handoff_id=outcome.handoff.id if outcome.handoff else None,
            advisory_id=own_advisory,
            message_id=message_id,
        )
        live = _live_handoff_row(session, patient.id)
        handoff = _handoff_out(session, live)
        session.commit()

    if outcome.notify_practitioner:
        background.add_task(_notify_after_turn, outcome, session_factory, notifier)
    if outcome.reading is not None and outcome.reading.advisory_queued:
        background.add_task(dispatch_for_user, patient.id)

    audio_url: str | None = None
    ttl = settings.advisory_audio_url_ttl_seconds
    if transcript is not None:
        audio_url = _spoken_reply_url(
            text=outcome.reply_text,
            user_id=patient.id,
            turn_id=turn.id,
            synthesizer=synthesizer,
            audio_store=audio_store,
            ttl=ttl,
        )
    return ChatReplyOut(
        turn_id=turn.id,
        transcript=transcript,
        reply=outcome.reply_text,
        intent=outcome.intent,
        escalated=outcome.escalated,
        handoff=handoff,
        audio_url=audio_url,
        audio_url_expires_in_s=ttl if audio_url else None,
    )


@router.get("/api/chat/messages")
def get_messages(
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    settings: Annotated[Settings, Depends(get_settings)],
    audio_store: Annotated[AudioStore, Depends(get_audio_store)],
) -> ConversationOut:
    """Return the signed-in patient's recent conversation.

    Args:
        auth_user: Verified login.
        session_factory: Database sessions.
        settings: Settings (URL lifetime).
        audio_store: Private audio storage (spoken replies).

    Returns:
        Messages (own turns only) and the live handoff.

    Raises:
        HTTPException: 401/403 without a linked patient login.
    """
    with session_factory() as session:
        patient = _require_patient(session, auth_user)
        turns = recent_turns(session, patient.id)
        handoff = _handoff_out(session, _live_handoff_row(session, patient.id))
    urls = _reply_audio_urls(turns, audio_store, settings.advisory_audio_url_ttl_seconds)
    return ConversationOut(messages=_messages(turns, urls), handoff=handoff)


# ---------------------------------------------------------------------------
# Routes: practitioner
# ---------------------------------------------------------------------------


@router.get("/api/practitioner/handoffs")
def list_handoffs(
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> list[HandoffSummary]:
    """List handoffs of the practitioner's assigned patients.

    Args:
        auth_user: Verified login.
        session_factory: Database sessions.

    Returns:
        Live handoffs first, then recently resolved ones.

    Raises:
        HTTPException: 401/403 without a linked practitioner login.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        return practitioner_handoffs(session, practitioner.id)


def _summary(session: Session, practitioner_id: uuid.UUID, handoff_id: uuid.UUID) -> HandoffSummary:
    """Find one handoff in the practitioner's list.

    Args:
        session: Open session.
        practitioner_id: Practitioner.
        handoff_id: Handoff.

    Returns:
        Its summary.

    Raises:
        HTTPException: 404 if it is not in their list.
    """
    for item in practitioner_handoffs(session, practitioner_id):
        if item.id == handoff_id:
            return item
    raise HTTPException(status.HTTP_404_NOT_FOUND, "handoff not found")


@router.get("/api/practitioner/handoffs/{handoff_id}")
def get_handoff(
    handoff_id: uuid.UUID,
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> HandoffDetailOut:
    """Return one handoff with the patient's recent conversation.

    Args:
        handoff_id: Handoff.
        auth_user: Verified login.
        session_factory: Database sessions.

    Returns:
        The handoff and messages.

    Raises:
        HTTPException: 403 without a practitioner login, 404 if the handoff does not
            exist or its patient is not assigned to this practitioner.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        handoff = handoff_for_practitioner(session, handoff_id, practitioner.id)
        if handoff is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "handoff not found")
        detail = HandoffDetailOut(
            handoff=_summary(session, practitioner.id, handoff_id),
            messages=_messages(recent_turns(session, handoff.user_id)),
        )
        session.rollback()  # release the row lock; nothing was changed
        return detail


@router.post("/api/practitioner/handoffs/{handoff_id}/messages", status_code=201)
def reply_to_handoff(  # noqa: PLR0917 - FastAPI dependency injection
    handoff_id: uuid.UUID,
    body: PractitionerMessageIn,
    background: BackgroundTasks,
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    notifier: Annotated[HandoffNotifier, Depends(get_notifier)],
) -> ChatMessageOut:
    """Send a practitioner reply to the patient (first reply: ``open -> assigned``).

    Args:
        handoff_id: Handoff.
        body: Reply text.
        background: Post-commit notification.
        auth_user: Verified login.
        session_factory: Database sessions.
        notifier: Push + Realtime.

    Returns:
        The stored reply.

    Raises:
        HTTPException: 403 without a practitioner login, 404 if not theirs, 409 if the
            handoff is resolved.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        handoff = handoff_for_practitioner(session, handoff_id, practitioner.id)
        if handoff is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "handoff not found")
        if handoff.status == "resolved":
            raise HTTPException(status.HTTP_409_CONFLICT, "this handoff is already resolved")
        _refuse_if_claimed_by_someone_else(handoff, practitioner.id)
        # The database clock, like every other turn's created_at, so the notice and the
        # reply sort correctly against the patient's messages.
        now = db_now(session)
        first_reply = not has_replied(session, handoff.id, practitioner.id)
        claim_handoff(session, handoff, practitioner.id, now)
        if first_reply:
            save_turn(
                session,
                user_id=handoff.user_id,
                direction="outbound",
                raw_text=None,
                transcript=None,
                intent=None,
                response_text=f"{practitioner.full_name} has joined the chat",
                model=SYSTEM_MODEL,
                model_version=None,
                latency_ms=None,
                escalated=False,
                handoff_id=handoff.id,
                created_at=now,
            )
        turn = save_turn(
            session,
            user_id=handoff.user_id,
            direction="outbound",
            raw_text=None,
            transcript=None,
            intent=None,
            response_text=body.text.strip(),
            model="practitioner",
            model_version=None,
            latency_ms=None,
            escalated=False,
            handoff_id=handoff.id,
            author_practitioner_id=practitioner.id,
            # Just after the notice (same transaction, so now() would tie).
            created_at=now + timedelta(microseconds=1),
        )
        patient = user_contact(session, handoff.user_id)
        session.commit()
    if patient is not None:
        background.add_task(notifier.patient_reply, patient, handoff_id=handoff_id)
    logger.info("practitioner %s replied on handoff %s", practitioner.id, handoff_id)
    return ChatMessageOut(
        id=f"{turn.id}:p",
        role="practitioner",
        text=turn.response_text or "",
        created_at=turn.created_at,
        handoff_id=handoff_id,
    )


def _refuse_if_claimed_by_someone_else(handoff: Handoff, practitioner_id: uuid.UUID) -> None:
    """Keep a claimed handoff with the practitioner who claimed it.

    Args:
        handoff: Live handoff.
        practitioner_id: Practitioner acting.

    Raises:
        HTTPException: 409 if another practitioner claimed it.
    """
    if handoff.status == "assigned" and handoff.practitioner_id != practitioner_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "another practitioner has already claimed this handoff"
        )


@router.post("/api/practitioner/handoffs/{handoff_id}/claim")
def claim(
    handoff_id: uuid.UUID,
    background: BackgroundTasks,
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    notifier: Annotated[HandoffNotifier, Depends(get_notifier)],
) -> HandoffSummary:
    """Claim a handoff (``open -> assigned``): the patient's messages now come to you.

    Idempotent for the practitioner who already claimed it.

    Args:
        handoff_id: Handoff.
        background: Post-commit Realtime ping to the patient (status changes).
        auth_user: Verified login.
        session_factory: Database sessions.
        notifier: Realtime.

    Returns:
        The updated handoff.

    Raises:
        HTTPException: 403 without a practitioner login, 404 if not theirs, 409 if
            resolved or claimed by another practitioner.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        handoff = handoff_for_practitioner(session, handoff_id, practitioner.id)
        if handoff is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "handoff not found")
        if handoff.status == "resolved":
            raise HTTPException(status.HTTP_409_CONFLICT, "this handoff is already resolved")
        _refuse_if_claimed_by_someone_else(handoff, practitioner.id)
        claimed = claim_handoff(session, handoff, practitioner.id, db_now(session))
        patient = user_contact(session, handoff.user_id)
        session.commit()
        summary = _summary(session, practitioner.id, handoff_id)
    if claimed and patient is not None:
        background.add_task(notifier.patient_ping, patient, handoff_id=handoff_id)
    logger.info("practitioner %s claimed handoff %s", practitioner.id, handoff_id)
    return summary


@router.post("/api/practitioner/handoffs/{handoff_id}/resolve")
def resolve(  # noqa: PLR0917 - FastAPI dependency injection
    handoff_id: uuid.UUID,
    body: ResolveIn,
    background: BackgroundTasks,
    auth_user: Annotated[AuthUser, Depends(current_auth_user)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    notifier: Annotated[HandoffNotifier, Depends(get_notifier)],
) -> HandoffSummary:
    """Resolve a handoff (``open``/``assigned`` -> ``resolved``).

    Args:
        handoff_id: Handoff.
        body: Optional resolution note.
        background: Post-commit Realtime ping to the patient.
        auth_user: Verified login.
        session_factory: Database sessions.
        notifier: Realtime.

    Returns:
        The updated handoff.

    Raises:
        HTTPException: 403 without a practitioner login, 404 if not theirs, 409 if
            already resolved.
    """
    with session_factory() as session:
        practitioner = _require_practitioner(session, auth_user)
        handoff = handoff_for_practitioner(session, handoff_id, practitioner.id)
        if handoff is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "handoff not found")
        if handoff.status == "resolved":
            raise HTTPException(status.HTTP_409_CONFLICT, "this handoff is already resolved")
        resolve_handoff(
            session,
            handoff,
            practitioner.id,
            note=body.note.strip() if body.note else None,
            at=datetime.now(UTC),
        )
        patient = user_contact(session, handoff.user_id)
        session.commit()
        summary = _summary(session, practitioner.id, handoff_id)
    if patient is not None:
        background.add_task(notifier.patient_ping, patient, handoff_id=handoff_id)
    logger.info("practitioner %s resolved handoff %s", practitioner.id, handoff_id)
    return summary
