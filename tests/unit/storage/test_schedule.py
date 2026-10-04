"""Schedule rule S1: archived final schedules are shown as they were published (ADR-0004)."""

from __future__ import annotations

import pandas as pd
import pytest

from fpl_storage.schedule import believed_schedule, reconstruct

T0 = pd.Timestamp("2030-08-10 14:00", tz="UTC")
WEEK = pd.Timedelta(days=7)
PUBLISHED = T0 - pd.Timedelta(days=60)


def _round_robin(n_teams: int = 6) -> list[tuple[int, int, int]]:
    """Double round robin by the circle method: (round, home, away), rounds 1..2(n-1)."""
    teams = list(range(1, n_teams + 1))
    rounds = []
    for r in range(n_teams - 1):
        pairs = [(teams[i], teams[-1 - i]) for i in range(n_teams // 2)]
        rounds.append([(a, b) if r % 2 == 0 else (b, a) for a, b in pairs])
        teams = [teams[0], teams[-1], *teams[1:-1]]
    second = [[(b, a) for a, b in rnd] for rnd in rounds]
    return [(g + 1, h, a) for g, rnd in enumerate(rounds + second) for h, a in rnd]


def _season(moves: dict[int, tuple[int, pd.Timedelta]] | None = None) -> pd.DataFrame:
    """Final schedule after moving fixture ids to (new round, kickoff offset in that round)."""
    rows = []
    for fid, (rnd, h, a) in enumerate(_round_robin(), start=1):
        g, ko = rnd, T0 + (rnd - 1) * WEEK
        if moves and fid in moves:
            g, off = moves[fid]
            ko = T0 + (g - 1) * WEEK + off
        rows.append(
            {
                "season": "2030-31",
                "fixture_id": fid,
                "gw": g,
                "kickoff_at": ko,
                "home_team_code": h,
                "away_team_code": a,
                "schedule_available_at": PUBLISHED,
            }
        )
    return pd.DataFrame(rows).astype({"gw": "Int64"})


def _gameweeks(fx: pd.DataFrame) -> pd.DataFrame:
    g = fx.groupby("gw")["kickoff_at"].min()
    return pd.DataFrame(
        {"season": "2030-31", "gw": g.index.astype(int), "deadline_at": g - pd.Timedelta("90min")}
    )


def _fid(fx: pd.DataFrame, gw: int, k: int = 0) -> int:
    return int(fx[fx["gw"] == gw].sort_values("fixture_id")["fixture_id"].iloc[k])


def test_unmoved_season_is_published_once_and_unchanged() -> None:
    fx = _season()
    rec = reconstruct("2030-31", fx, _gameweeks(fx))
    assert rec.status == "reconstructed" and rec.moves == {}
    seen = believed_schedule(fx, _gameweeks(fx), PUBLISHED)
    assert len(seen) == len(fx)
    assert believed_schedule(fx, _gameweeks(fx), PUBLISHED - pd.Timedelta("1s")).empty


def test_postponed_fixture_stays_in_its_original_round_until_its_deadline() -> None:
    base = _season()
    fid = _fid(base, 3)
    fx = _season({fid: (7, pd.Timedelta(days=3))})  # postponed from round 3 into round 7
    gws = _gameweeks(fx)
    mv = reconstruct("2030-31", fx, gws).moves[fid]
    assert (mv.original_gw, mv.final_gw) == (3, 7)
    deadline3 = gws.set_index("gw")["deadline_at"][3]
    assert mv.known_at == deadline3  # announced on match day: not knowable before the deadline
    before = believed_schedule(fx, gws, deadline3 - pd.Timedelta("2h"))
    row = before.set_index("fixture_id").loc[fid]
    assert row["gw"] == 3 and abs(row["kickoff_at"] - (T0 + 2 * WEEK)) < pd.Timedelta("1D")
    # each team plays exactly once in every round of the believed schedule
    long = pd.concat(
        [
            before[["gw", "home_team_code"]].set_axis(["gw", "t"], axis=1),
            before[["gw", "away_team_code"]].set_axis(["gw", "t"], axis=1),
        ]
    )
    assert (long.groupby(["gw", "t"]).size() == 1).all()
    # after the deadline the fixture is unscheduled until the new date is announced
    between = believed_schedule(fx, gws, deadline3 + pd.Timedelta("1h"))
    assert fid not in set(between["fixture_id"])
    after = believed_schedule(fx, gws, mv.announced_at)
    assert after.set_index("fixture_id").loc[fid, "gw"] == 7
    assert mv.announced_at == fx.set_index("fixture_id").loc[fid, "kickoff_at"] - pd.Timedelta(
        days=28
    )


def test_double_gameweek_only_gates_the_moved_fixture() -> None:
    base = _season()
    fid = _fid(base, 3)
    fx = _season({fid: (7, pd.Timedelta(days=3))})
    gws = _gameweeks(fx)
    early = believed_schedule(fx, gws, T0)  # long before round 7 is announced
    regular_round7 = set(fx[(fx["gw"] == 7) & (fx["fixture_id"] != fid)]["fixture_id"])
    assert regular_round7 <= set(early["fixture_id"])  # no false blanks for the doubling teams


def test_fixture_brought_forward_is_known_when_announced() -> None:
    base = _season()
    fid = _fid(base, 9)
    fx = _season({fid: (5, pd.Timedelta(days=3))})  # brought forward from round 9 to round 5
    gws = _gameweeks(fx)
    mv = reconstruct("2030-31", fx, gws).moves[fid]
    assert (mv.original_gw, mv.final_gw) == (9, 5)
    assert mv.known_at == mv.announced_at < gws.set_index("gw")["deadline_at"][5]
    assert (
        believed_schedule(fx, gws, mv.announced_at - pd.Timedelta("1s"))
        .set_index("fixture_id")
        .loc[fid, "gw"]
        == 9
    )


def test_chained_moves_are_reconstructed() -> None:
    """Round 4's fixture A moves to round 10; round 2's fixture B (sharing a team with A) moves
    into round 4 — that team then has one fixture in round 4 although both were moved."""
    base = _season()
    a = _fid(base, 4)
    ha = set(base.set_index("fixture_id").loc[a, ["home_team_code", "away_team_code"]])
    r2 = base[base["gw"] == 2]
    b = int(
        r2[(r2["home_team_code"].isin(ha)) | (r2["away_team_code"].isin(ha))]["fixture_id"].iloc[0]
    )
    fx = _season({a: (10, pd.Timedelta(days=3)), b: (4, pd.Timedelta(days=3))})
    rec = reconstruct("2030-31", fx, _gameweeks(fx))
    assert rec.status == "reconstructed"
    assert {k: (m.original_gw, m.final_gw) for k, m in rec.moves.items()} == {
        a: (4, 10),
        b: (2, 4),
    }


def test_ambiguous_reconstruction_is_refused_not_guessed() -> None:
    """Both legs of a pairing in one round (round 8 mirrors round 3 here): either could have
    been the moved one, so the archive's timestamps are kept instead of a guess."""
    base = _season()
    fid = _fid(base, 3)
    fx = _season({fid: (8, pd.Timedelta(days=3))})
    rec = reconstruct("2030-31", fx, _gameweeks(fx))
    assert rec.status == "ambiguous" and rec.moves == {}


def test_incomplete_or_live_season_keeps_captured_timestamps() -> None:
    fx = _season().iloc[:-3]
    rec = reconstruct("2030-31", fx, _gameweeks(fx))
    assert rec.status == "incomplete"
    late = fx.assign(schedule_available_at=T0 + 3 * WEEK)
    assert believed_schedule(late, _gameweeks(late), T0).empty


@pytest.mark.parametrize("fid_round", [3, 6])
def test_reconstruction_is_memoised_and_deterministic(fid_round: int) -> None:
    base = _season()
    fid = _fid(base, fid_round)
    fx = _season({fid: (10, pd.Timedelta(days=2))})
    a = reconstruct("2030-31", fx, _gameweeks(fx))
    b = reconstruct("2030-31", fx.sample(frac=1, random_state=1), _gameweeks(fx))
    assert a is b
