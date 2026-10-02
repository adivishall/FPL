"""Scoring rules (§77.1 'All scoring-rule calculations') + reproduction of official points."""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from fpl_domain.enums import Position
from fpl_domain.rules import load_ruleset
from fpl_domain.scoring import (
    COMPONENTS,
    MatchEvents,
    ScoringTables,
    allocate_bonus,
    defensive_actions,
    position_codes,
    score_components,
    score_match,
    total_points,
)
from tests.fixtures_util import fixture_dataset

R26 = load_ruleset("2026-27")
R24 = load_ruleset("2024-25")


def pts(position: Position, rs=R26, **ev) -> dict:
    return score_match(MatchEvents(**ev), position, rs)


@pytest.mark.parametrize(("minutes", "expected"), [(0, 0), (1, 1), (59, 1), (60, 2), (90, 2)])
def test_appearance(minutes: int, expected: int) -> None:
    assert pts(Position.MID, minutes=minutes)["appearance"] == expected


@pytest.mark.parametrize(
    ("pos", "goal_pts", "rs"),
    [
        (Position.FWD, 4, R26),
        (Position.MID, 5, R26),
        (Position.DEF, 6, R26),
        (Position.GK, 10, R26),
        (Position.GK, 6, R24),
    ],
)
def test_goal_points_by_position_and_season(pos: Position, goal_pts: int, rs) -> None:
    assert pts(pos, rs, minutes=90, goals=2)["goals"] == 2 * goal_pts


def test_clean_sheet_requires_sixty_minutes_and_depends_on_position() -> None:
    assert pts(Position.DEF, minutes=59, clean_sheet=True)["clean_sheet"] == 0
    assert pts(Position.DEF, minutes=60, clean_sheet=True)["clean_sheet"] == 4
    assert pts(Position.GK, minutes=90, clean_sheet=True)["clean_sheet"] == 4
    assert pts(Position.MID, minutes=90, clean_sheet=True)["clean_sheet"] == 1
    assert pts(Position.FWD, minutes=90, clean_sheet=True)["clean_sheet"] == 0


def test_goals_conceded_and_saves() -> None:
    assert pts(Position.DEF, minutes=90, goals_conceded=3)["goals_conceded"] == -1
    assert pts(Position.GK, minutes=90, goals_conceded=4)["goals_conceded"] == -2
    assert pts(Position.MID, minutes=90, goals_conceded=4)["goals_conceded"] == 0
    assert pts(Position.GK, minutes=90, saves=8)["saves"] == 2


def test_discipline_and_penalties() -> None:
    p = pts(Position.FWD, minutes=90, yellow_cards=1, penalties_missed=1, own_goals=1)
    assert (p["yellow_cards"], p["penalty_misses"], p["own_goals"]) == (-1, -2, -2)
    assert pts(Position.GK, minutes=90, penalties_saved=1)["penalty_saves"] == 5
    assert pts(Position.DEF, minutes=30, red_cards=1)["red_cards"] == -3


def test_cards_count_without_minutes_but_nothing_else_does() -> None:
    p = pts(Position.DEF, minutes=0, yellow_cards=1, clean_sheet=True, bonus=0)
    assert p["total"] == -1 and p["appearance"] == 0 and p["clean_sheet"] == 0


def test_defensive_contribution_thresholds() -> None:
    assert pts(Position.DEF, minutes=90, cbi=7, tackles=3)["defensive_contribution"] == 2
    assert (
        pts(Position.DEF, minutes=90, cbi=7, tackles=2, recoveries=9)["defensive_contribution"] == 0
    )  # recoveries do not count for defenders
    assert (
        pts(Position.MID, minutes=90, cbi=4, tackles=3, recoveries=5)["defensive_contribution"] == 2
    )
    assert (
        pts(Position.MID, minutes=90, cbi=4, tackles=3, recoveries=4)["defensive_contribution"] == 0
    )
    assert pts(Position.GK, minutes=90, cbi=20, tackles=5)["defensive_contribution"] == 0
    assert pts(Position.DEF, R24, minutes=90, cbi=15, tackles=5)["defensive_contribution"] == 0


