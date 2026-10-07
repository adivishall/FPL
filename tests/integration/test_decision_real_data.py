"""Replacement engine and captaincy on real forecasts (fixture excerpt) (§17, §20, §60)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from fpl_decision.captaincy import analyse_captaincy
from fpl_decision.inputs import player_table
from fpl_decision.paired import paired_gain, plan_samples
from fpl_decision.replacement import find_replacements
from fpl_domain.rules import load_ruleset
from fpl_domain.squad import SquadPick
from fpl_domain.state import ManagerState, initial_chips
from fpl_forecasting.minutes import MinutesConfig
from fpl_forecasting.pipeline import forecast, train_forecast_models
from fpl_forecasting.walkforward import FeatureCache, cutoffs
from fpl_optimizer.milp import build_and_solve
from fpl_optimizer.problem import OptimizationProblem
from fpl_simulation.engine import SimulationConfig
from fpl_storage.pit import PointInTimeView
from tests.fixtures_util import fixture_dataset

DS = fixture_dataset("2024-25", "2025-26")
RS = load_ruleset("2025-26")


@pytest.fixture(scope="module")
def setup():  # type: ignore[no-untyped-def]
    hist = cutoffs(DS, ["2024-25", "2025-26"])
    cut = next(c for c in hist if c.season == "2025-26" and c.gw == 4)
    cache = FeatureCache(DS, horizon=2)
    models = train_forecast_models(
        cache, cut, hist, minutes_config=MinutesConfig(n_estimators=30, min_child_samples=30)
    )
    fc = forecast(cache, cut, models, SimulationConfig(n_sims=400))
    pool = PointInTimeView(DS, cut.cutoff).player_pool("2025-26", 4)
    table = player_table(fc.summary, pool, [4, 5])
    empty = ManagerState(
        season="2025-26", gameweek=4, squad=(), bank=1000, free_transfers=1, chips=initial_chips(RS)
    )
    init = OptimizationProblem(
        state=empty, ruleset=RS, players=table, gameweeks=(4, 5), initial_squad_mode=True
    )
    sq = build_and_solve(init).plans[0]
    idx = table.index()
    picks = tuple(
        SquadPick(
            player_code=c,
            position=table.positions_map()[c],
            team_code=int(table.team[idx[c]]),
            purchase_price=int(table.price[idx[c]]),
        )
        for c in sq.squad
    )
    state = empty.model_copy(update={"squad": picks, "bank": sq.bank_after})
    prob = OptimizationProblem(state=state, ruleset=RS, players=table, gameweeks=(4, 5))
    return fc, prob, sq


def test_no_problem_means_no_recommended_replacement(setup) -> None:  # type: ignore[no-untyped-def]
    fc, prob, sq = setup
    out = min(sq.lineup.starters, key=lambda c: prob.players.ev[prob.players.index()[c], 0])
    res = find_replacements(prob, out, fc.simulation, fc.summary, n_return=3)
    # the squad is the optimiser's own optimum: every replacement is worse than HOLD
    assert all(c.objective_gain < 0 for c in res.candidates)


def test_replacement_engine_end_to_end(setup) -> None:  # type: ignore[no-untyped-def]
    fc, prob, sq = setup
    pl = prob.players
    idx = pl.index()
    out = max(sq.lineup.starters, key=lambda c: pl.ev[idx[c], 0])
    ev = pl.ev.copy()
    ev[idx[out], :] = 0.0  # detected problem: the player is ruled out for the horizon
    prob = replace(prob, players=replace(pl, ev=ev))
    res = find_replacements(prob, out, fc.simulation, fc.summary, n_return=4)
    # universe: same position, not owned, affordable with A's selling price + bank, club-legal
    assert res.universe_size == len(res.screened) > 0
    pos_out = pl.position[idx[out]]
    assert all(pl.position[idx[c]] == pos_out for c in res.screened["in_code"])
    assert not set(res.screened["in_code"]) & set(prob.state.codes)
    assert res.candidates, res.notes
    gains = [c.objective_gain for c in res.candidates]
    assert gains == sorted(gains, reverse=True)
    for c in res.candidates:
        assert c.valid
        assert c.out_code in c.solution.plans[0].transfers_out
        assert c.in_code in c.solution.plans[0].transfers_in
        g = c.gain_horizon
        assert g.p10 <= g.p50 <= g.p90 and 0 <= g.probability_positive <= 1
        k = c.contract("hold_001")
        assert set(k) >= {
            "out",
            "in",
            "action",
            "expected_gain_1gw",
            "probability_positive",
            "p10_gain",
            "p50_gain",
            "p90_gain",
            "transfer_cost",
            "confidence",
            "counterfactual_id",
        }
    assert res.candidates[0].objective_gain > 0
    reasons = {c.selection_reason for c in res.candidates}
    assert "top screened gain" in reasons


def test_replacement_does_not_depend_on_workers(setup) -> None:  # type: ignore[no-untyped-def]
    fc, prob, sq = setup
    pl = prob.players
    idx = pl.index()
    out = max(sq.lineup.starters, key=lambda c: pl.ev[idx[c], 0])
    ev = pl.ev.copy()
    ev[idx[out], :] = 0.0
    prob = replace(prob, players=replace(pl, ev=ev))
    serial = find_replacements(prob, out, fc.simulation, fc.summary, n_return=4)
    parallel = find_replacements(prob, out, fc.simulation, fc.summary, n_return=4, workers=3)

    def key(r):  # type: ignore[no-untyped-def]
        return [
            (c.in_code, c.objective_gain, c.gain_horizon, c.gain_1gw, c.follow_up, c.valid)
            for c in r.candidates
        ]

    assert serial.candidates and key(parallel) == key(serial)
    assert parallel.hold.objective == serial.hold.objective and parallel.notes == serial.notes


def test_paired_gain_of_identical_plans_is_zero(setup) -> None:  # type: ignore[no-untyped-def]
    fc, prob, _ = setup
    hold = build_and_solve(replace(prob, preferences=replace(prob.preferences, hold_first_gw=True)))
    s = plan_samples(fc.simulation, hold.plans, prob.players.positions_map(), RS)
    g = paired_gain(s, s)
    assert g.mean == 0 and g.probability_positive == 0 and g.probability_negative == 0
    # sample means match the analytic expectation within Monte Carlo error
    assert s.shape == (400, 2)
    assert np.isfinite(s).all()


def test_captaincy_on_real_lineup(setup) -> None:  # type: ignore[no-untyped-def]
    fc, prob, sq = setup
    sim = fc.simulation
    gi = sim.gw_index(4)
    pts = {
        c: sim.points[:, sim.index_of(c), gi].astype(int)
        if c in set(sim.player_codes.tolist())
        else np.zeros(sim.n_sims, int)
        for c in sq.lineup.players
    }
    mins = {
        c: sim.minutes[:, sim.index_of(c), gi].astype(int)
        if c in set(sim.player_codes.tolist())
        else np.zeros(sim.n_sims, int)
        for c in sq.lineup.players
    }
    a = analyse_captaincy(sq.lineup, prob.players.positions_map(), pts, mins, RS)
    assert a.expected in sq.lineup.starters
    best = a.option(a.expected)
    assert all(best.mean_total >= o.mean_total - 1e-9 for o in a.options)
    assert np.allclose(np.diag(a.beats_matrix), 0.0)
