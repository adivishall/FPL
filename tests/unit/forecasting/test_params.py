"""BPS map, globals and opponent elasticity estimators recover known parameters (ADR-0006)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from fpl_forecasting.params import estimate_globals, fit_bps_model, fit_elasticity


def test_fit_elasticity_recovers_known_beta() -> None:
    rng = np.random.default_rng(0)
    n = 5000
    base = rng.uniform(1.0, 4.0, n)
    minutes = rng.choice([45.0, 90.0], n)
    ratio = np.exp(rng.normal(0, 0.3, n))
    y = rng.poisson(base * minutes / 90 * ratio**0.7)
    assert abs(fit_elasticity(y, base, minutes, ratio) - 0.7) < 0.08


def test_fit_elasticity_refuses_tiny_samples() -> None:
    assert fit_elasticity(np.ones(10), np.ones(10), np.full(10, 90.0), np.ones(10)) == 0.0


def test_estimate_globals() -> None:
    hist = pd.DataFrame({"goals": [10, 30], "assists": [20, 10], "own_goals": [1, 1]})
    tm = pd.DataFrame({"goals_for": [1.0, 2.0, 1.5]})
    g = estimate_globals(hist, tm)
    assert np.isclose(g["p_assisted"], 30 / 40)
    assert np.isclose(g["own_goal_rate"], 2 / 42)
    assert np.isclose(g["league_mu"], 1.5)


def test_bps_model_recovers_linear_map() -> None:
    rng = np.random.default_rng(1)
    n = 20000
    pos = rng.choice(["GK", "DEF", "MID", "FWD"], n)
    codes = np.arange(n) % 400
    positions = pd.Series(pos[:400], index=np.arange(400))
    p = positions.reindex(codes).to_numpy()
    minutes = rng.choice([30, 70, 90], n)
    goals = rng.poisson(0.2, n)
    assists = rng.poisson(0.15, n)
    cs = rng.binomial(1, 0.3, n)
    gc = rng.poisson(1.2, n) * (1 - cs)
    saves = np.where(p == "GK", rng.poisson(3, n), 0)
    cbi = rng.poisson(4, n).astype(float)
    goal_coef = {"GK": 12.0, "DEF": 12.0, "MID": 18.0, "FWD": 24.0}
    bps = (
        3.0 * (minutes >= 60)
        + np.vectorize(goal_coef.get)(p) * goals
        + 9.0 * assists
        + np.where(np.isin(p, ["GK", "DEF"]), 12.0, 0.0) * cs
        + 2.0 * saves
        + 0.5 * np.where(p == "GK", 0.0, cbi)
        + rng.normal(0, 2.0, n)
    )
    hist = pd.DataFrame(
        {
            "player_code": codes,
            "season": "2025-26",
            "minutes": minutes,
            "goals": goals,
            "assists": assists,
            "clean_sheets": cs,
            "goals_conceded": gc,
            "saves": saves,
            "cbi": cbi,
            "tackles": 0.0,
            "recoveries": 0.0,
            "yellow_cards": 0,
            "red_cards": 0,
            "own_goals": 0,
            "penalties_missed": 0,
            "penalties_saved": 0,
            "bps": bps,
        }
    )
    model, offsets = fit_bps_model(hist, positions, "2025-26")
    assert np.allclose(model.goal[1:], [12, 18, 24], atol=0.5)
    assert abs(model.assist - 9.0) < 0.3
    assert abs(model.saves - 2.0) < 0.2
    assert np.allclose(model.dc_action[1:], 0.5, atol=0.05)
    assert np.allclose(model.sigma, 2.0, atol=0.2)
    # no player effect was simulated, so shrunk offsets are small
    assert offsets.abs().max() < 1.0
