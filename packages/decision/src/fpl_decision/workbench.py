"""Transfer Workbench (M1.2): whole-squad evaluation of concrete transfer plans against HOLD.

Every plan the manager or the engine proposes is solved as a complete multi-gameweek plan on the
same candidate pool, validated independently against the season's rules, scored on the same
joint simulation samples as HOLD (paired gains, ADR-0007) and reported with its transfer cost
separated from its gross gain. Plan kinds:

* ``hold`` — no transfer this gameweek, optimal afterwards: the baseline, always evaluated;
* ``transfer`` — exactly the named sells and buys now (``Preferences.first_gw_exact``), optimal
  afterwards; an illegal request is rejected with the rule it breaks, never solved around;
* ``replace`` — the replacement engine's shortlist for one outgoing player (screened over every
  affordable, club-legal candidate), each evaluated as an exact single transfer;
* ``best`` — the optimiser's best plan plus distinct alternatives, optionally capped in
  first-gameweek transfers or forbidden to take hits anywhere in the plan.

Nothing here changes a forecast, a simulation or an optimiser constraint: the pieces are the
recommendation engine's MILP, independent validator, paired comparison and option summary.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from fpl_decision.engine import GainSummary, PlanStep, summarise_option
from fpl_decision.paired import plan_samples
from fpl_decision.replacement import shortlist_replacements
from fpl_domain.squad import SquadPick, club_allowance, squad_violations
from fpl_forecasting.pipeline import Forecast
from fpl_optimizer.alternatives import PlanOption, hold_problem, solve_with_alternatives
from fpl_optimizer.milp import OptimizationError, Solution, build_and_solve
from fpl_optimizer.parallel import map_ordered
from fpl_optimizer.pool import candidate_pool
from fpl_optimizer.problem import OptimizationProblem
from fpl_optimizer.validate import ValidationResult, validate_solution

WORKBENCH_VERSION = "workbench-1"
Kind = Literal["hold", "transfer", "replace", "best"]
Requester = Literal["manager", "engine"]


class PlanSpec(BaseModel):
    """What to evaluate. ``transfer`` names the exact moves; ``replace`` asks the engine for
    ``candidates`` replacements of ``out_player``; ``best`` asks the optimiser for its best plan
    and ``alternatives`` distinct ones, with optional limits on transfers now and on hits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Kind
    label: str | None = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_\- ]+$")
    sells: list[int] = Field(default_factory=list, max_length=15)
    buys: list[int] = Field(default_factory=list, max_length=15)
    out_player: int | None = Field(None, gt=0)
    candidates: int = Field(3, ge=1, le=6)
    max_transfers: int | None = Field(None, ge=1, le=15)
    allow_hits: bool = True
    alternatives: int = Field(0, ge=0, le=4)


class PlayerMove(BaseModel):
    player_code: int
    name: str
    position: str
    team: str
    price: int  # current purchase price (tenths)
    selling_price: int | None = None  # owned players: what the manager actually receives
    xp_next: float
    xp_horizon: float
    p_start_next: float | None = None


class PlanEvaluation(BaseModel):
    """One plan, scored on the whole squad over the horizon, against HOLD."""

    label: str
    kind: Kind
    requested_by: Requester
    reason: str  # what was asked, or why the engine shortlisted it
    action: str  # HOLD | TRANSFER | HIT | CHIP
    sells: list[int]
    buys: list[int]
    moves_out: list[PlayerMove]
    moves_in: list[PlayerMove]
    valid: bool  # independent rules replay of the whole plan
    issues: list[str]
    optimality: str
    proven_optimal: bool
    transfers_now: int
    hit_now: int  # points paid in the first gameweek
    hits_total: int  # points paid over the plan
    hit_cost_vs_hold: int  # the plan's hits minus HOLD's (HOLD may pay later)
    expected_points_1gw: float  # net of hits, mean over the simulation
    expected_points_horizon: float
    gain_1gw: GainSummary  # paired vs HOLD, net of hits
    gain_horizon: GainSummary
    gross_gain_horizon: float  # gain_horizon.mean + hit_cost_vs_hold
    objective_gain_vs_hold: float  # optimiser units (discounted, with terminal values)
    passes_thresholds: bool  # the profile's E[gain] and P(gain > 0) thresholds
    bank_after: int
    free_transfers_after: int
    squad_after: list[int]
    lineup: dict[str, Any]
    timeline: list[PlanStep]
    follow_up: list[dict[str, Any]]  # moves the plan makes in later gameweeks


