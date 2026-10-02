"""Rotation pairing on hand-computable fixture patterns (§19)."""

from __future__ import annotations

import numpy as np

from fpl_decision.rotation import rotation_pairs
from fpl_optimizer.problem import PlayerTable


def test_alternating_goalkeepers_are_the_best_pair() -> None:
    ev = np.array([[5, 1, 5, 1], [1, 5, 1, 5], [3, 3, 3, 3], [2, 2, 2, 2]], dtype=float)
    t = PlayerTable(
        code=np.array([1, 2, 3, 4]),
        position=np.zeros(4, dtype=np.int64),
        team=np.array([1, 2, 3, 4]),
        price=np.full(4, 45),
        ev=ev,
    )
    pairs = rotation_pairs(t, [1, 2, 3, 4], position=0)
    best = pairs[0]
    assert (best.a, best.b) == (1, 2)
    assert best.combined == 20 and best.best_single == 12 and best.complementarity == 8
    assert best.weeks_a_better == 2
    assert all(p.complementarity >= 0 for p in pairs)
