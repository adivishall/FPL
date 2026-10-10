"""Golden parity: the recommendation engine's output for a fixed squad on the fixture snapshot is
byte-identical to the stored golden package (BUILD_STATUS M1.1a §5). The product layers added
around the engine (accounts, onboarding, the Copilot Home read model, analytics) must not change
a forecast, an optimiser constraint, a recommendation or a simulation. Regenerate deliberately,
and only after an independent validation of the change, with FPL_UPDATE_GOLDEN=1.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from fpl_api.container import AppServices, build_recommendation
from fpl_api.settings import Settings
from fpl_domain.enums import Position
from fpl_domain.squad import SquadPick
from fpl_domain.state import ManagerState, initial_chips
from tests.fixtures_util import fixture_dataset

GOLDEN = Path(__file__).parent / "golden" / "recommendation_package.json"
VOLATILE = {"timings", "generated_at"}  # wall-clock fields, never part of the contract


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


def _canonical(pkg: dict[str, Any]) -> dict[str, Any]:
    return json.loads(
        json.dumps({k: v for k, v in pkg.items() if k not in VOLATILE}, sort_keys=True)
    )


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    svc = AppServices.build(
        _settings(tmp_path_factory.mktemp("golden")),
        fixture_dataset("2024-25", "2025-26", "2026-27"),
    )
    ctx = svc.context()
    rs = svc.data.ruleset(ctx.season)
    pool = svc.data.view(ctx.cutoff).player_pool(ctx.season, ctx.gameweek)
    # a deterministic legal squad: the cheapest 2/5/5/3 by player code (no optimiser involved)
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
        provenance="golden",
    )
    _, pkg = build_recommendation(
        svc, "golden", state, None, profile="default", horizon=2, persist=False, run_stability=False
    )
    return _canonical(pkg.model_dump(mode="json"))


def test_recommendation_package_matches_the_golden(package: dict[str, Any]) -> None:
    if os.environ.get("FPL_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(package, indent=1, sort_keys=True) + "\n")
        pytest.skip("golden package regenerated")
    assert GOLDEN.exists(), "no golden package: run with FPL_UPDATE_GOLDEN=1 once, review, commit"
    golden = json.loads(GOLDEN.read_text())
    differing = sorted(k for k in set(golden) | set(package) if golden.get(k) != package.get(k))
    assert differing == [], f"engine output changed in: {differing}"
    assert package == golden
