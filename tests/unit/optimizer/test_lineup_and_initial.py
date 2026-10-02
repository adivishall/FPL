"""Exact lineup solver (hand-verifiable) and initial-squad selection vs brute force (§15, §63)."""

from __future__ import annotations

import numpy as np
import pytest

from fpl_domain.enums import ChipType, Position
from fpl_domain.rules import load_ruleset
from fpl_domain.squad import formation, formation_is_legal
from fpl_optimizer.bruteforce import brute_force_initial_squad
from fpl_optimizer.lineup import best_lineup
from fpl_optimizer.milp import OptimizationError, build_and_solve
from fpl_optimizer.problem import ObjectiveWeights
from fpl_optimizer.validate import validate_solution
from tests.opt_util import tiny_league

RS = load_ruleset("2026-27")
POS = [Position.GK] * 2 + [Position.DEF] * 5 + [Position.MID] * 5 + [Position.FWD] * 3
CODES = list(range(1, 16))
POSITIONS = dict(zip(CODES, POS, strict=True))


def _solve(ev: list[float], w: ObjectiveWeights | None = None, chip=None):  # type: ignore[no-untyped-def]
    e = dict(zip(CODES, ev, strict=True))
    return best_lineup(CODES, POSITIONS, e, e, w or ObjectiveWeights(), RS, chip)


def test_picks_formation_captain_and_bench_by_hand() -> None:
    # GK 5/1; DEF 6,5,4,1,1; MID 8,7,3,2,2; FWD 9,1,1
    ev = [5, 1, 6, 5, 4, 1, 1, 8, 7, 3, 2, 2, 9, 1, 1]
    ch = _solve(ev, ObjectiveWeights(vice_weight=0.0, bench_weights=(0, 0, 0), bench_gk_weight=0))
    lu = ch.lineup
    # best XI: GK1, DEF 6,5,4 + MID 8,7,3,2,2 + FWD 9 + one of the 1-pointers → 3-5-2 or 4-5-1…
    assert lu.captain == 13  # FWD with 9
    assert set(lu.starters) >= {1, 3, 4, 5, 8, 9, 10, 11, 12, 13}
    assert lu.bench[0] == 2  # substitute goalkeeper first
    assert formation_is_legal(formation(lu.starters, POSITIONS), RS)
    assert ch.expected_points == pytest.approx(5 + 6 + 5 + 4 + 8 + 7 + 3 + 2 + 2 + 9 + 1 + 9)


def test_bench_order_by_expected_points() -> None:
    ev = [5, 1, 6, 5, 4, 3, 2, 8, 7, 3, 2, 0.5, 9, 4, 1]
    lu = _solve(ev).lineup
    out_bench = lu.bench[1:]
    vals = [ev[c - 1] for c in out_bench]
    assert vals == sorted(vals, reverse=True)


def test_triple_captain_and_bench_boost_values() -> None:
    ev = [5, 1, 6, 5, 4, 3, 2, 8, 7, 3, 2, 0.5, 9, 4, 1]
    w = ObjectiveWeights(vice_weight=0.0, bench_weights=(0, 0, 0), bench_gk_weight=0.0)
    base = _solve(ev, w)
    tc = _solve(ev, w, ChipType.TRIPLE_CAPTAIN)
    bb = _solve(ev, w, ChipType.BENCH_BOOST)
    assert tc.value - base.value == pytest.approx(9.0)  # one extra captain multiple
    assert bb.value - base.value == pytest.approx(
        sum(ev) - sum(ev[c - 1] for c in base.lineup.starters)
    )


def test_lineup_matches_brute_force_over_all_xis() -> None:
    rng = np.random.default_rng(3)
    w = ObjectiveWeights()
    for _ in range(20):
        ev = rng.gamma(2, 2, 15).tolist()
        ch = _solve(ev, w)
        # brute force over captains for the chosen XI cannot beat it, and any random legal XI is ≤
        e = dict(zip(CODES, ev, strict=True))
        for _ in range(30):
            perm = rng.permutation(CODES[2:])
            xi = [1, *perm[:10].tolist()]
            if not formation_is_legal(formation(xi, POSITIONS), RS):
                continue
            bench = sorted(perm[10:].tolist(), key=lambda c: -e[c])
            top = sorted(xi, key=lambda c: -e[c])
            v = sum(e[c] for c in xi) + e[top[0]] + w.vice_weight * e[top[1]]
            v += sum(wk * e[c] for wk, c in zip(w.bench_weights, bench, strict=True))
            v += w.bench_gk_weight * e[2]
            assert v <= ch.value + 1e-9


def _initial(seed: int, budget: int):  # type: ignore[no-untyped-def]
    prob = tiny_league(1000 + seed, extras=(1, 1, 1, 1), horizon=1, bank=0)
    return prob.__class__(
        state=prob.state.model_copy(update={"squad": (), "bank": budget}),
        ruleset=prob.ruleset,
        players=prob.players,
        gameweeks=prob.gameweeks,
        config=prob.config,
        initial_squad_mode=True,
    )


@pytest.mark.parametrize("seed", range(3))
def test_initial_squad_matches_brute_force(seed: int) -> None:
    prob = _initial(seed, 1250)
    sol = build_and_solve(prob)
    val = validate_solution(prob, sol)
    assert val.valid, val.issues
    ref_obj, _ = brute_force_initial_squad(prob)
    assert sol.objective == pytest.approx(ref_obj, abs=1e-6)
    idx = prob.players.index()
    assert sum(int(prob.players.price[idx[c]]) for c in sol.plans[0].squad) <= 1250


def test_infeasible_budget_is_reported_not_papered_over() -> None:
    prob = _initial(0, 500)  # no legal 15-man squad costs ≤ £50.0m here
    assert brute_force_initial_squad(prob)[0] == float("-inf")
    with pytest.raises(OptimizationError, match="no feasible solution"):
        build_and_solve(prob)
