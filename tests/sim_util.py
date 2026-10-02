"""Toy simulation inputs with analytically known answers (test helper)."""

from __future__ import annotations

import numpy as np

from fpl_domain.forecast import (
    START_BUCKETS,
    SUB_BUCKETS,
    BpsModel,
    FixtureParams,
    GlobalParams,
    PlayerFixtureParams,
)

BPS = BpsModel(
    intercept=np.zeros(4),
    per_minute=np.full(4, 0.03),
    full_match=np.full(4, 3.0),
    goal=np.array([12, 12, 18, 24.0]),
    assist=9,
    clean_sheet=np.array([12, 12, 0, 0.0]),
    goals_conceded=np.array([-4, -4, 0, 0.0]),
    saves=2,
    dc_action=np.full(4, 0.3),
    yellow=-3,
    red=-9,
    own_goal=-6,
    pen_miss=-6,
    pen_save=15,
    sigma=np.full(4, 4.0),
)


def glob(own_goal_rate: float = 0.0, p_assisted: float = 0.75) -> GlobalParams:
    return GlobalParams(
        p_assisted=p_assisted,
        own_goal_rate=own_goal_rate,
        league_mu=1.4,
        saves_opp_elasticity=1.0,
        dc_opp_elasticity=0.0,
        bps=BPS,
        ruleset_version="2026-27.1",
    )


def toy_players(
    fixture_id: int = 1, gameweek: int = 1, home: int = 1, away: int = 2, full_minutes: bool = True
) -> PlayerFixtureParams:
    rows = []
    for side, team in ((True, home), (False, away)):
        for k in range(11):
            pos = 0 if k == 0 else (1 if k <= 4 else (2 if k <= 8 else 3))
            rows.append((team * 100 + k, fixture_id, gameweek, team, side, pos))
    a = np.array(rows)
    n = len(a)
    pos = a[:, 5]
    sb = np.zeros((n, len(START_BUCKETS)))
    sb[:, -1 if full_minutes else 2] = 1.0
    sub = np.zeros((n, len(SUB_BUCKETS)))
    sub[:, 0] = 1.0
    return PlayerFixtureParams(
        player_code=a[:, 0],
        fixture_id=a[:, 1],
        gameweek=a[:, 2],
        team_code=a[:, 3],
        is_home=a[:, 4].astype(bool),
        position=pos,
        p_start=np.ones(n),
        p_sub=np.zeros(n),
        start_buckets=sb,
        sub_buckets=sub,
        goal_share=np.select([pos == 3, pos == 2], [0.25, 0.0625], 0.0),
        assist_share=np.where(pos >= 2, 0.1, 0.02),
        og_propensity=np.where(pos == 1, 1.0, 0.1),
        saves_p90=np.where(pos == 0, 3.0, 0.0),
        dc_p90=np.where(pos == 1, 9.0, 5.0),
        dc_dispersion=np.full(n, 5.0),
        yellow_p90=np.full(n, 0.1),
        red_p90=np.full(n, 0.005),
        pen_miss_p90=np.zeros(n),
        pen_save_p90=np.zeros(n),
        bps_offset=np.zeros(n),
    )


def toy_fixture(
    fixture_id: int = 1,
    gameweek: int = 1,
    home: int = 1,
    away: int = 2,
    mu_home: float = 1.8,
    mu_away: float = 1.0,
) -> FixtureParams:
    return FixtureParams(
        fixture_id=np.array([fixture_id]),
        gameweek=np.array([gameweek]),
        home_team=np.array([home]),
        away_team=np.array([away]),
        mu_home=np.array([mu_home]),
        mu_away=np.array([mu_away]),
    )
