"""Walk-forward evaluation of the team strength model against baselines (§57.2)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import poisson

from fpl_forecasting.team_strength import TeamStrengthConfig, fit_team_strength
from fpl_storage.dataset import CanonicalDataset
from fpl_storage.pit import PointInTimeView, decision_cutoff


@dataclass(frozen=True)
class TeamEvalResult:
    n_matches: int
    log_lik: float
    log_lik_league_avg: float
    cs_brier: float
    cs_brier_league_avg: float
    cs_log_loss: float
    goals_mae: float
    goals_mae_league_avg: float
    rows: pd.DataFrame

    def as_dict(self) -> dict[str, float | int]:
        return {k: v for k, v in self.__dict__.items() if k != "rows"}


def evaluate_team_model(
    ds: CanonicalDataset,
    seasons: Iterable[str],
    config: TeamStrengthConfig,
    buffer_minutes: int = 90,
) -> TeamEvalResult:
    gws, fx = ds["gameweeks"], ds["fixtures"]
    rows: list[dict[str, Any]] = []
    for season in seasons:
        teams = sorted(set(fx.loc[fx["season"] == season, "home_team_code"]))
        for gw in sorted(gws.loc[gws["season"] == season, "gw"]):
            deadline = gws[(gws["season"] == season) & (gws["gw"] == gw)]["deadline_at"].iloc[0]
            cut = decision_cutoff(deadline, buffer_minutes)
            tm = PointInTimeView(ds, cut).team_match()
            if tm.empty:
                continue
            ts = fit_team_strength(tm, cut, config, teams)
            home = tm[tm["was_home"]]
            base_h, base_a = float(home["goals_for"].mean()), float(home["goals_against"].mean())
            tgt = fx[(fx["season"] == season) & (fx["gw"] == gw) & fx["home_score"].notna()]
            for r in tgt.itertuples():
                mh, ma = ts.expected_goals(int(r.home_team_code), int(r.away_team_code))
                rows.append(
                    {
                        "season": season,
                        "gw": gw,
                        "mu_h": mh,
                        "mu_a": ma,
                        "base_h": base_h,
                        "base_a": base_a,
                        "hg": int(r.home_score),
                        "ag": int(r.away_score),
                    }
                )
    d = pd.DataFrame(rows)
    ll = poisson.logpmf(d["hg"], d["mu_h"]) + poisson.logpmf(d["ag"], d["mu_a"])
    llb = poisson.logpmf(d["hg"], d["base_h"]) + poisson.logpmf(d["ag"], d["base_a"])
    # clean sheet of the home side ⇔ away goals == 0, and vice versa
    p_cs = np.concatenate([np.exp(-d["mu_a"]), np.exp(-d["mu_h"])])
    p_cs_b = np.concatenate([np.exp(-d["base_a"]), np.exp(-d["base_h"])])
    y_cs = np.concatenate([(d["ag"] == 0), (d["hg"] == 0)]).astype(float)
    eps = 1e-12
    return TeamEvalResult(
        n_matches=len(d),
        log_lik=float(ll.mean()),
        log_lik_league_avg=float(llb.mean()),
        cs_brier=float(np.mean((p_cs - y_cs) ** 2)),
        cs_brier_league_avg=float(np.mean((p_cs_b - y_cs) ** 2)),
        cs_log_loss=float(
            -np.mean(y_cs * np.log(p_cs + eps) + (1 - y_cs) * np.log(1 - p_cs + eps))
        ),
        goals_mae=float(
            np.mean(np.abs(np.concatenate([d["mu_h"] - d["hg"], d["mu_a"] - d["ag"]])))
        ),
        goals_mae_league_avg=float(
            np.mean(np.abs(np.concatenate([d["base_h"] - d["hg"], d["base_a"] - d["ag"]])))
        ),
        rows=d,
    )
