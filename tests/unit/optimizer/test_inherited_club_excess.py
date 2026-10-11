"""Defect 53 (BUILD_STATUS §2d), pinned as a regression: with an inherited club excess the
MILP caps the club at the *initial* owned count for every horizon gameweek, while the rules
replay lets an excess be kept or reduced but never increased again. The independent
validator rejects such a plan and the engine falls back to the best *validated* plan — which
is therefore "the best plan among those explored", not a proven optimum, and is labelled so:
the cut that excluded the illegal plan also excluded the legal plans sharing its first move.
When the optimiser is fixed, the first assertion flips and this file must be updated with the
fix (and the golden regenerated)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from fpl_decision.engine import summarise_option
from fpl_optimizer.alternatives import solve_with_alternatives
from fpl_optimizer.milp import build_and_solve
from fpl_optimizer.problem import OptimizerConfig, Preferences
from fpl_optimizer.validate import validate_solution
from tests.opt_util import tiny_league


def _excess_case():  # type: ignore[no-untyped-def]
    """Four owned players from club 1 (an inherited excess over the limit of three). Owned
    club-1 player A is worthless; every other owned player is solid; of the unowned players
    only B (not club 1, good now) and C (club 1, worth a fortune next gameweek) have value.
    The profitable plan sells A for B now and B for C next gameweek — returning the club-1
    count to four, which the rules forbid once it has dropped to three."""
    prob = tiny_league(5, extras=(2, 4, 4, 3), horizon=2, free_transfers=1)
    pl = prob.players
    idx = pl.index()
    pos = pl.positions_map()
    team = pl.team.copy()
    ev = np.zeros_like(pl.ev)
    owned = list(prob.state.codes)
    for c in owned:
        ev[idx[c], :] = 5.0
    outfield = [c for c in owned if pos[c].value != "GK"]
    movers = outfield[:4]  # exactly four owned players play for club 1 (the inherited excess)
    for c in owned:
        if int(team[idx[c]]) == 1 and c not in movers:
            team[idx[c]] = 9
    for c in movers:
        team[idx[c]] = 1
    a = movers[0]
    # A is a liability even on the last bench slot: selling him now is the only sensible move
    ev[idx[a], :] = -1000.0
    unowned_same = [c for c in pl.code.tolist() if c not in owned and pos[c] == pos[a]]
    b, c_code = unowned_same[0], unowned_same[1]
    team[idx[b]] = 2
    ev[idx[b], :] = [12.0, 1.0]  # B: the best buy now, worthless next gameweek
    team[idx[c_code]] = 1
    ev[idx[c_code], :] = [0.0, 40.0]
    price = pl.price.copy()
    price[idx[b]] = price[idx[a]]
    price[idx[c_code]] = price[idx[a]]
    squad = tuple(
        p.model_copy(update={"team_code": int(team[idx[p.player_code]])}) for p in prob.state.squad
    )
    prob = replace(
        prob,
        players=replace(pl, team=team, ev=ev, price=price, q10=None),
        state=prob.state.model_copy(update={"squad": squad, "bank": 20}),
        # one transfer per gameweek (a legitimate manager preference): the two-step plan
        preferences=Preferences(max_transfers_per_gw=1),
    )
    return prob, a, b, c_code


def test_milp_lets_an_inherited_excess_grow_back_and_the_validator_rejects_it() -> None:
    prob, a, b, c_code = _excess_case()
    sol = build_and_solve(prob)
    first, second = sol.plans
    assert a in first.transfers_out and b in first.transfers_in, "A → B now is the profitable move"
    assert c_code in second.transfers_in, "and C (club 1) comes in next gameweek"

    def club1(squad: tuple[int, ...]) -> int:
        return sum(int(prob.players.team[prob.players.index()[c]]) == 1 for c in squad)

    assert club1(prob.state.codes) == 4
    assert club1(first.squad) == 3 and club1(second.squad) == 4  # the excess grew back
    res = validate_solution(prob, sol)
    assert not res.valid and any("club_limit" in i for i in res.issues), res.issues
    # the engine's safeguard: the explored options include a validated plan to fall back on,
    # and the invalid plan is marked as such — it is never "the best valid plan"
    options = solve_with_alternatives(prob, n_alternatives=3)
    best = next(o for o in options if o.label == "best")
    assert best.validation.valid is False
    assert any(o.validation.valid for o in options if o.label != "hold")
    assert np.isfinite(best.solution.objective)


def test_plans_found_after_the_illegal_one_do_not_claim_optimality() -> None:
    """The legal plan "A → B now, keep B" shares the illegal plan's first move, so its cut
    removed it from every later solve: the alternatives are the best *explored* legal plans,
    solved to optimality only within the cut problem, and must not be labelled proven
    optimal. HOLD is solved separately and keeps its claim."""
    prob, *_ = _excess_case()
    options = {o.label: o for o in solve_with_alternatives(prob, n_alternatives=3)}
    assert options["hold"].solution.optimality() == "proven optimal"
    later = [o for label, o in options.items() if label.startswith("alt-")]
    assert later and all(o.validation.valid for o in later)
    for o in later:
        assert o.solution.status == "Optimal"  # optimal for the cut problem only
        assert not o.solution.proven_optimal
        assert o.solution.optimality().startswith(
            "best valid plan among those explored; not proven optimal ("
        )


def test_the_engine_gate_never_passes_the_illegal_plan() -> None:
    """Whatever its simulated gain, an option that fails the rules replay cannot clear the
    decision thresholds, so the engine can never choose it."""
    prob, *_ = _excess_case()
    options = {o.label: o for o in solve_with_alternatives(prob, n_alternatives=1)}
    hold = np.zeros((64, 2))
    overwhelming = np.full((64, 2), 100.0)
    illegal = summarise_option(options["best"], overwhelming, hold, OptimizerConfig())
    legal = summarise_option(options["alt-1"], overwhelming, hold, OptimizerConfig())
    assert illegal.valid is False and illegal.passes_thresholds is False
    assert legal.valid is True and legal.passes_thresholds is True
