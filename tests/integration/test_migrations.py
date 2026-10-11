"""Database migration validation (§78 'Database migration validation', §86.2)."""

from __future__ import annotations

from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from fpl_storage.models import Base
from tests.conftest import run_alembic

pytestmark = pytest.mark.integration

SPEC_TABLES = {  # §54 semantic model + §8 operational tables
    "seasons",
    "gameweeks",
    "teams",
    "players",
    "fixtures",
    "player_snapshots",
    "player_gw_stats",
    "team_gw_stats",
    "underlying_stats",
    "manager_state",
    "manager_squad",
    "manager_transfers",
    "manager_chips",
    "predictions",
    "optimization_runs",
    "recommendations",
    "recommendation_actions",
    "backtest_runs",
    "data_quality_events",
    "model_registry",
    "audit_log",
    "team_strength",
    "feature_snapshots",
    "transfer_candidates",
    "data_jobs",
}


def test_upgrade_creates_spec_tables(migrated_db: str) -> None:
    tables = set(inspect(create_engine(migrated_db)).get_table_names())
    missing = SPEC_TABLES - tables
    assert not missing, f"missing tables: {missing}"


def test_models_and_migrations_have_no_drift(migrated_db: str) -> None:
    engine = create_engine(migrated_db)
    with engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    assert diff == [], f"models differ from migrations: {diff}"


def test_downgrade_then_upgrade_is_reproducible(migrated_db: str) -> None:
    down = run_alembic(migrated_db, "downgrade", "base")
    assert down.returncode == 0, down.stderr
    assert set(inspect(create_engine(migrated_db)).get_table_names()) <= {"alembic_version"}
    up = run_alembic(migrated_db, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert set(inspect(create_engine(migrated_db)).get_table_names()) >= SPEC_TABLES


def _filler(col: Any) -> Any:
    """A value for a NOT NULL column without a default (shape only)."""
    from datetime import UTC, datetime

    t = str(col["type"]).upper()
    if "JSON" in t:
        return {}
    if "TIMESTAMP" in t or "DATE" in t:
        return datetime(2026, 9, 1, tzinfo=UTC)
    if "BOOL" in t:
        return False
    if "INT" in t:
        return 1
    if "FLOAT" in t or "DOUBLE" in t or "NUMERIC" in t or "REAL" in t:
        return 0.0
    return "x"


def _insert(conn: Any, table: str, values: dict[str, Any]) -> None:
    from sqlalchemy import MetaData, Table

    t = Table(table, MetaData(), autoload_with=conn)
    insp = inspect(conn)
    row = {
        c["name"]: _filler(c)
        for c in insp.get_columns(table)
        if not c["nullable"] and c.get("default") is None and not c.get("autoincrement")
    }
    row.update(values)
    conn.execute(t.insert().values(**row))


def test_0005_keeps_pre_journal_feedback_and_its_downgrade_restores_it(pg_url: str) -> None:
    """Pre-M1.4 feedback rows survive 0005: identity and decision-time identifiers come from
    their recommendation, the adherence vocabulary is mapped (ignored -> rejected, partial ->
    modified), only the newest row per manager and gameweek stays active, and — without
    decision-time evidence — their evaluation and verification are "unavailable" for good (so
    the hourly journal sweep never picks them up). Downgrading maps them back."""
    import uuid

    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    name = f"mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    try:
        assert run_alembic(url, "upgrade", "0004").returncode == 0
        eng = create_engine(url)
        with eng.begin() as conn:
            _insert(conn, "seasons", {"id": 7, "season_code": "2026-27"})
            _insert(
                conn,
                "recommendations",
                {
                    "id": "rec_legacy",
                    "season_id": 7,
                    "gw": 5,
                    "action_type": "TRANSFER",
                    "status": "active",
                    "data_snapshot_id": "snap_legacy",
                    "payload_json": {
                        "manager_key": "mk_legacy",
                        "season": "2026-27",
                        "decision": {"transfers_out": [11], "transfers_in": [22]},
                    },
                },
            )
            for followed in ("followed", "ignored", "partial"):
                conn.execute(
                    text(
                        "INSERT INTO decision_journal (recommendation_id, followed, review_json) "
                        "VALUES ('rec_legacy', :f, '{}'::jsonb)"
                    ),
                    {"f": followed},
                )
        res = run_alembic(url, "upgrade", "head")
        assert res.returncode == 0, res.stderr
        with eng.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT id, entry_id, manager_key, season, gameweek, followed, action, "
                        "sells, buys, state, evaluation_status, data_snapshot_id, "
                        "recommended_action, verification, verification_json "
                        "FROM decision_journal ORDER BY id"
                    )
                )
                .mappings()
                .all()
            )
        assert [r["followed"] for r in rows] == ["followed", "rejected", "modified"]
        assert [r["action"] for r in rows] == ["TRANSFER", "UNKNOWN", "UNKNOWN"]
        assert rows[0]["sells"] == [11] and rows[0]["buys"] == [22] and rows[1]["sells"] == []
        assert [r["state"] for r in rows] == ["superseded", "superseded", "active"]
        for r in rows:
            assert r["entry_id"] == f"jd_legacy_{r['id']}"
            assert (r["manager_key"], r["season"], r["gameweek"]) == ("mk_legacy", "2026-27", 5)
            assert r["evaluation_status"] == "unavailable"
            assert r["verification"] == "unavailable"
            assert r["verification_json"]["retryable"] is False
            assert (r["data_snapshot_id"], r["recommended_action"]) == ("snap_legacy", "TRANSFER")
        assert run_alembic(url, "downgrade", "0004").returncode == 0
        with eng.connect() as conn:
            back = conn.execute(text("SELECT followed FROM decision_journal ORDER BY id"))
            assert list(back.scalars()) == ["followed", "ignored", "partial"]
        assert run_alembic(url, "upgrade", "head").returncode == 0
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
