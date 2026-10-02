"""Squad composition, selling prices and lineup rules (§16, §19, §53.2, §63, §77.1)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fpl_domain.enums import POSITIONS, ChipType, Position
from fpl_domain.errors import RuleViolation
from fpl_domain.rules.model import Ruleset, SellingPriceRule


class SquadPick(BaseModel):
    """One owned player with the economics needed for selling-price arithmetic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    player_code: int
    position: Position
    team_code: int
    purchase_price: int = Field(ge=0, le=300)


def selling_price(purchase: int, current: int, rule: SellingPriceRule) -> int:
    """FPL selling price in tenths: half of any profit (rounded down); losses passed in full."""
    if rule is SellingPriceRule.HALF_PROFIT_FLOOR:
        if current > purchase:
            return purchase + (current - purchase) // 2
        return current
    raise ValueError(f"unsupported selling price rule {rule}")  # pragma: no cover


def squad_violations(
    picks: Sequence[SquadPick], ruleset: Ruleset, bank: int | None = None
) -> list[RuleViolation]:
    """All squad-level invariant violations (empty list == legal)."""
    out: list[RuleViolation] = []
    rules = ruleset.squad
    codes = [p.player_code for p in picks]
    dupes = [c for c, n in Counter(codes).items() if n > 1]
    if dupes:
        out.append(RuleViolation("duplicate_player", f"players appear twice: {dupes}"))
    if len(picks) != rules.size:
        out.append(
            RuleViolation("squad_size", f"squad has {len(picks)} players, needs {rules.size}")
        )
    counts = Counter(p.position for p in picks)
    for pos in POSITIONS:
        need = rules.positions.get(pos)
        if counts.get(pos, 0) != need:
            out.append(
                RuleViolation(
                    "position_quota",
                    f"{pos}: {counts.get(pos, 0)} players, needs {need}",
                    position=pos.value,
                )
            )
    clubs = Counter(p.team_code for p in picks)
    for team, n in clubs.items():
        if n > rules.max_per_club:
            out.append(
                RuleViolation(
                    "club_limit",
                    f"{n} players from team {team} (max {rules.max_per_club})",
                    team_code=team,
                )
            )
    if bank is not None and bank < 0:
        out.append(RuleViolation("negative_bank", f"bank {bank} < 0"))
    return out


def assert_legal_squad(
    picks: Sequence[SquadPick], ruleset: Ruleset, bank: int | None = None
) -> None:
    v = squad_violations(picks, ruleset, bank)
    if v:
        raise v[0]


# ------------------------------------------------------------------ lineup


class Lineup(BaseModel):
    """Starting XI + ordered bench (bench[0] is the substitute goalkeeper) + armbands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    starters: tuple[int, ...]
    bench: tuple[int, ...]
    captain: int
    vice_captain: int

    @model_validator(mode="after")
    def _shape(self) -> Lineup:
        if self.captain == self.vice_captain:
            raise ValueError("captain and vice-captain must differ")
        if self.captain not in self.starters or self.vice_captain not in self.starters:
            raise ValueError("captain and vice-captain must be starters")
        if set(self.starters) & set(self.bench):
            raise ValueError("a player cannot both start and be on the bench")
        return self

    @property
    def players(self) -> tuple[int, ...]:
        return self.starters + self.bench


def formation(starters: Iterable[int], positions: Mapping[int, Position]) -> dict[Position, int]:
    c = Counter(positions[p] for p in starters)
    return {pos: c.get(pos, 0) for pos in POSITIONS}


def formation_is_legal(counts: Mapping[Position, int], ruleset: Ruleset) -> bool:
    lo, hi = ruleset.lineup.min_per_position, ruleset.lineup.max_per_position
    if sum(counts.values()) != ruleset.lineup.starters:
        return False
    return all(lo.get(p) <= counts.get(p, 0) <= hi.get(p) for p in POSITIONS)


def lineup_violations(
    lineup: Lineup, squad_codes: Iterable[int], positions: Mapping[int, Position], ruleset: Ruleset
) -> list[RuleViolation]:
    out: list[RuleViolation] = []
    squad = set(squad_codes)
    if set(lineup.players) != squad or len(lineup.players) != len(squad):
        out.append(RuleViolation("lineup_squad_mismatch", "lineup must use exactly the squad"))
        return out
    if len(lineup.starters) != ruleset.lineup.starters:
        out.append(RuleViolation("xi_size", f"{len(lineup.starters)} starters"))
    if not formation_is_legal(formation(lineup.starters, positions), ruleset):
        out.append(
            RuleViolation("formation", f"illegal formation {formation(lineup.starters, positions)}")
        )
    if lineup.bench and positions[lineup.bench[0]] is not Position.GK:
        out.append(RuleViolation("bench_gk_slot", "first bench slot must be the goalkeeper"))
    return out


def auto_substitute(
    lineup: Lineup,
    positions: Mapping[int, Position],
    minutes: Mapping[int, int],
    ruleset: Ruleset,
) -> tuple[tuple[int, ...], list[tuple[int, int]]]:
    """Apply FPL automatic substitutions.

    A starter with 0 minutes in the gameweek is replaced by the first bench player (in order)
    who played and keeps the formation legal; the substitute goalkeeper may only replace the
    starting goalkeeper. Returns the effective XI and the (out, in) substitutions made.
    """
    xi = list(lineup.starters)
    used: set[int] = set()
    subs: list[tuple[int, int]] = []
    bench = list(lineup.bench)
    for idx, starter in enumerate(list(xi)):
        if minutes.get(starter, 0) > 0:
            continue
        for b in bench:
            if b in used or minutes.get(b, 0) <= 0:
                continue
            is_gk_pair = positions[starter] is Position.GK
            if is_gk_pair != (positions[b] is Position.GK):
                continue
            candidate = xi.copy()
            candidate[idx] = b
            if formation_is_legal(formation(candidate, positions), ruleset):
                xi = candidate
                used.add(b)
                subs.append((starter, b))
                break
    return tuple(xi), subs


def gameweek_points(
    lineup: Lineup,
    positions: Mapping[int, Position],
    points: Mapping[int, int],
    minutes: Mapping[int, int],
    ruleset: Ruleset,
    chip_type: str | None = None,
) -> dict[str, object]:
    """Squad points for a gameweek: auto-subs, captaincy (vice fallback), TC and BB."""
    bench_boost = chip_type == ChipType.BENCH_BOOST.value
    triple = chip_type == ChipType.TRIPLE_CAPTAIN.value
    if bench_boost:
        counting: tuple[int, ...] = lineup.players
        subs: list[tuple[int, int]] = []
    else:
        counting, subs = auto_substitute(lineup, positions, minutes, ruleset)
    mult = (
        ruleset.chips.triple_captain_multiplier
        if triple
        else (ruleset.captaincy.captain_multiplier)
    )
    captain_played = minutes.get(lineup.captain, 0) > 0
    vice_played = minutes.get(lineup.vice_captain, 0) > 0
    armband: int | None = None
    if captain_played:
        armband = lineup.captain
    elif (
        ruleset.captaincy.vice_promotes_if_captain_did_not_play
        and vice_played
        and (lineup.vice_captain in counting)
    ):
        armband = lineup.vice_captain
    total = 0
    for p in counting:
        pts = points.get(p, 0)
        total += pts * (mult if p == armband else 1)
    return {
        "points": total,
        "counting": tuple(counting),
        "auto_subs": subs,
        "armband": armband,
        "multiplier": mult if armband is not None else 1,
    }
