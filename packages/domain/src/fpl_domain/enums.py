"""Closed vocabularies used across the system."""

from __future__ import annotations

from enum import StrEnum


class Position(StrEnum):
    """FPL playing position. Values match the canonical short codes used in storage."""

    GK = "GK"
    DEF = "DEF"
    MID = "MID"
    FWD = "FWD"

    @classmethod
    def from_element_type(cls, element_type: int) -> Position:
        """Map the FPL API ``element_type`` (1..4) to a position."""
        mapping = {1: cls.GK, 2: cls.DEF, 3: cls.MID, 4: cls.FWD}
        try:
            return mapping[element_type]
        except KeyError as exc:  # pragma: no cover - defensive
            raise ValueError(f"unknown FPL element_type {element_type!r}") from exc

    @classmethod
    def parse(cls, value: str) -> Position:
        """Parse source spellings ("GK", "GKP", "Goalkeeper", "DEF", ...)."""
        v = value.strip().upper()
        aliases = {
            "GKP": "GK",
            "GOALKEEPER": "GK",
            "DEFENDER": "DEF",
            "MIDFIELDER": "MID",
            "FORWARD": "FWD",
        }
        return cls(aliases.get(v, v))


POSITIONS: tuple[Position, ...] = (Position.GK, Position.DEF, Position.MID, Position.FWD)


class ChipType(StrEnum):
    WILDCARD = "wildcard"
    FREE_HIT = "free_hit"
    BENCH_BOOST = "bench_boost"
    TRIPLE_CAPTAIN = "triple_captain"
    ASSISTANT_MANAGER = "assistant_manager"  # 2024-25 only; declared unsupported


class GameweekStatus(StrEnum):
    UPCOMING = "upcoming"
    LIVE = "live"  # deadline passed, matches in progress
    PROVISIONAL = "provisional"  # all matches played, scores may still be revised
    FINALIZED = "finalized"


class FixtureStatus(StrEnum):
    SCHEDULED = "scheduled"
    UNSCHEDULED = "unscheduled"  # postponed without a new date / no GW assigned
    LIVE = "live"
    PROVISIONAL = "provisional"
    FINAL = "final"


class PlayerStatus(StrEnum):
    """FPL availability status codes (bootstrap ``status``)."""

    AVAILABLE = "a"
    DOUBTFUL = "d"
    INJURED = "i"
    SUSPENDED = "s"
    UNAVAILABLE = "u"  # left club / on loan / not in squad
    NOT_IN_SQUAD = "n"  # ineligible (e.g. loanee vs parent club)

    @classmethod
    def parse(cls, value: str | None) -> PlayerStatus:
        if not value:
            return cls.AVAILABLE
        return cls(value.strip().lower())


class ActionType(StrEnum):
    """Top-level decision classes (§18, §26, §72.1)."""

    HOLD = "HOLD"
    TRANSFER = "TRANSFER"  # uses only free transfers
    HIT = "HIT"  # at least one paid transfer
    CHIP = "CHIP"


class ExplanationAction(StrEnum):
    HOLD = "HOLD"
    TRANSFER = "TRANSFER"
    CHIP = "CHIP"
    CAPTAIN = "CAPTAIN"
    BENCH = "BENCH"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class ModelStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    PRODUCTION = "production"
    REJECTED = "rejected"
    RETIRED = "retired"


class SolverStatus(StrEnum):
    OPTIMAL = "optimal"
    FEASIBLE_TIME_LIMIT = "feasible_time_limit"  # incumbent returned at timeout
    INFEASIBLE = "infeasible"
    NO_SOLUTION = "no_solution"
    ERROR = "error"
