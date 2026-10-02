from __future__ import annotations

import numpy as np
import pytest

from fpl_forecasting import metrics as fm


def test_point_metrics() -> None:
    assert fm.mae([1, 2, 3], [1, 3, 5]) == pytest.approx(1.0)
    assert fm.rmse([0, 0], [3, 4]) == pytest.approx(np.sqrt(12.5))
    assert fm.bias([2, 2], [1, 3]) == pytest.approx(0.0)


def test_probability_metrics() -> None:
    assert fm.brier([1.0, 0.0], [1, 0]) == 0.0
    assert fm.brier([0.5], [1]) == pytest.approx(0.25)
    assert fm.log_loss([0.5, 0.5], [1, 0]) == pytest.approx(np.log(2))
    rng = np.random.default_rng(0)
    p = rng.random(200_000)
    y = rng.random(200_000) < p  # perfectly calibrated by construction
    assert fm.ece(p, y) < 0.01
    assert fm.ece(np.full(1000, 0.9), np.zeros(1000)) == pytest.approx(0.9)
    tab = fm.reliability_table(p, y, 5)
    assert len(tab) == 5 and tab["count"].sum() == 200_000


def test_crps_samples_matches_closed_forms() -> None:
    assert fm.crps_samples(np.zeros((3, 50)), [0, 0, 0]) == pytest.approx(0.0)
    assert fm.crps_samples(np.full((1, 10), 2.0), [5.0]) == pytest.approx(3.0)
    rng = np.random.default_rng(1)
    x = rng.normal(0, 1, (1, 20_000))
    # CRPS of N(0,1) at y=0 is 2φ(0) − 1/√π ≈ 0.2337
    assert fm.crps_samples(x, [0.0]) == pytest.approx(0.2337, abs=0.01)


def test_quantile_metrics_and_coverage() -> None:
    assert fm.pinball([1.0], [2.0], 0.9) == pytest.approx(0.9)
    assert fm.pinball([3.0], [2.0], 0.9) == pytest.approx(0.1)
    assert fm.coverage([0, 0], [1, 1], [0.5, 2]) == pytest.approx(0.5)


def test_pit_uniform_for_calibrated_discrete_forecasts() -> None:
    rng = np.random.default_rng(2)
    lam = rng.uniform(0.5, 4, 4000)
    samples = rng.poisson(lam[:, None], (4000, 400))
    y = rng.poisson(lam)
    pit = fm.pit_values(samples, y, rng)
    hist, _ = np.histogram(pit, bins=10, range=(0, 1))
    assert hist.min() > 300 and hist.max() < 500