class WorkbenchResult(BaseModel):
    version: str = WORKBENCH_VERSION
    season: str
    gameweek: int
    horizon: int
    plans: list[PlanEvaluation]  # HOLD first, then valid plans by optimiser objective
    excluded: list[dict[str, Any]]  # requested or shortlisted plans that could not be scored
    notes: list[str]
    n_simulations: int
    simulation_seed: int | None
    assumptions: list[str]
    config_ref: str
    ruleset_version: str
    problem: dict[str, Any]  # OptimizationProblem.describe() of the evaluated pool
    timings: dict[str, float]


@dataclass
class _Request:
    label: str
    kind: Kind
    requested_by: Requester
    reason: str
    sells: tuple[int, ...] = ()
    buys: tuple[int, ...] = ()
    issues: tuple[str, ...] = ()  # non-empty: rejected before solving
    spec: PlanSpec | None = None  # ``best`` requests keep their limits
    problem: OptimizationProblem | None = None


def _solve(p: OptimizationProblem) -> Solution | str:
    try:
        return build_and_solve(p)
    except OptimizationError as exc:
        return str(exc)


def check_moves(
    prob: OptimizationProblem, sells: tuple[int, ...], buys: tuple[int, ...]
) -> list[str]:
    """Why an explicit transfer cannot be made now, in the rules' own words (empty = may be
    legal; the MILP and the independent validator still decide the whole plan)."""
    pl, st, rs = prob.players, prob.state, prob.ruleset
    idx = pl.index()
    owned = set(st.codes)
    issues = [f"{c} is not in the squad" for c in sells if c not in owned]
    issues += [f"{c} is already in the squad" for c in buys if c in owned]
    issues += [f"{c}: no forecast on this snapshot" for c in (*sells, *buys) if c not in idx]
    issues += [f"{c} is excluded by your preferences" for c in buys if c in prob.preferences.banned]
    if len(sells) != len(buys):
        issues.append(
            f"{len(sells)} out but {len(buys)} in: the squad must stay at {rs.squad.size}"
        )
    if issues:
        return issues
    prices = {int(c): int(p) for c, p in zip(pl.code, pl.price, strict=True)}
    sale = st.selling_values(prices, rs)
    pos = pl.positions_map()
    picks = [p for p in st.squad if p.player_code not in sells] + [
        SquadPick(
            player_code=c, position=pos[c], team_code=int(pl.team[idx[c]]), purchase_price=prices[c]
        )
        for c in buys
    ]
    bank = st.bank + sum(sale[c] for c in sells) - sum(prices[c] for c in buys)
    return [str(v) for v in squad_violations(picks, rs, bank, club_allowance(st.squad, rs))]


def _move(
    code: int,
    prob: OptimizationProblem,
    summary: pd.DataFrame,
    names: dict[int, str],
    teams: dict[int, str],
    selling: dict[int, int],
) -> PlayerMove:
    pl = prob.players
    i = pl.index()[code]
    nxt = summary[(summary["player_code"] == code) & (summary["gw"] == prob.gameweeks[0])]
    team = int(pl.team[i])
    return PlayerMove(
        player_code=code,
        name=names.get(code, str(code)),
        position=pl.positions_map()[code].value,
        team=teams.get(team, str(team)),
        price=int(pl.price[i]),
        selling_price=selling.get(code),
        xp_next=float(pl.ev[i, 0]),
        xp_horizon=float(pl.ev[i].sum()),
        p_start_next=float(nxt["prob_start"].iloc[0]) if len(nxt) else None,
    )


def _requests(
    prob: OptimizationProblem,
    specs: list[PlanSpec],
    summary: pd.DataFrame,
    names: dict[int, str],
    notes: list[str],
    excluded: list[dict[str, Any]],
) -> tuple[list[_Request], set[int]]:
    """The plans to solve and every player they name (who must enter the candidate pool)."""
    st = prob.state
    out: list[_Request] = []
    must: set[int] = set()
    for n, spec in enumerate(specs, start=1):
        label = spec.label or f"{spec.kind}-{n}"
        if spec.kind == "hold":
            continue  # always evaluated
        if spec.kind == "transfer":
            sells, buys = tuple(dict.fromkeys(spec.sells)), tuple(dict.fromkeys(spec.buys))
            issues = tuple(check_moves(prob, sells, buys))
            if not issues:
                must.update(buys)
            out.append(_Request(label, "transfer", "manager", "your transfer", sells, buys, issues))
        elif spec.kind == "replace":
            o = spec.out_player
            if o is None or o not in st.codes:
                excluded.append({"label": label, "reason": f"{o} is not in the squad"})
                continue
            try:
                sl = shortlist_replacements(prob, o, summary, n_screen=max(spec.candidates, 4))
            except OptimizationError as exc:
                excluded.append({"label": label, "reason": str(exc)})
                continue
            picks = list(sl.picks.items())[: spec.candidates]
            notes.append(
                f"{names.get(o, o)}: {len(sl.universe)} affordable, club-legal replacements "
                f"screened; {len(picks)} evaluated as whole-squad plans"
            )
            for k, (c, why) in enumerate(picks, start=1):
                must.add(c)
                out.append(_Request(f"{label}-{k}", "replace", "engine", why, (o,), (c,)))
        else:
            why = "optimiser's best plan"
            if spec.max_transfers:
                why += f" (at most {spec.max_transfers} transfer(s) now)"
            if not spec.allow_hits:
                why += " (no hits)"
            out.append(_Request(label, "best", "engine", why, spec=spec))
    return out, must


