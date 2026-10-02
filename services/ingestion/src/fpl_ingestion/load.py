"""Load canonical frames into PostgreSQL (idempotent; §55)."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from fpl_storage import models as m
from fpl_storage import repositories as repo
from fpl_storage.dataset import CanonicalDataset

UNDERLYING_SOURCE = "fpl"  # FPL-published Opta metrics (via the historical repository)
UNDERLYING_PRIORITY = 100


def _py(v: Any) -> Any:
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, float) and np.isnan(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if isinstance(v, np.generic):
        return v.item()
    return v


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [{k: _py(v) for k, v in r.items()} for r in df.to_dict("records")]


def _date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    return datetime.fromisoformat(str(v)).date()


# Which raw source file each canonical table is derived from (lineage, §76.2).
RAW_FILE_FOR_TABLE = {
    "fixtures": "fixtures",
    "teams": "teams",
    "players": "players_raw",
    "player_gw_stats": "merged_gw",
    "underlying_stats": "merged_gw",
    "team_gw_stats": "merged_gw",
    "player_snapshots": "players_raw",
}


def load_canonical(
    session: Session,
    ds: CanonicalDataset,
    raw_ids: dict[str, str] | None = None,
    *,
    source: str = "vaastav",
    source_priority: dict[str, int] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, int]]:
    """Upsert every canonical table of ``ds``; returns per-table counts.

    ``raw_ids`` maps source file keys (``merged_gw``, ``fixtures`` …) to raw snapshot ids so
    each canonical row records the exact payload it came from.
    """
    counts: dict[str, dict[str, int]] = {}
    raw_ids = raw_ids or {}

    def put(
        model: type[m.Base],
        rows: list[dict[str, Any]],
        key: list[str],
        name: str,
        hash_exclude: tuple[str, ...] = (),
    ) -> None:
        raw = raw_ids.get(RAW_FILE_FOR_TABLE.get(name, ""))
        res = repo.upsert_canonical(
            session,
            model,
            rows,
            key,
            raw_snapshot_id=raw,
            hash_exclude=hash_exclude,
            source_priority=source_priority,
        )
        if conflicts is not None:
            conflicts.extend(res.conflicts)
        prev = counts.get(name, {})
        counts[name] = {k: prev.get(k, 0) + v for k, v in res.as_dict().items()}

    seasons = [
        {
            "season_code": s["season"],
            "ruleset_version": s["ruleset_version"],
            "started_at": _date(s["started_at"]),
            "ended_at": _date(s["ended_at"]),
            "schema_version": 1,
        }
        for s in _records(ds["seasons"])
    ]
    put(m.SeasonRow, seasons, ["season_code"], "seasons")

    for season in ds.seasons():
        sid = repo.season_id(session, season)

        def sel(t: str, season: str = season) -> pd.DataFrame:
            return ds[t][ds[t]["season"] == season]

        gws = [
            {
                "season_id": sid,
                "gw": r["gw"],
                "deadline_at": r["deadline_at"],
                "started_at": r["first_kickoff_at"],
                "ended_at": r["last_kickoff_at"],
                "locked_at": None,
                "finalized_at": r["finalized_at"],
                "status": r["status"],
                "deadline_source": r["deadline_source"],
            }
            for r in _records(sel("gameweeks"))
        ]
        put(m.GameweekRow, gws, ["season_id", "gw"], "gameweeks")

        teams = [
            {
                "season_id": sid,
                "source_id": r["source_id"],
                "code": r["team_code"],
                "name": r["name"],
                "short_name": r["short_name"],
                "strength_home": None,
                "strength_away": None,
                "strength_json": json.loads(r["strength_json"] or "{}"),
            }
            for r in _records(sel("teams"))
        ]
        put(m.TeamRow, teams, ["season_id", "source_id"], "teams")
        team_id = repo.team_ids_by_code(session, sid)

        players = [
            {
                "season_id": sid,
                "source_id": r["source_id"],
                "code": r["player_code"],
                "team_id": team_id[r["team_code"]],
                "first_name": r["first_name"],
                "last_name": r["last_name"],
                "web_name": r["web_name"],
                "position": r["position"],
                "price": r["price"],
                "status": r["status"],
                "source_updated_at": r["source_updated_at"],
            }
            for r in _records(sel("players"))
        ]
        # source_updated_at alone changing (a new capture) is not a content change.
        put(
            m.PlayerRow,
            players,
            ["season_id", "source_id"],
            "players",
            hash_exclude=("source_updated_at",),
        )

        fixtures = [
            {
                "season_id": sid,
                "source_id": r["fixture_id"],
                "gw": r["gw"],
                "kickoff_at": r["kickoff_at"],
                "home_team_id": team_id[r["home_team_code"]],
                "away_team_id": team_id[r["away_team_code"]],
                "status": r["status"],
                "finalized": r["finalized"],
                "home_score": r["home_score"],
                "away_score": r["away_score"],
                "home_difficulty": r["home_difficulty"],
                "away_difficulty": r["away_difficulty"],
                "schedule_available_at": r["schedule_available_at"],
                "result_available_at": r["result_available_at"],
                "source": source,
            }
            for r in _records(sel("fixtures"))
        ]
        put(m.FixtureRow, fixtures, ["season_id", "source_id"], "fixtures")
        fixture_id = repo.fixture_ids_by_source(session, sid)

        pm = _records(sel("player_match"))
        stats, under = [], []
        for r in pm:
            fid = fixture_id[r["fixture_id"]]
            stats.append(
                {
                    "season_id": sid,
                    "gw": r["gw"],
                    "fixture_id": fid,
                    "player_code": r["player_code"],
                    "team_code": r["team_code"],
                    "opponent_team_code": r["opponent_team_code"],
                    "was_home": r["was_home"],
                    "kickoff_at": r["kickoff_at"],
                    "minutes": r["minutes"],
                    "starts": r["starts"],
                    "points": r["points"],
                    "goals": r["goals"],
                    "assists": r["assists"],
                    "clean_sheets": r["clean_sheets"],
                    "goals_conceded": r["goals_conceded"],
                    "own_goals": r["own_goals"],
                    "penalties_saved": r["penalties_saved"],
                    "penalties_missed": r["penalties_missed"],
                    "yellow_cards": r["yellow_cards"],
                    "red_cards": r["red_cards"],
                    "saves": r["saves"],
                    "bonus": r["bonus"],
                    "bps": r["bps"],
                    "dc": r["dc"],
                    "cbi": r["cbi"],
                    "tackles": r["tackles"],
                    "recoveries": r["recoveries"],
                    "price": r["price"],
                    "ownership_count": r["ownership_count"],
                    "transfers_in": r["transfers_in"],
                    "transfers_out": r["transfers_out"],
                    "available_at": r["available_at"],
                    "finalized": r["finalized"],
                    "source": source,
                }
            )
            under.append(
                {
                    "season_id": sid,
                    "gw": r["gw"],
                    "fixture_id": fid,
                    "player_code": r["player_code"],
                    "source": UNDERLYING_SOURCE,
                    "source_priority": UNDERLYING_PRIORITY,
                    "confidence": 1.0,
                    "xg": r["xg"],
                    "xa": r["xa"],
                    "xgc": r["xgc"],
                    "shots": None,
                    "box_touches": None,
                    "key_passes": None,
                    "influence": r["influence"],
                    "creativity": r["creativity"],
                    "threat": r["threat"],
                    "set_piece_share": None,
                    "available_at": r["available_at"],
                }
            )
        put(
            m.PlayerGwStatsRow, stats, ["season_id", "fixture_id", "player_code"], "player_gw_stats"
        )
        put(
            m.UnderlyingStatsRow,
            under,
            ["season_id", "fixture_id", "player_code", "source"],
            "underlying_stats",
        )

        tms = [
            {
                "season_id": sid,
                "gw": r["gw"],
                "fixture_id": fixture_id[r["fixture_id"]],
                "team_code": r["team_code"],
                "was_home": r["was_home"],
                "goals_for": r["goals_for"],
                "goals_against": r["goals_against"],
                "xg_for": r["xg_for"],
                "xg_against": r["xg_against"],
                "shots_proxy": r["threat_for"],
                "available_at": r["available_at"],
            }
            for r in _records(sel("team_match"))
        ]
        put(m.TeamGwStatsRow, tms, ["season_id", "fixture_id", "team_code"], "team_gw_stats")

        snaps = [
            {
                "season_id": sid,
                "player_code": r["player_code"],
                "team_code": r["team_code"],
                "captured_at": r["captured_at"],
                "price": r["price"],
                "form": r["form"],
                "ownership": r["ownership"],
                "status": r["status"],
                "news": r["news"],
                "news_added": r["news_added"],
                "chance_of_playing_next_round": r["chance_of_playing_next_round"],
                "expected_minutes": None,
                "set_piece_json": json.loads(r["set_piece_json"] or "{}"),
                "official_price_signal_json": json.loads(r["official_price_signal_json"] or "{}"),
                "source": r["source"],
                "source_version": r["source_version"],
                "available_at": r["available_at"],
            }
            for r in _records(sel("player_snapshots"))
        ]
        put(
            m.PlayerSnapshotRow,
            snaps,
            ["season_id", "player_code", "captured_at", "source"],
            "player_snapshots",
        )

        news = [
            {
                "id": r["id"],
                "season_id": sid,
                "player_code": r["player_code"],
                "source": r["source"],
                "source_url": None,
                "publisher": r["publisher"],
                "published_at": r["published_at"],
                "structured_status": r["structured_status"],
                "chance_of_playing": r["chance_of_playing"],
                "evidence_strength": r["evidence_strength"],
                "expires_at": None,
                "text": r["text"],
                "available_at": r["available_at"],
            }
            for r in _records(sel("news_signals"))
        ]
        put(m.NewsSignalRow, news, ["id"], "news_signals")
    return counts
