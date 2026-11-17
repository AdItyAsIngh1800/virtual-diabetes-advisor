"""Tests for the risk-model card (R5): the pure builder and ``GET /api/practitioner/model-card``.

No database: the practitioner lookup and the retrain-run query are faked; the model
folder is the real ``models/`` or a temporary copy.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from app import main
from app.api import audit, chat
from app.api.auth import current_auth_user
from app.core.config import Settings, get_settings
from app.core.exceptions import ModelMetadataError, ModelNotTrainedError
from app.db.models import BatchRun, Practitioner
from app.db.session import get_session_factory
from app.ml.artifacts import DEFAULT_MODEL_DIR, load_metadata, model_paths
from app.ml.model_card import LIMITATIONS, CardMetrics, build_model_card
from app.services.supabase_auth import AuthUser

SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="srk",
    database_url="postgresql://u:p@localhost:5432/db",
)
DOCTOR_LOGIN = AuthUser(id=uuid.uuid4(), email="doctor@example.com")
PATIENT_LOGIN = AuthUser(id=uuid.uuid4(), email="patient@example.com")
DOCTOR = Practitioner(id=uuid.uuid4(), user_id=uuid.uuid4(), full_name="Dr Demo")
URL = "/api/practitioner/model-card"


# --- the real metadata file and the pure builder -----------------------------------------


def test_real_meta_file_validates_and_builds_a_card() -> None:
    metadata = load_metadata(DEFAULT_MODEL_DIR)
    card = build_model_card(metadata)
    (tn, fp), (fn, tp) = metadata.test.confusion
    assert (card.confusion.tn, card.confusion.fp, card.confusion.fn, card.confusion.tp) == (
        tn,
        fp,
        fn,
        tp,
    )
    assert card.test_rows == tn + fp + fn + tp
    assert card.version == metadata.version
    assert card.test_metrics.recall == metadata.test.recall
    assert card.decision_threshold == metadata.decision_threshold


def test_flagged_without_diabetes_is_fp_over_fp_plus_tp() -> None:
    metadata = load_metadata(DEFAULT_MODEL_DIR)
    card = build_model_card(metadata)
    share = card.flagged_without_diabetes
    assert share.count == card.confusion.fp
    assert share.denominator == card.confusion.fp + card.confusion.tp
    assert share.rate == pytest.approx(card.confusion.fp / (card.confusion.fp + card.confusion.tp))
    # The point of showing it: with this recall-first threshold most flags are wrong.
    assert share.rate is not None and share.rate > 0.5
    # And it is 1 - precision, the same fact from the other side.
    assert share.rate == pytest.approx(1 - card.test_metrics.precision)


def test_roc_auc_comes_after_precision_recall_and_f1() -> None:
    fields = list(CardMetrics.model_fields)
    assert fields[:3] == ["precision", "recall", "f1"]
    assert fields.index("roc_auc") > fields.index("f1")


def test_limitations_say_what_the_model_is_not() -> None:
    text = " ".join(LIMITATIONS)
    for phrase in (
        "2015 US self-reported survey data",
        "does not predict glucose and is not real-time",
        "not a diagnosis and is not clinically validated",
        "favours recall, so most people it flags do not have diabetes",
        "never changes a glucose limit",
    ):
        assert phrase in text, phrase


def test_no_flags_gives_a_null_rate_not_a_division_error(tmp_path: Path) -> None:
    raw = json.loads(model_paths(DEFAULT_MODEL_DIR)[1].read_text())
    raw["test"]["confusion"] = [[100, 0], [10, 0]]
    model_paths(tmp_path)[1].write_text(json.dumps(raw))
    card = build_model_card(load_metadata(tmp_path))
    assert card.flagged_without_diabetes.model_dump() == {
        "count": 0,
        "denominator": 0,
        "rate": None,
    }


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda raw: "{not json",
        lambda raw: json.dumps({**raw, "test": {**raw["test"], "confusion": [[1, 2, 3]]}}),
        lambda raw: json.dumps({**raw, "test": {**raw["test"], "confusion": [[1, -2], [3, 4]]}}),
        lambda raw: json.dumps({k: v for k, v in raw.items() if k != "tier_cutoffs"}),
    ],
)
def test_malformed_meta_raises_model_metadata_error(tmp_path: Path, corrupt: object) -> None:
    raw = json.loads(model_paths(DEFAULT_MODEL_DIR)[1].read_text())
    model_paths(tmp_path)[1].write_text(corrupt(raw))  # type: ignore[operator]
    with pytest.raises(ModelMetadataError):
        load_metadata(tmp_path)


def test_missing_meta_raises_model_not_trained(tmp_path: Path) -> None:
    with pytest.raises(ModelNotTrainedError):
        load_metadata(tmp_path)


# --- API ---------------------------------------------------------------------------------


class FakeSession:
    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


RUN = BatchRun(
    id=uuid.uuid4(),
    job="retrain",
    triggered_by="manual",
    status="succeeded",
    started_at=datetime(2026, 11, 17, 8, 0, tzinfo=UTC),
    finished_at=datetime(2026, 11, 17, 8, 5, tzinfo=UTC),
    retrain_decision="kept",
    retrain_report={
        "dry_run": False,
        "sample_frac": 0.2,
        "old": None,
        "new": None,
        "reasons": ["test recall 0.8000 vs current 0.8824 (minimum 0.8724): too low"],
    },
    error=None,
)


class FakeRuns:
    """Stands in for SqlBatchRepository: returns as many retrain runs as asked for."""

    asked: ClassVar[list[tuple[str, int]]] = []

    def __init__(self, _session: object) -> None:
        return None

    def recent_runs(self, job: str, limit: int) -> list[BatchRun]:
        FakeRuns.asked.append((job, limit))
        return [RUN] * limit


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(
        chat,
        "find_practitioner_by_auth_user",
        lambda _s, auth_id: DOCTOR if auth_id == DOCTOR_LOGIN.id else None,
    )
    monkeypatch.setattr(audit, "SqlBatchRepository", FakeRuns)
    FakeRuns.asked = []
    app = main.app
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_session_factory] = lambda: FakeSession
    app.dependency_overrides[current_auth_user] = lambda: DOCTOR_LOGIN
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def test_model_card_api_on_the_real_meta_file(client: TestClient) -> None:
    response = client.get(URL)
    assert response.status_code == 200, response.text
    body = response.json()
    # ROC-AUC is only ever returned inside test_metrics, after precision/recall/F1.
    assert list(body["test_metrics"])[:4] == ["precision", "recall", "f1", "roc_auc"]
    assert "roc_auc" not in body
    assert set(body["confusion"]) == {"tn", "fp", "fn", "tp"}
    flagged = body["flagged_without_diabetes"]
    c = body["confusion"]
    assert (flagged["count"], flagged["denominator"]) == (c["fp"], c["fp"] + c["tp"])
    assert flagged["rate"] == pytest.approx(c["fp"] / (c["fp"] + c["tp"]))
    assert body["limitations"] == list(LIMITATIONS)
    assert body["tier_cutoffs"].keys() == {"moderate", "high"}
    assert body["top_features"][0]["feature"] == "BMI"
    assert len(body["recent_retrains"]) == 5
    assert body["recent_retrains"][0]["decision"] == "kept"
    assert FakeRuns.asked == [("retrain", 5)]


def test_malformed_meta_is_a_clean_500_with_a_logged_error(
    client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    model_paths(tmp_path)[1].write_text('{"version": "broken"}')
    main.app.dependency_overrides[audit.live_model_dir] = lambda: tmp_path
    with caplog.at_level(logging.ERROR, logger="app.api.audit"):
        response = client.get(URL)
    assert response.status_code == 500
    assert response.json() == {"detail": audit.MODEL_CARD_UNAVAILABLE}
    assert "invalid risk model metadata" in caplog.text


def test_missing_model_is_503(client: TestClient, tmp_path: Path) -> None:
    main.app.dependency_overrides[audit.live_model_dir] = lambda: tmp_path
    assert client.get(URL).status_code == 503


def test_model_card_is_practitioner_only(client: TestClient) -> None:
    main.app.dependency_overrides[current_auth_user] = lambda: PATIENT_LOGIN
    assert client.get(URL).status_code == 403


def test_copied_meta_file_still_validates(tmp_path: Path) -> None:
    # The retrain job backs up and restores this exact file format.
    shutil.copy2(model_paths(DEFAULT_MODEL_DIR)[1], model_paths(tmp_path)[1])
    assert load_metadata(tmp_path).version == load_metadata(DEFAULT_MODEL_DIR).version
