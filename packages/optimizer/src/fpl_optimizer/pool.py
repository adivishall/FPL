"""Candidate pool construction (§60.1 'include a wide candidate pool first, then prune only for
performance or feasibility reasons').

The full player universe (~700) makes multi-gameweek MILPs slow, so the optimiser works on a
pool that always contains: every owned player, user-forced players, and per position the top
players by horizon EV, the top by EV per £ (value picks / enablers that unlock funds elsewhere)
and the cheapest playable options. Pruning is a performance approximation; its cost is measured
(``ml/reports`` optimiser benchmark compares pooled vs full-universe solves).
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np

from fpl_optimizer.problem import PlayerTable, PoolSettings


def candidate_pool(
    players: PlayerTable,
    owned: Iterable[int],
    settings: PoolSettings,
    must_include: Iterable[int] = (),
    discount: float = 1.0,
) -> PlayerTable:
    keep: set[int] = set()
    idx = players.index()
    for c in list(owned) + list(must_include):
        if c in idx:
            keep.add(idx[c])
    w = discount ** np.arange(players.horizon)
    score = players.ev @ w
    playable = score > 0.05 * players.horizon
    for k in range(4):
        rows = np.flatnonzero((players.position == k) & playable)
        if len(rows) == 0:
            continue
        by_ev = rows[np.argsort(-score[rows], kind="stable")]
        keep.update(by_ev[: settings.per_position_top_ev].tolist())
        value = score[rows] / players.price[rows]
        keep.update(rows[np.argsort(-value, kind="stable")][: settings.per_position_top_value])
        cheap = rows[np.lexsort((-score[rows], players.price[rows]))]
        keep.update(cheap[: settings.per_position_cheapest].tolist())
    rows = np.array(sorted(keep), dtype=np.int64)
    return players.subset(rows)
