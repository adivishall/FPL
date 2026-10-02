"""Vectorised lineup scoring equals the domain rules sample by sample (auto-subs, armband)."""

from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from fpl_domain.enums import ChipType, Position
from fpl_domain.rules import load_ruleset
from fpl_domain.squad import Lineup, gameweek_points
from fpl_simulation.lineup_eval import score_lineup

RS = load_ruleset("2026-27")
POS = [Position.GK] * 2 + [Position.DEF] * 5 + [Position.MID] * 5 + [Position.FWD] * 3
CODES = list(range(1, 16))
POSITIONS = dict(zip(CODES, POS, strict=True))
# 3-4-3 with bench GK, DEF, MID, MID
LINEUP = Lineup(
    starters=(1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15),
    bench=(2, 6, 7, 12),
    captain=13,
    vice_captain=8,
)


@settings(max_examples=30, deadline=None)
@given(seed=st.integers(0, 10_000), chip=st.sampled_from([None, "bench_boost", "triple_captain"]))
def test_matches_domain_gameweek_points(seed: int, chip: str | None) -> None:
    rng = np.random.default_rng(seed)
    s = 64
    minutes = {c: np.where(rng.random(s) < 0.75, rng.integers(1, 91, s), 0) for c in CODES}
    points = {c: np.where(minutes[c] > 0, rng.integers(-1, 16, s), 0) for c in CODES}
    ct = ChipType(chip) if chip else None
    vec = score_lineup(LINEUP, POSITIONS, points, minutes, RS, ct)
    for k in range(s):
        ref = gameweek_points(
            LINEUP,
            POSITIONS,
            {c: int(points[c][k]) for c in CODES},
            {c: int(minutes[c][k]) for c in CODES},
            RS,
            chip,
        )["points"]
        assert vec[k] == ref
