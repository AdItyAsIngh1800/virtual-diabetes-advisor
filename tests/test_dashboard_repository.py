"""Live tests for the dashboard (R6) queries and API against Supabase.

Opt-in: ``RUN_DB_TESTS=1 uv run pytest tests/test_dashboard_repository.py``. Everything
runs in one outer transaction that is rolled back, so nothing is left behind.

The clinic below is built with real rows: thresholds come from the threshold engine
itself (so ``risk_adj`` is whatever the engine writes), and each patient carries a
phone number, a surname, a login e-mail and a push endpoint that must never appear in
any dashboard response.
"""

from __future__ import annotations

import json
import os
import uuid
import warnings
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Connection, func, literal, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.clinical_limits import DiabetesStatus, GlucoseLimitKind
from app.core.config import load_settings
from app.core.exceptions import ThresholdComputationError
from app.db import dashboard_repository as dash
from app.db.chat_repository import db_now
from app.db.models import (
    Advisory,
    AdvisoryState,
    DeliveryStatus,
    Handoff,
    HandoffStatus,
    PatientAssignment,
    Practitioner,
    Profile,
    PushSubscription,
    Reading,
    ReadingContext,
    RiskAssessmentRecord,
    ThresholdHistory,
    User,
    UserRole,
)
from app.db.session import get_session_factory, make_engine
from app.db.threshold_repository import SqlThresholdRepository
from app.ml.predict import RiskAssessment
from app.services.supabase_auth import AuthUser
from app.services.thresholds import (
    ReadingPoint,
    ThresholdInputs,
    classify_reading,
    refresh_user_thresholds,
)

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)

BAND: Final = 0.05
# Commit 6f54669 ("stop the ML risk tier from moving glucose limits"), author date.
RISK_ADJ_ZERO_SINCE: Final = "2026-11-16T21:58:32+05:30"
SECRET_ENDPOINT: Final = "https://push.example.invalid/secret-endpoint-"


@pytest.fixture
def conn() -> Iterator[Connection]:
    engine = make_engine(load_settings().database_url.get_secret_value())
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            yield connection
        finally:
            outer.rollback()
    engine.dispose()


@pytest.fixture
def factory(conn: Connection) -> Callable[[], Session]:
    return lambda: Session(bind=conn, join_transaction_mode="create_savepoint")


def _auth_login(session: Session, label: str) -> uuid.UUID:
    uid = uuid.uuid4()
    session.execute(
        text(
            "insert into auth.users (id, aud, role, email) "
            "values (:id, 'authenticated', 'authenticated', :email)"
        ),
        {"id": uid, "email": f"dash-{label}-{uid}@example.invalid"},
    )
    return uid


def _practitioner(
    session: Session, *, role: UserRole = "practitioner", login: uuid.UUID | None = None
) -> uuid.UUID:
    user = User(id=uuid.uuid4(), role=role, auth_user_id=login)
    session.add(user)
    session.flush()
    practitioner = Practitioner(
        id=uuid.uuid4(),
        user_id=user.id,
        full_name="Dr Dash",
        email=f"dr-{uuid.uuid4()}@example.invalid",
    )
    session.add(practitioner)
    session.flush()
    return practitioner.id


def _patient(
    session: Session,
    *,
    doctor: uuid.UUID | None,
    name: str,
    status: DiabetesStatus | None,
    age: int | None,
) -> uuid.UUID:
    login = _auth_login(session, "patient")
    user = User(id=uuid.uuid4(), role="patient", auth_user_id=login)
    session.add(user)
    session.flush()
    session.add(
        Profile(
            user_id=user.id,
            full_name=name,
            # Recognisable and unique: the tests check it never leaks.
            phone_e164=f"+9198{uuid.uuid4().int % 10**8:08d}",
            diabetes_status=status,
            age_years=age,
        )
    )
    session.add(
        PushSubscription(
            id=uuid.uuid4(),
            user_id=user.id,
            endpoint=f"{SECRET_ENDPOINT}{uuid.uuid4()}",
            p256dh="p256dh-key",
            auth="auth-secret",
        )
    )
    if doctor is not None:
        session.add(PatientAssignment(practitioner_id=doctor, patient_id=user.id))
    session.flush()
    return user.id


