"""Read-only queries behind the practitioner dashboard (R6).

Four views, each limited to the patients a practitioner may see
(:func:`app.db.audit_repository.visible_patient_ids`: assigned patients while the
practitioner and their login are active; every patient for an admin):

* :func:`overview` - KPI counts for a time window.
* :func:`list_patients` - one row per visible patient, keyset-paginated by id.
* :func:`patient_detail` - readings, the full threshold history with every component,
  the latest risk assessment and recent advisories of one patient.
* :func:`list_alerts` - advisories, newest reading first, keyset-paginated.
* :func:`own_health` - a patient's own limits, readings and advisories (no risk).

Nothing here writes: no INSERT, UPDATE or DELETE is issued, and nothing commits.

Privacy: a patient appears by first name, age band and diabetes status only. No
phone number, e-mail, push endpoint or full name leaves this module, and cursors carry
only ids and timestamps.

Band state of a reading (R3 rule, :func:`app.services.thresholds.classify_reading`) is
judged against the limits **in effect when it was measured** - the same step line the
chart draws - so a point above the line is always labelled a breach. A reading taken
before the patient's first limit was computed is judged against that first limit,
because the batch job judges a backlog against the limit it computes for it; the
chart draws that stretch of the line dashed. The batch job
judges a window's readings against the limits it computes at the window's end, so in
the rare window where a limit changed, a reading's label here can differ from the
advisory the batch raised; the advisory (with the limit it used) is shown as stored.
The patient list computes the state in SQL (:func:`_band_state_sql`, needed to filter
and paginate); the detail view calls ``classify_reading`` itself.
``tests/test_dashboard_repository.py`` checks that the two agree at every boundary.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Self
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError
from sqlalchemy import (
    ColumnElement,
    Numeric,
    ScalarSelect,
    SQLColumnExpression,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, aliased

from app.core.clinical_limits import (
    HYPO_LEVEL_2_MG_DL,
    DiabetesStatus,
    GlucoseLimitKind,
    LimitDirection,
)
from app.core.exceptions import ThresholdComputationError
from app.core.names import first_name
from app.core.rates import CountRate
from app.db.audit_repository import is_visible, visible_patient_ids
from app.db.batch_repository import BATCH_UPDATE_JOB, SqlBatchRepository
from app.db.chat_repository import db_now
from app.db.models import (
    Advisory,
    AdvisoryState,
    BatchStatus,
    DeliveryStatus,
    Handoff,
    HandoffStatus,
    Profile,
    Reading,
    ReadingContext,
    RiskAssessmentRecord,
    RiskTier,
    ThresholdHistory,
    ThresholdReason,
    User,
)
from app.db.threshold_repository import CURRENT_THRESHOLDS
from app.services.thresholds import BandState, ReadingPoint, classify_reading

MAX_PAGE_SIZE: Final = 100
# R6: the patient list counts alerts over the last week (a clinic-week review cycle).
ALERT_LOOKBACK_DAYS: Final = 7
# R6: overview default for alerts: the last 24 hours, so overnight events stay visible
# in the morning (a "today" window would hide them after midnight).
ALERT_WINDOW: Final = timedelta(hours=24)
RECENT_ADVISORIES: Final = 20
MAX_DETAIL_DAYS: Final = 90
LIMIT_KINDS: Final[tuple[GlucoseLimitKind, ...]] = ("fasting_high", "post_meal_high", "low")
# Handoffs a practitioner still has to act on (R4): waiting to be claimed, or claimed.
LIVE_HANDOFF: Final[tuple[HandoffStatus, ...]] = ("open", "assigned")
BREACH_STATES: Final[tuple[AdvisoryState, ...]] = ("breach_high", "breach_low")
PRE_BREACH_STATES: Final[tuple[AdvisoryState, ...]] = ("pre_breach_high", "pre_breach_low")
# Advisories that reached the patient's device; "suppressed" ones were deliberately
# not sent (same limit and state inside the cooldown), so they are not in the
# delivery denominator.
DELIVERED: Final[tuple[DeliveryStatus, ...]] = ("sent", "opened")
NOT_SENT_ON_PURPOSE: Final[DeliveryStatus] = "suppressed"

# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------


class _Cursor(BaseModel):
    """Base for opaque keyset cursors (URL-safe base64 of JSON)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def encode(self) -> str:
        """Serialise for a URL query parameter.

        Returns:
            URL-safe base64 of the JSON form.
        """
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, token: str) -> Self:
        """Parse a token made by :meth:`encode`.

        Args:
            token: The query parameter.

        Returns:
            The cursor.

        Raises:
            ValueError: The token is not a cursor of this kind.
        """
        try:
            raw = base64.urlsafe_b64decode(token.encode("ascii"))
            return cls.model_validate_json(raw)
        except (UnicodeEncodeError, binascii.Error, ValidationError) as exc:
            raise ValueError("invalid cursor") from exc


class PatientCursor(_Cursor):
    """Position in the patient list (ordered by user id).

    Attributes:
        id: Last patient id of the previous page.
    """

    id: uuid.UUID


