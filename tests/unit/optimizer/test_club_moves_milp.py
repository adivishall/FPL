"""MILP and state machine agree on club limits after mid-season club moves (§16, §61)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from fpl_optimizer.milp import build_and_solve
from fpl_optimizer.validate import validate_solution
from tests.opt_util import tiny_league


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_excess_from_a_club_move_is_kept_or_reduced_and_plans_stay_valid(seed: int) -> None:
    prob = tiny_league(seed, extras=(2, 3, 3, 2), horizon=3, free_transfers=2)
    owned = [p.player_code for p in prob.state.squad]
    movers = owned[2:6]  # four outfield players now play for club 1
    team = prob.players.team.copy()
    idx = prob.players.index()
    for c in movers:
        team[idx[c]] = 1
    squad = tuple(
        p.model_copy(update={"team_code": 1}) if p.player_code in movers else p
        for p in prob.state.squad
    )
    held_club1 = sum(p.team_code == 1 for p in squad)
    assert held_club1 > 3
    prob = replace(
        prob,
        players=replace(prob.players, team=team),
        state=prob.state.model_copy(update={"squad": squad}),
    )
    sol = build_and_solve(prob)
    res = validate_solution(prob, sol)
    assert res.valid, res.issues
    for plan in sol.plans:
        n = sum(int(team[idx[c]]) == 1 for c in plan.squad)
        assert n <= held_club1  # never increased beyond the inherited excess
    # a club that was within the limit still respects it
    for plan in sol.plans:
        counts = np.bincount([int(team[idx[c]]) for c in plan.squad])
        assert all(n <= 3 for k, n in enumerate(counts) if k != 1)