def _engine_thresholds(
    session: Session,
    user_id: uuid.UUID,
    status: DiabetesStatus,
    age: int,
    *,
    as_of: datetime,
    risk: RiskAssessment | None = None,
) -> dict[GlucoseLimitKind, uuid.UUID]:
    """Let the real threshold engine write this patient's limits."""
    repo = SqlThresholdRepository(session)
    refresh_user_thresholds(
        repo,
        user_id=user_id,
        inputs=ThresholdInputs(diabetes_status=status, age_years=age),
        risk=risk,
        as_of=as_of,
    )
    session.flush()
    return {kind: t.id for kind, t in repo.current_thresholds(user_id).items()}


def _reading(
    session: Session, user_id: uuid.UUID, value: str, context: ReadingContext, at: datetime
) -> uuid.UUID:
    reading = Reading(
        id=uuid.uuid4(),
        user_id=user_id,
        context=context,
        value_mg_dl=Decimal(value),
        measured_at=at,
        source="seed",
    )
    session.add(reading)
    session.flush()
    return reading.id


def _advisory(
    session: Session,
    *,
    user_id: uuid.UUID,
    reading_id: uuid.UUID,
    threshold_id: uuid.UUID,
    kind: GlucoseLimitKind,
    state: AdvisoryState,
    value: str,
    limit: str,
    delivery: DeliveryStatus,
    now: datetime,
) -> uuid.UUID:
    advisory = Advisory(
        id=uuid.uuid4(),
        user_id=user_id,
        reading_id=reading_id,
        threshold_id=threshold_id,
        limit_kind=kind,
        state=state,
        reading_value_mg_dl=Decimal(value),
        threshold_mg_dl=Decimal(limit),
        language="en",
        message_text="Your reading is close to your limit.",
        escalated=state.startswith("breach"),
        delivery_status=delivery,
        sent_at=now if delivery in ("sent", "opened") else None,
        opened_at=now if delivery == "opened" else None,
        failed_at=now if delivery == "failed" else None,
    )
    session.add(advisory)
    session.flush()
    return advisory.id


def _handoff(
    session: Session, user_id: uuid.UUID, status: HandoffStatus, doctor: uuid.UUID, now: datetime
) -> None:
    session.add(
        Handoff(
            id=uuid.uuid4(),
            user_id=user_id,
            practitioner_id=doctor if status == "assigned" else None,
            trigger="practitioner_request",
            status=status,
            opened_at=now,
            assigned_at=now if status == "assigned" else None,
        )
    )
    session.flush()