class AlertCursor(_Cursor):
    """Position in the alert list (newest reading first).

    Attributes:
        measured_at: Reading time of the last advisory of the previous page.
        id: That advisory's id (tie-breaker).
    """

    measured_at: AwareDatetime
    id: uuid.UUID


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class TimeWindow(BaseModel):
    """A half-open time range ``[since, until)``.

    Attributes:
        since: Inclusive start.
        until: Exclusive end.
    """

    since: AwareDatetime
    until: AwareDatetime


class DeliveryCounts(BaseModel):
    """Advisory delivery states in a window, each out of all advisories in it.

    Attributes:
        total: Advisories raised for readings in the window.
        queued: Not dispatched yet.
        sent: Pushed to the patient's device.
        opened: Opened by the patient.
        failed: The push service refused it.
        expired: The reading was too old to be worth pushing by dispatch time.
        suppressed: Not sent on purpose (same alert inside the cooldown).
        delivered: Sent or opened, out of every advisory except suppressed ones.
    """

    total: int
    queued: CountRate
    sent: CountRate
    opened: CountRate
    failed: CountRate
    expired: CountRate
    suppressed: CountRate
    delivered: CountRate


class BatchRunInfo(BaseModel):
    """The latest threshold batch run (R1).

    Attributes:
        started_at: When it started.
        finished_at: When it finished; ``None`` while running.
        status: ``running``, ``succeeded`` or ``failed``.
    """

    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    status: BatchStatus


class Overview(BaseModel):
    """Dashboard KPI tiles (R6).

    Attributes:
        readings_window: Window of ``readings`` (default: today, clinic time).
        alerts_window: Window of the alert counts, ``below_54_readings`` and
            ``delivery`` (default: the last 24 hours). All are on reading time.
        active_patients: Visible patients whose login is active.
        readings: Readings measured in ``readings_window``.
        pre_breach_alerts: Pre-breach advisories for readings in ``alerts_window``.
        breaches: Breach advisories for readings in ``alerts_window``.
        below_54_readings: Readings under 54 mg/dL (Level 2 hypoglycaemia) in
            ``alerts_window``.
        open_handoffs: Handoffs waiting to be claimed (now).
        claimed_handoffs: Handoffs claimed and not resolved (now).
        delivery: Advisory delivery states in ``alerts_window``.
        last_batch_run: Latest threshold batch run; ``None`` if it never ran.
    """

    readings_window: TimeWindow
    alerts_window: TimeWindow
    active_patients: int
    readings: int
    pre_breach_alerts: int
    breaches: int
    below_54_readings: int
    open_handoffs: int
    claimed_handoffs: int
    delivery: DeliveryCounts
    last_batch_run: BatchRunInfo | None


class CurrentLimits(BaseModel):
    """A patient's current personal limits in mg/dL (``None`` if never computed).

    Attributes:
        fasting_high: Upper limit for fasting readings.
        post_meal_high: Upper limit for post-meal readings.
        low: Lower limit for every reading.
    """

    fasting_high: float | None
    post_meal_high: float | None
    low: float | None


class RiskInfo(BaseModel):
    """The latest survey-based risk assessment (information only; moves no limit).

    Attributes:
        tier: ``low``, ``moderate`` or ``high``.
        score: Model probability, 0 to 1.
        flagged: At or above the model's decision threshold.
        model_version: Model that scored it.
        assessed_at: When it was scored.
    """

    tier: RiskTier
    score: float
    flagged: bool
    model_version: str
    assessed_at: AwareDatetime


class LastReading(BaseModel):
    """A patient's most recent reading.

    Attributes:
        value_mg_dl: Glucose.
        context: ``fasting``, ``post_meal`` or ``random``.
        measured_at: When it was measured.
        band_state: R3 band state against the limits in effect then; ``None`` if the
            patient had no limits yet.
    """

    value_mg_dl: float
    context: ReadingContext
    measured_at: AwareDatetime
    band_state: BandState | None


class PatientRow(BaseModel):
    """One row of the patient list. No contact details.

    Attributes:
        user_id: Patient id (to open the detail view).
        first_name: First word of the stored name, or ``None``.
        age_band: e.g. ``"50-59"``; ``None`` when no age is stored.
        diabetes_status: ``diabetes`` or ``no_diabetes``; ``None`` before onboarding.
        risk: Latest risk assessment, or ``None``.
        limits: Current personal limits.
        last_reading: Most recent reading, or ``None``.
        alerts_7d: Advisories for readings in the last 7 days.
        breach_7d: At least one of those is a breach.
        live_handoff: ``open`` or ``assigned`` handoff, or ``None``.
    """

    user_id: uuid.UUID
    first_name: str | None
    age_band: str | None
    diabetes_status: DiabetesStatus | None
    risk: RiskInfo | None
    limits: CurrentLimits
    last_reading: LastReading | None
    alerts_7d: int
    breach_7d: bool
    live_handoff: HandoffStatus | None


class PatientPage(BaseModel):
    """One page of the patient list.

    Attributes:
        items: Patients, by id.
        next_cursor: Pass back as ``cursor``; ``None`` on the last page.
    """

    items: list[PatientRow]
    next_cursor: str | None


