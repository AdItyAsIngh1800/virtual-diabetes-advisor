"""Unit tests for app/db/models.py: no database needed.

The SQL migrations are the source of truth, so these tests parse every migration
(CREATE TABLE blocks, then each later ALTER TABLE replayed in order) and fail if the
ORM models drift from the result.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
from sqlalchemy import Enum, event
from sqlalchemy.dialects import postgresql

from app.core.exceptions import AuditLogImmutableError
from app.db import models
from app.ml.schema import age_to_bucket

MIGRATIONS = Path(__file__).resolve().parents[1] / "app" / "db" / "migrations"
CORE_SQL = (MIGRATIONS / "0001_core_schema.sql").read_text()
# Later migrations may ALTER TABLE ... ADD COLUMN; the ORM must mirror those too.
ALL_SQL = "\n".join(p.read_text() for p in sorted(MIGRATIONS.glob("*.sql")))

# Lines inside a CREATE TABLE block that are table constraints, not columns.
_CONSTRAINT_PREFIXES = ("check", "constraint", "primary key", "unique", "foreign key", "--")


def _sql_tables() -> dict[str, str]:
    """Map table name -> body of its CREATE TABLE block."""
    pattern = re.compile(r"create table public\.(\w+) \((.*?)\n\);", re.DOTALL)
    return {name: body for name, body in pattern.findall(ALL_SQL)}


# One ALTER TABLE action per statement (0006 is written that way on purpose).
_ALTER = re.compile(
    r"alter table public\.(?P<table>\w+)\s+(?:"
    r"rename column (?P<old>\w+) to (?P<new>\w+)"
    r"|drop column (?P<dropped>\w+)"
    r"|add column (?P<added>\w+)(?P<add_def>[^;]*)"
    r"|add constraint \w+\s+check \((?P<checked>\w+) in\s*\((?P<values>[^)]*)\)\)"
    r")"
)


def _replayed_schema(table: str) -> tuple[set[str], dict[str, set[str]]]:
    """Columns and CHECK choice lists of ``table`` after every migration.

    Starts from the CREATE TABLE block, then applies each ALTER TABLE in file order:
    RENAME COLUMN, DROP COLUMN, ADD COLUMN (with an inline CHECK list) and ADD
    CONSTRAINT ... CHECK (col in (...)), which replaces a dropped CHECK list.
    """
    body = _sql_tables()[table]
    columns = _sql_columns(table, body)
    choices = _sql_choice_lists(body)
    for m in _ALTER.finditer(ALL_SQL):
        if m["table"] != table:
            continue
        if m["old"]:
            columns = (columns - {m["old"]}) | {m["new"]}
            if m["old"] in choices:
                choices[m["new"]] = choices.pop(m["old"])
        elif m["dropped"]:
            columns.discard(m["dropped"])
            choices.pop(m["dropped"], None)
        elif m["added"]:
            columns.add(m["added"])
            choices.update(_sql_choice_lists(m["add_def"]))
        elif m["checked"]:
            choices[m["checked"]] = set(re.findall(r"'([^']*)'", m["values"]))
    return columns, choices


def _sql_columns(table: str, body: str) -> set[str]:
    """Column names declared in ``table``'s CREATE TABLE block."""
    columns: set[str] = set()
    for raw in body.splitlines():
        line = raw.strip()
        if (
            not line
            or line.startswith(_CONSTRAINT_PREFIXES)
            or not re.match(r"^[a-z_][a-z0-9_]* ", line)
        ):
            continue
        name = line.split()[0]
        # Continuation lines of a multi-line column ("check (...)", "case", ...) start
        # with an SQL keyword, never with a column name followed by a type.
        if name in {"check", "case", "when", "else", "end", "references"}:
            continue
        columns.add(name)
    return columns


def _sql_choice_lists(body: str) -> dict[str, set[str]]:
    """Map column -> allowed string values from ``check (col in ('a', 'b'))``."""
    out: dict[str, set[str]] = {}
    for column, values in re.findall(r"check \((\w+) in\s*\(([^)]*)\)", body):
        quoted = re.findall(r"'([^']*)'", values)
        if quoted:  # skip numeric lists such as sex in (0, 1)
            out[column] = set(quoted)
    return out


