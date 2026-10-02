"""Core domain entities (§53.1). Immutable, typed, serialisable.

Identity conventions
--------------------
* ``season``: season code, e.g. ``"2026-27"``.
* Players and teams have a **stable cross-season identity** (FPL ``code``) and a per-season
  ``source_id`` (FPL element / team id). Canonical keys inside a season are ``(season,
  source_id)``; cross-season joins use ``code``.
* Prices are integers in tenths of £m (``55`` = £5.5m).
* All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from fpl_domain.enums import (
    FixtureStatus,
    GameweekStatus,
    PlayerStatus,
    Position,
    Severity,
)

PriceTenths = Annotated[int, Field(ge=0, le=300, description="Price in tenths of £m")]
GameweekNumber = Annotated[int, Field(ge=1, le=60)]
SeasonCode = Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")]


class Entity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Season(Entity):
    code: SeasonCode
    start_date: date
    end_date: date
    ruleset_version: str
    schema_version: int = 1


class Gameweek(Entity):
    season: SeasonCode
    number: GameweekNumber
    deadline_at: AwareDatetime
    status: GameweekStatus
    first_kickoff_at: AwareDatetime | None = None
    last_kickoff_at: AwareDatetime | None = None
    locked_at: AwareDatetime | None = None
    finalized_at: AwareDatetime | None = None


class Team(Entity):
    season: SeasonCode
    source_id: int = Field(ge=1)
    code: int = Field(description="Stable cross-season FPL team code")
    name: str
    short_name: str = Field(min_length=2, max_length=4)
    strength_overall_home: int | None = None
    strength_overall_away: int | None = None
    strength_attack_home: int | None = None
    strength_attack_away: int | None = None
    strength_defence_home: int | None = None
    strength_defence_away: int | None = None


class Player(Entity):
    season: SeasonCode
    source_id: int = Field(ge=1, description="FPL element id (per season)")
    code: int = Field(description="Stable cross-season FPL player code")
    team_code: int
    position: Position
    web_name: str
    first_name: str | None = None
    last_name: str | None = None
    price: PriceTenths
    status: PlayerStatus = PlayerStatus.AVAILABLE
    news: str | None = None
    news_added: AwareDatetime | None = None
    chance_of_playing_next_round: int | None = Field(None, ge=0, le=100)
    source_updated_at: AwareDatetime


class Fixture(Entity):
    season: SeasonCode
    source_id: int = Field(ge=1)
    gameweek: GameweekNumber | None = Field(None, description="None when unscheduled")
    kickoff_at: AwareDatetime | None
    home_team_code: int
    away_team_code: int
    status: FixtureStatus
    home_score: int | None = Field(None, ge=0)
    away_score: int | None = Field(None, ge=0)
    home_difficulty: int | None = Field(None, ge=1, le=5)
    away_difficulty: int | None = Field(None, ge=1, le=5)
    schedule_available_at: AwareDatetime = Field(
        description="When this fixture's (gameweek, kickoff) assignment became knowable (ADR-0004)"
    )


class PlayerSnapshot(Entity):
    """What was known about a player at a precise timestamp (live bootstrap captures)."""

    season: SeasonCode
    player_code: int
    captured_at: AwareDatetime
    price: PriceTenths
    selected_by_percent: float | None = Field(None, ge=0.0, le=100.0)
    form: float | None = None
    status: PlayerStatus
    news: str | None = None
    news_added: AwareDatetime | None = None
    chance_of_playing_next_round: int | None = Field(None, ge=0, le=100)
    penalties_order: int | None = None
    direct_freekicks_order: int | None = None
    corners_and_indirect_freekicks_order: int | None = None
    official_price_change_percent: float | None = None
    source: str
    source_version: str


class PlayerGameweekStats(Entity):
    """Official FPL scoring stats and underlying metrics for one player in one fixture."""

    season: SeasonCode
    gameweek: GameweekNumber
    fixture_source_id: int
    player_code: int
    team_code: int
    opponent_team_code: int
    was_home: bool
    kickoff_at: AwareDatetime
    minutes: int = Field(ge=0, le=130)
    starts: int | None = Field(None, ge=0, le=1)
    goals_scored: int = Field(ge=0)
    assists: int = Field(ge=0)
    clean_sheets: int = Field(ge=0, le=1)
    goals_conceded: int = Field(ge=0)
    own_goals: int = Field(ge=0)
    penalties_saved: int = Field(ge=0)
    penalties_missed: int = Field(ge=0)
    yellow_cards: int = Field(ge=0, le=2)
    red_cards: int = Field(ge=0, le=1)
    saves: int = Field(ge=0)
    bonus: int = Field(ge=0, le=3)
    bps: int
    clearances_blocks_interceptions: int | None = Field(None, ge=0)
    tackles: int | None = Field(None, ge=0)
    recoveries: int | None = Field(None, ge=0)
    defensive_contribution: int | None = Field(None, ge=0)
    expected_goals: float | None = Field(None, ge=0.0)
    expected_assists: float | None = Field(None, ge=0.0)
    expected_goals_conceded: float | None = Field(None, ge=0.0)
    influence: float | None = None
    creativity: float | None = None
    threat: float | None = None
    total_points: int
    price: PriceTenths
    selected: int | None = Field(None, ge=0)
    transfers_in: int | None = Field(None, ge=0)
    transfers_out: int | None = Field(None, ge=0)
    available_at: AwareDatetime
    finalized: bool = False


class DataQualityEvent(Entity):
    id: str
    source: str
    issue_code: str
    severity: Severity
    detected_at: AwareDatetime
    entity_type: str
    entity_id: str | None = None
    details: dict[str, object] = Field(default_factory=dict)
    resolved_at: datetime | None = None


class AuditEvent(Entity):
    id: str
    actor_type: str  # "user" | "system" | "api_key"
    actor_id: str | None
    action: str
    entity_type: str
    entity_id: str | None
    created_at: AwareDatetime
    payload_hash: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
