"""Database access for the help chat and practitioner connect (R4) and its audit (R5).

:class:`SqlChatStore` implements :class:`app.services.chat.ChatStore` on one session:
approved knowledge-base entries, the patient's live handoff, and chat readings (stored
with source ``web``, classified right away, advisory queued if needed). The module
functions serve the chat and practitioner API routes. Nothing here commits: the route
owns the transaction, so a turn and everything it caused commit together.

Access rules enforced here (the backend bypasses RLS, so these mirror 0002):

* a patient only ever reads their own turns;
* a practitioner only sees handoffs of patients in ``patient_assignments`` for them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from app.core.clinical_limits import GlucoseLimitKind
from app.core.config import Settings
from app.db.batch_repository import SqlBatchRepository
from app.db.models import (
    ConversationTurn,
    Handoff,
    HandoffStatus,
    HandoffTrigger,
    KbEntry,
    PatientAssignment,
    Practitioner,
    Profile,
    Reading,
    ReadingContext,
    RiskAssessmentRecord,
    TurnDirection,
    TurnIntent,
    User,
)
from app.db.threshold_repository import SqlThresholdRepository
from app.ml.predict import RiskAssessment
from app.services.advisory import EvaluatedReading, plan_advisories
from app.services.chat import (
    HandoffInfo,
    KbDoc,
    KbVersion,
    ParsedReading,
    ReadingOutcome,
)
from app.services.thresholds import (
    ReadingPoint,
    ThresholdInputs,
    classify_reading,
    refresh_user_thresholds,
    threshold_values,
)

LIVE_STATUSES: Final[tuple[HandoffStatus, ...]] = ("open", "assigned")
# numeric(5, 1) mg/dL, like every glucose column.
_GLUCOSE_STEP: Final = Decimal("0.1")
_UPPER_KIND: Final[dict[ReadingContext, GlucoseLimitKind | None]] = {
    "fasting": "fasting_high",
    "post_meal": "post_meal_high",
    "random": None,
}
# The practitioner list shows live handoffs plus the most recently resolved few.
_RECENT_RESOLVED: Final = 10


def _live_filter() -> object:
    """SQL condition for a live handoff.

    Returns:
        ``status in ('open', 'assigned')``.
    """
    return Handoff.status.in_(LIVE_STATUSES)


def _handoff_info(row: Handoff, *, just_linked: bool = False) -> HandoffInfo:
    """Convert a ``handoffs`` row.

    Args:
        row: The row.
        just_linked: The row got its practitioner in this call.

    Returns:
        The engine's view of it.
    """
    return HandoffInfo(
        id=row.id,
        status=row.status,
        trigger=row.trigger,
        practitioner_id=row.practitioner_id,
        practitioner_just_linked=just_linked,
    )


class SqlChatStore:
    """:class:`app.services.chat.ChatStore` on one SQLAlchemy session.

    Args:
        session: Open session (the caller commits).
        settings: Band and cooldown configuration.
        clock: Current time (tests pass a fixed one).
    """

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock

    # --- knowledge base ---------------------------------------------------------

    def kb_version(self) -> KbVersion:
        """Fingerprint the approved KB (cheap: one aggregate query).

        Returns:
            Highest approved version, approved-row count and latest change time.
        """
        row = self._session.execute(
            select(
                func.max(KbEntry.kb_version),
                func.count(KbEntry.id),
                func.max(KbEntry.updated_at),
            ).where(KbEntry.status == "approved")
        ).one()
        return KbVersion(kb_version=row[0], entries=int(row[1]), last_change=row[2])

    def approved_kb_entries(self) -> list[KbDoc]:
        """Return every approved entry (only these are ever served, R5).

        Returns:
            Entries ordered by key, for a stable index.
        """
        rows = self._session.scalars(
            select(KbEntry).where(KbEntry.status == "approved").order_by(KbEntry.entry_key)
        )
        return [
            KbDoc(
                id=r.id,
                entry_key=r.entry_key,
                title=r.title,
                body=r.body,
                source_citation=r.source_citation,
                # DB CHECK: approved rows always carry a version.
                kb_version=r.kb_version or 0,
            )
            for r in rows
        ]

    # --- handoffs -----------------------------------------------------------------

    def live_handoff(self, user_id: uuid.UUID) -> HandoffInfo | None:
        """Return the patient's open/assigned handoff.

        Args:
            user_id: Patient.

        Returns:
            The handoff, or ``None``.
        """
        row = self._session.scalar(
            select(Handoff).where(Handoff.user_id == user_id, _live_filter())  # type: ignore[arg-type]
        )
        return _handoff_info(row) if row is not None else None

    def open_handoff(
        self,
        user_id: uuid.UUID,
        *,
        trigger: HandoffTrigger,
        reason: str,
        advisory_id: uuid.UUID | None,
    ) -> tuple[HandoffInfo, bool]:
        """Open a handoff unless one is live; race-safe on the partial unique index.

        ``handoffs_one_live_per_user`` (0001) allows one open/assigned row per patient.
        ``ON CONFLICT DO NOTHING`` on that index means two simultaneous requests end
        with one handoff, and the loser simply reads it. A live row that is still
        ``open`` is then pointed at the patient's current practitioner
        (:func:`route_open_handoff`), so a handoff opened before anyone was linked is
        assigned as soon as someone is.

        Args:
            user_id: Patient.
            trigger: Why.
            reason: Short fixed reason for the practitioner.
            advisory_id: Advisory the request came from, if any.

        Returns:
            The live handoff and whether this call created it.
        """
        practitioner_id = assigned_practitioner_id(self._session, user_id)
        stmt = (
            pg_insert(Handoff)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                practitioner_id=practitioner_id,
                advisory_id=advisory_id,
                trigger=trigger,
                status="open",
                reason=reason,
                opened_at=self._clock(),
            )
            .on_conflict_do_nothing(
                index_elements=[Handoff.user_id],
                index_where=Handoff.status.in_(LIVE_STATUSES),
            )
            .returning(Handoff.id)
        )
        created_id = self._session.execute(stmt).scalar_one_or_none()
        row = self._session.scalar(
            select(Handoff)
            .where(Handoff.user_id == user_id, _live_filter())  # type: ignore[arg-type]
            .with_for_update()
        )
        if row is None:  # pragma: no cover - the insert or the conflicting row exists
            raise RuntimeError("no live handoff after insert")
        just_linked = created_id is None and route_open_handoff(self._session, row)
        return _handoff_info(row, just_linked=just_linked), created_id is not None

    # --- readings -------------------------------------------------------------------

    def record_reading(
        self, user_id: uuid.UUID, reading: ParsedReading, *, message_id: str | None
    ) -> ReadingOutcome:
        """Store a chat reading, classify it and queue an advisory if needed (R1-R3).

        Thresholds come from ``current_thresholds``; a patient who has none yet (never
        processed by the batch job) gets them computed now from the profile and the
        latest stored risk assessment, exactly as the batch job would.

        Args:
            user_id: Patient.
            reading: Parsed value and context.
            message_id: Client message id (``readings.source_message_id``).

        Returns:
            The outcome; ``state`` is ``None`` when no limits can be computed yet.
        """
        now = self._clock()
        value = reading.value_mg_dl.quantize(_GLUCOSE_STEP)
        row = Reading(
            id=uuid.uuid4(),
            user_id=user_id,
            context=reading.context,
            value_mg_dl=value,
            measured_at=now,
            source="web",
            source_message_id=message_id,
        )
        self._session.add(row)
        self._session.flush()

        profile = self._session.get(Profile, user_id)
        if profile is None or profile.diabetes_status is None:
            return ReadingOutcome(reading_id=row.id, state=None)
        repo = SqlThresholdRepository(self._session)
        current = repo.current_thresholds(user_id)
        if len(current) < len(_UPPER_KIND):
            refresh_user_thresholds(
                repo,
                user_id=user_id,
                inputs=ThresholdInputs.model_validate(profile),
                risk=self._latest_risk(user_id),
                as_of=now,
            )
            current = repo.current_thresholds(user_id)

        values = threshold_values(current)
        band = classify_reading(
            ReadingPoint(value_mg_dl=value, context=reading.context, measured_at=now),
            values,
            band=self._settings.pre_breach_band,
        )
        upper_kind = _UPPER_KIND[reading.context]
        outcome = ReadingOutcome(
            reading_id=row.id,
            state=band.state,
            threshold_mg_dl=band.threshold_mg_dl,
            upper_mg_dl=values.get(upper_kind) if upper_kind is not None else None,
            low_mg_dl=values.get("low"),
            escalated=band.needs_practitioner,
        )
        if band.state == "normal":
            return outcome

        # R3: same planner as the batch job (de-duplication + cooldown), so a chat
        # reading and a later batch run over it can never raise two advisories.
        batch_repo = SqlBatchRepository(self._session)
        cooldown = timedelta(hours=self._settings.advisory_cooldown_hours)
        plan = plan_advisories(
            [
                EvaluatedReading(
                    reading_id=row.id,
                    measured_at=now,
                    context=reading.context,
                    value_mg_dl=value,
                    band=band,
                )
            ],
            batch_repo.prior_advisories(user_id, now - cooldown, now + cooldown),
            cooldown=cooldown,
            preferred_language=profile.language,
        )
        created = batch_repo.insert_advisories(
            user_id=user_id,
            batch_run_id=None,
            drafts=plan.drafts,
            threshold_ids={kind: t.id for kind, t in current.items()},
        )
        return ReadingOutcome(
            reading_id=outcome.reading_id,
            state=outcome.state,
            threshold_mg_dl=outcome.threshold_mg_dl,
            upper_mg_dl=outcome.upper_mg_dl,
            low_mg_dl=outcome.low_mg_dl,
            advisory_queued=bool(created),
            escalated=outcome.escalated,
        )

    def _latest_risk(self, user_id: uuid.UUID) -> RiskAssessment | None:
        """Return the latest stored risk assessment (never re-scores here).

        Args:
            user_id: Patient.

        Returns:
            The assessment, or ``None`` if never scored.
        """
        latest = self._session.scalars(
            select(RiskAssessmentRecord)
            .where(RiskAssessmentRecord.user_id == user_id)
            .order_by(RiskAssessmentRecord.created_at.desc())
            .limit(1)
        ).first()
        if latest is None:
            return None
        return RiskAssessment(
            probability=float(latest.risk_score),
            tier=latest.risk_tier,
            flagged=latest.flagged,
            model_version=latest.model_version,
        )


# ---------------------------------------------------------------------------
# Users and practitioners
# ---------------------------------------------------------------------------


def find_user_by_auth_user(session: Session, auth_user_id: uuid.UUID) -> User | None:
    """Return the active user (any role) linked to a Supabase login.

    Args:
        session: Open session.
        auth_user_id: ``auth.users.id`` from a verified session.

    Returns:
        The user, or ``None``.
    """
    return session.scalar(select(User).where(User.auth_user_id == auth_user_id, User.is_active))


def find_practitioner_by_auth_user(
    session: Session, auth_user_id: uuid.UUID
) -> Practitioner | None:
    """Return the active practitioner linked to a Supabase login.

    Args:
        session: Open session.
        auth_user_id: ``auth.users.id`` from a verified session.

    Returns:
        The practitioner, or ``None``.
    """
    return session.scalar(
        select(Practitioner)
        .join(User, User.id == Practitioner.user_id)
        .where(
            User.auth_user_id == auth_user_id,
            User.is_active,
            User.role.in_(("practitioner", "admin")),
            Practitioner.is_active,
        )
    )


def assigned_practitioner_id(session: Session, patient_id: uuid.UUID) -> uuid.UUID | None:
    """Return the patient's first-assigned active practitioner.

    Args:
        session: Open session.
        patient_id: Patient.

    Returns:
        The practitioner id, or ``None`` if nobody is assigned.
    """
    return session.scalar(
        select(PatientAssignment.practitioner_id)
        .join(Practitioner, Practitioner.id == PatientAssignment.practitioner_id)
        .where(PatientAssignment.patient_id == patient_id, Practitioner.is_active)
        .order_by(PatientAssignment.assigned_at)
        .limit(1)
    )


def route_open_handoff(session: Session, handoff: Handoff) -> bool:
    """Point an ``open`` handoff at the patient's current practitioner.

    A claimed (``assigned``) handoff keeps its practitioner. Called on every "talk to a
    doctor" and red flag, so the handoff follows the *current* assignment rather than
    whatever was true when it was opened.

    Args:
        session: Open session (caller commits).
        handoff: Live handoff row (locked by the caller).

    Returns:
        ``True`` if the handoff had no practitioner and now has one.
    """
    if handoff.status != "open":
        return False
    current = assigned_practitioner_id(session, handoff.user_id)
    if current == handoff.practitioner_id:
        return False
    was_unrouted = handoff.practitioner_id is None
    handoff.practitioner_id = current
    session.flush()
    return was_unrouted and current is not None


def assign_patients(
    session: Session, practitioner_id: uuid.UUID, patient_ids: Sequence[uuid.UUID]
) -> int:
    """Assign patients to a practitioner and route their unassigned open handoffs.

    The one place assignments are made (``seed_demo --practitioner-email`` and any
    future assignment screen), so a handoff opened while the patient had no
    practitioner is never left unassigned. Idempotent.

    Args:
        session: Open session (caller commits).
        practitioner_id: Practitioner.
        patient_ids: Patients.

    Returns:
        How many open handoffs were routed to the practitioner now.
    """
    if not patient_ids:
        return 0
    session.execute(
        pg_insert(PatientAssignment)
        .values([{"practitioner_id": practitioner_id, "patient_id": p} for p in patient_ids])
        .on_conflict_do_nothing()
    )
    routed = session.execute(
        update(Handoff)
        .where(
            Handoff.user_id.in_(patient_ids),
            Handoff.status == "open",
            Handoff.practitioner_id.is_(None),
        )
        .values(practitioner_id=practitioner_id)
        .returning(Handoff.id)
    ).all()
    session.flush()
    return len(routed)


def practitioner_name(session: Session, practitioner_id: uuid.UUID | None) -> str | None:
    """Return a practitioner's display name.

    Args:
        session: Open session.
        practitioner_id: ``practitioners.id``, or ``None``.

    Returns:
        ``full_name``, or ``None``.
    """
    if practitioner_id is None:
        return None
    return session.scalar(select(Practitioner.full_name).where(Practitioner.id == practitioner_id))


def is_assigned(session: Session, practitioner_id: uuid.UUID, patient_id: uuid.UUID) -> bool:
    """Tell whether a practitioner may see a patient (``patient_assignments``).

    Args:
        session: Open session.
        practitioner_id: Practitioner.
        patient_id: Patient.

    Returns:
        ``True`` if an assignment row exists.
    """
    return (
        session.scalar(
            select(func.count())
            .select_from(PatientAssignment)
            .where(
                PatientAssignment.practitioner_id == practitioner_id,
                PatientAssignment.patient_id == patient_id,
            )
        )
        or 0
    ) > 0


class Contact(BaseModel):
    """What a notification needs about its recipient.

    Attributes:
        user_id: ``users.id``.
        auth_user_id: Linked login (Realtime topic), or ``None``.
        is_demo: ``users.is_demo`` (recipient guard).
    """

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    auth_user_id: uuid.UUID | None
    is_demo: bool


def user_contact(session: Session, user_id: uuid.UUID) -> Contact | None:
    """Return a user's notification contact.

    Args:
        session: Open session.
        user_id: ``users.id``.

    Returns:
        The contact, or ``None`` if the user does not exist.
    """
    user = session.get(User, user_id)
    if user is None:
        return None
    return Contact(user_id=user.id, auth_user_id=user.auth_user_id, is_demo=user.is_demo)


def practitioner_contact(session: Session, practitioner_id: uuid.UUID) -> Contact | None:
    """Return the notification contact of a practitioner's login.

    Args:
        session: Open session.
        practitioner_id: ``practitioners.id``.

    Returns:
        The contact, or ``None``.
    """
    user_id = session.scalar(select(Practitioner.user_id).where(Practitioner.id == practitioner_id))
    return user_contact(session, user_id) if user_id is not None else None


# ---------------------------------------------------------------------------
# Turns (append-only audit, R5)
# ---------------------------------------------------------------------------


def db_now(session: Session) -> datetime:
    """Return the database's current time (``clock_timestamp()``).

    Turns default to the database clock; rows that set ``created_at`` explicitly use
    this, so they sort correctly against the rest of the conversation.

    Args:
        session: Open session.

    Returns:
        Timezone-aware current time.
    """
    value = session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime):  # pragma: no cover - always a timestamptz
        raise TypeError("clock_timestamp() did not return a timestamp")
    return value


def turn_by_message_id(
    session: Session, user_id: uuid.UUID, message_id: str
) -> ConversationTurn | None:
    """Find an earlier turn of this patient with the same client message id.

    Args:
        session: Open session.
        user_id: Patient.
        message_id: Client-generated id.

    Returns:
        The turn, or ``None``.
    """
    return session.scalar(
        select(ConversationTurn).where(
            ConversationTurn.user_id == user_id, ConversationTurn.message_id == message_id
        )
    )


def save_turn(
    session: Session,
    *,
    user_id: uuid.UUID,
    direction: TurnDirection,
    raw_text: str | None,
    transcript: str | None,
    intent: TurnIntent | None,
    response_text: str | None,
    model: str | None,
    model_version: str | None,
    latency_ms: int | None,
    escalated: bool,
    kb_ids_used: list[uuid.UUID] | None = None,
    kb_version: int | None = None,
    retrieval_confidence: float | None = None,
    handoff_id: uuid.UUID | None = None,
    advisory_id: uuid.UUID | None = None,
    message_id: str | None = None,
    author_practitioner_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> ConversationTurn:
    """Append one audited turn (channel ``web``).

    Args:
        session: Open session (caller commits).
        user_id: Patient the conversation belongs to.
        direction: ``inbound`` (patient message + bot reply) or ``outbound``
            (practitioner reply).
        raw_text: Typed text, if typed.
        transcript: Voice transcript, if spoken.
        intent: Classified intent (``None`` for practitioner replies).
        response_text: Reply sent back.
        model: ``rules`` / ``tfidf`` / LLM name / ``handoff_relay`` / ``practitioner``.
        model_version: Version label.
        latency_ms: Time to decide.
        escalated: A practitioner was brought in.
        kb_ids_used: Entries used.
        kb_version: KB release that answered.
        retrieval_confidence: Best retrieval similarity.
        handoff_id: Handoff the turn belongs to.
        advisory_id: Advisory the turn refers to.
        message_id: Client message id (unique).
        author_practitioner_id: Author of an outbound practitioner reply.
        created_at: Override for tests; the database clock otherwise.

    Returns:
        The flushed row.
    """
    turn = ConversationTurn(
        id=uuid.uuid4(),
        user_id=user_id,
        handoff_id=handoff_id,
        advisory_id=advisory_id,
        channel="web",
        direction=direction,
        raw_text=raw_text,
        transcript=transcript,
        intent=intent,
        kb_ids_used=kb_ids_used or [],
        kb_version=kb_version,
        retrieval_confidence=(
            Decimal(str(retrieval_confidence)).quantize(Decimal("0.0001"))
            if retrieval_confidence is not None
            else None
        ),
        model=model,
        model_version=model_version,
        response_text=response_text,
        latency_ms=latency_ms,
        escalated=escalated,
        message_id=message_id,
        author_practitioner_id=author_practitioner_id,
    )
    if created_at is not None:
        turn.created_at = created_at
    session.add(turn)
    session.flush()
    return turn


def recent_turns(
    session: Session,
    user_id: uuid.UUID,
    *,
    limit: int = 50,
    handoff_id: uuid.UUID | None = None,
) -> list[ConversationTurn]:
    """Return a patient's latest turns, oldest first.

    Args:
        session: Open session.
        user_id: Patient (never anyone else's turns).
        limit: How many.
        handoff_id: Only turns of this handoff.

    Returns:
        Turns in chronological order.
    """
    stmt = select(ConversationTurn).where(ConversationTurn.user_id == user_id)
    if handoff_id is not None:
        stmt = stmt.where(ConversationTurn.handoff_id == handoff_id)
    rows = list(
        session.scalars(
            stmt.order_by(ConversationTurn.created_at.desc(), ConversationTurn.id).limit(limit)
        )
    )
    rows.reverse()
    return rows


# ---------------------------------------------------------------------------
# Practitioner side
# ---------------------------------------------------------------------------


class HandoffSummary(BaseModel):
    """One handoff in the practitioner's list.

    Attributes:
        id: Handoff id.
        patient_id: Patient.
        patient_name: ``profiles.full_name`` (assigned patients only).
        status: Status.
        trigger: Why it was opened.
        reason: Fixed reason text.
        practitioner_id: Practitioner it is routed to (the claimer once assigned).
        practitioner_name: That practitioner's name.
        opened_at: Opened.
        assigned_at: Claimed (Claim button or first reply).
        resolved_at: Resolved.
        last_message_at: Latest turn in the handoff.
    """

    id: uuid.UUID
    patient_id: uuid.UUID
    patient_name: str | None
    status: HandoffStatus
    trigger: HandoffTrigger
    reason: str | None
    practitioner_id: uuid.UUID | None = None
    practitioner_name: str | None = None
    opened_at: AwareDatetime
    assigned_at: AwareDatetime | None
    resolved_at: AwareDatetime | None
    last_message_at: AwareDatetime | None


def practitioner_handoffs(session: Session, practitioner_id: uuid.UUID) -> list[HandoffSummary]:
    """List handoffs of the practitioner's assigned patients.

    Live handoffs first (newest first), then the latest few resolved ones.

    Args:
        session: Open session.
        practitioner_id: Practitioner.

    Returns:
        Summaries.
    """
    last_turn = (
        select(
            ConversationTurn.handoff_id,
            func.max(ConversationTurn.created_at).label("last_at"),
        )
        .where(ConversationTurn.handoff_id.is_not(None))
        .group_by(ConversationTurn.handoff_id)
        .subquery()
    )
    routed_to = aliased(Practitioner)
    base = (
        select(Handoff, Profile.full_name, last_turn.c.last_at, routed_to.full_name)
        .join(
            PatientAssignment,
            and_(
                PatientAssignment.patient_id == Handoff.user_id,
                PatientAssignment.practitioner_id == practitioner_id,
            ),
        )
        .outerjoin(Profile, Profile.user_id == Handoff.user_id)
        .outerjoin(last_turn, last_turn.c.handoff_id == Handoff.id)
        .outerjoin(routed_to, routed_to.id == Handoff.practitioner_id)
    )
    live = session.execute(
        base.where(Handoff.status.in_(LIVE_STATUSES)).order_by(Handoff.opened_at.desc())
    ).all()
    resolved = session.execute(
        base.where(Handoff.status == "resolved")
        .order_by(Handoff.resolved_at.desc())
        .limit(_RECENT_RESOLVED)
    ).all()
    return [
        HandoffSummary(
            id=h.id,
            patient_id=h.user_id,
            patient_name=name,
            status=h.status,
            trigger=h.trigger,
            reason=h.reason,
            practitioner_id=h.practitioner_id,
            practitioner_name=routed_name,
            opened_at=h.opened_at,
            assigned_at=h.assigned_at,
            resolved_at=h.resolved_at,
            last_message_at=last_at,
        )
        for h, name, last_at, routed_name in [*live, *resolved]
    ]


def handoff_for_practitioner(
    session: Session, handoff_id: uuid.UUID, practitioner_id: uuid.UUID
) -> Handoff | None:
    """Load a handoff only if its patient is assigned to the practitioner.

    Args:
        session: Open session.
        handoff_id: Handoff.
        practitioner_id: Practitioner asking.

    Returns:
        The row (locked for update), or ``None`` if it does not exist or the patient
        is not theirs (the API answers 404 either way, so ids cannot be probed).
    """
    return session.scalar(
        select(Handoff)
        .join(
            PatientAssignment,
            and_(
                PatientAssignment.patient_id == Handoff.user_id,
                PatientAssignment.practitioner_id == practitioner_id,
            ),
        )
        .where(Handoff.id == handoff_id)
        .with_for_update(of=Handoff)
    )


def claim_handoff(
    session: Session, handoff: Handoff, practitioner_id: uuid.UUID, at: datetime
) -> bool:
    """Move an open handoff to ``assigned`` to this practitioner (Claim or first reply).

    From then on the patient's messages are relayed to them (route d).

    Args:
        session: Open session (caller commits).
        handoff: Live handoff (already access-checked).
        practitioner_id: Practitioner claiming it.
        at: Time.

    Returns:
        ``True`` if this call claimed it; ``False`` if it was already assigned.
    """
    if handoff.status != "open":
        return False
    handoff.status = "assigned"
    handoff.practitioner_id = practitioner_id
    handoff.assigned_at = at
    session.flush()
    return True


def has_replied(session: Session, handoff_id: uuid.UUID, practitioner_id: uuid.UUID) -> bool:
    """Tell whether a practitioner already wrote in this handoff.

    Args:
        session: Open session.
        handoff_id: Handoff.
        practitioner_id: Practitioner.

    Returns:
        ``True`` if an outbound turn by them exists in the handoff.
    """
    return (
        session.scalar(
            select(func.count())
            .select_from(ConversationTurn)
            .where(
                ConversationTurn.handoff_id == handoff_id,
                ConversationTurn.author_practitioner_id == practitioner_id,
            )
        )
        or 0
    ) > 0


def resolve_handoff(
    session: Session,
    handoff: Handoff,
    practitioner_id: uuid.UUID,
    *,
    note: str | None,
    at: datetime,
) -> None:
    """Resolve a live handoff (``open``/``assigned`` -> ``resolved``).

    An ``open`` handoff is assigned to the resolving practitioner first, so the
    record always says who closed it.

    Args:
        session: Open session (caller commits).
        handoff: Live handoff (already access-checked).
        practitioner_id: Practitioner resolving it.
        note: Optional resolution note.
        at: Time.
    """
    claim_handoff(session, handoff, practitioner_id, at)
    session.execute(
        update(Handoff)
        .where(Handoff.id == handoff.id, Handoff.status.in_(LIVE_STATUSES))
        .values(status="resolved", resolved_at=at, resolution_note=note)
    )
    session.refresh(handoff)
