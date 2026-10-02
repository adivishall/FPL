"""Empirical-Bayes event rates: recovery of known structure on synthetic data (§13, §57)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from fpl_forecasting.rates import RATES, RateConfig, fit_rate_model, sufficient_stats

FLAT = RateConfig(goal_weight=1.0, half_life_matches=1e9)


def _synthetic_history(
    n_players: int = 600, n_matches: int = 30, cv2: float = 0.25, seed: int = 0
) -> tuple[pd.DataFrame, pd.Series, np.ndarray]:
    """Midfielders with gamma-distributed true goal shares; goals ~ Poisson(θ · team xG)."""
    rng = np.random.default_rng(seed)
    m = 0.1
    shape = 1.0 / cv2
    theta = rng.gamma(shape, m / shape, n_players)
    n_matches_i = rng.integers(4, n_matches + 1, n_players)
    rows = []
    t0 = pd.Timestamp("2024-08-01", tz="UTC")
    for i in range(n_players):
        for j in range(n_matches_i[i]):
            goals = rng.poisson(theta[i] * 1.4)
            rows.append(
                {
                    "player_code": i,
                    "kickoff_at": t0 + pd.Timedelta(days=7 * j),
                    "minutes": 90,
                    "goals": goals,
                    "assists": 0,
                    "xg": 0.0,
                    "xa": 0.0,
                    "team_xg": 1.4,
                    "saves": 0,
                    "cbi": np.nan,
                    "tackles": np.nan,
                    "recoveries": np.nan,
                    "yellow_cards": 0,
                    "red_cards": 0,
                    "penalties_missed": 0,
                    "penalties_saved": 0,
                    "own_goals": 0,
                }
            )
    positions = pd.Series("MID", index=range(n_players))
    return pd.DataFrame(rows), positions, theta


def test_split_half_recovers_poisson_noise_and_between_player_variance() -> None:
    hist, positions, _ = _synthetic_history()
    stats = sufficient_stats(hist, positions, FLAT)
    model = fit_rate_model(stats, positions, pd.Series(0.0, index=positions.index), FLAT)
    k = 2  # MID
    # Counts are Poisson, so the noise scale must be ≈ 1; CV² of the true shares is 0.25.
    assert 0.8 < model.phi["goal_share"][k] < 1.2
    assert 0.15 < model.cv2["goal_share"][k] < 0.35


def test_posterior_beats_raw_rate_and_prior_against_truth() -> None:
    hist, positions, theta = _synthetic_history(seed=1)
    stats = sufficient_stats(hist, positions, FLAT)
    model = fit_rate_model(stats, positions, pd.Series(0.0, index=positions.index), FLAT)
    pos = np.full(len(stats), 2)
    post = model.posterior(stats, pos, np.zeros(len(stats)))["goal_share"].to_numpy()
    raw = (stats["goal_blend"] / stats["team_xg_exposure"]).to_numpy()
    prior = model.prior_mean("goal_share", pos, np.zeros(len(stats)))
    truth = theta[stats.index.to_numpy()]
    mse = {
        k: float(np.mean((v - truth) ** 2))
        for k, v in {"post": post, "raw": raw, "prior": prior}.items()
    }
    assert mse["post"] < 0.8 * mse["raw"]
    assert mse["post"] < 0.8 * mse["prior"]


def test_players_without_history_receive_the_prior() -> None:
    hist, positions, _ = _synthetic_history(n_players=200)
    stats = sufficient_stats(hist, positions, FLAT)
    model = fit_rate_model(stats, positions, pd.Series(0.0, index=positions.index), FLAT)
    empty = pd.DataFrame(0.0, index=[99999], columns=stats.columns)
    post = model.posterior(empty, np.array([2]), np.array([0.0]))
    prior = model.prior_mean("goal_share", np.array([2]), np.array([0.0]))
    assert np.isclose(post["goal_share"].iloc[0], prior[0])
    assert post["goal_share_exposure"].iloc[0] == 0.0


def test_recency_weighting_favours_recent_form() -> None:
    hist, positions, _ = _synthetic_history(n_players=50, n_matches=20, seed=2)
    p0 = hist[hist["player_code"] == 0].copy()
    p0["goals"] = 0
    recent = p0["kickoff_at"] >= p0["kickoff_at"].max() - pd.Timedelta(days=21)
    p0.loc[recent, "goals"] = 2
    hist = pd.concat([hist[hist["player_code"] != 0], p0])
    weighted = sufficient_stats(hist, positions, RateConfig(goal_weight=1.0, half_life_matches=4))
    flat = sufficient_stats(hist, positions, FLAT)
    r_w = weighted.loc[0, "goal_blend"] / weighted.loc[0, "team_xg_exposure"]
    r_f = flat.loc[0, "goal_blend"] / flat.loc[0, "team_xg_exposure"]
    assert r_w > 1.5 * r_f


def test_split_halves_partition_the_totals() -> None:
    hist, positions, _ = _synthetic_history(n_players=40)
    stats = sufficient_stats(hist, positions, RateConfig())
    for _, num, expo in RATES:
        for c in (num, expo):
            assert np.allclose(stats[f"{c}__a"] + stats[f"{c}__b"], stats[c])
