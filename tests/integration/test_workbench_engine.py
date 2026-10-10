"""Transfer Workbench evaluation on the fixture snapshot (M1.2, no database): HOLD is the
baseline, every plan is a whole-squad plan validated against the rules, hit costs are separated
from gross gains, illegal requests are rejected with the rule they break, the optimiser's limits
hold, and the workbench's HOLD is the recommendation engine's HOLD."""

from __future__ import annotations

from pathlib import Path

import pytest

from fpl_api.container import AppServices, build_recommendation
from fpl_api.settings import Settings
from fpl_api.workbench import workbench_problem
from fpl_decision.workbench import PlanSpec, evaluate_plans
from fpl_domain.enums import Position
from fpl_domain.squad import SquadPick
from fpl_domain.state import ManagerState, initial_chips
from fpl_optimizer.milp import build_and_solve
from fpl_optimizer.problem import OptimizationProblem, load_optimizer_config
from tests.fixtures_util import fixture_dataset


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


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict:  # type: ignore[type-arg]
    svc = AppServices.build(
        _settings(tmp_path_factory.mktemp("wb")), fixture_dataset("2024-25", "2025-26", "2026-27")
    )
    ctx = svc.context()
    fc = svc.forecast_for(ctx, 2)
    rs = svc.data.ruleset(ctx.season)
    table = svc.table(ctx, fc, 2)
    empty = ManagerState(
        season=ctx.season,
        gameweek=ctx.gameweek,
        squad=(),
        bank=1000,
        free_transfers=1,
        chips=initial_chips(rs),
    )
    built = build_and_solve(
        OptimizationProblem(
            state=empty,
            ruleset=rs,
            players=table,
            gameweeks=tuple(range(ctx.gameweek, ctx.gameweek + 2)),
            config=load_optimizer_config("default"),
            initial_squad_mode=True,
        )
    )
    idx = table.index()
    pos = table.positions_map()
    picks = tuple(
        SquadPick(
            player_code=c,
            position=pos[c],
            team_code=int(table.team[idx[c]]),
            purchase_price=int(table.price[idx[c]]),
        )
        for c in built.first.squad
    )
    state = ManagerState(
        season=ctx.season,
        gameweek=ctx.gameweek,
        squad=picks,
        bank=built.first.bank_after + 5,
        free_transfers=1,
        chips=initial_chips(rs),
        source="manual",
    )
    prob = workbench_problem(svc, ctx, fc, state, 2, "default")
    return {"svc": svc, "ctx": ctx, "fc": fc, "prob": prob, "state": state}


