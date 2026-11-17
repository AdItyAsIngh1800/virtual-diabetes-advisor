"""Tests for the manual retrain job (R5): the promotion guard, files, CLI and admin route.

``train_risk_model`` is monkeypatched to return fixed metrics, so nothing here trains a
real model. The ``models/`` folder is a temporary one. The ``batch_runs`` tests are
live (``RUN_DB_TESTS=1``) inside a rolled-back transaction.
"""

from __future__ import annotations

import os
import uuid
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from app import main
from app.api import admin
from app.core.config import Settings, get_settings, load_settings
from app.core.exceptions import BatchAlreadyRunningError, TrainingDataMissingError
from app.db.batch_repository import SqlBatchRepository
from app.db.models import BatchRun, ThresholdHistory
from app.db.session import make_engine
from app.jobs import retrain
from app.jobs.retrain import (
    RetrainReport,
    RetrainSummary,
    decide_promotion,
    retrain_candidate,
    run_retrain,
)
from app.ml.artifacts import DEFAULT_MODEL_DIR, ModelMetadata, load_metadata, model_paths
from app.ml.data import dataset_path

ADMIN_KEY = "admin-secret"
SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="srk",
    database_url="postgresql://u:p@localhost:5432/db",
    admin_api_key=ADMIN_KEY,
)
TEMPLATE = load_metadata(DEFAULT_MODEL_DIR)
CURRENT_RECALL = 0.88
CURRENT_PRECISION = 0.28

live = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)


def metadata(version: str, *, recall: float, precision: float) -> ModelMetadata:
    """Real metadata with the test recall and precision replaced."""
    test = TEMPLATE.test.model_copy(update={"recall": recall, "precision": precision})
    return TEMPLATE.model_copy(update={"version": version, "test": test})


def install(model_dir: Path, meta: ModelMetadata, payload: bytes) -> None:
    """Write a stand-in model: the job copies the estimator file but never loads it."""
    model_dir.mkdir(parents=True, exist_ok=True)
    estimator, meta_path = model_paths(model_dir)
    estimator.write_bytes(payload)
    meta_path.write_text(meta.model_dump_json())


class FakeTrainer:
    """Replaces ``train_risk_model``: writes a candidate with fixed test metrics."""

    def __init__(self, *, recall: float, precision: float, fail: bool = False) -> None:
        self.recall = recall
        self.precision = precision
        self.fail = fail
        self.calls: list[tuple[Path, Path, float | None]] = []

    def __call__(
        self, data_dir: Path, model_dir: Path, sample_frac: float | None = None
    ) -> ModelMetadata:
        self.calls.append((data_dir, model_dir, sample_frac))
        if self.fail:
            (model_dir / "half-written.joblib").write_bytes(b"x")
            raise RuntimeError("training blew up")
        meta = metadata("risk-candidate", recall=self.recall, precision=self.precision)
        install(model_dir, meta, b"candidate")
        return meta


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    """A ``models/`` with a current model, and a data folder with all three CSVs."""
    models = tmp_path / "models"
    install(
        models,
        metadata("risk-current", recall=CURRENT_RECALL, precision=CURRENT_PRECISION),
        b"current",
    )
    data = tmp_path / "raw"
    data.mkdir()
    for kind in ("binary", "binary_5050", "three_class"):
        dataset_path(kind, data).write_text("")
    return models, data


def use_trainer(monkeypatch: pytest.MonkeyPatch, trainer: FakeTrainer) -> FakeTrainer:
    monkeypatch.setattr(retrain, "train_risk_model", trainer)
    return trainer


def live_version(models: Path) -> str:
    return load_metadata(models).version


# --- the pure guard ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recall", "precision", "promote"),
    [
        (0.90, 0.30, True),  # better on both
        (CURRENT_RECALL - 0.01, CURRENT_PRECISION - 0.02, True),  # exactly at both limits
        (CURRENT_RECALL - 0.0101, CURRENT_PRECISION, False),  # recall just below its limit
        (CURRENT_RECALL, CURRENT_PRECISION - 0.0201, False),  # precision just below
        (0.99, 0.10, False),  # recall up, precision collapsed
    ],
)
def test_decide_promotion(recall: float, precision: float, promote: bool) -> None:
    current = metadata("c", recall=CURRENT_RECALL, precision=CURRENT_PRECISION).test
    candidate = metadata("n", recall=recall, precision=precision).test
    decision = decide_promotion(current, candidate)
    assert decision.promote is promote
    assert len(decision.reasons) == 2
    assert decision.reasons[0].startswith("test recall")


