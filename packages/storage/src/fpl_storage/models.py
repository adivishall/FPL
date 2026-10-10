"""PostgreSQL schema (§8, §54) as SQLAlchemy 2.0 declarative models.

Conventions
-----------
* Surrogate ``id`` primary keys; natural keys enforced with UNIQUE constraints so ingestion can
  upsert idempotently (§55).
* Canonical observation rows carry ``available_at`` (ADR-0004), ``source``, ``raw_snapshot_id``
  and ``content_hash`` (lineage, §6 "Audit").
* Prices are SMALLINT tenths of £m. Timestamps are ``timestamptz``. Flexible payloads are JSONB.
* Immutable run records (optimisation runs, recommendations, predictions, backtests) are only
  ever inserted, never updated in place (ADR-0009), except for status transitions of jobs.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSON = JSONB


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _ts(nullable: bool = False, **kw: Any) -> Any:
    return mapped_column(DateTime(timezone=True), nullable=nullable, **kw)


def _created() -> Any:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# =============================================================== lineage / operations


class RawSnapshot(Base):
    """Immutable raw payload captured from a source (§6.1, §55.1 'Raw capture')."""

    __tablename__ = "raw_snapshots"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # raw_<sha256[:24]>
    source: Mapped[str] = mapped_column(String(64), index=True)
    resource: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    source_version: Mapped[str | None] = mapped_column(String(80))  # e.g. git commit SHA
    retrieved_at: Mapped[datetime] = _ts()
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(64))
    storage_uri: Mapped[str] = mapped_column(Text)
    schema_version: Mapped[str] = mapped_column(String(32))
    job_id: Mapped[str | None] = mapped_column(ForeignKey("data_jobs.id"))
    __table_args__ = (UniqueConstraint("source", "resource", "sha256"),)


class DataJob(Base):
    """Operational record of every ingestion / pipeline job (§8 data_jobs, §37)."""

    __tablename__ = "data_jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), index=True
    )  # running|succeeded|failed|quarantined
    started_at: Mapped[datetime] = _ts()
    completed_at: Mapped[datetime | None] = _ts(nullable=True)
    row_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    errors: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=1)
    __table_args__ = (
        CheckConstraint("status in ('running','succeeded','failed','quarantined')", name="status"),
    )


class DataSnapshot(Base):
    """A reproducible export of canonical tables (data_snapshot_id, §8 DecisionState)."""

    __tablename__ = "data_snapshots"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # snap_<hash>
    created_at: Mapped[datetime] = _created()
    as_of: Mapped[datetime] = _ts()
    seasons: Mapped[list[Any]] = mapped_column(JSONB)
    tables: Mapped[dict[str, Any]] = mapped_column(JSONB)  # table -> {rows, sha256}
    storage_uri: Mapped[str] = mapped_column(Text)
    source_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    validated: Mapped[bool] = mapped_column(Boolean, default=False)


class DataQualityEventRow(Base):
    __tablename__ = "data_quality_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    detected_at: Mapped[datetime] = _ts()
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(128))
    issue_code: Mapped[str] = mapped_column(String(64), index=True)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("data_jobs.id"))
    resolved_at: Mapped[datetime | None] = _ts(nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("severity in ('info','warning','error','critical')", name="severity"),
    )


class RecordRevision(Base):
    """History of changed canonical rows (provisional → final, source corrections)."""

    __tablename__ = "record_revisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(64), index=True)
    natural_key: Mapped[str] = mapped_column(String(255), index=True)
    old_hash: Mapped[str] = mapped_column(String(64))
    new_hash: Mapped[str] = mapped_column(String(64))
    old_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    changed_at: Mapped[datetime] = _created()
    raw_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("raw_snapshots.id"))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = _created()
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    details_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


# =============================================================== canonical football data


class SeasonRow(Base):
    __tablename__ = "seasons"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_code: Mapped[str] = mapped_column(String(7), unique=True)
    ruleset_version: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[date | None] = mapped_column(Date)
    ended_at: Mapped[date | None] = mapped_column(Date)
    schema_version: Mapped[int] = mapped_column(SmallInteger, default=1)


class GameweekRow(Base):
    __tablename__ = "gameweeks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    gw: Mapped[int] = mapped_column(SmallInteger)
    deadline_at: Mapped[datetime] = _ts()
    started_at: Mapped[datetime | None] = _ts(nullable=True)
    ended_at: Mapped[datetime | None] = _ts(nullable=True)
    locked_at: Mapped[datetime | None] = _ts(nullable=True)
    finalized_at: Mapped[datetime | None] = _ts(nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    deadline_source: Mapped[str] = mapped_column(String(32), default="official")
    __table_args__ = (UniqueConstraint("season_id", "gw"),)


class TeamRow(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    source_id: Mapped[int] = mapped_column(SmallInteger)
    code: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(64))
    short_name: Mapped[str] = mapped_column(String(4))
    strength_home: Mapped[int | None] = mapped_column(Integer)
    strength_away: Mapped[int | None] = mapped_column(Integer)
    strength_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        UniqueConstraint("season_id", "source_id"),
        UniqueConstraint("season_id", "code"),
    )


class PlayerRow(Base):
    """Canonical player identity and *current* state within a season."""

    __tablename__ = "players"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    source_id: Mapped[int] = mapped_column(Integer)
    code: Mapped[int] = mapped_column(Integer, index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    web_name: Mapped[str] = mapped_column(String(128))
    position: Mapped[str] = mapped_column(String(3))
    price: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(String(1), default="a")
    source_updated_at: Mapped[datetime] = _ts()
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("season_id", "source_id"),
        UniqueConstraint("season_id", "code"),
        CheckConstraint("position in ('GK','DEF','MID','FWD')", name="position"),
        CheckConstraint("price between 0 and 300", name="price_range"),
    )


class FixtureRow(Base):
    __tablename__ = "fixtures"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    source_id: Mapped[int] = mapped_column(Integer)
    gw: Mapped[int | None] = mapped_column(SmallInteger, index=True)
    kickoff_at: Mapped[datetime | None] = _ts(nullable=True)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    status: Mapped[str] = mapped_column(String(16))
    finalized: Mapped[bool] = mapped_column(Boolean, default=False)
    home_score: Mapped[int | None] = mapped_column(SmallInteger)
    away_score: Mapped[int | None] = mapped_column(SmallInteger)
    home_difficulty: Mapped[int | None] = mapped_column(SmallInteger)
    away_difficulty: Mapped[int | None] = mapped_column(SmallInteger)
    schedule_available_at: Mapped[datetime] = _ts()
    result_available_at: Mapped[datetime | None] = _ts(nullable=True)
    source: Mapped[str] = mapped_column(String(64))
    raw_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("raw_snapshots.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("season_id", "source_id"),
        CheckConstraint("home_team_id <> away_team_id", name="distinct_teams"),
    )


class PlayerSnapshotRow(Base):
    """Point-in-time player state from live captures (§54 player_snapshots)."""

    __tablename__ = "player_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    player_code: Mapped[int] = mapped_column(Integer, index=True)
    team_code: Mapped[int] = mapped_column(Integer)
    captured_at: Mapped[datetime] = _ts()
    price: Mapped[int] = mapped_column(SmallInteger)
    form: Mapped[float | None] = mapped_column(Float)
    ownership: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(1))
    news: Mapped[str | None] = mapped_column(Text)
    news_added: Mapped[datetime | None] = _ts(nullable=True)
    chance_of_playing_next_round: Mapped[int | None] = mapped_column(SmallInteger)
    expected_minutes: Mapped[float | None] = mapped_column(Float)
    set_piece_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    official_price_signal_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[str] = mapped_column(String(80))
    raw_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("raw_snapshots.id"))
    available_at: Mapped[datetime] = _ts()
    __table_args__ = (UniqueConstraint("season_id", "player_code", "captured_at", "source"),)


class PlayerGwStatsRow(Base):
    """Official FPL scoring stats per player per fixture (§54 player_gw_stats)."""

    __tablename__ = "player_gw_stats"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    gw: Mapped[int] = mapped_column(SmallInteger, index=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    player_code: Mapped[int] = mapped_column(Integer, index=True)
    team_code: Mapped[int] = mapped_column(Integer)
    opponent_team_code: Mapped[int] = mapped_column(Integer)
    was_home: Mapped[bool] = mapped_column(Boolean)
    kickoff_at: Mapped[datetime] = _ts()
    minutes: Mapped[int] = mapped_column(SmallInteger)
    starts: Mapped[int | None] = mapped_column(SmallInteger)
    points: Mapped[int] = mapped_column(SmallInteger)
    goals: Mapped[int] = mapped_column(SmallInteger)
    assists: Mapped[int] = mapped_column(SmallInteger)
    clean_sheets: Mapped[int] = mapped_column(SmallInteger)
    goals_conceded: Mapped[int] = mapped_column(SmallInteger)
    own_goals: Mapped[int] = mapped_column(SmallInteger)
    penalties_saved: Mapped[int] = mapped_column(SmallInteger)
    penalties_missed: Mapped[int] = mapped_column(SmallInteger)
    yellow_cards: Mapped[int] = mapped_column(SmallInteger)
    red_cards: Mapped[int] = mapped_column(SmallInteger)
    saves: Mapped[int] = mapped_column(SmallInteger)
    bonus: Mapped[int] = mapped_column(SmallInteger)
    bps: Mapped[int] = mapped_column(SmallInteger)
    dc: Mapped[int | None] = mapped_column(SmallInteger)  # defensive_contribution actions
    cbi: Mapped[int | None] = mapped_column(SmallInteger)
    tackles: Mapped[int | None] = mapped_column(SmallInteger)
    recoveries: Mapped[int | None] = mapped_column(SmallInteger)
    price: Mapped[int] = mapped_column(SmallInteger)
    ownership_count: Mapped[int | None] = mapped_column(BigInteger)
    transfers_in: Mapped[int | None] = mapped_column(BigInteger)
    transfers_out: Mapped[int | None] = mapped_column(BigInteger)
    available_at: Mapped[datetime] = _ts()
    finalized: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(64))
    raw_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("raw_snapshots.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("season_id", "fixture_id", "player_code"),
        CheckConstraint("minutes between 0 and 130", name="minutes_range"),
        Index("ix_player_gw_stats_player_kickoff", "player_code", "kickoff_at"),
    )


class UnderlyingStatsRow(Base):
    """Source-attributed underlying metrics (§54 underlying_stats, §6.2)."""

    __tablename__ = "underlying_stats"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    gw: Mapped[int] = mapped_column(SmallInteger)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"))
    player_code: Mapped[int] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(64))
    source_priority: Mapped[int] = mapped_column(SmallInteger)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    xg: Mapped[float | None] = mapped_column(Float)
    xa: Mapped[float | None] = mapped_column(Float)
    xgc: Mapped[float | None] = mapped_column(Float)
    shots: Mapped[float | None] = mapped_column(Float)
    box_touches: Mapped[float | None] = mapped_column(Float)
    key_passes: Mapped[float | None] = mapped_column(Float)
    influence: Mapped[float | None] = mapped_column(Float)
    creativity: Mapped[float | None] = mapped_column(Float)
    threat: Mapped[float | None] = mapped_column(Float)
    set_piece_share: Mapped[float | None] = mapped_column(Float)
    available_at: Mapped[datetime] = _ts()
    raw_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("raw_snapshots.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("season_id", "fixture_id", "player_code", "source"),)


class TeamGwStatsRow(Base):
    __tablename__ = "team_gw_stats"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    gw: Mapped[int] = mapped_column(SmallInteger)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"))
    team_code: Mapped[int] = mapped_column(Integer, index=True)
    was_home: Mapped[bool] = mapped_column(Boolean)
    goals_for: Mapped[int] = mapped_column(SmallInteger)
    goals_against: Mapped[int] = mapped_column(SmallInteger)
    xg_for: Mapped[float | None] = mapped_column(Float)
    xg_against: Mapped[float | None] = mapped_column(Float)
    shots_proxy: Mapped[float | None] = mapped_column(Float)  # summed ICT threat
    available_at: Mapped[datetime] = _ts()
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("season_id", "fixture_id", "team_code"),)


class TeamStrengthRow(Base):
    """Point-in-time team strength estimates (§8 team_strength)."""

    __tablename__ = "team_strength"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    team_code: Mapped[int] = mapped_column(Integer)
    cutoff_at: Mapped[datetime] = _ts()
    model_version: Mapped[str] = mapped_column(String(64))
    attack_rating: Mapped[float] = mapped_column(Float)
    defence_rating: Mapped[float] = mapped_column(Float)
    attack_sd: Mapped[float] = mapped_column(Float)
    defence_sd: Mapped[float] = mapped_column(Float)
    home_advantage: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("season_id", "team_code", "cutoff_at", "model_version"),)


class NewsSignalRow(Base):
    """Structured availability signals (§67): source, referenced player, status, confidence."""

    __tablename__ = "news_signals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    player_code: Mapped[int] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(128))
    published_at: Mapped[datetime] = _ts()
    structured_status: Mapped[str] = mapped_column(String(32))
    chance_of_playing: Mapped[int | None] = mapped_column(SmallInteger)
    evidence_strength: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[datetime | None] = _ts(nullable=True)
    text: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime] = _ts()


# =============================================================== manager state


class ManagerStateRow(Base):
    """Manager state snapshot (§54 manager_state; §8 squad_snapshots)."""

    __tablename__ = "manager_state"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # mstate_<hash>
    manager_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    gw: Mapped[int] = mapped_column(SmallInteger)
    captured_at: Mapped[datetime] = _ts()
    bank: Mapped[int] = mapped_column(SmallInteger)
    free_transfers: Mapped[int] = mapped_column(SmallInteger)
    overall_points: Mapped[int | None] = mapped_column(Integer)
    overall_rank: Mapped[int | None] = mapped_column(BigInteger)
    team_value: Mapped[int | None] = mapped_column(SmallInteger)
    source: Mapped[str] = mapped_column(String(32))  # fpl_api|manual|simulated|backtest
    state_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_snapshot_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    __table_args__ = (CheckConstraint("free_transfers between 0 and 20", name="ft_range"),)


class ManagerSquadRow(Base):
    __tablename__ = "manager_squad"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    state_id: Mapped[str] = mapped_column(
        ForeignKey("manager_state.id", ondelete="CASCADE"), index=True
    )
    manager_id: Mapped[int | None] = mapped_column(BigInteger)
    gw: Mapped[int] = mapped_column(SmallInteger)
    player_code: Mapped[int] = mapped_column(Integer)
    slot: Mapped[int] = mapped_column(SmallInteger)
    purchase_price: Mapped[int] = mapped_column(SmallInteger)
    selling_price: Mapped[int] = mapped_column(SmallInteger)
    is_starting: Mapped[bool] = mapped_column(Boolean)
    is_captain: Mapped[bool] = mapped_column(Boolean, default=False)
    is_vice_captain: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("state_id", "player_code"),
        UniqueConstraint("state_id", "slot"),
        CheckConstraint("slot between 1 and 15", name="slot_range"),
    )


class ManagerTransferRow(Base):
    __tablename__ = "manager_transfers"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    manager_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    gw: Mapped[int] = mapped_column(SmallInteger)
    in_player_code: Mapped[int] = mapped_column(Integer)
    out_player_code: Mapped[int] = mapped_column(Integer)
    in_cost: Mapped[int] = mapped_column(SmallInteger)
    out_value: Mapped[int] = mapped_column(SmallInteger)
    cost: Mapped[int] = mapped_column(SmallInteger)  # points deducted (0 or hit)
    free_or_hit: Mapped[str] = mapped_column(String(8))
    made_at: Mapped[datetime | None] = _ts(nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    __table_args__ = (
        UniqueConstraint("manager_id", "season_id", "gw", "in_player_code", "out_player_code"),
    )


class ManagerChipRow(Base):
    __tablename__ = "manager_chips"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    manager_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    chip_id: Mapped[str] = mapped_column(String(32))
    chip_type: Mapped[str] = mapped_column(String(32))
    half: Mapped[int] = mapped_column(SmallInteger)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    used_gw: Mapped[int | None] = mapped_column(SmallInteger)
    used_at: Mapped[datetime | None] = _ts(nullable=True)
    __table_args__ = (UniqueConstraint("manager_id", "season_id", "chip_id"),)


class ManagerSettingsRow(Base):
    """User preferences (Settings screen, §73.1): horizon, objective profile, league context."""

    __tablename__ = "manager_settings"
    manager_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = _created()


# =============================================================== features & forecasts


class FeatureSnapshotRow(Base):
    """A materialised point-in-time feature set (§8 player_features)."""

    __tablename__ = "feature_snapshots"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # feat_<hash>
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    target_gw: Mapped[int] = mapped_column(SmallInteger)
    cutoff_at: Mapped[datetime] = _ts()
    feature_version: Mapped[str] = mapped_column(String(32))
    data_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("data_snapshots.id"))
    max_source_available_at: Mapped[datetime] = _ts()
    n_rows: Mapped[int] = mapped_column(Integer)
    storage_uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        CheckConstraint("max_source_available_at <= cutoff_at", name="no_future_sources"),
    )


class ModelRegistryRow(Base):
    __tablename__ = "model_registry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    artifact_uri: Mapped[str] = mapped_column(Text)
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    feature_version: Mapped[str] = mapped_column(String(32))
    ruleset_version: Mapped[str | None] = mapped_column(String(32))
    training_window: Mapped[dict[str, Any]] = mapped_column(JSONB)
    data_snapshot_id: Mapped[str | None] = mapped_column(String(80))
    params_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), index=True)
    owner: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str | None] = mapped_column(String(64))
    parent_version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created()
    promoted_at: Mapped[datetime | None] = _ts(nullable=True)
    __table_args__ = (
        UniqueConstraint("model_name", "version"),
        CheckConstraint(
            "status in ('candidate','approved','production','rejected','retired')", name="status"
        ),
        Index(
            "uq_model_registry_one_production",
            "model_name",
            unique=True,
            postgresql_where="status = 'production'",
        ),
    )


class PredictionRunRow(Base):
    """One forecast generation run (shared provenance for many prediction rows)."""

    __tablename__ = "prediction_runs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # pred_<hash>
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    base_gw: Mapped[int] = mapped_column(SmallInteger)
    horizon: Mapped[int] = mapped_column(SmallInteger)
    generated_at: Mapped[datetime] = _created()
    cutoff_at: Mapped[datetime] = _ts()
    feature_snapshot_id: Mapped[str] = mapped_column(ForeignKey("feature_snapshots.id"))
    model_versions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ruleset_version: Mapped[str] = mapped_column(String(32))
    simulation_seed: Mapped[int] = mapped_column(BigInteger)
    n_simulations: Mapped[int] = mapped_column(Integer)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    samples_uri: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="succeeded")
    degraded_reasons: Mapped[list[Any]] = mapped_column(JSONB, default=list)


class PredictionRow(Base):
    """Per player × gameweek forecast distribution (§54 predictions, §59.2)."""

    __tablename__ = "predictions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("prediction_runs.id", ondelete="CASCADE"), index=True
    )
    player_code: Mapped[int] = mapped_column(Integer, index=True)
    gw: Mapped[int] = mapped_column(SmallInteger)
    horizon: Mapped[int] = mapped_column(SmallInteger)
    target: Mapped[str] = mapped_column(String(32), default="points")
    n_fixtures: Mapped[int] = mapped_column(SmallInteger)
    mean: Mapped[float] = mapped_column(Float)
    p10: Mapped[float] = mapped_column(Float)
    p25: Mapped[float] = mapped_column(Float)
    p50: Mapped[float] = mapped_column(Float)
    p75: Mapped[float] = mapped_column(Float)
    p90: Mapped[float] = mapped_column(Float)
    std: Mapped[float] = mapped_column(Float)
    prob_2_plus: Mapped[float] = mapped_column(Float)
    prob_6_plus: Mapped[float] = mapped_column(Float)
    prob_10_plus: Mapped[float] = mapped_column(Float)
    prob_15_plus: Mapped[float] = mapped_column(Float)
    start_probability: Mapped[float] = mapped_column(Float)
    expected_minutes: Mapped[float] = mapped_column(Float)
    minutes_p10: Mapped[float] = mapped_column(Float)
    minutes_p50: Mapped[float] = mapped_column(Float)
    minutes_p90: Mapped[float] = mapped_column(Float)
    components_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    calibration_bucket: Mapped[str | None] = mapped_column(String(32))
    __table_args__ = (UniqueConstraint("run_id", "player_code", "gw"),)


# =============================================================== optimisation & decisions


class OptimizationRunRow(Base):
    __tablename__ = "optimization_runs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # opt_<hash>
    manager_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    gw: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = _created()
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    input_state_id: Mapped[str | None] = mapped_column(String(80))
    prediction_run_id: Mapped[str | None] = mapped_column(ForeignKey("prediction_runs.id"))
    ruleset_version: Mapped[str] = mapped_column(String(32))
    ruleset_hash: Mapped[str] = mapped_column(String(64))
    solver: Mapped[str] = mapped_column(String(32))
    solver_version: Mapped[str | None] = mapped_column(String(32))
    objective_version: Mapped[str] = mapped_column(String(64))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    horizon: Mapped[int] = mapped_column(SmallInteger)
    seed: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32))
    objective_value: Mapped[float | None] = mapped_column(Float)
    mip_gap: Mapped[float | None] = mapped_column(Float)
    runtime_ms: Mapped[int] = mapped_column(Integer)
    solution_hash: Mapped[str | None] = mapped_column(String(64))
    solution_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    validation_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class TransferCandidateRow(Base):
    """Explainable candidate moves considered by an optimisation run (§8 transfer_candidates)."""

    __tablename__ = "transfer_candidates"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("optimization_runs.id", ondelete="CASCADE"), index=True
    )
    snapshot_id: Mapped[str | None] = mapped_column(String(80))
    out_player_code: Mapped[int | None] = mapped_column(Integer)
    in_player_code: Mapped[int | None] = mapped_column(Integer)
    delta: Mapped[float] = mapped_column(Float)
    risk: Mapped[float | None] = mapped_column(Float)
    cost: Mapped[int] = mapped_column(SmallInteger, default=0)
    rank: Mapped[int] = mapped_column(SmallInteger)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class RecommendationRow(Base):
    __tablename__ = "recommendations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # rec_<hash>
    manager_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    manager_state_id: Mapped[str | None] = mapped_column(ForeignKey("manager_state.id"))
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    gw: Mapped[int] = mapped_column(SmallInteger, index=True)
    created_at: Mapped[datetime] = _created()
    action_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(16), default="active"
    )  # active|superseded|invalidated
    expected_value: Mapped[float] = mapped_column(Float)
    expected_gain_vs_hold: Mapped[float] = mapped_column(Float)
    downside: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    confidence_label: Mapped[str] = mapped_column(String(16))
    stability: Mapped[str | None] = mapped_column(String(16))
    explanation_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    optimization_run_id: Mapped[str | None] = mapped_column(ForeignKey("optimization_runs.id"))
    prediction_run_id: Mapped[str | None] = mapped_column(ForeignKey("prediction_runs.id"))
    data_snapshot_id: Mapped[str | None] = mapped_column(String(80))
    ruleset_version: Mapped[str] = mapped_column(String(32))
    model_versions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint("action_type in ('HOLD','TRANSFER','HIT','CHIP')", name="action_type"),
        CheckConstraint("confidence between 0 and 1", name="confidence_range"),
    )


class RecommendationActionRow(Base):
    __tablename__ = "recommendation_actions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("recommendations.id", ondelete="CASCADE"), index=True
    )
    gw: Mapped[int] = mapped_column(SmallInteger)
    sequence: Mapped[int] = mapped_column(SmallInteger)
    out_player_code: Mapped[int | None] = mapped_column(Integer)
    in_player_code: Mapped[int | None] = mapped_column(Integer)
    captain_code: Mapped[int | None] = mapped_column(Integer)
    vice_captain_code: Mapped[int | None] = mapped_column(Integer)
    lineup_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    chip_type: Mapped[str | None] = mapped_column(String(32))


class DecisionJournalRow(Base):
    """User feedback and post-GW review per recommendation (§30 Decision Journal, §38)."""

    __tablename__ = "decision_journal"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[str] = mapped_column(ForeignKey("recommendations.id"), index=True)
    followed: Mapped[str | None] = mapped_column(String(16))  # followed|ignored|partial
    user_note: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = _created()
    realized_points: Mapped[float | None] = mapped_column(Float)
    realized_hold_points: Mapped[float | None] = mapped_column(Float)
    forecast_error: Mapped[float | None] = mapped_column(Float)
    review_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


# =============================================================== evaluation


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # bt_<hash>
    season: Mapped[str] = mapped_column(String(7))
    from_gw: Mapped[int] = mapped_column(SmallInteger)
    to_gw: Mapped[int] = mapped_column(SmallInteger)
    cutoff_policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    benchmark: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[dict[str, Any]] = mapped_column(JSONB)
    code_version: Mapped[str | None] = mapped_column(String(64))
    data_snapshot_id: Mapped[str | None] = mapped_column(String(80))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = _created()
    completed_at: Mapped[datetime | None] = _ts(nullable=True)
    results_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    artifacts_uri: Mapped[str | None] = mapped_column(Text)


class BacktestDecisionRow(Base):
    """Per-GW decision trace inside a backtest (§8 backtests: cutoff, prediction, actual)."""

    __tablename__ = "backtest_decisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    backtest_id: Mapped[str] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True
    )
    strategy: Mapped[str] = mapped_column(String(64))
    gw: Mapped[int] = mapped_column(SmallInteger)
    cutoff_at: Mapped[datetime] = _ts()
    max_accessed_available_at: Mapped[datetime] = _ts()
    action_type: Mapped[str] = mapped_column(String(16))
    predicted_points: Mapped[float] = mapped_column(Float)
    actual_points: Mapped[float] = mapped_column(Float)
    hit_cost: Mapped[int] = mapped_column(SmallInteger)
    decision_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        CheckConstraint("max_accessed_available_at <= cutoff_at", name="no_lookahead"),
    )


# =============================================================== product operations


class JobRow(Base):
    """Durable record of async jobs (the queue itself lives in Redis, ADR-0009)."""

    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)  # queued|running|succeeded|failed
    request_hash: Mapped[str] = mapped_column(String(64), index=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result_ref: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = _ts(nullable=True)
    finished_at: Mapped[datetime | None] = _ts(nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)


class NotificationRow(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    manager_key: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    materiality: Mapped[float] = mapped_column(Float)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = _created()
    delivered_at: Mapped[datetime | None] = _ts(nullable=True)
    read_at: Mapped[datetime | None] = _ts(nullable=True)
    __table_args__ = (UniqueConstraint("manager_key", "dedupe_key"),)


class ApiKeyRow(Base):
    """Hashed API keys for privileged endpoints (§35, §75). Plaintext is never stored."""

    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    label: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime | None] = _ts(nullable=True)
    revoked_at: Mapped[datetime | None] = _ts(nullable=True)


class ArtifactBlob(Base):
    """Small binary artifacts stored in-database when object storage is not configured."""

    __tablename__ = "artifact_blobs"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = _created()


# =============================================================== accounts (invite-only beta)


class UserRow(Base):
    """A beta user. Passwords are stored as scrypt hashes (``fpl_api.accounts``); e-mail is the
    login name and the only personal datum kept about the person."""

    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # usr_<hex>
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = _created()
    last_login_at: Mapped[datetime | None] = _ts(nullable=True)
    disabled_at: Mapped[datetime | None] = _ts(nullable=True)
    analytics_opt_out: Mapped[bool] = mapped_column(Boolean, default=False)


class InviteRow(Base):
    """An invitation code (stored hashed) that admits one user."""

    __tablename__ = "invites"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime | None] = _ts(nullable=True)
    redeemed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    redeemed_at: Mapped[datetime | None] = _ts(nullable=True)


class SessionRow(Base):
    """A login session; the browser holds the random token, the database only its hash."""

    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = _ts()
    last_seen_at: Mapped[datetime | None] = _ts(nullable=True)
    revoked_at: Mapped[datetime | None] = _ts(nullable=True)


class UserManagerRow(Base):
    """Ownership: every manager key belongs to exactly one user (§75 isolation)."""

    __tablename__ = "user_managers"
    manager_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    label: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class SquadAnalysisRow(Base):
    """A precomputed squad analysis (Copilot Home read model), versioned by everything it depends
    on: the squad state, the data snapshot, the forecast and the analysis code."""

    __tablename__ = "squad_analyses"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # sqa_<hash>
    manager_key: Mapped[str] = mapped_column(String(64), index=True)
    state_id: Mapped[str] = mapped_column(ForeignKey("manager_state.id"))
    data_snapshot_id: Mapped[str] = mapped_column(String(80))
    forecast_key: Mapped[str] = mapped_column(String(80))
    version: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    computed_at: Mapped[datetime] = _created()
    __table_args__ = (
        UniqueConstraint("manager_key", "state_id", "data_snapshot_id", "forecast_key", "version"),
    )


class ProductEventRow(Base):
    """First-party product analytics: named events with a small allow-listed property set, never
    credentials or manager keys (``fpl_api.analytics``). Pruned by the retention job."""

    __tablename__ = "product_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    props_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (Index("ix_product_events_created_at", "created_at"),)
