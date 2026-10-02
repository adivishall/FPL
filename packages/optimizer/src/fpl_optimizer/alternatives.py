"""HOLD counterfactual and top-N distinct first-gameweek actions (§18, §29, ADR-0007).

Every optimisation returns the explicit HOLD plan (no transfers this gameweek, optimal
afterwards) next to the best plan, and up to N alternative plans whose *first-gameweek action*
(the set of players sold and bought now — the only part that is committed) differs. Alternatives
are found by adding a no-good cut on the previous actions and re-solving, so each is the best
plan among those not yet listed. All plans pass the independent validator.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from fpl_optimizer.milp import OptimizationError, Solution, build_and_solve
from fpl_optimizer.problem import OptimizationProblem
from fpl_optimizer.validate import ValidationResult, validate_solution


@dataclass
class PlanOption:
    label: str  # "best", "hold", "alt-1", …
    solution: Solution
    validation: ValidationResult
    gain_vs_hold: float  # objective difference (optimiser units) vs the HOLD plan

    @property
    def sells(self) -> frozenset[int]:
        return frozenset(self.solution.first.transfers_out)

    @property
    def buys(self) -> frozenset[int]:
        return frozenset(self.solution.first.transfers_in)

    @property
    def is_hold(self) -> bool:
        return not self.sells and not self.buys and self.solution.first.chip_id is None


def _cut(
    prob: OptimizationProblem, sol: Solution
) -> tuple[dict[tuple[str, int, int], float], float]:
    """No-good cut excluding exactly this first-GW (sells, buys) combination."""
    idx = prob.players.index()
    sells, buys = sol.first_action()
    coefs: dict[tuple[str, int, int], float] = {}
    for c, p in idx.items():
        coefs[("y", p, 0)] = 1.0 if c in buys else -1.0
        coefs[("z", p, 0)] = 1.0 if c in sells else -1.0
    return coefs, float(len(sells) + len(buys) - 1)


def hold_problem(prob: OptimizationProblem) -> OptimizationProblem:
    return replace(prob, preferences=replace(prob.preferences, hold_first_gw=True))


def solve_with_alternatives(prob: OptimizationProblem, n_alternatives: int = 3) -> list[PlanOption]:
    hold_sol = build_and_solve(hold_problem(prob))
    hold_val = validate_solution(hold_problem(prob), hold_sol)
    options = [PlanOption("hold", hold_sol, hold_val, 0.0)]
    cuts: list[tuple[dict[tuple[str, int, int], float], float]] = []
    for k in range(n_alternatives + 1):
        try:
            sol = build_and_solve(prob, cuts)
        except OptimizationError:
            break  # no further distinct feasible action
        val = validate_solution(prob, sol)
        label = "best" if k == 0 else f"alt-{k}"
        options.append(PlanOption(label, sol, val, sol.objective - hold_sol.objective))
        cuts.append(_cut(prob, sol))
    return options
