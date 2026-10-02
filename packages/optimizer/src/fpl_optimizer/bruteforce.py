"""Exhaustive reference optimiser for tiny instances (§36 flagship test, ADR-0007).

Shares no code with the MILP: every candidate action sequence is applied through the domain
state machine (``apply_deadline`` / ``advance``) — illegal ones raise and are skipped — and each
gameweek is scored with the exact lineup solver. The MILP must reproduce its optimum on random
small leagues; a disagreement is a bug in one of them.

Restrictions mirrored from the MILP (ADR-0007): a player sold within the horizon is not bought
back, and a player bought within the horizon is sold at most once.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from itertools import combinations

from fpl_domain.enums import ChipType, Position
from fpl_domain.errors import RuleViolation
from fpl_domain.state import GameweekDecision, ManagerState, Transfer, advance, apply_deadline
from fpl_optimizer.problem import OptimizationProblem
from fpl_optimizer.validate import gameweek_value


@dataclass(frozen=True)
class BruteResult:
    objective: float
    decisions: tuple[GameweekDecision, ...]
    evaluated: int


def _legal_squads(prob: OptimizationProblem, budget: int) -> Iterator[tuple[int, ...]]:
    pl = prob.players
    rs = prob.ruleset
    by_pos: dict[Position, list[int]] = {p: [] for p in Position}
    pos = pl.positions_map()
    for c in pl.code:
        by_pos[pos[int(c)]].append(int(c))
    price = {int(c): int(p) for c, p in zip(pl.code, pl.price, strict=True)}
    team = {int(c): int(t) for c, t in zip(pl.code, pl.team, strict=True)}
    q = rs.squad.positions
    for gk in combinations(by_pos[Position.GK], q.GK):
        for de in combinations(by_pos[Position.DEF], q.DEF):
            for mi in combinations(by_pos[Position.MID], q.MID):
                for fw in combinations(by_pos[Position.FWD], q.FWD):
                    squad = gk + de + mi + fw
                    if sum(price[c] for c in squad) > budget:
                        continue
                    counts: dict[int, int] = {}
                    for c in squad:
                        counts[team[c]] = counts.get(team[c], 0) + 1
                    if max(counts.values()) > rs.squad.max_per_club:
                        continue
                    yield squad


def _actions(
    prob: OptimizationProblem, state: ManagerState, sold: frozenset[int], bought: frozenset[int]
) -> Iterator[tuple[tuple[int, ...], tuple[int, ...]]]:
    pl = prob.players
    pos = pl.positions_map()
    owned = set(state.codes)
    banned = prob.preferences.banned
    buyable = [
        int(c)
        for c in pl.code
        if int(c) not in owned
        and int(c) not in sold
        and int(c) not in banned
        and int(c) not in bought
    ]
    sellable = [c for c in state.codes if c not in sold]
    max_k = min(len(buyable), len(sellable), prob.preferences.max_transfers_per_gw or 15)
    for k in range(max_k + 1):
        for ins in combinations(buyable, k):
            need = sorted(pos[i].value for i in ins)
            for outs in combinations(sellable, k):
                if sorted(pos[o].value for o in outs) == need:
                    yield outs, ins


def brute_force(prob: OptimizationProblem) -> BruteResult:
    pl, rs = prob.players, prob.ruleset
    w = prob.config.objective
    idx = pl.index()
    pos = pl.positions_map()
    price = {int(c): int(p) for c, p in zip(pl.code, pl.price, strict=True)}
    hit_cost = rs.transfers.hit_cost
    horizon = len(prob.gameweeks)
    chip_types = {c.chip_id: c.chip_type for c in prob.state.chips}
    original = set(prob.state.codes)

    @cache
    def gw_value(t: int, squad: tuple[int, ...], chip: ChipType | None) -> float:
        return gameweek_value(prob, t, squad, chip)[0]

    def transfer_objs(outs: tuple[int, ...], ins: tuple[int, ...]) -> tuple[Transfer, ...]:
        o_sorted = sorted(outs, key=lambda c: (pos[c].value, c))
        i_sorted = sorted(ins, key=lambda c: (pos[c].value, c))
        return tuple(
            Transfer(
                out_code=o,
                in_code=i,
                in_position=pos[i],
                in_team_code=int(pl.team[idx[i]]),
                in_price=price[i],
            )
            for o, i in zip(o_sorted, i_sorted, strict=True)
        )

    evaluated = 0

    def chips_for(t: int, used: frozenset[str]) -> list[str | None]:
        g = prob.gameweeks[t]
        out: list[str | None] = [None]
        for cid, wks in prob.chip_options.items():
            if cid not in used and g in wks:
                out.append(cid)
        forced = [cid for cid, gw in prob.forced_chips.items() if gw == g]
        if forced:
            return list(forced)
        return out

    def rec(
        t: int,
        state: ManagerState,
        sold: frozenset[int],
        bought: frozenset[int],
        used: frozenset[str],
    ) -> tuple[float, tuple[GameweekDecision, ...]]:
        nonlocal evaluated
        if t == horizon:
            v = w.free_transfer_value * state.free_transfers + w.bank_value_per_tenth * state.bank
            if w.terminal_squad_weight:
                v += w.terminal_squad_weight * sum(
                    float(pl.ev[idx[c], horizon - 1]) for c in state.codes
                )
            if any(cid not in used for cid in prob.forced_chips):
                return float("-inf"), ()
            return v, ()
        d = w.discount**t
        best: tuple[float, tuple[GameweekDecision, ...]] = (float("-inf"), ())
        for chip in chips_for(t, used):
            ctype = chip_types[chip] if chip else None
            if ctype is ChipType.FREE_HIT:
                budget = state.total_budget(price, rs)
                options = []
                for squad in _legal_squads(prob, budget):
                    ins = tuple(c for c in squad if c not in state.codes)
                    outs = tuple(c for c in state.codes if c not in squad)
                    if any(c in original and c not in state.codes for c in squad):
                        continue  # mirrors the MILP: sold original players are not re-bought
                    if any(c in prob.preferences.banned for c in ins):
                        continue
                    options.append((outs, ins))
            elif t == 0 and prob.preferences.hold_first_gw:
                options = [((), ())]
            else:
                options = list(_actions(prob, state, sold, bought))
            for outs, ins in options:
                if t == 0:
                    if not prob.preferences.forced_out <= set(outs):
                        continue
                    if not prob.preferences.forced_in <= set(ins):
                        continue
                if any(c in prob.preferences.locked for c in outs):
                    continue
                dec = GameweekDecision(transfers=transfer_objs(outs, ins), chip_id=chip)
                try:
                    res = apply_deadline(state, dec, price, rs)
                except RuleViolation:
                    continue
                evaluated += 1
                playing = tuple(sorted(p.player_code for p in res.playing_squad))
                v = gw_value(t, playing, ctype)
                paid = res.paid_transfers - (
                    max(0, prob.state.transfers_made - prob.state.free_transfers) if t == 0 else 0
                )
                gain = d * (v - hit_cost * paid)
                if ctype is not ChipType.FREE_HIT:
                    gain -= d * w.transfer_penalty * len(ins)
                if ctype is not None:
                    gain -= w.chip_values.get(ctype.value, 0.0)
                tail: tuple[GameweekDecision, ...]
                if prob.gameweeks[t] >= rs.num_gameweeks:
                    nxt_v, tail = 0.0, ()
                else:
                    nxt = advance(res, rs)
                    if ctype is ChipType.FREE_HIT:
                        n_sold, n_bought = sold, bought
                    else:
                        n_sold, n_bought = sold | set(outs), bought | set(ins)
                    nxt_v, tail = rec(
                        t + 1,
                        nxt,
                        frozenset(n_sold),
                        frozenset(n_bought),
                        used | ({chip} if chip else set()),
                    )
                total = gain + nxt_v
                if total > best[0] + 1e-9:
                    best = (total, (dec, *tail))
        return best

    obj, decisions = rec(0, prob.state, frozenset(), frozenset(), frozenset())
    return BruteResult(objective=obj, decisions=decisions, evaluated=evaluated)


def brute_force_initial_squad(prob: OptimizationProblem) -> tuple[float, tuple[int, ...]]:
    """Single-GW initial squad: best legal squad within the budget (``state.bank``)."""
    best: tuple[float, tuple[int, ...]] = (float("-inf"), ())
    for squad in _legal_squads(prob, prob.state.bank):
        v = gameweek_value(prob, 0, tuple(sorted(squad)), None)[0]
        if v > best[0] + 1e-9:
            best = (v, tuple(sorted(squad)))
    w = prob.config.objective
    first = prob.ruleset.transfers.initial_free_transfers_after_first_gameweek
    return best[0] + w.free_transfer_value * first, best[1]
