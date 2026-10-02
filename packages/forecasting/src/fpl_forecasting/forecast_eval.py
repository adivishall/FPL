"""Walk-forward evaluation of the probabilistic forecaster against benchmarks (§27, §57.2, §70).

For every evaluation cutoff (in order):

1. models are (re)trained every ``retrain_every`` gameweeks and at each season start, using only
   information available at that training cutoff (minutes, rates, BPS, globals, direct LightGBM,
   point baselines, minutes baseline, climatology);
2. the decomposed Monte Carlo forecast is produced for GWs t … t+H−1 with the *current* cutoff's
   point-in-time data (posterior rates and features are always up to date; only fitted model
   parameters may be up to ``retrain_every − 1`` gameweeks old);
3. benchmark predictions are produced from the same feature rows;
4. only then are realised outcomes joined (evaluation-only access, as in ``walkforward``).

Outputs, per player × target gameweek: point predictions of every forecaster, the Monte Carlo
distribution summaries, per-row CRPS and randomised PIT for the MC and climatology
distributions, minutes-model probabilities, and outcomes. Metric tables are derived from these
rows, so every reported number can be recomputed from the stored predictions.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from fpl_features.labels import realized_player_gw
from fpl_forecasting import metrics as fm
from fpl_forecasting.baselines import all_baselines
from fpl_forecasting.direct import ClimatologyDistribution, DirectConfig, DirectPointsModel
from fpl_forecasting.minutes import MinutesConfig, RateBaselineMinutes
from fpl_forecasting.pipeline import (
    TRAIN_HORIZONS,
    ForecastModels,
    forecast,
    train_forecast_models,
)
from fpl_forecasting.walkforward import Cutoff, FeatureCache, training_table
from fpl_simulation.engine import SimulationConfig
from fpl_storage.dataset import CanonicalDataset

POINT_THRESHOLDS = (2, 6, 10)


@dataclass(frozen=True)
class ForecastEvalConfig:
    horizon: int = 5
    retrain_every: int = 4
    sim: SimulationConfig = field(default_factory=lambda: SimulationConfig(n_sims=1000))
    minutes: MinutesConfig = field(default_factory=MinutesConfig)
    direct: DirectConfig = field(default_factory=DirectConfig)
    climatology_samples: int = 500
    pit_seed: int = 5


@dataclass
class ForecastEvalResult:
    rows: pd.DataFrame
    trainings: list[dict[str, Any]]
    windows: dict[str, Any]


@dataclass
class _Trained:
    at: Cutoff
    models: ForecastModels
    direct: DirectPointsModel
    baselines: list[Any]
    minutes_baseline: RateBaselineMinutes
    climatology: ClimatologyDistribution


def _train(
    cache: FeatureCache, c: Cutoff, history: Sequence[Cutoff], cfg: ForecastEvalConfig
) -> _Trained:
    models = train_forecast_models(cache, c, history, minutes_config=cfg.minutes)
    multi = training_table(cache, c, history, TRAIN_HORIZONS)
    direct = DirectPointsModel(cfg.direct)
    direct.fit(multi)
    h0 = multi[multi["horizon"] == 0]
    baselines = all_baselines()
    for b in baselines:
        b.fit(h0)
    mb = RateBaselineMinutes().fit(h0)
    clim = ClimatologyDistribution()
    clim.fit(h0)
    return _Trained(c, models, direct, baselines, mb, clim)


def _needs_retrain(prev: _Trained | None, c: Cutoff, every: int) -> bool:
    return prev is None or prev.at.season != c.season or c.gw - prev.at.gw >= every


def _gw_sum(keys: pd.DataFrame, values: np.ndarray) -> pd.Series:
    df = keys.assign(v=values)
    return df.groupby(["player_code", "target_gw"])["v"].sum()


def evaluate_forecasts(
    ds: CanonicalDataset,
    eval_cutoffs: Sequence[Cutoff],
    history_cutoffs: Sequence[Cutoff],
    cfg: ForecastEvalConfig | None = None,
    cache_root: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> ForecastEvalResult:
    cfg = cfg or ForecastEvalConfig()
    cache = FeatureCache(ds, cfg.horizon, cache_root)
    trained: _Trained | None = None
    trainings: list[dict[str, Any]] = []
    parts: list[pd.DataFrame] = []
    pit_rng = np.random.default_rng(cfg.pit_seed)
    for c in eval_cutoffs:
        if _needs_retrain(trained, c, cfg.retrain_every):
            trained = _train(cache, c, history_cutoffs, cfg)
            trainings.append(
                {
                    "season": c.season,
                    "gw": c.gw,
                    "cutoff": c.cutoff.isoformat(),
                    "training_rows": trained.models.training_rows,
                    "globals": trained.models.globals,
                    "saves_elasticity": trained.models.saves_elasticity,
                    "dc_elasticity": trained.models.dc_elasticity,
                }
            )
        assert trained is not None
        fc = forecast(cache, c, trained.models, cfg.sim)
        frame = cache.get(c).frame
        keys = frame[["player_code", "target_gw"]]

        # ---- benchmark predictions per player × target GW (fixture rows summed)
        bench: dict[str, pd.Series] = {}
        for b in trained.baselines:
            bench[f"pred_{b.name}"] = _gw_sum(keys, b.predict(frame).to_numpy(float))
        bench["pred_direct_lgbm"] = _gw_sum(keys, trained.direct.predict(frame).to_numpy(float))
        mb = trained.minutes_baseline.predict(frame)
        bench["base_p_start"] = 1.0 - np.exp(
            _gw_sum(keys, np.log1p(-np.clip(mb.p_start, 0, 0.999999)))
        )
        bench["base_exp_minutes"] = _gw_sum(keys, mb.expected_minutes())
        clim = trained.climatology.samples(frame, cfg.climatology_samples)  # [n, rows]
        clim_gw = (
            pd.DataFrame(clim.T)
            .assign(
                player_code=keys["player_code"].to_numpy(), target_gw=keys["target_gw"].to_numpy()
            )
            .groupby(["player_code", "target_gw"])
            .sum()
        )
        meta = frame.groupby(["player_code", "target_gw"]).agg(
            position=("position", "first"),
            start_rate_long=("start_rate_long", "first"),
            n_fixtures=("fixture_id", "count"),
        )
        out = meta.join(pd.DataFrame(bench)).reset_index()

        # ---- Monte Carlo distribution
        s = fc.summary.rename(columns={"gw": "target_gw"})
        out = out.merge(s, on=["player_code", "target_gw"], how="left")
        sim = fc.simulation
        pi = np.searchsorted(sim.player_codes, out["player_code"].to_numpy())
        gi = np.searchsorted(sim.gameweeks, out["target_gw"].to_numpy())
        mc_samples = sim.points[:, pi, gi].T.astype(float)  # [rows, S]
        clim_samples = clim_gw.loc[
            list(zip(out["player_code"], out["target_gw"], strict=True))
        ].to_numpy(float)  # [rows, n]

        # ---- evaluation-only: realised outcomes joined after all predictions exist
        act = realized_player_gw(ds, c.season, sorted(out["target_gw"].unique()))
        out = out.merge(
            act[["gw", "player_code", "points", "minutes", "starts"]].rename(
                columns={"gw": "target_gw", "minutes": "act_minutes", "starts": "act_starts"}
            ),
            on=["player_code", "target_gw"],
            how="left",
        )
        out[["points", "act_minutes", "act_starts"]] = out[
            ["points", "act_minutes", "act_starts"]
        ].fillna(0)
        y = out["points"].to_numpy(float)
        out["crps_mc"] = fm.crps_rows(mc_samples, y)
        out["crps_climatology"] = fm.crps_rows(clim_samples, y)
        out["pit_mc"] = fm.pit_values(mc_samples, y, pit_rng)
        out["pit_climatology"] = fm.pit_values(clim_samples, y, pit_rng)
        for k in POINT_THRESHOLDS:
            out[f"clim_prob_{k}_plus"] = (clim_samples >= k).mean(axis=1)
        out["clim_mean"] = clim_samples.mean(axis=1)
        out["season"] = c.season
        out["decision_gw"] = c.gw
        out["trained_at_gw"] = trained.at.gw
        out["prediction_run_id"] = fc.run_id
        parts.append(out)
        if progress:
            progress(f"{c.season} GW{c.gw}: {len(out)} rows")
    rows = pd.concat(parts, ignore_index=True)
    rows["horizon"] = rows["target_gw"] - rows["decision_gw"]
    rows["regular"] = rows["start_rate_long"].fillna(0) >= 0.5
    rows["pred_mc_mean"] = rows["mean"]
    windows = {
        "eval": [f"{c.season} GW{c.gw}" for c in eval_cutoffs],
        "n_eval_cutoffs": len(eval_cutoffs),
        "horizon": cfg.horizon,
        "retrain_every": cfg.retrain_every,
        "n_sims": cfg.sim.n_sims,
        "simulation_seed": cfg.sim.seed,
        "data_snapshot_id": ds.snapshot_id,
    }
    return ForecastEvalResult(rows=rows, trainings=trainings, windows=windows)


POINT_FORECASTERS = (
    "mc_mean",
    "direct_lgbm",
    "position_mean",
    "recent_form",
    "ppg_availability",
    "fpl_style",
)


def point_metrics(rows: pd.DataFrame) -> pd.DataFrame:
    met = []
    for (pop, h), g in _populations(rows):
        for f in POINT_FORECASTERS:
            p = g[f"pred_{f}"].to_numpy(float)
            yv = g["points"].to_numpy(float)
            met.append(
                {
                    "population": pop,
                    "horizon": h,
                    "forecaster": f,
                    "n": len(g),
                    "rmse": fm.rmse(p, yv),
                    "mae": fm.mae(p, yv),
                    "bias": fm.bias(p, yv),
                    "spearman": float(pd.Series(p).corr(pd.Series(yv), method="spearman")),
                }
            )
    return pd.DataFrame(met)


def distribution_metrics(rows: pd.DataFrame) -> pd.DataFrame:
    met = []
    for (pop, h), g in _populations(rows):
        yv = g["points"].to_numpy(float)
        for model, prefix in (("mc", ""), ("climatology", "clim_")):
            rec: dict[str, Any] = {"population": pop, "horizon": h, "model": model, "n": len(g)}
            rec["crps"] = float(g[f"crps_{model}"].mean())
            for k in POINT_THRESHOLDS:
                pk = g[f"{prefix}prob_{k}_plus"].to_numpy(float)
                yk = (yv >= k).astype(float)
                rec[f"brier_{k}"] = fm.brier(pk, yk)
                rec[f"log_loss_{k}"] = fm.log_loss(np.clip(pk, 1e-4, 1 - 1e-4), yk)
                rec[f"ece_{k}"] = fm.ece(pk, yk)
            if model == "mc":
                rec["coverage_80"] = fm.coverage(g["p10"], g["p90"], yv)
                rec["coverage_50"] = fm.coverage(g["p25"], g["p75"], yv)
            pit = g[f"pit_{model}"].to_numpy(float)
            # central coverage via the randomised PIT: exactly 0.80 / 0.50 under calibration,
            # also for discrete outcomes (quantile intervals are not — atoms at the endpoints)
            rec["pit_coverage_80"] = float(np.mean((pit >= 0.1) & (pit <= 0.9)))
            rec["pit_coverage_50"] = float(np.mean((pit >= 0.25) & (pit <= 0.75)))
            hist = np.histogram(pit, bins=10, range=(0, 1))[0] / max(len(pit), 1)
            rec["pit_max_dev"] = float(np.max(np.abs(hist - 0.1)))
            met.append(rec)
    return pd.DataFrame(met)


def minutes_metrics(rows: pd.DataFrame) -> pd.DataFrame:
    met = []
    for (pop, h), g in _populations(rows):
        started = (g["act_starts"] > 0).to_numpy(float)
        played = (g["act_minutes"] > 0).to_numpy(float)
        am = g["act_minutes"].to_numpy(float)
        for model, ps, em in (
            ("minutes_model", g["prob_start"], g["expected_minutes"]),
            ("rate_baseline", g["base_p_start"], g["base_exp_minutes"]),
        ):
            p = np.clip(ps.to_numpy(float), 1e-4, 1 - 1e-4)
            rec = {
                "population": pop,
                "horizon": h,
                "model": model,
                "n": len(g),
                "brier_start": fm.brier(p, started),
                "log_loss_start": fm.log_loss(p, started),
                "ece_start": fm.ece(p, started),
                "minutes_mae": fm.mae(em.to_numpy(float), am),
                "minutes_rmse": fm.rmse(em.to_numpy(float), am),
            }
            if model == "minutes_model":
                pp = np.clip(g["prob_play"].to_numpy(float), 1e-4, 1 - 1e-4)
                rec["brier_play"] = fm.brier(pp, played)
            met.append(rec)
    return pd.DataFrame(met)


def _populations(rows: pd.DataFrame):  # type: ignore[no-untyped-def]
    for h, g in rows.groupby("horizon"):
        yield ("all", int(h)), g
        yield ("regulars", int(h)), g[g["regular"]]


def paired_cutoff_bootstrap(
    rows: pd.DataFrame,
    a: str,
    b: str,
    metric: Literal["rmse", "mean"] = "rmse",
    n_boot: int = 2000,
    seed: int = 0,
) -> dict[str, float]:
    """Difference metric(a) − metric(b) with a bootstrap CI that resamples whole decision
    cutoffs (rows within a cutoff are dependent: same models, same gameweek shocks).

    ``rmse``: columns are predictions, compared with ``points``; ``mean``: columns are per-row
    losses (e.g. CRPS). Works on per-cutoff sufficient statistics, so it is fast.
    """
    g = rows.groupby(["season", "decision_gw"])
    if metric == "rmse":
        y = rows["points"].to_numpy(float)
        sa = pd.Series((rows[a].to_numpy(float) - y) ** 2, index=rows.index)
        sb = pd.Series((rows[b].to_numpy(float) - y) ** 2, index=rows.index)
    else:
        sa, sb = rows[a].astype(float), rows[b].astype(float)
    ssa = sa.groupby([rows["season"], rows["decision_gw"]]).sum().to_numpy()
    ssb = sb.groupby([rows["season"], rows["decision_gw"]]).sum().to_numpy()
    n = g.size().to_numpy().astype(float)

    def stat(wts: np.ndarray) -> float:
        ma, mb = (wts @ ssa) / (wts @ n), (wts @ ssb) / (wts @ n)
        return float(np.sqrt(ma) - np.sqrt(mb)) if metric == "rmse" else float(ma - mb)

    k = len(n)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, (n_boot, k))
    counts = np.zeros((n_boot, k))
    np.add.at(counts, (np.repeat(np.arange(n_boot), k), idx.ravel()), 1.0)
    boots = np.array([stat(c) for c in counts])
    return {
        "difference": stat(np.ones(k)),
        "ci_low": float(np.quantile(boots, 0.025)),
        "ci_high": float(np.quantile(boots, 0.975)),
        "n_cutoffs": k,
    }


BASELINES = ("position_mean", "recent_form", "ppg_availability", "fpl_style")


def gate_metrics(pm: pd.DataFrame, dm: pd.DataFrame, mm: pd.DataFrame) -> dict[str, float]:
    """Flatten evaluation tables into the metric names used by ``config/models`` gates."""
    out: dict[str, float] = {}
    for pop in ("all", "regulars"):
        p = pm[(pm["population"] == pop) & (pm["horizon"] == 0)].set_index("forecaster")
        out[f"rmse_{pop}_h0"] = float(p.loc["mc_mean", "rmse"])
        out[f"rmse_{pop}_h0_direct_lgbm"] = float(p.loc["direct_lgbm", "rmse"])
        out[f"rmse_{pop}_h0_best_baseline"] = float(p.loc[list(BASELINES), "rmse"].min())
        out[f"spearman_{pop}_h0"] = float(p.loc["mc_mean", "spearman"])
        out[f"spearman_{pop}_h0_best_baseline"] = float(p.loc[list(BASELINES), "spearman"].max())
        d = dm[(dm["population"] == pop) & (dm["horizon"] == 0)].set_index("model")
        out[f"crps_{pop}_h0"] = float(d.loc["mc", "crps"])
        out[f"crps_{pop}_h0_climatology"] = float(d.loc["climatology", "crps"])
        out[f"coverage_80_{pop}_h0"] = float(d.loc["mc", "coverage_80"])
        out[f"pit_coverage_80_{pop}_h0"] = float(d.loc["mc", "pit_coverage_80"])
        out[f"pit_coverage_50_{pop}_h0"] = float(d.loc["mc", "pit_coverage_50"])
        out[f"ece_6_{pop}_h0"] = float(d.loc["mc", "ece_6"])
        m = mm[(mm["population"] == pop) & (mm["horizon"] == 0)].set_index("model")
        for k in ("brier_start", "log_loss_start", "ece_start", "minutes_mae"):
            out[f"{k}_{pop}_h0"] = float(m.loc["minutes_model", k])
            out[f"{k}_{pop}_h0_rate_baseline"] = float(m.loc["rate_baseline", k])
    return out
