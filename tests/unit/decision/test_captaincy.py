"""Captaincy profiles on constructed samples with known answers (§20)."""

from __future__ import annotations

import numpy as np
import pytest

from fpl_decision.captaincy import analyse_captaincy
from fpl_domain.enums import ChipType, Position
from fpl_domain.rules import load_ruleset
from fpl_domain.squad import Lineup

RS = load_ruleset("2026-27")
POS = [Position.GK] * 2 + [Position.DEF] * 5 + [Position.MID] * 5 + [Position.FWD] * 3
CODES = list(range(1, 16))
POSITIONS = dict(zip(CODES, POS, strict=True))
LINEUP = Lineup(
    starters=(1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15), bench=(2, 6, 7, 12), captain=13, vice_captain=8
)
S = 4000


def _samples():  # type: ignore[no-untyped-def]
    rng = np.random.default_rng(0)
    minutes = {c: np.full(S, 90) for c in CODES}
    points = {c: np.full(S, 2) for c in CODES}
    points[8] = np.full(S, 6)  # steady: always 6
    points[13] = np.where(rng.random(S) < 0.5, 14, 0)  # boom/bust: mean 7
    points[14] = np.where(rng.random(S) < 0.9, 5, 3)  # safe-ish, mean 4.8
    return points, minutes


def test_profiles_are_distinct_and_correct() -> None:
    points, minutes = _samples()
    a = analyse_captaincy(LINEUP, POSITIONS, points, minutes, RS, candidates=[8, 13, 14])
    assert a.expected == 13  # highest mean
    assert a.safe == 8  # best 25th percentile
    assert a.high_variance == 13  # only one that can haul (2 × 14 ≥ 13)
    o8, o13 = a.option(8), a.option(13)
    assert o13.mean_total - o8.mean_total == pytest.approx(1.0, abs=0.15)
    # paired: armband on 13 beats armband on 8 exactly when 13 scores 14
    assert a.beats_matrix[1, 0] == pytest.approx((points[13] == 14).mean())
    assert o13.p_captain_haul == pytest.approx(0.5, abs=0.03)


def test_vice_takes_over_when_captain_misses() -> None:
    points, minutes = _samples()
    minutes[13] = np.where(np.arange(S) % 2 == 0, 0, 90)
    points[13] = np.where(minutes[13] > 0, 14, 0)
    a = analyse_captaincy(LINEUP, POSITIONS, points, minutes, RS, candidates=[13])
    o = a.option(13)
    assert o.vice_captain == 8  # best other starter by mean
    # half the time 13 plays (14×2), otherwise auto-sub + vice 8 doubled
    assert o.captain_mean == pytest.approx(14.0, abs=0.5)


def test_triple_captain_scales_the_armband() -> None:
    points, minutes = _samples()
    base = analyse_captaincy(LINEUP, POSITIONS, points, minutes, RS, candidates=[8])
    tc = analyse_captaincy(
        LINEUP, POSITIONS, points, minutes, RS, candidates=[8], chip=ChipType.TRIPLE_CAPTAIN
    )
    assert tc.option(8).mean_total - base.option(8).mean_total == pytest.approx(6.0)
