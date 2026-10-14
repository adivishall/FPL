"""Season evaluation statistics (M1.5; ``docs/architecture/season-evaluation.md`` §5–§7, §11).

Pure functions from scored rows to the numbers the Season Review shows, shared by the live record
and the past-season reference so both are summarised by one method:

1. **Per-row scores** from a forecast's exact points distribution (per-value counts,
   ``fpl_forecasting.distributions``): the fair CRPS (no self-pairs) and the expected,
   non-randomised PIT coverage of the central 80 % with its two tails (Czado–Gneiting–Held) —
   deterministic, so adding a gameweek never changes an earlier row's value.
2. **Per-gameweek sums** (``gameweek_sums``): the target gameweek is the inferential unit —
   rows within a gameweek share fixtures and shocks.
3. **Window summaries** (``summarize``): levels; paired comparisons as per-gameweek differences
   d_g with a t-interval (G−1 degrees of freedom, from G ≥ 3) and the exact sign-flip test;
   verdicts only at the formal looks (G = 8, 16 and the season's last evaluation), Holm-adjusted
   within the primary family C1–C4; calibration by equivalence (TOST) against margins fixed
   before any live outcome; ECE descriptive, against the null consistency band.
4. ``calibration_state``: ``deferred`` / ``not_needed`` / ``review_suggested`` — nothing is ever
   recalibrated automatically.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy import stats

EVALUATOR_VERSION = "season-eval-1.1.0"
LOOKS = (8, 16)  # formal looks by scored gameweeks; plus the season's last evaluation
ALPHA = 0.05
MIN_INTERVAL_GAMEWEEKS = 3
MIN_VERDICT_GAMEWEEKS = LOOKS[0]
SIGN_FLIP_EXACT_MAX = 15  # enumerate all 2^G patterns up to here, else seeded draws
SIGN_FLIP_DRAWS = 20_000
SEED = 20261014
ECE_BINS = 10
ECE_NULL_DRAWS = 500
MIN_ROWS_FOR_INTERVAL = 300
MIN_EVENTS_FOR_INTERVAL = 50

# Predeclared before any live outcome (contract §5.2, §6.3). Lower is better for every d_g.
PRIMARY: dict[str, dict[str, str]] = {
    "C1": {"target": "points mean", "statistic": "mse", "model": "model", "baseline": "ppg"},
    "C2": {"target": "points distribution", "statistic": "crps", "baseline": "climatology"},
    "C3": {"target": "P(points >= 6)", "statistic": "brier_6", "baseline": "climatology"},
    "C4": {"target": "P(start)", "statistic": "brier_start", "baseline": "rate"},
}
SECONDARY: dict[str, dict[str, str]] = {
    "rmse_vs_recent_form": {"statistic": "mse", "baseline": "recent_form"},
    "rmse_vs_fpl_style": {"statistic": "mse", "baseline": "fpl_style"},
    "rmse_vs_position_mean": {"statistic": "mse", "baseline": "position_mean"},
    "brier_2_vs_climatology": {"statistic": "brier_2", "baseline": "climatology"},
    "brier_10_vs_climatology": {"statistic": "brier_10", "baseline": "climatology"},
    "minutes_mse_vs_rate": {"statistic": "minutes_mse", "baseline": "rate"},
}
MARGINS: dict[str, dict[str, Any]] = {  # equivalence margins (contract §6.3)
    "K1": {
        "name": "calibration-in-the-large P(>=6)",
        "statistic": "citl_6",
        "band": (-0.015, 0.015),
    },
    "K2": {
        "name": "calibration-in-the-large P(start)",
        "statistic": "citl_start",
        "band": (-0.03, 0.03),
    },
    "K3": {"name": "PIT central-80 coverage", "statistic": "pit80", "band": (0.77, 0.83)},
}
BASELINE_NAMES = {
    "ppg": "ppg × availability",
    "recent_form": "recent form",
    "fpl_style": "a simple rule resembling FPL's expected points",
    "position_mean": "position mean",
    "climatology": "climatology (position × start-rate pools)",
    "rate": "start-rate baseline",
}
POINT_BASELINES = {
    "ppg": "pred_ppg_availability",
    "recent_form": "pred_recent_form",
    "fpl_style": "pred_fpl_style",
    "position_mean": "pred_position_mean",
}
FLAGGED_STATUS = ("i", "d", "s", "n")


def metric_config() -> dict[str, Any]:
    """Everything that defines the numbers (part of an evaluation's input digest)."""
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "looks": list(LOOKS),
        "alpha": ALPHA,
        "primary": PRIMARY,
        "secondary": SECONDARY,
        "margins": {k: {**v, "band": list(v["band"])} for k, v in MARGINS.items()},
        "min_interval_gameweeks": MIN_INTERVAL_GAMEWEEKS,
        "ece_bins": ECE_BINS,
        "ece_null_draws": ECE_NULL_DRAWS,
        "seed": SEED,
        "populations": {
            "all": "every registered player with a fixture",
            "regulars": "start_rate_long >= 0.5",
            "flagged": "status i/d/s/n or chance of playing < 100",
        },
        "pit": "expected central coverage (non-randomised)",
        "pit_central": [0.1, 0.9],
        "crps": "fair (no self-pairs)",
        "sign_flip": {"exact_max": SIGN_FLIP_EXACT_MAX, "draws": SIGN_FLIP_DRAWS},
        "min_rows_for_interval": MIN_ROWS_FOR_INTERVAL,
        "min_events_for_interval": MIN_EVENTS_FOR_INTERVAL,
        "flagged_status": list(FLAGGED_STATUS),
        "verdicts": "population all, horizon 0, at settled looks only",
    }


