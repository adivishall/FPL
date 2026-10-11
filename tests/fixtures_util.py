"""Helpers to load the committed real-data excerpts through the production pipeline code."""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import yaml

from fpl_domain.rules import load_ruleset
from fpl_ingestion.canonical import CanonicalSeason, PitPolicy, canonicalize
from fpl_ingestion.pipeline import parse_and_validate
from fpl_ingestion.quality import SeasonFrames, run_quality_gates
from fpl_ingestion.sources.historical import HistoricalRepoSource, LocalDirTransport
from fpl_storage.dataset import CanonicalDataset

ROOT = Path(__file__).resolve().parents[1]
VAASTAV_DIR = ROOT / "data" / "fixtures" / "vaastav"
API_DIR = ROOT / "data" / "fixtures" / "fpl_api"
SNAPSHOT_AT = datetime(2026, 8, 28, 9, 47, 53, tzinfo=UTC)


def sources_cfg() -> dict:
    return yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))


def local_source() -> HistoricalRepoSource:
    return HistoricalRepoSource.from_config(sources_cfg(), LocalDirTransport(VAASTAV_DIR))


def validated_frames(season: str) -> SeasonFrames:
    src = local_source()
    frames = parse_and_validate(season, src.fetch_season(season))
    return run_quality_gates(frames)


@lru_cache(maxsize=8)
def canonical_season(season: str) -> CanonicalSeason:
    cfg = sources_cfg()
    frames = validated_frames(season)
    snap = SNAPSHOT_AT if season == "2026-27" else None
    return canonicalize(
        frames,
        load_ruleset(season),
        PitPolicy.from_config(cfg),
        SNAPSHOT_AT,
        snapshot_captured_at=snap,
        source="vaastav",
        source_version=cfg["historical_repo"]["commit"],
    )


def fixture_dataset(*seasons: str) -> CanonicalDataset:
    parts = [CanonicalDataset(canonical_season(s).frames()) for s in seasons]
    return CanonicalDataset.concat(parts)


_OUTCOME_ZEROES = (
    "goals",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "saves",
    "bonus",
    "bps",
    "dc",
    "cbi",
    "tackles",
    "recoveries",
)


def with_gameweek_results(
    ds: CanonicalDataset,
    season: str,
    gw: int,
    points_of: Callable[[int], int],
    minutes_of: Callable[[int], int] = lambda _c: 90,
    fixture_ids: Collection[int] | None = None,
    final: bool = True,
) -> CanonicalDataset:
    """``ds`` with synthetic official results for one gameweek (tests and the e2e stack only).
    The chosen fixtures (default: all of the gameweek's) are played — scored, ``final`` (or
    provisional) — and every registered player of both clubs gets a per-fixture row with
    ``points_of(code)`` points and ``minutes_of(code)`` minutes. The gameweek is ``finalized``
    once all of its fixtures are final, ``provisional`` when all are played but not final, and
    ``live`` while some are still to come."""
    import pandas as pd

    frames = {k: v.copy() for k, v in ds.frames.items()}
    fx = frames["fixtures"]
    sel = (fx["season"] == season) & (fx["gw"] == gw)
    every = {int(i) for i in fx.loc[sel, "fixture_id"]}
    chosen = every if fixture_ids is None else {int(i) for i in fixture_ids}
    played = sel & fx["fixture_id"].isin(sorted(chosen))
    fx.loc[played, "status"] = "final" if final else "provisional"
    fx.loc[played, "finalized"] = final
    fx.loc[played, "home_score"] = 1
    fx.loc[played, "away_score"] = 0
    fx.loc[played, "result_available_at"] = fx.loc[played, "kickoff_at"] + pd.Timedelta(minutes=150)
    g = frames["gameweeks"]
    gsel = (g["season"] == season) & (g["gw"] == gw)
    if chosen >= every:
        g.loc[gsel, "status"] = "finalized" if final else "provisional"
        if final:
            g.loc[gsel, "finalized_at"] = fx.loc[sel, "kickoff_at"].max() + pd.Timedelta(hours=72)
    else:
        g.loc[gsel, "status"] = "live"
    pm = frames["player_match"]
    players = frames["players"]
    players = players[players["season"] == season]
    rows = []
    for r in fx[played].itertuples(index=False):
        for team, opp, home in (
            (r.home_team_code, r.away_team_code, True),
            (r.away_team_code, r.home_team_code, False),
        ):
            for code in players.loc[players["team_code"] == team, "player_code"]:
                minutes = int(minutes_of(int(code)))
                rows.append(
                    {
                        "season": season,
                        "gw": gw,
                        "fixture_id": int(r.fixture_id),
                        "player_code": int(code),
                        "team_code": int(team),
                        "opponent_team_code": int(opp),
                        "was_home": home,
                        "kickoff_at": r.kickoff_at,
                        "minutes": minutes,
                        "starts": 1 if minutes >= 60 else 0,
                        "points": int(points_of(int(code))),
                        "available_at": r.kickoff_at + pd.Timedelta(minutes=150),
                        "finalized": final,
                    }
                )
    template = pm[pm["season"] == season].iloc[[0]]
    new = template.loc[template.index.repeat(len(rows))].reset_index(drop=True)
    for col in rows[0] if rows else ():
        new[col] = pd.array([row[col] for row in rows], dtype=pm[col].dtype)
    for col in _OUTCOME_ZEROES:
        new[col] = pd.array([0] * len(new), dtype=pm[col].dtype)
    frames["player_match"] = pd.concat([pm, new], ignore_index=True)
    return CanonicalDataset(frames, dict(ds.meta))
