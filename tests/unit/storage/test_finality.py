"""The shared result-finality rule (M1.4 journal, M1.5 season evaluation): only FPL's
finalised gameweek, final fixtures and result rows loaded after the finalisation lag together
are final; everything earlier has a named state, and a change of finality changes the digest."""

from __future__ import annotations

import pandas as pd

from fpl_storage.finality import gameweek_finality

SEASON, GW = "2026-27", 6


def frames(
    fixture_status: dict[int, tuple[str, bool]],
    rows_final: dict[int, bool] | None = None,
    recorded: tuple[int, ...] | None = None,
    gw_status: str = "finalized",
) -> dict[str, pd.DataFrame]:
    """Fixture 1 = club 10 v 11, fixture 2 = club 20 v 21; two result rows per recorded
    fixture."""
    fx = pd.DataFrame(
        [
            {
                "season": SEASON,
                "fixture_id": fid,
                "gw": GW,
                "home_team_code": home,
                "away_team_code": home + 1,
                "status": st,
                "finalized": fin,
            }
            for fid, home, (st, fin) in ((1, 10, fixture_status[1]), (2, 20, fixture_status[2]))
        ]
    )
    rec = (
        recorded
        if recorded is not None
        else tuple(f for f, (st, _) in fixture_status.items() if st in ("final", "provisional"))
    )
    pm = pd.DataFrame(
        [
            {
                "season": SEASON,
                "gw": GW,
                "fixture_id": f,
                "player_code": 100 * f + i,
                "minutes": 90,
                "points": 2,
                "finalized": (rows_final or {}).get(f, fixture_status[f][1]),
            }
            for f in rec
            for i in range(2)
        ],
        columns=["season", "gw", "fixture_id", "player_code", "minutes", "points", "finalized"],
    )
    gws = pd.DataFrame([{"season": SEASON, "gw": GW, "status": gw_status}])
    return {"fixtures": fx, "player_match": pm, "gameweeks": gws}


FINAL = {1: ("final", True), 2: ("final", True)}


def test_final_needs_the_gameweek_the_fixtures_and_the_rows() -> None:
    f = gameweek_finality(frames(FINAL), SEASON, GW)
    assert f.final and f.state() == "final"
    assert f.counts() == {
        "relevant": 2,
        "played": 2,
        "recorded": 2,
        "final": 2,
        "results_final": 2,
        "gameweek_status": "finalized",
    }
    # FPL finalised everything, but the rows were loaded before the finalisation lag
    early = gameweek_finality(frames(FINAL, rows_final={2: False}), SEASON, GW)
    assert not early.final and early.state() == "results_not_final"
    assert early.digest_state() != f.digest_state()
    # rows final but FPL has not data-checked the gameweek
    unchecked = gameweek_finality(frames(FINAL, gw_status="provisional"), SEASON, GW)
    assert not unchecked.final and unchecked.state() == "provisional"


def test_states_before_the_results_are_complete() -> None:
    upcoming = {1: ("scheduled", False), 2: ("scheduled", False)}
    assert (
        gameweek_finality(frames(upcoming, gw_status="live"), SEASON, GW).state() == "not_started"
    )
    half = {1: ("final", True), 2: ("scheduled", False)}
    assert gameweek_finality(frames(half, gw_status="live"), SEASON, GW).state() == "in_progress"
    # played but no result rows loaded yet
    unloaded = gameweek_finality(frames(FINAL, recorded=(1,)), SEASON, GW)
    assert unloaded.state() == "in_progress" and not unloaded.complete
    prov = {1: ("provisional", False), 2: ("provisional", False)}
    assert (
        gameweek_finality(frames(prov, gw_status="provisional"), SEASON, GW).state()
        == "provisional"
    )


def test_a_subset_of_clubs_is_judged_on_its_own_fixtures() -> None:
    half = {1: ("final", True), 2: ("scheduled", False)}
    ds = frames(half, gw_status="live")
    club_10 = gameweek_finality(ds, SEASON, GW, teams=[10])
    assert club_10.fixtures == {1} and club_10.complete
    assert not club_10.final  # FPL has not finalised the gameweek
    assert gameweek_finality(ds, SEASON, GW, teams=[20]).state() == "not_started"
