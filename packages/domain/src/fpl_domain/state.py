"""Manager state and its transitions (§8 ManagerState, §53.2 invariants, §61.2).

Lifecycle of one gameweek ``t``::

    ManagerState(gw=t, pre-deadline)
        └─ apply_deadline(decision)  → DeadlineOutcome (squad that plays GW t, hits, chip)
              └─ advance(outcome)    → ManagerState(gw=t+1)  (FT rollover, chip status,
                                                               Free Hit reversion)

Every function validates the active ruleset and raises ``RuleViolation`` on illegal actions; it
never "repairs" an illegal decision. Each new state records the event that produced it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from fpl_domain.enums import ChipType, Position
from fpl_domain.errors import RuleViolation
from fpl_domain.hashing import content_hash
from fpl_domain.rules.model import ChipFtPolicy, Ruleset
from fpl_domain.squad import (
    Lineup,
    SquadPick,
    club_allowance,
    lineup_violations,
    selling_price,
    squad_violations,
)


class ChipStatus(StrEnum):
    AVAILABLE = "available"
    USED = "used"
    EXPIRED = "expired"


class ChipState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chip_id: str
    chip_type: ChipType
    first_gameweek: int
    last_gameweek: int
    status: ChipStatus = ChipStatus.AVAILABLE
    used_gameweek: int | None = None

    def usable_in(self, gameweek: int) -> bool:
        return (
            self.status is ChipStatus.AVAILABLE
            and self.first_gameweek <= gameweek <= self.last_gameweek
        )


def initial_chips(ruleset: Ruleset) -> tuple[ChipState, ...]:
    return tuple(
        ChipState(
            chip_id=c.id,
            chip_type=c.type,
            first_gameweek=c.first_gameweek,
            last_gameweek=c.last_gameweek,
        )
        for c in ruleset.chips.catalogue
        if c.supported
    )


class FreeHitRevert(BaseModel):
    """The pre-Free-Hit squad, restored after the Free Hit gameweek (§53.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    squad: tuple[SquadPick, ...]
    bank: int
    lineup: Lineup | None


class ManagerState(BaseModel):
    """State *before* the deadline of ``gameweek``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    season: str
    gameweek: int = Field(ge=1, le=38)
    squad: tuple[SquadPick, ...]
    bank: int = Field(ge=0)
    free_transfers: int = Field(ge=0, le=20)
    transfers_made: int = Field(0, ge=0, description="Transfers already made for this GW")
    hit_points_taken: int = Field(0, ge=0, description="Hit points already incurred this GW")
    chips: tuple[ChipState, ...]
    lineup: Lineup | None = None
    manager_id: int | None = None
    as_of: datetime | None = None
    source: str = "manual"
    provenance: str = Field("initial", description="Event that produced this state")
    overall_points: int | None = None
    overall_rank: int | None = None

    # -------------------------------------------------------------- helpers
    @property
    def codes(self) -> tuple[int, ...]:
        return tuple(p.player_code for p in self.squad)

    @property
    def positions(self) -> dict[int, Position]:
        return {p.player_code: p.position for p in self.squad}

    def pick(self, code: int) -> SquadPick:
        for p in self.squad:
            if p.player_code == code:
                return p
        raise KeyError(code)

    def selling_values(self, prices: Mapping[int, int], ruleset: Ruleset) -> dict[int, int]:
        rule = ruleset.pricing.selling_price_rule
        return {
            p.player_code: selling_price(p.purchase_price, prices[p.player_code], rule)
            for p in self.squad
        }

    def total_budget(self, prices: Mapping[int, int], ruleset: Ruleset) -> int:
        """Bank + selling value of the squad (the Free Hit / Wildcard budget)."""
        return self.bank + sum(self.selling_values(prices, ruleset).values())

    def chip(self, chip_id: str) -> ChipState:
        for c in self.chips:
            if c.chip_id == chip_id:
                return c
        raise KeyError(chip_id)

    def available_chips(self) -> tuple[ChipState, ...]:
        return tuple(c for c in self.chips if c.usable_in(self.gameweek))

    @property
    def state_hash(self) -> str:
        return content_hash(self.model_dump(mode="json", exclude={"as_of"}))


class Transfer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    out_code: int
    in_code: int
    in_position: Position
    in_team_code: int
    in_price: int = Field(ge=0, le=300, description="Purchase price at the deadline")


class GameweekDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    transfers: tuple[Transfer, ...] = ()
    chip_id: str | None = None
    lineup: Lineup | None = None


class DeadlineOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gameweek: int
    playing_squad: tuple[SquadPick, ...]
    bank_after: int
    lineup: Lineup | None
    chip: ChipState | None
    transfers: tuple[Transfer, ...]
    transfers_total: int
    paid_transfers: int
    hit_points: int
    free_transfers_at_start: int
    revert: FreeHitRevert | None
    prior: ManagerState


# ------------------------------------------------------------------ free transfers


def transfers_are_free(gameweek: int, chip: ChipType | None, ruleset: Ruleset) -> bool:
    tr = ruleset.transfers
    if gameweek == 1 and tr.first_gameweek_unlimited:
        return True
    if chip in (ChipType.WILDCARD, ChipType.FREE_HIT):
        return True
    top = tr.top_up_for(gameweek)
    return bool(top and top.unlimited)


def paid_transfer_count(
    free_transfers: int,
    transfers_total: int,
    gameweek: int,
    chip: ChipType | None,
    ruleset: Ruleset,
) -> int:
    if transfers_are_free(gameweek, chip, ruleset):
        return 0
    return max(0, transfers_total - free_transfers)


def next_free_transfers(
    free_transfers: int,
    transfers_total: int,
    gameweek: int,
    chip: ChipType | None,
    ruleset: Ruleset,
) -> int:
    """Free transfers available at ``gameweek + 1`` (§51.1 rollover, max banked)."""
    tr = ruleset.transfers
    per, cap = tr.free_transfers_per_gameweek, tr.max_banked_free_transfers
    top_now = tr.top_up_for(gameweek)
    if gameweek == 1 and tr.first_gameweek_unlimited:
        nxt = tr.initial_free_transfers_after_first_gameweek
    elif chip in (ChipType.WILDCARD, ChipType.FREE_HIT) or (top_now and top_now.unlimited):
        policy = tr.chip_ft_policy
        if policy is ChipFtPolicy.RETAIN:
            nxt = free_transfers
        elif policy is ChipFtPolicy.RETAIN_AND_ACCRUE:
            nxt = free_transfers + per
        else:
            nxt = per
    else:
        nxt = max(free_transfers - transfers_total, 0) + per
    nxt = min(cap, nxt)
    top_next = tr.top_up_for(gameweek + 1)
    if top_next and top_next.set_free_transfers_to is not None:
        nxt = max(nxt, top_next.set_free_transfers_to)  # e.g. AFCON top-up to five
    return nxt


# ------------------------------------------------------------------ transitions


def with_current_clubs(state: ManagerState, teams: Mapping[int, int]) -> ManagerState:
    """Owned players keep their purchase price but count for their *current* club: a player
    who moved club mid-season counts against the new club's limit (``teams``: code → club)."""
    squad = tuple(
        p
        if teams.get(p.player_code, p.team_code) == p.team_code
        else p.model_copy(update={"team_code": int(teams[p.player_code])})
        for p in state.squad
    )
    return state if squad == state.squad else state.model_copy(update={"squad": squad})


def apply_deadline(
    state: ManagerState,
    decision: GameweekDecision,
    prices: Mapping[int, int],
    ruleset: Ruleset,
) -> DeadlineOutcome:
    """Validate and apply a gameweek decision at the deadline of ``state.gameweek``."""
    gw = state.gameweek
    chip: ChipState | None = None
    if decision.chip_id is not None:
        try:
            chip = state.chip(decision.chip_id)
        except KeyError as exc:
            raise RuleViolation("chip_unknown", f"chip {decision.chip_id} not held") from exc
        if not chip.usable_in(gw):
            raise RuleViolation(
                "chip_unavailable",
                f"{chip.chip_id} is {chip.status.value} / window "
                f"{chip.first_gameweek}-{chip.last_gameweek}, GW {gw}",
            )
    chip_type = chip.chip_type if chip else None

    outs = [t.out_code for t in decision.transfers]
    ins = [t.in_code for t in decision.transfers]
    if len(set(outs)) != len(outs) or len(set(ins)) != len(ins):
        raise RuleViolation("transfer_duplicate", "a player is transferred twice")
    if set(outs) & set(ins):
        raise RuleViolation(
            "transfer_in_and_out", "a player cannot be sold and bought in the same gameweek"
        )
    owned = set(state.codes)
    missing = [o for o in outs if o not in owned]
    if missing:
        raise RuleViolation("transfer_out_not_owned", f"not in squad: {missing}")
    already = [i for i in ins if i in owned]
    if already:
        raise RuleViolation("transfer_in_owned", f"already owned: {already}")

    sell = state.selling_values(prices, ruleset)
    proceeds = sum(sell[o] for o in outs)
    cost = sum(t.in_price for t in decision.transfers)
    kept = [p for p in state.squad if p.player_code not in set(outs)]
    new = [
        SquadPick(
            player_code=t.in_code,
            position=t.in_position,
            team_code=t.in_team_code,
            purchase_price=t.in_price,
        )
        for t in decision.transfers
    ]
    squad = tuple(kept + new)
    bank_after = state.bank + proceeds - cost
    violations = squad_violations(squad, ruleset, bank_after, club_allowance(state.squad, ruleset))
    if violations:
        raise violations[0]

    total = state.transfers_made + len(decision.transfers)
    paid_total = paid_transfer_count(state.free_transfers, total, gw, chip_type, ruleset)
    paid_before = paid_transfer_count(
        state.free_transfers, state.transfers_made, gw, chip_type, ruleset
    )
    hit_points = state.hit_points_taken + (paid_total - paid_before) * ruleset.transfers.hit_cost
    if chip_type in (ChipType.WILDCARD, ChipType.FREE_HIT):
        hit_points = 0  # chips make the whole gameweek's transfers free

    lineup = decision.lineup
    if lineup is not None:
        positions = {p.player_code: p.position for p in squad}
        lv = lineup_violations(lineup, (p.player_code for p in squad), positions, ruleset)
        if lv:
            raise lv[0]

    revert = None
    if chip_type is ChipType.FREE_HIT and ruleset.chips.free_hit_reverts:
        revert = FreeHitRevert(squad=state.squad, bank=state.bank, lineup=state.lineup)
    return DeadlineOutcome(
        gameweek=gw,
        playing_squad=squad,
        bank_after=bank_after,
        lineup=lineup,
        chip=chip,
        transfers=decision.transfers,
        transfers_total=total,
        paid_transfers=paid_total,
        hit_points=hit_points,
        free_transfers_at_start=state.free_transfers,
        revert=revert,
        prior=state,
    )


def advance(outcome: DeadlineOutcome, ruleset: Ruleset, event: str | None = None) -> ManagerState:
    """State for the next gameweek after ``outcome.gameweek`` has been played."""
    prior = outcome.prior
    gw = outcome.gameweek
    chip_type = outcome.chip.chip_type if outcome.chip else None
    ft_next = next_free_transfers(
        outcome.free_transfers_at_start, outcome.transfers_total, gw, chip_type, ruleset
    )
    chips: list[ChipState] = []
    for c in prior.chips:
        updated = c
        if outcome.chip and c.chip_id == outcome.chip.chip_id:
            updated = c.model_copy(update={"status": ChipStatus.USED, "used_gameweek": gw})
        elif c.status is ChipStatus.AVAILABLE and c.last_gameweek < gw + 1:
            updated = c.model_copy(update={"status": ChipStatus.EXPIRED})
        chips.append(updated)
    if outcome.revert is not None:
        squad, bank, lineup = outcome.revert.squad, outcome.revert.bank, outcome.revert.lineup
    else:
        squad, bank, lineup = outcome.playing_squad, outcome.bank_after, outcome.lineup
    if gw >= ruleset.num_gameweeks:
        raise RuleViolation("season_over", "no gameweek after the final one")
    return ManagerState(
        season=prior.season,
        gameweek=gw + 1,
        squad=squad,
        bank=bank,
        free_transfers=ft_next,
        transfers_made=0,
        hit_points_taken=0,
        chips=tuple(chips),
        lineup=lineup,
        manager_id=prior.manager_id,
        source=prior.source,
        provenance=event or f"advance:gw{gw}:{content_hash(outcome.model_dump(mode='json'))[:12]}",
        overall_points=prior.overall_points,
        overall_rank=prior.overall_rank,
    )


def state_violations(state: ManagerState, ruleset: Ruleset) -> list[RuleViolation]:
    """§53.2 invariants for a stored state."""
    out = squad_violations(state.squad, ruleset, state.bank)
    cap = ruleset.transfers.max_banked_free_transfers
    if state.free_transfers > cap:
        out.append(RuleViolation("ft_cap", f"{state.free_transfers} free transfers > cap {cap}"))
    ids = [c.chip_id for c in state.chips]
    if len(ids) != len(set(ids)):
        out.append(RuleViolation("chip_duplicate", "duplicate chip ids"))
    for c in state.chips:
        if c.status is ChipStatus.USED and c.used_gameweek is None:
            out.append(RuleViolation("chip_used_without_gw", f"{c.chip_id} used without GW"))
    if state.lineup is not None:
        out.extend(lineup_violations(state.lineup, state.codes, state.positions, ruleset))
    return out


def initial_state(
    season: str,
    picks: Sequence[SquadPick],
    budget_left: int,
    ruleset: Ruleset,
    lineup: Lineup | None = None,
    source: str = "manual",
) -> ManagerState:
    """State for GW1 (squad selection) or any manually entered squad."""
    st = ManagerState(
        season=season,
        gameweek=1,
        squad=tuple(picks),
        bank=budget_left,
        free_transfers=0,
        chips=initial_chips(ruleset),
        lineup=lineup,
        source=source,
        provenance="initial_squad",
    )
    v = state_violations(st, ruleset)
    if v:
        raise v[0]
    return st
