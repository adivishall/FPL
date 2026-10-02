"""Score a lineup on every simulated sample with the exact FPL rules (§19, §63).

Vectorised over samples, this reproduces ``fpl_domain.squad.gameweek_points``: automatic
substitutions (starters in lineup order; first bench player in bench order who played and keeps
the formation legal; goalkeeper only for goalkeeper), captain → vice-captain fallback, Triple
Captain and Bench Boost. Equality with the domain implementation is property-tested.

This is what "rank the starting XI by simulated total contribution, not raw expected points"
(§63) and the paired plan comparisons of the decision engine are built on.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import numpy.typing as npt

from fpl_domain.enums import POSITIONS, ChipType, Position
from fpl_domain.rules import Ruleset
from fpl_domain.squad import Lineup

I = npt.NDArray[np.int64]  # noqa: E741


def score_lineup(
    lineup: Lineup,
    positions: Mapping[int, Position],
    points: Mapping[int, npt.NDArray[np.integer]],
    minutes: Mapping[int, npt.NDArray[np.integer]],
    ruleset: Ruleset,
    chip: ChipType | None = None,
) -> I:
    """Total gameweek points per sample (hits excluded). ``points[c]`` / ``minutes[c]`` are
    length-S arrays for every squad member."""
    s = len(next(iter(points.values())))
    pidx = {p: i for i, p in enumerate(POSITIONS)}
    lo = np.array([ruleset.lineup.min_per_position.get(p) for p in POSITIONS])
    hi = np.array([ruleset.lineup.max_per_position.get(p) for p in POSITIONS])
    played = {c: np.asarray(minutes[c]) > 0 for c in lineup.players}
    pts = {c: np.asarray(points[c], dtype=np.int64) for c in lineup.players}

    bench_boost = chip is ChipType.BENCH_BOOST
    counting = {c: np.ones(s, dtype=bool) for c in lineup.starters}
    if bench_boost:
        for c in lineup.bench:
            counting[c] = np.ones(s, dtype=bool)
    else:
        for c in lineup.bench:
            counting[c] = np.zeros(s, dtype=bool)
        counts = np.zeros((s, 4), dtype=np.int64)
        for c in lineup.starters:
            counts[:, pidx[positions[c]]] += 1
        used = {b: np.zeros(s, dtype=bool) for b in lineup.bench}
        for st in lineup.starters:
            need = ~played[st]
            if not need.any():
                continue
            ps = pidx[positions[st]]
            done = np.zeros(s, dtype=bool)
            for b in lineup.bench:
                pb = pidx[positions[b]]
                if (positions[st] is Position.GK) != (positions[b] is Position.GK):
                    continue
                new = counts.copy()
                new[:, ps] -= 1
                new[:, pb] += 1
                legal = np.all((new >= lo) & (new <= hi), axis=1)
                take = need & ~done & ~used[b] & played[b] & legal
                if take.any():
                    counts[take] = new[take]
                    used[b] |= take
                    counting[b] |= take
                    counting[st] &= ~take
                    done |= take
    mult = (
        ruleset.chips.triple_captain_multiplier
        if chip is ChipType.TRIPLE_CAPTAIN
        else ruleset.captaincy.captain_multiplier
    )
    cap, vice = lineup.captain, lineup.vice_captain
    cap_on = played[cap]
    vice_on = (
        ~cap_on & played[vice] & counting[vice]
        if ruleset.captaincy.vice_promotes_if_captain_did_not_play
        else np.zeros(s, dtype=bool)
    )
    total = np.zeros(s, dtype=np.int64)
    for c, mask in counting.items():
        total += np.where(mask, pts[c], 0)
    total += np.where(cap_on, (mult - 1) * pts[cap], 0)
    total += np.where(vice_on, (mult - 1) * pts[vice], 0)
    return total
