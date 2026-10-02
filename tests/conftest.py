"""Shared pytest fixtures.

Integration fixtures provision real infrastructure: PostgreSQL from ``$FPL_TEST_DATABASE_URL``
(CI service container) or a throw-away local cluster; otherwise those tests are skipped with an
explicit reason (never silently passed).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("FPL_CONFIG_DIR", str(REPO_ROOT / "config"))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """A PostgreSQL URL for an empty database (session-scoped)."""
    url = os.environ.get("FPL_TEST_DATABASE_URL")
    if url:
        yield url
        return
    from fpl_storage.testing import ephemeral_postgres, postgres_binaries_available

    if not postgres_binaries_available():
        pytest.skip("no FPL_TEST_DATABASE_URL and no local PostgreSQL binaries")
    with ephemeral_postgres() as url:
        yield url


def run_alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, FPL_DATABASE_URL=url)
    return subprocess.run(
        ["alembic", "-c", str(REPO_ROOT / "alembic.ini"), *args],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def fresh_db(pg_url: str) -> Iterator[str]:
    """A brand-new, fully migrated database in the test cluster (dropped afterwards)."""
    import uuid

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = f"t_{uuid.uuid4().hex[:12]}"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    res = run_alembic(url, "upgrade", "head")
    if res.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed:\n{res.stderr}")
    yield url
    from fpl_storage.db import _cached_engine

    _cached_engine.cache_clear()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(scope="session")
def migrated_db(pg_url: str) -> str:
    """Database URL with all migrations applied."""
    res = run_alembic(pg_url, "upgrade", "head")
    if res.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed:\n{res.stdout}\n{res.stderr}")
    return pg_url
