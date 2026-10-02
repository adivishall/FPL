"""Team strength model: recovery of known ratings, PIT behaviour, priors (§14, §36 flagship)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fpl_forecasting.team_strength import TeamStrengthConfig, fit_team_strength

T0 = pd.Timestamp("2025-08-01T00:00:00Z")


def synthetic_league(n_teams: int = 12, rounds: int = 6, seed: int = 0):
    """Double round-robin repeated; goals ~ Poisson(exp(c + h + att_i − def_j))."""
    rng = np.random.default_rng(seed)
    att = rng.normal(0, 0.3, n_teams)
    att -= att.mean()
    dfn = rng.normal(0, 0.3, n_teams)
    dfn -= dfn.mean()
    c, h = np.log(1.3), 0.25
    rows, fid, day = [], 0, 0
    for _ in range(rounds):
        for i in range(n_teams):
            for j in range(n_teams):
                if i == j:
                    continue
                fid += 1
                day += 1
                ko = T0 + pd.Timedelta(hours=8 * day)
                hg = rng.poisson(np.exp(c + h + att[i] - dfn[j]))
                ag = rng.poisson(np.exp(c + att[j] - dfn[i]))
                for home, team, opp, gf, ga in ((True, i, j, hg, ag), (False, j, i, ag, hg)):
                    rows.append(
                        {
                            "season": "2025-26",
                            "fixture_id": fid,
                            "team_code": team + 1,
                            "opponent_team_code": opp + 1,
                            "was_home": home,
                            "kickoff_at": ko,
                            "goals_for": gf,
                            "goals_against": ga,
                            "xg_for": np.nan,
                            "xg_against": np.nan,
                        }
                    )
    return pd.DataFrame(rows), att, dfn, h


def test_recovers_known_ratings_and_home_advantage() -> None:
    tm, att, dfn, h = synthetic_league()
    cut = tm["kickoff_at"].max() + pd.Timedelta(days=1)
    cfg = TeamStrengthConfig(
        half_life_days=10_000,
        window_days=10_000,
        goals_weight=1.0,
        prior_sd=1.0,
        home_advantage_sd=1.0,
    )
    ts = fit_team_strength(tm, cut, cfg)
    assert np.corrcoef(ts.attack, att)[0, 1] > 0.9
    assert np.corrcoef(ts.defence, dfn)[0, 1] > 0.9
    assert abs(ts.home_advantage - h) < 0.1
    assert (ts.attack_sd > 0).all() and (ts.attack_sd < 0.12).all()  # identified contrasts
    assert abs(ts.attack.sum()) < 1e-3 and abs(ts.defence.sum()) < 1e-3


def test_fit_ignores_matches_after_cutoff() -> None:
    tm, *_ = synthetic_league(rounds=2)
    cut = tm["kickoff_at"].quantile(0.5)
    past = fit_team_strength(tm[tm["kickoff_at"] <= cut], cut)
    full = fit_team_strength(tm, cut)  # future rows supplied but must be ignored
    assert past.n_matches == full.n_matches
    np.testing.assert_allclose(past.attack, full.attack)


def test_promoted_teams_get_prior_and_more_uncertainty() -> None:
    tm, *_ = synthetic_league(rounds=2)
    cut = tm["kickoff_at"].max() + pd.Timedelta(days=1)
    cfg = TeamStrengthConfig()
    ts = fit_team_strength(tm, cut, cfg, current_teams=[*range(1, 13), 99])
    assert 99 in ts.promoted
    a, d, asd, _ = ts.rating(99)
    others = [ts.rating(t)[0] for t in range(1, 13)]
    # With sum-to-zero identification the promoted prior acts as an offset vs the league.
    assert a - np.mean(others) == pytest.approx(cfg.promoted_attack_prior, abs=0.05)
    assert d - np.mean([ts.rating(t)[1] for t in range(1, 13)]) < 0
    assert asd > np.median(ts.attack_sd)
    unseen_a, *_ = ts.rating(12345)  # never fitted at all → raw promoted prior
    assert unseen_a == cfg.promoted_attack_prior
    mu_vs_promoted, _ = ts.expected_goals(1, 99)
    mu_vs_avg, _ = ts.expected_goals(1, 2)
    assert mu_vs_promoted > 0 and mu_vs_avg > 0
    assert ts.log_mu_sd(1, 99) > ts.log_mu_sd(1, 2)


def test_recency_weighting_tracks_a_regime_change() -> None:
    """Team 1's true attack jumps from 0 to +0.8 halfway through; a short half-life should end
    closer to the new level than a very long one."""
    rng = np.random.default_rng(5)
    n, rows, fid = 10, [], 0
    days = 360
    for day in range(days):
        if day % 2:
            continue
        i, j = rng.choice(n, 2, replace=False)
        fid += 1
        ko = T0 + pd.Timedelta(days=day)
        att = np.zeros(n)
        if day >= days // 2:
            att[0] = 0.8
        for _ in range(3):  # three matches per matchday
            hg = rng.poisson(np.exp(np.log(1.3) + 0.2 + att[i]))
            ag = rng.poisson(np.exp(np.log(1.3) + att[j]))
            fid += 1
            for home, team, opp, gf, ga in ((True, i, j, hg, ag), (False, j, i, ag, hg)):
                rows.append(
                    {
                        "season": "2025-26",
                        "fixture_id": fid,
                        "team_code": team + 1,
                        "opponent_team_code": opp + 1,
                        "was_home": home,
                        "kickoff_at": ko,
                        "goals_for": gf,
                        "goals_against": ga,
                        "xg_for": np.nan,
                        "xg_against": np.nan,
                    }
                )
            i, j = rng.choice(n, 2, replace=False)
    tm = pd.DataFrame(rows)
    cut = T0 + pd.Timedelta(days=days + 1)
    base = {"window_days": 5000, "prior_sd": 1.0, "goals_weight": 1.0}
    slow = fit_team_strength(tm, cut, TeamStrengthConfig(half_life_days=2000, **base))
    fast = fit_team_strength(tm, cut, TeamStrengthConfig(half_life_days=45, **base))
    k = slow.teams.index(1)
    rel = lambda ts: ts.attack[k] - np.delete(ts.attack, k).mean()  # noqa: E731
    assert abs(rel(fast) - 0.8) < abs(rel(slow) - 0.8)
