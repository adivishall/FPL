"""Optimiser on real forecasts (fixture excerpt): initial squad, transfers with alternatives,
HOLD counterfactual, independent validation, and the cost of candidate-pool pruning."""

from __future__ import annotations

from dataclasses import replace

import pytest

from fpl_decision.inputs import player_table
from fpl_domain.rules import load_ruleset
from fpl_domain.squad import SquadPick
from fpl_domain.state import ManagerState, initial_chips
from fpl_forecasting.minutes import MinutesConfig
from fpl_forecasting.pipeline import forecast, train_forecast_models
from fpl_forecasting.walkforward import FeatureCache, cutoffs
from fpl_optimizer.alternatives import solve_with_alternatives
from fpl_optimizer.milp import build_and_solve
from fpl_optimizer.pool import candidate_pool
from fpl_optimizer.problem import OptimizationProblem, OptimizerConfig, PoolSettings
from fpl_optimizer.validate import validate_solution
from fpl_simulation.engine import SimulationConfig
from fpl_storage.pit import PointInTimeView
from tests.fixtures_util import fixture_dataset

DS = fixture_dataset("2024-25", "2025-26")


@pytest.fixture(scope="module")
def inputs():  # type: ignore[no-untyped-def]
    hist = cutoffs(DS, ["2024-25", "2025-26"])
    cut = next(c for c in hist if c.season == "2025-26" and c.gw == 4)
    cache = FeatureCache(DS, horizon=2)
    models = train_forecast_models(
        cache, cut, hist, minutes_config=MinutesConfig(n_estimators=30, min_child_samples=30)
    )
    fc = forecast(cache, cut, models, SimulationConfig(n_sims=300))
    pool = PointInTimeView(DS, cut.cutoff).player_pool("2025-26", 4)
    table = player_table(fc.summary, pool, [4, 5])
    return cut, table


def test_initial_squad_then_transfers(inputs) -> None:  # type: ignore[no-untyped-def]
    _, table = inputs
    rs = load_ruleset("2025-26")
    empty = ManagerState(
        season="2025-26", gameweek=4, squad=(), bank=1000, free_transfers=1, chips=initial_chips(rs)
    )
    cfg = OptimizerConfig()
    init = OptimizationProblem(
        state=empty,
        ruleset=rs,
        players=table,
        gameweeks=(4, 5),
        config=cfg,
        initial_squad_mode=True,
    )
    sol = build_and_solve(init)
    val = validate_solution(init, sol)
    assert val.valid, val.issues
    assert len(sol.plans[0].squad) == 15
    idx = table.index()
    picks = tuple(
        SquadPick(
            player_code=c,
            position=table.positions_map()[c],
            team_code=int(table.team[idx[c]]),
            purchase_price=int(table.price[idx[c]]),
        )
        for c in sol.plans[0].squad
    )
    state = empty.model_copy(update={"squad": picks, "bank": sol.plans[0].bank_after})
    # make the problem interesting: the best-value midfielder is ruled out (e.g. injured)
    worst = max(sol.plans[0].lineup.starters, key=lambda c: table.ev[idx[c], 0])
    ev = table.ev.copy()
    ev[idx[worst], :] = 0.0
    hurt = replace(table, ev=ev)
    prob = OptimizationProblem(state=state, ruleset=rs, players=hurt, gameweeks=(4, 5), config=cfg)
    options = solve_with_alternatives(prob, n_alternatives=2)
    assert all(o.validation.valid for o in options), [o.validation.issues for o in options]
    labels = [o.label for o in options]
    assert labels[0] == "hold" and "best" in labels
    best = next(o for o in options if o.label == "best")
    assert best.gain_vs_hold >= -1e-6
    assert worst in best.sells  # the zero-EV starter is sold
    actions = {(o.sells, o.buys) for o in options if o.label != "hold"}
    assert len(actions) == len([o for o in options if o.label != "hold"])  # distinct actions


def test_pool_pruning_costs_nothing_here(inputs) -> None:  # type: ignore[no-untyped-def]
    _, table = inputs
    rs = load_ruleset("2025-26")
    empty = ManagerState(
        season="2025-26", gameweek=4, squad=(), bank=1000, free_transfers=1, chips=initial_chips(rs)
    )
    full = OptimizationProblem(
        state=empty, ruleset=rs, players=table, gameweeks=(4, 5), initial_squad_mode=True
    )
    pooled_table = candidate_pool(table, (), PoolSettings())
    pooled = replace(full, players=pooled_table)
    assert pooled_table.n < table.n
    a, b = build_and_solve(full), build_and_solve(pooled)
    assert b.objective == pytest.approx(a.objective, rel=1e-6)
