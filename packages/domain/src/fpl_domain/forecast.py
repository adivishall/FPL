"""Typed contracts between parameter estimation (forecasting) and simulation (ADR-0006).

Columnar (struct-of-arrays) for vectorised simulation. One row of ``PlayerFixtureParams`` is one
player in one scheduled fixture; double gameweeks have two rows for the same player.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

F = npt.NDArray[np.float64]
I = npt.NDArray[np.int64]  # noqa: E741
B = npt.NDArray[np.bool_]

# Minute buckets: [lo, hi] inclusive. Start buckets end at 90 (stoppage time is irrelevant for
# FPL thresholds); sub buckets cover substitute appearances.
START_BUCKETS: tuple[tuple[int, int], ...] = ((1, 44), (45, 59), (60, 74), (75, 88), (89, 90))
SUB_BUCKETS: tuple[tuple[int, int], ...] = ((1, 15), (16, 30), (31, 45), (46, 89))


@dataclass(frozen=True)
class PlayerFixtureParams:
    player_code: I
    fixture_id: I
    gameweek: I
    team_code: I
    is_home: B
    position: I  # 0=GK 1=DEF 2=MID 3=FWD
    p_start: F
    p_sub: F  # P(appears as substitute | does not start)
    start_buckets: F  # [n, len(START_BUCKETS)] rows sum to 1
    sub_buckets: F  # [n, len(SUB_BUCKETS)]
    goal_share: F  # share of team goals while on the pitch (per-90 normalised)
    assist_share: F
    og_propensity: F
    saves_p90: F
    dc_p90: F  # defensive-contribution actions per 90 (position-specific definition)
    dc_dispersion: F  # negative-binomial size parameter
    yellow_p90: F
    red_p90: F
    pen_miss_p90: F
    pen_save_p90: F
    bps_offset: F  # player-specific BPS tendency per 90 (shrunk residual)

    def __post_init__(self) -> None:
        n = len(self.player_code)
        for name, arr in self.__dict__.items():
            a = np.asarray(arr)
            if a.shape[0] != n:
                raise ValueError(f"{name}: length {a.shape[0]} != {n}")
        for name in ("p_start", "p_sub"):
            a = getattr(self, name)
            if np.any((a < 0) | (a > 1)) or np.any(np.isnan(a)):
                raise ValueError(f"{name} must be probabilities")
        for name, nb in (("start_buckets", len(START_BUCKETS)), ("sub_buckets", len(SUB_BUCKETS))):
            a = getattr(self, name)
            if a.shape != (n, nb) or not np.allclose(a.sum(axis=1), 1.0, atol=1e-6):
                raise ValueError(f"{name} must be [n, {nb}] probability rows")
        for name in (
            "goal_share",
            "assist_share",
            "og_propensity",
            "saves_p90",
            "dc_p90",
            "dc_dispersion",
            "yellow_p90",
            "red_p90",
            "pen_miss_p90",
            "pen_save_p90",
        ):
            a = getattr(self, name)
            if np.any(a < 0) or np.any(np.isnan(a)):
                raise ValueError(f"{name} must be non-negative and finite")

    def __len__(self) -> int:
        return len(self.player_code)

    def take(self, idx: npt.ArrayLike) -> PlayerFixtureParams:
        ix = np.asarray(idx)
        return PlayerFixtureParams(**{k: np.asarray(v)[ix] for k, v in self.__dict__.items()})

    def replace(self, **changes: npt.ArrayLike) -> PlayerFixtureParams:
        data = {k: np.array(v, copy=True) for k, v in self.__dict__.items()}
        data.update({k: np.asarray(v) for k, v in changes.items()})
        return PlayerFixtureParams(**data)


@dataclass(frozen=True)
class FixtureParams:
    fixture_id: I
    gameweek: I
    home_team: I
    away_team: I
    mu_home: F  # expected goals scored by the home side
    mu_away: F

    def __post_init__(self) -> None:
        if np.any(self.mu_home <= 0) or np.any(self.mu_away <= 0):
            raise ValueError("expected goals must be positive")

    def __len__(self) -> int:
        return len(self.fixture_id)

    def replace(self, **changes: npt.ArrayLike) -> FixtureParams:
        data = {k: np.array(v, copy=True) for k, v in self.__dict__.items()}
        data.update({k: np.asarray(v) for k, v in changes.items()})
        return FixtureParams(**data)


@dataclass(frozen=True)
class BpsModel:
    """Learned linear map from simulated match events to BPS (per position), plus noise."""

    intercept: F  # [4] per position
    per_minute: F  # [4]
    full_match: F  # [4] bonus for ≥60 minutes
    goal: F  # [4]
    assist: float
    clean_sheet: F  # [4]
    goals_conceded: F  # [4]
    saves: float
    dc_action: F  # [4]
    yellow: float
    red: float
    own_goal: float
    pen_miss: float
    pen_save: float
    sigma: F  # [4] residual SD


@dataclass(frozen=True)
class GlobalParams:
    p_assisted: float  # share of (non-own-goal) goals credited with an FPL assist
    own_goal_rate: float  # share of team goals that are opponent own goals
    league_mu: float  # league-average goals per team per match (for opponent factors)
    saves_opp_elasticity: float
    dc_opp_elasticity: float
    bps: BpsModel
    ruleset_version: str
    model_versions: dict[str, str] = field(default_factory=dict)