def test_no_current_model_promotes() -> None:
    decision = decide_promotion(None, TEMPLATE.test)
    assert decision.promote
    assert "no current model" in decision.reasons[0]


# --- files -------------------------------------------------------------------------------


def test_promotes_when_metrics_hold_and_keeps_a_backup(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]
) -> None:
    models, data = dirs
    trainer = use_trainer(monkeypatch, FakeTrainer(recall=0.875, precision=0.27))
    outcome = retrain_candidate(model_dir=models, data_dir=data, sample_frac=0.2)
    assert outcome.decision == "promoted"
    assert outcome.current_version == "risk-candidate" == live_version(models)
    assert model_paths(models)[0].read_bytes() == b"candidate"
    # Trained into a new folder, never straight over the live files.
    assert trainer.calls[0][1] != models
    assert trainer.calls[0][1].parent == models / retrain.CANDIDATES_DIR
    assert outcome.report.candidate_dir == "candidates/risk-candidate"
    # The previous model and metadata are kept so the promotion can be undone.
    backup = models / "backups" / "risk-current"
    assert outcome.report.backup_dir == "backups/risk-current"
    assert model_paths(backup)[0].read_bytes() == b"current"
    assert load_metadata(backup).version == "risk-current"
    assert outcome.report.old is not None and outcome.report.old.recall == CURRENT_RECALL
    assert outcome.report.new is not None and outcome.report.new.recall == 0.875
    # No temporary files left next to the live model.
    assert sorted(p.name for p in models.iterdir()) == [
        "backups",
        "candidates",
        "risk_model.joblib",
        "risk_model.meta.json",
    ]


def test_keeps_current_model_when_recall_drops(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]
) -> None:
    models, data = dirs
    use_trainer(monkeypatch, FakeTrainer(recall=0.80, precision=0.40))
    outcome = retrain_candidate(model_dir=models, data_dir=data)
    assert outcome.decision == "kept"
    assert live_version(models) == "risk-current" == outcome.current_version
    assert model_paths(models)[0].read_bytes() == b"current"
    assert not (models / "backups").exists()
    assert any("recall" in r and "too low" in r for r in outcome.report.reasons)
    # The candidate stays on disk for inspection.
    assert load_metadata(models / "candidates" / "risk-candidate").version == "risk-candidate"


def test_keeps_current_model_when_precision_drops(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]
) -> None:
    models, data = dirs
    use_trainer(monkeypatch, FakeTrainer(recall=0.95, precision=0.20))
    outcome = retrain_candidate(model_dir=models, data_dir=data)
    assert outcome.decision == "kept"
    assert live_version(models) == "risk-current"
    assert any("precision" in r and "too low" in r for r in outcome.report.reasons)


def test_dry_run_never_promotes(monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]) -> None:
    models, data = dirs
    use_trainer(monkeypatch, FakeTrainer(recall=0.99, precision=0.99))
    outcome = retrain_candidate(model_dir=models, data_dir=data, dry_run=True)
    assert outcome.decision == "dry_run"
    assert live_version(models) == "risk-current"
    assert model_paths(models)[0].read_bytes() == b"current"
    assert not (models / "backups").exists()
    assert outcome.report.dry_run
    assert outcome.report.reasons[-1] == "dry run: never promoted"


def test_failed_training_leaves_everything_in_place(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]
) -> None:
    models, data = dirs
    use_trainer(monkeypatch, FakeTrainer(recall=0.9, precision=0.3, fail=True))
    with pytest.raises(RuntimeError):
        retrain_candidate(model_dir=models, data_dir=data)
    assert live_version(models) == "risk-current"
    assert list((models / "candidates").iterdir()) == []  # partial folder removed


def test_first_model_is_promoted_without_a_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dirs: tuple[Path, Path]
) -> None:
    _, data = dirs
    empty = tmp_path / "fresh-models"
    use_trainer(monkeypatch, FakeTrainer(recall=0.5, precision=0.1))
    outcome = retrain_candidate(model_dir=empty, data_dir=data)
    assert outcome.decision == "promoted"
    assert outcome.report.backup_dir is None
    assert live_version(empty) == "risk-candidate"


