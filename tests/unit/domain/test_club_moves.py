"""Owned players who change club mid-season count for their new club (§16, §51.1).

Regression for a 2025-26 backtest decision that the optimiser (current clubs) considered legal
and the state machine (clubs at purchase) rejected as 4 players from one club.
"""

from __future__ import annotations

import random

import pytest

from fpl_domain.enums import Position
from fpl_domain.errors import RuleViolation
from fpl_domain.squad import SquadPick
from fpl_domain.state import GameweekDecision, Transfer, apply_deadline, with_current_clubs
from tests.domain_util import RS, pool, prices, random_squad, state_for


def _full_club_squad() -> tuple[list[SquadPick], int]:
    """A legal random squad that holds exactly the maximum (3) players of some club X."""
    for seed in range(500):
        squad = random_squad(random.Random(seed))
        full = sorted(
            t for t in {p.team_code for p in squad} if sum(p.team_code == t for p in squad) == 3
        )
        if full:
            return squad, full[0]
    raise AssertionError("no seed produced a full club")  # pragma: no cover


def _buy(state, out: SquadPick, team: int | None, exclude_team: int | None = None) -> Transfer:  # type: ignore[no-untyped-def]
    owned = set(state.codes)
    cand = next(
        r
        for r in sorted(pool(), key=lambda r: r["price"])
        if r["position"] == out.position.value
        and r["player_code"] not in owned
        and (team is None or r["team_code"] == team)
        and (exclude_team is None or r["team_code"] != exclude_team)
    )
    return Transfer(
        out_code=out.player_code,
        in_code=cand["player_code"],
        in_position=Position(cand["position"]),
        in_team_code=cand["team_code"],
        in_price=int(cand["price"]),
    )


def test_refresh_moves_the_player_to_his_current_club_only() -> None:
    squad, x = _full_club_squad()
    mover = next(p for p in squad if p.team_code != x)
    st = state_for(squad, bank=200)
    moved = with_current_clubs(st, {mover.player_code: x, 999_999: 1})
    after = {p.player_code: p.team_code for p in moved.squad}
    assert after[mover.player_code] == x
    assert {c: t for c, t in after.items() if c != mover.player_code} == {
        p.player_code: p.team_code for p in squad if p.player_code != mover.player_code
    }
    assert {p.player_code: p.purchase_price for p in moved.squad} == {
        p.player_code: p.purchase_price for p in squad
    }  # purchase prices (selling-value arithmetic) are untouched
    assert with_current_clubs(st, {}) is st


def test_buying_into_the_old_club_of_a_mover_is_legal_once_refreshed() -> None:
    """The backtest case: club Y holds 3 by purchase records, but one of them now plays for X."""
    squad, y = _full_club_squad()
    mover = next(p for p in squad if p.team_code == y)
    x = next(
        t
        for t in {r["team_code"] for r in pool()}
        if t != y and all(p.team_code != t for p in squad)
    )
    stale = state_for(squad, bank=300)
    out = next(p for p in squad if p.team_code != y and p.position is not Position.GK)
    buy_y = _buy(stale, out, y)
    with pytest.raises(RuleViolation, match="club_limit"):
        apply_deadline(stale, GameweekDecision(transfers=(buy_y,)), prices(), RS)
    fresh = with_current_clubs(stale, {mover.player_code: x})
    res = apply_deadline(fresh, GameweekDecision(transfers=(buy_y,)), prices(), RS)
    assert sum(p.team_code == y for p in res.playing_squad) == 3


def test_an_excess_caused_by_a_move_may_be_kept_or_reduced_never_increased() -> None:
    squad, x = _full_club_squad()
    mover = next(p for p in squad if p.team_code != x and p.position is not Position.GK)
    st = with_current_clubs(state_for(squad, bank=300), {mover.player_code: x})
    assert sum(p.team_code == x for p in st.squad) == 4
    # holding is legal
    assert len(apply_deadline(st, GameweekDecision(), prices(), RS).playing_squad) == 15
    # selling one of club X for a non-X player reduces the excess: legal
    sell_x = next(p for p in st.squad if p.team_code == x and p.player_code != mover.player_code)
    res = apply_deadline(
        st, GameweekDecision(transfers=(_buy(st, sell_x, None, exclude_team=x),)), prices(), RS
    )
    assert sum(p.team_code == x for p in res.playing_squad) == 3
    # buying a fifth player of club X is not
    out = next(p for p in st.squad if p.team_code != x)
    with pytest.raises(RuleViolation, match="club_limit"):
        apply_deadline(st, GameweekDecision(transfers=(_buy(st, out, x),)), prices(), RS)
