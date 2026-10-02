"""Squad legality, selling prices, formations, auto-subs and captaincy (§77.1)."""

from __future__ import annotations

import random

import pytest

from fpl_domain.enums import ChipType, Position
from fpl_domain.rules import SellingPriceRule
from fpl_domain.squad import (
    Lineup,
    SquadPick,
    auto_substitute,
    formation,
    formation_is_legal,
    gameweek_points,
    lineup_violations,
    selling_price,
    squad_violations,
)
from tests.domain_util import RS, default_lineup, random_squad

H = SellingPriceRule.HALF_PROFIT_FLOOR


@pytest.mark.parametrize(
    ("buy", "now", "sell"),
    [
        (50, 50, 50),
        (50, 51, 50),
        (50, 52, 51),
        (50, 53, 51),
        (50, 55, 52),
        (50, 49, 49),
        (50, 45, 45),
        (100, 131, 115),
    ],
)
def test_selling_price_half_profit_rounded_down(buy: int, now: int, sell: int) -> None:
    assert selling_price(buy, now, H) == sell


def test_random_squads_are_legal() -> None:
    rng = random.Random(1)
    for _ in range(50):
        assert squad_violations(random_squad(rng), RS) == []


def test_squad_violations_detected() -> None:
    squad = random_squad(random.Random(2))
    codes = {v.code for v in squad_violations(squad[:-1], RS)}
    assert {"squad_size", "position_quota"} <= codes
    dup = [*squad[:-1], squad[0]]
    assert "duplicate_player" in {v.code for v in squad_violations(dup, RS)}
    same_club = [p.model_copy(update={"team_code": 1}) for p in squad]
    assert "club_limit" in {v.code for v in squad_violations(same_club, RS)}
    assert "negative_bank" in {v.code for v in squad_violations(squad, RS, bank=-1)}


@pytest.mark.parametrize(
    ("d", "m", "f", "legal"),
    [
        (3, 4, 3, True),
        (3, 5, 2, True),
        (4, 5, 1, True),
        (5, 2, 3, True),
        (5, 4, 1, True),
        (2, 5, 3, False),
        (3, 6, 1, False),
        (4, 6, 0, False),
        (6, 3, 1, False),
    ],
)
def test_formation_legality(d: int, m: int, f: int, legal: bool) -> None:
    counts = {Position.GK: 1, Position.DEF: d, Position.MID: m, Position.FWD: f}
    assert formation_is_legal(counts, RS) is legal


def _squad_and_lineup() -> tuple[list[SquadPick], Lineup, dict[int, Position]]:
    squad = random_squad(random.Random(3))
    return squad, default_lineup(squad), {p.player_code: p.position for p in squad}


def test_lineup_validation() -> None:
    squad, lu, pos = _squad_and_lineup()
    assert lineup_violations(lu, [p.player_code for p in squad], pos, RS) == []
    bad = Lineup(
        starters=lu.starters,
        bench=(lu.bench[1], lu.bench[0], *lu.bench[2:]),
        captain=lu.captain,
        vice_captain=lu.vice_captain,
    )
    assert "bench_gk_slot" in {
        v.code for v in lineup_violations(bad, [p.player_code for p in squad], pos, RS)
    }
    with pytest.raises(ValueError, match="differ"):
        Lineup(starters=lu.starters, bench=lu.bench, captain=lu.captain, vice_captain=lu.captain)


def test_autosub_goalkeeper_only_replaced_by_goalkeeper() -> None:
    _, lu, pos = _squad_and_lineup()
    minutes = dict.fromkeys(lu.players, 90)
    minutes[lu.starters[0]] = 0  # starting GK did not play
    xi, subs = auto_substitute(lu, pos, minutes, RS)
    assert subs == [(lu.starters[0], lu.bench[0])]
    assert pos[xi[0]] is Position.GK


def test_autosub_respects_bench_order_and_skips_non_players() -> None:
    _, lu, pos = _squad_and_lineup()
    minutes = dict.fromkeys(lu.players, 90)
    mid_out = next(p for p in lu.starters if pos[p] is Position.MID)
    minutes[mid_out] = 0
    minutes[lu.bench[1]] = 0  # first outfield sub did not play either
    xi, subs = auto_substitute(lu, pos, minutes, RS)
    assert subs == [(mid_out, lu.bench[2])]
    assert lu.bench[1] not in xi


def test_autosub_keeps_formation_legal() -> None:
    squad, _, pos = _squad_and_lineup()
    gks = [p.player_code for p in squad if p.position is Position.GK]
    defs = [p.player_code for p in squad if p.position is Position.DEF]
    mids = [p.player_code for p in squad if p.position is Position.MID]
    fwds = [p.player_code for p in squad if p.position is Position.FWD]
    # 3-5-2 with DEF, DEF on the bench first, then a FWD.
    lu = Lineup(
        starters=(gks[0], *defs[:3], *mids, *fwds[:2]),
        bench=(gks[1], fwds[2], defs[3], defs[4]),
        captain=mids[0],
        vice_captain=mids[1],
    )
    minutes = dict.fromkeys(lu.players, 90)
    minutes[defs[0]] = 0  # a defender out: only a defender keeps ≥3 DEF
    xi, subs = auto_substitute(lu, pos, minutes, RS)
    assert subs == [(defs[0], defs[3])]
    assert formation_is_legal(formation(xi, pos), RS)


def test_captain_vice_and_chips() -> None:
    _, lu, pos = _squad_and_lineup()
    minutes = dict.fromkeys(lu.players, 90)
    points = dict.fromkeys(lu.players, 2)
    points[lu.captain], points[lu.vice_captain] = 10, 6
    base = gameweek_points(lu, pos, points, minutes, RS)
    assert base["points"] == 2 * 9 + 10 * 2 + 6
    tc = gameweek_points(lu, pos, points, minutes, RS, ChipType.TRIPLE_CAPTAIN.value)
    assert tc["points"] == base["points"] + 10
    bb = gameweek_points(lu, pos, points, minutes, RS, ChipType.BENCH_BOOST.value)
    assert bb["points"] == base["points"] + 4 * 2
    minutes[lu.captain] = 0
    points[lu.captain] = 0
    vice = gameweek_points(lu, pos, points, minutes, RS)
    assert vice["armband"] == lu.vice_captain
    minutes[lu.vice_captain] = 0
    points[lu.vice_captain] = 0
    none = gameweek_points(lu, pos, points, minutes, RS)
    assert none["armband"] is None
