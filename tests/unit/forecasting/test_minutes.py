"""Minutes model: calibration, structure and the live availability layer (§10, §58)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from fpl_domain.forecast import START_BUCKETS
from fpl_forecasting.minutes import (
    MINUTES_FEATURES,
    AvailabilityAdjustment,
    MinutesConfig,
    MinutesModel,
    RateBaselineMinutes,
    start_bucket,
    sub_bucket,
)

SMALL = MinutesConfig(n_estimators=60, min_child_samples=40)


def _frame(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    f = pd.DataFrame(0.0, index=range(n), columns=list(MINUTES_FEATURES))
    rate = rng.beta(0.8, 0.8, n)
    f["start_rate_short"] = np.clip(rate + rng.normal(0, 0.1, n), 0, 1)
    f["start_rate_long"] = rate
    f["horizon"] = rng.integers(0, 5, n)
    pos = rng.integers(0, 4, n)
    for k, c in enumerate(["pos_GK", "pos_DEF", "pos_MID", "pos_FWD"]):
        f[c] = (pos == k).astype(float)
    p_true = np.clip(rate * (1 - 0.03 * f["horizon"]), 0, 1)
    starts = rng.random(n) < p_true
    sub = (~starts) & (rng.random(n) < 0.3)
    minutes = np.where(
        starts, rng.choice([60, 78, 90], n), np.where(sub, rng.choice([10, 25], n), 0)
    )
    f["starts"] = starts.astype(int)
    f["minutes"] = minutes
    f["kickoff_at"] = pd.Timestamp("2024-08-01", tz="UTC") + pd.to_timedelta(np.arange(n), "h")
    f["p_true"] = p_true
    return f


def test_start_probability_is_calibrated_out_of_sample() -> None:
    model = MinutesModel(SMALL).fit(_frame(12000, 0))
    test = _frame(6000, 1)
    pred = model.predict(test)
    bins = np.clip((pred.p_start * 10).astype(int), 0, 9)
    for b in range(10):
        sel = bins == b
        if sel.sum() > 300:
            assert abs(pred.p_start[sel].mean() - test["starts"][sel].mean()) < 0.06
    assert np.corrcoef(pred.p_start, test["p_true"])[0, 1] > 0.85


def test_prediction_structure() -> None:
    model = MinutesModel(SMALL).fit(_frame(5000, 2))
    pred = model.predict(_frame(500, 3))
    assert np.allclose(pred.start_buckets.sum(axis=1), 1.0)
    assert np.allclose(pred.sub_buckets.sum(axis=1), 1.0)
    em = pred.expected_minutes()
    assert ((em >= 0) & (em <= 90)).all()
    assert ((pred.p_start >= 0) & (pred.p_start <= 1)).all()


def test_fit_is_deterministic() -> None:
    a = MinutesModel(SMALL).fit(_frame(4000, 4)).predict(_frame(300, 5))
    b = MinutesModel(SMALL).fit(_frame(4000, 4)).predict(_frame(300, 5))
    assert np.array_equal(a.p_start, b.p_start)
    assert np.array_equal(a.start_buckets, b.start_buckets)


def test_calibration_slice_is_the_most_recent() -> None:
    """Isotonic calibration must be fitted on the chronologically last rows (no shuffling)."""
    f = _frame(4000, 6)
    model = MinutesModel(SMALL).fit(f)
    assert model.trained_rows == 4000
    # shuffling input order must not change the fit (rows are sorted by kickoff first)
    g = MinutesModel(SMALL).fit(f.sample(frac=1.0, random_state=0))
    t = _frame(200, 7)
    assert np.allclose(model.predict(t).p_start, g.predict(t).p_start)


def test_bucket_helpers() -> None:
    m = pd.Series([1, 44, 45, 74, 75, 89, 90])
    assert start_bucket(m).tolist() == [0, 0, 1, 2, 3, 4, 4]
    assert sub_bucket(pd.Series([1, 15, 16, 45, 46, 89])).tolist() == [0, 0, 1, 2, 3, 3]
    assert len(START_BUCKETS) == 5


def test_rate_baseline_uses_recent_start_rate() -> None:
    f = _frame(2000, 8)
    f["sub_app_rate_long"] = 0.1
    f["mins_if_start_long"] = 85.0
    base = RateBaselineMinutes().fit(f)
    p = base.predict(f)
    assert np.corrcoef(p.p_start, f["start_rate_long"])[0, 1] > 0.9


def test_availability_layer() -> None:
    adj = AvailabilityAdjustment()
    nan = np.nan
    status = np.array([nan, 0, 1, 2, 1])
    chance = np.array([nan, nan, nan, nan, 75])
    m0 = adj.multiplier(status, chance, np.zeros(5))
    assert np.allclose(m0, [1.0, 1.0, 0.6, 0.05, 0.75])
    # flagged players recover over the horizon; healthy players are untouched
    m4 = adj.multiplier(status, chance, np.full(5, 4.0))
    assert np.all(m4[2:] > m0[2:]) and np.allclose(m4[:2], 1.0)
    # chance 0 is floored, never exactly zero (returns are possible)
    assert np.isclose(adj.multiplier(np.array([2.0]), np.array([0.0]), np.zeros(1))[0], 0.02)


def test_departed_players_do_not_recover() -> None:
    adj = AvailabilityAdjustment()
    m = adj.multiplier(
        np.array([2.0, 2.0]), np.array([0.0, 0.0]), np.array([4.0, 4.0]), np.array(["u", "i"])
    )
    assert m[0] == 0.0 and m[1] > 0.5
