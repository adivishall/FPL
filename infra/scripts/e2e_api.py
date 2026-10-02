"""Start a real API stack for end-to-end tests (Playwright webServer).

* canonical snapshot exported from the committed real-data excerpt (2024-25, 2025-26, 2026-27);
* PostgreSQL: ``$FPL_E2E_DATABASE_URL`` (CI service) or a throw-away local cluster;
* Alembic migrations, then uvicorn on :8000 (blocking).

Usage (repo root): uv run python infra/scripts/e2e_api.py
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FPL_CONFIG_DIR", str(ROOT / "config"))


def main() -> None:
    from fpl_storage.dataset import export_snapshot  # noqa: PLC0415 (after sys.path setup)
    from fpl_storage.testing import ephemeral_postgres  # noqa: PLC0415
    from tests.fixtures_util import fixture_dataset  # noqa: PLC0415

    work = Path(tempfile.mkdtemp(prefix="fpl-e2e-"))
    snap = export_snapshot(fixture_dataset("2024-25", "2025-26", "2026-27"), work / "snapshots")
    with contextlib.ExitStack() as stack:
        url = os.environ.get("FPL_E2E_DATABASE_URL") or stack.enter_context(ephemeral_postgres())
        env = dict(os.environ, FPL_DATABASE_URL=url)
        subprocess.run(  # noqa: S603 - fixed, trusted command
            ["alembic", "-c", str(ROOT / "alembic.ini"), "upgrade", "head"],  # noqa: S607
            env=env,
            cwd=ROOT,
            check=True,
        )
        os.environ.update(
            {
                "FPL_DATABASE_URL": url,
                "FPL_SNAPSHOT_DIR": str(snap),
                "FPL_ARTIFACT_DIR": str(work / "artifacts"),
                "FPL_N_SIMS": "200",
                "FPL_HORIZON_DEFAULT": "2",
                "FPL_HORIZON_MAX": "3",
                "FPL_FORECAST_HORIZON": "3",
                "FPL_SYNC_HORIZON_LIMIT": "2",
                "FPL_RATE_LIMIT_PER_MINUTE": "1000",
                "FPL_EXPENSIVE_RATE_LIMIT_PER_MINUTE": "1000",
                "FPL_CORS_ORIGINS": '["http://localhost:3000","http://127.0.0.1:3000"]',
            }
        )
        from fpl_api.app import create_app  # noqa: PLC0415 (reads env set above)
        from fpl_api.settings import Settings  # noqa: PLC0415

        uvicorn.run(create_app(Settings()), host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
