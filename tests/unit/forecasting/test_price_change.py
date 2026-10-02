"""Price-change model: information set, label availability and probabilistic outputs (§66)."""

from __future__ import annotations

import numpy as np
import pandas as pd
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fpl_forecasting.price_change import (
    PRICE_FEATURES,
    PriceChangeModel,
    PriceModelConfig,
    TrendBaseline,
    decision_features,
    official_signal,
    training_rows,
)
from fpl_forecasting.walkforward import cutoffs
from fpl_storage.pit import PointInTimeView
from tests.fixtures_util import fixture_dataset
from tests.unit.storage.test_raw_store_and_pit import _perturb_future

DS = fixture_dataset("2024-25", "2025-26", "2026-27")


def _cut(season: str, gw: int) -> pd.Timestamp:
    return next(c.cutoff for c in cutoffs(DS, [season]) if c.gw == gw)


@settings(max_examples=6, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(gw=st.integers(3, 5), seed=st.integers(0, 1000))
def test_decision_features_ignore_future_data(gw: int, seed: int) -> None:
    cut = _cut("2025-26", gw)
    a = decision_features(PointInTimeView(DS, cut), "2025-26", gw)
    b = decision_features(PointInTimeView(_perturb_future(DS, cut, seed), cut), "2025-26", gw)
    pd.testing.assert_frame_equal(a, b)


def test_training_labels_are_visible_at_the_cutoff() -> None:
    cut = _cut("2025-26", 5)
    view = PointInTimeView(DS, cut)
    rows = training_rows(view, ["2024-25", "2025-26"])
    view.assert_no_leakage()
    assert len(rows) > 0
    cur = rows[rows["season"] == "2025-26"]
    # label needs price at GW t+1, observed at that GW's kickoff (< cutoff of GW 5)
    assert cur["decision_gw"].max() <= 3
    assert set(PRICE_FEATURES) <= set(rows.columns)


def test_features_match_hand_computation() -> None:
    cut = _cut("2025-26", 5)
    f = decision_features(PointInTimeView(DS, cut), "2025-26", 5).set_index("player_code")
    pm = DS["player_match"]
    pm = pm[(pm["season"] == "2025-26") & (pm["gw"] <= 4)].sort_values("kickoff_at")
    price = pm.groupby(["player_code", "gw"])["price"].last().astype(float).unstack()
    code = int(price.dropna().index[0])
    assert f.loc[code, "price"] == price.loc[code, 4]
    assert f.loc[code, "d_price_1"] == price.loc[code, 4] - price.loc[code, 3]
    assert f.loc[code, "d_price_season"] == price.loc[code, 4] - price.loc[code, 1]


def test_model_outputs_are_probabilities() -> None:
    cut = _cut("2025-26", 5)
    view = PointInTimeView(DS, cut)
    rows = training_rows(view, ["2024-25", "2025-26"])
    cfg = PriceModelConfig(n_estimators=20, min_child_samples=20)
    model = PriceChangeModel(cfg).fit(rows)
    p = model.predict(decision_features(view, "2025-26", 5))
    assert ((p["p_rise"] >= 0) & (p["p_fall"] >= 0)).all()
    assert (p["p_rise"] + p["p_fall"] <= 1 + 1e-9).all()


def test_trend_baseline_learns_persistence() -> None:
    rng = np.random.default_rng(0)
    n = 5000
    d1 = rng.choice([-1.0, 0.0, 1.0], n, p=[0.1, 0.8, 0.1])
    rise = rng.random(n) < np.where(d1 > 0, 0.4, 0.02)
    rows = pd.DataFrame(
        {"player_code": np.arange(n), "d_price_1": d1, "net_ratio_1": rng.normal(0, 1, n)}
    )
    rows["d_next"] = np.where(rise, 1.0, 0.0)
    b = TrendBaseline().fit(rows)
    p = b.predict(rows)
    assert p.loc[d1 > 0, "p_rise"].mean() > 0.3 > p.loc[d1 == 0, "p_rise"].mean()


def test_official_signal_is_separate_and_live_only() -> None:
    hist = official_signal(PointInTimeView(DS, _cut("2025-26", 5)), "2025-26")
    assert hist.empty
    live = official_signal(PointInTimeView(DS, _cut("2026-27", 2)), "2026-27")
    assert len(live) > 100  # the committed 2026-27 snapshot carries the official signal
    assert set(live.columns) == {
        "player_code",
        "official_price_change_percent",
        "official_captured_at",
    }
