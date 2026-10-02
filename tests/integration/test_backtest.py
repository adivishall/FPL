"""Walk-forward backtest on the real excerpt: legality, scoring, no-lookahead (§27, §70)."""

from __future__ import annotations

import pandas as pd
import pytest

from fpl_backtest.runner import EngineStrategy, HoldStrategy, OptimizerStrategy, run_season
from fpl_domain.enums import Position
from fpl_domain.rules import load_ruleset
from fpl_domain.squad import Lineup, gameweek_points
from fpl_forecasting.walkforward import cutoffs
from fpl_optimizer.problem import load_optimizer_config
from fpl_simulation.engine import SimulationConfig
from tests.fixtures_util import fixture_dataset
from tests.unit.storage.test_raw_store_and_pit import _perturb_future

DS = fixture_dataset("2024-25", "2025-26")
CFG = load_optimizer_config("default")
SIM = SimulationConfig(n_sims=200)


def _strategies():  # type: ignore[no-untyped-def]
    return [
        EngineStrategy("engine", CFG, use_chips=False, n_alternatives=1),
        OptimizerStrategy("form", "recent_form", 1, CFG),
        HoldStrategy("hold", CFG),
    ]


@pytest.fixture(scope="module")
def result() -> pd.DataFrame:
    return run_season(
        DS,
        "2025-26",
        _strategies(),
        ["2024-25"],
        horizon=2,
        retrain_every=2,
        sim=SIM,
        gameweeks=[1, 2, 3, 4],
        initial_cfg=CFG,
    )


def test_every_strategy_plays_every_gameweek_legally(result: pd.DataFrame) -> None:
    assert set(result["strategy"]) == {"engine", "form", "hold"}
    assert result.groupby("strategy")["gw"].apply(list).map(lambda g: g == [1, 2, 3, 4]).all()
    assert result["valid"].all()
    hold = result[result["strategy"] == "hold"]
    assert (hold["transfers"] == 0).all() and (hold["hit_points"] == 0).all()
    # GW1 is the initial squad: no paid transfers anywhere
    assert (result.loc[result["gw"] == 1, "hit_points"] == 0).all()


def test_scores_are_actual_points_and_hindsight_bounds_them(result: pd.DataFrame) -> None:
    assert (result["points"] == result["raw_points"] - result["hit_points"]).all()
    # the hindsight-best lineup of the same squad can never score less than the chosen lineup
    assert (result["hindsight_lineup_points"] >= result["raw_points"]).all()
    # data used is never newer than the decision cutoff
    assert (result["data_age_hours"].dropna() >= 0).all()


def test_scoring_recomputes_with_domain_rules(result: pd.DataFrame) -> None:
    pm = DS["player_match"]
    pl = DS["players"]
    pos = {
        int(c): Position(p)
        for c, p in zip(
            pl.loc[pl["season"] == "2025-26", "player_code"],
            pl.loc[pl["season"] == "2025-26", "position"],
            strict=True,
        )
    }
    rs = load_ruleset("2025-26")
    for r in result.itertuples():
        g = pm[(pm["season"] == "2025-26") & (pm["gw"] == r.gw)].groupby("player_code")
        pts, mins = g["points"].sum().to_dict(), g["minutes"].sum().to_dict()
        lu = Lineup(
            starters=tuple(r.starters),
            bench=tuple(r.bench),
            captain=r.captain,
            vice_captain=r.vice_captain,
        )
        assert (
            gameweek_points(lu, pos, pts, mins, rs, r.chip.rsplit("_", 1)[0] if r.chip else None)[
                "points"
            ]
            == r.raw_points
        )


def test_no_lookahead_future_perturbation_does_not_change_decisions() -> None:
    """Corrupt everything after GW3's cutoff: decisions up to GW3 must be identical."""
    cut3 = next(c for c in cutoffs(DS, ["2025-26"]) if c.gw == 3).cutoff
    strategies = [OptimizerStrategy("form", "recent_form", 1, CFG), HoldStrategy("hold", CFG)]
    a = run_season(
        DS,
        "2025-26",
        strategies,
        ["2024-25"],
        horizon=2,
        retrain_every=2,
        sim=SIM,
        gameweeks=[1, 2, 3],
        initial_cfg=CFG,
    )
    b = run_season(
        _perturb_future(DS, cut3, 7),
        "2025-26",
        strategies,
        ["2024-25"],
        horizon=2,
        retrain_every=2,
        sim=SIM,
        gameweeks=[1, 2, 3],
        initial_cfg=CFG,
    )
    cols = ["strategy", "gw", "transfers_out", "transfers_in", "captain", "chip"]
    pd.testing.assert_frame_equal(a[cols], b[cols])
