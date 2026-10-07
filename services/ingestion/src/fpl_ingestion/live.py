"""Canonicalise live FPL API payloads into the same canonical frames as historical data.

The hourly bootstrap capture (``canonicalize_bootstrap``) produces:
* teams / players / gameweeks (official deadlines from ``events``),
* fixtures with schedule availability = capture time (what was published when we looked),
* ``player_snapshots`` (price, ownership, status, news, set pieces, official price signal),
* ``news_signals`` for non-empty news.

Completed-match results come from ``element-summary/{id}`` histories (``results_frames``): the
very records the historical archive's ``merged_gw`` is built from — per fixture (double
gameweeks split), with the price, ownership and transfers *at that fixture* — so the archive's
contracts, quality gates and canonicaliser apply to them unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from fpl_domain.enums import Position
from fpl_domain.rules import Ruleset
from fpl_ingestion.canonical import CanonicalSeason
from fpl_ingestion.contracts import CONTRACTS, Issue
from fpl_ingestion.quality import POS_BY_TYPE, SeasonFrames
from fpl_ingestion.sources import fpl_api_schemas as s

_STATUS_TEXT = {
    "a": "available",
    "d": "doubtful",
    "i": "injured",
    "s": "suspended",
    "u": "unavailable",
    "n": "not_in_squad",
}


def season_code_from_events(events: list[s.ApiEvent]) -> str:
    first = min(e.deadline_time for e in events)
    y = first.year if first.month >= 6 else first.year - 1
    return f"{y}-{str(y + 1)[-2:]}"


def canonicalize_bootstrap(
    boot: s.BootstrapStatic,
    fixtures: list[s.ApiFixture],
    captured_at: datetime,
    ruleset: Ruleset,
    source_version: str = "live",
) -> tuple[CanonicalSeason, list[Issue]]:
    issues: list[Issue] = []
    season = ruleset.season
    cap = pd.Timestamp(captured_at)
    timing = ruleset.timing
    code_by_tid = {t.id: t.code for t in boot.teams}

    teams = pd.DataFrame(
        [
            {
                "season": season,
                "team_code": t.code,
                "source_id": t.id,
                "name": t.name,
                "short_name": t.short_name,
                "strength_json": json.dumps(
                    {k: v for k, v in t.model_dump().items() if k.startswith("strength")},
                    sort_keys=True,
                ),
            }
            for t in boot.teams
        ]
    )

    elements = [e for e in boot.elements if e.element_type in (1, 2, 3, 4)]
    players = pd.DataFrame(
        [
            {
                "season": season,
                "player_code": e.code,
                "source_id": e.id,
                "team_code": e.team_code,
                "web_name": e.web_name,
                "first_name": e.first_name,
                "last_name": e.second_name,
                "position": Position.from_element_type(e.element_type).value,
                "price": e.now_cost,
                "status": e.status,
                "source_updated_at": cap,
            }
            for e in elements
        ]
    )

    kickoffs: dict[int, list[datetime]] = {}
    for f in fixtures:
        if f.event is not None and f.kickoff_time is not None:
            kickoffs.setdefault(f.event, []).append(f.kickoff_time)
    gws = []
    for ev in boot.events:
        ks = kickoffs.get(ev.id, [])
        last = max(ks) if ks else None
        fin = (
            pd.Timestamp(last) + timedelta(hours=timing.finalization_lag_hours)
            if last and ev.finished
            else None
        )
        status = (
            "finalized"
            if ev.finished and ev.data_checked
            else "provisional"
            if ev.finished
            else "live"
            if ev.deadline_time <= captured_at
            else "upcoming"
        )
        gws.append(
            {
                "season": season,
                "gw": ev.id,
                "deadline_at": ev.deadline_time,
                "first_kickoff_at": min(ks) if ks else None,
                "last_kickoff_at": last,
                "finalized_at": fin,
                "status": status,
                "deadline_source": "official",
            }
        )
    gameweeks = pd.DataFrame(gws)

    prov = timedelta(minutes=timing.provisional_lag_minutes)
    fx_rows = []
    for f in fixtures:
        finished = f.finished or f.finished_provisional
        fx_rows.append(
            {
                "season": season,
                "fixture_id": f.id,
                "gw": f.event,
                "kickoff_at": f.kickoff_time,
                "home_team_code": code_by_tid[f.team_h],
                "away_team_code": code_by_tid[f.team_a],
                "status": (
                    "unscheduled"
                    if f.event is None
                    else "final"
                    if f.finished
                    else "provisional"
                    if f.finished_provisional
                    else "scheduled"
                ),
                "finalized": bool(f.finished),
                "home_score": f.team_h_score if finished else None,
                "away_score": f.team_a_score if finished else None,
                "home_difficulty": f.team_h_difficulty,
                "away_difficulty": f.team_a_difficulty,
                # Live: the schedule is known as published at capture time (or earlier).
                "schedule_available_at": cap,
                "result_available_at": (
                    pd.Timestamp(f.kickoff_time) + prov if finished and f.kickoff_time else None
                ),
            }
        )
    fixtures_df = pd.DataFrame(fx_rows)

    snaps = pd.DataFrame(
        [
            {
                "season": season,
                "player_code": e.code,
                "team_code": e.team_code,
                "captured_at": cap,
                "price": e.now_cost,
                "form": e.form,
                "ownership": e.selected_by_percent,
                "status": e.status,
                "news": e.news or "",
                "news_added": e.news_added,
                "chance_of_playing_next_round": e.chance_of_playing_next_round,
                "set_piece_json": json.dumps(
                    {
                        "penalties_order": e.penalties_order,
                        "direct_freekicks_order": e.direct_freekicks_order,
                        "corners_and_indirect_freekicks_order": (
                            e.corners_and_indirect_freekicks_order
                        ),
                    },
                    sort_keys=True,
                ),
                "official_price_signal_json": json.dumps(
                    {
                        "price_change_percent": e.price_change_percent,
                        "projections": [p.model_dump() for p in e.price_change_projections]
                        if e.price_change_projections
                        else None,
                    },
                    sort_keys=True,
                ),
                "source": "fpl_api:bootstrap",
                "source_version": source_version,
                "available_at": cap,
            }
            for e in elements
        ]
    )
    with_news = [e for e in elements if e.news]
    news = pd.DataFrame(
        [
            {
                "id": f"news_{season}_{e.code}_{int(cap.timestamp())}",
                "season": season,
                "player_code": e.code,
                "source": "fpl_bootstrap",
                "publisher": "Fantasy Premier League",
                "published_at": e.news_added or captured_at,
                "structured_status": _STATUS_TEXT.get(e.status, "unknown"),
                "chance_of_playing": e.chance_of_playing_next_round,
                "evidence_strength": 1.0,
                "text": e.news,
                "available_at": cap,
            }
            for e in with_news
        ]
    )

    ks_all = [f.kickoff_time for f in fixtures if f.kickoff_time]
    seasons = pd.DataFrame(
        [
            {
                "season": season,
                "ruleset_version": ruleset.ruleset_version,
                "started_at": min(ks_all).date() if ks_all else None,
                "ended_at": max(ks_all).date() if ks_all else None,
            }
        ]
    )
    empty = pd.DataFrame()
    return CanonicalSeason(
        season, seasons, gameweeks, teams, players, fixtures_df, empty, empty, snaps, news
    ), issues


def results_frames(
    season: str,
    bootstrap: dict[str, Any],
    fixtures: list[dict[str, Any]],
    histories: dict[int, list[dict[str, Any]]],
) -> SeasonFrames:
    """The archive's four input files rebuilt from live payloads (same columns and meaning),
    validated by the archive's contracts. ``histories`` maps element id → its history rows."""
    teams = pd.DataFrame(bootstrap["teams"])
    players = pd.DataFrame(bootstrap["elements"])
    team_name = teams.set_index("id")["name"]
    el = players.set_index("id")
    rows = []
    for eid, hist in sorted(histories.items()):
        if eid not in el.index:
            continue
        e = el.loc[eid]
        for h in hist:
            rows.append(
                {
                    **h,
                    "GW": h["round"],
                    "name": f"{e['first_name']} {e['second_name']}",
                    "position": POS_BY_TYPE[int(e["element_type"])],
                    "team": team_name[int(e["team"])],
                }
            )
    parsed, issues = {}, []
    for key, df in (
        ("merged_gw", pd.DataFrame(rows)),
        ("fixtures", pd.DataFrame(fixtures)),
        ("teams", teams),
        ("players_raw", players),
    ):
        parsed[key], found = CONTRACTS[key].validate(df)
        issues.extend(found)
    return SeasonFrames(
        season=season,
        merged_gw=parsed["merged_gw"],
        fixtures=parsed["fixtures"],
        teams=parsed["teams"],
        players_raw=parsed["players_raw"],
        issues=issues,
    )
