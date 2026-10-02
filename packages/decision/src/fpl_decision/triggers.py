"""Re-optimisation triggers (§62.1): decide whether new information invalidates a plan.

Compares the inputs a recommendation was built on with the current inputs and lists every
material change: availability changes for owned / planned players, price changes that affect the
plan's affordability, fixture changes (postponements, new doubles) in the horizon, gameweek
finalisation and ruleset changes. Each trigger carries the evidence (old → new).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel


class Trigger(BaseModel):
    kind: str
    detail: str
    player_code: int | None = None
    old: str | None = None
    new: str | None = None


@dataclass(frozen=True)
class PlanInputs:
    ruleset_version: str
    finalized_gameweeks: frozenset[int]
    fixtures: Mapping[int, frozenset[int]]  # gameweek → fixture ids
    status: Mapping[int, tuple[str, float | None]]  # player → (FPL status, chance of playing)
    prices: Mapping[int, int]


def reoptimization_triggers(
    old: PlanInputs,
    new: PlanInputs,
    owned: Iterable[int],
    planned_buys: Iterable[int],
    horizon: Iterable[int],
    bank_after_plan: int = 0,
) -> list[Trigger]:
    out: list[Trigger] = []
    watch = set(owned) | set(planned_buys)
    if old.ruleset_version != new.ruleset_version:
        out.append(
            Trigger(
                kind="rules_change",
                detail="ruleset version changed",
                old=old.ruleset_version,
                new=new.ruleset_version,
            )
        )
    for gw in sorted(new.finalized_gameweeks - old.finalized_gameweeks):
        out.append(Trigger(kind="gameweek_finalised", detail=f"GW{gw} finalised"))
    for gw in horizon:
        a, b = old.fixtures.get(gw, frozenset()), new.fixtures.get(gw, frozenset())
        if a != b:
            out.append(
                Trigger(
                    kind="fixture_change",
                    detail=f"GW{gw}: {len(a - b)} fixture(s) removed, {len(b - a)} added",
                    old=str(sorted(a - b)),
                    new=str(sorted(b - a)),
                )
            )
    for c in sorted(watch):
        so, sn = old.status.get(c), new.status.get(c)
        if so != sn and sn is not None:
            out.append(
                Trigger(
                    kind="availability_change",
                    player_code=c,
                    detail="FPL status / chance of playing changed",
                    old=str(so),
                    new=str(sn),
                )
            )
    extra_cost = 0
    for c in sorted(set(planned_buys)):
        po, pn = old.prices.get(c), new.prices.get(c)
        if po is not None and pn is not None and pn != po:
            extra_cost += pn - po
            out.append(
                Trigger(
                    kind="price_change",
                    player_code=c,
                    detail="planned purchase price changed",
                    old=str(po),
                    new=str(pn),
                )
            )
    for c in sorted(set(owned)):
        po, pn = old.prices.get(c), new.prices.get(c)
        if po is not None and pn is not None and pn != po:
            out.append(
                Trigger(
                    kind="price_change",
                    player_code=c,
                    detail="owned player price changed (selling value may change)",
                    old=str(po),
                    new=str(pn),
                )
            )
    if extra_cost > bank_after_plan:
        out.append(
            Trigger(
                kind="affordability",
                detail=f"planned buys now cost {extra_cost} more"
                f" than the {bank_after_plan} left in the bank — plan infeasible",
            )
        )
    return out
