"""Typed, immutable representation of one season's FPL rules (ADR-0005).

The YAML files under ``config/rules/`` are validated against this model; the committed
``config/rules/schema.yaml`` is generated from it (a test guarantees they never drift).
Nothing in the application hard-codes a rule value — every component receives a ``Ruleset``.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fpl_domain.enums import POSITIONS, ChipType, Position
from fpl_domain.hashing import content_hash

PositiveInt = Annotated[int, Field(ge=1)]
NonNegInt = Annotated[int, Field(ge=0)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class VerificationMethod(StrEnum):
    OFFICIAL = "official"  # read directly from an official FPL / Premier League page
    SECONDARY_SOURCE = "secondary-source"  # reputable summaries of the official announcement
    EMPIRICAL = "empirical"  # reproduced from historical data (e.g. official points)
    ASSUMED = "assumed"  # documented assumption pending verification


class SourceRef(_Frozen):
    title: str
    url: str
    accessed: date
    note: str | None = None


class Provenance(_Frozen):
    sources: tuple[SourceRef, ...] = ()
    verification: dict[str, VerificationMethod] = Field(
        default_factory=dict,
        description="Verification method per ruleset section (squad, transfers, chips, scoring…)",
    )
    notes: tuple[str, ...] = ()


class PositionCounts(_Frozen):
    GK: NonNegInt
    DEF: NonNegInt
    MID: NonNegInt
    FWD: NonNegInt

    def get(self, position: Position) -> int:
        return int(getattr(self, position.value))

    def as_dict(self) -> dict[Position, int]:
        return {p: self.get(p) for p in POSITIONS}


class SquadRules(_Frozen):
    size: PositiveInt
    initial_budget: PositiveInt = Field(description="Budget in tenths of £m (1000 = £100.0m)")
    positions: PositionCounts
    max_per_club: PositiveInt

    @model_validator(mode="after")
    def _positions_sum_to_size(self) -> SquadRules:
        total = sum(self.positions.as_dict().values())
        if total != self.size:
            raise ValueError(f"position quotas sum to {total}, squad size is {self.size}")
        return self


class LineupRules(_Frozen):
    starters: PositiveInt
    min_per_position: PositionCounts
    max_per_position: PositionCounts
    bench_goalkeepers: NonNegInt = 1
    bench_outfield: NonNegInt = 3

    @model_validator(mode="after")
    def _bounds_consistent(self) -> LineupRules:
        mins, maxs = self.min_per_position.as_dict(), self.max_per_position.as_dict()
        for p in POSITIONS:
            if mins[p] > maxs[p]:
                raise ValueError(f"lineup min > max for {p}")
        if sum(mins.values()) > self.starters or sum(maxs.values()) < self.starters:
            raise ValueError("lineup bounds cannot produce a legal XI")
        return self


class ChipFtPolicy(StrEnum):
    """What happens to banked free transfers in a gameweek where WC/FH is played."""

    RETAIN = "retain"  # banked count carried unchanged to the next GW (no +1)
    RETAIN_AND_ACCRUE = "retain_and_accrue"  # carried and +1 as if no transfer was made
    RESET = "reset"  # next GW starts with the base allowance (pre-2024-25 behaviour)


class TransferTopUp(_Frozen):
    """A scheduled change to free transfers (e.g. AFCON top-up, post-World-Cup unlimited)."""

    gameweek: PositiveInt
    set_free_transfers_to: NonNegInt | None = None
    unlimited: bool = False
    reason: str

    @model_validator(mode="after")
    def _one_kind(self) -> TransferTopUp:
        if self.unlimited == (self.set_free_transfers_to is not None):
            raise ValueError("top-up must set exactly one of unlimited / set_free_transfers_to")
        return self


class TransferRules(_Frozen):
    free_transfers_per_gameweek: PositiveInt
    max_banked_free_transfers: PositiveInt
    hit_cost: NonNegInt = Field(description="Points deducted per transfer beyond the allowance")
    first_gameweek_unlimited: bool = True
    initial_free_transfers_after_first_gameweek: PositiveInt = 1
    chip_ft_policy: ChipFtPolicy
    top_ups: tuple[TransferTopUp, ...] = ()

    def top_up_for(self, gameweek: int) -> TransferTopUp | None:
        for t in self.top_ups:
            if t.gameweek == gameweek:
                return t
        return None


class SellingPriceRule(StrEnum):
    HALF_PROFIT_FLOOR = "half_profit_floor"  # sell = buy + floor((now - buy)/2) if now > buy


class PricingRules(_Frozen):
    selling_price_rule: SellingPriceRule
    price_step: PositiveInt = Field(1, description="Price change granularity in tenths")
    min_price: PositiveInt = 35
    max_price: PositiveInt = 200


class ChipDefinition(_Frozen):
    id: str = Field(pattern=r"^[a-z_]+_[0-9]+$")
    type: ChipType
    first_gameweek: PositiveInt
    last_gameweek: PositiveInt
    supported: bool = True
    note: str | None = None

    @model_validator(mode="after")
    def _window(self) -> ChipDefinition:
        if self.first_gameweek > self.last_gameweek:
            raise ValueError(f"chip {self.id}: empty validity window")
        return self

    def valid_in(self, gameweek: int) -> bool:
        return self.first_gameweek <= gameweek <= self.last_gameweek


class ChipRules(_Frozen):
    one_chip_per_gameweek: bool = True
    triple_captain_multiplier: PositiveInt = 3
    free_hit_reverts: bool = True
    catalogue: tuple[ChipDefinition, ...]

    @model_validator(mode="after")
    def _unique_ids(self) -> ChipRules:
        ids = [c.id for c in self.catalogue]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate chip ids in catalogue")
        return self

    def by_id(self, chip_id: str) -> ChipDefinition:
        for c in self.catalogue:
            if c.id == chip_id:
                return c
        raise KeyError(chip_id)


class CaptaincyRules(_Frozen):
    captain_multiplier: PositiveInt = 2
    vice_promotes_if_captain_did_not_play: bool = True


class PerPosition(_Frozen):
    GK: int
    DEF: int
    MID: int
    FWD: int

    def get(self, position: Position) -> int:
        return int(getattr(self, position.value))


class AppearanceScoring(_Frozen):
    full_minutes_threshold: PositiveInt = 60
    points_below_threshold: int = 1
    points_at_or_above_threshold: int = 2


class CleanSheetScoring(_Frozen):
    min_minutes: PositiveInt = 60
    points: PerPosition


class GoalsConcededScoring(_Frozen):
    per_goals: PositiveInt = 2
    points: PerPosition


class SavesScoring(_Frozen):
    per_saves: PositiveInt = 3
    points: int = 1


DefensiveAction = Literal["clearances_blocks_interceptions", "tackles", "recoveries"]


class DefensiveContributionRule(_Frozen):
    threshold: PositiveInt
    points: int
    actions: tuple[DefensiveAction, ...]


class DefensiveContributionScoring(_Frozen):
    enabled: bool
    by_position: dict[Position, DefensiveContributionRule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _enabled_requires_rules(self) -> DefensiveContributionScoring:
        if self.enabled and not self.by_position:
            raise ValueError("defensive contribution enabled without per-position rules")
        return self


class BonusTieRule(StrEnum):
    FPL_STANDARD = "fpl_standard"


class BonusScoring(_Frozen):
    awards: tuple[int, ...] = (3, 2, 1)
    tie_rule: BonusTieRule = BonusTieRule.FPL_STANDARD


class ScoringRules(_Frozen):
    appearance: AppearanceScoring
    goal: PerPosition
    assist: int
    clean_sheet: CleanSheetScoring
    goals_conceded: GoalsConcededScoring
    saves: SavesScoring
    penalty_save: int
    penalty_miss: int
    yellow_card: int
    red_card: int
    own_goal: int
    defensive_contribution: DefensiveContributionScoring
    bonus: BonusScoring


class GameweekTiming(_Frozen):
    decision_buffer_minutes: NonNegInt = Field(
        90, description="Decision cutoff = deadline minus this buffer (ADR-0004)"
    )
    provisional_lag_minutes: NonNegInt = Field(
        150, description="Kickoff → provisional stats (incl. bonus) availability"
    )
    finalization_lag_hours: NonNegInt = Field(
        72, description="Last kickoff of a GW → scores locked/finalised (late lockdown, §46)"
    )


class Ruleset(_Frozen):
    """All rules for one season. Hash and version are stored with every run."""

    schema_version: Literal[1] = 1
    ruleset_version: str = Field(pattern=r"^\d{4}-\d{2}\.\d+$")
    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    num_gameweeks: PositiveInt = 38
    provenance: Provenance
    squad: SquadRules
    lineup: LineupRules
    transfers: TransferRules
    pricing: PricingRules
    captaincy: CaptaincyRules
    chips: ChipRules
    scoring: ScoringRules
    timing: GameweekTiming = GameweekTiming()
    bps_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _cross_checks(self) -> Ruleset:
        if not self.ruleset_version.startswith(self.season):
            raise ValueError("ruleset_version must start with the season code")
        if self.lineup.starters + self.lineup.bench_goalkeepers + self.lineup.bench_outfield != (
            self.squad.size
        ):
            raise ValueError("starters + bench must equal squad size")
        for p in POSITIONS:
            if self.lineup.max_per_position.get(p) > self.squad.positions.get(p):
                raise ValueError(f"lineup max for {p} exceeds squad quota")
        for c in self.chips.catalogue:
            if c.last_gameweek > self.num_gameweeks:
                raise ValueError(f"chip {c.id} window beyond season length")
        return self

    @property
    def content_hash(self) -> str:
        return content_hash(self)

    def legal_formations(self) -> tuple[tuple[int, int, int], ...]:
        """All (DEF, MID, FWD) counts for a legal XI, derived from the lineup bounds."""
        lo, hi = self.lineup.min_per_position, self.lineup.max_per_position
        gk = lo.GK  # exactly one GK in practice (min == max)
        outfield = self.lineup.starters - gk
        out: list[tuple[int, int, int]] = []
        for d in range(lo.DEF, hi.DEF + 1):
            for f in range(lo.FWD, hi.FWD + 1):
                m = outfield - d - f
                if lo.MID <= m <= hi.MID:
                    out.append((d, m, f))
        return tuple(out)

    def chips_valid_in(self, gameweek: int) -> tuple[ChipDefinition, ...]:
        return tuple(c for c in self.chips.catalogue if c.supported and c.valid_in(gameweek))
