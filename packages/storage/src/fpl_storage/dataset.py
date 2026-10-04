"""Canonical dataset: one in-memory representation of canonical data for analytics.

The same frames come from three places and must be identical:
1. ingestion output (``fpl_ingestion.canonical``),
2. PostgreSQL (``load_from_db``),
3. a versioned Parquet snapshot (``export_snapshot`` / ``load_snapshot``).

A snapshot's id is the hash of its per-table content hashes, so "same data" ⇔ "same id"
(§49.1 reproducibility, §8 ``data_snapshot_id``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, text

# Natural-key ordering and expected columns of every canonical table.
SCHEMA: dict[str, dict[str, Any]] = {
    "seasons": {
        "key": ["season"],
        "columns": ["season", "ruleset_version", "started_at", "ended_at"],
    },
    "gameweeks": {
        "key": ["season", "gw"],
        "columns": [
            "season",
            "gw",
            "deadline_at",
            "first_kickoff_at",
            "last_kickoff_at",
            "finalized_at",
            "status",
            "deadline_source",
        ],
    },
    "teams": {
        "key": ["season", "team_code"],
        "columns": ["season", "team_code", "source_id", "name", "short_name", "strength_json"],
    },
    "players": {
        "key": ["season", "player_code"],
        "columns": [
            "season",
            "player_code",
            "source_id",
            "team_code",
            "web_name",
            "first_name",
            "last_name",
            "position",
            "price",
            "status",
            "source_updated_at",
        ],
    },
    "fixtures": {
        "key": ["season", "fixture_id"],
        "columns": [
            "season",
            "fixture_id",
            "gw",
            "kickoff_at",
            "home_team_code",
            "away_team_code",
            "status",
            "finalized",
            "home_score",
            "away_score",
            "home_difficulty",
            "away_difficulty",
            "schedule_available_at",
            "result_available_at",
        ],
    },
    "player_match": {
        "key": ["season", "fixture_id", "player_code"],
        "columns": [
            "season",
            "gw",
            "fixture_id",
            "player_code",
            "team_code",
            "opponent_team_code",
            "was_home",
            "kickoff_at",
            "minutes",
            "starts",
            "points",
            "goals",
            "assists",
            "clean_sheets",
            "goals_conceded",
            "own_goals",
            "penalties_saved",
            "penalties_missed",
            "yellow_cards",
            "red_cards",
            "saves",
            "bonus",
            "bps",
            "dc",
            "cbi",
            "tackles",
            "recoveries",
            "price",
            "ownership_count",
            "transfers_in",
            "transfers_out",
            "xg",
            "xa",
            "xgc",
            "influence",
            "creativity",
            "threat",
            "available_at",
            "finalized",
        ],
    },
    "team_match": {
        "key": ["season", "fixture_id", "team_code"],
        "columns": [
            "season",
            "gw",
            "fixture_id",
            "team_code",
            "opponent_team_code",
            "was_home",
            "kickoff_at",
            "goals_for",
            "goals_against",
            "xg_for",
            "xg_against",
            "threat_for",
            "threat_against",
            "available_at",
        ],
    },
    "player_snapshots": {
        "key": ["season", "player_code", "captured_at", "source"],
        "columns": [
            "season",
            "player_code",
            "team_code",
            "captured_at",
            "price",
            "form",
            "ownership",
            "status",
            "news",
            "news_added",
            "chance_of_playing_next_round",
            "set_piece_json",
            "official_price_signal_json",
            "source",
            "source_version",
            "available_at",
        ],
    },
    "news_signals": {
        "key": ["id"],
        "columns": [
            "id",
            "season",
            "player_code",
            "source",
            "publisher",
            "published_at",
            "structured_status",
            "chance_of_playing",
            "evidence_strength",
            "text",
            "available_at",
        ],
    },
}
TABLES = tuple(SCHEMA)


# Declared logical types (everything not listed is a string column).
_TS = {
    "deadline_at",
    "first_kickoff_at",
    "last_kickoff_at",
    "finalized_at",
    "kickoff_at",
    "schedule_available_at",
    "result_available_at",
    "available_at",
    "source_updated_at",
    "captured_at",
    "news_added",
    "published_at",
}
_INT = {
    "gw",
    "team_code",
    "source_id",
    "player_code",
    "price",
    "fixture_id",
    "home_team_code",
    "away_team_code",
    "home_score",
    "away_score",
    "home_difficulty",
    "away_difficulty",
    "opponent_team_code",
    "minutes",
    "starts",
    "points",
    "goals",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "saves",
    "bonus",
    "bps",
    "dc",
    "cbi",
    "tackles",
    "recoveries",
    "ownership_count",
    "transfers_in",
    "transfers_out",
    "goals_for",
    "goals_against",
    "chance_of_playing_next_round",
    "chance_of_playing",
}
_FLOAT = {
    "xg",
    "xa",
    "xgc",
    "influence",
    "creativity",
    "threat",
    "xg_for",
    "xg_against",
    "threat_for",
    "threat_against",
    "form",
    "ownership",
    "evidence_strength",
}
_BOOL = {"finalized", "was_home"}
_DATE = {"started_at", "ended_at"}
_JSON = {"strength_json", "set_piece_json", "official_price_signal_json"}


def _canon_json(v: Any) -> Any:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    obj = json.loads(v) if isinstance(v, str) else v
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def normalize(table: str, df: pd.DataFrame) -> pd.DataFrame:
    """Column order, row order and dtypes that make equal data compare (and hash) equal."""
    spec = SCHEMA[table]
    cols = spec["columns"]
    if df is None or df.empty:
        df = pd.DataFrame({c: pd.Series(dtype="object") for c in cols})
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"{table}: missing canonical columns {missing}")
    out = df[cols].copy()
    for c in cols:
        s = out[c]
        if c in _TS:
            out[c] = pd.to_datetime(s, utc=True).dt.as_unit("us")
        elif c in _DATE:
            out[c] = pd.to_datetime(s).dt.strftime("%Y-%m-%d").astype("string")
        elif c in _INT:
            out[c] = pd.to_numeric(s).round().astype("Int64")
        elif c in _FLOAT:
            out[c] = pd.to_numeric(s).astype("Float64")
        elif c in _BOOL:
            out[c] = s.astype(bool)
        elif c in _JSON:
            out[c] = s.map(_canon_json).astype("string")
        else:
            out[c] = s.astype("string")
    return out.sort_values(spec["key"]).reset_index(drop=True)


def table_hash(df: pd.DataFrame) -> str:
    payload = df.to_csv(
        index=False, date_format="%Y-%m-%dT%H:%M:%S.%fZ", na_rep="<NA>", float_format="%.10g"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CanonicalDataset:
    """Normalised canonical tables. Treated as immutable after construction (derive a new
    dataset instead of editing frames), so the content hashes are computed once, lazily."""

    frames: dict[str, pd.DataFrame]
    meta: dict[str, Any] = field(default_factory=dict)
    _hashes: dict[str, str] | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.frames = {t: normalize(t, self.frames.get(t, pd.DataFrame())) for t in TABLES}

    def __getitem__(self, table: str) -> pd.DataFrame:
        return self.frames[table]

    @property
    def table_hashes(self) -> dict[str, str]:
        # hashing ~150k rows costs seconds; the id is read on every API response and cache key
        if self._hashes is None:
            self._hashes = {t: table_hash(df) for t, df in self.frames.items()}
        return self._hashes

    @property
    def snapshot_id(self) -> str:
        digest = hashlib.sha256(json.dumps(self.table_hashes, sort_keys=True).encode()).hexdigest()
        return f"snap_{digest[:20]}"

    def seasons(self) -> list[str]:
        return sorted(self.frames["seasons"]["season"].tolist())

    def subset(self, seasons: list[str]) -> CanonicalDataset:
        return CanonicalDataset(
            {
                t: df[df["season"].isin(seasons)] if "season" in df else df
                for t, df in self.frames.items()
            },
            dict(self.meta),
        )

    @classmethod
    def concat(cls, parts: list[CanonicalDataset]) -> CanonicalDataset:
        frames = {
            t: pd.concat([p.frames[t] for p in parts if not p.frames[t].empty], ignore_index=True)
            if any(not p.frames[t].empty for p in parts)
            else pd.DataFrame()
            for t in TABLES
        }
        return cls(frames)


# ------------------------------------------------------------------ Parquet snapshots


def export_snapshot(
    ds: CanonicalDataset, root: Path, extra_meta: dict[str, Any] | None = None
) -> Path:
    """Write ``ds`` to ``<root>/<snapshot_id>/`` (idempotent; content-addressed)."""
    sid = ds.snapshot_id
    out = Path(root) / sid
    manifest_path = out / "manifest.json"
    if manifest_path.exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    hashes = ds.table_hashes
    for t, df in ds.frames.items():
        df.to_parquet(out / f"{t}.parquet", index=False)
    manifest = {
        "snapshot_id": sid,
        "created_at": datetime.now(UTC).isoformat(),
        "tables": {t: {"rows": len(ds.frames[t]), "sha256": hashes[t]} for t in TABLES},
        "seasons": ds.seasons(),
        **(extra_meta or {}),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return out


def load_snapshot(path: Path, verify: bool = True) -> CanonicalDataset:
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    frames = {t: pd.read_parquet(path / f"{t}.parquet") for t in TABLES}
    ds = CanonicalDataset(frames, meta=manifest)
    if verify and ds.snapshot_id != manifest["snapshot_id"]:
        raise ValueError(f"snapshot {path} failed verification: content hash mismatch")
    return ds


# ------------------------------------------------------------------ PostgreSQL reader

_SQL = {
    "seasons": """select season_code as season, ruleset_version, started_at, ended_at
                  from seasons""",
    "gameweeks": """select s.season_code as season, g.gw, g.deadline_at,
                    g.started_at as first_kickoff_at, g.ended_at as last_kickoff_at,
                    g.finalized_at, g.status, g.deadline_source
                    from gameweeks g join seasons s on s.id = g.season_id""",
    "teams": """select s.season_code as season, t.code as team_code, t.source_id, t.name,
                t.short_name, t.strength_json from teams t join seasons s on s.id = t.season_id""",
    "players": """select s.season_code as season, p.code as player_code, p.source_id,
                  t.code as team_code, p.web_name, p.first_name, p.last_name, p.position,
                  p.price, p.status, p.source_updated_at
                  from players p join seasons s on s.id = p.season_id
                  join teams t on t.id = p.team_id""",
    "fixtures": """select s.season_code as season, f.source_id as fixture_id, f.gw, f.kickoff_at,
                   th.code as home_team_code, ta.code as away_team_code, f.status, f.finalized,
                   f.home_score, f.away_score, f.home_difficulty, f.away_difficulty,
                   f.schedule_available_at, f.result_available_at
                   from fixtures f join seasons s on s.id = f.season_id
                   join teams th on th.id = f.home_team_id
                   join teams ta on ta.id = f.away_team_id""",
    "player_match": """select s.season_code as season, p.gw, f.source_id as fixture_id,
                       p.player_code, p.team_code, p.opponent_team_code, p.was_home, p.kickoff_at,
                       p.minutes, p.starts, p.points, p.goals, p.assists, p.clean_sheets,
                       p.goals_conceded, p.own_goals, p.penalties_saved, p.penalties_missed,
                       p.yellow_cards, p.red_cards, p.saves, p.bonus, p.bps, p.dc, p.cbi,
                       p.tackles, p.recoveries, p.price, p.ownership_count, p.transfers_in,
                       p.transfers_out, u.xg, u.xa, u.xgc, u.influence, u.creativity, u.threat,
                       p.available_at, p.finalized
                       from player_gw_stats p join seasons s on s.id = p.season_id
                       join fixtures f on f.id = p.fixture_id
                       left join underlying_stats u on u.fixture_id = p.fixture_id
                         and u.player_code = p.player_code and u.source = 'fpl'""",
    "team_match": """select s.season_code as season, t.gw, f.source_id as fixture_id, t.team_code,
                     case when t.was_home then ta.code else th.code end as opponent_team_code,
                     t.was_home, f.kickoff_at, t.goals_for, t.goals_against, t.xg_for,
                     t.xg_against, t.shots_proxy as threat_for, o.shots_proxy as threat_against,
                     t.available_at
                     from team_gw_stats t join seasons s on s.id = t.season_id
                     join fixtures f on f.id = t.fixture_id
                     join teams th on th.id = f.home_team_id join teams ta on ta.id = f.away_team_id
                     left join team_gw_stats o on o.fixture_id = t.fixture_id
                       and o.team_code <> t.team_code""",
    "player_snapshots": """select s.season_code as season, ps.player_code, ps.team_code,
                           ps.captured_at, ps.price, ps.form, ps.ownership, ps.status, ps.news,
                           ps.news_added, ps.chance_of_playing_next_round, ps.set_piece_json,
                           ps.official_price_signal_json, ps.source, ps.source_version,
                           ps.available_at
                           from player_snapshots ps join seasons s on s.id = ps.season_id""",
    "news_signals": """select n.id, s.season_code as season, n.player_code, n.source, n.publisher,
                       n.published_at, n.structured_status, n.chance_of_playing,
                       n.evidence_strength, n.text, n.available_at
                       from news_signals n join seasons s on s.id = n.season_id""",
}


def load_from_db(engine: Engine, seasons: list[str] | None = None) -> CanonicalDataset:
    frames: dict[str, pd.DataFrame] = {}
    with engine.connect() as conn:
        for t, sql in _SQL.items():
            df = pd.read_sql(text(sql), conn)
            if seasons is not None and "season" in df:
                df = df[df["season"].isin(seasons)]
            frames[t] = df
    return CanonicalDataset(frames)
