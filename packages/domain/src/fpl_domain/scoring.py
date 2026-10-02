"""FPL points from match events under a versioned ruleset (§11, §77.1).

One vectorised implementation (``score_components``) serves three callers:
* scoring of official historical events (verified against official ``total_points``),
* the Monte Carlo simulator (arrays of sampled events),
* the scalar convenience wrapper ``score_match``.
Clean-sheet *eligibility* is an input: official data reports it; the simulator derives it from
goals conceded while the player was on the pitch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from fpl_domain.enums import POSITIONS, Position
from fpl_domain.rules.model import Ruleset

IntArray = npt.NDArray[np.int64]
ArrayLike = npt.ArrayLike

POSITION_INDEX = {p: i for i, p in enumerate(POSITIONS)}  # GK=0, DEF=1, MID=2, FWD=3

COMPONENTS = (
    "appearance",
    "goals",
    "assists",
    "clean_sheet",
    "goals_conceded",
    "saves",
    "penalty_saves",
    "penalty_misses",
    "yellow_cards",
    "red_cards",
    "own_goals",
    "defensive_contribution",
    "bonus",
)


def _per_position(table: Mapping[Position, int] | object, getter: str = "get") -> IntArray:
    return np.array([getattr(table, getter)(p) for p in POSITIONS], dtype=np.int64)


@dataclass(frozen=True)
class ScoringTables:
    """Ruleset scoring rules compiled into position-indexed arrays (fast, vectorisable)."""

    full_minutes: int
    app_below: int
    app_full: int
    goal: IntArray
    assist: int
    cs_min_minutes: int
    cs: IntArray
    gc_per: int
    gc_points: IntArray
    saves_per: int
    saves_points: int
    pen_save: int
    pen_miss: int
    yellow: int
    red: int
    own_goal: int
    dc_enabled: bool
    dc_threshold: IntArray  # 0 = not eligible
    dc_points: IntArray
    dc_uses_recoveries: npt.NDArray[np.bool_]

    @classmethod
    def from_ruleset(cls, rs: Ruleset) -> ScoringTables:
        sc = rs.scoring
        dc = sc.defensive_contribution
        thr = np.zeros(4, dtype=np.int64)
        pts = np.zeros(4, dtype=np.int64)
        rec = np.zeros(4, dtype=bool)
        if dc.enabled:
            for pos, rule in dc.by_position.items():
                i = POSITION_INDEX[pos]
                thr[i], pts[i] = rule.threshold, rule.points
                rec[i] = "recoveries" in rule.actions
        return cls(
            full_minutes=sc.appearance.full_minutes_threshold,
            app_below=sc.appearance.points_below_threshold,
            app_full=sc.appearance.points_at_or_above_threshold,
            goal=_per_position(sc.goal),
            assist=sc.assist,
            cs_min_minutes=sc.clean_sheet.min_minutes,
            cs=_per_position(sc.clean_sheet.points),
            gc_per=sc.goals_conceded.per_goals,
            gc_points=_per_position(sc.goals_conceded.points),
            saves_per=sc.saves.per_saves,
            saves_points=sc.saves.points,
            pen_save=sc.penalty_save,
            pen_miss=sc.penalty_miss,
            yellow=sc.yellow_card,
            red=sc.red_card,
            own_goal=sc.own_goal,
            dc_enabled=dc.enabled,
            dc_threshold=thr,
            dc_points=pts,
            dc_uses_recoveries=rec,
        )


def position_codes(positions: ArrayLike) -> IntArray:
    """Map an array of position strings to integer codes (GK=0 … FWD=3)."""
    arr = np.asarray(positions)
    if arr.dtype.kind in "iu":
        return arr.astype(np.int64)
    lookup = {p.value: i for p, i in POSITION_INDEX.items()}
    return np.vectorize(lambda v: lookup[str(v)], otypes=[np.int64])(arr)


def defensive_actions(
    pos: IntArray, cbi: ArrayLike, tackles: ArrayLike, recoveries: ArrayLike, t: ScoringTables
) -> IntArray:
    """Action count relevant to the player's position (CBIT for DEF, CBIRT for MID/FWD)."""
    base = np.asarray(cbi, dtype=np.int64) + np.asarray(tackles, dtype=np.int64)
    return base + np.where(t.dc_uses_recoveries[pos], np.asarray(recoveries, dtype=np.int64), 0)


