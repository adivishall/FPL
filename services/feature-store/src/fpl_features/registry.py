"""Feature registry (§56): every feature declares definition, source, window, timestamp policy,
missing-value policy, leakage risk and expected type/range.

The registry is the single source for ``docs/DATA_DICTIONARY.md`` §5 (generated) and for
runtime validation of feature frames (``validate_frame``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

Family = Literal[
    "availability",
    "minutes",
    "attacking",
    "defensive",
    "form",
    "economics",
    "role",
    "team",
    "fixture",
    "schedule",
    "live",
    "meta",
]

FEATURE_VERSION = "1.2.0"


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    family: Family
    definition: str
    source: str
    window: str
    timestamp_policy: str
    missing_policy: str
    leakage_risk: Literal["none", "low", "medium"]
    dtype: Literal["float", "int", "bool"]
    lo: float | None = None
    hi: float | None = None


_PM = "player_match (PIT: available_at ≤ cutoff)"
_HL = "EWM over the player's last ≤40 team fixtures, half-life"
_AT = "as of the latest row available at cutoff"


def _f(
    name: str,
    family: Family,
    definition: str,
    source: str = _PM,
    window: str = "",
    ts: str = _AT,
    missing: str = "NaN (model handles); new players flagged",
    leak: Literal["none", "low", "medium"] = "none",
    dtype: Literal["float", "int", "bool"] = "float",
    lo: float | None = None,
    hi: float | None = None,
) -> FeatureSpec:
    return FeatureSpec(name, family, definition, source, window, ts, missing, leak, dtype, lo, hi)


REGISTRY: tuple[FeatureSpec, ...] = (
    # ---- availability / minutes
    _f(
        "n_prior_matches",
        "meta",
        "Team fixtures observed for the player before cutoff (any season)",
        window="all history",
        missing="0",
        dtype="int",
        lo=0,
    ),
    _f(
        "mins_last1",
        "minutes",
        "Minutes in the most recent team fixture",
        window="1 match",
        lo=0,
        hi=130,
    ),
    _f(
        "mins_ewm_short",
        "minutes",
        "Recency-weighted mean minutes",
        window=f"{_HL} 3",
        lo=0,
        hi=130,
    ),
    _f(
        "mins_ewm_long",
        "minutes",
        "Recency-weighted mean minutes",
        window=f"{_HL} 10",
        lo=0,
        hi=130,
    ),
    _f(
        "start_rate_short",
        "minutes",
        "Recency-weighted share of team fixtures started",
        window=f"{_HL} 3",
        missing="NaN where starts unpopulated",
        lo=0,
        hi=1,
    ),
    _f(
        "start_rate_long",
        "minutes",
        "Recency-weighted share of team fixtures started",
        window=f"{_HL} 10",
        lo=0,
        hi=1,
    ),
    _f(
        "app_rate_short",
        "availability",
        "Recency-weighted share of team fixtures with minutes>0",
        window=f"{_HL} 3",
        lo=0,
        hi=1,
    ),
    _f(
        "full90_rate_long",
        "minutes",
        "Recency-weighted share of fixtures with ≥89 minutes",
        window=f"{_HL} 10",
        lo=0,
        hi=1,
    ),
    _f(
        "sub_app_rate_long",
        "minutes",
        "Share of team fixtures entered as a substitute",
        window=f"{_HL} 10",
        lo=0,
        hi=1,
    ),
    _f(
        "mins_if_start_long",
        "minutes",
        "Recency-weighted mean minutes when starting",
        window=f"{_HL} 10",
        lo=0,
        hi=130,
    ),
    _f(
        "zero_min_streak",
        "availability",
        "Consecutive most recent team fixtures with 0 minutes (injury/drop proxy)",
        window="up to 40 matches",
        missing="0",
        dtype="int",
        lo=0,
        hi=40,
    ),
    _f(
        "days_since_last_app",
        "availability",
        "Days from last appearance (minutes>0) to cutoff",
        window="all history",
        missing="NaN if never appeared",
        lo=0,
    ),
    # ---- attacking process
    _f(
        "xg_p90",
        "attacking",
        "Recency-weighted xG per 90 minutes",
        window=f"{_HL} 10",
        missing="NaN if <90 weighted minutes",
        lo=0,
        hi=3,
    ),
    _f("xa_p90", "attacking", "Recency-weighted xA per 90 minutes", window=f"{_HL} 10", lo=0, hi=3),
    _f("goals_p90", "attacking", "Recency-weighted goals per 90", window=f"{_HL} 10", lo=0, hi=5),
    _f(
        "assists_p90",
        "attacking",
        "Recency-weighted assists per 90",
        window=f"{_HL} 10",
        lo=0,
        hi=5,
    ),
    _f(
        "threat_p90",
        "attacking",
        "Recency-weighted ICT threat per 90 (shot/box proxy)",
        window=f"{_HL} 10",
        lo=0,
    ),
    _f(
        "creativity_p90",
        "attacking",
        "Recency-weighted ICT creativity per 90 (chance creation)",
        window=f"{_HL} 10",
        lo=0,
    ),
    _f(
        "xg_share",
        "role",
        "Player xG / team xG over fixtures played, minutes-adjusted",
        window=f"{_HL} 10",
        lo=0,
        hi=1.5,
    ),
    _f(
        "xa_share",
        "role",
        "Player xA / team xG over fixtures played, minutes-adjusted",
        window=f"{_HL} 10",
        lo=0,
        hi=1.5,
    ),
    # ---- defensive process
    _f("saves_p90", "defensive", "Recency-weighted saves per 90 (GK)", window=f"{_HL} 10", lo=0),
    _f(
        "dc_actions_p90",
        "defensive",
        "Recency-weighted defensive actions per 90 (CBI+tackles(+recoveries for MID/FWD))",
        window=f"{_HL} 10",
        missing="NaN before 2025-26 (not recorded)",
        lo=0,
    ),
    _f(
        "dc_hit_rate",
        "defensive",
        "Share of appearances reaching the position DC threshold",
        window=f"{_HL} 10",
        missing="NaN before 2025-26",
        lo=0,
        hi=1,
    ),
    _f("bps_p90", "defensive", "Recency-weighted BPS per 90", window=f"{_HL} 10"),
    _f("bonus_p90", "form", "Recency-weighted bonus per 90", window=f"{_HL} 10", lo=0, hi=3),
    _f("yellow_p90", "defensive", "Recency-weighted yellow cards per 90", window=f"{_HL} 10", lo=0),
    # ---- form
    _f("pts_last1", "form", "FPL points in the most recent team fixture", window="1 match"),
    _f("pts_ewm_short", "form", "Recency-weighted points per team fixture", window=f"{_HL} 3"),
    _f("pts_ewm_long", "form", "Recency-weighted points per team fixture", window=f"{_HL} 10"),
    _f("pts_p90", "form", "Recency-weighted points per 90", window=f"{_HL} 10"),
    _f(
        "ppg_season",
        "form",
        "Season points per appearance up to cutoff",
        window="season",
        missing="NaN before first appearance",
    ),
    # ---- economics
    _f(
        "price",
        "economics",
        "Price (tenths) known at cutoff (ADR-0004 price policy)",
        source="price_observations / live snapshot",
        ts="last observation before cutoff (A2)",
        missing="never missing for pool players",
        dtype="int",
        lo=35,
        hi=200,
    ),
    _f(
        "price_change_season",
        "economics",
        "Price at cutoff minus first price this season",
        source="price_observations",
        dtype="int",
    ),
    _f(
        "ownership_pctile",
        "economics",
        "Percentile rank of ownership within the player pool at cutoff (scale-free: identical "
        "for historical counts and live percentages)",
        source="price_observations / live snapshot",
        missing="NaN at GW1 historically",
        lo=0,
        hi=1,
    ),
    _f(
        "net_transfers_last_gw",
        "economics",
        "(transfers in − out) / (in + out + 1000) for the last completed GW (bounded momentum)",
        source="gw_transfers",
        ts="available at that GW's deadline",
        lo=-1,
        hi=1,
    ),
    # ---- role / team
    _f(
        "pos_GK",
        "role",
        "Position indicator",
        source="players (season registry)",
        ts="season start",
        dtype="bool",
    ),
    _f("pos_DEF", "role", "Position indicator", source="players", ts="season start", dtype="bool"),
    _f("pos_MID", "role", "Position indicator", source="players", ts="season start", dtype="bool"),
    _f("pos_FWD", "role", "Position indicator", source="players", ts="season start", dtype="bool"),
    _f(
        "team_xg_for_ewm",
        "team",
        "Team xG for per match (recency-weighted)",
        source="team_match (PIT)",
        window="EWM half-life 8 team matches",
    ),
    _f(
        "team_xg_against_ewm",
        "team",
        "Team xG against per match (recency-weighted)",
        source="team_match (PIT)",
        window="EWM half-life 8 team matches",
    ),
    _f(
        "opp_xg_for_ewm",
        "fixture",
        "Opponent xG for per match (recency-weighted)",
        source="team_match (PIT)",
        window="EWM half-life 8 team matches",
        missing="NaN for opponents without PL history (promoted)",
    ),
    _f(
        "opp_xg_against_ewm",
        "fixture",
        "Opponent xG against per match (recency-weighted)",
        source="team_match (PIT)",
        window="EWM half-life 8 team matches",
        missing="NaN for opponents without PL history (promoted)",
    ),
    # ---- schedule (per target fixture)
    _f(
        "is_home",
        "fixture",
        "Target fixture at home",
        source="fixtures schedule (PIT)",
        ts="schedule_available_at ≤ cutoff",
        dtype="bool",
    ),
    _f(
        "horizon",
        "schedule",
        "Gameweeks ahead of the decision GW (0 = this GW)",
        source="derived",
        dtype="int",
        lo=0,
        hi=10,
    ),
    _f(
        "fixtures_in_gw",
        "schedule",
        "Team fixtures in the target GW (2 = double)",
        source="fixtures schedule (PIT)",
        dtype="int",
        lo=1,
        hi=3,
    ),
    _f(
        "days_rest",
        "schedule",
        "Days between the team's previous scheduled fixture and target",
        source="fixtures schedule (PIT)",
        missing="NaN for season opener",
        lo=0,
    ),
    # ---- live-only signals (NaN historically: ADR-0001 #7)
    _f(
        "status_flag",
        "live",
        "FPL status mapped: a=0, d=1, i/s/u/n=2",
        source="player_snapshots",
        ts="captured_at ≤ cutoff",
        missing="NaN when no live snapshot (all historical seasons)",
        lo=0,
        hi=2,
    ),
    _f(
        "chance_of_playing",
        "live",
        "FPL chance of playing next round (0–100)",
        source="player_snapshots",
        missing="NaN = no flag",
        lo=0,
        hi=100,
    ),
    _f(
        "penalty_taker",
        "live",
        "FPL penalties_order == 1",
        source="player_snapshots",
        missing="NaN historically",
        dtype="bool",
    ),
)

BY_NAME = {f.name: f for f in REGISTRY}
FEATURE_NAMES = tuple(f.name for f in REGISTRY)


def validate_frame(df: pd.DataFrame) -> list[str]:
    """Range/type checks for a built feature frame (returns human-readable problems)."""
    problems = []
    for spec in REGISTRY:
        if spec.name not in df.columns:
            problems.append(f"missing feature {spec.name}")
            continue
        s = pd.to_numeric(df[spec.name], errors="coerce").astype(float)
        vals = s[~np.isnan(s)]
        if spec.lo is not None and (vals < spec.lo - 1e-9).any():
            problems.append(f"{spec.name}: {int((vals < spec.lo - 1e-9).sum())} below {spec.lo}")
        if spec.hi is not None and (vals > spec.hi + 1e-9).any():
            problems.append(f"{spec.name}: {int((vals > spec.hi + 1e-9).sum())} above {spec.hi}")
    return problems


def dictionary_markdown() -> str:
    lines = [
        f"Feature version `{FEATURE_VERSION}` — generated from `fpl_features.registry`.",
        "",
        "| Feature | Family | Definition | Source | Window | Timestamp policy | Missing | "
        "Leakage | Type/range |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for f in REGISTRY:
        rng = f"{f.dtype}" + (f" [{f.lo}, {f.hi}]" if f.lo is not None or f.hi is not None else "")
        lines.append(
            f"| `{f.name}` | {f.family} | {f.definition} | {f.source} | {f.window} | "
            f"{f.timestamp_policy} | {f.missing_policy} | {f.leakage_risk} | {rng} |"
        )
    return "\n".join(lines)
