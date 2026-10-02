"""Assemble optimiser inputs from a probabilistic forecast and the point-in-time player pool.

The forecast summary has one row per player × target gameweek (mean, quantiles, …). A player
without a fixture in a gameweek (blank) gets EV 0 for that gameweek; a player in the pool with no
forecast row at all (e.g. not registered for any upcoming fixture) gets EV 0 throughout.
Prices are the purchase prices known at the cutoff (``PointInTimeView.player_pool``).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from fpl_domain.enums import POSITIONS
from fpl_domain.state import ManagerState
from fpl_optimizer.problem import PlayerTable

POS = {p.value: i for i, p in enumerate(POSITIONS)}


def player_table(
    summary: pd.DataFrame,
    pool: pd.DataFrame,
    gameweeks: Sequence[int],
    state: ManagerState | None = None,
) -> PlayerTable:
    """``pool`` columns: player_code, team_code, position, price, web_name (optional)."""
    p = pool.dropna(subset=["position", "price", "team_code"]).copy()
    p["price"] = pd.to_numeric(p["price"], errors="coerce")
    p = p[p["price"] > 0]
    if state is not None:
        missing = set(state.codes) - set(p["player_code"].astype(int))
        if missing:
            raise ValueError(f"owned players missing from the pool at the cutoff: {missing}")
    p = p.sort_values("player_code").drop_duplicates("player_code").reset_index(drop=True)
    codes = p["player_code"].astype(np.int64).to_numpy()
    gws = list(gameweeks)
    mean = summary.pivot_table(index="player_code", columns="gw", values="mean", aggfunc="sum")
    q10 = summary.pivot_table(index="player_code", columns="gw", values="p10", aggfunc="sum")
    ev = mean.reindex(index=codes, columns=gws).fillna(0.0).to_numpy(float)
    lo = q10.reindex(index=codes, columns=gws).fillna(0.0).to_numpy(float)
    names = (
        tuple(str(n) for n in p["web_name"].fillna("").to_numpy())
        if "web_name" in p.columns
        else None
    )
    return PlayerTable(
        code=codes,
        position=p["position"].map(POS).astype(np.int64).to_numpy(),
        team=p["team_code"].astype(np.int64).to_numpy(),
        price=p["price"].astype(np.int64).to_numpy(),
        ev=ev,
        q10=np.minimum(lo, ev),
        name=names,
    )