def test_every_sql_table_has_a_model_and_vice_versa() -> None:
    assert set(models.Base.metadata.tables) == set(_sql_tables())


@pytest.mark.parametrize("table", sorted(_sql_tables()))
def test_model_columns_match_sql(table: str) -> None:
    model_columns = set(models.Base.metadata.tables[table].columns.keys())
    assert model_columns == _replayed_schema(table)[0]


@pytest.mark.parametrize("table", sorted(_sql_tables()))
def test_choice_columns_match_sql_check_lists(table: str) -> None:
    sql_lists = _replayed_schema(table)[1]
    model_lists = {
        col.name: set(col.type.enums)
        for col in models.Base.metadata.tables[table].columns
        if isinstance(col.type, Enum)
    }
    assert model_lists == sql_lists


def test_choice_type_rejects_unknown_value() -> None:
    role_type = models.User.__table__.c.role.type
    process = role_type.bind_processor(postgresql.dialect())  # type: ignore[no-untyped-call]
    assert process is not None
    assert process("patient") == "patient"
    with pytest.raises(LookupError):
        process("superuser")


@pytest.mark.parametrize("model", models.APPEND_ONLY_MODELS)
@pytest.mark.parametrize("hook", ["before_update", "before_delete"])
def test_append_only_models_have_orm_guard(model: type[models.Base], hook: str) -> None:
    assert event.contains(model, hook, models._reject_mutation)


def test_orm_guard_raises() -> None:
    turn = models.ConversationTurn(channel="web", direction="inbound")
    with pytest.raises(AuditLogImmutableError, match="conversation_turns is append-only"):
        models._reject_mutation(None, None, turn)  # type: ignore[arg-type]


def test_replay_sees_0006_changes() -> None:
    advisories, advisory_choices = _replayed_schema("advisories")
    assert {"message_id", "audio_message_id", "channel", "opened_at"} <= advisories
    assert not {"wamid", "audio_wamid", "delivery_channel", "delivered_at", "read_at"} & advisories
    assert {"queued", "sent", "opened", "failed", "expired"} <= advisory_choices["delivery_status"]
    assert advisory_choices["channel"] == {"webpush"}
    assert "last_inbound_at" not in _replayed_schema("profiles")[0]
    turns, turn_choices = _replayed_schema("conversation_turns")
    assert "message_id" in turns
    assert "web" in turn_choices["channel"]
    assert models.Profile.__table__.c.phone_e164.nullable is True


def test_replay_sees_0007_changes() -> None:
    advisories, choices = _replayed_schema("advisories")
    assert {"audio_path", "dispatch_claimed_at"} <= advisories
    assert choices["delivery_status"] == {
        "queued",
        "sent",
        "opened",
        "failed",
        "expired",
        "suppressed",
    }


def test_brfss_age_bucket_expression_matches_python() -> None:
    """The generated-column CASE must agree with app.ml.schema.age_to_bucket."""

    def sql_bucket(age: int) -> int:
        # Same arithmetic as the Computed() expression (integer division).
        if age < 25:
            return 1
        if age >= 80:
            return 13
        return (age - 25) // 5 + 2

    for age in range(18, 121):
        assert sql_bucket(age) == age_to_bucket(age), age
    assert "(age_years - 25) / 5 + 2" in CORE_SQL


def test_replay_sees_0008_changes() -> None:
    turns, _ = _replayed_schema("conversation_turns")
    assert "author_practitioner_id" in turns
    assert models.ConversationTurn.__table__.c.author_practitioner_id.nullable is True


def test_replay_sees_0009_changes() -> None:
    kb, _ = _replayed_schema("kb_entries")
    assert {"review_hint", "priority"} <= kb
    runs, _ = _replayed_schema("batch_runs")
    assert {"kb_candidates", "kb_drafts_proposed"} <= runs


def test_replay_sees_0010_changes() -> None:
    runs, choices = _replayed_schema("batch_runs")
    assert {"retrain_decision", "retrain_report"} <= runs
    assert choices["retrain_decision"] == set(get_args(models.RetrainDecision))