def test_missing_csvs_fail_with_a_clear_error(tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir()
    dataset_path("binary", tmp_path / "raw").write_text("")
    with pytest.raises(TrainingDataMissingError) as exc:
        retrain.check_training_data(tmp_path / "raw")
    message = str(exc.value)
    assert "diabetes_012_health_indicators_BRFSS2015.csv" in message
    assert "diabetes_binary_5050split_health_indicators_BRFSS2015.csv" in message
    assert "diabetes_binary_health_indicators_BRFSS2015.csv," not in message


def test_run_refuses_missing_data_before_recording_a_run(tmp_path: Path) -> None:
    def no_db() -> Session:
        raise AssertionError("no batch_runs row for a run that cannot start")

    with pytest.raises(TrainingDataMissingError):
        run_retrain(no_db, model_dir=tmp_path, data_dir=tmp_path)


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
def test_sample_must_be_a_share(bad: float, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sample"):
        run_retrain(lambda: None, sample_frac=bad, model_dir=tmp_path, data_dir=tmp_path)  # type: ignore[arg-type, return-value]


def test_retrain_never_imports_the_threshold_engine() -> None:
    # The risk tier never moves a glucose limit; retraining must not even reach for it.
    source = Path(retrain.__file__).read_text(encoding="utf-8")
    for forbidden in ("app.services.thresholds", "threshold_repository", "ThresholdHistory"):
        assert forbidden not in source, forbidden


# --- CLI ---------------------------------------------------------------------------------


def _summary(**changes: object) -> RetrainSummary:
    data: dict[str, object] = {
        "run_id": uuid.uuid4(),
        "status": "succeeded",
        "triggered_by": "manual",
        "decision": "dry_run",
        "current_version": "risk-current",
        "report": RetrainReport(dry_run=True, sample_frac=0.2, old=None, new=None, reasons=[]),
        "error": None,
    }
    data.update(changes)
    return RetrainSummary.model_validate(data)


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(retrain, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr("app.db.session.get_session_factory", lambda: "factory")
    return calls


def test_cli_passes_dry_run_and_sample(
    monkeypatch: pytest.MonkeyPatch, cli: list[dict[str, object]]
) -> None:
    def fake(factory: object, **kwargs: object) -> RetrainSummary:
        cli.append({"factory": factory, **kwargs})
        return _summary()

    monkeypatch.setattr(retrain, "run_retrain", fake)
    assert retrain.main(["--dry-run", "--sample", "0.2"]) == 0
    assert cli == [
        {"factory": "factory", "triggered_by": "manual", "dry_run": True, "sample_frac": 0.2}
    ]


def test_cli_exit_codes(monkeypatch: pytest.MonkeyPatch, cli: list[dict[str, object]]) -> None:
    monkeypatch.setattr(retrain, "run_retrain", lambda *_a, **_k: _summary(status="failed"))
    assert retrain.main([]) == 1

    def missing(*_a: object, **_k: object) -> RetrainSummary:
        raise TrainingDataMissingError("BRFSS 2015 CSVs missing")

    monkeypatch.setattr(retrain, "run_retrain", missing)
    assert retrain.main([]) == 2


# --- admin route -------------------------------------------------------------------------


@pytest.fixture
def admin_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(admin, "get_session_factory", lambda: None)
    main.app.dependency_overrides[get_settings] = lambda: SETTINGS
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_admin_retrain_run_needs_the_admin_key(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def fake(_factory: object, **kwargs: object) -> RetrainSummary:
        calls.append(kwargs)
        return _summary(triggered_by="admin_api")

    monkeypatch.setattr(admin, "run_retrain", fake)
    assert admin_client.post("/admin/retrain-run").status_code == 401
    wrong = admin_client.post("/admin/retrain-run", headers={"X-Admin-Api-Key": "nope"})
    assert wrong.status_code == 401
    assert calls == []
    ok = admin_client.post(
        "/admin/retrain-run?dry_run=true&sample=0.2", headers={"X-Admin-Api-Key": ADMIN_KEY}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["decision"] == "dry_run"
    assert calls == [{"triggered_by": "admin_api", "dry_run": True, "sample_frac": 0.2}]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TrainingDataMissingError("BRFSS 2015 CSVs missing from data/raw"), 503),
        (BatchAlreadyRunningError("a retrain run is already in progress"), 409),
    ],
)
def test_admin_retrain_run_errors(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch, error: Exception, code: int
) -> None:
    def fail(*_a: object, **_k: object) -> RetrainSummary:
        raise error

    monkeypatch.setattr(admin, "run_retrain", fail)
    response = admin_client.post("/admin/retrain-run", headers={"X-Admin-Api-Key": ADMIN_KEY})
    assert response.status_code == code
    assert response.json()["detail"] == str(error)


def test_admin_retrain_run_rejects_a_bad_sample(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/admin/retrain-run?sample=1.5", headers={"X-Admin-Api-Key": ADMIN_KEY}
    )
    assert response.status_code == 422


def test_retraining_is_not_scheduled() -> None:
    jobs = main.build_scheduler(SETTINGS.model_copy(update={"kb_upgrade_enabled": True})).get_jobs()
    assert all("retrain" not in job.id for job in jobs)


# --- batch_runs (live) -------------------------------------------------------------------


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


def _thresholds(factory: Callable[[], Session]) -> int:
    with factory() as session:
        return int(session.scalar(select(func.count()).select_from(ThresholdHistory)) or 0)


@live
def test_run_is_recorded_in_batch_runs_and_writes_no_threshold(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path], factory: Callable[[], Session]
) -> None:
    models, data = dirs
    use_trainer(monkeypatch, FakeTrainer(recall=0.875, precision=0.27))
    before = _thresholds(factory)
    summary = run_retrain(factory, model_dir=models, data_dir=data, sample_frac=0.2)
    assert (summary.status, summary.decision, summary.error) == ("succeeded", "promoted", None)
    assert _thresholds(factory) == before  # never touches thresholds_history
    with factory() as session:
        row = session.get(BatchRun, summary.run_id)
        assert row is not None
        assert (row.job, row.status, row.triggered_by) == ("retrain", "succeeded", "manual")
        assert row.retrain_decision == "promoted"
        assert row.model_version == "risk-current"  # the model live when it started
        assert row.thresholds_written == 0
        assert row.finished_at is not None
        report = RetrainReport.model_validate(row.retrain_report)
        assert report.old is not None and report.old.version == "risk-current"
        assert report.new is not None and report.new.version == "risk-candidate"
        assert report.old.recall == CURRENT_RECALL and report.new.precision == 0.27
        assert report.backup_dir == "backups/risk-current"


@live
def test_failed_training_is_recorded_and_keeps_the_model(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path], factory: Callable[[], Session]
) -> None:
    models, data = dirs
    use_trainer(monkeypatch, FakeTrainer(recall=0.9, precision=0.3, fail=True))
    summary = run_retrain(factory, triggered_by="admin_api", model_dir=models, data_dir=data)
    assert (summary.status, summary.decision, summary.error) == ("failed", "kept", "RuntimeError")
    assert live_version(models) == "risk-current"
    with factory() as session:
        row = session.get(BatchRun, summary.run_id)
        assert row is not None
        assert (row.status, row.retrain_decision, row.error) == ("failed", "kept", "RuntimeError")
        report = RetrainReport.model_validate(row.retrain_report)
        assert report.new is None and "training failed" in report.reasons[0]


@live
def test_dry_run_is_recorded_without_promoting(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path], factory: Callable[[], Session]
) -> None:
    models, data = dirs
    use_trainer(monkeypatch, FakeTrainer(recall=0.99, precision=0.99))
    summary = run_retrain(factory, dry_run=True, model_dir=models, data_dir=data)
    assert summary.decision == "dry_run"
    assert live_version(models) == "risk-current"
    with factory() as session:
        runs = SqlBatchRepository(session).recent_runs("retrain", 5)
        assert runs[0].id == summary.run_id
        view = retrain.run_view(runs[0])
        assert view.decision == "dry_run" and view.report is not None and view.report.dry_run


@live
def test_retrain_columns_are_refused_on_other_jobs(factory: Callable[[], Session]) -> None:
    with factory() as session, pytest.raises(IntegrityError):
        session.add(
            BatchRun(
                id=uuid.uuid4(),
                job="batch_update",
                status="succeeded",
                finished_at=func.now(),
                retrain_decision="kept",
            )
        )
        session.flush()
