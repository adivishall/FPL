"""Walk-forward harness causality and baseline behaviour (§27, §57.2, §70.1)."""

from __future__ import annotations

import numpy as np

from fpl_forecasting.baselines import all_baselines
from fpl_forecasting.walkforward import (
    FeatureCache,
    cutoffs,
    evaluate_point_forecasters,
    summarize,
    training_table,
)
from tests.fixtures_util import fixture_dataset

DS = fixture_dataset("2024-25", "2025-26")


def test_training_table_only_contains_outcomes_known_at_cutoff() -> None:
    hist = cutoffs(DS, ["2024-25", "2025-26"])
    target = next(c for c in hist if c.season == "2025-26" and c.gw == 4)
    cache = FeatureCache(DS, horizon=1)
    tt = training_table(cache, target, hist)
    assert len(tt) > 0
    assert (tt["kickoff_at"] < target.cutoff).all()
    assert tt["points"].notna().all()
    assert not ((tt["season"] == "2025-26") & (tt["target_gw"] >= 4)).any()


def test_baselines_evaluate_on_real_excerpt() -> None:
    hist = cutoffs(DS, ["2024-25", "2025-26"])
    ev = [c for c in hist if c.season == "2025-26" and c.gw in (3, 4)]
    res = evaluate_point_forecasters(DS, all_baselines(), ev, hist, horizon=2)
    pred_cols = [c for c in res.predictions if c.startswith("pred_")]
    assert len(pred_cols) == 4
    assert np.isfinite(res.predictions[pred_cols].to_numpy()).all()
    # the naive form baseline can be negative (recent card/own-goal deductions); others are not
    others = [c for c in pred_cols if c != "pred_recent_form"]
    assert (res.predictions[others] >= 0).all().all()
    s = summarize(res.metrics)
    assert set(s["forecaster"]) == {"position_mean", "recent_form", "ppg_availability", "fpl_style"}
    form = s[(s.population == "all") & (s.horizon == 0)].set_index("forecaster")
    # informed baselines must rank players better than the constant-by-position baseline
    assert form.loc["recent_form", "spearman"] > form.loc["position_mean", "spearman"]
    assert res.windows["n_eval_cutoffs"] == 2
