"""Decision engine: HOLD vs TRANSFER vs HIT vs CHIP with evidence (§18, §26, §29, §68–§72).

Flow for one decision gameweek:

1. Candidate plans from the MILP on the candidate pool: the explicit HOLD plan and the best plan
   plus N alternatives with distinct first-gameweek actions (independently validated).
2. Every plan is scored on the joint simulation; gains vs HOLD are *paired* per sample (1 GW and
   horizon): mean, quantiles, P(gain > 0).
3. Decision policy (ADR-0007): plans are taken in optimiser-objective order and the first one
   whose horizon gain clears ``min_gain`` **and** ``P(gain > 0) ≥ min_prob_positive`` (profile
   thresholds) is recommended; if none does, HOLD is recommended with the reason ("do nothing" is
   an explicit decision class, §18).
4. For the chosen plan: lineup and captaincy analysis, stability under forecast perturbation
   (§29), stress scenarios (§69), structured evidence and binding constraints (§68), and the
   recommendation package (§72.1) with reproducibility identifiers.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from fpl_decision.captaincy import analyse_captaincy
from fpl_decision.chips import ChipPlan, plan_chips
from fpl_decision.evidence import Evidence, move_evidence, primary_drivers
from fpl_decision.paired import PairedGain, paired_gain, plan_samples
from fpl_decision.scenarios import (
    SIMULATION_KINDS,
    ScenarioSpec,
    perturb_forecast,
    perturb_table,
    stress_set,
)
from fpl_decision.stability import stability
from fpl_domain.enums import ChipType, Position
from fpl_domain.hashing import short_id
from fpl_domain.rules import Ruleset
from fpl_domain.state import ManagerState
from fpl_forecasting.pipeline import Forecast
from fpl_optimizer.alternatives import PlanOption, solve_with_alternatives
from fpl_optimizer.milp import OptimizationError, Solution, build_and_solve
from fpl_optimizer.pool import candidate_pool
from fpl_optimizer.problem import OptimizationProblem, OptimizerConfig, PlayerTable, Preferences

Action = Literal["HOLD", "TRANSFER", "HIT", "CHIP"]


class GainSummary(BaseModel):
    mean: float
    p10: float
    p50: float
    p90: float
    probability_positive: float

    @classmethod
    def of(cls, g: PairedGain) -> GainSummary:
        return cls(
            mean=g.mean,
            p10=g.p10,
            p50=g.p50,
            p90=g.p90,
            probability_positive=g.probability_positive,
        )


class PlanStep(BaseModel):
    gameweek: int
    action: Action
    transfers_out: list[int]
    transfers_in: list[int]
    chip: str | None
    hit_points: int
    free_transfers: int
    bank_after: int
    expected_points: float
    p10: float
    p90: float
    expected_delta_vs_hold: float
    probability_delta_positive: float
    captain: int
    vice_captain: int
    squad_blanks: list[int] = Field(default_factory=list)  # squad players without a fixture
    squad_doubles: list[int] = Field(default_factory=list)  # squad players with two fixtures


class OptionSummary(BaseModel):
    label: str
    action: Action
    sells: list[int]
    buys: list[int]
    chip: str | None
    objective: float
    objective_gain_vs_hold: float
    gain_1gw: GainSummary
    gain_horizon: GainSummary
    passes_thresholds: bool
    valid: bool
    timeline: list[PlanStep]


class ScenarioOutcome(BaseModel):
    name: str
    kind: str
    description: str
    feasible: bool
    gain_vs_hold: GainSummary | None = None
    objective_gain_vs_hold: float | None = None
    recommended_still_best: bool | None = None
    note: str = ""


class StabilityReport(BaseModel):
    perturbations: int
    share_same_action: float
    share_near_optimal: float
    label: str
    competing_actions: list[dict[str, Any]]
    mean_regret: float


class CaptainChoice(BaseModel):
    player: int
    vice_captain: int
    mean_total: float
    p25_total: float
    p90_total: float
    captain_mean: float
    p_captain_haul: float
    p_beats_expected_choice: float


class CaptaincyReport(BaseModel):
    expected: int
    safe: int
    high_variance: int
    options: list[CaptainChoice]


class DecisionExplanation(BaseModel):
    """§26 / §68.2 explanation object; every driver references evidence ids."""

    action: Action
    decision: str
    expected_effect: dict[str, float]
    primary_drivers: list[dict[str, Any]]
    supporting_evidence: list[str]
    constraints_binding: list[str]
    downside_scenarios: list[dict[str, Any]]
    hold_counterfactual: dict[str, Any]
    alternatives: list[dict[str, Any]]
    confidence: dict[str, float]
    refusal_reasons: list[str]
    assumptions: list[str]
    data_timestamp: str
    model_versions: dict[str, str]
    optimizer_run_id: str


class RecommendationPackage(BaseModel):
    """§72.1 recommendation API contract (plus the full evidence trail)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    decision_id: str
    season: str
    gameweek: int
    snapshot_id: str
    ruleset_version: str
    decision: dict[str, Any]
    chosen: OptionSummary
    hold: OptionSummary
    alternatives: list[OptionSummary]
    lineup: dict[str, Any]
    captaincy: CaptaincyReport | None
    evidence: list[Evidence]
    explanation: DecisionExplanation
    scenarios: list[ScenarioOutcome]
    stability: StabilityReport | None
    chips: list[ChipPlan] = Field(default_factory=list)
    assumptions: list[str]
    generated_at: str
    model_versions: dict[str, str]
    optimizer_run_id: str
    config_refs: dict[str, str]
    timings: dict[str, float] = Field(default_factory=dict)


