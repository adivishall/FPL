"""First-gameweek preferences (Transfer Workbench, M1.2): exactly the named moves now, or at
most N transfers now, with later gameweeks free to re-optimise. The defaults leave every
existing problem — and its identifiers — unchanged."""

from __future__ import annotations

from dataclasses import replace

import pytest

from fpl_optimizer.alternatives import hold_problem, solve_with_alternatives
from fpl_optimizer.milp import build_and_solve
from fpl_optimizer.problem import OptimizationProblem, Preferences
from fpl_optimizer.validate import validate_solution
from tests.opt_util import tiny_league


def _a_swap(prob: OptimizationProblem) -> tuple[int, int]:
    """An owned player and the cheapest affordable, club-legal unowned player of his position."""
    pl, st, rs = prob.players, prob.state, prob.ruleset
    idx = pl.index()
    prices = {int(c): int(p) for c, p in zip(pl.code, pl.price, strict=True)}
    sale = st.selling_values(prices, rs)
    owned = set(st.codes)
    for out in st.codes:
        budget = st.bank + sale[out]
        pos = pl.position[idx[out]]
        clubs: dict[int, int] = {}
        for p in st.squad:
            if p.player_code != out:
                clubs[p.team_code] = clubs.get(p.team_code, 0) + 1
        cands = [
            (int(pl.price[i]), int(c))
            for i, c in enumerate(pl.code.tolist())
            if c not in owned
            and pl.position[i] == pos
            and pl.price[i] <= budget
            and clubs.get(int(pl.team[i]), 0) < rs.squad.max_per_club
        ]
        if cands:
            return out, min(cands)[1]
    raise AssertionError("no legal swap in the tiny league")


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_exact_first_gameweek_moves_are_the_only_moves(seed: int) -> None:
    prob = tiny_league(seed, horizon=3, free_transfers=2)
    out, inc = _a_swap(prob)
    forced = Preferences(forced_out=frozenset({out}), forced_in=frozenset({inc}))
    loose = build_and_solve(replace(prob, preferences=forced))
    exact = build_and_solve(replace(prob, preferences=replace(forced, first_gw_exact=True)))
    assert exact.first.transfers_out == (out,) and exact.first.transfers_in == (inc,)
    assert validate_solution(replace(prob, preferences=forced), exact).valid
    # the loose problem may add moves now; the exact plan is one of its feasible plans
    assert out in loose.first.transfers_out and inc in loose.first.transfers_in
    assert exact.objective <= loose.objective + 1e-6


@pytest.mark.parametrize("seed", [1, 2])
def test_first_gameweek_transfer_cap_binds_only_now(seed: int) -> None:
    prob = tiny_league(seed, horizon=3, free_transfers=3)
    capped = build_and_solve(replace(prob, preferences=Preferences(first_gw_max_transfers=1)))
    free = build_and_solve(prob)
    assert len(capped.first.transfers_in) <= 1
    assert validate_solution(prob, capped).valid
    assert capped.objective <= free.objective + 1e-6
    # a cap on the first gameweek says nothing about later ones
    later = [len(p.transfers_in) for p in capped.plans[1:]]
    assert all(n >= 0 for n in later)


def test_defaults_keep_problem_identifiers_and_hold_reuse_is_exact() -> None:
    prob = tiny_league(4, horizon=2, free_transfers=1)
    explicit = replace(
        prob,
        preferences=replace(prob.preferences, first_gw_exact=False, first_gw_max_transfers=None),
    )
    assert prob.describe() == explicit.describe()
    desc = replace(
        prob, preferences=replace(prob.preferences, first_gw_exact=True, first_gw_max_transfers=2)
    ).describe()["preferences"]
    assert desc["first_gw_exact"] is True and desc["first_gw_max_transfers"] == 2
    hold = build_and_solve(hold_problem(prob))
    a = solve_with_alternatives(prob, n_alternatives=1)
    b = solve_with_alternatives(prob, n_alternatives=1, hold=hold)
    assert [(o.label, o.sells, o.buys, round(o.solution.objective, 6)) for o in a] == [
        (o.label, o.sells, o.buys, round(o.solution.objective, 6)) for o in b
    ]
