"""Estimation of simulation-level parameters from point-in-time history (ADR-0006).

* ``fit_bps_model``    — BPS as a learned linear function of match events per position
  (recency-weighted least squares over recent seasons), residual SD per position, and a shrunk
  per-player residual offset (per 90). Learning the map from data — rather than hard-coding a
  BPS formula — lets 2026/27 BPS rule changes enter as data accrues (ADR-0001 #13).
* ``estimate_globals`` — assisted-goal share, own-goal share, league goal rate.
* ``fit_elasticity``   — Poisson maximum likelihood for how saves / defensive actions scale
  with opponent attacking strength (offset = player's own rate × minutes).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar

from fpl_domain.enums import POSITIONS
from fpl_domain.forecast import BpsModel

POS_INDEX = {p.value: i for i, p in enumerate(POSITIONS)}


def _season_weights(seasons: pd.Series, current: str, decay: float = 0.5) -> np.ndarray:
    order = sorted(seasons.unique())
    rank = {s: len(order) - 1 - k for k, s in enumerate(order)}
    if current in rank:
        rank = {s: max(r - rank[current], 0) for s, r in rank.items()}
    return np.array([decay ** rank[s] for s in seasons])


def fit_bps_model(
    hist: pd.DataFrame, positions: pd.Series, current_season: str, offset_kappa_90: float = 8.0
) -> tuple[BpsModel, pd.Series]:
    """Returns the BPS model and per-player BPS offsets (per 90, shrunk toward 0)."""
    h = hist[hist["minutes"] > 0].copy()
    h["pos"] = h["player_code"].map(positions).map(POS_INDEX)
    h = h[h["pos"].notna()]
    pos = h["pos"].astype(int).to_numpy()
    m = h["minutes"].astype(float).to_numpy()
    w = _season_weights(h["season"], current_season)
    cbi = h["cbi"].astype("Float64").astype(float)
    tck = h["tackles"].astype("Float64").astype(float)
    rec = h["recoveries"].astype("Float64").astype(float)
    dca = (cbi + tck + np.where(np.isin(pos, [2, 3]), rec, 0.0)).fillna(0.0).to_numpy()
    has_dc = cbi.notna().to_numpy() & (pos != 0)
    cols: list[np.ndarray] = []
    names: list[tuple[str, int | None]] = []

    def per_pos(name: str, v: np.ndarray) -> None:
        for p in range(4):
            cols.append(np.where(pos == p, v, 0.0))
            names.append((name, p))

    def shared(name: str, v: np.ndarray) -> None:
        cols.append(v)
        names.append((name, None))

    per_pos("intercept", np.ones(len(h)))
    per_pos("per_minute", m)
    per_pos("full_match", (m >= 60).astype(float))
    per_pos("goal", h["goals"].astype(float).to_numpy())
    shared("assist", h["assists"].astype(float).to_numpy())
    per_pos("clean_sheet", h["clean_sheets"].astype(float).to_numpy())
    per_pos("goals_conceded", h["goals_conceded"].astype(float).to_numpy())
    shared("saves", h["saves"].astype(float).to_numpy())
    per_pos("dc_action", np.where(has_dc, dca, 0.0))
    shared("dc_missing", (~has_dc & (pos != 0)).astype(float))
    shared("yellow", h["yellow_cards"].astype(float).to_numpy())
    shared("red", h["red_cards"].astype(float).to_numpy())
    shared("own_goal", h["own_goals"].astype(float).to_numpy())
    shared("pen_miss", h["penalties_missed"].astype(float).to_numpy())
    shared("pen_save", h["penalties_saved"].astype(float).to_numpy())
    x = np.column_stack(cols)
    y = h["bps"].astype(float).to_numpy()
    sw = np.sqrt(w)
    ridge = 1e-3 * np.eye(x.shape[1])
    beta = np.linalg.solve((x * w[:, None]).T @ x + ridge, (x * w[:, None]).T @ y)
    del sw
    coef: dict[tuple[str, int | None], float] = dict(zip(names, beta, strict=True))
    resid = y - x @ beta

    def vec(name: str) -> np.ndarray:
        return np.array([coef[(name, p)] for p in range(4)])

    sigma = np.array(
        [
            np.sqrt(np.average(resid[pos == p] ** 2, weights=w[pos == p]))
            if (pos == p).any()
            else 5.0
            for p in range(4)
        ]
    )
    model = BpsModel(
        intercept=vec("intercept"),
        per_minute=vec("per_minute"),
        full_match=vec("full_match"),
        goal=vec("goal"),
        assist=coef[("assist", None)],
        clean_sheet=vec("clean_sheet"),
        goals_conceded=vec("goals_conceded"),
        saves=coef[("saves", None)],
        dc_action=vec("dc_action"),
        yellow=coef[("yellow", None)],
        red=coef[("red", None)],
        own_goal=coef[("own_goal", None)],
        pen_miss=coef[("pen_miss", None)],
        pen_save=coef[("pen_save", None)],
        sigma=sigma,
    )
    # Player tendency (key passes, dribbles … not modelled explicitly): shrunk residual per 90.
    h["resid"] = resid * w
    h["m90"] = m / 90.0 * w
    g = h.groupby("player_code")[["resid", "m90"]].sum()
    offsets = g["resid"] / (g["m90"] + offset_kappa_90)
    return model, offsets


def estimate_globals(hist: pd.DataFrame, team_match: pd.DataFrame) -> dict[str, float]:
    goals = float(hist["goals"].sum())
    assists = float(hist["assists"].sum())
    ogs = float(hist["own_goals"].sum())
    league_mu = float(team_match["goals_for"].mean()) if len(team_match) else 1.4
    return {
        "p_assisted": float(np.clip(assists / max(goals, 1.0), 0.3, 1.0)),
        "own_goal_rate": float(np.clip(ogs / max(goals + ogs, 1.0), 0.0, 0.1)),
        "league_mu": league_mu,
    }


def fit_elasticity(
    y: np.ndarray,
    base_rate: np.ndarray,
    minutes: np.ndarray,
    opp_ratio: np.ndarray,
    bounds: tuple[float, float] = (-1.0, 2.0),
) -> float:
    """MLE of β in y ~ Poisson(base_rate · minutes/90 · opp_ratio^β)."""
    ok = (base_rate > 0) & (minutes > 0) & np.isfinite(opp_ratio) & (opp_ratio > 0)
    if ok.sum() < 50:
        return 0.0
    yy, lam0, lr = y[ok], base_rate[ok] * minutes[ok] / 90.0, np.log(opp_ratio[ok])

    def nll(beta: float) -> float:
        lam = lam0 * np.exp(beta * lr)
        return float(np.sum(lam - yy * np.log(lam)))

    res = minimize_scalar(nll, bounds=bounds, method="bounded")
    return float(res.x)