@dataclass
class DecisionContext:
    state: ManagerState
    ruleset: Ruleset
    forecast: Forecast
    players: PlayerTable  # full universe with EV for the planning horizon
    config: OptimizerConfig
    gameweeks: tuple[int, ...]
    features: pd.DataFrame | None = None
    price_probs: pd.DataFrame | None = None
    names: dict[int, str] | None = None
    preferences: Preferences = field(default_factory=Preferences)
    chip_options: dict[str, tuple[int, ...]] | None = None


def _action(plan_first: Any) -> Action:
    if plan_first.chip_id is not None:
        return "CHIP"
    if plan_first.transfers_in:
        return "HIT" if plan_first.hit_points > 0 else "TRANSFER"
    return "HOLD"


def _timeline(
    sol: Solution,
    samples: np.ndarray,
    hold_samples: np.ndarray,
    n_fixtures: dict[tuple[int, int], int] | None = None,
) -> list[PlanStep]:
    steps = []
    nf = n_fixtures or {}
    for t, p in enumerate(sol.plans):
        d = samples[:, t] - hold_samples[:, t]
        steps.append(
            PlanStep(
                gameweek=p.gameweek,
                action=_action(p),
                transfers_out=list(p.transfers_out),
                transfers_in=list(p.transfers_in),
                chip=p.chip_id,
                hit_points=p.hit_points,
                free_transfers=p.free_transfers,
                bank_after=p.bank_after,
                expected_points=float(samples[:, t].mean()),
                p10=float(np.percentile(samples[:, t], 10)),
                p90=float(np.percentile(samples[:, t], 90)),
                expected_delta_vs_hold=float(d.mean()),
                probability_delta_positive=float((d > 0).mean()),
                captain=p.lineup.captain,
                vice_captain=p.lineup.vice_captain,
                squad_blanks=[c for c in p.playing_squad if nf.get((c, p.gameweek), 0) == 0],
                squad_doubles=[c for c in p.playing_squad if nf.get((c, p.gameweek), 0) >= 2],
            )
        )
    return steps


def _summarise(
    opt: PlanOption,
    samples: np.ndarray,
    hold_samples: np.ndarray,
    cfg: OptimizerConfig,
    n_fixtures: dict[tuple[int, int], int] | None = None,
) -> OptionSummary:
    g1 = paired_gain(samples, hold_samples, slice(0, 1))
    gh = paired_gain(samples, hold_samples)
    first = opt.solution.first
    is_hold = opt.label == "hold"
    passes = is_hold or (
        gh.mean >= cfg.decision.min_gain
        and gh.probability_positive >= cfg.decision.min_prob_positive
        and opt.validation.valid
    )
    return OptionSummary(
        label=opt.label,
        action=_action(first),
        sells=sorted(opt.sells),
        buys=sorted(opt.buys),
        chip=first.chip_id,
        objective=opt.solution.objective,
        objective_gain_vs_hold=opt.gain_vs_hold,
        gain_1gw=GainSummary.of(g1),
        gain_horizon=GainSummary.of(gh),
        passes_thresholds=passes,
        valid=opt.validation.valid,
        timeline=_timeline(opt.solution, samples, hold_samples, n_fixtures),
    )


