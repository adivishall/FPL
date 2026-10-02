"""Monte Carlo engine: analytic checks, scoring consistency, common random numbers (§12, §59)."""

from __future__ import annotations

import numpy as np
import pytest

from fpl_domain.forecast import FixtureParams, PlayerFixtureParams
from fpl_domain.rules import load_ruleset
from fpl_simulation.engine import SimulationConfig, simulate
from tests.sim_util import glob, toy_fixture, toy_players

RS = load_ruleset("2026-27")
N = 20_000


@pytest.fixture(scope="module")
def big():
    return simulate(toy_players(), toy_fixture(), glob(), RS, SimulationConfig(n_sims=N, seed=7))


def se(p: float) -> float:
    return np.sqrt(p * (1 - p) / N)


def test_team_goals_match_expected_goals(big) -> None:
    home = big.events["goals"][big.player_codes // 100 == 1].sum()
    away = big.events["goals"][big.player_codes // 100 == 2].sum()
    assert home == pytest.approx(1.8, abs=4 * np.sqrt(1.8 / N))
    assert away == pytest.approx(1.0, abs=4 * np.sqrt(1.0 / N))


def test_clean_sheet_probability_is_poisson_zero(big) -> None:
    gk = big.index_of(100)
    assert big.events["clean_sheets"][gk, 0] == pytest.approx(np.exp(-1.0), abs=4 * se(0.37))


def test_saves_assists_bonus_and_minutes(big) -> None:
    gk = big.index_of(100)
    assert big.events["saves"][gk, 0] == pytest.approx(3.0 / 1.4, rel=0.03)
    total_goals = big.events["goals"].sum()
    assert big.events["assists"].sum() / total_goals == pytest.approx(0.75, abs=0.02)
    assert big.events["bonus"].sum() == pytest.approx(6.0)
    assert set(np.unique(big.minutes)) <= {89, 90}


def test_points_equal_component_sum_and_summary_contract(big) -> None:
    comp_total = sum(v for v in big.components.values())
    np.testing.assert_allclose(comp_total, big.points.mean(axis=0), rtol=1e-4, atol=1e-4)
    s = big.summary()
    for key in (
        "mean",
        "p10",
        "p25",
        "p50",
        "p75",
        "p90",
        "prob_2_plus",
        "prob_6_plus",
        "prob_10_plus",
        "prob_15_plus",
        "expected_minutes",
    ):
        assert s[key].shape == (22, 1)
    assert (s["p10"] <= s["p50"]).all() and (s["p50"] <= s["p90"]).all()
    assert (s["prob_play"] == 1.0).all()  # everyone plays 89–90 minutes
    assert (s["prob_2_plus"] < 1.0).any()  # deductions (cards, goals conceded) still apply


def test_minutes_threshold_affects_clean_sheet() -> None:
    r = simulate(
        toy_players(full_minutes=False),
        toy_fixture(),
        glob(),
        RS,
        SimulationConfig(n_sims=4000, seed=3),
    )
    assert set(np.unique(r.minutes)) <= set(range(60, 75))
    assert r.events["clean_sheets"].max() > 0  # 60–74 minutes still eligible


def test_exactly_one_starting_goalkeeper_per_team() -> None:
    p = toy_players()
    gks = np.flatnonzero(p.position == 0)
    # add a second home GK with 50% start probability
    extra = p.take([gks[0]]).replace(player_code=np.array([150]), p_start=np.array([0.5]))
    base = p.replace(p_start=np.where(p.player_code == 100, 0.5, p.p_start))
    both = PlayerFixtureParams(
        **{k: np.concatenate([getattr(base, k), getattr(extra, k)]) for k in base.__dict__}
    )
    r = simulate(both, toy_fixture(), glob(), RS, SimulationConfig(n_sims=3000, seed=4))
    gk1, gk2 = r.index_of(100), r.index_of(150)
    started = (r.minutes[:, gk1, 0] > 0).astype(int) + (r.minutes[:, gk2, 0] > 0).astype(int)
    assert (started == 1).all()


def test_double_gameweek_sums_fixtures() -> None:
    p1, p2 = toy_players(1, 1), toy_players(2, 1, home=2, away=1)
    both = PlayerFixtureParams(
        **{k: np.concatenate([getattr(p1, k), getattr(p2, k)]) for k in p1.__dict__}
    )
    fx = FixtureParams(
        fixture_id=np.array([1, 2]),
        gameweek=np.array([1, 1]),
        home_team=np.array([1, 2]),
        away_team=np.array([2, 1]),
        mu_home=np.array([1.5, 1.5]),
        mu_away=np.array([1.5, 1.5]),
    )
    r = simulate(both, fx, glob(), RS, SimulationConfig(n_sims=2000, seed=5))
    assert set(np.unique(r.minutes)) <= set(range(178, 181))


def test_determinism_and_seed_sensitivity() -> None:
    a = simulate(toy_players(), toy_fixture(), glob(), RS, SimulationConfig(n_sims=500, seed=1))
    b = simulate(toy_players(), toy_fixture(), glob(), RS, SimulationConfig(n_sims=500, seed=1))
    c = simulate(toy_players(), toy_fixture(), glob(), RS, SimulationConfig(n_sims=500, seed=2))
    np.testing.assert_array_equal(a.points, b.points)
    assert not np.array_equal(a.points, c.points)


def test_common_random_numbers_isolate_a_scenario_change() -> None:
    """§77.2: scenario simulations only vary the intended stochastic inputs."""
    p1, p2 = toy_players(1, 1, 1, 2), toy_players(2, 1, 3, 4)
    both = PlayerFixtureParams(
        **{k: np.concatenate([getattr(p1, k), getattr(p2, k)]) for k in p1.__dict__}
    )
    fx = FixtureParams(
        fixture_id=np.array([1, 2]),
        gameweek=np.array([1, 1]),
        home_team=np.array([1, 3]),
        away_team=np.array([2, 4]),
        mu_home=np.array([1.5, 1.2]),
        mu_away=np.array([1.1, 1.3]),
    )
    cfg = SimulationConfig(n_sims=800, seed=11)
    base = simulate(both, fx, glob(), RS, cfg)
    # scenario: the home forward of fixture 1 is benched (p_start → 0)
    changed = both.replace(p_start=np.where(both.player_code == 109, 0.0, both.p_start))
    alt = simulate(changed, fx, glob(), RS, cfg)
    other_fixture = np.isin(base.player_codes, p2.player_code)
    np.testing.assert_array_equal(base.points[:, other_fixture], alt.points[:, other_fixture])
    same_fixture_others = np.isin(base.player_codes, p1.player_code) & (base.player_codes != 109)
    np.testing.assert_array_equal(
        base.minutes[:, same_fixture_others], alt.minutes[:, same_fixture_others]
    )
    assert (alt.minutes[:, alt.index_of(109)] == 0).all()
    # team-mates score more goals when the forward is out (share redistributed on the pitch)
    mates = np.isin(alt.player_codes, [106, 107, 108, 110])
    assert alt.events["goals"][mates].sum() > base.events["goals"][mates].sum()


def test_sample_cap_enforced() -> None:
    with pytest.raises(ValueError, match="n_sims"):
        SimulationConfig(n_sims=50_000)


def test_invalid_parameters_rejected() -> None:
    p = toy_players()
    with pytest.raises(ValueError, match="probabilities"):
        p.replace(p_start=np.full(len(p), 1.5))
    with pytest.raises(ValueError, match="positive"):
        toy_fixture(mu_home=0.0)
