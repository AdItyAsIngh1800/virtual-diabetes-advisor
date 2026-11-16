"""Integration tests for RLS and append-only rules against the real Supabase database.

Opt-in: run with ``RUN_DB_TESTS=1 uv run pytest tests/test_db_rls.py``. Every test
runs inside one transaction that is rolled back, so nothing is left behind.
Simulated callers use ``SET LOCAL ROLE`` + ``request.jwt.claims``, which is what
PostgREST does for a real Supabase JWT.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, LiteralString

import psycopg
import pytest
from psycopg import errors, sql

from app.core.config import load_settings
from app.db.session import DB_CONNECT_TIMEOUT_S
from app.ml.schema import age_to_bucket

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to hit Supabase"
)


@dataclass(frozen=True)
class World:
    """Ids of the fixture rows created for one test."""

    patient_a: uuid.UUID
    patient_b: uuid.UUID
    auth_a: uuid.UUID
    auth_b: uuid.UUID
    auth_prac: uuid.UUID
    auth_other_prac: uuid.UUID
    auth_admin: uuid.UUID
    prac_id: uuid.UUID
    reading_a: uuid.UUID
    threshold_a: uuid.UUID
    turn_a: uuid.UUID


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    dsn = load_settings().database_url.get_secret_value()
    with psycopg.connect(
        dsn, autocommit=False, prepare_threshold=None, connect_timeout=DB_CONNECT_TIMEOUT_S
    ) as connection:
        try:
            yield connection
        finally:
            connection.rollback()


def _auth_user(conn: psycopg.Connection[Any]) -> uuid.UUID:
    uid = uuid.uuid4()
    conn.execute(
        "insert into auth.users (id, aud, role, email) values (%s, 'authenticated', "
        "'authenticated', %s)",
        (uid, f"rls-test-{uid}@example.invalid"),
    )
    return uid


def _app_user(conn: psycopg.Connection[Any], auth_id: uuid.UUID | None, role: str) -> uuid.UUID:
    row = conn.execute(
        "insert into public.users (auth_user_id, role) values (%s, %s) returning id",
        (auth_id, role),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


@pytest.fixture
def world(conn: psycopg.Connection[Any]) -> World:
    auth_a, auth_b = _auth_user(conn), _auth_user(conn)
    auth_prac, auth_other, auth_admin = _auth_user(conn), _auth_user(conn), _auth_user(conn)
    patient_a = _app_user(conn, auth_a, "patient")
    patient_b = _app_user(conn, auth_b, "patient")
    prac_user = _app_user(conn, auth_prac, "practitioner")
    other_user = _app_user(conn, auth_other, "practitioner")
    _app_user(conn, auth_admin, "admin")

    for pid, phone in ((patient_a, "+919800000001"), (patient_b, "+919800000002")):
        conn.execute(
            "insert into public.profiles (user_id, phone_e164, age_years, diabetes_status) "
            "values (%s, %s, 52, 'diabetes')",
            (pid, phone),
        )
    prac_row = conn.execute(
        "insert into public.practitioners (user_id, full_name) values (%s, 'Dr A') returning id",
        (prac_user,),
    ).fetchone()
    conn.execute(
        "insert into public.practitioners (user_id, full_name) values (%s, 'Dr Other')",
        (other_user,),
    )
    assert prac_row is not None
    prac_id = uuid.UUID(str(prac_row[0]))
    conn.execute(
        "insert into public.patient_assignments (practitioner_id, patient_id) values (%s, %s)",
        (prac_id, patient_a),
    )

    readings: dict[uuid.UUID, uuid.UUID] = {}
    for pid in (patient_a, patient_b):
        row = conn.execute(
            "insert into public.readings (user_id, context, value_mg_dl, measured_at, source) "
            "values (%s, 'fasting', 126, now(), 'seed') returning id",
            (pid,),
        ).fetchone()
        assert row is not None
        readings[pid] = uuid.UUID(str(row[0]))

    th = conn.execute(
        "insert into public.thresholds_history (user_id, limit_kind, direction, diabetes_status,"
        " base_mg_dl, unclamped_mg_dl, hard_min_mg_dl, hard_max_mg_dl, threshold_mg_dl,"
        " model_version, reason, source_citation) values (%s, 'fasting_high', 'upper',"
        " 'diabetes', 130, 130, 100, 180, 130, 'test-v1', 'initial', 'ADA 2025 T6.3')"
        " returning id",
        (patient_a,),
    ).fetchone()
    assert th is not None
    turn = conn.execute(
        "insert into public.conversation_turns (user_id, channel, direction, raw_text)"
        " values (%s, 'web', 'inbound', 'hello') returning id",
        (patient_a,),
    ).fetchone()
    assert turn is not None
    conn.execute(
        "insert into public.kb_entries (entry_key, title, body, status) values"
        " ('hypo-basics', 'Draft', 'draft body', 'draft')"
    )
    return World(
        patient_a=patient_a,
        patient_b=patient_b,
        auth_a=auth_a,
        auth_b=auth_b,
        auth_prac=auth_prac,
        auth_other_prac=auth_other,
        auth_admin=auth_admin,
        prac_id=prac_id,
        reading_a=readings[patient_a],
        threshold_a=uuid.UUID(str(th[0])),
        turn_a=uuid.UUID(str(turn[0])),
    )


def _as(
    conn: psycopg.Connection[Any],
    role: LiteralString,
    sub: uuid.UUID | None,
    query: LiteralString,
    params: tuple[object, ...] = (),
) -> list[tuple[Any, ...]]:
    """Run ``query`` as a simulated Supabase caller inside a rolled-back savepoint.

    Rolling back to the savepoint also undoes ``SET LOCAL ROLE`` and the JWT claims,
    so the connection is back to the owner role afterwards, even if ``query`` failed.
    """
    conn.execute("savepoint as_caller")
    try:
        conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
        claims = {"sub": str(sub), "role": role} if sub else {"role": role}
        conn.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
        return conn.execute(query, params).fetchall()
    finally:
        conn.execute("rollback to savepoint as_caller")


def _patient_ids(conn: psycopg.Connection[Any], sub: uuid.UUID) -> set[uuid.UUID]:
    rows = _as(conn, "authenticated", sub, "select user_id from public.profiles")
    return {uuid.UUID(str(r[0])) for r in rows}


# --- Read isolation ---------------------------------------------------------------


def test_patient_sees_only_own_rows(conn: psycopg.Connection[Any], world: World) -> None:
    assert _patient_ids(conn, world.auth_a) == {world.patient_a}
    for table in ("readings", "thresholds_history", "conversation_turns"):
        rows = _as(conn, "authenticated", world.auth_b, f"select user_id from public.{table}")
        assert {uuid.UUID(str(r[0])) for r in rows} <= {world.patient_b}


def test_practitioner_sees_only_assigned_patients(
    conn: psycopg.Connection[Any], world: World
) -> None:
    assert _patient_ids(conn, world.auth_prac) == {world.patient_a}
    readings = _as(conn, "authenticated", world.auth_prac, "select user_id from public.readings")
    assert {uuid.UUID(str(r[0])) for r in readings} == {world.patient_a}


def test_unassigned_practitioner_sees_no_patients(
    conn: psycopg.Connection[Any], world: World
) -> None:
    assert _patient_ids(conn, world.auth_other_prac) == set()


def test_admin_sees_all_patients(conn: psycopg.Connection[Any], world: World) -> None:
    assert {world.patient_a, world.patient_b} <= _patient_ids(conn, world.auth_admin)


def test_current_thresholds_view_respects_rls(conn: psycopg.Connection[Any], world: World) -> None:
    own = _as(conn, "authenticated", world.auth_a, "select id from public.current_thresholds")
    other = _as(conn, "authenticated", world.auth_b, "select id from public.current_thresholds")
    assert [uuid.UUID(str(r[0])) for r in own] == [world.threshold_a]
    assert other == []


def test_drafts_hidden_from_patients_visible_to_staff(
    conn: psycopg.Connection[Any], world: World
) -> None:
    q: LiteralString = "select 1 from public.kb_entries where entry_key = 'hypo-basics'"
    assert _as(conn, "authenticated", world.auth_a, q) == []
    assert len(_as(conn, "authenticated", world.auth_prac, q)) == 1


def test_anon_has_no_access(conn: psycopg.Connection[Any], world: World) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        _as(conn, "anon", None, "select 1 from public.profiles")


def test_authenticated_cannot_write(conn: psycopg.Connection[Any], world: World) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        _as(
            conn,
            "authenticated",
            world.auth_a,
            "insert into public.readings (user_id, context, value_mg_dl, measured_at, source)"
            " values (%s, 'fasting', 100, now(), 'dashboard')",
            (world.patient_a,),
        )


# --- Append-only audit (R5) ---------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "update public.conversation_turns set raw_text = 'edited' where id = %s",
        "delete from public.conversation_turns where id = %s",
    ],
)
def test_service_role_cannot_mutate_turns(
    conn: psycopg.Connection[Any], world: World, query: LiteralString
) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        _as(conn, "service_role", None, query, (world.turn_a,))


def test_service_role_can_append_turns(conn: psycopg.Connection[Any], world: World) -> None:
    rows = _as(
        conn,
        "service_role",
        None,
        "insert into public.conversation_turns (user_id, channel, direction)"
        " values (%s, 'dashboard', 'outbound') returning id",
        (world.patient_a,),
    )
    assert len(rows) == 1


def test_trigger_blocks_even_db_owner(conn: psycopg.Connection[Any], world: World) -> None:
    with pytest.raises(errors.InsufficientPrivilege, match="append-only"), conn.transaction():
        conn.execute("delete from public.conversation_turns where id = %s", (world.turn_a,))
    with pytest.raises(errors.InsufficientPrivilege, match="append-only"), conn.transaction():
        conn.execute("truncate public.conversation_turns cascade")


# --- Integrity rules the services rely on -------------------------------------------


def test_advisory_is_idempotent_per_reading_and_limit(
    conn: psycopg.Connection[Any], world: World
) -> None:
    insert: LiteralString = (
        "insert into public.advisories (user_id, reading_id, threshold_id, limit_kind, state,"
        " reading_value_mg_dl, threshold_mg_dl, language, message_text) values"
        " (%s, %s, %s, 'fasting_high', 'pre_breach_high', 126, 130, 'en', 'msg')"
    )
    params = (world.patient_a, world.reading_a, world.threshold_a)
    conn.execute(insert, params)
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        conn.execute(insert, params)


def test_threshold_outside_clamp_is_rejected(conn: psycopg.Connection[Any], world: World) -> None:
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(
            "insert into public.thresholds_history (user_id, limit_kind, direction,"
            " diabetes_status, base_mg_dl, unclamped_mg_dl, hard_min_mg_dl, hard_max_mg_dl,"
            " threshold_mg_dl, model_version, reason, source_citation) values (%s,"
            " 'fasting_high', 'upper', 'diabetes', 130, 200, 100, 180, 200, 'v', 'batch', 'x')",
            (world.patient_a,),
        )


def test_approved_kb_entry_requires_practitioner(conn: psycopg.Connection[Any]) -> None:
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(
            "insert into public.kb_entries (entry_key, title, body, status, kb_version)"
            " values ('auto', 't', 'b', 'approved', 1)"
        )


def test_brfss_age_bucket_is_generated(conn: psycopg.Connection[Any], world: World) -> None:
    row = conn.execute(
        "select brfss_age_bucket from public.profiles where user_id = %s", (world.patient_a,)
    ).fetchone()
    assert row == (age_to_bucket(52),)  # 7 = BRFSS 50-54


# --- push_subscriptions (0006) ------------------------------------------------------


def _subscribe(conn: psycopg.Connection[Any], user_id: uuid.UUID) -> uuid.UUID:
    row = conn.execute(
        "insert into public.push_subscriptions (user_id, endpoint, p256dh, auth)"
        " values (%s, %s, 'p256dh-key', 'auth-secret') returning id",
        (user_id, f"https://push.example.invalid/{uuid.uuid4()}"),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def test_patient_sees_only_own_push_subscriptions(
    conn: psycopg.Connection[Any], world: World
) -> None:
    own = _subscribe(conn, world.patient_a)
    _subscribe(conn, world.patient_b)
    rows = _as(conn, "authenticated", world.auth_a, "select id from public.push_subscriptions")
    assert [uuid.UUID(str(r[0])) for r in rows] == [own]
    # Not even an assigned practitioner reads a patient's device keys.
    assert (
        _as(conn, "authenticated", world.auth_prac, "select id from public.push_subscriptions")
        == []
    )


@pytest.mark.parametrize(
    "query",
    [
        "insert into public.push_subscriptions (user_id, endpoint, p256dh, auth)"
        " values (%s, 'https://push.example.invalid/x', 'k', 'a')",
        "update public.push_subscriptions set revoked_at = now() where user_id = %s",
        "delete from public.push_subscriptions where user_id = %s",
    ],
)
def test_clients_cannot_write_push_subscriptions(
    conn: psycopg.Connection[Any], world: World, query: LiteralString
) -> None:
    _subscribe(conn, world.patient_a)
    with pytest.raises(errors.InsufficientPrivilege):
        _as(conn, "authenticated", world.auth_a, query, (world.patient_a,))


def test_anon_cannot_read_push_subscriptions(conn: psycopg.Connection[Any], world: World) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        _as(conn, "anon", None, "select 1 from public.push_subscriptions")


def test_push_endpoint_must_be_https(conn: psycopg.Connection[Any], world: World) -> None:
    with pytest.raises(errors.CheckViolation):
        conn.execute(
            "insert into public.push_subscriptions (user_id, endpoint, p256dh, auth)"
            " values (%s, 'http://push.example.invalid/x', 'k', 'a')",
            (world.patient_a,),
        )