class Clinic:
    """Two practitioners and an admin; ``doctor`` has Asha, Ben and Cara.

    * Asha (diabetes, 55): a 50 mg/dL reading (breach_low, failed push) and a fasting
      128 (pre-breach of 130, opened push); an open handoff.
    * Ben (no diabetes, 70, high risk tier): post-meal 150, normal; a claimed handoff.
    * Cara: no readings and no limits.
    * Dev belongs to ``other_doctor`` (one sent advisory).
    """

    def __init__(self, session: Session) -> None:
        self.now = db_now(session)
        now = self.now
        self.doctor_login = _auth_login(session, "doctor")
        self.admin_login = _auth_login(session, "admin")
        self.doctor = _practitioner(session, login=self.doctor_login)
        self.other_doctor = _practitioner(session)
        self.admin = _practitioner(session, role="admin", login=self.admin_login)
        self.empty_doctor = _practitioner(session)

        self.asha = _patient(
            session, doctor=self.doctor, name="Asha Rao Kumar", status="diabetes", age=55
        )
        self.ben = _patient(
            session, doctor=self.doctor, name="Ben Ortiz", status="no_diabetes", age=70
        )
        self.cara = _patient(session, doctor=self.doctor, name="Cara Lund", status=None, age=None)
        self.dev = _patient(
            session, doctor=self.other_doctor, name="Dev Shah", status="diabetes", age=40
        )

        start = now - timedelta(days=10)
        asha_limits = _engine_thresholds(session, self.asha, "diabetes", 55, as_of=start)
        self.high_risk = RiskAssessment(
            probability=0.7, tier="high", flagged=True, model_version="risk-test"
        )
        _engine_thresholds(session, self.ben, "no_diabetes", 70, as_of=start, risk=self.high_risk)
        session.add(
            RiskAssessmentRecord(
                id=uuid.uuid4(),
                user_id=self.ben,
                profile_hash="0" * 64,
                model_version="risk-test",
                risk_score=Decimal("0.7000"),
                risk_tier="high",
                flagged=True,
            )
        )
        dev_limits = _engine_thresholds(session, self.dev, "diabetes", 40, as_of=start)

        self.hypo = _reading(session, self.asha, "50.0", "random", now - timedelta(hours=2))
        self.near = _reading(session, self.asha, "128.0", "fasting", now - timedelta(minutes=30))
        self.ben_reading = _reading(
            session, self.ben, "150.0", "post_meal", now - timedelta(hours=3)
        )
        dev_reading = _reading(session, self.dev, "250.0", "post_meal", now - timedelta(hours=1))

        self.hypo_alert = _advisory(
            session,
            user_id=self.asha,
            reading_id=self.hypo,
            threshold_id=asha_limits["low"],
            kind="low",
            state="breach_low",
            value="50.0",
            limit="70.0",
            delivery="failed",
            now=now,
        )
        self.near_alert = _advisory(
            session,
            user_id=self.asha,
            reading_id=self.near,
            threshold_id=asha_limits["fasting_high"],
            kind="fasting_high",
            state="pre_breach_high",
            value="128.0",
            limit="130.0",
            delivery="opened",
            now=now,
        )
        self.dev_alert = _advisory(
            session,
            user_id=self.dev,
            reading_id=dev_reading,
            threshold_id=dev_limits["post_meal_high"],
            kind="post_meal_high",
            state="breach_high",
            value="250.0",
            limit="180.0",
            delivery="sent",
            now=now,
        )
        _handoff(session, self.asha, "open", self.doctor, now)
        _handoff(session, self.ben, "assigned", self.doctor, now)
        _handoff(session, self.dev, "open", self.other_doctor, now)


@pytest.fixture
def clinic(factory: Callable[[], Session]) -> Iterator[tuple[Session, Clinic]]:
    with factory() as session:
        yield session, Clinic(session)


def _window(c: Clinic) -> dict[str, datetime]:
    return {"since": c.now - timedelta(hours=24), "until": c.now + timedelta(minutes=1)}


# --- overview ------------------------------------------------------------------------


def test_overview_counts_on_seeded_data(clinic: tuple[Session, Clinic]) -> None:
    session, c = clinic
    ov = dash.overview(session, c.doctor, timezone_name="Asia/Kolkata", **_window(c))
    assert ov.readings_window == ov.alerts_window
    assert ov.active_patients == 3
    assert ov.readings == 3
    assert ov.pre_breach_alerts == 1
    assert ov.breaches == 1
    assert ov.below_54_readings == 1
    assert (ov.open_handoffs, ov.claimed_handoffs) == (1, 1)
    d = ov.delivery
    assert d.total == 2
    assert (d.failed.count, d.opened.count, d.sent.count, d.queued.count) == (1, 1, 0, 0)
    assert d.failed.denominator == 2 and d.failed.rate == 0.5
    # Delivered = sent + opened, out of everything not suppressed on purpose.
    assert (d.delivered.count, d.delivered.denominator, d.delivered.rate) == (1, 2, 0.5)
    runs = session.execute(
        text(
            "select started_at, status from batch_runs where job = 'batch_update' "
            "order by started_at desc, id desc limit 1"
        )
    ).first()
    if runs is None:
        assert ov.last_batch_run is None
    else:
        assert ov.last_batch_run is not None
        assert (ov.last_batch_run.started_at, ov.last_batch_run.status) == tuple(runs)