class ReadingPointOut(BaseModel):
    """One reading on the patient chart.

    Attributes:
        id: ``readings.id``.
        measured_at: When it was measured.
        value_mg_dl: Glucose.
        context: Reading context.
        band_state: R3 band state against the limits in effect then; ``None`` when
            the patient has no limit the reading needs.
        limit_kind: The limit that state refers to (``None`` when normal).
        before_first_limit: Taken before the first limit was computed, so judged
            against that first limit (as the batch job does with a backlog).
    """

    id: uuid.UUID
    measured_at: AwareDatetime
    value_mg_dl: float
    context: ReadingContext
    band_state: BandState | None
    limit_kind: GlucoseLimitKind | None
    before_first_limit: bool


class ThresholdStep(BaseModel):
    """One stored threshold with every component that produced it (R2).

    ``value = clamp(base + demo_adj + risk_adj + trend_adj, hard_min, hard_max)``.

    Attributes:
        id: ``thresholds_history.id``.
        limit_kind: Which limit.
        direction: ``upper`` or ``lower``.
        effective_from: When it took effect (the step of the chart line).
        base_mg_dl: Cited published value.
        demo_adj_mg_dl: Bounded age/sex adjustment.
        risk_adj_mg_dl: Risk-tier adjustment (0 since commit 6f54669: the survey
            score never moves a limit).
        trend_adj_mg_dl: Bounded trend revision.
        unclamped_mg_dl: The sum before clamping.
        hard_min_mg_dl: Clamp floor.
        hard_max_mg_dl: Clamp ceiling.
        value_mg_dl: The limit.
        reason: Why this row was written.
        source_citation: Where the base and clamp come from.
        model_version: Engine/model version that wrote it.
    """

    id: uuid.UUID
    limit_kind: GlucoseLimitKind
    direction: LimitDirection
    effective_from: AwareDatetime
    base_mg_dl: float
    demo_adj_mg_dl: float
    risk_adj_mg_dl: float
    trend_adj_mg_dl: float
    unclamped_mg_dl: float
    hard_min_mg_dl: float
    hard_max_mg_dl: float
    value_mg_dl: float
    reason: ThresholdReason
    source_citation: str
    model_version: str


