"""Canonicalisation and availability timestamps (ADR-0004, §7)."""

from __future__ import annotations

import pandas as pd

from tests.fixtures_util import SNAPSHOT_AT, canonical_season


def test_deadline_is_ninety_minutes_before_first_kickoff() -> None:
    c = canonical_season("2025-26")
    g = c.gameweeks.set_index("gw")
    first = c.fixtures.groupby("gw")["kickoff_at"].min()
    assert ((first - g["deadline_at"]) == pd.Timedelta(minutes=90)).all()
    assert g.loc[1, "deadline_at"] == pd.Timestamp("2025-08-15T17:30:00Z")


def test_match_stats_available_only_after_kickoff() -> None:
    pm = canonical_season("2025-26").player_match
    assert (pm["available_at"] > pm["kickoff_at"]).all()
    assert ((pm["available_at"] - pm["kickoff_at"]) == pd.Timedelta(minutes=150)).all()


def test_results_available_after_kickoff_and_schedule_before_season() -> None:
    fx = canonical_season("2025-26").fixtures
    assert (fx["result_available_at"] > fx["kickoff_at"]).all()
    assert (fx["schedule_available_at"] < fx["kickoff_at"].min()).all()


def test_team_identity_uses_stable_codes() -> None:
    c = canonical_season("2025-26")
    team_codes = set(c.teams["team_code"])
    assert set(c.player_match["team_code"]) <= team_codes
    assert set(c.player_match["opponent_team_code"]) <= team_codes
    assert set(c.player_match["player_code"]) <= set(c.players["player_code"])


def test_one_row_per_player_fixture_after_dedup() -> None:
    pm = canonical_season("2025-26").player_match
    assert not pm.duplicated(["fixture_id", "player_code"]).any()
    starters = pm.groupby(["fixture_id", "team_code"])["starts"].sum()
    assert (starters == 11).all()


def test_scores_reconcile_with_player_goals() -> None:
    c = canonical_season("2025-26")
    tm = c.team_match.set_index(["fixture_id", "team_code"])
    pm = c.player_match
    goals = pm.groupby(["fixture_id", "team_code"])["goals"].sum()
    ogs = pm.groupby(["fixture_id", "opponent_team_code"])["own_goals"].sum()
    for (fid, team), row in tm.iterrows():
        expected = goals.get((fid, team), 0) + ogs.get((fid, team), 0)
        assert row["goals_for"] == expected


def test_2026_27_snapshot_is_point_in_time() -> None:
    c = canonical_season("2026-27")
    assert len(c.player_snapshots) == 616
    assert (c.player_snapshots["available_at"] == pd.Timestamp(SNAPSHOT_AT)).all()
    gw = c.gameweeks.set_index("gw")
    assert gw.loc[1, "status"] == "finalized"
    assert gw.loc[2, "status"] == "upcoming"
    assert gw.loc[2, "deadline_at"] > pd.Timestamp(SNAPSHOT_AT)
    assert len(c.news_signals) > 0
    assert set(c.news_signals["structured_status"]) <= {
        "available",
        "doubtful",
        "injured",
        "suspended",
        "unavailable",
        "not_in_squad",
    }


def test_official_price_signal_preserved_separately() -> None:
    import json

    snap = canonical_season("2026-27").player_snapshots
    sig = snap["official_price_signal_json"].map(json.loads)
    assert sig.map(lambda d: d["price_change_percent"] is not None).mean() > 0.9
