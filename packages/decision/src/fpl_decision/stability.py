"""Recommendation stability under forecast perturbation (§29, ADR-0007).

The expected points fed to the optimiser are estimates. Each perturbation multiplies every
player's EV by a log-normal factor (a player-level shift for the horizon plus per-gameweek
noise) — "perturb forecasts within their confidence intervals" — and re-optimises. For each
perturbation we record (a) whether the same first-gameweek action is optimal and (b) whether the
recommended action stays *near-optimal*: its forced objective is within ``tolerance`` points of
the perturbed optimum. A recommendation is **stable** if (b) holds in ≥ ``stable_share`` of the
perturbations, otherwise **fragile** — a measured label, not an intuition.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace

import numpy as np

from fpl_optimizer.milp import OptimizationError, build_and_solve
from fpl_optimizer.problem import OptimizationProblem


@dataclass(frozen=True)
class StabilityResult:
    perturbations: int
    share_same_action: float
    share_near_optimal: float
    label: str  # "stable" | "fragile"
    competing_actions: list[tuple[tuple[int, ...], tuple[int, ...], int]]  # (sells, buys, count)
    mean_regret: float  # mean objective shortfall of the recommended action


def stability(
    prob: OptimizationProblem, sells: frozenset[int], buys: frozenset[int]
) -> StabilityResult:
    st = prob.config.stability
    table = prob.players
    same = near = 0
    regrets = []
    competitors: Counter[tuple[tuple[int, ...], tuple[int, ...]]] = Counter()
    hold = not sells and not buys
    for k in range(st.perturbations):
        rng = np.random.default_rng([st.seed, k])
        level = rng.normal(0.0, st.relative_sd, (table.n, 1))
        weekly = rng.normal(0.0, st.weekly_sd, table.ev.shape)
        factor = np.exp(level + weekly - 0.5 * (st.relative_sd**2 + st.weekly_sd**2))
        ev = table.ev * factor
        q10 = None if table.q10 is None else np.minimum(table.q10 * factor, ev)
        p = replace(prob, players=replace(table, ev=ev, q10=q10))
        best = build_and_solve(p)
        action = best.first_action()
        if action == (sells, buys):
            same += 1
            near += 1
            regrets.append(0.0)
            continue
        competitors[(tuple(sorted(action[0])), tuple(sorted(action[1])))] += 1
        forced = replace(
            p,
            preferences=replace(
                p.preferences,
                hold_first_gw=hold,
                forced_out=frozenset() if hold else sells,
                forced_in=frozenset() if hold else buys,
            ),
        )
        try:
            mine = build_and_solve(forced).objective
        except OptimizationError:  # e.g. unaffordable after a perturbation — not near-optimal
            regrets.append(float("inf"))
            continue
        gap = best.objective - mine
        regrets.append(gap)
        near += int(gap <= st.tolerance)
    n = st.perturbations
    finite = [r for r in regrets if np.isfinite(r)]
    return StabilityResult(
        perturbations=n,
        share_same_action=same / n,
        share_near_optimal=near / n,
        label="stable" if near / n >= st.stable_share else "fragile",
        competing_actions=[(s, b, c) for (s, b), c in competitors.most_common(5)],
        mean_regret=float(np.mean(finite)) if finite else float("nan"),
    )
