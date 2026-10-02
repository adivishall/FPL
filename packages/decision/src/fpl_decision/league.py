"""Optional mini-league / ownership awareness (§24, §65).

Rank effects are never claimed; everything is a modelled probability on the joint simulation:

* head-to-head: P(my gameweek points > rival's), the distribution of the points gap;
* differential exposure: players I own that the rival does not (and vice versa) with their
  ownership share; template assets (high ownership) I do not own;
* lead-protection vs chasing view of alternative plans: a plan that maximises the mean is not the
  one that maximises P(overtaking a rival) — both are shown, the user chooses.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd
from pydantic import BaseModel

from fpl_domain.enums import ChipType, Position
from fpl_domain.rules import Ruleset
from fpl_domain.squad import Lineup
from fpl_simulation.engine import SimulationResult
from fpl_simulation.lineup_eval import score_lineup


class HeadToHead(BaseModel):
    rival: str
    probability_ahead: float
    probability_level: float
    gap_mean: float
    gap_p10: float
    gap_p90: float
    my_differentials: list[int]
    rival_differentials: list[int]


def lineup_points(
    sim: SimulationResult,
    lineup: Lineup,
    gw: int,
    positions: Mapping[int, Position],
    ruleset: Ruleset,
    chip: ChipType | None = None,
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


def head_to_head(
    sim: SimulationResult,
    mine: Lineup,
    rivals: Mapping[str, Lineup],
    gw: int,
    positions: Mapping[int, Position],
    ruleset: Ruleset,
) -> list[HeadToHead]:
    me = lineup_points(sim, mine, gw, positions, ruleset)
    out = []
    for name, lu in rivals.items():
        r = lineup_points(sim, lu, gw, positions, ruleset)
        gap = me - r
        out.append(
            HeadToHead(
                rival=name,
                probability_ahead=float((gap > 0).mean()),
                probability_level=float((gap == 0).mean()),
                gap_mean=float(gap.mean()),
                gap_p10=float(np.percentile(gap, 10)),
                gap_p90=float(np.percentile(gap, 90)),
                my_differentials=sorted(set(mine.starters) - set(lu.starters)),
                rival_differentials=sorted(set(lu.starters) - set(mine.starters)),
            )
        )
    return out


def template_gaps(owned: set[int], ownership: pd.Series, threshold: float = 30.0) -> pd.DataFrame:
    """Highly-owned players (≥ threshold %) not in my squad: main sources of rank volatility."""
    t = ownership[ownership >= threshold].sort_values(ascending=False)
    keep = ~t.index.isin(list(owned))
    return pd.DataFrame({"player_code": t.index[keep], "ownership": t.to_numpy()[keep]})


def plan_choice_vs_rival(plans: Mapping[str, np.ndarray], rival: np.ndarray) -> pd.DataFrame:
    """For alternative plans' sample totals: mean points vs P(finishing ahead of the rival)."""
    rows = [
        {
            "plan": k,
            "mean_points": float(v.mean()),
            "probability_ahead": float((v > rival).mean()),
            "std": float(v.std()),
        }
        for k, v in plans.items()
    ]
    return pd.DataFrame(rows).sort_values("mean_points", ascending=False).reset_index(drop=True)