# ----------------------------------------------------------------------------- per-row scores


def _values(lo: int, counts: npt.NDArray[np.integer]) -> npt.NDArray[np.float64]:
    return np.arange(lo, lo + counts.shape[1], dtype=float)


def fair_crps(
    lo: int, counts: npt.NDArray[np.integer], y: npt.ArrayLike
) -> npt.NDArray[np.float64]:
    """CRPS of each row's empirical distribution at ``y`` with the fair estimator:
    E|X − y| − Σ_{i≠j}|X_i − X_j| / (2 n (n − 1))."""
    c = counts.astype(float)
    v = _values(lo, counts)
    yv = np.asarray(y, dtype=float)[:, None]
    n = c.sum(axis=1)
    e_abs = (c * np.abs(v[None, :] - yv)).sum(axis=1) / n
    below_n = np.cumsum(c, axis=1) - c  # count of values strictly below each value
    below_v = np.cumsum(c * v, axis=1) - c * v
    pair_sum = 2.0 * (c * (below_n * v[None, :] - below_v)).sum(axis=1)  # Σ_{i,j} |X_i − X_j|
    return np.asarray(e_abs - pair_sum / (2.0 * n * (n - 1.0)), dtype=float)


def pit_coverage(
    lo: int, counts: npt.NDArray[np.integer], y: npt.ArrayLike, a: float = 0.1, b: float = 0.9
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Expected (non-randomised) PIT probabilities per row: of the central [a, b], below a and
    above b. For an integer outcome the randomised PIT is uniform on [F(y−), F(y)]; these are
    its probabilities of each region, so they have the randomised PIT's expectation and none
    of its noise."""
    c = counts.astype(float)
    v = _values(lo, counts)
    yv = np.asarray(y, dtype=float)[:, None]
    n = c.sum(axis=1)
    f_lo = (c * (v[None, :] < yv)).sum(axis=1) / n  # F(y−)
    f_hi = (c * (v[None, :] <= yv)).sum(axis=1) / n  # F(y)
    w = f_hi - f_lo

    def share(lo_edge: float, hi_edge: float, point_in: npt.NDArray[np.bool_]) -> Any:
        overlap = np.clip(np.minimum(f_hi, hi_edge) - np.maximum(f_lo, lo_edge), 0.0, None)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(w > 0, overlap / np.where(w > 0, w, 1.0), point_in.astype(float))

    central = share(a, b, (f_lo >= a) & (f_lo <= b))
    below = share(0.0, a, f_lo < a)
    above = share(b, 1.0, f_lo > b)
    return central, below, above


def distribution_scores(
    lo: int, counts: npt.NDArray[np.integer], y: npt.ArrayLike
) -> dict[str, npt.NDArray[np.float64]]:
    central, below, above = pit_coverage(lo, counts, y)
    return {"crps": fair_crps(lo, counts, y), "pit80": central, "pit_lo": below, "pit_hi": above}


ROW_INPUTS = (
    "target_gw",
    "y",
    "minutes_actual",
    "starts_actual",
    "mean",
    "p10",
    "p90",
    "prob_2_plus",
    "prob_6_plus",
    "prob_10_plus",
    "start_probability",
    "expected_minutes",
    "pred_ppg_availability",
    "pred_recent_form",
    "pred_fpl_style",
    "pred_position_mean",
    "base_p_start",
    "base_exp_minutes",
    "clim_prob_2_plus",
    "clim_prob_6_plus",
    "clim_prob_10_plus",
    "crps_model",
    "crps_climatology",
    "pit80",
    "pit_lo",
    "pit_hi",
)


def row_scores(rows: pd.DataFrame) -> pd.DataFrame:
    """Per-row losses and indicators from the standardised inputs (``ROW_INPUTS``)."""
    missing = [c for c in ROW_INPUTS if c not in rows.columns]
    if missing:
        raise KeyError(f"rows lack {missing}")
    y = rows["y"].to_numpy(float)
    out = pd.DataFrame({"target_gw": rows["target_gw"].to_numpy(int)})
    mean = rows["mean"].to_numpy(float)
    out["se_model"] = (mean - y) ** 2
    out["ae_model"] = np.abs(mean - y)
    out["err_model"] = mean - y
    for name, col in POINT_BASELINES.items():
        out[f"se_{name}"] = (rows[col].to_numpy(float) - y) ** 2
    for k in (2, 6, 10):
        ev = (y >= k).astype(float)
        out[f"y{k}"] = ev
        out[f"p{k}"] = rows[f"prob_{k}_plus"].to_numpy(float)
        out[f"brier_{k}_model"] = (out[f"p{k}"] - ev) ** 2
        out[f"brier_{k}_climatology"] = (rows[f"clim_prob_{k}_plus"].to_numpy(float) - ev) ** 2
    ystart = (rows["starts_actual"].to_numpy(float) >= 1).astype(float)
    out["ystart"] = ystart
    out["pstart"] = rows["start_probability"].to_numpy(float)
    out["brier_start_model"] = (out["pstart"] - ystart) ** 2
    out["brier_start_rate"] = (rows["base_p_start"].to_numpy(float) - ystart) ** 2
    mins = rows["minutes_actual"].to_numpy(float)
    out["minutes_mse_model"] = (rows["expected_minutes"].to_numpy(float) - mins) ** 2
    out["minutes_mse_rate"] = (rows["base_exp_minutes"].to_numpy(float) - mins) ** 2
    out["minutes_ae_model"] = np.abs(rows["expected_minutes"].to_numpy(float) - mins)
    out["crps_model"] = rows["crps_model"].to_numpy(float)
    out["crps_climatology"] = rows["crps_climatology"].to_numpy(float)
    for c in ("pit80", "pit_lo", "pit_hi"):
        out[c] = rows[c].to_numpy(float)
    out["covered_p10_p90"] = (
        (y >= rows["p10"].to_numpy(float)) & (y <= rows["p90"].to_numpy(float))
    ).astype(float)
    out["mean"] = mean
    out["y"] = y
    return out


# ----------------------------------------------------------------------------- per gameweek

SUM_COLUMNS = (
    "se_model",
    "ae_model",
    "err_model",
    *(f"se_{b}" for b in POINT_BASELINES),
    *(f"brier_{k}_{m}" for k in (2, 6, 10) for m in ("model", "climatology")),
    "brier_start_model",
    "brier_start_rate",
    "minutes_mse_model",
    "minutes_mse_rate",
    "minutes_ae_model",
    "crps_model",
    "crps_climatology",
    "pit80",
    "pit_lo",
    "pit_hi",
    "covered_p10_p90",
    "p6",
    "y2",
    "y6",
    "y10",
    "pstart",
    "ystart",
)


def _bins(p: npt.NDArray[np.float64], y: npt.NDArray[np.float64]) -> list[list[float]]:
    idx = np.minimum((np.clip(p, 0, 1) * ECE_BINS).astype(int), ECE_BINS - 1)
    return [
        [float((idx == b).sum()), float(p[idx == b].sum()), float(y[idx == b].sum())]
        for b in range(ECE_BINS)
    ]


def gameweek_sums(scored: pd.DataFrame) -> pd.DataFrame:
    """One row per target gameweek: row count and sums of every per-row score, the ECE bin sums
    of P(≥ 6) and P(start), and the within-gameweek Spearman correlation of mean vs outcome."""
    out = []
    for gw, g in scored.groupby("target_gw", sort=True):
        row: dict[str, Any] = {"target_gw": int(str(gw)), "n": len(g)}
        for c in SUM_COLUMNS:
            row[c] = float(g[c].sum())
        row["bins_6"] = _bins(g["p6"].to_numpy(float), g["y6"].to_numpy(float))
        row["bins_start"] = _bins(g["pstart"].to_numpy(float), g["ystart"].to_numpy(float))
        rho = (
            stats.spearmanr(g["mean"], g["y"]).statistic
            if len(g) >= 10 and g["mean"].nunique() > 1 and g["y"].nunique() > 1
            else np.nan
        )
        row["spearman"] = float(rho)
        out.append(row)
    return pd.DataFrame(out)


# ----------------------------------------------------------------------------- inference


def t_interval(d: npt.ArrayLike, level: float = 0.95) -> tuple[float, float] | None:
    """mean(d) ± t_{(1+level)/2, G−1} · sd(d)/√G; None below ``MIN_INTERVAL_GAMEWEEKS``."""
    x = np.asarray(d, dtype=float)
    if len(x) < MIN_INTERVAL_GAMEWEEKS:
        return None
    half = stats.t.ppf((1 + level) / 2, len(x) - 1) * x.std(ddof=1) / np.sqrt(len(x))
    return float(x.mean() - half), float(x.mean() + half)


def sign_flip_p(d: npt.ArrayLike) -> float | None:
    """Two-sided p of the exact sign-flip randomisation test of mean(d) = 0 (all 2^G patterns
    up to G = 15, seeded draws beyond); smallest attainable value 2/2^G."""
    x = np.asarray(d, dtype=float)
    g = len(x)
    if g == 0:
        return None
    obs = abs(x.mean())
    if g <= SIGN_FLIP_EXACT_MAX:
        signs = np.array(list(itertools.product((-1.0, 1.0), repeat=g)))
    else:
        rng = np.random.default_rng(SEED)
        signs = rng.choice((-1.0, 1.0), size=(SIGN_FLIP_DRAWS, g))
    means = np.abs((signs * x).mean(axis=1))
    tol = 1e-12 * max(1.0, obs)
    hits = int((means >= obs - tol).sum())
    if g <= SIGN_FLIP_EXACT_MAX:  # every pattern, the observed one included
        return float(hits / len(means))
    return float((1 + hits) / (1 + len(means)))  # a valid Monte Carlo p-value, never 0


def holm(pvals: dict[str, float | None]) -> dict[str, float | None]:
    """Holm step-down adjusted p-values (monotone); None stays None and is not counted."""
    have = sorted(((p, k) for k, p in pvals.items() if p is not None), key=lambda t: t[0])
    m_ = len(have)
    out: dict[str, float | None] = dict.fromkeys(pvals)
    running = 0.0
    for i, (p, k) in enumerate(have):
        running = max(running, min(1.0, (m_ - i) * p))
        out[k] = running
    return out


def _direction(d: npt.ArrayLike) -> dict[str, int]:
    x = np.asarray(d, dtype=float)
    return {"lower": int((x < 0).sum()), "higher": int((x > 0).sum()), "equal": int((x == 0).sum())}


def equivalence(stat_g: npt.ArrayLike, band: Sequence[float]) -> dict[str, Any]:
    """TOST at 5 %: ``within`` when the 90 % t-interval lies inside the band, ``outside`` when
    the 95 % interval lies entirely outside it, else ``inconclusive``."""
    ci90, ci95 = t_interval(stat_g, 0.90), t_interval(stat_g, 0.95)
    lo, hi = band
    if ci90 is None or ci95 is None:
        verdict = "too_few_gameweeks"
    elif lo <= ci90[0] and ci90[1] <= hi:
        verdict = "within_tolerance"
    elif ci95[1] < lo or ci95[0] > hi:
        verdict = "outside_tolerance"
    else:
        verdict = "inconclusive"
    return {"verdict": verdict, "interval_90": ci90, "interval_95": ci95}


# ----------------------------------------------------------------------------- window summary


def _per_gw(sums: pd.DataFrame, num: str, den: str = "n") -> npt.NDArray[np.float64]:
    return (sums[num] / sums[den]).to_numpy(float)


def _diff(sums: pd.DataFrame, statistic: str, baseline: str) -> npt.NDArray[np.float64]:
    model = {"mse": "se_model", "minutes_mse": "minutes_mse_model"}.get(
        statistic, f"{statistic}_model"
    )
    base = {"mse": f"se_{baseline}", "minutes_mse": "minutes_mse_rate"}.get(
        statistic, f"{statistic}_{baseline}"
    )
    return _per_gw(sums, model) - _per_gw(sums, base)


def _ece(bins: Iterable[Sequence[Sequence[float]]]) -> float | None:
    tot = np.zeros((ECE_BINS, 3))
    for b in bins:
        tot += np.asarray(b, dtype=float)
    n = tot[:, 0].sum()
    return float(np.abs(tot[:, 1] - tot[:, 2]).sum() / n) if n else None


def ece_null_band(p: npt.ArrayLike, draws: int = ECE_NULL_DRAWS) -> dict[str, float]:
    """The ECE a perfectly calibrated forecaster would show on these rows and bins (y ~
    Bernoulli(p), seeded): its median and 95th percentile (Bröcker–Smith consistency band)."""
    pv = np.clip(np.asarray(p, dtype=float), 0, 1)
    rng = np.random.default_rng(SEED)
    idx = np.minimum((pv * ECE_BINS).astype(int), ECE_BINS - 1)
    sims = np.empty(draws)
    for i in range(draws):
        y = rng.random(len(pv)) < pv
        sp = np.bincount(idx, weights=pv, minlength=ECE_BINS)
        sy = np.bincount(idx, weights=y.astype(float), minlength=ECE_BINS)
        sims[i] = np.abs(sp - sy).sum() / max(len(pv), 1)
    return {"null_median": float(np.median(sims)), "null_p95": float(np.quantile(sims, 0.95))}


EVENTS = {"brier_2": "y2", "brier_6": "y6", "brier_10": "y10", "brier_start": "ystart"}


def interval_allowed(sums: pd.DataFrame, statistic: str) -> tuple[bool, str | None]:
    """Contract §6.4 minimum sizes for an interval: ≥ 3 gameweeks and, for point, CRPS and
    minutes statistics ≥ 300 rows, for probabilities ≥ 50 events and ≥ 50 non-events."""
    g, rows = len(sums), float(sums["n"].sum()) if len(sums) else 0.0
    if g < MIN_INTERVAL_GAMEWEEKS:
        return False, f"fewer than {MIN_INTERVAL_GAMEWEEKS} gameweeks"
    ev = EVENTS.get(statistic)
    if ev is not None:
        events = float(sums[ev].sum())
        if min(events, rows - events) < MIN_EVENTS_FOR_INTERVAL:
            return False, f"fewer than {MIN_EVENTS_FOR_INTERVAL} events or non-events"
    elif rows < MIN_ROWS_FOR_INTERVAL:
        return False, f"fewer than {MIN_ROWS_FOR_INTERVAL} rows"
    return True, None


def _comparison(d: npt.NDArray[np.float64], sums: pd.DataFrame, statistic: str) -> dict[str, Any]:
    g = len(d)
    ok, why = interval_allowed(sums, statistic)
    ci = t_interval(d) if ok else None
    return {
        "G": g,
        "mean": float(d.mean()) if g else None,
        "interval_95": ci,
        "interval_label": (f"wide: {g} gameweeks" if ci is not None and g < 8 else why),
        "p_sign_flip": sign_flip_p(d),
        "direction": _direction(d),
        "per_gameweek": [float(x) for x in d],
    }


def _verdict(d: npt.NDArray[np.float64], p_holm: float | None) -> str:
    ci = t_interval(d)
    if ci is None or p_holm is None:
        return "no_clear_difference"
    if p_holm < ALPHA and ci[1] < 0:
        return "lower_error"
    if p_holm < ALPHA and ci[0] > 0:
        return "higher_error"
    return "no_clear_difference"


def look_points(g: int, final: bool) -> list[int]:
    """The formal looks reached with ``g`` scored gameweeks (the last one also when the
    season's final gameweek is scored)."""
    pts = [x for x in LOOKS if x <= g]
    if final and g >= MIN_VERDICT_GAMEWEEKS and g not in pts:
        pts.append(g)
    return pts


def levels(sums: pd.DataFrame) -> dict[str, Any]:
    """Pooled levels of a window (row-weighted), from its per-gameweek sums."""
    n = float(sums["n"].sum())
    tot = sums[list(SUM_COLUMNS)].sum()
    return {
        "rmse": {
            "model": float(np.sqrt(tot["se_model"] / n)),
            **{b: float(np.sqrt(tot[f"se_{b}"] / n)) for b in POINT_BASELINES},
        },
        "mae_model": float(tot["ae_model"] / n),
        "bias_model": float(tot["err_model"] / n),
        "crps": {
            "model": float(tot["crps_model"] / n),
            "climatology": float(tot["crps_climatology"] / n),
        },
        **{
            f"brier_{k}": {
                "model": float(tot[f"brier_{k}_model"] / n),
                "climatology": float(tot[f"brier_{k}_climatology"] / n),
            }
            for k in (2, 6, 10)
        },
        "brier_start": {
            "model": float(tot["brier_start_model"] / n),
            "rate": float(tot["brier_start_rate"] / n),
        },
        "minutes_rmse": {
            "model": float(np.sqrt(tot["minutes_mse_model"] / n)),
            "rate": float(np.sqrt(tot["minutes_mse_rate"] / n)),
        },
        "minutes_mae_model": float(tot["minutes_ae_model"] / n),
        "pit80": float(tot["pit80"] / n),
        "pit_below_10": float(tot["pit_lo"] / n),
        "pit_above_90": float(tot["pit_hi"] / n),
        "coverage_p10_p90_inclusive": float(tot["covered_p10_p90"] / n),
        "citl_6": float((tot["p6"] - tot["y6"]) / n),
        "citl_start": float((tot["pstart"] - tot["ystart"]) / n),
        "events_6": int(tot["y6"]),
        "events_start": int(tot["ystart"]),
        "ece_6": _ece(sums["bins_6"]),
        "ece_start": _ece(sums["bins_start"]),
        "spearman_mean": float(np.nanmean(sums["spearman"]))
        if sums["spearman"].notna().any()
        else None,
    }


def comparison_means(sums: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Mean per-gameweek difference and direction counts of the primary comparisons (no
    tests): the past-season reference needs only these."""
    out = {}
    for k, spec in PRIMARY.items():
        d = _diff(sums, spec["statistic"], spec["baseline"])
        out[k] = {"mean": float(d.mean()) if len(d) else None, "direction": _direction(d)}
    return out


def _look(head: pd.DataFrame) -> dict[str, Any]:
    raw = {
        k: sign_flip_p(_diff(head, PRIMARY[k]["statistic"], PRIMARY[k]["baseline"]))
        for k in PRIMARY
    }
    adj = holm(raw)
    comps = {}
    for k in PRIMARY:
        d = _diff(head, PRIMARY[k]["statistic"], PRIMARY[k]["baseline"])
        ok, _ = interval_allowed(head, PRIMARY[k]["statistic"])
        comps[k] = {
            "p_sign_flip": raw[k],
            "p_holm": adj[k],
            "interval_95": t_interval(d) if ok else None,
            "verdict": _verdict(d, adj[k]) if ok else "no_clear_difference",
        }
    cal = {}
    for k, spec in MARGINS.items():
        ok, _ = interval_allowed(
            head, "brier_6" if k == "K1" else "brier_start" if k == "K2" else "pit"
        )
        cal[k] = (
            equivalence(_calibration_stat(head, spec["statistic"]), spec["band"])["verdict"]
            if ok
            else "inconclusive"
        )
    return {
        "gameweeks": [int(x) for x in head["target_gw"]],
        "comparisons": comps,
        "calibration": cal,
    }


def summarize(
    sums: pd.DataFrame,
    final: bool = False,
    verdicts: bool = False,
    pending: Iterable[int] = (),
) -> dict[str, Any]:
    """Every number of one population × horizon window from its per-gameweek sums (ordered by
    target gameweek). Formal looks — the only place verdicts appear — are computed only when
    ``verdicts`` (the primary window: all players, horizon 0; segments never carry verdicts)
    and only once the look's gameweeks are settled: look L uses the first L scored gameweeks
    and is issued only when no gameweek before the L-th is still awaiting final results
    (``pending``), so a later result can never silently re-form it; a revised official result
    inside it revises it, and the evaluation's changes block says so."""
    sums = sums.sort_values("target_gw").reset_index(drop=True)
    g = len(sums)
    out: dict[str, Any] = {"G": g, "gameweeks": [int(x) for x in sums["target_gw"]]}
    if g == 0:
        out["status"] = "no_scored_gameweeks"
        return out
    out["rows"] = int(sums["n"].sum())
    out["levels"] = levels(sums)
    comps: dict[str, Any] = {}
    for key, spec in {**PRIMARY, **SECONDARY}.items():
        d = _diff(sums, spec["statistic"], spec["baseline"])
        comps[key] = {
            **{k: v for k, v in spec.items() if k != "model"},
            "baseline_label": BASELINE_NAMES[spec["baseline"]],
            **_comparison(d, sums, spec["statistic"]),
            "primary": key in PRIMARY,
        }
    out["comparisons"] = comps
    look_out: dict[str, Any] = {}
    waiting: dict[str, int] = {}
    if verdicts:
        waiting_on = sorted(int(p) for p in pending)
        for at in look_points(g, final):
            head = sums.iloc[:at]
            before = [p for p in waiting_on if p < int(head["target_gw"].max())]
            if before:
                waiting[str(at)] = before[0]
                continue
            look_out[str(at)] = _look(head)
    calibration: dict[str, Any] = {}
    for k, mspec in MARGINS.items():
        stat_g = _calibration_stat(sums, mspec["statistic"])
        ok, why = interval_allowed(
            sums, "brier_6" if k == "K1" else "brier_start" if k == "K2" else "pit"
        )
        eq = (
            equivalence(stat_g, mspec["band"]) if ok else {"interval_90": None, "interval_95": None}
        )
        calibration[k] = {
            "name": mspec["name"],
            "band": list(mspec["band"]),
            "per_gameweek": [float(x) for x in stat_g],
            "mean": float(stat_g.mean()),
            "interval_90": eq["interval_90"],
            "interval_95": eq["interval_95"],
            "interval_label": None if ok else why,
        }
    out["calibration"] = calibration
    out["looks"] = look_out
    out["looks_waiting"] = waiting  # look → the earliest gameweek whose results it awaits
    if not verdicts:
        out["status"] = "descriptive"
    elif g < MIN_VERDICT_GAMEWEEKS:
        out["status"] = "too_few_gameweeks"
    else:
        out["status"] = "look" if str(g) in look_out else "monitoring"
    out["per_gameweek"] = [
        {
            "gw": int(r.target_gw),
            "n": int(r.n),
            "rmse_model": float(np.sqrt(r.se_model / r.n)),
            "rmse_ppg": float(np.sqrt(r.se_ppg / r.n)),
            "bias_model": float(r.err_model / r.n),
            "crps_model": float(r.crps_model / r.n),
            "crps_climatology": float(r.crps_climatology / r.n),
            "brier_6_model": float(r.brier_6_model / r.n),
            "brier_start_model": float(r.brier_start_model / r.n),
            "pit80": float(r.pit80 / r.n),
            "citl_6": float((r.p6 - r.y6) / r.n),
            "citl_start": float((r.pstart - r.ystart) / r.n),
            "events_6": int(r.y6),
        }
        for r in sums.itertuples()
    ]
    return out


def _calibration_stat(sums: pd.DataFrame, statistic: str) -> npt.NDArray[np.float64]:
    if statistic == "citl_6":
        return ((sums["p6"] - sums["y6"]) / sums["n"]).to_numpy(float)
    if statistic == "citl_start":
        return ((sums["pstart"] - sums["ystart"]) / sums["n"]).to_numpy(float)
    return (sums["pit80"] / sums["n"]).to_numpy(float)


def calibration_state(window: dict[str, Any]) -> dict[str, Any]:
    """The calibration state of the live record (contract §11): from its latest formal look."""
    g = int(window.get("G", 0))
    looks = window.get("looks") or {}
    if not looks:
        waiting = window.get("looks_waiting") or {}
        reason = (
            f"the first formal check waits for GW{min(waiting.values())}'s final results"
            if waiting
            else f"{g} of {MIN_VERDICT_GAMEWEEKS} live gameweeks final"
        )
        return {"state": "deferred", "reason": reason, "final_gameweeks": g}
    latest = looks[max(looks, key=int)]
    verdicts = latest["calibration"]
    if any(v == "outside_tolerance" for v in verdicts.values()):
        state, reason = "review_suggested", "a calibration check is outside its tolerance"
    elif all(v == "within_tolerance" for v in verdicts.values()):
        state, reason = "not_needed", "every calibration check is within its tolerance"
    else:
        state, reason = "deferred", "a calibration check is inconclusive"
    return {
        "state": state,
        "reason": reason,
        "look": int(max(looks, key=int)),
        "checks": verdicts,
        "final_gameweeks": g,
    }
