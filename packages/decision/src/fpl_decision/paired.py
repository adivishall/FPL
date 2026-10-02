"""Paired Monte Carlo comparison of plans on the joint simulation (ADR-0007, ADR-0001 #4).

Every plan (a sequence of gameweek lineups with chips and hits) is scored on the *same* simulated
samples, so the difference between two plans is computed sample by sample: this removes the
shared noise (a blank gameweek for a player both plans own cancels out) and gives a proper
distribution of the gain — mean, quantiles and P(gain > 0) — rather than a difference of means.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from fpl_domain.enums import Position
from fpl_domain.rules import Ruleset
from fpl_optimizer.milp import GameweekPlan
from fpl_simulation.engine import SimulationResult
from fpl_simulation.lineup_eval import score_lineup

F = npt.NDArray[np.float64]


def _samples(
    sim: SimulationResult, code: int, gameweek: int
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
    gi = sim.gw_index(gameweek)
    if gi >= len(sim.gameweeks) or sim.gameweeks[gi] != gameweek:
        raise KeyError(f"gameweek {gameweek} not simulated")
    try:
        pi = sim.index_of(code)
    except KeyError:  # no fixture rows at all in the simulated window → never plays
        z = np.zeros(sim.n_sims, dtype=np.int64)
        return z, z
    return sim.points[:, pi, gi].astype(np.int64), sim.minutes[:, pi, gi].astype(np.int64)


def plan_samples(
    sim: SimulationResult,
    plans: Sequence[GameweekPlan],
    positions: Mapping[int, Position],
    ruleset: Ruleset,
) -> F:
    """[S, G] points per sample and plan gameweek, net of the plan's hit points."""
    out = np.zeros((sim.n_sims, len(plans)))
    for t, plan in enumerate(plans):
        pts, mins = {}, {}
        for c in plan.lineup.players:
            pts[c], mins[c] = _samples(sim, c, plan.gameweek)
        total = score_lineup(plan.lineup, positions, pts, mins, ruleset, plan.chip_type)
        out[:, t] = total - plan.hit_points
    return out


@dataclass(frozen=True)
class PairedGain:
    mean: float
    p10: float
    p50: float
    p90: float
    probability_positive: float
    probability_negative: float
    n_samples: int

    @classmethod
    def from_diff(cls, diff: F) -> PairedGain:
        return cls(
            mean=float(diff.mean()),
            p10=float(np.percentile(diff, 10)),
            p50=float(np.percentile(diff, 50)),
            p90=float(np.percentile(diff, 90)),
            probability_positive=float((diff > 0).mean()),
            probability_negative=float((diff < 0).mean()),
            n_samples=len(diff),
        )


def paired_gain(a: F, b: F, gameweeks: slice | None = None) -> PairedGain:
    """Gain of plan ``a`` over plan ``b`` (both [S, G]) summed over the selected gameweeks."""
    sl = gameweeks or slice(None)
    return PairedGain.from_diff(a[:, sl].sum(axis=1) - b[:, sl].sum(axis=1))


def confidence_label(p_positive: float, high: float = 0.75, medium: float = 0.6) -> str:
    """Confidence = paired probability that the plan beats the counterfactual (ADR-0001 #4)."""
    if p_positive >= high:
        return "high"
    if p_positive >= medium:
        return "medium"
    return "low"