def test_overview_excludes_older_readings_and_suppressed_from_delivery(
    clinic: tuple[Session, Clinic],
) -> None:
    session, c = clinic
    # A suppressed advisory counts in the total but not in the delivery denominator.
    old = _reading(session, c.asha, "60.0", "random", c.now - timedelta(minutes=5))
    low_id = session.scalar(
        select(ThresholdHistory.id).where(
            ThresholdHistory.user_id == c.asha, ThresholdHistory.limit_kind == "low"
        )
    )
    assert low_id is not None
    _advisory(
        session,
        user_id=c.asha,
        reading_id=old,
        threshold_id=low_id,
        kind="low",
        state="breach_low",
        value="60.0",
        limit="70.0",
        delivery="suppressed",
        now=c.now,
    )
    ov = dash.overview(session, c.doctor, timezone_name="Asia/Kolkata", **_window(c))
    assert ov.delivery.total == 3
    assert ov.delivery.suppressed.count == 1
    assert (ov.delivery.delivered.count, ov.delivery.delivered.denominator) == (1, 2)
    narrow = dash.overview(
        session,
        c.doctor,
        since=c.now - timedelta(hours=1),
        until=c.now + timedelta(minutes=1),
        timezone_name="Asia/Kolkata",
    )
    # Only the fasting 128 and the 60 are inside the last hour.
    assert narrow.readings == 2 and narrow.below_54_readings == 0


def test_overview_default_windows(clinic: tuple[Session, Clinic]) -> None:
    session, c = clinic
    ov = dash.overview(session, c.doctor, since=None, until=None, timezone_name="Asia/Kolkata")
    assert ov.alerts_window.until - ov.alerts_window.since == timedelta(hours=24)
    local = ov.readings_window.since.astimezone(ZoneInfo("Asia/Kolkata"))
    assert (local.hour, local.minute, local.second) == (0, 0, 0)
    assert ov.readings_window.until == ov.alerts_window.until


def test_empty_data_gives_null_rates(clinic: tuple[Session, Clinic]) -> None:
    session, c = clinic
    ov = dash.overview(session, c.empty_doctor, timezone_name="Asia/Kolkata", **_window(c))
    assert (ov.active_patients, ov.readings, ov.pre_breach_alerts, ov.breaches) == (0, 0, 0, 0)
    assert ov.delivery.total == 0
    for name in ("queued", "sent", "opened", "failed", "expired", "suppressed", "delivered"):
        cr = getattr(ov.delivery, name)
        assert (cr.count, cr.denominator, cr.rate) == (0, 0, None), name


# --- patient list --------------------------------------------------------------------


def _patients(session: Session, who: uuid.UUID, **filters: object) -> list[uuid.UUID]:
    page = dash.list_patients(session, who, band=BAND, limit=100, **filters)  # type: ignore[arg-type]
    assert page.next_cursor is None
    return [r.user_id for r in page.items]


def test_patient_rows(clinic: tuple[Session, Clinic]) -> None:
    session, c = clinic
    page = dash.list_patients(session, c.doctor, band=BAND, limit=100)
    rows = {r.user_id: r for r in page.items}
    assert set(rows) == {c.asha, c.ben, c.cara}
    asha = rows[c.asha]
    assert (asha.first_name, asha.age_band, asha.diabetes_status) == ("Asha", "50-59", "diabetes")
    current = SqlThresholdRepository(session).current_thresholds(c.asha)
    assert asha.limits.fasting_high == float(current["fasting_high"].threshold_mg_dl)
    assert asha.limits.low == float(current["low"].threshold_mg_dl)
    assert asha.last_reading is not None
    assert (asha.last_reading.value_mg_dl, asha.last_reading.band_state) == (
        128.0,
        "pre_breach_high",
    )
    assert asha.alerts_7d == 2 and asha.breach_7d and asha.live_handoff == "open"
    assert asha.risk is None
    ben = rows[c.ben]
    assert ben.age_band == "70-79" and ben.live_handoff == "assigned"
    assert ben.risk is not None and (ben.risk.tier, ben.risk.model_version) == ("high", "risk-test")
    assert ben.last_reading is not None and ben.last_reading.band_state == "normal"
    assert ben.alerts_7d == 0 and not ben.breach_7d
    cara = rows[c.cara]
    assert cara.last_reading is None and cara.limits.low is None and cara.age_band is None


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"band_state": "pre_breach_high"}, {"asha"}),
        ({"band_state": "normal"}, {"ben"}),
        ({"band_state": "breach_high"}, set()),
        ({"breach_7d": True}, {"asha"}),
        ({"breach_7d": False}, {"ben", "cara"}),
        ({"live_handoff": True}, {"asha", "ben"}),
        ({"live_handoff": False}, {"cara"}),
        ({"live_handoff": True, "breach_7d": True}, {"asha"}),
    ],
)
def test_patient_filters(
    clinic: tuple[Session, Clinic], filters: dict[str, object], expected: set[str]
) -> None:
    session, c = clinic
    names = {c.asha: "asha", c.ben: "ben", c.cara: "cara"}
    assert {names[i] for i in _patients(session, c.doctor, **filters)} == expected


