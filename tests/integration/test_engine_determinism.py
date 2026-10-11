"""Defect 52 (BUILD_STATUS §2d): the engine is not byte-reproducible across machines. The
strongest honest check a shared CI runner can give is *self*-consistency — two independent
trainings and recommendations on the same machine, same data, same configuration must be
byte-identical. A failure here would mean thread-order or other run-to-run nondeterminism
on this machine, which is a defect in its own right; passing says nothing about another
machine, and this test does not claim that it does.

With FPL_GOLDEN_ACTUAL set (CI), a fingerprint of this machine's numerics is written next to
the computed package so differences between runners can be correlated with their CPU and
SIMD dispatch (``docs/BUILD_STATUS.md`` defect 52).
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any

import numpy as np

from fpl_api.container import AppServices, build_recommendation
from fpl_api.settings import Settings
from fpl_domain.enums import Position
from fpl_domain.squad import SquadPick
from fpl_domain.state import ManagerState, initial_chips
from tests.fixtures_util import fixture_dataset

VOLATILE = {"timings", "generated_at"}


def _settings(tmp: Path) -> Settings:
    return Settings(
        artifact_dir=tmp / "artifacts",
        feature_store_dir=tmp / "features",
        n_sims=200,
        horizon_default=2,
        horizon_max=2,
        forecast_horizon=2,
        sync_horizon_limit=2,
    )


def _package(tmp: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """A fresh service (own caches: the forecast is trained again) and its package."""
    svc = AppServices.build(_settings(tmp), fixture_dataset("2024-25", "2025-26", "2026-27"))
    ctx = svc.context()
    rs = svc.data.ruleset(ctx.season)
    pool = svc.data.view(ctx.cutoff).player_pool(ctx.season, ctx.gameweek)
    picks: list[SquadPick] = []
    for pos, n in (("GK", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)):
        rows = pool[pool["position"] == pos].sort_values(["price", "player_code"]).head(n)
        picks += [
            SquadPick(
                player_code=int(r.player_code),
                position=Position(pos),
                team_code=int(r.team_code),
                purchase_price=int(r.price),
            )
            for r in rows.itertuples(index=False)
        ]
    state = ManagerState(
        season=ctx.season,
        gameweek=ctx.gameweek,
        squad=tuple(picks),
        bank=50,
        free_transfers=1,
        chips=initial_chips(rs),
        source="manual",
        provenance="determinism",
    )
    _, pkg = build_recommendation(
        svc, "det", state, None, profile="default", horizon=2, persist=False, run_stability=False
    )
    fc = svc.forecast_for(ctx, 2)
    fingerprint = {
        "prediction_run_id": fc.run_id,
        "xp_sum": float(fc.summary["mean"].sum()),
        "xp_sum_round6": round(float(fc.summary["mean"].sum()), 6),
        "n_rows": len(fc.summary),
    }
    canon = json.loads(
        json.dumps({k: v for k, v in pkg.model_dump(mode="json").items() if k not in VOLATILE})
    )
    return canon, fingerprint


def test_engine_reproduces_itself_on_this_machine(tmp_path: Path) -> None:
    first, fp1 = _package(tmp_path / "one")
    second, fp2 = _package(tmp_path / "two")
    differing = sorted(k for k in set(first) | set(second) if first.get(k) != second.get(k))
    assert differing == [], f"run-to-run nondeterminism on this machine in: {differing}"
    assert fp1 == fp2
    actual = os.environ.get("FPL_GOLDEN_ACTUAL")
    if actual:
        cpu: dict[str, Any] = {}
        try:
            from numpy._core._multiarray_umath import __cpu_features__

            cpu = {k: bool(v) for k, v in __cpu_features__.items() if v}
        except Exception:  # fingerprint detail only
            cpu = {}
        out = Path(actual).with_name("platform_fingerprint.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "platform": platform.platform(),
                    "machine": platform.machine(),
                    "processor": platform.processor(),
                    "numpy": np.__version__,
                    "cpu_features_enabled": sorted(cpu),
                    "forecast": fp1,
                    "decision": {
                        "action": first["decision"]["action"],
                        "transfers_in": first["decision"]["transfers_in"],
                        "chosen_objective": first["chosen"]["objective"],
                    },
                },
                indent=1,
            )
            + "\n"
        )
