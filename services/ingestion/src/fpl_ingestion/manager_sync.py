"""Reconstruct a ``ManagerState`` from public FPL endpoints (§6.1, §86.1, §35).

No credentials are used. Public endpoints do not expose free transfers or selling prices, so:
* **squad / lineup** — picks of the latest gameweek whose deadline has passed; if that gameweek
  was a Free Hit, the picks of the gameweek before it (the squad reverts);
* **purchase prices** — the most recent ``element_in_cost`` for each owned player from the
  transfer history; players held since squad creation use their season start price
  (``now_cost − cost_change_start``);
* **bank** — ``entry_history.current[-1].bank``;
* **free transfers** — replayed gameweek by gameweek through the versioned ruleset from the
  manager's first gameweek, using recorded transfers and chips. The replay is cross-checked
  against FPL's recorded ``event_transfers_cost``; any disagreement is reported (it would mean
  the ruleset is wrong), never hidden;
* **chips** — mapped onto the ruleset catalogue by the window containing the gameweek played.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from fpl_domain.enums import ChipType, Position
from fpl_domain.rules.model import Ruleset
from fpl_domain.squad import Lineup, SquadPick
from fpl_domain.state import (
    ChipState,
    ChipStatus,
    ManagerState,
    initial_chips,
    next_free_transfers,
    paid_transfer_count,
)
from fpl_ingestion.sources import fpl_api_schemas as s


@dataclass
class SyncReport:
    state: ManagerState
    warnings: list[str] = field(default_factory=list)
    replay: list[dict[str, int | str | None]] = field(default_factory=list)


def _chip_type(name: str) -> ChipType | None:
    mapped = s.CHIP_NAME_MAP.get(name)
    return ChipType(mapped) if mapped else None


def reconstruct_state(
    entry: s.ApiEntry,
    history: s.EntryHistory,
    picks_for: Callable[[int], s.EntryPicks],
    transfers: list[s.ApiTransfer],
    boot: s.BootstrapStatic,
    ruleset: Ruleset,
    next_gameweek: int,
    as_of: datetime,
) -> SyncReport:
    warnings: list[str] = []
    played = sorted(h.event for h in history.current if h.event < next_gameweek)
    if not played:
        raise ValueError("manager has no completed gameweek before the target gameweek")
    last_gw = played[-1]
    chips_by_gw = {c.event: _chip_type(c.name) for c in history.chips}

    # ---------------------------------------------------------------- squad + lineup
    picks = picks_for(last_gw)
    fh_gw = last_gw if chips_by_gw.get(last_gw) is ChipType.FREE_HIT else None
    if fh_gw is not None:
        picks = picks_for(last_gw - 1)  # Free Hit squad reverts
    elements = {e.id: e for e in boot.elements}
    ordered = sorted(picks.picks, key=lambda p: p.position)

    fh_events = {g for g, c in chips_by_gw.items() if c is ChipType.FREE_HIT}
    latest_buy: dict[int, int] = {}
    for t in sorted(transfers, key=lambda t: (t.event, t.time)):
        if t.event in fh_events or t.event >= next_gameweek:
            continue
        latest_buy[t.element_in] = t.element_in_cost

    squad: list[SquadPick] = []
    for p in ordered:
        e = elements[p.element]
        if p.element in latest_buy:
            purchase = latest_buy[p.element]
        else:
            if e.cost_change_start is None:
                warnings.append(f"no start price for {e.web_name}; using current price")
            purchase = e.now_cost - (e.cost_change_start or 0)
        squad.append(
            SquadPick(
                player_code=e.code,
                position=Position.from_element_type(e.element_type),
                team_code=e.team_code,
                purchase_price=purchase,
            )
        )
    code_of = {p.element: elements[p.element].code for p in ordered}
    starters = tuple(code_of[p.element] for p in ordered if p.position <= 11)
    bench = tuple(code_of[p.element] for p in ordered if p.position > 11)
    cap = next(code_of[p.element] for p in ordered if p.is_captain)
    vice = next(code_of[p.element] for p in ordered if p.is_vice_captain)
    lineup = Lineup(starters=starters, bench=bench, captain=cap, vice_captain=vice)

    # ---------------------------------------------------------------- free-transfer replay
    replay: list[dict[str, int | str | None]] = []
    ft = 0
    by_event = {h.event: h for h in history.current}
    first = entry.started_event
    for gw in range(first, next_gameweek):
        h = by_event.get(gw)
        n = h.event_transfers if h else 0
        chip = chips_by_gw.get(gw)
        is_start = gw == first
        if is_start:
            # A manager's first gameweek is squad creation: transfers are unlimited.
            paid = 0
            ft_next = ruleset.transfers.initial_free_transfers_after_first_gameweek
        else:
            paid = paid_transfer_count(ft, n, gw, chip, ruleset)
            ft_next = next_free_transfers(ft, n, gw, chip, ruleset)
        recorded_cost = h.event_transfers_cost if h else 0
        expected_cost = paid * ruleset.transfers.hit_cost
        if h and recorded_cost != expected_cost:
            warnings.append(
                f"GW{gw}: replayed hit cost {expected_cost} != recorded "
                f"{recorded_cost}; ruleset or history disagree"
            )
        replay.append(
            {
                "gw": gw,
                "ft_start": ft,
                "transfers": n,
                "chip": chip.value if chip else None,
                "ft_next": ft_next,
                "hit_cost": recorded_cost,
            }
        )
        ft = ft_next

    # ---------------------------------------------------------------- chips
    chips: list[ChipState] = []
    used = [(c.event, _chip_type(c.name)) for c in history.chips]
    for c in initial_chips(ruleset):
        match = next(
            (g for g, t in used if t is c.chip_type and c.first_gameweek <= g <= c.last_gameweek),
            None,
        )
        updated = c
        if match is not None:
            updated = c.model_copy(update={"status": ChipStatus.USED, "used_gameweek": match})
        elif c.last_gameweek < next_gameweek:
            updated = c.model_copy(update={"status": ChipStatus.EXPIRED})
        chips.append(updated)

    last = by_event[last_gw]
    state = ManagerState(
        season=ruleset.season,
        gameweek=next_gameweek,
        squad=tuple(squad),
        bank=last.bank,
        free_transfers=ft,
        chips=tuple(chips),
        lineup=lineup,
        manager_id=entry.id,
        as_of=as_of,
        source="fpl_api",
        provenance=f"fpl_api:sync:entry{entry.id}:gw{last_gw}",
        overall_points=last.total_points,
        overall_rank=last.overall_rank,
    )
    return SyncReport(state=state, warnings=warnings, replay=replay)
