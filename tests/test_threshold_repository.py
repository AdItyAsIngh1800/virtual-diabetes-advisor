"""Tests for the SQL threshold repository.

The SQL-shape test runs everywhere. The round-trip tests are opt-in
(``RUN_DB_TESTS=1``) and run inside one transaction that is rolled back.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.core.config import load_settings
from app.db.models import Reading, User
from app.db.session import CONNECT_ARGS
from app.db.threshold_repository import SqlThresholdRepository, current_thresholds_query
from app.ml.predict import RiskAssessment
from app.services.thresholds import (
    ReadingPoint,
    ThresholdInputs,
    analyse_trend,
    plan_threshold_update,
    refresh_user_thresholds,
)

AS_OF = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)
# 70-year-old with diabetes and 3 complex-health proxy conditions (ADA-13 complex).
INPUTS = ThresholdInputs(
    diabetes_status="diabetes",
    age_years=70,
    sex=0,
    high_bp=True,
    high_chol=True,
    diff_walk=True,
)
RISK = RiskAssessment(probability=0.62, tier="high", flagged=True, model_version="test-v1")

live = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)


def test_current_values_are_read_from_the_view() -> None:
    sql = str(
        current_thresholds_query(uuid.uuid4()).compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    )
    assert "FROM current_thresholds" in sql
    assert "thresholds_history" not in sql


@pytest.fixture
def session() -> Iterator[Session]:
    url = load_settings().database_url.get_secret_value()
    # The URL is a plain postgresql:// DSN; SQLAlchemy needs the psycopg 3 driver named.
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), connect_args=dict(CONNECT_ARGS)
    )
    with Session(engine) as db:
        try:
            yield db
        finally:
            db.rollback()
    engine.dispose()


@pytest.fixture
def user_id(session: Session) -> uuid.UUID:
    user = User(role="patient")
    session.add(user)
    session.flush()
    return user.id


@live
def test_save_and_read_back_through_view(session: Session, user_id: uuid.UUID) -> None:
    repo = SqlThresholdRepository(session)
    decision = plan_threshold_update(
        limit_kind="fasting_high",
        inputs=INPUTS,
        risk=RISK,
        trend=analyse_trend(
            [
                ReadingPoint(
                    value_mg_dl=Decimal(v),
                    context="fasting",
                    measured_at=AS_OF - timedelta(days=d),
                )
                for d, v in ((2, 120), (1, 126), (0, 131))
            ],
            AS_OF,
        ),
        previous=None,
    )
    assert decision.threshold.ewma_mg_dl is not None
    saved = repo.save_threshold(user_id, decision.threshold, effective_from=AS_OF)
    current = repo.current_thresholds(user_id)
    assert set(current) == {"fasting_high"}
    row = current["fasting_high"]
    assert row.id == saved.id
    assert row.threshold_mg_dl == decision.threshold.threshold_mg_dl == Decimal("140.0")
    assert row.demo_adj_mg_dl == Decimal(10)
    assert row.risk_adj_mg_dl == 0  # BRFSS tier never moves a diabetic user's limits
    assert row.risk_tier == "high"
    assert row.ewma_mg_dl == decision.threshold.ewma_mg_dl
    assert row.hard_min_mg_dl == Decimal("100.0")
    assert row.hard_max_mg_dl == Decimal("180.0")
    assert row.model_version == "test-v1"
    assert row.risk_score == Decimal("0.6200")
    assert row.source_citation == decision.threshold.source_citation


@live
def test_newest_row_is_current(session: Session, user_id: uuid.UUID) -> None:
    repo = SqlThresholdRepository(session)
    for when, tier in ((AS_OF - timedelta(days=1), "low"), (AS_OF, "high")):
        risk = RiskAssessment(probability=0.5, tier=tier, flagged=False, model_version="v")
        decision = plan_threshold_update(
            limit_kind="fasting_high",
            inputs=INPUTS,
            risk=risk,
            trend=analyse_trend([], when),
            previous=None,
        )
        repo.save_threshold(user_id, decision.threshold, effective_from=when)
    assert repo.current_thresholds(user_id)["fasting_high"].risk_tier == "high"


@live
def test_readings_since(session: Session, user_id: uuid.UUID) -> None:
    for days_ago, value in ((20, 150), (3, 128), (1, 131)):
        session.add(
            Reading(
                user_id=user_id,
                context="fasting",
                value_mg_dl=Decimal(value),
                measured_at=AS_OF - timedelta(days=days_ago),
                source="seed",
            )
        )
    session.flush()
    readings = SqlThresholdRepository(session).readings_since(user_id, AS_OF - timedelta(days=14))
    assert [r.value_mg_dl for r in readings] == [Decimal(128), Decimal(131)]
    assert all(r.measured_at.tzinfo is not None for r in readings)


@live
def test_refresh_end_to_end(session: Session, user_id: uuid.UUID) -> None:
    repo = SqlThresholdRepository(session)
    first = refresh_user_thresholds(
        repo, user_id=user_id, inputs=INPUTS, risk=RISK, as_of=AS_OF, batch_run_id=None
    )
    assert all(d.action == "initial" for d in first.values())
    second = refresh_user_thresholds(
        repo, user_id=user_id, inputs=INPUTS, risk=RISK, as_of=AS_OF, batch_run_id=None
    )
    assert all(d.action == "kept" for d in second.values())
    assert len(repo.current_thresholds(user_id)) == 3
