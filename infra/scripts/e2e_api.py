"""Start a real API stack for end-to-end tests (Playwright webServer).

* canonical snapshot exported from the committed real-data excerpt (2024-25, 2025-26, 2026-27);
* PostgreSQL: ``$FPL_E2E_DATABASE_URL`` (CI service) or a throw-away local cluster;
* Alembic migrations, then uvicorn on :8000 (blocking).

Usage (repo root): uv run python infra/scripts/e2e_api.py
Test helper: uv run python infra/scripts/e2e_api.py seed-legacy-decision KEY REC_ID LATER_ID
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


def _stage_results(snapshot_b: Path, root: Path) -> Path:
    """Snapshot B plus synthetic, final GW2 results (M1.4): promoting it lets a browser test
    evaluate a journal decision on finished results."""
    from fpl_storage.dataset import export_snapshot, load_snapshot  # noqa: PLC0415
    from tests.fixtures_util import with_gameweek_results  # noqa: PLC0415

    ds_b = load_snapshot(snapshot_b)
    season = sorted(ds_b["gameweeks"]["season"].unique())[-1]
    time.sleep(1.1)  # newer than B
    return export_snapshot(with_gameweek_results(ds_b, season, 2, lambda c: c % 7 + 1), root)


STACK_INFO = ROOT / "apps" / "web" / "e2e" / ".e2e-stack.json"


def seed_legacy_decision(manager_key: str, recommendation_id: str, later_id: str) -> str:
    """A decision as migration 0005 leaves pre-M1.4 feedback — no decision-time evidence,
    evaluation and verification unavailable — replaced by ``later_id``, so a browser test can
    open it (M1.4 review: such an entry crashed the entry page). Throw-away stack only."""
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    url = json.loads(STACK_INFO.read_text())["database_url"]
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                """
                INSERT INTO decision_journal (entry_id, manager_key, recommendation_id, season,
                  gameweek, state_id, data_snapshot_id, recommended_action, action, followed,
                  state, superseded_by, evaluation_status, verification, verification_json,
                  review_json)
                SELECT 'jd_legacy_e2e', :k, r.id, r.payload_json->>'season', r.gw,
                  r.manager_state_id, r.data_snapshot_id, r.action_type, 'UNKNOWN', 'rejected',
                  'superseded', :later, 'unavailable', 'unavailable',
                  jsonb_build_object('reason',
                    'recorded before the journal kept decision-time evidence',
                    'retryable', false),
                  '{}'::jsonb
                FROM recommendations r WHERE r.id = :rid
                ON CONFLICT (entry_id) DO NOTHING
                """
            ),
            {"k": manager_key, "rid": recommendation_id, "later": later_id},
        )
    engine.dispose()
    return "jd_legacy_e2e"


def main() -> None:
    if sys.argv[1:2] == ["seed-legacy-decision"]:
        print(seed_legacy_decision(*sys.argv[2:5]))  # the entry id, read by the browser test
        return
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
    results = _stage_results(staged, work / "staged")
    with contextlib.ExitStack() as stack:
        url = os.environ.get("FPL_E2E_DATABASE_URL") or stack.enter_context(ephemeral_postgres())
        STACK_INFO.write_text(
            json.dumps(
                {
                    "snapshots_root": str(work / "snapshots"),
                    "artifact_dir": str(work / "artifacts"),
                    "served_snapshot": snap.name,
                    "staged_snapshot": str(staged),
                    "staged_snapshot_id": staged.name,
                    "staged_results_snapshot": str(results),
                    "staged_results_snapshot_id": results.name,
                    "database_url": url,  # the throw-away test database (seed helpers)
                }
            )
        )
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
