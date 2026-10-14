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


def test_0006_refuses_rows_it_cannot_place_and_its_downgrade_removes_the_record(
    pg_url: str,
) -> None:
    """0006 keys ``predictions`` by checkpoint. Nothing wrote the table before M1.5, so the
    upgrade adds the non-null column to an empty table — and refuses to guess a checkpoint for
    rows that should not be there. A checkpoint's rows cannot be deleted from under it (RESTRICT);
    downgrading removes the forecast record (0005 cannot hold rows without a run)."""
    import uuid

    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import IntegrityError

    name = f"mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    try:
        assert run_alembic(url, "upgrade", "0005").returncode == 0
        eng = create_engine(url)
        with eng.begin() as conn:
            conn.execute(text("SET LOCAL session_replication_role = replica"))  # no FK checks
            _insert(conn, "predictions", {"run_id": "pred_orphan", "components_json": {}})
        r = run_alembic(url, "upgrade", "0006")
        assert r.returncode != 0 and "belong to no checkpoint" in r.stderr
        with eng.begin() as conn:
            conn.execute(text("DELETE FROM predictions"))
        assert run_alembic(url, "upgrade", "0006").returncode == 0
        with eng.begin() as conn:
            _insert(
                conn,
                "forecast_checkpoints",
                {"id": "ckp_x", "season": "2026-27", "kind": "live", "state": "prospective"},
            )
            _insert(
                conn,
                "predictions",
                {"checkpoint_id": "ckp_x", "run_id": None, "components_json": {}},
            )
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(text("DELETE FROM forecast_checkpoints"))
        assert run_alembic(url, "downgrade", "0005").returncode == 0
        insp = inspect(eng)
        assert "forecast_checkpoints" not in insp.get_table_names()
        with eng.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM predictions")).scalar() == 0
        assert run_alembic(url, "upgrade", "head").returncode == 0
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def test_0008_keeps_the_record_and_holds_fpl_server_time_to_its_promotion(pg_url: str) -> None:
    """0008 adds FPL server time (``forecast_attestations``) and the ``prospective_unverified``
    state without touching existing promotions or checkpoints. An attestation belongs to a
    promotion (which then cannot be deleted from under it) and its bounds are ordered.
    Downgrading records an unverified checkpoint as ``live_unverified`` — a check did not hold —
    the only state 0007 has for it."""
    import uuid

    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import IntegrityError

    name = f"mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    fpl = "https://fantasy.premierleague.com/api/event-status/"
    try:
        assert run_alembic(url, "upgrade", "0007").returncode == 0
        eng = create_engine(url)
        with eng.begin() as conn:
            _insert(conn, "forecast_promotions", {"snapshot_id": "snap_a", "forecast_key": "fc_a"})
            _insert(
                conn,
                "forecast_checkpoints",
                {"id": "ckp_a", "season": "2026-27", "kind": "live", "state": "prospective"},
            )
        assert run_alembic(url, "upgrade", "0008").returncode == 0
        with eng.begin() as conn:
            assert conn.execute(
                text("SELECT state, attested_at FROM forecast_checkpoints")
            ).one() == (
                "prospective",
                None,
            )
            _insert(
                conn,
                "forecast_checkpoints",
                {
                    "id": "ckp_b",
                    "season": "2026-27",
                    "gameweek": 8,
                    "kind": "live",
                    "state": "prospective_unverified",
                },
            )
            _insert(
                conn,
                "forecast_attestations",
                {
                    "snapshot_id": "snap_a",
                    "forecast_key": "fc_a",
                    "kind": "generation",
                    "not_after": "2026-10-14T07:00:01Z",
                    "not_before": "2026-10-14T06:58:00Z",
                    "source_url": fpl,
                    "evidence": {},
                },
            )
        bad = (
            {"snapshot_id": "snap_none", "forecast_key": "fc_a", "kind": "existence"},
            {"snapshot_id": "snap_a", "forecast_key": "fc_a", "kind": "guess"},
            {
                "snapshot_id": "snap_a",
                "forecast_key": "fc_a",
                "kind": "generation",
                "not_before": "2026-10-14T08:00:00Z",
            },
        )
        for values in bad:
            row = {"not_after": "2026-10-14T07:00:01Z", "source_url": fpl, "evidence": {}, **values}
            with pytest.raises(IntegrityError), eng.begin() as conn:
                _insert(conn, "forecast_attestations", row)
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(text("DELETE FROM forecast_promotions"))
        assert run_alembic(url, "downgrade", "0007").returncode == 0
        insp = inspect(eng)
        assert "forecast_attestations" not in insp.get_table_names()
        with eng.connect() as conn:
            states = dict(conn.execute(text("SELECT id, state FROM forecast_checkpoints")).all())
            assert states == {"ckp_a": "prospective", "ckp_b": "live_unverified"}
        assert run_alembic(url, "upgrade", "head").returncode == 0
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
