"""Deterministic forecasting baselines (§28, §57.2 'Always compare against naive and domain
baselines', §70.3).

Each baseline maps a point-in-time feature row (player × target fixture) to expected points for
that fixture. None of them uses information unavailable at the cutoff; all are fitted (where
needed) only on training rows supplied by the walk-forward harness.

* ``position_mean``   — naive: mean points per player-fixture by position (training window).
* ``recent_form``     — naive form ranking: recency-weighted points of the last ~3 matches.
* ``ppg_availability``— "simple xP": season points-per-appearance × recent appearance rate.
* ``fpl_style``       — an *approximation* of an official-style heuristic: short-term form ×
  availability × a PIT-safe fixture factor from the opponent's recent xG conceded. It is NOT the
  official FPL algorithm (which is not public; ADR-0001 #10).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class PositionMeanBaseline:
    name: str = "position_mean"
    version: str = "1.0.0"
    means: dict[str, float] = field(default_factory=dict)

    def fit(self, train: pd.DataFrame) -> None:
        """``train`` = feature rows of past GWs joined with realised ``points``."""
        self.means = train.groupby("position")["points"].mean().to_dict()

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        overall = float(np.mean(list(self.means.values()))) if self.means else 1.0
        return frame["position"].map(self.means).fillna(overall).astype(float)


@dataclass
class RecentFormBaseline:
    name: str = "recent_form"
    version: str = "1.0.0"

    def fit(self, train: pd.DataFrame) -> None:
        return None

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        return frame["pts_ewm_short"].astype(float).fillna(0.0)


@dataclass
class PpgAvailabilityBaseline:
    name: str = "ppg_availability"
    version: str = "1.0.0"

    def fit(self, train: pd.DataFrame) -> None:
        return None

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        ppg = frame["ppg_season"].astype(float)
        # Early season: fall back to recency-weighted points per 90 × typical minutes
        fallback = frame["pts_p90"].astype(float) * frame["mins_ewm_long"].astype(float) / 90.0
        ppg = ppg.fillna(fallback / frame["app_rate_short"].astype(float).replace(0, np.nan))
        avail = frame["app_rate_short"].astype(float).fillna(0.0)
        return (ppg.fillna(0.0) * avail).clip(lower=0.0)


@dataclass
class FplStyleBaseline:
    name: str = "fpl_style"
    version: str = "1.0.0"
    league_xga: float = 1.35
    sensitivity: float = 0.25

    def fit(self, train: pd.DataFrame) -> None:
        if "team_xg_against_ewm" in train and train["team_xg_against_ewm"].notna().any():
            self.league_xga = float(train["team_xg_against_ewm"].mean())

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        form = frame["pts_ewm_short"].astype(float).fillna(0.0)
        avail = frame["app_rate_short"].astype(float).fillna(0.0)
        opp_xga = (
            frame["opp_xg_against_ewm"].astype(float)
            if "opp_xg_against_ewm" in frame
            else pd.Series(self.league_xga, index=frame.index)
        )
        factor = 1.0 + self.sensitivity * (opp_xga.fillna(self.league_xga) / self.league_xga - 1.0)
        home = np.where(frame["is_home"].astype(bool), 1.05, 0.95)
        return (form * np.maximum(avail, 0.25) * factor * home).clip(lower=0.0)


def all_baselines() -> list:
    return [
        PositionMeanBaseline(),
        RecentFormBaseline(),
        PpgAvailabilityBaseline(),
        FplStyleBaseline(),
    ]
