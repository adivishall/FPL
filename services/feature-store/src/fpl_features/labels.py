"""Labels (realised outcomes) — for training and evaluation ONLY.

Two access paths with different guarantees:

* ``training_labels(view)`` — outcomes visible at the view's ``as_of`` (PIT-safe; usable to
  train a model at that cutoff).
* ``realized_*`` — read the canonical dataset directly. They return *future* information relative
  to any decision cutoff and must only be called by evaluation/backtesting code **after** the
  forecast or decision has been produced. They are never imported by inference code paths
  (enforced by review and by the backtest no-lookahead audit).
"""

from __future__ import annotations

import pandas as pd

from fpl_storage.dataset import CanonicalDataset
from fpl_storage.pit import PointInTimeView

OUTCOME_COLS = (
    "minutes",
    "starts",
    "points",
    "goals",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "saves",
    "bonus",
    "bps",
    "yellow_cards",
    "red_cards",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "dc",
)


def training_labels(view: PointInTimeView, seasons: list[str] | None = None) -> pd.DataFrame:
    """Per player-fixture outcomes available at ``view.as_of``."""
    return view.player_match(seasons)


def realized_player_fixture(
    ds: CanonicalDataset, season: str, gameweeks: list[int]
) -> pd.DataFrame:
    pm = ds["player_match"]
    pm = pm[(pm["season"] == season) & pm["gw"].isin(gameweeks)]
    return pm[["season", "gw", "fixture_id", "player_code", "team_code", *OUTCOME_COLS]].copy()


def realized_player_gw(ds: CanonicalDataset, season: str, gameweeks: list[int]) -> pd.DataFrame:
    """Per player-GW totals (double gameweeks summed). Players without a row scored 0."""
    pf = realized_player_fixture(ds, season, gameweeks)
    agg = pf.groupby(["season", "gw", "player_code"], as_index=False).agg(
        points=("points", "sum"),
        minutes=("minutes", "sum"),
        starts=("starts", "sum"),
        n_fixtures=("fixture_id", "nunique"),
    )
    return agg
