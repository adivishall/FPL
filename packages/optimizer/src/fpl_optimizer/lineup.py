"""Exact starting XI / bench / armband solver for one gameweek (§19, §63, ADR-0007).

For a 15-player squad the search space is tiny (≤ 2 goalkeepers × C(13, 10) outfield sets), so
the solver enumerates every legal XI and is *exact* for the optimiser's per-gameweek objective:

    Σ_starters a_p + (m − 1)·a_captain + w_vice·a_vice + Σ_k w_k·ev(bench_k) + w_gk·ev(bench_gk)

where ``a_p = ev_p − λ_risk·(ev_p − q10_p)`` and m is the captain multiplier (3 with Triple
Captain). With Bench Boost every squad member counts with ``a_p`` and bench weights vanish.
Bench order follows the rearrangement inequality (weights are non-increasing): highest EV first.
It is used to (1) cross-check every MILP lineup, (2) score squads in the brute-force verifier and
(3) set lineups for squads chosen by other means.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

from fpl_domain.enums import ChipType, Position
from fpl_domain.rules import Ruleset
from fpl_domain.squad import Lineup
from fpl_optimizer.problem import ObjectiveWeights


@dataclass(frozen=True)
class LineupChoice:
    lineup: Lineup
    value: float  # optimiser objective for the gameweek (before discounting)
    expected_points: float  # risk-neutral EV of the XI with armband (no auto-sub credit)


def _key(code: int) -> int:
    return code


def best_lineup(
    codes: Sequence[int],
    positions: Mapping[int, Position],
    ev: Mapping[int, float],
    value: Mapping[int, float],
    weights: ObjectiveWeights,
    ruleset: Ruleset,
    chip: ChipType | None = None,
) -> LineupChoice:
    lo, hi = ruleset.lineup.min_per_position, ruleset.lineup.max_per_position
    n_start = ruleset.lineup.starters
    gks = sorted((c for c in codes if positions[c] is Position.GK), key=_key)
    outfield = sorted((c for c in codes if positions[c] is not Position.GK), key=_key)
    n_gk = lo.GK
    if n_gk != hi.GK or ruleset.lineup.bench_goalkeepers != len(gks) - n_gk:
        raise ValueError("lineup solver expects a fixed number of starting goalkeepers")
    cap_extra = (
        ruleset.chips.triple_captain_multiplier
        if chip is ChipType.TRIPLE_CAPTAIN
        else ruleset.captaincy.captain_multiplier
    ) - 1
    bench_boost = chip is ChipType.BENCH_BOOST
    bw = weights.bench_weights
    best: tuple[float, float, Lineup] | None = None
    for gk_start in combinations(gks, n_gk):
        gk_bench = [g for g in gks if g not in gk_start]
        for out_start in combinations(outfield, n_start - n_gk):
            counts = dict.fromkeys((Position.DEF, Position.MID, Position.FWD), 0)
            for c in out_start:
                counts[positions[c]] += 1
            if not all(lo.get(p) <= counts[p] <= hi.get(p) for p in counts):
                continue
            starters = list(gk_start) + list(out_start)
            bench_out = sorted(
                (c for c in outfield if c not in out_start), key=lambda c: (-ev[c], c)
            )
            ranked = sorted(starters, key=lambda c: (-value[c], c))
            cap, vice = ranked[0], ranked[1]
            v = sum(value[c] for c in starters) + cap_extra * value[cap]
            v += weights.vice_weight * value[vice]
            pts = sum(ev[c] for c in starters) + cap_extra * ev[cap]
            if bench_boost:
                v += sum(value[c] for c in bench_out + gk_bench)
                pts += sum(ev[c] for c in bench_out + gk_bench)
            else:
                v += sum(w * ev[c] for w, c in zip(bw, bench_out, strict=False))
                v += weights.bench_gk_weight * sum(ev[g] for g in gk_bench)
            lineup = Lineup(
                starters=tuple(starters),
                bench=tuple(gk_bench + bench_out),
                captain=cap,
                vice_captain=vice,
            )
            if best is None or v > best[0] + 1e-12:
                best = (v, pts, lineup)
    if best is None:
        raise ValueError("no legal formation for this squad")
    return LineupChoice(lineup=best[2], value=best[0], expected_points=best[1])
