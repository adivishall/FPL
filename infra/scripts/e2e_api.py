"""Start a real API stack for end-to-end tests (Playwright webServer).

* canonical snapshot exported from the committed real-data excerpt (2024-25, 2025-26, 2026-27);
* PostgreSQL: ``$FPL_E2E_DATABASE_URL`` (CI service) or a throw-away local cluster;
* Alembic migrations, then uvicorn on :8000 (blocking).

Usage (repo root): uv run python infra/scripts/e2e_api.py
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FPL_CONFIG_DIR", str(ROOT / "config"))
E2E_KEY = "e2e-only-key"  # the web proxy's key: needs a signed-in user for manager data
E2E_OPS_KEY = "e2e-ops-key"  # operator key for test setup (invitations); throw-away stack only


def _stage_snapshot_b(ds_a: Any, root: Path) -> Path:
    """Snapshot A with the first owned-looking player's availability changed, exported (not
    served) so a test can promote it by copying it into the snapshots root."""
    from fpl_storage.dataset import CanonicalDataset, export_snapshot  # noqa: PLC0415

    ps = ds_a["player_snapshots"].copy()
    season = sorted(ps["season"].unique())[-1]
    code = int(ps.loc[ps["season"] == season, "player_code"].iloc[0])
    mask = (ps["player_code"] == code) & (ps["season"] == season)
    ps.loc[mask, "status"] = "i"
    ps.loc[mask, "chance_of_playing_next_round"] = 25.0
    ps.loc[mask, "news"] = "Knee injury - 25% chance of playing"
    time.sleep(1.1)  # manifests order by creation time: B must be newer than A
    return export_snapshot(CanonicalDataset({**ds_a.frames, "player_snapshots": ps}, {}), root)


def main() -> None:
    from fpl_api.security import hash_key  # noqa: PLC0415 (after sys.path setup)
    from fpl_api.services import mark_serving_ready  # noqa: PLC0415
    from fpl_api.settings import Settings  # noqa: PLC0415
    from fpl_storage.dataset import export_snapshot  # noqa: PLC0415
    from fpl_storage.testing import ephemeral_postgres  # noqa: PLC0415
    from tests.fixtures_util import fixture_dataset  # noqa: PLC0415

    work = Path(tempfile.mkdtemp(prefix="fpl-e2e-"))
    ds_a = fixture_dataset("2024-25", "2025-26", "2026-27")
    snap = export_snapshot(ds_a, work / "snapshots")
    # M1.3 lifecycle: the API serves from a snapshots directory with ready markers (production
    # behaviour), and a second snapshot — one player's availability changed — is staged next
    # to it so a browser test can promote it (copy + ready marker) and see what changed
    mark_serving_ready(Settings(artifact_dir=work / "artifacts"), snap.name, "fc_e2e_a")
    staged = _stage_snapshot_b(ds_a, work / "staged")
    (ROOT / "apps" / "web" / "e2e" / ".e2e-stack.json").write_text(
        json.dumps(
            {
                "snapshots_root": str(work / "snapshots"),
                "artifact_dir": str(work / "artifacts"),
                "served_snapshot": snap.name,
                "staged_snapshot": str(staged),
                "staged_snapshot_id": staged.name,
            }
        )
    )
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
                "FPL_SNAPSHOTS_ROOT": str(work / "snapshots"),
                "FPL_SERVE_READY_SNAPSHOTS_ONLY": "true",
                "FPL_SNAPSHOT_CHECK_SECONDS": "1",
                "FPL_ARTIFACT_DIR": str(work / "artifacts"),
                "FPL_N_SIMS": "200",
                "FPL_HORIZON_DEFAULT": "2",
                "FPL_HORIZON_MAX": "3",
                "FPL_FORECAST_HORIZON": "3",
                "FPL_SYNC_HORIZON_LIMIT": "2",
                "FPL_LIVE_SYNC_ENABLED": "false",  # no network: the UI must surface this
                "FPL_RATE_LIMIT_PER_MINUTE": "1000",
                "FPL_EXPENSIVE_RATE_LIMIT_PER_MINUTE": "1000",
                "FPL_CORS_ORIGINS": '["http://localhost:3000","http://127.0.0.1:3000"]',
                # production auth path: the UI's server-side proxy holds the key (playwright
                # config passes FPL_API_KEY=e2e-only-key to the Next server only)
                "FPL_REQUIRE_API_KEY": "true",
                "FPL_API_KEYS_SHA256": json.dumps([hash_key(E2E_OPS_KEY)]),
                "FPL_WEB_API_KEYS_SHA256": json.dumps([hash_key(E2E_KEY)]),
            }
        )
        from fpl_api.app import create_app  # noqa: PLC0415 (reads env set above)
        from fpl_api.settings import Settings  # noqa: PLC0415

        uvicorn.run(create_app(Settings()), host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
