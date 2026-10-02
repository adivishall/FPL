"""Forecast evaluation metrics (§28, §57.2, §70.4).

Point: MAE, RMSE, bias. Probability: Brier, log loss, expected calibration error and reliability
tables. Distribution: CRPS from samples (energy form) or from quantiles (pinball average),
interval coverage, probability integral transform (PIT) values.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd

Arr = npt.ArrayLike


def mae(pred: Arr, y: Arr) -> float:
    return float(np.mean(np.abs(np.asarray(pred, float) - np.asarray(y, float))))


def rmse(pred: Arr, y: Arr) -> float:
    return float(np.sqrt(np.mean((np.asarray(pred, float) - np.asarray(y, float)) ** 2)))


def bias(pred: Arr, y: Arr) -> float:
    return float(np.mean(np.asarray(pred, float) - np.asarray(y, float)))


def brier(p: Arr, y: Arr) -> float:
    return float(np.mean((np.asarray(p, float) - np.asarray(y, float)) ** 2))


def log_loss(p: Arr, y: Arr, eps: float = 1e-12) -> float:
    pp = np.clip(np.asarray(p, float), eps, 1 - eps)
    yy = np.asarray(y, float)
    return float(-np.mean(yy * np.log(pp) + (1 - yy) * np.log(1 - pp)))


def reliability_table(p: Arr, y: Arr, n_bins: int = 10) -> pd.DataFrame:
    """Equal-width probability bins: mean forecast vs observed frequency (reliability diagram)."""
    pp, yy = np.asarray(p, float), np.asarray(y, float)
    edges = np.linspace(0, 1, n_bins + 1)
    idx = np.clip(np.digitize(pp, edges[1:-1]), 0, n_bins - 1)
    rows = []
    for b in range(n_bins):
        m = idx == b
        if m.any():
            rows.append(
                {
                    "bin_lo": edges[b],
                    "bin_hi": edges[b + 1],
                    "count": int(m.sum()),
                    "mean_pred": float(pp[m].mean()),
                    "observed": float(yy[m].mean()),
                }
            )
    return pd.DataFrame(rows)


def ece(p: Arr, y: Arr, n_bins: int = 10) -> float:
    """Expected calibration error: count-weighted |mean_pred − observed| over bins."""
    t = reliability_table(p, y, n_bins)
    if t.empty:
        return float("nan")
    return float(np.sum(t["count"] * np.abs(t["mean_pred"] - t["observed"])) / t["count"].sum())


def crps_samples(samples: npt.NDArray[np.floating], y: Arr) -> float:
    """Mean CRPS of an ensemble: E|X − y| − ½ E|X − X'| (per row; rows = observations).

    Uses the sorted-sample identity E|X − X'| = 2/n² Σ_i (2i − n − 1) x_(i) for O(n log n).
    """
    x = np.sort(np.asarray(samples, float), axis=1)
    yy = np.asarray(y, float)[:, None]
    n = x.shape[1]
    term1 = np.mean(np.abs(x - yy), axis=1)
    i = np.arange(1, n + 1)
    term2 = (2.0 / n**2) * np.sum((2 * i - n - 1) * x, axis=1)
    return float(np.mean(term1 - 0.5 * term2))


def pinball(q_pred: Arr, y: Arr, tau: float) -> float:
    d = np.asarray(y, float) - np.asarray(q_pred, float)
    return float(np.mean(np.maximum(tau * d, (tau - 1) * d)))


def crps_quantiles(quantiles: dict[float, Arr], y: Arr) -> float:
    """Approximate CRPS as 2 × mean pinball loss over the supplied quantile levels."""
    return float(2.0 * np.mean([pinball(q, y, tau) for tau, q in quantiles.items()]))


def coverage(lo: Arr, hi: Arr, y: Arr) -> float:
    yy = np.asarray(y, float)
    return float(np.mean((yy >= np.asarray(lo, float)) & (yy <= np.asarray(hi, float))))


def pit_values(
    samples: npt.NDArray[np.floating], y: Arr, rng: np.random.Generator | None = None
) -> npt.NDArray[np.floating]:
    """Randomised PIT for discrete outcomes: U(F(y−), F(y)) — uniform iff calibrated."""
    x = np.asarray(samples, float)
    yy = np.asarray(y, float)[:, None]
    f_lo = np.mean(x < yy, axis=1)
    f_hi = np.mean(x <= yy, axis=1)
    r = (rng or np.random.default_rng(0)).random(len(f_lo))
    return f_lo + r * (f_hi - f_lo)