def _binding_constraints(
    ctx: DecisionContext, sol: Solution, prob: OptimizationProblem
) -> list[str]:
    out = []
    first = sol.first
    rs = ctx.ruleset
    if first.bank_after < 5:
        out.append(
            f"budget: £{first.bank_after / 10:.1f}m left in the bank after GW{first.gameweek}"
        )
    n_in = len(first.transfers_in)
    free_week = first.chip_type in (ChipType.WILDCARD, ChipType.FREE_HIT)
    if not free_week and n_in and n_in >= first.free_transfers:
        out.append(
            f"free transfers: {n_in} transfer(s) use all {first.free_transfers} available"
            + (f"; {first.hit_points}-point hit" if first.hit_points else "")
        )
    idx = prob.players.index()
    teams = Counter(int(prob.players.team[idx[c]]) for c in first.squad)
    full = sorted(t for t, n in teams.items() if n >= rs.squad.max_per_club)
    if full:
        out.append(f"club limit: {rs.squad.max_per_club} players already from team(s) {full}")
    if prob.preferences.locked:
        out.append(f"user locks: {sorted(prob.preferences.locked)} must be kept")
    if prob.preferences.banned:
        out.append(f"user exclusions: {sorted(prob.preferences.banned)}")
    unavailable = [c.chip_id for c in ctx.state.chips if not c.usable_in(ctx.gameweeks[0])]
    if unavailable:
        out.append(f"chips not usable this GW: {unavailable}")
    return out