def _best_options(
    r: _Request, base: OptimizationProblem, hold: Solution
) -> list[tuple[_Request, OptimizationProblem, PlanOption]]:
    assert r.spec is not None
    p = replace(
        base,
        preferences=replace(
            base.preferences,
            hold_first_gw=False,
            first_gw_max_transfers=r.spec.max_transfers,
            max_hits_total=None if r.spec.allow_hits else 0,
        ),
    )
    try:
        found = solve_with_alternatives(p, n_alternatives=r.spec.alternatives, hold=hold)
    except OptimizationError:
        return []
    out = []
    for o in found:
        if o.label == "hold":
            continue
        label = r.label if o.label == "best" else f"{r.label}-{o.label}"
        why = r.reason if o.label == "best" else f"next-best plan with a different move ({o.label})"
        out.append(
            (replace(r, label=label, reason=why, sells=tuple(o.sells), buys=tuple(o.buys)), p, o)
        )
    return out


def evaluate_plans(
    prob: OptimizationProblem,
    fc: Forecast,
    specs: list[PlanSpec],
    names: dict[int, str] | None = None,
    teams: dict[int, str] | None = None,
    workers: int = 1,
) -> WorkbenchResult:
    """``prob`` holds the full player universe (every owned player and every player with a
    forecast); plans are solved on the candidate pool plus every player a plan names. ``workers``
    solves independent plans in parallel processes; the result does not depend on it."""
    t0 = time.perf_counter()
    names, teams = names or {}, teams or {}
    cfg, st, rs = prob.config, prob.state, prob.ruleset
    notes: list[str] = []
    excluded: list[dict[str, Any]] = []
    prices = {int(c): int(p) for c, p in zip(prob.players.code, prob.players.price, strict=True)}
    selling = st.selling_values(prices, rs)
    summary = fc.summary[fc.summary["gw"].isin(set(prob.gameweeks))]
    requests, must = _requests(prob, specs, summary, names, notes, excluded)

    # --- solve: HOLD and every exact plan in parallel, then the optimiser's own plans
    pool = candidate_pool(
        prob.players, st.codes, cfg.pool, must_include=sorted(must), discount=cfg.objective.discount
    )
    base = replace(prob, players=pool)
    exact = [r for r in requests if r.kind in ("transfer", "replace") and not r.issues]
    exact_problems: list[OptimizationProblem] = []
    for r in exact:
        r.problem = replace(
            base,
            preferences=replace(
                base.preferences,
                forced_out=frozenset(r.sells),
                forced_in=frozenset(r.buys),
                hold_first_gw=False,
                first_gw_exact=True,
            ),
        )
        exact_problems.append(r.problem)
    hold_prob = hold_problem(base)
    t1 = time.perf_counter()
    hold_r, *solved = map_ordered(_solve, [hold_prob, *exact_problems], workers)
    if isinstance(hold_r, str):
        raise OptimizationError(f"HOLD plan infeasible: {hold_r}")
    hold = hold_r
    hold_val: ValidationResult = validate_solution(hold_prob, hold)
    hold_req = _Request("hold", "hold", "manager", "make no transfer this gameweek")
    options: list[tuple[_Request, OptimizationProblem, PlanOption]] = [
        (hold_req, hold_prob, PlanOption("hold", hold, hold_val, 0.0))
    ]
    for r, sol in zip(exact, solved, strict=True):
        assert r.problem is not None
        if isinstance(sol, str):
            excluded.append(
                {
                    "label": r.label,
                    "reason": f"no legal plan: {sol}",
                    "sells": list(r.sells),
                    "buys": list(r.buys),
                }
            )
            continue
        val = validate_solution(r.problem, sol)
        options.append(
            (r, r.problem, PlanOption(r.label, sol, val, sol.objective - hold.objective))
        )
    for r in requests:
        if r.kind == "best":
            options.extend(_best_options(r, base, hold))
    for r in requests:
        if r.issues:
            excluded.append(
                {
                    "label": r.label,
                    "reason": "; ".join(r.issues),
                    "sells": list(r.sells),
                    "buys": list(r.buys),
                }
            )
    timings = {"optimise": time.perf_counter() - t1}

    # --- score every plan on the same samples as HOLD
    t2 = time.perf_counter()
    positions = prob.players.positions_map()
    pf = fc.players
    n_fix: dict[tuple[int, int], int] = {}
    for c, g in zip(pf.player_code.tolist(), pf.gameweek.tolist(), strict=True):
        n_fix[(c, g)] = n_fix.get((c, g), 0) + 1
    hold_s = plan_samples(fc.simulation, hold.plans, positions, rs)
    plans: list[PlanEvaluation] = []
    for r, _p, opt in options:
        sol = opt.solution
        samples = plan_samples(fc.simulation, sol.plans, positions, rs)
        summ = summarise_option(opt, samples, hold_s, cfg, n_fix)
        first = sol.first
        hit_cost = sol.total_hits - hold.total_hits
        plans.append(
            PlanEvaluation(
                label=r.label,
                kind=r.kind,
                requested_by=r.requested_by,
                reason=r.reason,
                action=summ.action,
                sells=summ.sells,
                buys=summ.buys,
                moves_out=[_move(c, prob, summary, names, teams, selling) for c in summ.sells],
                moves_in=[_move(c, prob, summary, names, teams, selling) for c in summ.buys],
                valid=opt.validation.valid,
                issues=list(opt.validation.issues),
                optimality=summ.optimality,
                proven_optimal=sol.proven_optimal,
                transfers_now=len(first.transfers_in),
                hit_now=first.hit_points,
                hits_total=sol.total_hits,
                hit_cost_vs_hold=hit_cost,
                expected_points_1gw=float(samples[:, 0].mean()),
                expected_points_horizon=float(samples.sum(axis=1).mean()),
                gain_1gw=summ.gain_1gw,
                gain_horizon=summ.gain_horizon,
                gross_gain_horizon=summ.gain_horizon.mean + hit_cost,
                objective_gain_vs_hold=summ.objective_gain_vs_hold,
                passes_thresholds=summ.passes_thresholds,
                bank_after=first.bank_after,
                free_transfers_after=(
                    sol.plans[1].free_transfers if len(sol.plans) > 1 else sol.free_transfers_end
                ),
                squad_after=sorted(first.squad),
                lineup={
                    "starters": list(first.lineup.starters),
                    "bench": list(first.lineup.bench),
                    "captain": first.lineup.captain,
                    "vice_captain": first.lineup.vice_captain,
                },
                timeline=summ.timeline,
                follow_up=[
                    {
                        "gameweek": pl.gameweek,
                        "out": list(pl.transfers_out),
                        "in": list(pl.transfers_in),
                        "chip": pl.chip_id,
                        "hit_points": pl.hit_points,
                    }
                    for pl in sol.plans[1:]
                    if pl.transfers_in or pl.chip_id
                ],
            )
        )
    timings["simulate"] = time.perf_counter() - t2
    hold_plan, rest = plans[0], plans[1:]
    rest.sort(key=lambda e: (not e.valid, -e.objective_gain_vs_hold, -e.gain_horizon.mean))
    prov = fc.provenance
    timings["total"] = time.perf_counter() - t0
    return WorkbenchResult(
        season=st.season,
        gameweek=prob.gameweeks[0],
        horizon=len(prob.gameweeks),
        plans=[hold_plan, *rest],
        excluded=excluded,
        notes=notes,
        n_simulations=int(prov.get("n_simulations") or fc.simulation.n_sims),
        simulation_seed=prov.get("simulation_seed"),
        assumptions=[
            f"horizon {len(prob.gameweeks)} gameweeks (GW{prob.gameweeks[0]}–"
            f"GW{prob.gameweeks[-1]}); objective profile '{cfg.profile}' ({cfg.config_ref})",
            "every plan is scored on the same joint simulation samples as HOLD "
            "(gains are paired per sample and net of hit points)",
            "prices held at current values inside the horizon; selling prices follow the "
            "half-profit rule from your purchase prices",
            "later gameweeks of every plan are re-optimised; only the first gameweek is "
            "a commitment",
        ],
        config_ref=cfg.config_ref or cfg.profile,
        ruleset_version=rs.ruleset_version,
        problem=base.describe(),
        timings=timings,
    )
