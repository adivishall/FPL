"""Builders for legal squads/lineups from the real 2026-27 player pool (test helper)."""

from __future__ import annotations

import random
from collections import Counter
from functools import lru_cache

from fpl_domain.enums import POSITIONS, Position
from fpl_domain.rules import Ruleset, load_ruleset
from fpl_domain.squad import Lineup, SquadPick
from fpl_domain.state import ManagerState, initial_chips
from tests.fixtures_util import canonical_season

RS = load_ruleset("2026-27")


@lru_cache(maxsize=1)
def pool() -> list[dict]:
    p = canonical_season("2026-27").players
    return p[["player_code", "position", "team_code", "price"]].to_dict("records")


def prices() -> dict[int, int]:
    return {r["player_code"]: int(r["price"]) for r in pool()}


def random_squad(
    rng: random.Random, ruleset: Ruleset = RS, exclude: set[int] | None = None
) -> list[SquadPick]:
    exclude = exclude or set()
    while True:
        chosen: list[dict] = []
        clubs: Counter[int] = Counter()
        ok = True
        for pos in POSITIONS:
            cands = [
                r for r in pool() if r["position"] == pos.value and r["player_code"] not in exclude
            ]
            rng.shuffle(cands)
            need = ruleset.squad.positions.get(pos)
            for r in cands:
                if need == 0:
                    break
                if clubs[r["team_code"]] < ruleset.squad.max_per_club:
                    chosen.append(r)
                    clubs[r["team_code"]] += 1
                    need -= 1
            ok &= need == 0
        if ok:
            return [
                SquadPick(
                    player_code=r["player_code"],
                    position=Position(r["position"]),
                    team_code=r["team_code"],
                    purchase_price=int(r["price"]),
                )
                for r in chosen
            ]


def default_lineup(squad: list[SquadPick]) -> Lineup:
    """A legal 4-4-2 with the backup GK first on the bench."""
    by = {p: [s.player_code for s in squad if s.position is p] for p in POSITIONS}
    starters = (
        by[Position.GK][:1] + by[Position.DEF][:4] + by[Position.MID][:4] + by[Position.FWD][:2]
    )
    bench = by[Position.GK][1:] + by[Position.DEF][4:] + by[Position.MID][4:] + by[Position.FWD][2:]
    return Lineup(
        starters=tuple(starters),
        bench=tuple(bench),
        captain=starters[-1],
        vice_captain=starters[-2],
    )


def state_for(
    squad: list[SquadPick], gameweek: int = 2, bank: int = 50, ft: int = 1, ruleset: Ruleset = RS
) -> ManagerState:
    return ManagerState(
        season=ruleset.season,
        gameweek=gameweek,
        squad=tuple(squad),
        bank=bank,
        free_transfers=ft,
        chips=initial_chips(ruleset),
        lineup=default_lineup(squad),
    )
