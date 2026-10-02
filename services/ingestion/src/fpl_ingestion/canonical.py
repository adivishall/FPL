"""Canonical layer (§55.1): map validated source frames into stable domain entities.

Output frames use **natural keys** (season code, stable player/team ``code``, fixture source id)
so they are identical whether produced from a fresh ingestion, read back from PostgreSQL, or
loaded from a Parquet snapshot. Every observation carries its ADR-0004 availability timestamp.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from fpl_domain.rules import Ruleset
from fpl_ingestion.quality import POS_BY_TYPE, SeasonFrames


@dataclass(frozen=True)
class PitPolicy:
    derived_deadline_offset_minutes: int = 90
    schedule_published_days_before_season: int = 60
    dgw_fixture_announce_lag_days: int = 28

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> PitPolicy:
        return cls(**cfg.get("point_in_time", {}))


@dataclass
class CanonicalSeason:
    season: str
    seasons: pd.DataFrame
    gameweeks: pd.DataFrame
    teams: pd.DataFrame
    players: pd.DataFrame
    fixtures: pd.DataFrame
    player_match: pd.DataFrame
    team_match: pd.DataFrame
    player_snapshots: pd.DataFrame
    news_signals: pd.DataFrame

    TABLES = (
        "seasons",
        "gameweeks",
        "teams",
        "players",
        "fixtures",
        "player_match",
        "team_match",
        "player_snapshots",
        "news_signals",
    )

    def frames(self) -> dict[str, pd.DataFrame]:
        return {t: getattr(self, t) for t in self.TABLES}


def canonicalize(
    frames: SeasonFrames,
    ruleset: Ruleset,
    policy: PitPolicy,
    observed_at: datetime,
    *,
    snapshot_captured_at: datetime | None = None,
    source: str = "vaastav",
    source_version: str | None = None,
) -> CanonicalSeason:
    """Build canonical frames.

    ``observed_at`` is when the batch's information was known to be published (for the pinned
    historical repository: the commit time). Gameweek/fixture *status* is evaluated at that time.
    ``snapshot_captured_at`` (optional) marks ``players_raw`` as a genuine point-in-time bootstrap
    capture to be stored as player snapshots.
    """
    season = frames.season
    timing = ruleset.timing
    teams_src = frames.teams
    code_by_tid = teams_src.set_index("id")["code"].astype(int)

    # ---------------------------------------------------------------- fixtures
    fx = frames.fixtures.copy()
    fx["home_team_code"] = fx["team_h"].map(code_by_tid)
    fx["away_team_code"] = fx["team_a"].map(code_by_tid)
    first_kickoff = fx["kickoff_time"].min()
    publish_lead = pd.Timedelta(days=policy.schedule_published_days_before_season)
    season_published = (first_kickoff - publish_lead).floor("D")
    long = pd.concat(
        [
            fx[["id", "event", "team_h"]].rename(columns={"team_h": "team"}),
            fx[["id", "event", "team_a"]].rename(columns={"team_a": "team"}),
        ]
    )
    sched_long = long.dropna(subset=["event"])
    per_team_gw = sched_long.groupby(["event", "team"])["id"].transform("count")
    dgw_fixture_ids = set(sched_long.loc[per_team_gw > 1, "id"])
    announce = fx["kickoff_time"] - pd.Timedelta(days=policy.dgw_fixture_announce_lag_days)
    is_dgw = fx["id"].isin(dgw_fixture_ids)
    fx["schedule_available_at"] = np.where(
        is_dgw, announce.where(announce > season_published, season_published), season_published
    )
    fx["schedule_available_at"] = pd.to_datetime(fx["schedule_available_at"], utc=True)
    prov = pd.Timedelta(minutes=timing.provisional_lag_minutes)
    fx["result_available_at"] = (fx["kickoff_time"] + prov).where(fx["finished"].astype(bool))

    gw_last_kickoff = fx.groupby("event")["kickoff_time"].max()
    finalized_at_by_gw = gw_last_kickoff + pd.Timedelta(hours=timing.finalization_lag_hours)
    fin_at = fx["event"].map(finalized_at_by_gw)
    observed = pd.Timestamp(observed_at)
    fx["finalized"] = fx["finished"].astype(bool) & (fin_at <= observed)
    fx["status"] = np.select(
        [fx["event"].isna(), fx["finalized"], fx["finished"].astype(bool)],
        ["unscheduled", "final", "provisional"],
        default="scheduled",
    )
    fixtures = (
        pd.DataFrame(
            {
                "season": season,
                "fixture_id": fx["id"].astype(int),
                "gw": fx["event"].astype("Int64"),
                "kickoff_at": fx["kickoff_time"],
                "home_team_code": fx["home_team_code"].astype(int),
                "away_team_code": fx["away_team_code"].astype(int),
                "status": fx["status"],
                "finalized": fx["finalized"].astype(bool),
                "home_score": fx["team_h_score"].where(fx["finished"].astype(bool)).astype("Int64"),
                "away_score": fx["team_a_score"].where(fx["finished"].astype(bool)).astype("Int64"),
                "home_difficulty": fx["team_h_difficulty"].astype("Int64"),
                "away_difficulty": fx["team_a_difficulty"].astype("Int64"),
                "schedule_available_at": fx["schedule_available_at"],
                "result_available_at": fx["result_available_at"],
            }
        )
        .sort_values("fixture_id")
        .reset_index(drop=True)
    )

    # ---------------------------------------------------------------- gameweeks
    sched = fx.dropna(subset=["event"])
    agg = sched.groupby("event").agg(
        first=("kickoff_time", "min"),
        last=("kickoff_time", "max"),
        all_finished=("finished", "all"),
    )
    deadline = agg["first"] - pd.Timedelta(minutes=policy.derived_deadline_offset_minutes)
    fin = agg["last"] + pd.Timedelta(hours=timing.finalization_lag_hours)
    status = np.select(
        [
            agg["all_finished"].astype(bool) & (fin <= observed),
            agg["all_finished"].astype(bool),
            deadline <= observed,
        ],
        ["finalized", "provisional", "live"],
        default="upcoming",
    )
    gameweeks = pd.DataFrame(
        {
            "season": season,
            "gw": agg.index.astype(int),
            "deadline_at": deadline.to_numpy(),
            "first_kickoff_at": agg["first"].to_numpy(),
            "last_kickoff_at": agg["last"].to_numpy(),
            "finalized_at": fin.where(agg["all_finished"].astype(bool)).to_numpy(),
            "status": status,
            "deadline_source": f"derived:first_kickoff-{policy.derived_deadline_offset_minutes}m",
        }
    ).reset_index(drop=True)
    for c in ("deadline_at", "first_kickoff_at", "last_kickoff_at", "finalized_at"):
        gameweeks[c] = pd.to_datetime(gameweeks[c], utc=True)

    # ---------------------------------------------------------------- teams & players
    teams = (
        pd.DataFrame(
            {
                "season": season,
                "team_code": teams_src["code"].astype(int),
                "source_id": teams_src["id"].astype(int),
                "name": teams_src["name"].astype(str),
                "short_name": teams_src["short_name"].astype(str),
                "strength_json": [
                    json.dumps(
                        {k: _num(r.get(k)) for k in r.index if str(k).startswith("strength")},
                        sort_keys=True,
                    )
                    for _, r in teams_src.iterrows()
                ],
            }
        )
        .sort_values("team_code")
        .reset_index(drop=True)
    )

    pr = frames.players_raw
    players = (
        pd.DataFrame(
            {
                "season": season,
                "player_code": pr["code"].astype(int),
                "source_id": pr["id"].astype(int),
                "team_code": pr["team_code"].astype(int),
                "web_name": pr["web_name"].astype(str),
                "first_name": pr["first_name"],
                "last_name": pr["second_name"],
                "position": pr["element_type"].astype(int).map(POS_BY_TYPE),
                "price": pr["now_cost"].astype(int),
                "status": pr["status"].astype(str),
                "source_updated_at": pd.Timestamp(observed_at),
            }
        )
        .sort_values("player_code")
        .reset_index(drop=True)
    )

    # ---------------------------------------------------------------- player_match
    mg = frames.merged_gw
    code_by_element = pr.set_index("id")["code"].astype(int)
    team_code = mg["team_id"].map(code_by_tid)
    opp_code = mg["opponent_team"].astype(int).map(code_by_tid)
    available_at = mg["kickoff_time"] + prov
    fixture_final = mg["fixture"].map(fixtures.set_index("fixture_id")["finalized"])
    pm = (
        pd.DataFrame(
            {
                "season": season,
                "gw": mg["GW"].astype(int),
                "fixture_id": mg["fixture"].astype(int),
                "player_code": mg["element"].map(code_by_element).astype(int),
                "team_code": team_code.astype(int),
                "opponent_team_code": opp_code.astype(int),
                "was_home": mg["was_home"].astype(bool),
                "kickoff_at": mg["kickoff_time"],
                "minutes": mg["minutes"].astype(int),
                "starts": mg["starts"].astype("Int64"),
                "points": mg["total_points"].astype(int),
                "goals": mg["goals_scored"].astype(int),
                "assists": mg["assists"].astype(int),
                "clean_sheets": mg["clean_sheets"].astype(int),
                "goals_conceded": mg["goals_conceded"].astype(int),
                "own_goals": mg["own_goals"].astype(int),
                "penalties_saved": mg["penalties_saved"].astype(int),
                "penalties_missed": mg["penalties_missed"].astype(int),
                "yellow_cards": mg["yellow_cards"].astype(int),
                "red_cards": mg["red_cards"].astype(int),
                "saves": mg["saves"].astype(int),
                "bonus": mg["bonus"].astype(int),
                "bps": mg["bps"].astype(int),
                "dc": mg["defensive_contribution"].astype("Int64"),
                "cbi": mg["clearances_blocks_interceptions"].astype("Int64"),
                "tackles": mg["tackles"].astype("Int64"),
                "recoveries": mg["recoveries"].astype("Int64"),
                "price": mg["value"].astype(int),
                "ownership_count": mg["selected"].astype("Int64"),
                "transfers_in": mg["transfers_in"].astype("Int64"),
                "transfers_out": mg["transfers_out"].astype("Int64"),
                "xg": mg["expected_goals"].astype("Float64"),
                "xa": mg["expected_assists"].astype("Float64"),
                "xgc": mg["expected_goals_conceded"].astype("Float64"),
                "influence": mg["influence"].astype("Float64"),
                "creativity": mg["creativity"].astype("Float64"),
                "threat": mg["threat"].astype("Float64"),
                "available_at": available_at,
                "finalized": fixture_final.fillna(False).astype(bool),
            }
        )
        .sort_values(["fixture_id", "player_code"])
        .reset_index(drop=True)
    )

    # ---------------------------------------------------------------- team_match
    tm_rows = []
    sums = pm.groupby(["fixture_id", "team_code"])[["xg", "threat"]].sum(min_count=1)
    for r in fixtures[fixtures["status"].isin(["final", "provisional"])].itertuples(index=False):
        for home in (True, False):
            t, o = (
                (r.home_team_code, r.away_team_code)
                if home
                else (r.away_team_code, r.home_team_code)
            )
            gf, ga = (r.home_score, r.away_score) if home else (r.away_score, r.home_score)
            xg_for = sums["xg"].get((r.fixture_id, t), np.nan)
            xg_against = sums["xg"].get((r.fixture_id, o), np.nan)
            tm_rows.append(
                {
                    "season": season,
                    "gw": int(r.gw),
                    "fixture_id": int(r.fixture_id),
                    "team_code": int(t),
                    "opponent_team_code": int(o),
                    "was_home": home,
                    "kickoff_at": r.kickoff_at,
                    "goals_for": int(gf),
                    "goals_against": int(ga),
                    "xg_for": _num(xg_for),
                    "xg_against": _num(xg_against),
                    "threat_for": _num(sums["threat"].get((r.fixture_id, t), np.nan)),
                    "threat_against": _num(sums["threat"].get((r.fixture_id, o), np.nan)),
                    "available_at": r.result_available_at,
                }
            )
    team_match = pd.DataFrame(tm_rows)
    if len(team_match):
        team_match = team_match.sort_values(["fixture_id", "team_code"]).reset_index(drop=True)

    # ---------------------------------------------------------------- snapshots & news
    snaps = pd.DataFrame()
    news = pd.DataFrame()
    if snapshot_captured_at is not None:
        cap = pd.Timestamp(snapshot_captured_at)
        snaps = (
            pd.DataFrame(
                {
                    "season": season,
                    "player_code": pr["code"].astype(int),
                    "team_code": pr["team_code"].astype(int),
                    "captured_at": cap,
                    "price": pr["now_cost"].astype(int),
                    "form": pr["form"].astype("Float64"),
                    "ownership": pr["selected_by_percent"].astype("Float64"),
                    "status": pr["status"].astype(str),
                    "news": pr["news"].fillna(""),
                    "news_added": pr["news_added"],
                    "chance_of_playing_next_round": pr["chance_of_playing_next_round"].astype(
                        "Int64"
                    ),
                    "set_piece_json": [
                        json.dumps(
                            {
                                "penalties_order": _num(r.get("penalties_order")),
                                "direct_freekicks_order": _num(r.get("direct_freekicks_order")),
                                "corners_and_indirect_freekicks_order": _num(
                                    r.get("corners_and_indirect_freekicks_order")
                                ),
                            },
                            sort_keys=True,
                        )
                        for _, r in pr.iterrows()
                    ],
                    "official_price_signal_json": [
                        json.dumps(_price_signal(r), sort_keys=True) for _, r in pr.iterrows()
                    ],
                    "source": f"{source}:bootstrap_snapshot",
                    "source_version": source_version or "",
                    "available_at": cap,
                }
            )
            .sort_values("player_code")
            .reset_index(drop=True)
        )
        has_news = snaps["news"].astype(str).str.len() > 0
        n = snaps[has_news]
        news = pd.DataFrame(
            {
                "id": [f"news_{season}_{c}_{int(cap.timestamp())}" for c in n["player_code"]],
                "season": season,
                "player_code": n["player_code"].to_numpy(),
                "source": "fpl_bootstrap",
                "publisher": "Fantasy Premier League",
                "published_at": n["news_added"].fillna(cap).to_numpy(),
                "structured_status": n["status"].map(_STATUS_TEXT).to_numpy(),
                "chance_of_playing": n["chance_of_playing_next_round"].to_numpy(),
                "evidence_strength": 1.0,
                "text": n["news"].to_numpy(),
                "available_at": cap,
            }
        )
        if len(news):
            news["published_at"] = pd.to_datetime(news["published_at"], utc=True)

    seasons = pd.DataFrame(
        [
            {
                "season": season,
                "ruleset_version": ruleset.ruleset_version,
                "started_at": fx["kickoff_time"].min().date(),
                "ended_at": fx["kickoff_time"].max().date(),
            }
        ]
    )
    return CanonicalSeason(
        season, seasons, gameweeks, teams, players, fixtures, pm, team_match, snaps, news
    )


_STATUS_TEXT = {
    "a": "available",
    "d": "doubtful",
    "i": "injured",
    "s": "suspended",
    "u": "unavailable",
    "n": "not_in_squad",
}


def _num(v: Any) -> Any:
    if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA:
        return None
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and v == 0.0:
        return 0.0  # normalise IEEE negative zero (JSONB stores -0 as 0)
    return v


def _price_signal(r: pd.Series) -> dict[str, Any]:
    proj_raw = r.get("price_change_projections")
    projections: Any = None
    if isinstance(proj_raw, str) and proj_raw.startswith("["):
        try:
            projections = ast.literal_eval(proj_raw)  # python-literal list of dicts (safe)
        except (ValueError, SyntaxError):
            projections = None
    return {"price_change_percent": _num(r.get("price_change_percent")), "projections": projections}


def utcnow() -> datetime:
    return datetime.now(UTC)
