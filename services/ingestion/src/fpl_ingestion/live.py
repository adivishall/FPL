"""Canonicalise live FPL API payloads into the same canonical frames as historical data.

Live captures produce:
* teams / players / gameweeks (official deadlines from ``events``),
* fixtures with schedule availability = capture time (what was published when we looked),
* ``player_snapshots`` (price, ownership, status, news, set pieces, official price signal),
* ``news_signals`` for non-empty news,
* ``player_match`` rows from ``event/{gw}/live`` for players with exactly one fixture in the
  gameweek. Players in a double gameweek are not split from the aggregated live payload; this is
  recorded as a data-quality warning and their rows arrive with the historical import.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd

from fpl_domain.enums import Position, Severity
from fpl_domain.rules import Ruleset
from fpl_ingestion.canonical import CanonicalSeason
from fpl_ingestion.contracts import Issue
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


def player_match_from_live(
    live: s.EventLive,
    gameweek: int,
    boot: s.BootstrapStatic,
    fixtures: list[s.ApiFixture],
    ruleset: Ruleset,
) -> tuple[pd.DataFrame, list[Issue]]:
    """Per-player match rows for a gameweek from ``event/{gw}/live`` (single-fixture players)."""
    issues: list[Issue] = []
    code_by_tid = {t.id: t.code for t in boot.teams}
    el = {e.id: e for e in boot.elements}
    gw_fx: dict[int, list[s.ApiFixture]] = {}
    for f in fixtures:
        if f.event == gameweek:
            gw_fx.setdefault(f.team_h, []).append(f)
            gw_fx.setdefault(f.team_a, []).append(f)
    prov = timedelta(minutes=ruleset.timing.provisional_lag_minutes)
    rows, skipped_dgw = [], 0
    for item in live.elements:
        e = el.get(item.id)
        if e is None or e.element_type not in (1, 2, 3, 4):
            continue
        fxs = gw_fx.get(e.team, [])
        if len(fxs) != 1:
            skipped_dgw += int(len(fxs) > 1)
            continue
        f = fxs[0]
        st = item.stats
        home = f.team_h == e.team
        rows.append(
            {
                "season": ruleset.season,
                "gw": gameweek,
                "fixture_id": f.id,
                "player_code": e.code,
                "team_code": e.team_code,
                "opponent_team_code": code_by_tid[f.team_a if home else f.team_h],
                "was_home": home,
                "kickoff_at": f.kickoff_time,
                "minutes": st.minutes,
                "starts": st.starts,
                "points": st.total_points,
                "goals": st.goals_scored,
                "assists": st.assists,
                "clean_sheets": st.clean_sheets,
                "goals_conceded": st.goals_conceded,
                "own_goals": st.own_goals,
                "penalties_saved": st.penalties_saved,
                "penalties_missed": st.penalties_missed,
                "yellow_cards": st.yellow_cards,
                "red_cards": st.red_cards,
                "saves": st.saves,
                "bonus": st.bonus,
                "bps": st.bps,
                "dc": st.defensive_contribution,
                "cbi": st.clearances_blocks_interceptions,
                "tackles": st.tackles,
                "recoveries": st.recoveries,
                "price": e.now_cost,
                "ownership_count": None,
                "transfers_in": e.transfers_in_event,
                "transfers_out": e.transfers_out_event,
                "xg": st.expected_goals,
                "xa": st.expected_assists,
                "xgc": st.expected_goals_conceded,
                "influence": None,
                "creativity": None,
                "threat": None,
                "available_at": (pd.Timestamp(f.kickoff_time) + prov) if f.kickoff_time else None,
                "finalized": bool(f.finished),
            }
        )
    if skipped_dgw:
        issues.append(
            Issue(
                "live_dgw_not_split",
                Severity.WARNING,
                "player_gw_stats",
                f"{skipped_dgw} players in double gameweeks need per-fixture "
                "splitting from the explain payload; deferred to the "
                "end-of-season historical import",
                count=skipped_dgw,
            )
        )
    return pd.DataFrame(rows), issues