def _swap(prob: OptimizationProblem, position: Position = Position.MID) -> tuple[int, int]:
    pl, st, rs = prob.players, prob.state, prob.ruleset
    idx = pl.index()
    prices = {int(c): int(p) for c, p in zip(pl.code, pl.price, strict=True)}
    sale = st.selling_values(prices, rs)
    owned = set(st.codes)
    pos = pl.positions_map()
    out = next(p.player_code for p in st.squad if p.position is position)
    clubs: dict[int, int] = {}
    for p in st.squad:
        if p.player_code != out:
            clubs[p.team_code] = clubs.get(p.team_code, 0) + 1
    budget = st.bank + sale[out]
    cands = sorted(
        (prices[c], c)
        for c in pl.code.tolist()
        if c not in owned
        and pos[c] is position
        and prices[c] <= budget
        and clubs.get(int(pl.team[idx[c]]), 0) < rs.squad.max_per_club
    )
    return out, cands[len(cands) // 2][1]  # a mid-priced legal replacement


def test_hold_baseline_whole_squad_plans_and_hit_accounting(world: dict) -> None:  # type: ignore[type-arg]
    prob, fc, svc, ctx = world["prob"], world["fc"], world["svc"], world["ctx"]
    out, inc = _swap(prob)
    specs = [
        PlanSpec(kind="hold"),
        PlanSpec(kind="transfer", label="mine", sells=[out], buys=[inc]),
        PlanSpec(kind="replace", out_player=out, candidates=2),
        PlanSpec(kind="best", label="best", alternatives=1),
    ]
    names, teams = svc.data.names(ctx.season), svc.data.teams(ctx.season)
    res = evaluate_plans(prob, fc, specs, names, teams)
    hold = res.plans[0]
    assert hold.label == "hold" and hold.action == "HOLD" and hold.hit_cost_vs_hold == 0
    assert hold.gain_horizon.mean == 0.0 and hold.gain_horizon.probability_positive == 0.0
    assert hold.sells == [] and hold.buys == [] and hold.transfers_now == 0
    assert res.horizon == 2 and res.gameweek == ctx.gameweek and res.n_simulations == 200
    labels = [p.label for p in res.plans]
    assert "mine" in labels and "best" in labels and "best-alt-1" in labels
    assert sum(p.kind == "replace" for p in res.plans) == 2
    assert res.excluded == [], res.excluded
    for p in res.plans:
        assert p.valid, (p.label, p.issues)
        assert len(p.squad_after) == 15 and len(set(p.squad_after)) == 15
        assert len(p.lineup["starters"]) == 11 and len(p.lineup["bench"]) == 4
        assert len(p.timeline) == 2 and p.timeline[0].gameweek == ctx.gameweek
        assert p.hits_total == sum(s.hit_points for s in p.timeline)
        assert p.hit_cost_vs_hold == p.hits_total - hold.hits_total
        assert abs(p.gross_gain_horizon - (p.gain_horizon.mean + p.hit_cost_vs_hold)) < 1e-9
        assert p.gain_horizon.p10 <= p.gain_horizon.p50 <= p.gain_horizon.p90
        assert 0.0 <= p.gain_horizon.probability_positive <= 1.0
        assert p.optimality and (p.proven_optimal == (p.optimality == "proven optimal"))
        assert len(p.moves_in) == len(p.buys) and len(p.moves_out) == len(p.sells)
        assert p.bank_after >= 0
    # distinct first-gameweek moves between the optimiser's best plan and its alternative
    best = next(p for p in res.plans if p.label == "best")
    alt = next(p for p in res.plans if p.label == "best-alt-1")
    assert (set(best.sells), set(best.buys)) != (set(alt.sells), set(alt.buys))
    # valid plans are ordered by the optimiser's objective after HOLD
    gains = [p.objective_gain_vs_hold for p in res.plans[1:] if p.valid]
    assert gains == sorted(gains, reverse=True)
    assert any("screened" in n for n in res.notes)


def test_an_exact_transfer_is_evaluated_as_requested(world: dict) -> None:  # type: ignore[type-arg]
    prob, fc, st = world["prob"], world["fc"], world["state"]
    out, inc = _swap(prob, Position.DEF)
    res = evaluate_plans(prob, fc, [PlanSpec(kind="transfer", label="t", sells=[out], buys=[inc])])
    plan = next(p for p in res.plans if p.label == "t")
    assert plan.requested_by == "manager" and plan.kind == "transfer"
    assert plan.sells == [out] and plan.buys == [inc] and plan.transfers_now == 1
    assert plan.action == "TRANSFER" and plan.hit_now == 0  # one free transfer available
    assert inc in plan.squad_after and out not in plan.squad_after
    prices = {int(c): int(p) for c, p in zip(prob.players.code, prob.players.price, strict=True)}
    selling = st.selling_values(prices, prob.ruleset)
    assert plan.moves_out[0].selling_price == selling[out]
    assert plan.moves_in[0].selling_price is None and plan.moves_in[0].price == prices[inc]
    assert plan.bank_after == st.bank + selling[out] - prices[inc]
    assert plan.valid, plan.issues


def test_illegal_requests_are_rejected_with_the_rule_they_break(world: dict) -> None:  # type: ignore[type-arg]
    prob, fc, st = world["prob"], world["fc"], world["state"]
    out, inc = _swap(prob)
    owned_other = next(c for c in st.codes if c != out)
    gk = next(p.player_code for p in st.squad if p.position is Position.GK)
    pl = prob.players
    pos = pl.positions_map()
    a_gk_outside = next(
        int(c) for c in pl.code.tolist() if c not in set(st.codes) and pos[c] is Position.GK
    )
    specs = [
        PlanSpec(kind="transfer", label="owned", sells=[out], buys=[owned_other]),
        PlanSpec(kind="transfer", label="size", sells=[out], buys=[inc, a_gk_outside]),
        PlanSpec(kind="transfer", label="position", sells=[gk], buys=[inc]),
        PlanSpec(kind="transfer", label="unknown", sells=[out], buys=[999_999_999]),
        PlanSpec(kind="replace", label="notmine", out_player=999_999_999),
    ]
    res = evaluate_plans(prob, fc, specs)
    assert [p.label for p in res.plans] == ["hold"]
    reasons = {e["label"]: e["reason"] for e in res.excluded}
    assert "already in the squad" in reasons["owned"]
    assert "must stay at 15" in reasons["size"]
    assert "position_quota" in reasons["position"] or "needs" in reasons["position"]
    assert "no forecast" in reasons["unknown"]
    assert "not in the squad" in reasons["notmine"]


def test_optimiser_limits_hold(world: dict) -> None:  # type: ignore[type-arg]
    prob, fc = world["prob"], world["fc"]
    res = evaluate_plans(
        prob,
        fc,
        [
            PlanSpec(kind="best", label="nohit", allow_hits=False),
            PlanSpec(kind="best", label="one", max_transfers=1),
        ],
    )
    nohit = next(p for p in res.plans if p.label == "nohit")
    one = next(p for p in res.plans if p.label == "one")
    assert nohit.hits_total == 0 and nohit.hit_cost_vs_hold <= 0
    assert one.transfers_now <= 1 and one.valid
    assert nohit.valid


def test_workbench_hold_is_the_engines_hold(world: dict) -> None:  # type: ignore[type-arg]
    """Same pool, same HOLD problem, same samples: the baseline the workbench compares against
    is exactly the recommendation engine's HOLD counterfactual."""
    prob, fc, svc, st = world["prob"], world["fc"], world["svc"], world["state"]
    res = evaluate_plans(prob, fc, [PlanSpec(kind="hold")])
    _, pkg = build_recommendation(
        svc,
        "wb",
        st,
        None,
        horizon=2,
        persist=False,
        run_stability=False,
        run_scenarios=False,
        run_chips=False,
    )
    hold = res.plans[0]
    assert hold.expected_points_horizon == pytest.approx(
        pkg.explanation.hold_counterfactual["expected_points_horizon"], abs=1e-9
    )
    assert [s.expected_points for s in hold.timeline] == [
        s.expected_points for s in pkg.hold.timeline
    ]
    assert hold.squad_after == sorted(st.codes)