def test_patient_pages_walk_without_gaps_or_repeats(clinic: tuple[Session, Clinic]) -> None:
    session, c = clinic
    seen: list[uuid.UUID] = []
    cursor: dash.PatientCursor | None = None
    for _ in range(10):
        page = dash.list_patients(session, c.doctor, band=BAND, limit=1, cursor=cursor)
        seen += [r.user_id for r in page.items]
        if page.next_cursor is None:
            break
        cursor = dash.PatientCursor.decode(page.next_cursor)
    assert seen == sorted([c.asha, c.ben, c.cara])
    with pytest.raises(ValueError, match="limit"):
        dash.list_patients(session, c.doctor, band=BAND, limit=dash.MAX_PAGE_SIZE + 1)


def test_other_practitioner_and_admin_visibility(clinic: tuple[Session, Clinic]) -> None:
    session, c = clinic
    assert _patients(session, c.other_doctor) == [c.dev]
    everyone: set[uuid.UUID] = set()
    cursor: dash.PatientCursor | None = None
    while True:
        page = dash.list_patients(session, c.admin, band=BAND, limit=100, cursor=cursor)
        everyone |= {r.user_id for r in page.items}
        if page.next_cursor is None:
            break
        cursor = dash.PatientCursor.decode(page.next_cursor)
    assert {c.asha, c.ben, c.cara, c.dev} <= everyone
    admin_alerts = dash.list_alerts(session, c.admin, limit=100, since=c.now - timedelta(days=1))
    assert {c.hypo_alert, c.near_alert, c.dev_alert} <= {a.id for a in admin_alerts.items}


# --- band state: SQL mirror agrees with the Python rule ------------------------------

_LIMITS: Final[dict[GlucoseLimitKind, Decimal]] = {
    "low": Decimal(70),
    "fasting_high": Decimal(130),
    "post_meal_high": Decimal(180),
}


@pytest.mark.parametrize(
    ("value", "context"),
    [
        # low 70: breach <= 70, pre-breach <= 73.5 (1.05 * 70)
        ("69.9", "fasting"),
        ("70.0", "fasting"),
        ("70.1", "fasting"),
        ("73.5", "fasting"),
        ("73.6", "fasting"),
        # fasting 130: pre-breach >= 123.5 (0.95 * 130), breach >= 130
        ("123.4", "fasting"),
        ("123.5", "fasting"),
        ("129.9", "fasting"),
        ("130.0", "fasting"),
        ("130.1", "fasting"),
        # post-meal 180: pre-breach >= 171, breach >= 180
        ("170.9", "post_meal"),
        ("171.0", "post_meal"),
        ("179.9", "post_meal"),
        ("180.0", "post_meal"),
        # random: low limit only
        ("70.0", "random"),
        ("73.5", "random"),
        ("73.6", "random"),
        ("400.0", "random"),
    ],
)
def test_sql_band_state_matches_python(
    factory: Callable[[], Session], value: str, context: ReadingContext
) -> None:
    python = classify_reading(
        ReadingPoint(
            value_mg_dl=Decimal(value), context=context, measured_at=datetime.now().astimezone()
        ),
        _LIMITS,
        band=BAND,
    ).state
    with factory() as session:
        sql = session.scalar(
            select(
                dash._band_state_sql(
                    literal(Decimal(value)),
                    context=literal(context),
                    low=literal(_LIMITS["low"]),
                    fasting_high=literal(_LIMITS["fasting_high"]),
                    post_meal_high=literal(_LIMITS["post_meal_high"]),
                    band=Decimal(str(BAND)),
                )
            )
        )
    assert sql == python


