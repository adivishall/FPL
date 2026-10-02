"""HTTP request/response schemas (validated input; §75 'validate all inputs')."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ManagerKey = str


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PickIn(_Req):
    player_code: int = Field(gt=0)
    purchase_price: int | None = Field(None, ge=35, le=200, description="tenths of £m")


class SquadIn(_Req):
    """Manual squad entry — a first-class alternative to live sync (ADR-0001 #5)."""

    manager_key: ManagerKey = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    picks: list[PickIn] = Field(min_length=15, max_length=15)
    bank: int = Field(ge=0, le=1000, description="tenths of £m")
    free_transfers: int = Field(1, ge=0, le=5)
    chips_used: list[str] = Field(default_factory=list)
    captain: int | None = None


class SyncIn(_Req):
    manager_key: ManagerKey = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    manager_id: int = Field(gt=0, description="public FPL entry id (no credentials)")


class PreferencesIn(_Req):
    locked: list[int] = Field(default_factory=list)
    banned: list[int] = Field(default_factory=list)
    max_transfers_per_gw: int | None = Field(None, ge=0, le=15)


class OptimizeIn(_Req):
    manager_key: ManagerKey
    profile: Literal["default", "conservative", "aggressive"] = "default"
    horizon: int | None = Field(None, ge=1, le=10)
    alternatives: int = Field(3, ge=0, le=6)
    preferences: PreferencesIn = Field(default_factory=PreferencesIn)
    chip_options: dict[str, list[int]] = Field(default_factory=dict)


class SquadBuildIn(_Req):
    budget: int = Field(1000, ge=800, le=1100)
    horizon: int | None = Field(None, ge=1, le=10)
    profile: Literal["default", "conservative", "aggressive"] = "default"


class ReplacementIn(_Req):
    manager_key: ManagerKey
    out_player: int = Field(gt=0)
    profile: Literal["default", "conservative", "aggressive"] = "default"
    horizon: int | None = Field(None, ge=1, le=10)
    candidates: int = Field(5, ge=1, le=10)


class LineupIn(_Req):
    manager_key: ManagerKey
    chip: Literal["bench_boost", "triple_captain"] | None = None


class ChipSimIn(_Req):
    manager_key: ManagerKey
    horizon: int | None = Field(None, ge=1, le=10)
    chip_id: str | None = Field(None, description="what-if: force this chip …")
    gameweek: int | None = Field(None, ge=1, le=38, description="… in this gameweek")


class ScenarioIn(_Req):
    kind: Literal[
        "minutes_downside",
        "minutes_upside",
        "injury_shock",
        "team_attack_downside",
        "fixture_shock",
        "price_shock",
        "conservative",
    ]
    players: list[int] = Field(default_factory=list)
    teams: list[int] = Field(default_factory=list)
    gameweeks: list[int] = Field(default_factory=list)
    magnitude: float = Field(0.5, ge=0, le=20)


class WhatIfIn(_Req):
    manager_key: ManagerKey
    scenarios: list[ScenarioIn] = Field(min_length=1, max_length=8)
    horizon: int | None = Field(None, ge=1, le=10)
    sells: list[int] = Field(default_factory=list, description="what-if transfer to compare")
    buys: list[int] = Field(default_factory=list)


class RecommendationIn(_Req):
    manager_key: ManagerKey
    profile: Literal["default", "conservative", "aggressive"] = "default"
    horizon: int | None = Field(None, ge=1, le=10)
    stability: bool = True
    scenarios: bool = True
    chips: bool = True


class FeedbackIn(_Req):
    followed: Literal["followed", "ignored", "partial"]
    note: str | None = Field(None, max_length=2000)
    realized_points: float | None = Field(None, ge=-50, le=300)


class BacktestIn(_Req):
    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    gameweeks: list[int] | None = None
    horizon: int | None = Field(None, ge=1, le=10)
    n_sims: int = Field(500, ge=100, le=5000)
    profile: Literal["default", "conservative", "aggressive"] = "default"