def score_components(
    t: ScoringTables,
    pos: IntArray,
    minutes: ArrayLike,
    goals: ArrayLike,
    assists: ArrayLike,
    clean_sheet: ArrayLike,
    goals_conceded: ArrayLike,
    saves: ArrayLike,
    penalties_saved: ArrayLike,
    penalties_missed: ArrayLike,
    yellow_cards: ArrayLike,
    red_cards: ArrayLike,
    own_goals: ArrayLike,
    bonus: ArrayLike,
    dc_actions: ArrayLike | None = None,
) -> dict[str, IntArray]:
    """Points by component for arrays of player-match events (any broadcastable shape)."""
    m = np.asarray(minutes, dtype=np.int64)
    played = m > 0
    full = m >= t.full_minutes
    cs_ok = (np.asarray(clean_sheet, dtype=np.int64) > 0) & (m >= t.cs_min_minutes)
    out: dict[str, IntArray] = {
        "appearance": np.where(full, t.app_full, np.where(played, t.app_below, 0)),
        "goals": np.asarray(goals, dtype=np.int64) * t.goal[pos],
        "assists": np.asarray(assists, dtype=np.int64) * t.assist,
        "clean_sheet": np.where(cs_ok, t.cs[pos], 0),
        "goals_conceded": (np.asarray(goals_conceded, dtype=np.int64) // t.gc_per)
        * t.gc_points[pos],
        "saves": (np.asarray(saves, dtype=np.int64) // t.saves_per) * t.saves_points,
        "penalty_saves": np.asarray(penalties_saved, dtype=np.int64) * t.pen_save,
        "penalty_misses": np.asarray(penalties_missed, dtype=np.int64) * t.pen_miss,
        "yellow_cards": np.asarray(yellow_cards, dtype=np.int64) * t.yellow,
        "red_cards": np.asarray(red_cards, dtype=np.int64) * t.red,
        "own_goals": np.asarray(own_goals, dtype=np.int64) * t.own_goal,
        "bonus": np.asarray(bonus, dtype=np.int64),
    }
    if t.dc_enabled and dc_actions is not None:
        thr = t.dc_threshold[pos]
        acts = np.asarray(dc_actions, dtype=np.int64)
        out["defensive_contribution"] = np.where((thr > 0) & (acts >= thr), t.dc_points[pos], 0)
    else:
        out["defensive_contribution"] = np.zeros_like(out["appearance"])
    # Only cards count without minutes (players can be booked on the bench — verified on
    # official data, e.g. 2022-23 GW16); every other component requires an appearance.
    always = {"yellow_cards", "red_cards"}
    return {k: (v if k in always else np.where(played, v, 0)) for k, v in out.items()}


def total_points(components: Mapping[str, IntArray]) -> IntArray:
    return np.sum([components[c] for c in COMPONENTS], axis=0)


@dataclass(frozen=True)
class MatchEvents:
    minutes: int
    goals: int = 0
    assists: int = 0
    clean_sheet: bool = False
    goals_conceded: int = 0
    saves: int = 0
    penalties_saved: int = 0
    penalties_missed: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    own_goals: int = 0
    bonus: int = 0
    cbi: int = 0
    tackles: int = 0
    recoveries: int = 0


def score_match(events: MatchEvents, position: Position, ruleset: Ruleset) -> dict[str, int]:
    """Scalar reference API: points by component plus ``total``."""
    t = ScoringTables.from_ruleset(ruleset)
    pos = np.array([POSITION_INDEX[position]])
    dca = defensive_actions(pos, [events.cbi], [events.tackles], [events.recoveries], t)
    comps = score_components(
        t,
        pos,
        [events.minutes],
        [events.goals],
        [events.assists],
        [int(events.clean_sheet)],
        [events.goals_conceded],
        [events.saves],
        [events.penalties_saved],
        [events.penalties_missed],
        [events.yellow_cards],
        [events.red_cards],
        [events.own_goals],
        [events.bonus],
        dca,
    )
    out = {k: int(v[0]) for k, v in comps.items()}
    out["total"] = sum(out[c] for c in COMPONENTS)
    return out


def allocate_bonus(bps: list[int], awards: tuple[int, ...] = (3, 2, 1)) -> list[int]:
    """Bonus points per player of one match from BPS, applying FPL tie rules.

    Ties share the higher award and consume the following places: e.g. two players tied for
    first both get 3 and the next player gets 1; a tie for second gives 3, 2, 2; a tie for third
    gives 3, 2, 1, 1. Players with no positive rank place get 0.
    """
    order = sorted(range(len(bps)), key=lambda i: -bps[i])
    out = [0] * len(bps)
    place = 0
    i = 0
    while i < len(order) and place < len(awards):
        j = i
        while j + 1 < len(order) and bps[order[j + 1]] == bps[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = awards[place]
        place += j - i + 1
        i = j + 1
    return out
