"""The evaluation snapshot pinned in config/backtest/default.yaml (shared by the experiments).

Experiments never pick "whatever snapshot is newest on disk": live captures produce newer
snapshots, and historical evaluation must not silently mix them in.
"""

from __future__ import annotations

from pathlib import Path

from fpl_domain.config import load_versioned_config

ROOT = Path(__file__).resolve().parents[2]


def pinned_snapshot() -> Path:
    snap_id = str(load_versioned_config("backtest", "default").data["snapshot_id"])
    path = ROOT / "data" / "snapshots" / snap_id
    if not path.exists():
        raise SystemExit(
            f"pinned snapshot {snap_id} not found: run `fpl-ingest historical --all` and "
            "`fpl-ingest export-snapshot` first"
        )
    return path
