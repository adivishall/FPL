"""Data contracts for source files (§55.1 'Validated layer').

A contract declares, per column: type, nullability, domain range / allowed values, whether the
column is required, and its leakage risk. ``validate`` coerces types and returns issues; it never
silently drops rows (rows failing hard constraints are reported with their keys).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from fpl_domain.enums import Severity

DType = Literal["int", "float", "bool", "str", "datetime"]
Leakage = Literal["none", "low", "high"]


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    entity_type: str
    message: str
    entity_id: str | None = None
    count: int = 1
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    dtype: DType
    nullable: bool = False
    required: bool = True
    min: float | None = None
    max: float | None = None
    allowed: frozenset[Any] | None = None
    leakage: Leakage = "none"
    description: str = ""


@dataclass(frozen=True)
class DataContract:
    name: str
    version: str
    entity_type: str
    columns: tuple[ColumnSpec, ...]
    unique_key: tuple[str, ...] = ()

    def spec(self, name: str) -> ColumnSpec:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    def validate(self, df: pd.DataFrame) -> tuple[pd.DataFrame, list[Issue]]:
        issues: list[Issue] = []
        out = df.copy()
        for col in self.columns:
            if col.name not in out.columns:
                if col.required:
                    issues.append(
                        Issue(
                            "schema_missing_column",
                            Severity.CRITICAL,
                            self.entity_type,
                            f"{self.name}: missing {col.name}",
                        )
                    )
                else:
                    out[col.name] = pd.NA
                continue
            try:
                out[col.name] = _coerce(out[col.name], col.dtype)
            except (ValueError, TypeError) as exc:
                issues.append(
                    Issue(
                        "schema_type",
                        Severity.CRITICAL,
                        self.entity_type,
                        f"{self.name}.{col.name}: cannot coerce to {col.dtype}",
                        details={"error": str(exc)[:300]},
                    )
                )
                continue
            s = out[col.name]
            nulls = int(s.isna().sum())
            if nulls and not col.nullable and col.required:
                issues.append(
                    Issue(
                        "null_violation",
                        Severity.ERROR,
                        self.entity_type,
                        f"{self.name}.{col.name}: {nulls} nulls",
                        count=nulls,
                    )
                )
            if col.min is not None or col.max is not None:
                num = pd.to_numeric(s, errors="coerce")
                bad = pd.Series(False, index=s.index)
                if col.min is not None:
                    bad |= num < col.min
                if col.max is not None:
                    bad |= num > col.max
                n_bad = int(bad.sum())
                if n_bad:
                    issues.append(
                        Issue(
                            "range_violation",
                            Severity.ERROR,
                            self.entity_type,
                            f"{self.name}.{col.name}: {n_bad} values outside "
                            f"[{col.min}, {col.max}]",
                            count=n_bad,
                            details={"examples": _examples(out[bad], self.unique_key)},
                        )
                    )
            if col.allowed is not None:
                bad = ~s.isna() & ~s.isin(list(col.allowed))
                n_bad = int(bad.sum())
                if n_bad:
                    issues.append(
                        Issue(
                            "allowed_values",
                            Severity.ERROR,
                            self.entity_type,
                            f"{self.name}.{col.name}: {n_bad} unexpected values",
                            count=n_bad,
                            details={"values": sorted(map(str, s[bad].unique()))[:10]},
                        )
                    )
        if self.unique_key and all(k in out.columns for k in self.unique_key):
            dup = out.duplicated(list(self.unique_key), keep=False)
            if dup.any():
                groups = out[dup].groupby(list(self.unique_key), dropna=False)
                identical = sum(1 for _, g in groups if len(g.drop_duplicates()) == 1)
                conflicting = groups.ngroups - identical
                if identical:
                    issues.append(
                        Issue(
                            "duplicate_exact",
                            Severity.WARNING,
                            self.entity_type,
                            f"{self.name}: {identical} exactly duplicated keys (deduplicated)",
                            count=identical,
                        )
                    )
                if conflicting:
                    issues.append(
                        Issue(
                            "duplicate_conflicting",
                            Severity.CRITICAL,
                            self.entity_type,
                            f"{self.name}: {conflicting} keys with conflicting rows",
                            count=conflicting,
                        )
                    )
                out = out.drop_duplicates()
        return out, issues


def _examples(df: pd.DataFrame, key: tuple[str, ...], n: int = 5) -> list[dict[str, Any]]:
    cols = [k for k in key if k in df.columns]
    return [{k: _py(v) for k, v in r.items()} for r in df[cols].head(n).to_dict("records")]


def _py(v: Any) -> Any:
    if isinstance(v, np.generic):
        return v.item()
    return v


_BOOL = {"true": True, "false": False, "1": True, "0": False}


def _coerce(s: pd.Series, dtype: DType) -> pd.Series:
    if dtype == "int":
        num = pd.to_numeric(s, errors="raise")
        if (num.dropna() % 1 != 0).any():
            raise ValueError("non-integral values")
        return num.astype("Int64")
    if dtype == "float":
        return pd.to_numeric(s.replace({"None": None}), errors="raise").astype("Float64")
    if dtype == "bool":
        if s.dtype == bool:
            return s.astype("boolean")
        mapped = s.astype("string").str.strip().str.lower().map(_BOOL)
        if mapped.isna().sum() > s.isna().sum():
            raise ValueError("unparseable booleans")
        return mapped.astype("boolean")
    if dtype == "datetime":
        cleaned = s.replace({"None": None, "": None})
        return pd.to_datetime(cleaned, utc=True, errors="raise", format="ISO8601")
    cleaned = s.astype("string")
    return cleaned.mask(cleaned.isin(["None", "nan"]))


def _c(name: str, dtype: DType, **kw: Any) -> ColumnSpec:
    return ColumnSpec(name=name, dtype=dtype, **kw)


POSITIONS_SRC = frozenset({"GK", "DEF", "MID", "FWD", "AM"})

MERGED_GW = DataContract(
    name="vaastav.merged_gw",
    version="1.1.0",
    entity_type="player_gw_stats",
    unique_key=("fixture", "element"),
    columns=(
        _c("name", "str", description="Player display name"),
        _c("position", "str", allowed=POSITIONS_SRC),
        _c("team", "str", description="Team name (validated against fixture-derived team)"),
        _c("element", "int", min=1, description="FPL element id (per season)"),
        _c("fixture", "int", min=1),
        _c("GW", "int", min=1, max=60),
        _c("kickoff_time", "datetime"),
        _c("was_home", "bool"),
        _c("opponent_team", "int", min=1, max=20),
        _c("minutes", "int", min=0, max=130),
        _c("starts", "int", min=0, max=1, nullable=True, required=False),
        _c("total_points", "int", min=-20, max=50),
        _c("goals_scored", "int", min=0, max=10),
        _c("assists", "int", min=0, max=10),
        _c("clean_sheets", "int", min=0, max=1),
        _c("goals_conceded", "int", min=0, max=20),
        _c("own_goals", "int", min=0, max=5),
        _c("penalties_saved", "int", min=0, max=5),
        _c("penalties_missed", "int", min=0, max=5),
        _c("yellow_cards", "int", min=0, max=2),
        _c("red_cards", "int", min=0, max=1),
        _c("saves", "int", min=0, max=30),
        _c("bonus", "int", min=0, max=3),
        _c("bps", "int", min=-50, max=200),
        _c(
            "value",
            "int",
            min=0,
            max=200,
            description="Price at the fixture (tenths); playing-position range checked later",
        ),
        _c("selected", "int", min=0, description="Managers owning the player at that GW"),
        _c("transfers_in", "int", min=0),
        _c("transfers_out", "int", min=0),
        _c("expected_goals", "float", min=0, max=10, nullable=True, required=False),
        _c("expected_assists", "float", min=0, max=10, nullable=True, required=False),
        _c("expected_goals_conceded", "float", min=0, max=15, nullable=True, required=False),
        _c("influence", "float", min=0, nullable=True),
        _c("creativity", "float", min=0, nullable=True),
        _c("threat", "float", min=0, nullable=True),
        _c("clearances_blocks_interceptions", "int", min=0, max=100, nullable=True, required=False),
        _c("tackles", "int", min=0, max=50, nullable=True, required=False),
        _c("recoveries", "int", min=0, max=100, nullable=True, required=False),
        _c("defensive_contribution", "int", min=0, max=150, nullable=True, required=False),
        _c(
            "xP",
            "float",
            nullable=True,
            required=False,
            leakage="high",
            description="Scraped after the GW (upstream caveat) — never used (ADR-0001 #11)",
        ),
    ),
)

FIXTURES = DataContract(
    name="vaastav.fixtures",
    version="1.0.0",
    entity_type="fixture",
    unique_key=("id",),
    columns=(
        _c("id", "int", min=1),
        _c("event", "int", min=1, max=60, nullable=True),
        _c("kickoff_time", "datetime", nullable=True),
        _c("team_h", "int", min=1, max=20),
        _c("team_a", "int", min=1, max=20),
        _c("team_h_score", "int", min=0, max=20, nullable=True),
        _c("team_a_score", "int", min=0, max=20, nullable=True),
        _c("finished", "bool"),
        _c("finished_provisional", "bool", required=False, nullable=True),
        _c("team_h_difficulty", "int", min=1, max=5, nullable=True),
        _c("team_a_difficulty", "int", min=1, max=5, nullable=True),
    ),
)

TEAMS = DataContract(
    name="vaastav.teams",
    version="1.0.0",
    entity_type="team",
    unique_key=("id",),
    columns=(
        _c("id", "int", min=1, max=20),
        _c("code", "int", min=1),
        _c("name", "str"),
        _c("short_name", "str"),
        _c(
            "strength_overall_home",
            "int",
            nullable=True,
            leakage="high",
            description="End-of-season FPL strength index; not point-in-time safe historically",
        ),
        _c("strength_overall_away", "int", nullable=True, leakage="high"),
    ),
)

PLAYERS_RAW = DataContract(
    name="vaastav.players_raw",
    version="1.0.0",
    entity_type="player",
    unique_key=("id",),
    columns=(
        _c("id", "int", min=1),
        _c("code", "int", min=1),
        _c("element_type", "int", min=1, max=5),
        _c("team", "int", min=1, max=20),
        _c("team_code", "int", min=1),
        _c("web_name", "str"),
        _c("first_name", "str", nullable=True),
        _c("second_name", "str", nullable=True),
        _c(
            "now_cost",
            "int",
            min=0,
            max=200,
            description="Tenths of £m; playing-position range is checked after AM filtering",
        ),
        _c("status", "str", allowed=frozenset({"a", "d", "i", "s", "u", "n"})),
        _c("news", "str", nullable=True),
        _c("news_added", "datetime", nullable=True),
        _c("chance_of_playing_next_round", "int", min=0, max=100, nullable=True),
        _c("selected_by_percent", "float", min=0, max=100),
        _c(
            "cost_change_start",
            "int",
            nullable=True,
            required=False,
            description="Price change since season start (start price = now_cost − this)",
        ),
        _c("form", "float", nullable=True),
        _c("penalties_order", "int", nullable=True, required=False),
        _c("direct_freekicks_order", "int", nullable=True, required=False),
        _c("corners_and_indirect_freekicks_order", "int", nullable=True, required=False),
        _c("price_change_percent", "float", nullable=True, required=False),
        _c("price_change_projections", "str", nullable=True, required=False),
    ),
)

CONTRACTS = {
    "merged_gw": MERGED_GW,
    "fixtures": FIXTURES,
    "teams": TEAMS,
    "players_raw": PLAYERS_RAW,
}