def test_sql_band_state_is_null_where_python_refuses(factory: Callable[[], Session]) -> None:
    with pytest.raises(ThresholdComputationError):
        classify_reading(
            ReadingPoint(
                value_mg_dl=Decimal(100), context="fasting", measured_at=datetime.now().astimezone()
            ),
            {"low": Decimal(70)},
            band=BAND,
        )
    with factory() as session:
        sql = session.scalar(
            select(
                dash._band_state_sql(
                    literal(Decimal(100)),
                    context=literal("fasting"),
                    low=literal(Decimal(70)),
                    fasting_high=literal(None),
                    post_meal_high=literal(None),
                    band=Decimal("0.05"),
                )
            )
        )
    assert sql is None


# --- patient detail ------------------------------------------------------------------


def test_patient_detail(clinic: tuple[Session, Clinic]) -> None:
    session, c = clinic
    detail = dash.patient_detail(session, c.doctor, c.asha, days=30, band=BAND)
    assert detail is not None
    assert detail.pre_breach_band == BAND
    assert [(r.value_mg_dl, r.band_state, r.limit_kind) for r in detail.readings] == [
        (50.0, "breach_low", "low"),
        (128.0, "pre_breach_high", "fasting_high"),
    ]
    assert not any(r.before_first_limit for r in detail.readings)
    assert sorted(t.limit_kind for t in detail.thresholds) == [
        "fasting_high",
        "low",
        "post_meal_high",
    ]
    for t in detail.thresholds:
        clamped = min(max(t.unclamped_mg_dl, t.hard_min_mg_dl), t.hard_max_mg_dl)
        assert t.value_mg_dl == clamped
        assert t.unclamped_mg_dl == pytest.approx(
            t.base_mg_dl + t.demo_adj_mg_dl + t.risk_adj_mg_dl + t.trend_adj_mg_dl
        )
        assert t.source_citation and t.reason == "initial"
    assert [a.id for a in detail.advisories] == [c.near_alert, c.hypo_alert]
    assert [a.delivery_status for a in detail.advisories] == ["opened", "failed"]
    assert dash.patient_detail(session, c.doctor, c.dev, days=30, band=BAND) is None
    assert dash.patient_detail(session, c.doctor, uuid.uuid4(), days=30, band=BAND) is None
    assert dash.patient_detail(session, c.admin, c.dev, days=30, band=BAND) is not None
    with pytest.raises(ValueError, match="days"):
        dash.patient_detail(session, c.doctor, c.asha, days=0, band=BAND)


def test_readings_before_the_first_limit_are_judged_by_it(clinic: tuple[Session, Clinic]) -> None:
    session, c = clinic
    # Older than Asha's first limits (written 10 days ago): judged by those, flagged.
    early = c.now - timedelta(days=20)
    _reading(session, c.asha, "131.0", "fasting", early)
    detail = dash.patient_detail(session, c.doctor, c.asha, days=30, band=BAND)
    assert detail is not None
    first = detail.readings[0]
    assert (first.value_mg_dl, first.band_state, first.before_first_limit) == (
        131.0,
        "breach_high",
        True,
    )


def test_threshold_history_risk_adj_is_zero(clinic: tuple[Session, Clinic]) -> None:
    session, c = clinic
    # Ben: no diabetes, high risk tier. Before 6f54669 this tightened his upper limits.
    detail = dash.patient_detail(session, c.doctor, c.ben, days=30, band=BAND)
    assert detail is not None and detail.thresholds
    assert all(t.risk_adj_mg_dl == 0 for t in detail.thresholds)
    assert detail.risk is not None and detail.risk.tier == "high"


def test_no_threshold_row_written_after_6f54669_moves_a_limit(
    factory: Callable[[], Session],
) -> None:
    with factory() as session:
        moved = session.scalar(
            select(func.count())
            .select_from(ThresholdHistory)
            .where(
                ThresholdHistory.created_at > datetime.fromisoformat(RISK_ADJ_ZERO_SINCE),
                ThresholdHistory.risk_adj_mg_dl != 0,
            )
        )
    assert moved == 0


# --- alerts --------------------------------------------------------------------------