def recommend(
    ctx: DecisionContext,
    n_alternatives: int = 3,
    run_stability: bool = True,
    run_scenarios: bool = True,
    run_chips: bool = True,
) -> RecommendationPackage:
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    cfg = ctx.config
    fc = ctx.forecast
    pool = candidate_pool(
        ctx.players,
        ctx.state.codes,
        cfg.pool,
        must_include=list(ctx.preferences.forced_in),
        discount=cfg.objective.discount,
    )
    prob = OptimizationProblem(
        state=ctx.state,
        ruleset=ctx.ruleset,
        players=pool,
        gameweeks=ctx.gameweeks,
        config=cfg,
        preferences=ctx.preferences,
        chip_options=ctx.chip_options or {},
    )
    options = solve_with_alternatives(prob, n_alternatives=n_alternatives)
    timings["optimise"] = time.perf_counter() - t0
    positions = ctx.players.positions_map()
    t1 = time.perf_counter()
    samples = {
        o.label: plan_samples(fc.simulation, o.solution.plans, positions, ctx.ruleset)
        for o in options
    }
    hold_s = samples["hold"]
    pf = fc.players
    n_fix: dict[tuple[int, int], int] = {}
    for c, g in zip(pf.player_code.tolist(), pf.gameweek.tolist(), strict=True):
        n_fix[(c, g)] = n_fix.get((c, g), 0) + 1
    summaries = [_summarise(o, samples[o.label], hold_s, cfg, n_fix) for o in options]
    timings["simulate_plans"] = time.perf_counter() - t1
    by_label = {s.label: s for s in summaries}
    hold = by_label["hold"]
    ranked = sorted((s for s in summaries if s.label != "hold"), key=lambda s: -s.objective)
    refusal: list[str] = []
    chosen = hold
    for s in ranked:
        if s.action == "HOLD":
            continue
        if s.passes_thresholds and s.objective_gain_vs_hold > 0:
            chosen = s
            break
        refusal.append(
            f"{s.label} ({s.action} out {s.sells} in {s.buys}): horizon gain "
            f"{s.gain_horizon.mean:+.2f} pts (threshold {cfg.decision.min_gain}), "
            f"P(gain>0) {s.gain_horizon.probability_positive:.2f} "
            f"(threshold {cfg.decision.min_prob_positive})"
        )
    chosen_opt = next(o for o in options if o.label == chosen.label)
    sol = chosen_opt.solution
    first = sol.first

    # lineup + captaincy on the joint samples for the first gameweek
    sim = fc.simulation
    gw0 = ctx.gameweeks[0]
    pts, mins = {}, {}
    gi = sim.gw_index(gw0)
    codes_sim = set(sim.player_codes.tolist())
    for c in first.lineup.players:
        if c in codes_sim:
            pi = sim.index_of(c)
            pts[c] = sim.points[:, pi, gi].astype(np.int64)
            mins[c] = sim.minutes[:, pi, gi].astype(np.int64)
        else:
            pts[c] = mins[c] = np.zeros(sim.n_sims, dtype=np.int64)
    cap = analyse_captaincy(first.lineup, positions, pts, mins, ctx.ruleset, chip=first.chip_type)
    captaincy = CaptaincyReport(
        expected=cap.expected,
        safe=cap.safe,
        high_variance=cap.high_variance,
        options=[
            CaptainChoice(
                player=o.player_code,
                vice_captain=o.vice_captain,
                mean_total=o.mean_total,
                p25_total=o.p25_total,
                p90_total=o.p90_total,
                captain_mean=o.captain_mean,
                p_captain_haul=o.p_captain_haul,
                p_beats_expected_choice=o.p_beats_expected_choice,
            )
            for o in cap.options
        ],
    )

    # stability
    stab: StabilityReport | None = None
    if (run_stability and chosen.label != "hold") or run_stability:
        t2 = time.perf_counter()
        r = stability(prob, frozenset(chosen.sells), frozenset(chosen.buys))
        stab = StabilityReport(
            perturbations=r.perturbations,
            share_same_action=r.share_same_action,
            share_near_optimal=r.share_near_optimal,
            label=r.label,
            competing_actions=[
                {"sells": list(s), "buys": list(b), "count": c} for s, b, c in r.competing_actions
            ],
            mean_regret=r.mean_regret,
        )
        timings["stability"] = time.perf_counter() - t2

    # scenarios
    scen_out: list[ScenarioOutcome] = []
    if run_scenarios:
        t3 = time.perf_counter()
        idx = ctx.players.index()
        in_teams = tuple(sorted({int(ctx.players.team[idx[c]]) for c in chosen.buys}))
        specs = stress_set(
            tuple(chosen.sells), tuple(chosen.buys), first.lineup.captain, gw0, in_teams
        )
        for spec in specs:
            scen_out.append(_run_scenario(ctx, prob, spec, sol, options, positions))
        timings["scenarios"] = time.perf_counter() - t3

    chip_plans: list[ChipPlan] = []
    if run_chips:
        t4 = time.perf_counter()
        chip_plans = plan_chips(prob, sol, fc, cfg.decision.min_prob_positive)
        timings["chips"] = time.perf_counter() - t4

    # evidence and explanation
    snapshot = str(fc.provenance.get("data_snapshot_id"))
    model_version = str(fc.provenance.get("model_versions", {}).get("points", "points"))
    evidence = move_evidence(
        chosen.sells or [],
        chosen.buys or [],
        fc.summary,
        ctx.gameweeks,
        snapshot,
        model_version,
        ctx.features,
        ctx.price_probs,
        ctx.names,
    )
    first_pts = samples[chosen.label][:, 0]
    run_id = short_id(
        "opt",
        {
            "problem": prob.describe(),
            "state": ctx.state.state_hash,
            "forecast": fc.run_id,
            "chosen": [chosen.sells, chosen.buys, chosen.chip],
        },
    )
    downside = [
        {
            "scenario": s.name,
            "description": s.description,
            "gain_vs_hold": None if s.gain_vs_hold is None else s.gain_vs_hold.mean,
            "still_best": s.recommended_still_best,
            "feasible": s.feasible,
            "note": s.note,
        }
        for s in scen_out
        if s.name != "expected"
    ]
    downside.sort(key=lambda d: (d["gain_vs_hold"] is None, d["gain_vs_hold"] or 0.0))
    assumptions = [
        f"decision cutoff {fc.provenance.get('cutoff')}; latest source data used "
        f"{fc.provenance.get('max_source_available_at')}",
        f"horizon {len(ctx.gameweeks)} gameweeks (GW{ctx.gameweeks[0]}–GW{ctx.gameweeks[-1]}); "
        f"objective profile '{cfg.profile}' ({cfg.config_ref})",
        f"{fc.provenance.get('n_simulations')} joint simulations (seed "
        f"{fc.provenance.get('simulation_seed')}); gains are paired per sample",
        "prices held at current values inside the horizon (price risk shown separately)",
        "live availability (FPL status / chance of playing) enters through an uncalibrated "
        "adjustment layer",
        f"recommendation thresholds: E[gain] ≥ {cfg.decision.min_gain} pts and "
        f"P(gain > 0) ≥ {cfg.decision.min_prob_positive}",
    ]
    model_versions = {k: str(v) for k, v in fc.provenance.get("model_versions", {}).items()}
    model_versions["ruleset"] = ctx.ruleset.ruleset_version
    explanation = DecisionExplanation(
        action=chosen.action,
        decision=_decision_text(chosen, ctx.names),
        expected_effect={
            "gain_1gw": chosen.gain_1gw.mean,
            "gain_horizon": chosen.gain_horizon.mean,
            "objective_gain": chosen.objective_gain_vs_hold,
        },
        primary_drivers=[d.model_dump() for d in primary_drivers(evidence)],
        supporting_evidence=[e.evidence_id for e in evidence],
        constraints_binding=_binding_constraints(ctx, sol, prob),
        downside_scenarios=downside[:4],
        hold_counterfactual={
            "expected_points_gw": float(hold_s[:, 0].mean()),
            "expected_points_horizon": float(hold_s.sum(axis=1).mean()),
            "timeline": [st.model_dump() for st in hold.timeline],
        },
        alternatives=[
            {
                "label": s.label,
                "action": s.action,
                "sells": s.sells,
                "buys": s.buys,
                "chip": s.chip,
                "gain_horizon": s.gain_horizon.mean,
                "probability_positive": s.gain_horizon.probability_positive,
                "objective_gain": s.objective_gain_vs_hold,
            }
            for s in ranked
            if s.label != chosen.label
        ],
        confidence={
            "probability_beats_hold": chosen.gain_horizon.probability_positive,
            "gain_p10": chosen.gain_horizon.p10,
            "gain_p90": chosen.gain_horizon.p90,
        },
        refusal_reasons=refusal if chosen.label == "hold" else [],
        assumptions=assumptions,
        data_timestamp=str(fc.provenance.get("max_source_available_at")),
        model_versions=model_versions,
        optimizer_run_id=run_id,
    )
    decision = {
        "action": chosen.action,
        "transfers_out": chosen.sells,
        "transfers_in": chosen.buys,
        "chip": chosen.chip,
        "expected_points": float(first_pts.mean()),
        "expected_gain_vs_hold": chosen.gain_horizon.mean,
        "p10": float(np.percentile(first_pts, 10)),
        "p50": float(np.percentile(first_pts, 50)),
        "p90": float(np.percentile(first_pts, 90)),
        "confidence": chosen.gain_horizon.probability_positive if chosen.label != "hold" else None,
        "stability": stab.label if stab else None,
    }
    lineup = {
        "starters": list(first.lineup.starters),
        "bench": list(first.lineup.bench),
        "captain": first.lineup.captain,
        "vice_captain": first.lineup.vice_captain,
    }
    timings["total"] = time.perf_counter() - t0
    decision_id = short_id("dec", {"run": run_id, "action": decision["action"]})
    return RecommendationPackage(
        decision_id=decision_id,
        season=ctx.state.season,
        gameweek=gw0,
        snapshot_id=snapshot,
        ruleset_version=ctx.ruleset.ruleset_version,
        decision=decision,
        chosen=chosen,
        hold=hold,
        alternatives=[s for s in ranked if s.label != chosen.label],
        lineup=lineup,
        captaincy=captaincy,
        evidence=evidence,
        explanation=explanation,
        scenarios=scen_out,
        stability=stab,
        chips=chip_plans,
        assumptions=assumptions,
        generated_at=datetime.now(UTC).isoformat(),
        model_versions=model_versions,
        optimizer_run_id=run_id,
        config_refs={
            "optimizer": cfg.config_ref or cfg.profile,
            **{
                k: str(v)
                for k, v in fc.provenance.get("model_versions", {}).items()
                if k.startswith("config_")
            },
        },
        timings=timings,
    )


