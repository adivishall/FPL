"""Forecast evaluation harness on the real excerpt: causality, completeness, metric sanity."""

from __future__ import annotations

import numpy as np

from fpl_forecasting.direct import DirectConfig
from fpl_forecasting.forecast_eval import (
    ForecastEvalConfig,
    distribution_metrics,
    evaluate_forecasts,
    minutes_metrics,
    paired_cutoff_bootstrap,
    point_metrics,
)
from fpl_forecasting.minutes import MinutesConfig
from fpl_forecasting.walkforward import cutoffs
from fpl_simulation.engine import SimulationConfig
from tests.fixtures_util import fixture_dataset

DS = fixture_dataset("2024-25", "2025-26")
CFG = ForecastEvalConfig(
    horizon=2,
    retrain_every=2,
    sim=SimulationConfig(n_sims=200),
    minutes=MinutesConfig(n_estimators=20, min_child_samples=30),
    direct=DirectConfig(n_estimators=30, min_child_samples=30),
    climatology_samples=100,
)


def test_forecast_eval_end_to_end() -> None:
    hist = cutoffs(DS, ["2024-25", "2025-26"])
    ev = [c for c in hist if c.season == "2025-26" and c.gw in (3, 4, 5)]
    res = evaluate_forecasts(DS, ev, hist, CFG)
    r = res.rows
    # retraining schedule: GW3 (season's first eval) and GW5 (two GWs later)
    assert [t["gw"] for t in res.trainings] == [3, 5]
    assert set(r.loc[r["decision_gw"] == 4, "trained_at_gw"]) == {3}
    # models trained at a cutoff never postdate the decision they serve
    assert (r["trained_at_gw"] <= r["decision_gw"]).all()
    assert not r.duplicated(["decision_gw", "player_code", "target_gw"]).any()
    pred_cols = [c for c in r.columns if c.startswith("pred_")]
    assert np.isfinite(r[pred_cols].to_numpy(float)).all()
    assert (r["crps_mc"] >= 0).all() and (r["crps_climatology"] >= 0).all()
    assert r["pit_mc"].between(0, 1).all()

    pm = point_metrics(r)
    assert set(pm["forecaster"]) >= {"mc_mean", "direct_lgbm", "position_mean"}
    dm = distribution_metrics(r)
    assert set(dm["model"]) == {"mc", "climatology"}
    assert dm["coverage_80"].dropna().between(0, 1).all()
    mm = minutes_metrics(r)
    assert set(mm["model"]) == {"minutes_model", "rate_baseline"}

    boot = paired_cutoff_bootstrap(r, "pred_mc_mean", "pred_position_mean", n_boot=50)
    assert boot["ci_low"] <= boot["difference"] <= boot["ci_high"]
    assert boot["n_cutoffs"] == 3
