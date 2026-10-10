"""HTTP request/response schemas (validated input; §75 'validate all inputs')."""

from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fpl_forecasting.pipeline import SUPPORTED_HORIZON

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
    horizon: int | None = Field(None, ge=1, le=SUPPORTED_HORIZON)
    alternatives: int = Field(3, ge=0, le=6)
    preferences: PreferencesIn = Field(default_factory=PreferencesIn)
    chip_options: dict[str, list[int]] = Field(default_factory=dict)


class SquadBuildIn(_Req):
    budget: int = Field(1000, ge=800, le=1100)
    horizon: int | None = Field(None, ge=1, le=SUPPORTED_HORIZON)
    profile: Literal["default", "conservative", "aggressive"] = "default"


class ReplacementIn(_Req):
    manager_key: ManagerKey
    out_player: int = Field(gt=0)
    profile: Literal["default", "conservative", "aggressive"] = "default"
    horizon: int | None = Field(None, ge=1, le=SUPPORTED_HORIZON)
    candidates: int = Field(5, ge=1, le=10)


class LineupIn(_Req):
    manager_key: ManagerKey
    chip: Literal["bench_boost", "triple_captain"] | None = None


class ChipSimIn(_Req):
    manager_key: ManagerKey
    horizon: int | None = Field(None, ge=1, le=SUPPORTED_HORIZON)
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
    horizon: int | None = Field(None, ge=1, le=SUPPORTED_HORIZON)
    sells: list[int] = Field(default_factory=list, description="what-if transfer to compare")
    buys: list[int] = Field(default_factory=list)


class RecommendationIn(_Req):
    manager_key: ManagerKey
    profile: Literal["default", "conservative", "aggressive"] = "default"
    horizon: int | None = Field(None, ge=1, le=SUPPORTED_HORIZON)
    stability: bool = True
    scenarios: bool = True
    chips: bool = True
    state_id: str | None = Field(
        None,
        max_length=80,
        description="evaluate exactly this stored squad state (default: the latest one)",
    )


class RegisterIn(_Req):
    invite_code: str = Field(min_length=8, max_length=64)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=10, max_length=256)


class LoginIn(_Req):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class InviteIn(_Req):
    label: str = Field("beta", min_length=1, max_length=64)
    days: int | None = Field(14, ge=1, le=365, description="validity; null = no expiry")


class ManagerCreateIn(_Req):
    label: str | None = Field(None, max_length=64)


class AccountPreferencesIn(_Req):
    analytics_opt_out: bool


class EventIn(_Req):
    event: str = Field(min_length=1, max_length=64, pattern=r"^[a-z_]+$")
    props: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=12)


class FeedbackIn(_Req):
    followed: Literal["followed", "ignored", "partial"]
    note: str | None = Field(None, max_length=2000)
    realized_points: float | None = Field(None, ge=-50, le=300)


class BacktestIn(_Req):
    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    gameweeks: list[int] | None = None
    horizon: int | None = Field(None, ge=1, le=SUPPORTED_HORIZON)
    n_sims: int = Field(500, ge=100, le=5000)
    profile: Literal["default", "conservative", "aggressive"] = "default"


class ManagerSettingsIn(_Req):
    """Settings screen (§73.1): horizon, risk profile, differential tolerance, notifications."""

    horizon: int = Field(5, ge=1, le=SUPPORTED_HORIZON)
    profile: Literal["default", "conservative", "aggressive"] = "default"
    differential_tolerance: float = Field(0.0, ge=0.0, le=1.0)
    league_rivals: list[int] = Field(default_factory=list, max_length=20)
    notify_min_gain: float = Field(1.0, ge=0.0, le=20.0)
    notify_injuries: bool = True
    timezone: str = Field("Europe/London", max_length=64, description="IANA zone for reminders")
    webhook_url: str | None = Field(None, max_length=512, description="HTTPS, allow-listed host")

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone '{v}'") from exc
        return v


class AlertsIn(_Req):
    manager_key: ManagerKey = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


class MarkReadIn(_Req):
    ids: list[str] = Field(min_length=1, max_length=200)
