"""Independent validation of optimiser output (ADR-0007, §91).

The MILP is never trusted on its own. Every solution is

1. converted to domain ``GameweekDecision`` objects and replayed through the rules state
   machine (``fpl_domain.validation.validate_plan``): squad legality each GW, budget with selling
   prices, free-transfer accounting, hits, chip windows, Free Hit reversion, lineup formation;
2. re-scored gameweek by gameweek with the exact lineup solver, and the objective recomputed
   from the replayed state (hits from the state machine, not from the MILP's ``h`` variables).

A solution is accepted only if the replay is valid, the replayed hits / bank / free transfers
equal the MILP's, and the MILP lineup objective is within tolerance of the exact optimum for the
chosen squad (it must be, since the lineup sub-problem is part of the same MILP).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fpl_domain.enums import Position
from fpl_domain.squad import SquadPick, lineup_violations, squad_violations
from fpl_domain.state import GameweekDecision, ManagerState, Transfer
from fpl_domain.validation import ValidationReport, validate_plan
from fpl_optimizer.lineup import best_lineup
from fpl_optimizer.milp import Solution, season_ends
from fpl_optimizer.problem import POS_INDEX, OptimizationProblem


@dataclass
class ValidationResult:
    valid: bool
    issues: list[str]
    replay: ValidationReport
    recomputed_objective: float
    lineup_gaps: list[float] = field(default_factory=list)


def to_decisions(
    prob: OptimizationProblem, sol: Solution, start: int = 0
) -> list[GameweekDecision]:
    """Domain decisions for plans[start:] (initial-squad mode starts at 1: GW0 is a selection)."""
    pl = prob.players
    idx = pl.index()
    pos = pl.positions_map()
    out: list[GameweekDecision] = []
    for plan in sol.plans[start:]:
        outs = sorted(plan.transfers_out, key=lambda c: (POS_INDEX[pos[c]], c))
        ins = sorted(plan.transfers_in, key=lambda c: (POS_INDEX[pos[c]], c))
        transfers = tuple(
            Transfer(
                out_code=o,
                in_code=i,
                in_position=pos[i],
                in_team_code=int(pl.team[idx[i]]),
                in_price=int(pl.price[idx[i]]),
            )
            for o, i in zip(outs, ins, strict=True)
        )
        out.append(GameweekDecision(transfers=transfers, chip_id=plan.chip_id, lineup=plan.lineup))
    return out


def gameweek_value(
    prob: OptimizationProblem, t: int, codes: tuple[int, ...], chip_type: object
) -> tuple[float, float]:
    """Exact best lineup value (and EV) of ``codes`` in horizon GW ``t``."""
    pl = prob.players
    idx = pl.index()
    pos: dict[int, Position] = pl.positions_map()
    w = prob.config.objective
    down = pl.downside()
    ev = {c: float(pl.ev[idx[c], t]) for c in codes}
    val = {c: float(pl.ev[idx[c], t] - w.risk_aversion * down[idx[c], t]) for c in codes}
    ch = best_lineup(codes, pos, ev, val, w, prob.ruleset, chip_type)  # type: ignore[arg-type]
    return ch.value, ch.expected_points


def _initial_squad_check(
    prob: OptimizationProblem, sol: Solution
) -> tuple[list[str], ManagerState | None]:
    """Initial-squad mode: GW0 is a squad *selection* (no transfers to replay)."""
    pl, rs = prob.players, prob.ruleset
    idx = pl.index()
    pos = pl.positions_map()
    plan = sol.plans[0]
    picks = [
        SquadPick(
            player_code=c,
            position=pos[c],
            team_code=int(pl.team[idx[c]]),
            purchase_price=int(pl.price[idx[c]]),
        )
        for c in plan.squad
    ]
    cost = sum(p.purchase_price for p in picks)
    bank = prob.state.bank - cost
    issues = [str(v) for v in squad_violations(picks, rs, bank)]
    issues += [str(v) for v in lineup_violations(plan.lineup, plan.squad, pos, rs)]
    if bank != plan.bank_after:
        issues.append(f"initial bank {plan.bank_after} vs recomputed {bank}")
    if issues or len(sol.plans) == 1:
        return issues, None
    nxt = prob.state.model_copy(
        update={
            "gameweek": prob.gameweeks[1],
            "squad": tuple(picks),
            "bank": bank,
            "free_transfers": sol.plans[1].free_transfers,
            "transfers_made": 0,
            "hit_points_taken": 0,
            "lineup": plan.lineup,
        }
    )
    return issues, nxt


def validate_solution(
    prob: OptimizationProblem, sol: Solution, tol: float = 1e-5
) -> ValidationResult:
    issues: list[str] = []
    decisions = to_decisions(prob, sol, start=1 if prob.initial_squad_mode else 0)
    prices = {int(c): int(p) for c, p in zip(prob.players.code, prob.players.price, strict=True)}
    offset = 0
    state: ManagerState = prob.state
    if prob.initial_squad_mode:
        init_issues, nxt = _initial_squad_check(prob, sol)
        if init_issues:
            empty = ValidationReport(
                valid=False, violations=tuple(init_issues), steps=(), final_state=None
            )
            return ValidationResult(False, init_issues, empty, float("nan"))
        offset = 1
        if nxt is None:
            replay = ValidationReport(valid=True, violations=(), steps=(), final_state=None)
        else:
            state = nxt
            replay = validate_plan(state, decisions, prices, prob.ruleset)
    else:
        replay = validate_plan(state, decisions, prices, prob.ruleset)
    if not replay.valid:
        return ValidationResult(False, list(replay.violations), replay, float("nan"))
    gaps: list[float] = []
    for t, plan in enumerate(sol.plans):
        best_v, _ = gameweek_value(prob, t, plan.playing_squad, plan.chip_type)
        chosen_v = _lineup_value(prob, t, plan)
        gaps.append(best_v - chosen_v)
        if best_v - chosen_v > tol * max(1.0, abs(best_v)):
            issues.append(f"GW{plan.gameweek}: lineup suboptimal by {best_v - chosen_v:.6f}")
        is_fh = plan.chip_type is not None and plan.chip_type.value == "free_hit"
        if t < offset:
            continue
        step = replay.steps[t - offset]
        expected_hits = plan.hit_points + (prob.state.hit_points_taken if t == 0 else 0)
        if step.hit_points != expected_hits:
            issues.append(f"GW{plan.gameweek}: hits {expected_hits} vs replay {step.hit_points}")
        # in a Free Hit week the replay reports the temporary squad's bank; the plan reports
        # the persistent bank, which the next step's replay verifies after reversion
        if not is_fh and step.bank_after != plan.bank_after:
            issues.append(f"GW{plan.gameweek}: bank {plan.bank_after} vs replay {step.bank_after}")
        if step.free_transfers_at_start != plan.free_transfers:
            issues.append(
                f"GW{plan.gameweek}: FT {plan.free_transfers} vs replay "
                f"{step.free_transfers_at_start}"
            )
    final = replay.final_state
    terminal: tuple[int, int, tuple[int, ...] | None] | None = None
    if final is not None:
        if final.free_transfers != sol.free_transfers_end:
            issues.append(f"end FT {sol.free_transfers_end} vs replay {final.free_transfers}")
        terminal = (final.free_transfers, final.bank, tuple(final.codes))
    elif prob.initial_squad_mode and len(sol.plans) == 1 and not season_ends(prob):
        terminal = (sol.free_transfers_end, sol.plans[0].bank_after, None)
    total = plan_objective(prob, sol, terminal)
    if abs(total - sol.objective) > 1e-4 * max(1.0, abs(total)):
        issues.append(f"objective {sol.objective:.6f} vs recomputed {total:.6f}")
    return ValidationResult(not issues, issues, replay, total, gaps)


def plan_objective(
    prob: OptimizationProblem,
    sol: Solution,
    terminal: tuple[int, int, tuple[int, ...] | None] | None,
) -> float:
    """Exact objective of a plan (the MILP's objective, recomputed from the decisions):
    discounted lineup values minus hits and churn penalties, plus terminal values of the free
    transfers, bank and squad left after the horizon (``terminal``; none after the season's
    last gameweek), minus the opportunity value of chips played."""
    w = prob.config.objective
    hit_cost = prob.ruleset.transfers.hit_cost
    total = 0.0
    for t, plan in enumerate(sol.plans):
        d = w.discount**t
        is_fh = plan.chip_type is not None and plan.chip_type.value == "free_hit"
        total += d * (_lineup_value(prob, t, plan) - hit_cost * plan.paid_transfers)
        if not is_fh and not (prob.initial_squad_mode and t == 0):
            total -= d * w.transfer_penalty * len(plan.transfers_in)
    if terminal is not None:
        ft, bank, codes = terminal
        total += w.free_transfer_value * ft + w.bank_value_per_tenth * bank
        if codes is not None and w.terminal_squad_weight:
            pl = prob.players
            idx = pl.index()
            total += w.terminal_squad_weight * sum(
                float(pl.ev[idx[c], pl.horizon - 1]) for c in codes
            )
    for plan in sol.plans:
        if plan.chip_type is not None:
            total -= w.chip_values.get(plan.chip_type.value, 0.0)
    return total


def _lineup_value(prob: OptimizationProblem, t: int, plan) -> float:  # type: ignore[no-untyped-def]
    """The per-GW objective of the MILP's own lineup (same formula as the lineup solver)."""
    pl = prob.players
    idx = pl.index()
    w = prob.config.objective
    rs = prob.ruleset
    down = pl.downside()

    def a(c: int) -> float:
        return float(pl.ev[idx[c], t] - w.risk_aversion * down[idx[c], t])

    def ev(c: int) -> float:
        return float(pl.ev[idx[c], t])

    lu = plan.lineup
    ct = plan.chip_type.value if plan.chip_type is not None else None
    mult = (
        rs.chips.triple_captain_multiplier
        if ct == "triple_captain"
        else (rs.captaincy.captain_multiplier)
    )
    v = (
        sum(a(c) for c in lu.starters)
        + (mult - 1) * a(lu.captain)
        + w.vice_weight * a(lu.vice_captain)
    )
    gk_bench = [c for c in lu.bench if prob.players.positions_map()[c] is Position.GK]
    out_bench = [c for c in lu.bench if c not in gk_bench]
    if ct == "bench_boost":
        v += sum(a(c) for c in lu.bench)
    else:
        v += sum(wk * ev(c) for wk, c in zip(w.bench_weights, out_bench, strict=False))
        v += w.bench_gk_weight * sum(ev(c) for c in gk_bench)
    return v
