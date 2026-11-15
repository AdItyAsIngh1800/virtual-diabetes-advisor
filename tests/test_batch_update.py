"""Tests for the batch update job (R1).

Pure helpers run everywhere. The round-trip tests are opt-in (``RUN_DB_TESTS=1``): each
runs inside one outer transaction on Supabase that is rolled back at the end. Sessions
join it with ``join_transaction_mode="create_savepoint"``, so the job's per-user
commits and rollbacks become savepoints and nothing is left behind.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, create_engine, func, select, text, update
from sqlalchemy.orm import Session

from app.core.config import Settings, load_settings
from app.core.exceptions import BatchAlreadyRunningError, ModelNotTrainedError
from app.db.models import (
    Advisory,
    BatchRun,
    ConversationTurn,
    Profile,
    Reading,
    ThresholdHistory,
    User,
)
from app.db.session import sqlalchemy_url
from app.jobs import batch_update
from app.jobs.batch_update import (
    BatchRunSummary,
    _failure_summary,
    profile_digest,
    risk_profile_from,
    run_batch_update,
)
from app.ml.predict import RiskAssessment, RiskModel
from app.ml.schema import RiskProfile
from app.services.thresholds import refresh_user_thresholds

live = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)

# A complete BRFSS profile (model input) for users that should be scored.
BRFSS: dict[str, Any] = {
    "high_bp": True,
    "high_chol": False,
    "chol_check": True,
    "bmi": Decimal("31.0"),
    "smoker": False,
    "stroke": False,
    "heart_disease_or_attack": False,
    "phys_activity": True,
    "fruits": True,
    "veggies": True,
    "hvy_alcohol_consump": False,
    "any_healthcare": True,
    "no_doc_bc_cost": False,
    "gen_hlth": 3,
    "ment_hlth": 2,
    "phys_hlth": 1,
    "diff_walk": False,
    "education": 5,
    "income": 6,
}


class FakeScorer:
    """Risk model stand-in that counts calls."""

    version = "fake-v1"

    def __init__(self) -> None:
        self.calls = 0

    def assess(self, profile: RiskProfile) -> RiskAssessment:
        self.calls += 1
        return RiskAssessment(
            probability=0.5, tier="high", flagged=True, model_version=self.version
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _profile(**overrides: Any) -> Profile:
    fields = {
        "user_id": uuid.uuid4(),
        "phone_e164": "+15550999",
        "sex": 1,
        "age_years": 57,
        "brfss_age_bucket": 9,
        **BRFSS,
    }
    return Profile(**{**fields, **overrides})


def test_risk_profile_from_complete_profile() -> None:
    risk_profile = risk_profile_from(_profile())
    assert risk_profile is not None
    assert risk_profile.age == 9
    assert risk_profile.bmi == 31.0


@pytest.mark.parametrize("missing", ["bmi", "income", "brfss_age_bucket", "sex"])
def test_incomplete_profile_is_unscored(missing: str) -> None:
    assert risk_profile_from(_profile(**{missing: None})) is None


def test_profile_digest_tracks_model_inputs_only() -> None:
    base = risk_profile_from(_profile())
    same = risk_profile_from(_profile(full_name="Someone Else", phone_e164="+15550998"))
    changed = risk_profile_from(_profile(bmi=Decimal("31.5")))
    assert base is not None and same is not None and changed is not None
    assert profile_digest(base) == profile_digest(same)
    assert profile_digest(base) != profile_digest(changed)
    assert len(profile_digest(base)) == 64


def test_failure_summary_lists_ids_and_types_only() -> None:
    ids = [uuid.uuid4() for _ in range(12)]
    text = _failure_summary([(i, "ValueError") for i in ids], 20)
    assert text.startswith("12 of 20 users failed: ")
    assert str(ids[0]) in text and str(ids[9]) in text
    assert str(ids[10]) not in text
    assert text.endswith("and 2 more")


# ---------------------------------------------------------------------------
# Live database
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[Connection]:
    url = sqlalchemy_url(load_settings().database_url.get_secret_value())
    engine = create_engine(url, connect_args={"prepare_threshold": None})
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


@pytest.fixture
def settings() -> Settings:
    return load_settings().model_copy(
        update={"advisory_cooldown_hours": 24, "pre_breach_band": 0.05}
    )


def clock(conn: Connection) -> datetime:
    """Database wall clock (now() is frozen at the start of the outer transaction)."""
    moment = conn.scalar(select(func.clock_timestamp()))
    assert isinstance(moment, datetime)
    return moment


def add_patient(
    factory: Callable[[], Session],
    readings: list[tuple[datetime, str, str]],
    *,
    status: str = "diabetes",
    brfss: bool = False,
) -> uuid.UUID:
    """Create an active patient with readings ``(measured_at, context, value)``."""
    with factory() as session:
        user = User(id=uuid.uuid4(), role="patient")
        session.add(user)
        session.flush()
        phone = f"+4479{uuid.uuid4().int % 10**8:08d}"
        extra = BRFSS if brfss else {}
        session.add(
            Profile(
                user_id=user.id,
                phone_e164=phone,
                age_years=57,
                sex=1,
                diabetes_status=status,
                **extra,
            )
        )
        for measured_at, context, value in readings:
            session.add(
                Reading(
                    user_id=user.id,
                    context=context,
                    value_mg_dl=Decimal(value),
                    measured_at=measured_at,
                    source="api",
                )
            )
        session.commit()
        return user.id


def advisory_count(factory: Callable[[], Session], user_id: uuid.UUID) -> int:
    with factory() as session:
        count = session.scalar(
            select(func.count()).select_from(Advisory).where(Advisory.user_id == user_id)
        )
        return int(count or 0)


def run(
    factory: Callable[[], Session],
    settings: Settings,
    users: list[uuid.UUID],
    end: datetime,
    *,
    start: datetime | None = None,
    scorer: FakeScorer | None = None,
) -> BatchRunSummary:
    return run_batch_update(
        factory,
        scorer=scorer,
        triggered_by="manual",
        window=(start, end),
        user_ids=users,
        settings=settings,
    )


@live
def test_same_window_twice_creates_no_duplicate_advisories(
    conn: Connection, factory: Callable[[], Session], settings: Settings
) -> None:
    now = clock(conn)
    days = [now - timedelta(hours=25 * k) for k in range(1, 6)]
    user = add_patient(
        factory,
        [(t, "fasting", "125.0") for t in days[1:]]  # pre-breach of 130
        + [(days[0], "fasting", "135.0")]  # breach
        + [(days[0] + timedelta(hours=6), "post_meal", "150.0")],  # normal
    )
    scorer = FakeScorer()
    first = run(factory, settings, [user], now + timedelta(seconds=1), scorer=scorer)
    assert first.status == "succeeded"
    assert first.users_processed == 1
    assert first.readings_evaluated == 6
    assert first.thresholds_written == 3
    assert first.advisories_by_state == {"pre_breach_high": 4, "breach_high": 1}
    assert advisory_count(factory, user) == 5

    second = run(factory, settings, [user], now + timedelta(seconds=1), scorer=scorer)
    assert second.status == "succeeded"
    assert second.readings_evaluated == 6
    assert second.advisories_created == 0
    assert second.thresholds_written == 0
    assert advisory_count(factory, user) == 5

    with factory() as session:
        rows = session.scalars(
            select(BatchRun).where(BatchRun.id.in_([first.run_id, second.run_id]))
        )
        recorded = {r.id: r for r in rows}
        assert recorded[first.run_id].status == "succeeded"
        assert recorded[first.run_id].advisories_created == 5
        assert recorded[first.run_id].finished_at is not None
        assert recorded[second.run_id].advisories_created == 0
        queued = session.scalars(select(Advisory.delivery_status).where(Advisory.user_id == user))
        assert set(queued) == {"queued"}
        escalated = session.scalars(
            select(Advisory.escalated).where(
                Advisory.user_id == user, Advisory.state == "breach_high"
            )
        )
        assert list(escalated) == [True]


@live
def test_default_window_starts_after_last_successful_run(
    conn: Connection, factory: Callable[[], Session], settings: Settings
) -> None:
    now = clock(conn)
    user = add_patient(factory, [(now - timedelta(days=2), "fasting", "126.0")])
    first = run(factory, settings, [user], now + timedelta(seconds=1))
    assert first.readings_evaluated == 1
    # A reading stored after that run (explicit created_at: now() is frozen in tests).
    later = clock(conn) + timedelta(seconds=2)
    with factory() as session:
        session.add(
            Reading(
                user_id=user,
                context="fasting",
                value_mg_dl=Decimal("126.0"),
                measured_at=later - timedelta(minutes=5),
                source="api",
                created_at=later,
            )
        )
        session.commit()
    second = run_batch_update(
        factory,
        scorer=None,
        user_ids=[user],
        settings=settings,
        now=later + timedelta(seconds=1),
    )
    assert second.window_start == first.window_end
    assert second.readings_evaluated == 1
    assert second.advisories_created == 1


@live
def test_cooldown_limits_advisories_per_state(
    conn: Connection, factory: Callable[[], Session], settings: Settings
) -> None:
    now = clock(conn)
    hours = [30, 28, 26, 2]  # -30 issues; -28, -26 within 24 h; -2 is 28 h later
    readings = [(now - timedelta(hours=h), "fasting", "125.0") for h in hours]
    cooled = add_patient(factory, readings)
    summary = run(factory, settings, [cooled], now + timedelta(seconds=1))
    assert summary.advisories_by_state == {"pre_breach_high": 2}

    short = settings.model_copy(update={"advisory_cooldown_hours": 1})
    uncooled = add_patient(factory, readings)
    summary = run(factory, short, [uncooled], now + timedelta(seconds=1))
    assert summary.advisories_by_state == {"pre_breach_high": 4}


@live
def test_failure_for_one_user_does_not_stop_the_others(
    conn: Connection,
    factory: Callable[[], Session],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = clock(conn)
    readings = [(now - timedelta(hours=3), "fasting", "125.0")]
    bad = add_patient(factory, readings)
    good = add_patient(factory, readings)
    real_refresh = refresh_user_thresholds

    def flaky(repo: Any, *, user_id: uuid.UUID, **kwargs: Any) -> Any:
        if user_id == bad:
            raise RuntimeError("simulated failure")
        return real_refresh(repo, user_id=user_id, **kwargs)

    monkeypatch.setattr(batch_update, "refresh_user_thresholds", flaky)
    end = now + timedelta(seconds=1)
    summary = run(factory, settings, [bad, good], end)
    assert summary.status == "failed"
    assert summary.users_processed == 1
    assert summary.users_failed == 1
    assert summary.failed_user_ids == [bad]
    assert advisory_count(factory, good) == 1
    assert advisory_count(factory, bad) == 0
    with factory() as session:
        stored = session.scalar(
            select(func.count())
            .select_from(ThresholdHistory)
            .where(ThresholdHistory.user_id == bad)
        )
        assert stored == 0  # the failed user's partial writes were rolled back
        row = session.get(BatchRun, summary.run_id)
        assert row is not None and row.status == "failed"
        assert row.error is not None and str(bad) in row.error and "RuntimeError" in row.error

    # Next run retries the window: the failed user is processed, no duplicates for the other.
    monkeypatch.setattr(batch_update, "refresh_user_thresholds", real_refresh)
    retry = run(factory, settings, [bad, good], end)
    assert retry.status == "succeeded"
    assert retry.advisories_created == 1
    assert advisory_count(factory, good) == 1
    assert advisory_count(factory, bad) == 1


@live
def test_risk_rescored_only_when_profile_changes(
    conn: Connection, factory: Callable[[], Session], settings: Settings
) -> None:
    now = clock(conn)
    user = add_patient(
        factory, [(now - timedelta(hours=3), "fasting", "99.0")], status="no_diabetes", brfss=True
    )
    scorer = FakeScorer()
    end = now + timedelta(seconds=1)
    assert run(factory, settings, [user], end, scorer=scorer).risk_rescored == 1
    assert run(factory, settings, [user], end, scorer=scorer).risk_rescored == 0
    assert scorer.calls == 1
    with factory() as session:
        profile = session.get(Profile, user)
        assert profile is not None
        profile.bmi = Decimal("35.0")
        session.commit()
    assert run(factory, settings, [user], end, scorer=scorer).risk_rescored == 1
    assert scorer.calls == 2


@live
def test_concurrent_run_is_rejected_and_stale_run_reclaimed(
    conn: Connection, factory: Callable[[], Session], settings: Settings
) -> None:
    now = clock(conn)
    with factory() as session:
        session.add(BatchRun(job="batch_update", status="running", started_at=now))
        session.commit()
    with pytest.raises(BatchAlreadyRunningError):
        run_batch_update(factory, scorer=None, user_ids=[], settings=settings)

    with factory() as session:
        session.execute(
            update(BatchRun)
            .where(BatchRun.status == "running")
            .values(started_at=now - timedelta(hours=2))
        )
        session.commit()
    summary = run_batch_update(factory, scorer=None, user_ids=[], settings=settings)
    assert summary.status == "succeeded"


@live
def test_seed_script_produces_every_advisory_state(
    conn: Connection, factory: Callable[[], Session], settings: Settings
) -> None:
    from scripts.seed_demo import DEMO_PATIENTS, seed_demo  # noqa: PLC0415

    scorer = batch_update.load_risk_scorer()
    now = clock(conn)
    with factory() as session:
        report = seed_demo(session, now=now, scorer=scorer)
        session.commit()
    assert len(report.patients) == len(DEMO_PATIENTS)
    ids = [p.user_id for p in report.patients]

    summary = run_batch_update(
        factory,
        scorer=scorer,
        user_ids=ids,
        window=(None, now + timedelta(seconds=1)),
        settings=settings,
    )
    assert summary.status == "succeeded"
    assert summary.users_processed == len(ids)
    for state in ("pre_breach_high", "pre_breach_low", "breach_high", "breach_low"):
        assert summary.advisories_by_state.get(state, 0) >= 1, state
    with factory() as session:
        level_2 = session.scalar(
            select(func.count())
            .select_from(Advisory)
            .where(
                Advisory.user_id.in_(ids),
                Advisory.reading_value_mg_dl < 54,
                Advisory.escalated,
            )
        )
        assert level_2 and level_2 >= 1
        demo = session.scalars(select(User.is_demo).where(User.id.in_(ids)))
        assert set(demo) == {True}

    # Re-running the seed replaces the cohort (advisories included) instead of failing.
    with factory() as session:
        again = seed_demo(session, now=clock(conn), scorer=scorer)
        session.commit()
    # Demo users with conversation turns (e.g. your own, if you chatted) are kept, not
    # deleted; their seeded advisories are still cleared.
    assert again.deleted_users + again.kept_users >= len(ids)
    assert sum(advisory_count(factory, uid) for uid in ids) == 0


def add_login(conn: Connection) -> tuple[uuid.UUID, str]:
    """Create a Supabase login (auth.users row) inside the test transaction."""
    auth_id, email = uuid.uuid4(), f"seed-test-{uuid.uuid4().hex[:8]}@example.com"
    conn.execute(
        text("insert into auth.users (id, email) values (:id, :email)"),
        {"id": auth_id, "email": email},
    )
    return auth_id, email


@live
def test_reseed_keeps_demo_user_with_conversation_turns(
    conn: Connection, factory: Callable[[], Session], settings: Settings
) -> None:
    from scripts.seed_demo import DEMO_PATIENTS, seed_demo  # noqa: PLC0415

    (my_login, my_email), (new_login, new_email) = add_login(conn), add_login(conn)
    slot = next(i for i, spec in enumerate(DEMO_PATIENTS) if spec.my_login_slot)
    with factory() as session:
        first = seed_demo(session, now=clock(conn), scorer=None, my_email=my_email)
        session.commit()
    ids = [p.user_id for p in first.patients]
    me = ids[slot]
    assert [p.linked for p in first.patients].count(True) == 1
    assert first.patients[slot].linked
    run_batch_update(
        factory, scorer=None, user_ids=ids, window=(None, clock(conn)), settings=settings
    )

    def turn_count() -> int:
        with factory() as session:
            count = session.scalar(
                select(func.count())
                .select_from(ConversationTurn)
                .where(ConversationTurn.user_id == me)
            )
            return int(count or 0)

    # The database may already hold turns for this patient (the seed keeps a demo user
    # with history and links your login to it), so count relative to that.
    turns_before = turn_count()

    # The chat stored an inbound message, and a reply referred to one advisory.
    with factory() as session:
        audited = session.scalars(select(Advisory).where(Advisory.user_id == me)).first()
        assert audited is not None, "the linked patient should have an advisory"
        audited_id, audited_reading = audited.id, audited.reading_id
        session.add_all(
            [
                ConversationTurn(
                    user_id=me, channel="web", direction="inbound",
                    raw_text="hi", message_id=f"msg-in-{uuid.uuid4()}",
                ),
                ConversationTurn(
                    user_id=me, channel="web", direction="outbound",
                    advisory_id=audited_id, message_id=f"msg-out-{uuid.uuid4()}",
                ),
            ]
        )  # fmt: skip
        session.commit()
        old_readings = set(session.scalars(select(Reading.id).where(Reading.user_id == me)))

    with factory() as session:
        again = seed_demo(session, now=clock(conn), scorer=None, my_email=my_email)
        session.commit()

    assert again.patients[slot].user_id == me
    assert again.patients[slot].kept is True
    assert again.preserved_advisories == 1
    assert again.deleted_users + again.kept_users >= len(ids)
    others = {p.user_id for i, p in enumerate(again.patients) if i != slot}
    assert others.isdisjoint(ids)  # everyone else was replaced
    assert turn_count() == turns_before + 2
    with factory() as session:
        advisories = list(session.scalars(select(Advisory.id).where(Advisory.user_id == me)))
        assert advisories == [audited_id]  # only the audited advisory survived
        thresholds = session.scalar(
            select(func.count()).select_from(ThresholdHistory).where(ThresholdHistory.user_id == me)
        )
        assert thresholds == 1  # the audited advisory's threshold
        readings = set(session.scalars(select(Reading.id).where(Reading.user_id == me)))
        assert readings & old_readings == {audited_reading}
        assert len(readings) == again.patients[slot].readings + 1
        assert session.scalar(select(User.auth_user_id).where(User.id == me)) == my_login

    # A different --my-email re-attaches to the same user; no --my-email keeps the link.
    for requested, expected in ((new_email, new_login), (None, new_login)):
        with factory() as session:
            report = seed_demo(session, now=clock(conn), scorer=None, my_email=requested)
            session.commit()
            assert report.patients[slot].user_id == me
            assert session.scalar(select(User.auth_user_id).where(User.id == me)) == expected

    # The batch job rebuilds thresholds and advisories for the kept user.
    summary = run_batch_update(
        factory, scorer=None, user_ids=[me], window=(None, clock(conn)), settings=settings
    )
    assert (summary.status, summary.users_failed) == ("succeeded", 0)
    assert advisory_count(factory, me) > 1


@live
def test_seed_rejects_unknown_or_real_patient_login(
    conn: Connection, factory: Callable[[], Session]
) -> None:
    from scripts.seed_demo import SeedError, seed_demo  # noqa: PLC0415

    with factory() as session, pytest.raises(SeedError, match="create the account"):
        seed_demo(session, now=clock(conn), scorer=None, my_email="nobody-xyz@example.com")
    auth_id, email = add_login(conn)
    with factory() as session:
        session.add(User(id=uuid.uuid4(), role="patient", auth_user_id=auth_id))
        session.commit()
    with factory() as session, pytest.raises(SeedError, match="real"):
        seed_demo(session, now=clock(conn), scorer=None, my_email=email)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgres://u:p@h:5432/db",
        "postgresql://u:p@h:5432/db",
        "postgresql+psycopg://u:p@h:5432/db",
    ],
)
def test_sqlalchemy_url_uses_psycopg(dsn: str) -> None:
    assert sqlalchemy_url(dsn) == "postgresql+psycopg://u:p@h:5432/db"


def test_sqlalchemy_url_error_hides_password() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL") as exc:
        sqlalchemy_url("mysql://u:hunter2@h/db")
    assert "hunter2" not in str(exc.value)


def test_missing_model_means_unscored(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(cls: Any) -> Any:
        raise ModelNotTrainedError("no model")

    monkeypatch.setattr(RiskModel, "load", classmethod(missing))
    batch_update.load_risk_scorer.cache_clear()
    try:
        assert batch_update.load_risk_scorer() is None
    finally:
        batch_update.load_risk_scorer.cache_clear()


@live
def test_patient_without_diabetes_status_is_skipped(
    conn: Connection, factory: Callable[[], Session], settings: Settings
) -> None:
    now = clock(conn)
    user_id = uuid.uuid4()
    with factory() as session:
        session.add(User(id=user_id, role="patient"))
        session.flush()
        session.add(Profile(user_id=user_id, phone_e164=f"+4479{uuid.uuid4().int % 10**8:08d}"))
        session.commit()
    summary = run(factory, settings, [user_id], now + timedelta(seconds=1))
    assert summary.status == "succeeded"
    assert summary.users_skipped == 1
    assert summary.users_processed == 0


@live
def test_level_2_hypos_bypass_cooldown_end_to_end(
    conn: Connection, factory: Callable[[], Session], settings: Settings
) -> None:
    now = clock(conn)
    user = add_patient(
        factory,
        [
            (now - timedelta(hours=6), "fasting", "52.0"),  # Level 2
            (now - timedelta(hours=4), "random", "49.5"),  # Level 2, 2 h later
            (now - timedelta(hours=3), "random", "66.0"),  # ordinary low: cooled down
        ],
    )
    summary = run(factory, settings, [user], now + timedelta(seconds=1))
    assert summary.advisories_by_state == {"breach_low": 2}
    with factory() as session:
        rows = session.execute(
            select(Advisory.reading_value_mg_dl, Advisory.escalated).where(Advisory.user_id == user)
        ).all()
    assert sorted(rows) == [(Decimal("49.5"), True), (Decimal("52.0"), True)]
