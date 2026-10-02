"""Independent validation of multi-gameweek plans (§91: every optimiser output is validated by a
separate rules validator; §77.2: applying a transfer sequence yields the optimiser's squad).

The validator *replays* the plan through the domain state machine (``apply_deadline`` →
``advance``) rather than re-implementing any rule, so optimiser and validator can never share a
bug in formulation code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from fpl_domain.errors import RuleViolation
from fpl_domain.rules.model import Ruleset
from fpl_domain.state import GameweekDecision, ManagerState, advance, apply_deadline


class StepReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    gameweek: int
    transfers: int
    paid_transfers: int
    hit_points: int
    free_transfers_at_start: int
    free_transfers_next: int | None
    bank_after: int
    chip_id: str | None
    squad: tuple[int, ...]


class ValidationReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    valid: bool
    violations: tuple[str, ...]
    steps: tuple[StepReport, ...]
    final_state: ManagerState | None

    @property
    def total_hit_points(self) -> int:
        return sum(s.hit_points for s in self.steps)


def validate_plan(
    state: ManagerState,
    decisions: Sequence[GameweekDecision],
    prices: Mapping[int, int] | Sequence[Mapping[int, int]],
    ruleset: Ruleset,
) -> ValidationReport:
    """Replay ``decisions`` for consecutive gameweeks starting at ``state.gameweek``.

    ``prices`` is either one price map used for every gameweek (the optimiser's assumption) or one
    map per gameweek.
    """
    steps: list[StepReport] = []
    current = state
    for i, decision in enumerate(decisions):
        pmap = prices[i] if isinstance(prices, Sequence) else prices
        try:
            out = apply_deadline(current, decision, pmap, ruleset)
            nxt = advance(out, ruleset) if out.gameweek < ruleset.num_gameweeks else None
        except RuleViolation as exc:
            return ValidationReport(
                valid=False,
                violations=(f"GW{current.gameweek}: {exc}",),
                steps=tuple(steps),
                final_state=None,
            )
        steps.append(
            StepReport(
                gameweek=out.gameweek,
                transfers=len(out.transfers),
                paid_transfers=out.paid_transfers,
                hit_points=out.hit_points,
                free_transfers_at_start=out.free_transfers_at_start,
                free_transfers_next=nxt.free_transfers if nxt else None,
                bank_after=out.bank_after,
                chip_id=out.chip.chip_id if out.chip else None,
                squad=tuple(sorted(p.player_code for p in out.playing_squad)),
            )
        )
        if nxt is None:
            return ValidationReport(valid=True, violations=(), steps=tuple(steps), final_state=None)
        current = nxt
    return ValidationReport(valid=True, violations=(), steps=tuple(steps), final_state=current)
