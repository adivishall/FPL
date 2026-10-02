"""Chip planner (§21, §64): chips as finite resources with windows and opportunity cost.

For every available chip instance and every horizon gameweek inside its window, the uplift of
playing it *on top of the chosen transfer plan* is measured:

* **Triple Captain / Bench Boost** — the plan's lineup for that gameweek is re-scored on the joint
  samples with and without the chip (paired), giving the full uplift distribution (lineup and
  minutes uncertainty included). Sensitivity: the uplift when the main contributor's fixture is
  postponed (re-simulated).
* **Wildcard / Free Hit** — the MILP is re-solved with the chip forced in that gameweek (transfers
  re-optimised around it, Free Hit reverting) and the resulting plan compared with the base plan
  on the samples (paired) and in objective units.

Recommendation per chip: *play now* only if this gameweek is the best in-window week of the
horizon **and** its uplift is positive with P(uplift > 0) ≥ the profile threshold; otherwise
*wait* (naming the better week and the value of waiting) or *keep*. When the window ends inside
the horizon, the expiring chip's best week is shown. A deadline alone is never a reason (§64).
Beyond the horizon no value is invented: the report states the horizon limit explicitly.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
from pydantic import BaseModel

from fpl_decision.paired import PairedGain, paired_gain, plan_samples
from fpl_decision.scenarios import ScenarioSpec, perturb_forecast
from fpl_domain.enums import ChipType, Position
from fpl_domain.rules import Ruleset
from fpl_domain.squad import Lineup
from fpl_optimizer.milp import OptimizationError, Solution, build_and_solve
from fpl_optimizer.problem import OptimizationProblem
from fpl_simulation.engine import SimulationResult
from fpl_simulation.lineup_eval import score_lineup


class ChipWeekValue(BaseModel):
    gameweek: int
    uplift_mean: float
    uplift_p10: float
    uplift_p90: float
    probability_positive: float
    objective_gain: float | None = None
    method: str
    fixture_shock_uplift: float | None = None


class ChipPlan(BaseModel):
    chip_id: str
    chip_type: str
    window: tuple[int, int]
    expires_in_horizon: bool
    by_gameweek: list[ChipWeekValue]
    best_gameweek: int | None
    value_now: float | None
    best_value: float | None
    value_of_waiting: float | None
    recommendation: str  # "play now" | "wait" | "keep"
    reason: str


def _lineup_samples(
    sim: SimulationResult,
    lineup: Lineup,
    gw: int,
    positions: dict[int, Position],
    ruleset: Ruleset,
    chip: ChipType | None,
) -> np.ndarray:
    gi = sim.gw_index(gw)
    have = set(sim.player_codes.tolist())
    pts, mins = {}, {}
    for c in lineup.players:
        if c in have:
            pi = sim.index_of(c)
            pts[c] = sim.points[:, pi, gi].astype(np.int64)
            mins[c] = sim.minutes[:, pi, gi].astype(np.int64)
        else:
            pts[c] = mins[c] = np.zeros(sim.n_sims, dtype=np.int64)
    return score_lineup(lineup, positions, pts, mins, ruleset, chip).astype(float)


def plan_chips(
    prob: OptimizationProblem,
    base: Solution,
    forecast: Any,  # fpl_forecasting.pipeline.Forecast
    min_prob_positive: float = 0.6,
    sensitivity: bool = True,
) -> list[ChipPlan]:
    """``sensitivity=False`` skips the fixture-postponement re-simulation (backtests)."""
    rs = prob.ruleset
    sim = forecast.simulation
    positions = prob.players.positions_map()
    gws = prob.gameweeks
    base_s = plan_samples(sim, base.plans, positions, rs)
    out: list[ChipPlan] = []
    for chip in prob.state.chips:
        if chip.status.value != "available" or chip.chip_type is ChipType.ASSISTANT_MANAGER:
            continue
        weeks = [g for g in gws if chip.first_gameweek <= g <= chip.last_gameweek]
        values: list[ChipWeekValue] = []
        for g in weeks:
            t = gws.index(g)
            plan = base.plans[t]
            if plan.chip_id is not None:
                continue  # the base plan already plays a chip that week
            if chip.chip_type in (ChipType.TRIPLE_CAPTAIN, ChipType.BENCH_BOOST):
                with_c = _lineup_samples(sim, plan.lineup, g, positions, rs, chip.chip_type)
                without = _lineup_samples(sim, plan.lineup, g, positions, rs, None)
                d = PairedGain.from_diff(with_c - without)
                shock = None
                if not sensitivity:
                    values.append(
                        ChipWeekValue(
                            gameweek=g,
                            uplift_mean=d.mean,
                            uplift_p10=d.p10,
                            uplift_p90=d.p90,
                            probability_positive=d.probability_positive,
                            method="paired_simulation",
                        )
                    )
                    continue
                top = (
                    plan.lineup.captain
                    if chip.chip_type is ChipType.TRIPLE_CAPTAIN
                    else max(
                        plan.lineup.bench, key=lambda c: prob.players.ev[prob.players.index()[c], t]
                    )
                )
                team = int(prob.players.team[prob.players.index()[top]])
                fc2 = perturb_forecast(
                    forecast,
                    ScenarioSpec(
                        name=f"{chip.chip_id}_gw{g}_postponed",
                        kind="fixture_shock",
                        teams=(team,),
                        gameweeks=(g,),
                    ),
                )
                shock = float(
                    (
                        _lineup_samples(
                            fc2.simulation, plan.lineup, g, positions, rs, chip.chip_type
                        )
                        - _lineup_samples(fc2.simulation, plan.lineup, g, positions, rs, None)
                    ).mean()
                )
                values.append(
                    ChipWeekValue(
                        gameweek=g,
                        uplift_mean=d.mean,
                        uplift_p10=d.p10,
                        uplift_p90=d.p90,
                        probability_positive=d.probability_positive,
                        method="paired_simulation",
                        fixture_shock_uplift=shock,
                    )
                )
            else:
                p2 = replace(
                    prob, forced_chips={chip.chip_id: g}, chip_options={chip.chip_id: (g,)}
                )
                try:
                    sol = build_and_solve(p2)
                except OptimizationError:
                    continue
                s2 = plan_samples(sim, sol.plans, positions, rs)
                d = paired_gain(s2, base_s)
                values.append(
                    ChipWeekValue(
                        gameweek=g,
                        uplift_mean=d.mean,
                        uplift_p10=d.p10,
                        uplift_p90=d.p90,
                        probability_positive=d.probability_positive,
                        objective_gain=sol.objective - base.objective,
                        method="optimizer+paired_simulation",
                    )
                )
        expires = chip.last_gameweek <= gws[-1]
        if not values:
            out.append(
                ChipPlan(
                    chip_id=chip.chip_id,
                    chip_type=chip.chip_type.value,
                    window=(chip.first_gameweek, chip.last_gameweek),
                    expires_in_horizon=expires,
                    by_gameweek=[],
                    best_gameweek=None,
                    value_now=None,
                    best_value=None,
                    value_of_waiting=None,
                    recommendation="keep",
                    reason="not playable inside the planning horizon",
                )
            )
            continue
        best = max(values, key=lambda v: v.uplift_mean)
        now = next((v for v in values if v.gameweek == gws[0]), None)
        v_now = now.uplift_mean if now else None
        waiting = best.uplift_mean - (v_now if v_now is not None else 0.0)
        if (
            now is not None
            and best.gameweek == gws[0]
            and now.uplift_mean > 0
            and now.probability_positive >= min_prob_positive
        ):
            rec = "play now"
            reason = (
                f"GW{gws[0]} is the best week in the horizon: +{now.uplift_mean:.1f} pts "
                f"(P>0 {now.probability_positive:.2f}); later weeks offer at most "
                f"+{max((v.uplift_mean for v in values if v is not now), default=0):.1f}"
            )
        elif best.gameweek != gws[0] and best.uplift_mean > 0:
            rec = "wait"
            reason = (
                f"GW{best.gameweek} offers +{best.uplift_mean:.1f} pts vs "
                f"{'+' if (v_now or 0) >= 0 else ''}{(v_now or 0):.1f} now "
                f"(value of waiting {waiting:+.1f})"
            )
        else:
            rec = "keep"
            reason = (
                "no week in the horizon clears the bar (best "
                f"{best.uplift_mean:+.1f} pts, P>0 {best.probability_positive:.2f}); "
                "value beyond the horizon is not estimated"
            )
        if expires:
            reason += f"; window closes GW{chip.last_gameweek} (inside the horizon)"
        out.append(
            ChipPlan(
                chip_id=chip.chip_id,
                chip_type=chip.chip_type.value,
                window=(chip.first_gameweek, chip.last_gameweek),
                expires_in_horizon=expires,
                by_gameweek=values,
                best_gameweek=best.gameweek,
                value_now=v_now,
                best_value=best.uplift_mean,
                value_of_waiting=waiting,
                recommendation=rec,
                reason=reason,
            )
        )
    return out