def _decision_text(s: OptionSummary, names: dict[int, str] | None) -> str:
    def n(c: int) -> str:
        return (names or {}).get(c, str(c))

    if s.action == "HOLD":
        return "Hold: make no transfer this gameweek (bank the free transfer)."
    parts = []
    if s.sells or s.buys:
        parts.append(
            "Transfer " + ", ".join(n(c) for c in s.sells) + " → " + ", ".join(n(c) for c in s.buys)
        )
    if s.chip:
        parts.append(f"play {s.chip}")
    return "; ".join(parts) + "."


def _run_scenario(
    ctx: DecisionContext,
    prob: OptimizationProblem,
    spec: ScenarioSpec,
    sol: Solution,
    options: list[PlanOption],
    positions: dict[int, Position],
) -> ScenarioOutcome:
    hold_opt = next(o for o in options if o.label == "hold")
    if spec.kind == "expected":
        a = plan_samples(ctx.forecast.simulation, sol.plans, positions, ctx.ruleset)
        b = plan_samples(ctx.forecast.simulation, hold_opt.solution.plans, positions, ctx.ruleset)
        return ScenarioOutcome(
            name=spec.name,
            kind=spec.kind,
            description=spec.description,
            feasible=True,
            gain_vs_hold=GainSummary.of(paired_gain(a, b)),
            objective_gain_vs_hold=sol.objective - hold_opt.solution.objective,
            recommended_still_best=True,
        )
    if spec.kind in SIMULATION_KINDS:
        fc2 = perturb_forecast(ctx.forecast, spec)
        a = plan_samples(fc2.simulation, sol.plans, positions, ctx.ruleset)
        b = plan_samples(fc2.simulation, hold_opt.solution.plans, positions, ctx.ruleset)
        g = paired_gain(a, b)
        # did another considered plan overtake the recommendation under this scenario?
        best_other = max(
            (
                paired_gain(
                    plan_samples(fc2.simulation, o.solution.plans, positions, ctx.ruleset), b
                ).mean
                for o in options
                if o.solution is not sol
            ),
            default=float("-inf"),
        )
        return ScenarioOutcome(
            name=spec.name,
            kind=spec.kind,
            description=spec.description,
            feasible=True,
            gain_vs_hold=GainSummary.of(g),
            recommended_still_best=bool(g.mean >= best_other),
            note=f"simulation re-run with perturbed inputs (paired, {fc2.provenance['scenario']})",
        )
    table = perturb_table(prob.players, spec)
    p2 = replace(prob, players=table)
    try:
        best = build_and_solve(p2)
        hold2 = build_and_solve(
            replace(p2, preferences=replace(p2.preferences, hold_first_gw=True))
        )
    except OptimizationError as exc:
        return ScenarioOutcome(
            name=spec.name,
            kind=spec.kind,
            description=spec.description,
            feasible=False,
            note=str(exc),
        )
    sells, buys = sol.first_action()
    if not sells and not buys:
        mine = hold2
    else:
        try:
            mine = build_and_solve(
                replace(
                    p2,
                    preferences=replace(
                        p2.preferences, forced_out=sells, forced_in=buys, hold_first_gw=False
                    ),
                )
            )
        except OptimizationError as exc:
            return ScenarioOutcome(
                name=spec.name,
                kind=spec.kind,
                description=spec.description,
                feasible=False,
                note=f"recommended move no longer feasible: {exc}",
            )
    return ScenarioOutcome(
        name=spec.name,
        kind=spec.kind,
        description=spec.description,
        feasible=True,
        objective_gain_vs_hold=mine.objective - hold2.objective,
        recommended_still_best=bool(mine.objective >= best.objective - 1e-6),
        note="re-optimised with perturbed table (objective units)",
    )