@pytest.mark.parametrize(
    ("bps", "expected"),
    [
        ([30, 20, 10, 5], [3, 2, 1, 0]),
        ([30, 30, 10, 5], [3, 3, 1, 0]),  # tie for first
        ([30, 20, 20, 5], [3, 2, 2, 0]),  # tie for second
        ([30, 20, 10, 10], [3, 2, 1, 1]),  # tie for third
        ([30, 30, 30, 5], [3, 3, 3, 0]),  # three-way tie for first
        ([5], [3]),
    ],
)
def test_bonus_tie_rules(bps: list[int], expected: list[int]) -> None:
    assert allocate_bonus(bps) == expected


@given(st.lists(st.integers(-10, 80), min_size=1, max_size=30))
def test_bonus_properties(bps: list[int]) -> None:
    b = allocate_bonus(bps)
    assert all(x in (0, 1, 2, 3) for x in b)
    order = sorted(range(len(bps)), key=lambda i: -bps[i])
    assert all(b[order[i]] >= b[order[i + 1]] for i in range(len(order) - 1))  # monotone
    assert all(
        b[i] == b[j] for i in range(len(bps)) for j in range(len(bps)) if bps[i] == bps[j]
    )  # equal BPS → equal bonus


def test_vectorised_matches_scalar() -> None:
    rng = np.random.default_rng(0)
    t = ScoringTables.from_ruleset(R26)
    n = 500
    pos = rng.integers(0, 4, n)
    ev = {
        k: rng.integers(0, hi, n)
        for k, hi in [
            ("minutes", 95),
            ("goals", 3),
            ("assists", 3),
            ("clean_sheet", 2),
            ("goals_conceded", 5),
            ("saves", 9),
            ("penalties_saved", 2),
            ("penalties_missed", 2),
            ("yellow_cards", 2),
            ("red_cards", 2),
            ("own_goals", 2),
            ("bonus", 4),
            ("cbi", 12),
            ("tackles", 6),
            ("recoveries", 10),
        ]
    }
    dca = defensive_actions(pos, ev["cbi"], ev["tackles"], ev["recoveries"], t)
    tot = total_points(
        score_components(
            t,
            pos,
            ev["minutes"],
            ev["goals"],
            ev["assists"],
            ev["clean_sheet"],
            ev["goals_conceded"],
            ev["saves"],
            ev["penalties_saved"],
            ev["penalties_missed"],
            ev["yellow_cards"],
            ev["red_cards"],
            ev["own_goals"],
            ev["bonus"],
            dca,
        )
    )
    positions = [Position.GK, Position.DEF, Position.MID, Position.FWD]
    for i in range(n):
        scalar = score_match(
            MatchEvents(**{k: int(v[i]) for k, v in ev.items()}), positions[pos[i]], R26
        )
        assert scalar["total"] == tot[i]


@pytest.mark.parametrize("season", ["2024-25", "2025-26", "2026-27"])
def test_reproduces_official_points_exactly(season: str) -> None:
    """ADR-0005 empirical verification on committed real-data excerpts (100% required)."""
    ds = fixture_dataset(season)
    pm = ds["player_match"].merge(
        ds["players"][["season", "player_code", "position"]], on=["season", "player_code"]
    )
    t = ScoringTables.from_ruleset(load_ruleset(season))
    pos = position_codes(pm["position"].to_numpy())

    def col(c: str) -> np.ndarray:
        return pm[c].fillna(0).astype(int).to_numpy()

    comps = score_components(
        t,
        pos,
        col("minutes"),
        col("goals"),
        col("assists"),
        col("clean_sheets"),
        col("goals_conceded"),
        col("saves"),
        col("penalties_saved"),
        col("penalties_missed"),
        col("yellow_cards"),
        col("red_cards"),
        col("own_goals"),
        col("bonus"),
        defensive_actions(pos, col("cbi"), col("tackles"), col("recoveries"), t),
    )
    ours = total_points(comps)
    mismatch = int((ours != pm["points"].to_numpy()).sum())
    assert mismatch == 0, f"{mismatch}/{len(pm)} rows differ from official points"
    assert set(comps) == set(COMPONENTS)
