"""Replacement engine — the flagship workflow (§17, §60).

Given the manager's actual squad and a player to replace (A):

1. **Candidate universe** (§60.1): every player of A's position not already owned, not banned,
   affordable for a direct swap with A's *selling* price plus the bank, and legal under the club
   limit after the swap. Nothing else is pruned at this stage.
2. **Screen** every candidate B exactly but myopically: rebuild the squad with A → B, re-solve the
   best lineup (XI, bench order, armbands) for each horizon gameweek with the exact lineup solver,
   and score the discounted gain vs keeping A (hit cost included when no free transfer is left).
3. **Shortlist**: the best screened candidates, plus — so the engine is not biased toward template
   picks or expensive options (§60.1) — the highest-upside candidates (P(≥10) over the horizon) and
   the best cheap enablers (lowest price among positive-gain candidates), whose freed budget can
   fund other moves.
4. **Re-optimise** each shortlisted move with the full multi-gameweek MILP (A out / B in forced in
   the first gameweek, everything after optimal), independently validated, and compare with the
   explicit HOLD plan (§18) — capturing knock-on moves, future transfer burden and bench effects.
5. **Paired Monte Carlo** gains vs HOLD on the joint simulation: 1-GW and horizon mean, p10/p50/
   p90 and P(gain > 0) (§60.3); confidence = P(gain > 0) bands.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pandas as pd

from fpl_decision.paired import PairedGain, confidence_label, paired_gain, plan_samples
from fpl_domain.enums import ChipType
from fpl_optimizer.alternatives import hold_problem
from fpl_optimizer.milp import OptimizationError, Solution, build_and_solve
from fpl_optimizer.parallel import map_ordered
from fpl_optimizer.pool import candidate_pool
from fpl_optimizer.problem import OptimizationProblem
from fpl_optimizer.validate import gameweek_value, validate_solution
from fpl_simulation.engine import SimulationResult


@dataclass
class ReplacementCandidate:
    out_code: int
    in_code: int
    action: str  # "TRANSFER" (free) or "HIT"
    screen_gain: float  # myopic discounted lineup gain (exact lineups, no further moves)
    objective_gain: float  # optimiser objective vs HOLD with optimal follow-up moves
    gain_1gw: PairedGain
    gain_horizon: PairedGain
    transfer_cost: int  # hit points in the first gameweek
    confidence: str
    price: int
    bank_after: int
    start_probability: float | None
    upside_probability: float | None  # Σ_t P(points ≥ 10) over the horizon
    follow_up: list[dict[str, Any]]  # later moves in the optimal plan
    selection_reason: str  # why the candidate was shortlisted
    solution: Solution
    valid: bool

    def contract(self, hold_id: str) -> dict[str, Any]:
        """The §60.3 replacement output contract."""
        return {
            "out": {"player_id": self.out_code},
            "in": {"player_id": self.in_code},
            "action": self.action,
            "expected_gain_1gw": round(self.gain_1gw.mean, 3),
            "expected_gain_horizon": round(self.gain_horizon.mean, 3),
            "probability_positive": round(self.gain_horizon.probability_positive, 3),
            "p10_gain": self.gain_horizon.p10,
            "p50_gain": self.gain_horizon.p50,
            "p90_gain": self.gain_horizon.p90,
            "transfer_cost": self.transfer_cost,
            "confidence": self.confidence,
            "counterfactual_id": hold_id,
        }


@dataclass
class ReplacementResult:
    out_code: int
    universe_size: int
    screened: pd.DataFrame
    candidates: list[ReplacementCandidate]
    hold: Solution
    hold_samples_mean: float
    notes: list[str] = field(default_factory=list)


def _affordable_universe(prob: OptimizationProblem, out_code: int) -> list[int]:
    pl, st, rs = prob.players, prob.state, prob.ruleset
    idx = pl.index()
    prices = {int(c): int(p) for c, p in zip(pl.code, pl.price, strict=True)}
    budget = st.bank + st.selling_values(prices, rs)[out_code]
    pos = pl.position[idx[out_code]]
    owned = set(st.codes)
    clubs: dict[int, int] = {}
    for p in st.squad:
        if p.player_code != out_code:
            clubs[p.team_code] = clubs.get(p.team_code, 0) + 1
    out = []
    for i, c in enumerate(pl.code.tolist()):
        if c in owned or c in prob.preferences.banned or pl.position[i] != pos:
            continue
        if pl.price[i] > budget:
            continue
        if clubs.get(int(pl.team[i]), 0) + 1 > rs.squad.max_per_club:
            continue
        out.append(c)
    return out


def _solve(p: OptimizationProblem) -> Solution | str:
    """The plan, or why it is infeasible (a picklable result for worker processes)."""
    try:
        return build_and_solve(p)
    except OptimizationError as exc:
        return str(exc)


def _screen(prob: OptimizationProblem, out_code: int, universe: list[int]) -> pd.DataFrame:
    w = prob.config.objective
    base_codes = tuple(prob.state.codes)
    horizon = len(prob.gameweeks)
    disc = [w.discount**t for t in range(horizon)]
    hold_v = [gameweek_value(prob, t, base_codes, None)[0] for t in range(horizon)]
    paid = 1 if prob.state.free_transfers - prob.state.transfers_made < 1 else 0
    hit = prob.ruleset.transfers.hit_cost * paid
    rows = []
    for c in universe:
        squad = (*(x for x in base_codes if x != out_code), c)
        vals = [gameweek_value(prob, t, squad, None)[0] for t in range(horizon)]
        gain = sum(d * (v - h) for d, v, h in zip(disc, vals, hold_v, strict=True)) - hit
        rows.append({"in_code": c, "screen_gain": gain, "gain_gw0": vals[0] - hold_v[0] - hit})
    return pd.DataFrame(rows).sort_values("screen_gain", ascending=False).reset_index(drop=True)


def find_replacements(
    prob: OptimizationProblem,
    out_code: int,
    sim: SimulationResult,
    summary: pd.DataFrame | None = None,
    n_screen: int = 8,
    n_upside: int = 2,
    n_enablers: int = 2,
    n_return: int = 5,
    use_pool: bool = True,
    workers: int = 1,
) -> ReplacementResult:
    """``workers`` solves HOLD and the shortlisted moves in parallel processes; the result does
    not depend on it."""
    if out_code not in prob.state.codes:
        raise ValueError(f"{out_code} is not in the squad")
    notes: list[str] = []
    universe = _affordable_universe(prob, out_code)
    if not universe:
        raise OptimizationError("no affordable, club-legal replacement exists")
    screened = _screen(prob, out_code, universe)
    upside = pd.Series(dtype=float)
    start_p = pd.Series(dtype=float)
    if summary is not None and len(summary):
        gws = set(prob.gameweeks)
        s = summary[summary["gw"].isin(gws)]
        upside = s.groupby("player_code")["prob_10_plus"].sum()
        first = s[s["gw"] == prob.gameweeks[0]].set_index("player_code")
        start_p = first.get("prob_start", start_p)
    screened["upside"] = screened["in_code"].map(upside)
    price = dict(zip(prob.players.code.tolist(), prob.players.price.tolist(), strict=True))
    screened["price"] = screened["in_code"].map(price)
    picks: dict[int, str] = {}
    for c in screened["in_code"].head(n_screen):
        picks[int(c)] = "top screened gain"
    if summary is not None and screened["upside"].notna().any():
        for c in screened.sort_values("upside", ascending=False)["in_code"].head(n_upside):
            picks.setdefault(int(c), "high upside (P(≥10) over horizon)")
    positive = screened[screened["screen_gain"] > 0]
    for c in positive.sort_values(["price", "screen_gain"], ascending=[True, False])[
        "in_code"
    ].head(n_enablers):
        picks.setdefault(int(c), "cheap enabler (frees budget)")

    base = prob
    if use_pool:
        pooled = candidate_pool(
            prob.players,
            prob.state.codes,
            prob.config.pool,
            must_include=list(picks),
            discount=prob.config.objective.discount,
        )
        base = replace(prob, players=pooled)
    hold_prob = hold_problem(base)
    moves = {
        c: replace(
            base,
            preferences=replace(
                base.preferences,
                forced_out=frozenset({out_code}),
                forced_in=frozenset({c}),
                hold_first_gw=False,
            ),
        )
        for c in picks
    }
    hold_r, *move_r = map_ordered(_solve, [hold_prob, *moves.values()], workers)
    if isinstance(hold_r, str):
        raise OptimizationError(hold_r)
    hold = hold_r
    if not validate_solution(hold_prob, hold).valid:
        notes.append("HOLD plan failed validation")
    positions = prob.players.positions_map()
    hold_s = plan_samples(sim, hold.plans, positions, prob.ruleset)
    # paired gains are reported in undiscounted points; the optimiser objective is discounted
    thresholds = prob.config.decision
    cands: list[ReplacementCandidate] = []
    for (c, reason), (p, sol) in zip(
        picks.items(), zip(moves.values(), move_r, strict=True), strict=True
    ):
        if isinstance(sol, str):
            notes.append(f"{out_code}->{c}: infeasible ({sol})")
            continue
        val = validate_solution(p, sol)
        samples = plan_samples(sim, sol.plans, positions, prob.ruleset)
        g1 = paired_gain(samples, hold_s, slice(0, 1))
        gh = paired_gain(samples, hold_s)
        first = sol.plans[0]
        follow = [
            {
                "gameweek": pl.gameweek,
                "out": list(pl.transfers_out),
                "in": list(pl.transfers_in),
                "chip": pl.chip_id,
                "hit_points": pl.hit_points,
            }
            for pl in sol.plans[1:]
            if pl.transfers_in or pl.chip_id
        ]
        extra = set(first.transfers_in) - {c}
        if extra:
            follow.insert(
                0,
                {
                    "gameweek": first.gameweek,
                    "out": sorted(set(first.transfers_out) - {out_code}),
                    "in": sorted(extra),
                    "chip": first.chip_id,
                    "hit_points": first.hit_points,
                },
            )
        cands.append(
            ReplacementCandidate(
                out_code=out_code,
                in_code=c,
                action="HIT" if first.hit_points > 0 else "TRANSFER",
                screen_gain=float(screened.loc[screened["in_code"] == c, "screen_gain"].iloc[0]),
                objective_gain=sol.objective - hold.objective,
                gain_1gw=g1,
                gain_horizon=gh,
                transfer_cost=first.hit_points,
                confidence=confidence_label(
                    gh.probability_positive, medium=thresholds.min_prob_positive
                ),
                price=int(price[c]),
                bank_after=first.bank_after,
                start_probability=float(start_p[c]) if c in start_p.index else None,
                upside_probability=float(upside[c]) if c in upside.index else None,
                follow_up=follow,
                selection_reason=reason,
                solution=sol,
                valid=val.valid,
            )
        )
        if first.chip_type is ChipType.FREE_HIT:
            notes.append(f"{out_code}->{c}: plan uses a Free Hit in the first gameweek")
    cands.sort(key=lambda r: (-r.objective_gain, -r.gain_horizon.mean))
    return ReplacementResult(
        out_code=out_code,
        universe_size=len(universe),
        screened=screened,
        candidates=cands[:n_return],
        hold=hold,
        hold_samples_mean=float(hold_s.sum(axis=1).mean()),
        notes=notes,
    )
