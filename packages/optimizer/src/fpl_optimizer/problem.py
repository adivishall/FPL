"""Optimisation problem definition (§16, §61, ADR-0007).

Prices are in tenths of £m (as everywhere in the domain). Expected points ``ev[p, t]`` are per
player per horizon gameweek (the sum over that gameweek's fixtures; 0 for a blank). ``q10`` is the
10th percentile of the same distribution and drives the optional risk penalty.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field

from fpl_domain.config import VersionedConfig, load_versioned_config
from fpl_domain.enums import POSITIONS, Position
from fpl_domain.rules import Ruleset
from fpl_domain.state import ManagerState

POS_INDEX = {p: i for i, p in enumerate(POSITIONS)}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ObjectiveWeights(_Frozen):
    discount: float = Field(0.92, gt=0, le=1)
    risk_aversion: float = Field(0.0, ge=0, le=1)
    transfer_penalty: float = Field(0.0, ge=0)
    vice_weight: float = Field(0.05, ge=0, le=1)
    bench_weights: tuple[float, float, float] = (0.10, 0.05, 0.02)
    bench_gk_weight: float = Field(0.03, ge=0, le=1)
    free_transfer_value: float = Field(1.0, ge=0)
    bank_value_per_tenth: float = Field(0.0, ge=0)
    terminal_squad_weight: float = Field(0.0, ge=0)
    chip_values: dict[str, float] = Field(default_factory=dict)


class SolverSettings(_Frozen):
    time_limit_seconds: float = Field(60.0, gt=0)
    mip_rel_gap: float = Field(0.0, ge=0)
    threads: int = Field(1, ge=1)
    random_seed: int = 7


class PoolSettings(_Frozen):
    per_position_top_ev: int = 30
    per_position_top_value: int = 12
    per_position_cheapest: int = 5


class DecisionThresholds(_Frozen):
    min_gain: float = 0.5
    min_prob_positive: float = 0.6


class StabilitySettings(_Frozen):
    perturbations: int = 20
    stable_share: float = 0.7
    relative_sd: float = Field(0.15, ge=0, description="epistemic SD of a player's EV level")
    weekly_sd: float = Field(0.08, ge=0, description="extra per-gameweek EV noise")
    tolerance: float = Field(
        0.25,
        ge=0,
        description="objective points within which the "
        "recommended action still counts as near-optimal",
    )
    seed: int = 11


class OptimizerConfig(_Frozen):
    profile: str = "default"
    description: str = ""
    objective: ObjectiveWeights = Field(default_factory=ObjectiveWeights)
    solver: SolverSettings = Field(default_factory=SolverSettings)
    pool: PoolSettings = Field(default_factory=PoolSettings)
    decision: DecisionThresholds = Field(default_factory=DecisionThresholds)
    stability: StabilitySettings = Field(default_factory=StabilitySettings)
    config_ref: str | None = None


def load_optimizer_config(profile: str = "default") -> OptimizerConfig:
    vc: VersionedConfig = load_versioned_config("optimizer", profile)
    cfg = vc.parse(OptimizerConfig)
    return cfg.model_copy(update={"config_ref": vc.ref})


@dataclass(frozen=True)
class PlayerTable:
    """The candidate universe for one optimisation (owned players must be included)."""

    code: npt.NDArray[np.int64]
    position: npt.NDArray[np.int64]  # 0..3 (GK, DEF, MID, FWD)
    team: npt.NDArray[np.int64]
    price: npt.NDArray[np.int64]  # current purchase price (tenths)
    ev: npt.NDArray[np.float64]  # [P, T]
    q10: npt.NDArray[np.float64] | None = None  # [P, T]
    name: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        n = len(self.code)
        if len(set(self.code.tolist())) != n:
            raise ValueError("duplicate player codes")
        for arr in (self.position, self.team, self.price):
            if len(arr) != n:
                raise ValueError("player arrays must have equal length")
        if self.ev.ndim != 2 or self.ev.shape[0] != n:
            raise ValueError("ev must be [players, gameweeks]")
        if self.q10 is not None and self.q10.shape != self.ev.shape:
            raise ValueError("q10 must match ev")

    @property
    def n(self) -> int:
        return len(self.code)

    @property
    def horizon(self) -> int:
        return int(self.ev.shape[1])

    def index(self) -> dict[int, int]:
        return {int(c): i for i, c in enumerate(self.code)}

    def teams_by_code(self) -> dict[int, int]:
        """Current club of every player in the table (code → team code)."""
        return {int(c): int(t) for c, t in zip(self.code, self.team, strict=True)}

    def downside(self) -> npt.NDArray[np.float64]:
        if self.q10 is None:
            return np.zeros_like(self.ev)
        return np.clip(self.ev - self.q10, 0.0, None)

    def subset(self, rows: npt.NDArray[np.int64]) -> PlayerTable:
        return PlayerTable(
            code=self.code[rows],
            position=self.position[rows],
            team=self.team[rows],
            price=self.price[rows],
            ev=self.ev[rows],
            q10=None if self.q10 is None else self.q10[rows],
            name=None if self.name is None else tuple(self.name[i] for i in rows),
        )

    def positions_map(self) -> dict[int, Position]:
        return {int(c): POSITIONS[int(p)] for c, p in zip(self.code, self.position, strict=True)}


@dataclass(frozen=True)
class Preferences:
    """User constraints (§22 'locked players / preferences')."""

    locked: frozenset[int] = frozenset()  # owned players that must be kept all horizon
    banned: frozenset[int] = frozenset()  # players that must not be bought
    forced_out: frozenset[int] = frozenset()  # owned players that must be sold in the first GW
    forced_in: frozenset[int] = frozenset()  # players that must be bought in the first GW
    max_transfers_per_gw: int | None = None
    hold_first_gw: bool = False  # no transfers in the first GW (the HOLD counterfactual)
    max_hits_total: int | None = None


@dataclass(frozen=True)
class OptimizationProblem:
    state: ManagerState
    ruleset: Ruleset
    players: PlayerTable
    gameweeks: tuple[int, ...]
    config: OptimizerConfig = field(default_factory=OptimizerConfig)
    preferences: Preferences = field(default_factory=Preferences)
    # chip_id → gameweeks in which the optimiser may play it (empty = chip not considered)
    chip_options: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    # chip_id → gameweek where it must be played (what-if analysis, §64.1)
    forced_chips: Mapping[str, int] = field(default_factory=dict)
    initial_squad_mode: bool = False  # build a squad from scratch (§15)

    def __post_init__(self) -> None:
        if len(self.gameweeks) != self.players.horizon:
            raise ValueError("gameweeks must match the ev horizon")
        if list(self.gameweeks) != list(
            range(self.gameweeks[0], self.gameweeks[0] + len(self.gameweeks))
        ):
            raise ValueError("gameweeks must be consecutive")
        if self.gameweeks[0] != self.state.gameweek:
            raise ValueError("horizon must start at the state's gameweek")
        idx = self.players.index()
        missing = [c for c in self.state.codes if c not in idx]
        if missing:
            raise ValueError(f"owned players missing from the player table: {missing}")

    def describe(self) -> dict[str, Any]:
        return {
            "gameweeks": list(self.gameweeks),
            "n_players": self.players.n,
            "profile": self.config.profile,
            "config_ref": self.config.config_ref,
            "chip_options": {k: list(v) for k, v in self.chip_options.items()},
            "forced_chips": dict(self.forced_chips),
            "preferences": {
                "locked": sorted(self.preferences.locked),
                "banned": sorted(self.preferences.banned),
                "forced_out": sorted(self.preferences.forced_out),
                "forced_in": sorted(self.preferences.forced_in),
                "max_transfers_per_gw": self.preferences.max_transfers_per_gw,
                "hold_first_gw": self.preferences.hold_first_gw,
            },
            "ruleset_version": self.ruleset.ruleset_version,
        }
