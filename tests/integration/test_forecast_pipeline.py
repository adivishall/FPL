"""End-to-end probabilistic forecast on the real data excerpt (§57–§59, §70.1, ADR-0006).

Covers: training only on information available at the cutoff, PIT access audit, determinism of
the full train → simulate path, provenance completeness (§59.2) and distribution sanity.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from scipy.stats import binom

from fpl_forecasting.minutes import MinutesConfig
from fpl_forecasting.pipeline import forecast, train_forecast_models
from fpl_forecasting.walkforward import FeatureCache, cutoffs
from fpl_simulation.engine import SimulationConfig
from tests.fixtures_util import fixture_dataset

DS = fixture_dataset("2024-25", "2025-26")
SMALL = MinutesConfig(n_estimators=30, min_child_samples=30)
SIM = SimulationConfig(n_sims=300)


@pytest.fixture(scope="module")
def run():  # type: ignore[no-untyped-def]
    hist = cutoffs(DS, ["2024-25", "2025-26"])
    cut = next(c for c in hist if c.season == "2025-26" and c.gw == 4)
    cache = FeatureCache(DS, horizon=2)
    models = train_forecast_models(cache, cut, hist, minutes_config=SMALL)
    return cut, cache, hist, models, forecast(cache, cut, models, SIM)


def test_models_train_only_on_pre_cutoff_outcomes(run) -> None:  # type: ignore[no-untyped-def]
    cut, _, _, models, _ = run
    assert models.cutoff == cut.cutoff
    assert models.training_rows > 0
    assert 0.3 <= models.globals["p_assisted"] <= 1.0


def test_summary_structure_and_distribution_sanity(run) -> None:  # type: ignore[no-untyped-def]
    cut, _, _, _, fc = run
    s = fc.summary
    assert set(s["gw"]) <= {4, 5}
    assert (s["horizon"] == s["gw"] - cut.gw).all()
    assert s["player_code"].is_unique is False  # multiple target gameweeks per player
    assert not s.duplicated(["player_code", "gw"]).any()
    q = s[["p10", "p25", "p50", "p75", "p90"]].to_numpy()
    assert (np.diff(q, axis=1) >= 0).all()
    assert ((s["start_probability_model"] >= 0) & (s["start_probability_model"] <= 1)).all()
    assert (s["prob_play"] >= s["prob_start"]).all()
    # Outfield starts are not constrained by the simulator, so the Monte Carlo frequency must be
    # consistent with the minutes model (exact binomial bounds, family-wise level ≈ 1e-3);
    # goalkeepers are made coherent (one per side), which can only lower their start rate.
    n, p = SIM.n_sims, s["start_probability_model"].to_numpy()
    alpha = 1e-3 / len(s)
    lo, hi = binom.ppf(alpha / 2, n, p) / n, binom.isf(alpha / 2, n, p) / n
    fc = run[4]
    pos_of = pd.Series(fc.players.position, index=fc.players.player_code)
    pos = s["player_code"].map(pos_of[~pos_of.index.duplicated()]).to_numpy()
    ps = s["prob_start"].to_numpy()
    out = pos != 0
    assert ((ps[out] >= lo[out] - 1e-9) & (ps[out] <= hi[out] + 1e-9)).all()
    assert (ps[~out] <= hi[~out] + 1e-9).all()
    comps = [c for c in s.columns if c.startswith("xp_")]
    assert np.allclose(s[comps].sum(axis=1), s["mean"], atol=1e-6)


def test_forecast_is_deterministic_and_provenance_complete(run) -> None:  # type: ignore[no-untyped-def]
    cut, cache, hist, _, fc = run
    again = forecast(cache, cut, train_forecast_models(cache, cut, hist, SMALL), SIM)
    pd.testing.assert_frame_equal(fc.summary, again.summary)
    assert fc.run_id == again.run_id
    p = fc.provenance
    for key in (
        "cutoff",
        "feature_snapshot_id",
        "feature_version",
        "data_snapshot_id",
        "model_versions",
        "ruleset_version",
        "simulation_seed",
        "n_simulations",
        "max_source_available_at",
        "prediction_run_id",
    ):
        assert key in p
    assert pd.Timestamp(p["max_source_available_at"]) <= cut.cutoff


def test_changing_seed_changes_run_id_not_expectations_much(run) -> None:  # type: ignore[no-untyped-def]
    cut, cache, _, models, fc = run
    other = forecast(cache, cut, models, SimulationConfig(n_sims=300, seed=1))
    assert other.run_id != fc.run_id
    a = fc.summary.set_index(["player_code", "gw"])["mean"]
    b = other.summary.set_index(["player_code", "gw"])["mean"].reindex(a.index)
    assert np.corrcoef(a, b)[0, 1] > 0.95
