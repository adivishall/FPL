"""The MILP must match exhaustive search on tiny leagues and pass independent validation
(§36 flagship optimiser test, §61 'unit-tested with small hand-verifiable examples')."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from fpl_optimizer.bruteforce import brute_force
from fpl_optimizer.milp import build_and_solve
from fpl_optimizer.problem import ObjectiveWeights, Preferences
from fpl_optimizer.validate import validate_solution
from tests.opt_util import tiny_league


def _check(prob, tol: float = 1e-5) -> None:  # type: ignore[no-untyped-def]
    sol = build_and_solve(prob)
    val = validate_solution(prob, sol)
    assert val.valid, val.issues
    ref = brute_force(prob)
    assert ref.objective == pytest.approx(sol.objective, abs=tol), (ref, sol.objective)


@pytest.mark.parametrize("seed", range(6))
def test_two_gameweek_transfers_match_brute_force(seed: int) -> None:
    _check(tiny_league(seed, free_transfers=1 + seed % 3))


@pytest.mark.parametrize("seed", range(3))
def test_risk_aversion_and_churn_penalty(seed: int) -> None:
    w = ObjectiveWeights(
        risk_aversion=0.4, transfer_penalty=0.7, discount=0.85, free_transfer_value=0.3
    )
    _check(tiny_league(100 + seed, weights=w))


@pytest.mark.parametrize("seed", range(3))
def test_hits_are_taken_only_when_worth_it(seed: int) -> None:
    w = ObjectiveWeights(free_transfer_value=0.0)
    prob = tiny_league(200 + seed, extras=(1, 1, 2, 1), free_transfers=1, horizon=1, weights=w)
    _check(prob)


@pytest.mark.parametrize("chip", ["wildcard_1", "bench_boost_1", "triple_captain_1"])
def test_chip_choice_matches_brute_force(chip: str) -> None:
    prob = tiny_league(
        300,
        chip_options={chip: (10, 11)},
        weights=ObjectiveWeights(chip_values={chip.rsplit("_", 1)[0]: 3.0}),
    )
    _check(prob)


def test_free_hit_reverts_and_matches_brute_force() -> None:
    prob = tiny_league(
        400,
        extras=(1, 1, 1, 1),
        chip_options={"free_hit_1": (10,)},
        weights=ObjectiveWeights(chip_values={"free_hit": 2.0}),
    )
    sol = build_and_solve(prob)
    val = validate_solution(prob, sol)
    assert val.valid, val.issues
    assert brute_force(prob).objective == pytest.approx(sol.objective, abs=1e-5)
    if sol.plans[0].chip_type is not None:
        # persistent squad unchanged through the Free Hit week
        assert set(sol.plans[0].squad) == set(prob.state.codes)


@pytest.mark.slow
def test_forced_free_hit_what_if() -> None:
    prob = tiny_league(401, forced_chips={"free_hit_1": 10})
    sol = build_and_solve(prob)
    assert sol.plans[0].chip_id == "free_hit_1"
    assert validate_solution(prob, sol).valid
    assert brute_force(prob).objective == pytest.approx(sol.objective, abs=1e-5)


def test_afcon_top_up_and_banking_rules() -> None:
    # 2025-26: free transfers are topped up to 5 before GW16
    prob = tiny_league(500, season="2025-26", gameweek=14, horizon=3, free_transfers=2)
    sol = build_and_solve(prob)
    assert validate_solution(prob, sol).valid
    assert sol.plans[2].free_transfers == 5
    assert brute_force(prob).objective == pytest.approx(sol.objective, abs=1e-5)


def test_post_world_cup_unlimited_week() -> None:
    # 2022-23: GW17 transfers were unlimited
    prob = tiny_league(501, season="2022-23", gameweek=16, horizon=2, free_transfers=1)
    sol = build_and_solve(prob)
    assert validate_solution(prob, sol).valid
    assert sol.plans[1].hit_points == 0
    assert brute_force(prob).objective == pytest.approx(sol.objective, abs=1e-5)


def test_preferences_lock_ban_force() -> None:
    base = tiny_league(600)
    owned = base.state.codes
    extra = [int(c) for c in base.players.code if int(c) not in owned]
    pref = Preferences(
        locked=frozenset(owned[:3]), banned=frozenset(extra[:1]), forced_out=frozenset([owned[5]])
    )
    prob = tiny_league(600, preferences=pref)
    sol = build_and_solve(prob)
    assert owned[5] in sol.plans[0].transfers_out
    assert not set(owned[:3]) & set().union(*(p.transfers_out for p in sol.plans))
    assert extra[0] not in set().union(*(p.transfers_in for p in sol.plans))
    assert validate_solution(prob, sol).valid
    assert brute_force(prob).objective == pytest.approx(sol.objective, abs=1e-5)


def test_hold_is_explicit() -> None:
    prob = tiny_league(700, preferences=Preferences(hold_first_gw=True))
    sol = build_and_solve(prob)
    assert sol.plans[0].transfers_in == () and sol.plans[0].transfers_out == ()
    assert brute_force(prob).objective == pytest.approx(sol.objective, abs=1e-5)


def test_hand_verifiable_single_swap() -> None:
    """One obviously better, affordable midfielder: the only sensible move is that swap."""
    prob = tiny_league(
        800,
        extras=(0, 0, 1, 0),
        horizon=1,
        bank=100,
        weights=ObjectiveWeights(
            free_transfer_value=0.0, bench_weights=(0, 0, 0), bench_gk_weight=0.0, vice_weight=0.0
        ),
    )
    pl = prob.players
    new = int(pl.code[pl.position == 2][-1])
    worst_mid = min(
        (c for c in prob.state.codes if pl.positions_map()[c].value == "MID"),
        key=lambda c: pl.ev[pl.index()[c], 0],
    )
    pl.ev[pl.index()[new], 0] = 50.0  # dominant
    sol = build_and_solve(prob)
    assert sol.plans[0].transfers_in == (new,)
    assert sol.plans[0].lineup.captain == new
    # gain = (50 − ev_out) as starter + 50 extra as captain vs previous best captain …
    assert validate_solution(prob, sol).valid
    assert worst_mid in prob.state.codes
    assert np.isclose(brute_force(prob).objective, sol.objective)


def test_each_chip_respects_its_own_allowed_weeks() -> None:
    """Regression: chip options with different allowed weeks must not leak into each other."""
    prob = tiny_league(
        900,
        chip_options={"bench_boost_1": (10,), "triple_captain_1": (11,)},
        weights=ObjectiveWeights(chip_values={"bench_boost": -50.0, "triple_captain": -50.0}),
    )
    sol = build_and_solve(prob)  # negative chip values: both chips are attractive to play
    chips = {p.gameweek: p.chip_id for p in sol.plans}
    assert chips == {10: "bench_boost_1", 11: "triple_captain_1"}
    assert validate_solution(prob, sol).valid
    assert brute_force(prob).objective == pytest.approx(sol.objective, abs=1e-5)


def test_warm_started_chip_choice_is_never_worse_than_no_chip() -> None:
    from fpl_optimizer.milp import solve_with_chips

    prob = tiny_league(1100, chip_options={"wildcard_1": (10, 11), "bench_boost_1": (10, 11)})
    sol = solve_with_chips(prob)
    assert sol.stats["warm_start"] is True
    no_chip = build_and_solve(replace(prob, chip_options={}))
    assert sol.objective >= no_chip.objective - 1e-9
    assert validate_solution(prob, sol).valid
    assert brute_force(prob).objective == pytest.approx(sol.objective, abs=1e-5)


def test_time_limited_incumbent_is_polished_and_valid() -> None:
    """A time-limited incumbent may carry a sub-optimal lineup; polishing re-solves lineups
    exactly so independent validation (incl. the recomputed objective) passes."""
    from fpl_optimizer.problem import SolverSettings

    prob = tiny_league(1200, extras=(3, 6, 6, 4), horizon=3)
    base = build_and_solve(prob)
    # corrupt the warm start: move the armband to the weakest starter in the first gameweek
    vals = dict(base.values)
    idx = prob.players.index()
    first = base.plans[0].lineup
    weak = min(first.starters, key=lambda c: prob.players.ev[idx[c], 0])
    vals.pop(f"c[{idx[first.captain]},0]", None)
    vals.pop(f"v[{idx[weak]},0]", None)
    vals[f"c[{idx[weak]},0]"] = 1.0
    if weak == first.vice_captain:
        vals[f"v[{idx[first.captain]},0]"] = 1.0
    bad = replace(base, values=vals)
    cfg = prob.config.model_copy(update={"solver": SolverSettings(time_limit_seconds=1e-4)})
    sol = build_and_solve(replace(prob, config=cfg), start=bad)
    v = validate_solution(prob, sol)
    assert v.valid, (sol.status, v.issues)
    assert sol.objective == pytest.approx(base.objective, abs=1e-6)  # repaired to the optimum


def test_time_limited_incumbent_with_slack_hits_is_repaired() -> None:
    """An incumbent may charge a hit for a transfer that is actually free (the hit variable is
    only bounded below); the polish recomputes hits from the true free-transfer path."""
    from fpl_optimizer.problem import SolverSettings

    for seed in range(1200, 1260):
        prob = tiny_league(seed, extras=(3, 6, 6, 4), horizon=3, free_transfers=2)
        base = build_and_solve(prob)
        if base.plans[0].transfers_in and base.plans[0].paid_transfers == 0:
            break
    else:  # pragma: no cover
        pytest.fail("no seed with a free first-week transfer")
    vals = dict(base.values)
    vals["h[0]"] = vals.get("h[0]", 0.0) + 1.0  # charge one spurious hit
    bad = replace(base, values=vals)
    cfg = prob.config.model_copy(update={"solver": SolverSettings(time_limit_seconds=1e-4)})
    sol = build_and_solve(replace(prob, config=cfg), start=bad)
    assert sol.status != "Optimal"  # the incumbent was returned, not re-optimised
    # reported honestly: best found within the limit, never "optimal"
    assert base.proven_optimal and base.optimality() == "proven optimal"
    assert not sol.proven_optimal and sol.optimality().endswith("not proven optimal")
    v = validate_solution(prob, sol)
    assert v.valid, (sol.status, v.issues)
    assert sol.plans[0].hit_points == 0


@pytest.mark.parametrize(("gw", "horizon"), [(37, 2), (38, 1), (34, 5)])
def test_no_terminal_value_after_the_last_gameweek(gw: int, horizon: int) -> None:
    """Banked transfers and money are worthless once the season ends (objective = replay)."""
    prob = tiny_league(7, gameweek=gw, horizon=horizon, free_transfers=3)
    sol = build_and_solve(prob)
    assert "free_transfers_end" not in sol.terms
    v = validate_solution(prob, sol)
    assert v.valid, v.issues
    mid = tiny_league(7, gameweek=30, horizon=horizon, free_transfers=3)
    assert "free_transfers_end" in build_and_solve(mid).terms  # still valued mid-season
