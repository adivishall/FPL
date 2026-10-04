"""The fixture schedule as it was believed at a point in time (ADR-0004, schedule rule S1).

Archived seasons only contain the *final* schedule: a fixture postponed in December appears in
the February gameweek it was eventually played in, so a naive reader at a December cutoff would
"know" about a blank that was announced on match day. This module reconstructs what was
published before such changes.

A Premier League season is a double round robin: every team plays exactly once in each of the
``2 × (teams − 1)`` rounds of the published schedule, and FPL gameweek ``g`` is round ``g``
unless a fixture was moved. Given the final schedule, the original round of every moved fixture
is recovered by an exact assignment problem — give each fixture one round so that every team
plays exactly once per round, moving as few fixtures as possible (ties: smallest shift) — solved
with HiGHS. The reconstruction is only accepted when it is **unique** (no other assignment needs
the same number of moves); otherwise — or when uniqueness cannot be proven within the time
limit — the season keeps the archive's timestamps and reports why (``status``).

Timeline of a moved fixture (all conservative — it can only under-state what was known):

* until ``known_at`` it is shown in its original round, at an estimated kickoff (median kickoff of
  that round's other fixtures). ``known_at = min(deadline of the original round, announced_at)``:
  a postponement is assumed to become public no earlier than the original deadline unless the
  new date was announced before it (fixtures brought forward);
* from ``known_at`` until ``announced_at`` it is unscheduled (not visible);
* from ``announced_at = max(season publication, final kickoff − announce lag)`` it is shown in
  its final gameweek.

Fixtures that were never moved are visible from the season's publication, including the regular
fixture of a team's double gameweek (only the *moved* fixture is lag-gated).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

DEFAULT_ANNOUNCE_LAG = pd.Timedelta(days=28)  # config/sources.yaml dgw_fixture_announce_lag_days
DEFAULT_DEADLINE_OFFSET = pd.Timedelta(minutes=90)  # derived_deadline_offset_minutes
_MOVE_COST = 1000.0
_TIME_LIMIT_S = 120.0
_NODE_LIMIT = 20_000  # bounds branch-and-bound memory; hitting it means "unverified"


@dataclass(frozen=True)
class FixtureMove:
    fixture_id: int
    original_gw: int
    final_gw: int
    original_kickoff_at: pd.Timestamp  # estimate (median kickoff of the original round)
    known_at: pd.Timestamp  # earliest time the move is treated as public
    announced_at: pd.Timestamp  # final slot visible from here


@dataclass(frozen=True)
class ScheduleReconstruction:
    season: str
    status: str  # "reconstructed" | "incomplete" | "ambiguous" | "unverified" | "infeasible"
    moves: dict[int, FixtureMove]
    published_at: pd.Timestamp | None


_CACHE: dict[tuple[str, str], ScheduleReconstruction] = {}


def _key(season: str, fx: pd.DataFrame) -> tuple[str, str]:
    cols = ["fixture_id", "gw", "kickoff_at", "home_team_code", "away_team_code"]
    h = pd.util.hash_pandas_object(fx[cols].sort_values("fixture_id"), index=False)
    return season, hashlib.sha256(h.to_numpy().tobytes()).hexdigest()


def _assign_rounds(
    fx: pd.DataFrame,
    n_rounds: int,
    exclude: list[list[int]] | None = None,
) -> tuple[float, list[int]] | str:
    """Min-cost exact assignment of fixtures to rounds: (cost, chosen variable ids), or
    "infeasible" / "time_limit". ``exclude`` adds no-good cuts (find the next-best)."""
    fx = fx.reset_index(drop=True)
    n = len(fx)
    teams = sorted(set(fx["home_team_code"]) | set(fx["away_team_code"]))
    ti = {t: i for i, t in enumerate(teams)}
    nv = n * n_rounds
    final = fx["gw"].to_numpy(int)
    rounds = np.arange(1, n_rounds + 1)
    cost = np.where(rounds[None, :] == final[:, None], 0.0, _MOVE_COST)
    cost = (cost + np.where(cost > 0, np.abs(rounds[None, :] - final[:, None]), 0)).ravel()
    exclude = exclude or []
    a = lil_matrix((n + len(teams) * n_rounds + len(exclude), nv))
    for i in range(n):
        a[i, i * n_rounds : (i + 1) * n_rounds] = 1
    for i, (h, w) in enumerate(zip(fx["home_team_code"], fx["away_team_code"], strict=True)):
        for t in (h, w):
            for g in range(n_rounds):
                a[n + ti[t] * n_rounds + g, i * n_rounds + g] = 1
    lo = [1.0] * (n + len(teams) * n_rounds)
    hi = [1.0] * (n + len(teams) * n_rounds)
    for k, chosen in enumerate(exclude):  # no-good cut: not exactly this assignment again
        for v in chosen:
            a[n + len(teams) * n_rounds + k, v] = 1
        lo.append(0.0)
        hi.append(float(len(chosen) - 1))
    res = milp(
        cost,
        constraints=LinearConstraint(a.tocsr(), lo, hi),
        integrality=np.ones(nv),
        bounds=Bounds(0, 1),
        options={"disp": False, "time_limit": _TIME_LIMIT_S, "node_limit": _NODE_LIMIT},
    )
    if res.status == 2:
        return "infeasible"
    if not res.success or res.x is None:  # time/node limit (with or without an incumbent)
        return "time_limit"
    x = np.round(res.x).astype(int)
    return float(res.fun), [int(v) for v in np.flatnonzero(x)]


def reconstruct(
    season: str,
    fixtures: pd.DataFrame,
    gameweeks: pd.DataFrame,
    announce_lag: pd.Timedelta = DEFAULT_ANNOUNCE_LAG,
    deadline_offset: pd.Timedelta = DEFAULT_DEADLINE_OFFSET,
) -> ScheduleReconstruction:
    """Reconstruct moved fixtures of one season (memoised by schedule content)."""
    fx = fixtures[fixtures["season"] == season]
    key = _key(season, fx)
    if key in _CACHE:
        return _CACHE[key]
    out = _reconstruct(season, fx, gameweeks, announce_lag, deadline_offset)
    _CACHE[key] = out
    return out


def _reconstruct(
    season: str,
    fx: pd.DataFrame,
    gameweeks: pd.DataFrame,
    announce_lag: pd.Timedelta,
    deadline_offset: pd.Timedelta,
) -> ScheduleReconstruction:
    teams = set(fx["home_team_code"]) | set(fx["away_team_code"])
    n_rounds = 2 * (len(teams) - 1)
    published = fx["schedule_available_at"].min() if len(fx) else None
    complete = (
        len(teams) >= 2
        and len(fx) == len(teams) * (len(teams) - 1)
        and fx["gw"].notna().all()
        and fx["kickoff_at"].notna().all()
        and int(fx["gw"].max()) <= n_rounds
    )
    if not complete:  # live / partial seasons keep their captured timestamps
        return ScheduleReconstruction(season, "incomplete", {}, published)
    f = fx.assign(gw=fx["gw"].astype(int)).sort_values("fixture_id").reset_index(drop=True)
    long = pd.concat(
        [
            f[["home_team_code", "gw"]].set_axis(["t", "gw"], axis=1),
            f[["away_team_code", "gw"]].set_axis(["t", "gw"], axis=1),
        ]
    )
    per = long.groupby(["t", "gw"]).size()
    if len(per) == len(teams) * n_rounds and (per == 1).all():
        # every team once in every round: no fixture moved (any other assignment moves some)
        return ScheduleReconstruction(season, "reconstructed", {}, published)
    best = _assign_rounds(f, n_rounds)
    if isinstance(best, str):
        status = "infeasible" if best == "infeasible" else "unverified"
        return ScheduleReconstruction(season, status, {}, published)
    # unique ⇔ no other assignment with the same number of moves (cost < best + one move)
    alt = _assign_rounds(f, n_rounds, exclude=[best[1]])
    if alt == "time_limit":
        return ScheduleReconstruction(season, "unverified", {}, published)
    if not isinstance(alt, str) and alt[0] < best[0] + _MOVE_COST - 0.5:
        return ScheduleReconstruction(season, "ambiguous", {}, published)
    original = {int(f["fixture_id"].iloc[v // n_rounds]): v % n_rounds + 1 for v in best[1]}
    moved = {fid: g for fid, g in original.items() if g != int(f.set_index("fixture_id").gw[fid])}
    stay = f[~f["fixture_id"].isin(list(moved))]
    median_ko = stay.groupby("gw")["kickoff_at"].median()
    gw = gameweeks[gameweeks["season"] == season].set_index("gw")["deadline_at"]
    by_id = f.set_index("fixture_id")
    moves: dict[int, FixtureMove] = {}
    for fid, g0 in moved.items():
        r = by_id.loc[fid]
        if g0 in median_ko.index:
            ko0 = pd.Timestamp(median_ko[g0])
        else:  # whole round moved (e.g. 2022-23 GW7): midpoint of the neighbouring rounds
            near = median_ko[(median_ko.index >= g0 - 1) & (median_ko.index <= g0 + 1)]
            ko0 = pd.Timestamp(near.min() + (near.max() - near.min()) / 2)
        deadline0 = pd.Timestamp(gw[g0]) if g0 in gw.index else ko0 - deadline_offset
        announced = max(pd.Timestamp(published), pd.Timestamp(r["kickoff_at"]) - announce_lag)
        moves[fid] = FixtureMove(
            fixture_id=fid,
            original_gw=g0,
            final_gw=int(r["gw"]),
            original_kickoff_at=ko0,
            known_at=min(deadline0, announced),
            announced_at=announced,
        )
    return ScheduleReconstruction(season, "reconstructed", moves, pd.Timestamp(published))


def believed_schedule(
    fixtures: pd.DataFrame, gameweeks: pd.DataFrame, as_of: pd.Timestamp
) -> pd.DataFrame:
    """Fixtures as published at ``as_of``; ``schedule_available_at`` becomes the time the shown
    version (gameweek + kickoff) became public. Rows not yet/no longer scheduled are dropped."""
    parts = []
    for season, fx in fixtures.groupby("season", sort=False):
        rec = reconstruct(str(season), fx, gameweeks)
        if rec.status != "reconstructed":
            parts.append(fx[fx["schedule_available_at"] <= as_of])
            continue
        out = fx.copy()
        out["schedule_available_at"] = rec.published_at  # original schedule: season publication
        keep = pd.Series(True, index=out.index)
        for i, fid in out["fixture_id"].items():
            mv = rec.moves.get(int(fid))
            if mv is None:
                continue
            if as_of < mv.known_at:
                out.at[i, "gw"] = mv.original_gw
                out.at[i, "kickoff_at"] = mv.original_kickoff_at
            elif as_of < mv.announced_at:
                keep[i] = False
            else:
                out.at[i, "schedule_available_at"] = mv.announced_at
        out = out[keep]
        parts.append(out[out["schedule_available_at"] <= as_of])
    if not parts:
        return fixtures.iloc[0:0]
    return pd.concat(parts).sort_values(["season", "fixture_id"])
