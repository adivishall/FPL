"""Cross-table data-quality gates (§55.2) for one historical season batch.

Gate policy:
* ``critical`` → the batch is **quarantined**: nothing is written to canonical tables and the
  last good data stays in service (§6 'Validate: quarantine invalid batches').
* ``error``    → loaded, but recorded and surfaced on the Data Health screen.
* ``warning``/``info`` → recorded.

Some checks also *repair* data in a documented, reversible way (e.g. unpopulated ``starts`` for a
team-fixture is set to NULL rather than left as a misleading 0); every repair emits an issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from fpl_domain.enums import Severity
from fpl_ingestion.contracts import Issue


@dataclass
class SeasonFrames:
    season: str
    merged_gw: pd.DataFrame
    fixtures: pd.DataFrame
    teams: pd.DataFrame
    players_raw: pd.DataFrame
    issues: list[Issue] = field(default_factory=list)

    @property
    def gate_passed(self) -> bool:
        return not any(i.severity is Severity.CRITICAL for i in self.issues)


POS_BY_TYPE = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD", 5: "AM"}


def run_quality_gates(frames: SeasonFrames, deadline_offset_minutes: int = 90) -> SeasonFrames:
    """Apply all cross-table checks; returns frames with repairs and accumulated issues."""
    out = frames
    _filter_unsupported_positions(out)
    _referential_integrity(out)
    _team_and_position_consistency(out)
    _gameweek_assignment(out)
    _starters(out)
    _score_reconciliation(out)
    _completeness(out)
    _temporal_ordering(out, deadline_offset_minutes)
    _price_steps(out)
    return out


def _add(
    frames: SeasonFrames,
    code: str,
    sev: Severity,
    entity: str,
    msg: str,
    *,
    count: int = 1,
    **details: object,
) -> None:
    frames.issues.append(Issue(code, sev, entity, msg, count=count, details=dict(details)))


def _filter_unsupported_positions(f: SeasonFrames) -> None:
    am = f.merged_gw["position"] == "AM"
    if am.any():
        _add(
            f,
            "unsupported_position_filtered",
            Severity.INFO,
            "player_gw_stats",
            f"{int(am.sum())} Assistant-Manager rows removed (not a playing position)",
            count=int(am.sum()),
        )
        f.merged_gw = f.merged_gw[~am].copy()
    am_p = f.players_raw["element_type"] == 5
    if am_p.any():
        f.players_raw = f.players_raw[~am_p].copy()
    cheap = f.players_raw["now_cost"] < 30
    if cheap.any():
        _add(
            f,
            "range_violation",
            Severity.ERROR,
            "player",
            f"{int(cheap.sum())} playing-position prices below £3.0m",
            count=int(cheap.sum()),
        )
    cheap_rows = f.merged_gw["value"] < 30
    if cheap_rows.any():
        _add(
            f,
            "range_violation",
            Severity.ERROR,
            "player_gw_stats",
            f"{int(cheap_rows.sum())} playing-position match prices below £3.0m",
            count=int(cheap_rows.sum()),
        )


def _referential_integrity(f: SeasonFrames) -> None:
    fx_ids = set(f.fixtures["id"].astype(int))
    team_ids = set(f.teams["id"].astype(int))
    player_ids = set(f.players_raw["id"].astype(int))
    missing_fx = ~f.merged_gw["fixture"].isin(fx_ids)
    if missing_fx.any():
        _add(
            f,
            "ref_missing_fixture",
            Severity.CRITICAL,
            "player_gw_stats",
            f"{int(missing_fx.sum())} rows reference unknown fixtures",
            count=int(missing_fx.sum()),
        )
    missing_pl = ~f.merged_gw["element"].isin(player_ids)
    if missing_pl.any():
        _add(
            f,
            "ref_missing_player",
            Severity.CRITICAL,
            "player_gw_stats",
            f"{int(missing_pl.sum())} rows reference unknown players",
            count=int(missing_pl.sum()),
        )
    bad_team = ~f.fixtures["team_h"].isin(team_ids) | ~f.fixtures["team_a"].isin(team_ids)
    if bad_team.any():
        _add(
            f,
            "ref_missing_team",
            Severity.CRITICAL,
            "fixture",
            f"{int(bad_team.sum())} fixtures reference unknown teams",
            count=int(bad_team.sum()),
        )
    same = f.fixtures["team_h"] == f.fixtures["team_a"]
    if same.any():
        _add(
            f,
            "fixture_same_teams",
            Severity.CRITICAL,
            "fixture",
            "home team equals away team",
            count=int(same.sum()),
        )
    if not f.players_raw["code"].is_unique:
        _add(
            f,
            "player_code_not_unique",
            Severity.CRITICAL,
            "player",
            "stable player code is not unique within season",
        )


def _derived_team_id(f: SeasonFrames) -> pd.Series:
    fx = f.fixtures.set_index("id")
    home = f.merged_gw["fixture"].map(fx["team_h"])
    away = f.merged_gw["fixture"].map(fx["team_a"])
    return pd.Series(
        np.where(f.merged_gw["was_home"].astype(bool), home, away), index=f.merged_gw.index
    ).astype("Int64")


def _team_and_position_consistency(f: SeasonFrames) -> None:
    team_id = _derived_team_id(f)
    f.merged_gw["team_id"] = team_id
    name_by_id = f.teams.set_index("id")["name"]
    mismatch = f.merged_gw["team"].astype(str) != team_id.map(name_by_id).astype(str)
    if mismatch.any():
        # Expected for mid-season moves between PL clubs; the fixture-derived team is used.
        _add(
            f,
            "team_name_differs_from_fixture",
            Severity.INFO,
            "player_gw_stats",
            f"{int(mismatch.sum())} rows: source team column differs from fixture-derived team "
            "(fixture-derived value used)",
            count=int(mismatch.sum()),
        )
    fx = f.fixtures.set_index("id")
    expected_opp = np.where(
        f.merged_gw["was_home"].astype(bool),
        f.merged_gw["fixture"].map(fx["team_a"]),
        f.merged_gw["fixture"].map(fx["team_h"]),
    )
    bad_opp = f.merged_gw["opponent_team"].astype("Int64").to_numpy() != expected_opp
    if bad_opp.any():
        _add(
            f,
            "opponent_inconsistent",
            Severity.ERROR,
            "player_gw_stats",
            f"{int(bad_opp.sum())} rows have opponent inconsistent with fixture",
            count=int(bad_opp.sum()),
        )
    pos = f.merged_gw["element"].map(f.players_raw.set_index("id")["element_type"]).map(POS_BY_TYPE)
    bad_pos = pos.notna() & (pos != f.merged_gw["position"])
    if bad_pos.any():
        _add(
            f,
            "position_conflict",
            Severity.ERROR,
            "player_gw_stats",
            f"{int(bad_pos.sum())} rows: position differs from season player registry "
            "(registry value is authoritative, §51.1)",
            count=int(bad_pos.sum()),
        )


def _gameweek_assignment(f: SeasonFrames) -> None:
    fx_gw = f.merged_gw["fixture"].map(f.fixtures.set_index("id")["event"])
    bad = fx_gw.notna() & (fx_gw.astype("Int64") != f.merged_gw["GW"])
    if bad.any():
        _add(
            f,
            "gameweek_mismatch",
            Severity.ERROR,
            "player_gw_stats",
            f"{int(bad.sum())} rows: GW differs from the fixture's gameweek",
            count=int(bad.sum()),
        )


def _starters(f: SeasonFrames) -> None:
    mg = f.merged_gw
    if mg["starts"].isna().all():
        _add(
            f,
            "starts_unavailable",
            Severity.WARNING,
            "player_gw_stats",
            "starts column not provided for this season",
        )
        return
    sums = mg.groupby(["fixture", "team_id"])["starts"].sum()
    zero = sums[sums == 0].index
    if len(zero):
        key = pd.MultiIndex.from_frame(mg[["fixture", "team_id"]])
        mask = key.isin(zero)
        mg.loc[mask, "starts"] = pd.NA
        _add(
            f,
            "starts_unpopulated",
            Severity.WARNING,
            "player_gw_stats",
            f"{len(zero)} team-fixtures have no starters recorded; starts set to NULL",
            count=len(zero),
        )
    bad = sums[(sums != 0) & (sums != 11)]
    if len(bad):
        _add(
            f,
            "starters_not_eleven",
            Severity.ERROR,
            "player_gw_stats",
            f"{len(bad)} team-fixtures do not have exactly 11 starters",
            count=len(bad),
            examples=[list(map(int, k)) for k in bad.index[:5]],
        )


def _score_reconciliation(f: SeasonFrames) -> None:
    mg, fx = f.merged_gw, f.fixtures
    goals = mg.groupby(["fixture", "team_id"])["goals_scored"].sum()
    ogs = mg.groupby(["fixture", "team_id"])["own_goals"].sum()
    done = fx[fx["finished"].fillna(False).astype(bool)]
    bad = []
    for r in done.itertuples(index=False):
        h, a = int(r.team_h), int(r.team_a)
        gh = goals.get((r.id, h), 0) + ogs.get((r.id, a), 0)
        ga = goals.get((r.id, a), 0) + ogs.get((r.id, h), 0)
        if (gh, ga) != (r.team_h_score, r.team_a_score):
            bad.append(int(r.id))
    if bad:
        _add(
            f,
            "score_reconciliation",
            Severity.ERROR,
            "fixture",
            f"{len(bad)} finished fixtures: score ≠ Σ player goals + opponent own goals",
            count=len(bad),
            fixtures=bad[:20],
        )


def _completeness(f: SeasonFrames) -> None:
    mg, fx = f.merged_gw, f.fixtures
    played = mg[mg["minutes"] > 0].groupby(["fixture", "team_id"]).size()
    done = fx[fx["finished"].fillna(False).astype(bool)]
    missing = []
    for r in done.itertuples(index=False):
        for t in (int(r.team_h), int(r.team_a)):
            if played.get((r.id, t), 0) < 11:
                missing.append((int(r.id), t))
    if missing:
        _add(
            f,
            "incomplete_lineup_rows",
            Severity.ERROR,
            "fixture",
            f"{len(missing)} finished team-fixtures have <11 players with minutes",
            count=len(missing),
            examples=missing[:10],
        )


def _temporal_ordering(f: SeasonFrames, offset_min: int) -> None:
    fx = f.fixtures.dropna(subset=["event", "kickoff_time"])
    by_gw = fx.groupby("event")["kickoff_time"].agg(["min", "max"]).sort_index()
    next_deadline = by_gw["min"].shift(-1) - pd.Timedelta(minutes=offset_min)
    overlap = by_gw.index[(next_deadline.notna()) & (next_deadline <= by_gw["max"])]
    if len(overlap):
        _add(
            f,
            "gameweek_overlap",
            Severity.ERROR,
            "gameweek",
            "a gameweek's derived deadline precedes the previous gameweek's last kickoff",
            count=len(overlap),
            gameweeks=[int(g) for g in overlap],
        )
    mg_ko = f.merged_gw["kickoff_time"]
    fx_ko = f.merged_gw["fixture"].map(f.fixtures.set_index("id")["kickoff_time"])
    bad = fx_ko.notna() & (mg_ko != fx_ko)
    if bad.any():
        _add(
            f,
            "kickoff_mismatch",
            Severity.ERROR,
            "player_gw_stats",
            f"{int(bad.sum())} rows: kickoff differs from fixture kickoff",
            count=int(bad.sum()),
        )


def _price_steps(f: SeasonFrames) -> None:
    mg = f.merged_gw.sort_values(["element", "kickoff_time"])
    step = mg.groupby("element")["value"].diff().abs()
    big = step > 5
    if big.any():
        _add(
            f,
            "price_jump",
            Severity.WARNING,
            "player_gw_stats",
            f"{int(big.sum())} consecutive-fixture price changes larger than £0.5m",
            count=int(big.sum()),
        )
