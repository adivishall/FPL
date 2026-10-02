"""Full-dataset scoring reproduction (slow; needs a locally exported canonical snapshot)."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from fpl_domain.rules import load_ruleset
from fpl_domain.scoring import (
    ScoringTables,
    defensive_actions,
    position_codes,
    score_components,
    total_points,
)
from fpl_storage.dataset import load_snapshot

pytestmark = pytest.mark.slow
SNAP_DIR = Path(os.environ.get("FPL_DATA_DIR", "data")) / "snapshots"


def _latest_snapshot() -> Path:
    snaps = sorted(SNAP_DIR.glob("snap_*/manifest.json"), key=lambda p: p.stat().st_mtime)
    if not snaps:
        pytest.skip(
            "no exported snapshot; run `fpl-ingest historical --all && fpl-ingest export-snapshot`"
        )
    return snaps[-1].parent


def test_every_real_row_reproduces_official_points() -> None:
    ds = load_snapshot(_latest_snapshot())
    pm = ds["player_match"].merge(
        ds["players"][["season", "player_code", "position"]], on=["season", "player_code"]
    )
    assert len(pm) > 100_000
    for season, g in pm.groupby("season"):
        t = ScoringTables.from_ruleset(load_ruleset(season))
        pos = position_codes(g["position"].to_numpy())

        def c(col: str, g=g) -> np.ndarray:
            return g[col].fillna(0).astype(int).to_numpy()

        tot = total_points(
            score_components(
                t,
                pos,
                c("minutes"),
                c("goals"),
                c("assists"),
                c("clean_sheets"),
                c("goals_conceded"),
                c("saves"),
                c("penalties_saved"),
                c("penalties_missed"),
                c("yellow_cards"),
                c("red_cards"),
                c("own_goals"),
                c("bonus"),
                defensive_actions(pos, c("cbi"), c("tackles"), c("recoveries"), t),
            )
        )
        assert (tot == g["points"].to_numpy()).all(), season