def test_alerts_newest_reading_first_with_filters(clinic: tuple[Session, Clinic]) -> None:
    session, c = clinic
    page = dash.list_alerts(session, c.doctor, limit=100)
    assert [a.id for a in page.items] == [c.near_alert, c.hypo_alert]
    first = page.items[0]
    assert (first.first_name, first.state, first.reading_value_mg_dl, first.threshold_mg_dl) == (
        "Asha",
        "pre_breach_high",
        128.0,
        130.0,
    )
    assert [a.id for a in dash.list_alerts(session, c.doctor, state="breach_low").items] == [
        c.hypo_alert
    ]
    assert [a.id for a in dash.list_alerts(session, c.doctor, escalated=False).items] == [
        c.near_alert
    ]
    assert [a.id for a in dash.list_alerts(session, c.doctor, delivery_status="failed").items] == [
        c.hypo_alert
    ]
    recent = dash.list_alerts(session, c.doctor, since=c.now - timedelta(hours=1))
    assert [a.id for a in recent.items] == [c.near_alert]
    older = dash.list_alerts(session, c.doctor, until=c.now - timedelta(hours=1))
    assert [a.id for a in older.items] == [c.hypo_alert]
    with pytest.raises(ValueError, match="since"):
        dash.list_alerts(session, c.doctor, since=c.now, until=c.now)


def test_alert_pages(clinic: tuple[Session, Clinic]) -> None:
    session, c = clinic
    one = dash.list_alerts(session, c.doctor, limit=1)
    assert [a.id for a in one.items] == [c.near_alert] and one.next_cursor
    two = dash.list_alerts(
        session, c.doctor, limit=1, cursor=dash.AlertCursor.decode(one.next_cursor)
    )
    assert [a.id for a in two.items] == [c.hypo_alert] and two.next_cursor is None


# --- API end to end ------------------------------------------------------------------


def test_api_end_to_end_access_and_privacy(
    conn: Connection, factory: Callable[[], Session]
) -> None:
    from app import main  # noqa: PLC0415
    from app.api.auth import current_auth_user  # noqa: PLC0415

    with factory() as session:
        c = Clinic(session)
        secrets = [
            str(v)
            for v in session.scalars(
                select(Profile.phone_e164).where(Profile.user_id.in_([c.asha, c.ben, c.dev]))
            )
        ]
        logins = [c.doctor_login, c.admin_login]
        logins += [
            u
            for u in session.scalars(
                select(User.auth_user_id).where(User.id.in_([c.asha, c.ben, c.cara, c.dev]))
            )
            if u is not None
        ]
        secrets += [
            str(v)
            for v in session.scalars(
                text("select email from auth.users where id = any(:ids)"), {"ids": logins}
            )
        ]
        assert len(secrets) == 3 + 6
        session.commit()

    app = main.app
    api_factory = sessionmaker(
        bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    login = {"who": AuthUser(id=c.doctor_login, email="doctor@example.invalid")}
    app.dependency_overrides[get_session_factory] = lambda: api_factory
    app.dependency_overrides[current_auth_user] = lambda: login["who"]
    try:
        client = TestClient(app)
        bodies: list[str] = []
        for url in (
            "/api/dashboard/overview",
            "/api/dashboard/patients?limit=2",
            f"/api/dashboard/patients/{c.asha}?days=30",
            f"/api/dashboard/patients/{c.ben}",
            "/api/dashboard/alerts?limit=5",
        ):
            response = client.get(url)
            assert response.status_code == 200, (url, response.text)
            bodies.append(response.text)
        first = client.get("/api/dashboard/patients?limit=2").json()
        rest = client.get(
            "/api/dashboard/patients", params={"limit": 2, "cursor": first["next_cursor"]}
        ).json()
        assert len(first["items"]) + len(rest["items"]) == 3 and rest["next_cursor"] is None
        assert client.get(f"/api/dashboard/patients/{c.dev}").status_code == 404
        assert client.get(f"/api/dashboard/patients/{uuid.uuid4()}").status_code == 404
        ben = json.loads(bodies[3])
        assert ben["thresholds"] and all(t["risk_adj_mg_dl"] == 0 for t in ben["thresholds"])

        login["who"] = AuthUser(id=c.admin_login, email="admin@example.invalid")
        assert client.get(f"/api/dashboard/patients/{c.dev}").status_code == 200
    finally:
        app.dependency_overrides.clear()

    everything = "\n".join(bodies)
    for leak in (*secrets, SECRET_ENDPOINT, "Kumar", "Ortiz", "+91", "@"):
        assert leak not in everything, leak
    for key in ("phone", "email", "endpoint", "full_name", "p256dh"):
        assert f'"{key}' not in everything, key
