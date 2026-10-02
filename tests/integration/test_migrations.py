"""Database migration validation (§78 'Database migration validation', §86.2)."""

from __future__ import annotations

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
