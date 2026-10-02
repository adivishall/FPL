"""Typed contracts for the public FPL API responses we consume.

``extra="ignore"``: the API adds fields over time; we validate what we depend on and keep the
raw payload unchanged in the raw store, so nothing is lost when the schema evolves.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Api(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ApiEvent(_Api):
    id: int = Field(ge=1, le=60)
    name: str
    deadline_time: datetime
    finished: bool
    data_checked: bool = False
    is_current: bool = False
    is_next: bool = False
    is_previous: bool = False
    average_entry_score: int | None = None


class ApiTeam(_Api):
    id: int = Field(ge=1)
    code: int
    name: str
    short_name: str
    strength: int | None = None
    strength_overall_home: int | None = None
    strength_overall_away: int | None = None
    strength_attack_home: int | None = None
    strength_attack_away: int | None = None
    strength_defence_home: int | None = None
    strength_defence_away: int | None = None


class ApiPriceProjection(_Api):
    offset: int
    projected_percent: float
    likelihood: int


class ApiElement(_Api):
    id: int = Field(ge=1)
    code: int
    web_name: str
    first_name: str | None = None
    second_name: str | None = None
    team: int
    team_code: int
    element_type: int = Field(ge=1, le=5)
    now_cost: int = Field(ge=0, le=300)
    status: str
    news: str | None = ""
    news_added: datetime | None = None
    chance_of_playing_next_round: int | None = Field(None, ge=0, le=100)
    chance_of_playing_this_round: int | None = Field(None, ge=0, le=100)
    selected_by_percent: float = Field(ge=0.0, le=100.0)
    form: float | None = None
    cost_change_start: int | None = None
    transfers_in_event: int | None = None
    transfers_out_event: int | None = None
    penalties_order: int | None = None
    direct_freekicks_order: int | None = None
    corners_and_indirect_freekicks_order: int | None = None
    # Official 2026/27 Price Change Predictor fields (§66). Optional: absent in older seasons.
    price_change_percent: float | None = None
    price_change_hourly_rate: float | None = None
    price_change_calibrating: bool | None = None
    price_change_locked_until: datetime | None = None
    price_change_projections: list[ApiPriceProjection] | None = None


class ApiElementType(_Api):
    id: int
    singular_name_short: str
    squad_select: int
    squad_min_play: int
    squad_max_play: int


class BootstrapStatic(_Api):
    events: list[ApiEvent]
    teams: list[ApiTeam]
    elements: list[ApiElement]
    element_types: list[ApiElementType]


class ApiFixture(_Api):
    id: int
    code: int | None = None
    event: int | None = None
    finished: bool
    finished_provisional: bool = False
    kickoff_time: datetime | None = None
    started: bool | None = None
    team_h: int
    team_a: int
    team_h_score: int | None = None
    team_a_score: int | None = None
    team_h_difficulty: int | None = None
    team_a_difficulty: int | None = None


class ApiLiveStats(_Api):
    minutes: int
    goals_scored: int
    assists: int
    clean_sheets: int
    goals_conceded: int
    own_goals: int
    penalties_saved: int
    penalties_missed: int
    yellow_cards: int
    red_cards: int
    saves: int
    bonus: int
    bps: int
    total_points: int
    starts: int | None = None
    expected_goals: float | None = None
    expected_assists: float | None = None
    expected_goals_conceded: float | None = None
    clearances_blocks_interceptions: int | None = None
    tackles: int | None = None
    recoveries: int | None = None
    defensive_contribution: int | None = None


class ApiLiveElement(_Api):
    id: int
    stats: ApiLiveStats


class EventLive(_Api):
    elements: list[ApiLiveElement]


class ApiEntry(_Api):
    id: int
    name: str
    started_event: int = 1
    current_event: int | None = None
    summary_overall_points: int | None = None
    summary_overall_rank: int | None = None
    last_deadline_bank: int | None = None
    last_deadline_value: int | None = None


class ApiHistoryEvent(_Api):
    event: int
    points: int
    total_points: int
    rank: int | None = None
    overall_rank: int | None = None
    bank: int
    value: int
    event_transfers: int
    event_transfers_cost: int
    points_on_bench: int


class ApiChipPlay(_Api):
    name: str  # wildcard | freehit | bboost | 3xc | manager
    time: datetime
    event: int


class EntryHistory(_Api):
    current: list[ApiHistoryEvent]
    chips: list[ApiChipPlay]


class ApiPick(_Api):
    element: int
    position: int = Field(ge=1, le=15)
    multiplier: int = Field(ge=0, le=3)
    is_captain: bool
    is_vice_captain: bool


class EntryPicks(_Api):
    active_chip: str | None = None
    picks: list[ApiPick] = Field(min_length=15, max_length=15)
    entry_history: ApiHistoryEvent


class ApiTransfer(_Api):
    element_in: int
    element_in_cost: int
    element_out: int
    element_out_cost: int
    entry: int
    event: int
    time: datetime


class ApiStanding(_Api):
    entry: int
    entry_name: str
    player_name: str
    rank: int
    last_rank: int | None = None
    total: int
    event_total: int


class ApiStandingsPage(_Api):
    has_next: bool
    page: int
    results: list[ApiStanding]


class LeagueStandings(_Api):
    standings: ApiStandingsPage


CHIP_NAME_MAP = {
    "wildcard": "wildcard",
    "freehit": "free_hit",
    "bboost": "bench_boost",
    "3xc": "triple_captain",
    "manager": "assistant_manager",
}