class AdvisoryRow(BaseModel):
    """One advisory (alert) and its delivery state.

    Attributes:
        id: ``advisories.id``.
        user_id: Patient id.
        first_name: Patient's first name.
        measured_at: When the reading was measured.
        created_at: When the alert was raised.
        limit_kind: Which limit.
        state: Pre-breach or breach, high or low.
        reading_value_mg_dl: The reading.
        threshold_mg_dl: The limit the batch judged it against.
        escalated: A practitioner was brought in.
        delivery_status: Delivery state.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    first_name: str | None
    measured_at: AwareDatetime
    created_at: AwareDatetime
    limit_kind: GlucoseLimitKind
    state: AdvisoryState
    reading_value_mg_dl: float
    threshold_mg_dl: float
    escalated: bool
    delivery_status: DeliveryStatus


class AlertPage(BaseModel):
    """One page of the alert list.

    Attributes:
        items: Advisories, newest reading first.
        next_cursor: Pass back as ``cursor``; ``None`` on the last page.
    """

    items: list[AdvisoryRow]
    next_cursor: str | None


class PatientDetail(BaseModel):
    """Everything the patient detail view draws.

    Attributes:
        user_id: Patient id.
        first_name: First name.
        age_band: Age band.
        diabetes_status: Diabetes status.
        window: The readings' time range.
        pre_breach_band: Band fraction from settings (0.05 = within 5 % of a limit).
        readings: Readings in the window, oldest first.
        thresholds: Threshold rows that apply in the window (the one in effect at its
            start, then every later one), by kind, oldest first.
        risk: Latest risk assessment, or ``None``.
        advisories: Last :data:`RECENT_ADVISORIES` advisories, newest first.
    """

    user_id: uuid.UUID
    first_name: str | None
    age_band: str | None
    diabetes_status: DiabetesStatus | None
    window: TimeWindow
    pre_breach_band: float
    readings: list[ReadingPointOut]
    thresholds: list[ThresholdStep]
    risk: RiskInfo | None
    advisories: list[AdvisoryRow]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Decade bands hide the exact age (fewer re-identification clues) but still show the
# older-adult cut-off at 65 within "60-69"; 80+ is grouped like the BRFSS top bucket.
_OLDEST_BAND_START: Final = 80
_YOUNGEST_BAND_END: Final = 20


def age_band(age_years: int | None) -> str | None:
    """Return a decade band for an age.

    Args:
        age_years: Age in whole years.

    Returns:
        ``"<20"``, ``"20-29"`` ... ``"70-79"``, ``"80+"``, or ``None`` for no age.
    """
    if age_years is None:
        return None
    if age_years < _YOUNGEST_BAND_END:
        return f"<{_YOUNGEST_BAND_END}"
    if age_years >= _OLDEST_BAND_START:
        return f"{_OLDEST_BAND_START}+"
    start = age_years // 10 * 10
    return f"{start}-{start + 9}"


def _num(value: Decimal | None) -> float | None:
    """Convert a numeric column to a JSON number.

    Args:
        value: Database value.

    Returns:
        The float, or ``None``.
    """
    return float(value) if value is not None else None


def _limit_at(
    kind: GlucoseLimitKind, user: SQLColumnExpression[uuid.UUID], at: SQLColumnExpression[datetime]
) -> ColumnElement[Decimal]:
    """Correlated subquery: the user's ``kind`` limit that judges a reading taken at ``at``.

    The limit in effect at ``at`` (ordered like the ``current_thresholds`` view); for a
    reading taken before the first limit was computed, that first limit, because the
    batch job judges such a backlog against the limit it computes for it.

    Args:
        kind: Limit kind.
        user: The user id column.
        at: The moment (e.g. a reading's ``measured_at``).

    Returns:
        The threshold value, or NULL when the user has no ``kind`` limit at all.
    """
    th = aliased(ThresholdHistory)
    in_effect = (
        select(th.threshold_mg_dl)
        .where(th.user_id == user, th.limit_kind == kind, th.effective_from <= at)
        .order_by(th.effective_from.desc(), th.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    first = aliased(ThresholdHistory)
    earliest = (
        select(first.threshold_mg_dl)
        .where(first.user_id == user, first.limit_kind == kind)
        .order_by(first.effective_from.asc(), first.created_at.asc())
        .limit(1)
        .scalar_subquery()
    )
    return func.coalesce(in_effect, earliest)


def _current_limit(
    kind: GlucoseLimitKind, user: SQLColumnExpression[uuid.UUID]
) -> ScalarSelect[Decimal]:
    """Correlated subquery on the ``current_thresholds`` view.

    Args:
        kind: Limit kind.
        user: The user id column.

    Returns:
        The current threshold value, or NULL.
    """
    view = CURRENT_THRESHOLDS.alias()
    return (
        select(view.c.threshold_mg_dl)
        .where(view.c.user_id == user, view.c.limit_kind == kind)
        .scalar_subquery()
    )


def _band_state_sql(
    value: ColumnElement[Decimal],
    *,
    context: ColumnElement[str],
    low: ColumnElement[Decimal],
    fasting_high: ColumnElement[Decimal],
    post_meal_high: ColumnElement[Decimal],
    band: Decimal,
) -> ColumnElement[str | None]:
    """SQL mirror of :func:`app.services.thresholds.classify_reading` (R3).

    Same order as the Python rule: breach before pre-breach, low before high::

        breach_low       x <= L
        breach_high      x >= U
        pre_breach_low   x <= (1 + b) * L
        pre_breach_high  x >= (1 - b) * U
        normal           otherwise

    ``U`` is the upper limit for the reading's context (none for ``random``). NULL
    when a limit the reading needs is missing (the Python rule raises there).

    Args:
        value: Reading value.
        context: Reading context.
        low: Low limit in effect.
        fasting_high: Fasting limit in effect.
        post_meal_high: Post-meal limit in effect.
        band: Band fraction (``Settings.pre_breach_band``), exact as a Decimal.

    Returns:
        The band state expression.
    """
    # Cast so a NULL limit still compares as a number, never as text.
    upper = cast(
        case(
            (context == "fasting", fasting_high),
            (context == "post_meal", post_meal_high),
            else_=None,
        ),
        Numeric,
    )
    needs_upper = context.in_(("fasting", "post_meal"))
    fraction = literal(band)
    return case(
        (or_(value.is_(None), low.is_(None), and_(needs_upper, upper.is_(None))), None),
        (value <= low, "breach_low"),
        (and_(needs_upper, value >= upper), "breach_high"),
        (value <= low * (1 + fraction), "pre_breach_low"),
        (and_(needs_upper, value >= upper * (1 - fraction)), "pre_breach_high"),
        else_="normal",
    )


def _band_fraction(band: float) -> Decimal:
    """Turn the settings float into the exact decimal the Python rule uses.

    Args:
        band: ``Settings.pre_breach_band``.

    Returns:
        ``Decimal(str(band))`` (0.05 -> exactly 0.05).
    """
    return Decimal(str(band))


def _risk(row: RiskAssessmentRecord | None) -> RiskInfo | None:
    """Convert a stored risk assessment.

    Args:
        row: The row, or ``None``.

    Returns:
        The API form, or ``None``.
    """
    if row is None:
        return None
    return RiskInfo(
        tier=row.risk_tier,
        score=float(row.risk_score),
        flagged=row.flagged,
        model_version=row.model_version,
        assessed_at=row.created_at,
    )


def _check_limit(limit: int) -> None:
    """Validate a page size.

    Args:
        limit: Requested page size.

    Raises:
        ValueError: Outside 1 to :data:`MAX_PAGE_SIZE`.
    """
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")


def start_of_day(moment: datetime, zone: str) -> datetime:
    """Local midnight of ``moment``'s day in ``zone``.

    Args:
        moment: Any aware time.
        zone: IANA zone name.

    Returns:
        That midnight, timezone-aware.
    """
    local = moment.astimezone(ZoneInfo(zone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def overview(
    session: Session,
    practitioner_id: uuid.UUID,
    *,
    since: datetime | None,
    until: datetime | None,
    timezone_name: str,
) -> Overview:
    """Count the dashboard KPIs over the practitioner's visible patients.

    With ``since`` given, both windows are ``[since, until)``. Without it, readings
    count from local midnight (``timezone_name``) and alerts over the 24 hours before
    ``until``. Alerts are windowed on the reading's ``measured_at``, not on when the
    batch raised them, so a backfill does not pile old events into "last 24 h".

    Args:
        session: Open session.
        practitioner_id: Viewer (``practitioners.id``).
        since: Inclusive start for every count, or ``None`` for the defaults.
        until: Exclusive end (default: now, on the database clock).
        timezone_name: Zone whose midnight starts "today".

    Returns:
        The counts.

    Raises:
        ValueError: ``since`` is not before ``until``.
    """
    end = until or db_now(session)
    if since is not None and since >= end:
        raise ValueError("since must be before until")
    readings_window = TimeWindow(since=since or start_of_day(end, timezone_name), until=end)
    alerts_window = TimeWindow(since=since or end - ALERT_WINDOW, until=end)
    visible = visible_patient_ids(practitioner_id)

    active = session.scalar(
        select(func.count()).select_from(User).where(User.id.in_(visible), User.is_active)
    )
    readings = session.scalar(
        select(func.count())
        .select_from(Reading)
        .where(
            Reading.user_id.in_(visible),
            Reading.measured_at >= readings_window.since,
            Reading.measured_at < readings_window.until,
        )
    )
    below_54 = session.scalar(
        select(func.count())
        .select_from(Reading)
        .where(
            Reading.user_id.in_(visible),
            Reading.measured_at >= alerts_window.since,
            Reading.measured_at < alerts_window.until,
            # ADA 2026 Table 6.4 / IHSG (clinical_limits): Level 2 is strictly < 54.
            Reading.value_mg_dl < Decimal(str(HYPO_LEVEL_2_MG_DL)),
        )
    )
    alert_rows = session.execute(
        select(Advisory.state, Advisory.delivery_status, func.count())
        .join(Reading, Reading.id == Advisory.reading_id)
        .where(
            Advisory.user_id.in_(visible),
            Reading.measured_at >= alerts_window.since,
            Reading.measured_at < alerts_window.until,
        )
        .group_by(Advisory.state, Advisory.delivery_status)
    ).all()
    handoffs = dict(
        session.execute(
            select(Handoff.status, func.count())
            .where(Handoff.user_id.in_(visible), Handoff.status.in_(LIVE_HANDOFF))
            .group_by(Handoff.status)
        ).all()
    )

    by_status: dict[str, int] = {}
    pre_breach = breaches = 0
    for state, delivery, n in alert_rows:
        by_status[delivery] = by_status.get(delivery, 0) + n
        if state in PRE_BREACH_STATES:
            pre_breach += n
        elif state in BREACH_STATES:
            breaches += n
    total = sum(by_status.values())
    attempted = total - by_status.get(NOT_SENT_ON_PURPOSE, 0)

    def share(status_name: DeliveryStatus) -> CountRate:
        return CountRate.of(by_status.get(status_name, 0), total)

    runs = SqlBatchRepository(session).recent_runs(BATCH_UPDATE_JOB, 1)
    last = runs[0] if runs else None
    return Overview(
        readings_window=readings_window,
        alerts_window=alerts_window,
        active_patients=int(active or 0),
        readings=int(readings or 0),
        pre_breach_alerts=pre_breach,
        breaches=breaches,
        below_54_readings=int(below_54 or 0),
        open_handoffs=int(handoffs.get("open", 0)),
        claimed_handoffs=int(handoffs.get("assigned", 0)),
        delivery=DeliveryCounts(
            total=total,
            queued=share("queued"),
            sent=share("sent"),
            opened=share("opened"),
            failed=share("failed"),
            expired=share("expired"),
            suppressed=share("suppressed"),
            delivered=CountRate.of(sum(by_status.get(s, 0) for s in DELIVERED), attempted),
        ),
        last_batch_run=(
            BatchRunInfo(
                started_at=last.started_at, finished_at=last.finished_at, status=last.status
            )
            if last is not None
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Patient list
# ---------------------------------------------------------------------------


def list_patients(
    session: Session,
    practitioner_id: uuid.UUID,
    *,
    band: float,
    limit: int = 25,
    cursor: PatientCursor | None = None,
    band_state: BandState | None = None,
    breach_7d: bool | None = None,
    live_handoff: bool | None = None,
) -> PatientPage:
    """List the practitioner's visible patients, one row each, by id.

    Args:
        session: Open session.
        practitioner_id: Viewer (``practitioners.id``).
        band: Pre-breach band fraction (``Settings.pre_breach_band``).
        limit: Page size, 1 to :data:`MAX_PAGE_SIZE`.
        cursor: Continue after this patient.
        band_state: Only patients whose last reading is in this state.
        breach_7d: Only patients with (``True``) or without (``False``) a breach
            advisory for a reading in the last 7 days.
        live_handoff: Only patients with (``True``) or without (``False``) an open
            or claimed handoff.

    Returns:
        One page and the next cursor.

    Raises:
        ValueError: ``limit`` is out of range.
    """
    _check_limit(limit)
    now = db_now(session)
    week_ago = now - timedelta(days=ALERT_LOOKBACK_DAYS)
    last = (
        select(Reading.value_mg_dl, Reading.context, Reading.measured_at)
        .where(Reading.user_id == User.id)
        .order_by(Reading.measured_at.desc(), Reading.id.desc())
        .limit(1)
        .lateral("last_reading")
    )
    risk = (
        select(RiskAssessmentRecord.id)
        .where(RiskAssessmentRecord.user_id == User.id)
        .order_by(RiskAssessmentRecord.created_at.desc(), RiskAssessmentRecord.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    in_week = (
        Advisory.user_id == User.id,
        Reading.id == Advisory.reading_id,
        Reading.measured_at >= week_ago,
    )
    # Correlated on users.id: a count per patient, not over everyone.
    alerts_7d = (
        select(func.count(Advisory.id))
        .select_from(Advisory)
        .join(Reading, Reading.id == Advisory.reading_id)
        .where(*in_week)
        .scalar_subquery()
    )
    breach = exists().where(*in_week, Advisory.state.in_(BREACH_STATES))
    handoff = (
        select(Handoff.status)
        .where(Handoff.user_id == User.id, Handoff.status.in_(LIVE_HANDOFF))
        .order_by(Handoff.opened_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    state = _band_state_sql(
        last.c.value_mg_dl,
        context=last.c.context,
        low=_limit_at("low", User.id, last.c.measured_at),
        fasting_high=_limit_at("fasting_high", User.id, last.c.measured_at),
        post_meal_high=_limit_at("post_meal_high", User.id, last.c.measured_at),
        band=_band_fraction(band),
    )
    rows = (
        select(
            User.id.label("user_id"),
            Profile.full_name,
            Profile.age_years,
            Profile.diabetes_status,
            risk.label("risk_id"),
            _current_limit("fasting_high", User.id).label("fasting_high"),
            _current_limit("post_meal_high", User.id).label("post_meal_high"),
            _current_limit("low", User.id).label("low"),
            last.c.value_mg_dl,
            last.c.context,
            last.c.measured_at,
            state.label("band_state"),
            alerts_7d.label("alerts_7d"),
            breach.label("breach_7d"),
            handoff.label("live_handoff"),
        )
        .select_from(User)
        .outerjoin(Profile, Profile.user_id == User.id)
        .outerjoin(last, literal(True))
        .where(User.id.in_(visible_patient_ids(practitioner_id)))
        .subquery("patient_rows")
    )
    stmt = select(rows)
    if band_state is not None:
        stmt = stmt.where(rows.c.band_state == band_state)
    if breach_7d is not None:
        stmt = stmt.where(rows.c.breach_7d.is_(breach_7d))
    if live_handoff is not None:
        stmt = stmt.where(
            rows.c.live_handoff.is_not(None) if live_handoff else rows.c.live_handoff.is_(None)
        )
    if cursor is not None:
        stmt = stmt.where(rows.c.user_id > cursor.id)
    found = session.execute(stmt.order_by(rows.c.user_id).limit(limit + 1)).mappings().all()
    page, more = found[:limit], len(found) > limit

    risk_ids = [r["risk_id"] for r in page if r["risk_id"] is not None]
    risks = (
        {
            r.id: r
            for r in session.scalars(
                select(RiskAssessmentRecord).where(RiskAssessmentRecord.id.in_(risk_ids))
            )
        }
        if risk_ids
        else {}
    )

    items = [
        PatientRow(
            user_id=r["user_id"],
            first_name=first_name(r["full_name"]),
            age_band=age_band(r["age_years"]),
            diabetes_status=r["diabetes_status"],
            risk=_risk(risks.get(r["risk_id"])) if r["risk_id"] is not None else None,
            limits=CurrentLimits(
                fasting_high=_num(r["fasting_high"]),
                post_meal_high=_num(r["post_meal_high"]),
                low=_num(r["low"]),
            ),
            last_reading=(
                LastReading(
                    value_mg_dl=float(r["value_mg_dl"]),
                    context=r["context"],
                    measured_at=r["measured_at"],
                    band_state=r["band_state"],
                )
                if r["measured_at"] is not None
                else None
            ),
            alerts_7d=int(r["alerts_7d"] or 0),
            breach_7d=bool(r["breach_7d"]),
            live_handoff=r["live_handoff"],
        )
        for r in page
    ]
    next_cursor = PatientCursor(id=page[-1]["user_id"]).encode() if more else None
    return PatientPage(items=items, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Patient detail
# ---------------------------------------------------------------------------


def _steps(row: ThresholdHistory) -> ThresholdStep:
    """Convert a stored threshold.

    Args:
        row: The row.

    Returns:
        The API form.
    """
    return ThresholdStep(
        id=row.id,
        limit_kind=row.limit_kind,
        direction=row.direction,
        effective_from=row.effective_from,
        base_mg_dl=float(row.base_mg_dl),
        demo_adj_mg_dl=float(row.demo_adj_mg_dl),
        risk_adj_mg_dl=float(row.risk_adj_mg_dl),
        trend_adj_mg_dl=float(row.trend_adj_mg_dl),
        unclamped_mg_dl=float(row.unclamped_mg_dl),
        hard_min_mg_dl=float(row.hard_min_mg_dl),
        hard_max_mg_dl=float(row.hard_max_mg_dl),
        value_mg_dl=float(row.threshold_mg_dl),
        reason=row.reason,
        source_citation=row.source_citation,
        model_version=row.model_version,
    )


def _in_effect(
    steps: list[ThresholdHistory], at: datetime
) -> tuple[dict[GlucoseLimitKind, Decimal], bool]:
    """Limits that judge a reading taken at ``at`` (same rule as :func:`_limit_at`).

    Args:
        steps: Threshold rows, oldest first (by ``effective_from``, ``created_at``).
        at: The moment.

    Returns:
        The latest value per kind with ``effective_from <= at``, falling back to a
        kind's first row when none had taken effect yet; and whether any kind fell
        back.
    """
    values: dict[GlucoseLimitKind, Decimal] = {}
    earliest: dict[GlucoseLimitKind, Decimal] = {}
    for step in steps:
        earliest.setdefault(step.limit_kind, step.threshold_mg_dl)
        if step.effective_from <= at:
            values[step.limit_kind] = step.threshold_mg_dl
    fell_back = any(kind not in values for kind in earliest)
    return {**earliest, **values}, fell_back


def _advisory_rows(
    session: Session, where: list[ColumnElement[bool]], limit: int
) -> list[tuple[Advisory, datetime, str | None]]:
    """Advisories with their reading time and patient name, newest reading first.

    Args:
        session: Open session.
        where: Conditions.
        limit: Row cap.

    Returns:
        ``(advisory, measured_at, full_name)`` tuples.
    """
    stmt = (
        select(Advisory, Reading.measured_at, Profile.full_name)
        .join(Reading, Reading.id == Advisory.reading_id)
        .outerjoin(Profile, Profile.user_id == Advisory.user_id)
        .where(*where)
        .order_by(Reading.measured_at.desc(), Advisory.id.desc())
        .limit(limit)
    )
    return [(a, at, name) for a, at, name in session.execute(stmt)]


def _advisory_out(advisory: Advisory, measured_at: datetime, full_name: str | None) -> AdvisoryRow:
    """Convert an advisory row.

    Args:
        advisory: The advisory.
        measured_at: Its reading's time.
        full_name: Patient's stored name (only the first word is kept).

    Returns:
        The API form.
    """
    return AdvisoryRow(
        id=advisory.id,
        user_id=advisory.user_id,
        first_name=first_name(full_name),
        measured_at=measured_at,
        created_at=advisory.created_at,
        limit_kind=advisory.limit_kind,
        state=advisory.state,
        reading_value_mg_dl=float(advisory.reading_value_mg_dl),
        threshold_mg_dl=float(advisory.threshold_mg_dl),
        escalated=advisory.escalated,
        delivery_status=advisory.delivery_status,
    )


def patient_detail(
    session: Session,
    practitioner_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    days: int,
    band: float,
) -> PatientDetail | None:
    """Load one patient's chart data, or ``None`` if the viewer may not see them.

    Args:
        session: Open session.
        practitioner_id: Viewer.
        user_id: Patient.
        days: How many days of readings, 1 to :data:`MAX_DETAIL_DAYS`.
        band: Pre-breach band fraction (``Settings.pre_breach_band``).

    Returns:
        The detail, or ``None`` for a patient that does not exist or is not visible.

    Raises:
        ValueError: ``days`` is out of range.
    """
    if not 1 <= days <= MAX_DETAIL_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DETAIL_DAYS}")
    if not is_visible(session, practitioner_id, user_id):
        return None
    return _load_detail(session, user_id, days=days, band=band)


def _load_detail(session: Session, user_id: uuid.UUID, *, days: int, band: float) -> PatientDetail:
    """Load one patient's chart data without any access check (callers check).

    Args:
        session: Open session.
        user_id: Patient.
        days: How many days of readings (already validated).
        band: Pre-breach band fraction.

    Returns:
        The detail.
    """
    end = db_now(session)
    start = end - timedelta(days=days)
    profile = session.get(Profile, user_id)

    order = (ThresholdHistory.effective_from, ThresholdHistory.created_at, ThresholdHistory.id)
    # The row in effect at the window start (one per kind), then every later row: the
    # step line starts at the left edge of the chart.
    before = (
        select(ThresholdHistory)
        .where(ThresholdHistory.user_id == user_id, ThresholdHistory.effective_from <= start)
        .order_by(
            ThresholdHistory.limit_kind,
            ThresholdHistory.effective_from.desc(),
            ThresholdHistory.created_at.desc(),
        )
        .ext(distinct_on(ThresholdHistory.limit_kind))
    )
    during = (
        select(ThresholdHistory)
        .where(ThresholdHistory.user_id == user_id, ThresholdHistory.effective_from > start)
        .order_by(*order)
    )
    steps = sorted(
        [*session.scalars(before), *session.scalars(during)],
        key=lambda t: (t.effective_from, t.created_at, t.id),
    )

    readings: list[ReadingPointOut] = []
    for r in session.scalars(
        select(Reading)
        .where(Reading.user_id == user_id, Reading.measured_at >= start, Reading.measured_at < end)
        .order_by(Reading.measured_at, Reading.id)
    ):
        try:
            limits, before_first = _in_effect(steps, r.measured_at)
            result = classify_reading(ReadingPoint.model_validate(r), limits, band=band)
            state: BandState | None = result.state
            kind = result.limit_kind
        except ThresholdComputationError:
            # The patient has no limit this reading needs: say so, never guess.
            state, kind, before_first = None, None, False
        readings.append(
            ReadingPointOut(
                id=r.id,
                measured_at=r.measured_at,
                value_mg_dl=float(r.value_mg_dl),
                context=r.context,
                band_state=state,
                limit_kind=kind,
                before_first_limit=before_first,
            )
        )

    latest_risk = session.scalars(
        select(RiskAssessmentRecord)
        .where(RiskAssessmentRecord.user_id == user_id)
        .order_by(RiskAssessmentRecord.created_at.desc(), RiskAssessmentRecord.id.desc())
        .limit(1)
    ).first()
    advisories = _advisory_rows(session, [Advisory.user_id == user_id], RECENT_ADVISORIES)
    return PatientDetail(
        user_id=user_id,
        first_name=first_name(profile.full_name if profile else None),
        age_band=age_band(profile.age_years if profile else None),
        diabetes_status=profile.diabetes_status if profile else None,
        window=TimeWindow(since=start, until=end),
        pre_breach_band=band,
        readings=readings,
        thresholds=[_steps(t) for t in steps],
        risk=_risk(latest_risk),
        advisories=[_advisory_out(*row) for row in advisories],
    )


class OwnHealth(BaseModel):
    """A patient's own limits, readings and alerts (the patient app's R6 view).

    Deliberately narrower than :class:`PatientDetail`: no risk assessment (the
    survey-based risk is never shown to patients) and no threshold components, only
    the current limits.

    Attributes:
        window: The readings' time range.
        pre_breach_band: Band fraction from settings.
        limits: Current limits (the latest row of each kind).
        readings: Readings in the window, oldest first, with their band state.
        advisories: Last :data:`RECENT_ADVISORIES` advisories, newest first.
    """

    window: TimeWindow
    pre_breach_band: float
    limits: CurrentLimits
    readings: list[ReadingPointOut]
    advisories: list[AdvisoryRow]


def own_health(session: Session, user_id: uuid.UUID, *, days: int, band: float) -> OwnHealth:
    """Load the signed-in patient's own view (the caller has resolved ``user_id``).

    Args:
        session: Open session.
        user_id: The patient asking (never a request parameter).
        days: How many days of readings, 1 to :data:`MAX_DETAIL_DAYS`.
        band: Pre-breach band fraction.

    Returns:
        The patient's limits, readings and advisories.

    Raises:
        ValueError: ``days`` is out of range.
    """
    if not 1 <= days <= MAX_DETAIL_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DETAIL_DAYS}")
    detail = _load_detail(session, user_id, days=days, band=band)
    latest: dict[str, float] = {}
    for step in detail.thresholds:  # oldest first: the last row of each kind wins
        latest[step.limit_kind] = step.value_mg_dl
    return OwnHealth(
        window=detail.window,
        pre_breach_band=detail.pre_breach_band,
        limits=CurrentLimits(
            fasting_high=latest.get("fasting_high"),
            post_meal_high=latest.get("post_meal_high"),
            low=latest.get("low"),
        ),
        readings=detail.readings,
        advisories=detail.advisories,
    )


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def list_alerts(
    session: Session,
    practitioner_id: uuid.UUID,
    *,
    limit: int = 25,
    cursor: AlertCursor | None = None,
    state: AdvisoryState | None = None,
    escalated: bool | None = None,
    delivery_status: DeliveryStatus | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> AlertPage:
    """List advisories of the practitioner's visible patients, newest reading first.

    Args:
        session: Open session.
        practitioner_id: Viewer.
        limit: Page size, 1 to :data:`MAX_PAGE_SIZE`.
        cursor: Continue after this advisory.
        state: Only this band state.
        escalated: Only escalated (``True``) or not (``False``).
        delivery_status: Only this delivery state.
        since: Readings measured at or after this.
        until: Readings measured before this.

    Returns:
        One page and the next cursor.

    Raises:
        ValueError: ``limit`` is out of range, or ``since`` is not before ``until``.
    """
    _check_limit(limit)
    if since is not None and until is not None and since >= until:
        raise ValueError("since must be before until")
    where: list[ColumnElement[bool]] = [Advisory.user_id.in_(visible_patient_ids(practitioner_id))]
    if state is not None:
        where.append(Advisory.state == state)
    if escalated is not None:
        where.append(Advisory.escalated.is_(escalated))
    if delivery_status is not None:
        where.append(Advisory.delivery_status == delivery_status)
    if since is not None:
        where.append(Reading.measured_at >= since)
    if until is not None:
        where.append(Reading.measured_at < until)
    if cursor is not None:
        where.append(
            tuple_(Reading.measured_at, Advisory.id) < tuple_(cursor.measured_at, cursor.id)
        )
    rows = _advisory_rows(session, where, limit + 1)
    page, more = rows[:limit], len(rows) > limit
    next_cursor = AlertCursor(measured_at=page[-1][1], id=page[-1][0].id).encode() if more else None
    return AlertPage(items=[_advisory_out(*row) for row in page], next_cursor=next_cursor)


__all__ = [
    "MAX_PAGE_SIZE",
    "AdvisoryRow",
    "AlertCursor",
    "AlertPage",
    "Overview",
    "PatientCursor",
    "PatientDetail",
    "PatientPage",
    "PatientRow",
    "age_band",
    "is_visible",
    "list_alerts",
    "list_patients",
    "overview",
    "patient_detail",
    "start_of_day",
]
