"""Season evaluation statistics (M1.5 contract §5–§6, §11), on deterministic fixtures: the
scores computed from exact distribution counts equal their sample definitions; the inferential
unit is the gameweek; no verdict before the first formal look however one-sided a short run is;
calibration verdicts are equivalence tests against fixed margins; nothing recalibrates."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fpl_forecasting import distributions as dist
from fpl_forecasting import metrics as fm
from fpl_forecasting import season_eval as se


def _samples(rows: int = 40, n: int = 300, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    lam = rng.uniform(0.3, 6.0, size=(rows, 1))
    return rng.poisson(lam, size=(rows, n)).astype(float) - (rng.random((rows, 1)) < 0.2)


def test_fair_crps_from_counts_equals_the_sample_definition() -> None:
    x = _samples()
    y = np.arange(len(x)) % 9 - 1.0
    lo, counts = dist.encode(x)
    got = se.fair_crps(lo, counts, y)
    n = x.shape[1]
    e_abs = np.abs(x - y[:, None]).mean(axis=1)
    pairs = np.abs(x[:, :, None] - x[:, None, :]).sum(axis=(1, 2))
    assert np.allclose(got, e_abs - pairs / (2 * n * (n - 1)), rtol=0, atol=1e-12)
    # the published estimator keeps self-pairs, which understates the spread term: it is higher
    # by Σ|X_i − X_j| / (2n²(n − 1)) ≈ E|X − X'| / (2n), favouring larger ensembles
    v_stat = fm.crps_rows(x, y)
    assert np.allclose(v_stat - got, pairs / (2 * n * n * (n - 1)), atol=1e-12)


def test_expected_pit_coverage_is_the_randomised_pit_without_its_noise() -> None:
    x = _samples(rows=30, n=400, seed=2)
    y = (np.arange(30) % 7).astype(float)
    lo, counts = dist.encode(x)
    central, below, above = se.pit_coverage(lo, counts, y)
    assert np.allclose(central + below + above, 1.0)
    rng = np.random.default_rng(3)
    draws = np.stack([fm.pit_values(x, y, rng) for _ in range(4000)])
    assert np.allclose(((draws >= 0.1) & (draws <= 0.9)).mean(axis=0), central, atol=0.03)
    assert np.allclose((draws < 0.1).mean(axis=0), below, atol=0.03)
    # an outcome outside the support: PIT is a point at 0 or 1
    point = np.zeros((2, 5), dtype=np.int32)
    point[:, 2] = 10  # every sample = lo + 2
    c, b, a = se.pit_coverage(3, point, np.array([1.0, 9.0]))
    assert list(c) == [0.0, 0.0] and list(b) == [1.0, 0.0] and list(a) == [0.0, 1.0]


def test_sign_flip_floor_and_symmetry() -> None:
    assert se.sign_flip_p([1.0] * 5) == pytest.approx(2 / 32)  # never < 0.05 with 5 gameweeks
    assert se.sign_flip_p([1.0] * 6) == pytest.approx(2 / 64)
    assert se.sign_flip_p([1.0] * 8) == pytest.approx(2 / 256)
    assert se.sign_flip_p([1.0, -1.0, 1.0, -1.0]) == 1.0
    big = se.sign_flip_p(np.linspace(-0.2, 1.0, 20))  # seeded draws beyond 15
    assert big is not None and big == se.sign_flip_p(np.linspace(-0.2, 1.0, 20))


def test_t_interval_and_holm() -> None:
    assert se.t_interval([1.0, 2.0]) is None  # below three gameweeks: values only
    d = [0.1, -0.2, 0.3, 0.05]
    lo, hi = se.t_interval(d)  # type: ignore[misc]
    half = 3.182446305284263 * np.std(d, ddof=1) / 2
    assert (lo, hi) == pytest.approx((np.mean(d) - half, np.mean(d) + half))
    adj = se.holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": None})
    assert adj == pytest.approx({"a": 0.03, "b": 0.06, "c": 0.06, "d": None})


def test_equivalence_states() -> None:
    band = (0.77, 0.83)
    assert se.equivalence([0.8, 0.81], band)["verdict"] == "too_few_gameweeks"
    tight = [0.80, 0.805, 0.795, 0.80, 0.81, 0.79, 0.80, 0.80]
    assert se.equivalence(tight, band)["verdict"] == "within_tolerance"
    low = [0.70, 0.71, 0.69, 0.70, 0.72, 0.70, 0.71, 0.70]
    assert se.equivalence(low, band)["verdict"] == "outside_tolerance"
    noisy = [0.70, 0.90, 0.75, 0.85, 0.72, 0.88, 0.80, 0.79]
    assert se.equivalence(noisy, band)["verdict"] == "inconclusive"


def _sums(d_mse: list[float], seed: int = 0) -> pd.DataFrame:
    """Per-gameweek sums with a chosen model − ppg MSE difference per gameweek."""
    rng = np.random.default_rng(seed)
    rows = []
    for i, d in enumerate(d_mse):
        n = 600
        base = 4.0 + rng.normal(0, 0.3)
        r: dict[str, object] = {"target_gw": 7 + i, "n": n}
        for c in se.SUM_COLUMNS:
            r[c] = float(n * 0.1)
        r.update(
            se_model=n * (base + d),
            se_ppg=n * base,
            pit80=n * (0.80 + rng.normal(0, 0.005)),
            p6=n * 0.07,
            y6=n * (0.07 + rng.normal(0, 0.002)),
            pstart=n * 0.5,
            ystart=n * (0.5 + rng.normal(0, 0.004)),
            bins_6=[[n / 10, n * 0.007, n * 0.007]] * 10,
            bins_start=[[n / 10, n * 0.05, n * 0.05]] * 10,
            spearman=0.7,
        )
        rows.append(r)
    return pd.DataFrame(rows)


def test_no_verdict_before_the_first_look_however_one_sided() -> None:
    w = se.summarize(_sums([-0.3] * 5), verdicts=True)
    c1 = w["comparisons"]["C1"]
    assert w["status"] == "too_few_gameweeks" and w["looks"] == {}
    assert c1["direction"] == {"lower": 5, "higher": 0, "equal": 0}
    assert c1["p_sign_flip"] == pytest.approx(0.0625)
    assert c1["interval_label"] == "wide: 5 gameweeks"
    assert se.calibration_state(w)["state"] == "deferred"


def test_a_look_gives_verdicts_with_holm_and_equivalence() -> None:
    w = se.summarize(_sums([-0.3, -0.25, -0.35, -0.2, -0.3, -0.28, -0.32, -0.27]), verdicts=True)
    assert w["status"] == "look" and list(w["looks"]) == ["8"]
    look = w["looks"]["8"]
    assert look["comparisons"]["C1"]["verdict"] == "lower_error"
    assert look["comparisons"]["C1"]["p_holm"] >= look["comparisons"]["C1"]["p_sign_flip"]
    assert look["calibration"]["K3"] == "within_tolerance"
    # between looks: the latest look stands, the current window is monitoring
    w9 = se.summarize(
        _sums([-0.3, -0.25, -0.35, -0.2, -0.3, -0.28, -0.32, -0.27, 0.4]), verdicts=True
    )
    assert w9["status"] == "monitoring" and list(w9["looks"]) == ["8"]
    mixed = se.summarize(_sums([-0.3, 0.25, -0.35, 0.2, -0.3, 0.28, -0.32, 0.27]), verdicts=True)
    assert mixed["looks"]["8"]["comparisons"]["C1"]["verdict"] == "no_clear_difference"
    assert se.look_points(8, final=False) == [8]
    assert se.look_points(20, final=True) == [8, 16, 20]


def test_calibration_state_never_recalibrates() -> None:
    w = se.summarize(_sums([-0.3] * 8), verdicts=True)
    st = se.calibration_state(w)
    assert st["state"] in ("not_needed", "deferred") and st["look"] == 8
    off = _sums([-0.3] * 8)
    off["pit80"] = off["n"] * 0.70
    assert se.calibration_state(se.summarize(off, verdicts=True))["state"] == "review_suggested"


def test_ece_null_band_is_seeded_and_covers_calibrated_forecasts() -> None:
    rng = np.random.default_rng(4)
    p = rng.uniform(0, 0.3, size=3000)
    band = se.ece_null_band(p)
    assert band == se.ece_null_band(p)
    y = (rng.random(3000) < p).astype(float)
    observed = se._ece([se._bins(p, y)])
    assert observed is not None and observed <= band["null_p95"] * 1.5


def test_row_scores_need_every_input() -> None:
    with pytest.raises(KeyError):
        se.row_scores(pd.DataFrame({"y": [1.0]}))


def test_a_look_waits_for_every_earlier_gameweek_and_segments_never_carry_verdicts() -> None:
    """Review finding: look 8 used the first 8 *scored* gameweeks, so a gameweek still awaiting
    final results could be skipped and the look silently re-formed once it arrived."""
    sums = _sums([-0.3] * 9)
    sums["target_gw"] = [7, 8, 9, 11, 12, 13, 14, 15, 16]  # GW10 not final yet
    w = se.summarize(sums, verdicts=True, pending=[10])
    assert w["looks"] == {} and w["looks_waiting"] == {"8": 10}
    assert w["status"] == "monitoring"
    assert "GW10" in se.calibration_state(w)["reason"]
    seg = se.summarize(_sums([-0.3] * 8))  # a segment or another horizon
    assert seg["looks"] == {} and seg["status"] == "descriptive"


def test_monte_carlo_sign_flip_p_is_never_zero() -> None:
    p = se.sign_flip_p([-1.0] * 20)
    assert p is not None and p > 0 and p == pytest.approx(1 / (1 + se.SIGN_FLIP_DRAWS))


def test_intervals_need_the_minimum_events_and_rows() -> None:
    few = _sums([-0.3] * 4)
    few["y10"] = 2.0  # 8 hauls of 10+ in four gameweeks
    w = se.summarize(few)
    assert w["comparisons"]["brier_10_vs_climatology"]["interval_95"] is None
    assert "events" in w["comparisons"]["brier_10_vs_climatology"]["interval_label"]
    assert w["comparisons"]["C1"]["interval_95"] is not None
    small = few.assign(n=60)  # 240 rows in four gameweeks
    assert se.summarize(small)["comparisons"]["C1"]["interval_95"] is None
