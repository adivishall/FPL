"""Rotation pairing (§19 'identify complementary players whose fixture patterns reduce bench
risk'): for two players of the same position, the value of picking the better of the two each
gameweek (Σ_t max(ev_a, ev_b)) versus committing to the better one throughout. High
complementarity means the pair covers each other's weak fixtures — typical for a goalkeeper pair
or the fifth defender."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import numpy as np

from fpl_optimizer.problem import PlayerTable


@dataclass(frozen=True)
class RotationPair:
    a: int
    b: int
    combined: float  # Σ_t max(ev_a, ev_b)
    best_single: float  # max(Σ ev_a, Σ ev_b)
    complementarity: float  # combined − best_single (≥ 0)
    weeks_a_better: int


def rotation_pairs(
    table: PlayerTable, codes: list[int], position: int, top: int = 5
) -> list[RotationPair]:
    idx = table.index()
    rows = [c for c in codes if table.position[idx[c]] == position]
    out = []
    for a, b in combinations(sorted(rows), 2):
        ea, eb = table.ev[idx[a]], table.ev[idx[b]]
        comb = float(np.maximum(ea, eb).sum())
        single = float(max(ea.sum(), eb.sum()))
        out.append(RotationPair(a, b, comb, single, comb - single, int((ea > eb).sum())))
    out.sort(key=lambda p: (-p.complementarity, -p.combined, p.a, p.b))
    return out[:top]
