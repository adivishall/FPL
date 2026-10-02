"""End-to-end probabilistic forecast at a decision cutoff (§10–§13, §57–§59, ADR-0006).

``train_forecast_models`` fits, using only information available at the cutoff:
  minutes models (multi-horizon, calibrated), empirical-Bayes event rates, the BPS map and player
  offsets, global event shares and opponent elasticities.
``forecast`` then builds point-in-time features, fits the team model, assembles per-row
simulation parameters (applying the live availability layer), runs the joint simulator and
returns per player × gameweek distributions with full provenance (§59.2 contract).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from fpl_domain.enums import POSITIONS
from fpl_domain.forecast import FixtureParams, GlobalParams, PlayerFixtureParams
from fpl_domain.hashing import short_id
from fpl_domain.rules import load_ruleset
from fpl_features.builder import FeatureFrame
from fpl_features.registry import FEATURE_VERSION
from fpl_forecasting.minutes import (
    AvailabilityAdjustment,
    MinutesConfig,
    MinutesModel,
    MinutesPrediction,
)
from fpl_forecasting.model_config import minutes_spec, points_spec
from fpl_forecasting.params import estimate_globals, fit_bps_model, fit_elasticity
from fpl_forecasting.rates import (
    RateConfig,
    RateModel,
    fit_rate_model,
    history_with_team_xg,
    sufficient_stats,
)
from fpl_forecasting.team_strength import TeamStrength, config_for_season, fit_team_strength
from fpl_forecasting.walkforward import Cutoff, FeatureCache, training_table
from fpl_simulation.engine import SimulationConfig, SimulationResult, simulate
from fpl_storage.pit import PointInTimeView

POS_INDEX = {p.value: i for i, p in enumerate(POSITIONS)}
TRAIN_HORIZONS = (0, 1, 2, 3, 4)  # mirrors config/models/points.yaml (training.horizons)
MODEL_VERSION = "points-decomposed-1.0.0"


@dataclass
class ForecastModels:
    cutoff: pd.Timestamp
    season: str
    minutes: MinutesModel
    rate_model: RateModel
    bps: Any  # fpl_domain.forecast.BpsModel
    bps_offsets: pd.Series
    globals: dict[str, float]
    saves_elasticity: float
    dc_elasticity: float
    availability: AvailabilityAdjustment = field(default_factory=AvailabilityAdjustment)
    training_rows: int = 0
    config_refs: dict[str, str] = field(default_factory=dict)

    @property
    def versions(self) -> dict[str, str]:
        return {
            "minutes": self.minutes.version,
            "points": MODEL_VERSION,
            "features": FEATURE_VERSION,
            "trained_at": self.cutoff.isoformat(),
            **{f"config_{k}": v for k, v in self.config_refs.items()},
        }


def train_forecast_models(
    cache: FeatureCache,
    cut: Cutoff,
    history: Sequence[Cutoff],
    minutes_config: MinutesConfig | None = None,
    rate_config: RateConfig | None = None,
) -> ForecastModels:
    spec, points_ref = points_spec()
    mspec, minutes_ref = minutes_spec()
    view = PointInTimeView(cache.ds, cut.cutoff)
    tab = training_table(cache, cut, history, spec.training.horizons)
    if tab.empty:
        raise ValueError(f"no training data before {cut}")
    minutes = MinutesModel(minutes_config or mspec.params).fit(tab)

    hist = history_with_team_xg(view)
    pool = view.player_pool(cut.season, cut.gw)
    positions = pool.set_index("player_code")["position"]
    log_price = np.log(pool.set_index("player_code")["price"].astype(float))
    rcfg = rate_config or spec.rates
    stats = sufficient_stats(hist[hist["player_code"].isin(positions.index)], positions, rcfg)
    rate_model = fit_rate_model(stats, positions, log_price, rcfg)

    seasons = sorted(hist["season"].unique())
    recent = hist[hist["season"].isin(seasons[-spec.training.bps_seasons :])]
    bps, offsets = fit_bps_model(recent, positions, cut.season)
    glob = estimate_globals(recent, view.team_match())

    h0 = tab[tab["horizon"] == 0]
    league = glob["league_mu"]
    gk = h0[h0["pos_GK"].astype(bool) & (h0["minutes"] > 0)]
    saves_el = fit_elasticity(
        gk["saves"].to_numpy(float),
        gk["saves_p90"].to_numpy(float),
        gk["minutes"].to_numpy(float),
        (gk["opp_xg_for_ewm"] / league).to_numpy(float),
    )
    dcr = h0[h0["dc"].notna() & (h0["minutes"] > 0) & ~h0["pos_GK"].astype(bool)]
    dc_el = fit_elasticity(
        dcr["dc"].to_numpy(float),
        dcr["dc_actions_p90"].to_numpy(float),
        dcr["minutes"].to_numpy(float),
        (dcr["opp_xg_for_ewm"] / league).to_numpy(float),
    )
    return ForecastModels(
        cutoff=cut.cutoff,
        season=cut.season,
        minutes=minutes,
        rate_model=rate_model,
        bps=bps,
        bps_offsets=offsets,
        globals=glob,
        saves_elasticity=saves_el,
        dc_elasticity=dc_el,
        availability=spec.availability,
        training_rows=len(tab),
        config_refs={"points": points_ref.ref, "minutes": minutes_ref.ref},
    )


@dataclass
class Forecast:
    summary: pd.DataFrame  # one row per player × target gameweek
    simulation: SimulationResult
    players: PlayerFixtureParams
    fixtures: FixtureParams
    team_strength: TeamStrength
    minutes: MinutesPrediction
    provenance: dict[str, Any]

    @property
    def run_id(self) -> str:
        return str(self.provenance["prediction_run_id"])


def build_params(
    ff: FeatureFrame,
    models: ForecastModels,
    view: PointInTimeView,
    team: TeamStrength,
) -> tuple[PlayerFixtureParams, FixtureParams, MinutesPrediction]:
    frame = ff.frame
    mp = models.minutes.predict(frame)
    snap = view.latest_snapshot(ff.season)
    status_code = (
        frame["player_code"].map(snap.set_index("player_code")["status"]).to_numpy(object)
        if not snap.empty
        else None
    )
    mult = models.availability.multiplier(
        frame["status_flag"].to_numpy(float),
        frame["chance_of_playing"].to_numpy(float),
        frame["horizon"].to_numpy(float),
        status_code,
    )
    p_start = mp.p_start * mult
    p_sub = mp.p_sub * mult
    hist = history_with_team_xg(view)
    codes = frame["player_code"].unique()
    positions = frame.drop_duplicates("player_code").set_index("player_code")["position"]
    stats = sufficient_stats(
        hist[hist["player_code"].isin(codes)], positions, models.rate_model.config
    )
    stats = stats.reindex(codes).fillna(0.0)
    pos_idx = positions.reindex(codes).map(POS_INDEX).to_numpy(int)
    price = frame.drop_duplicates("player_code").set_index("player_code")["price"].reindex(codes)
    post = models.rate_model.posterior(stats, pos_idx, np.log(price.astype(float).to_numpy()))
    rows = post.reindex(frame["player_code"]).reset_index(drop=True)
    pos_rows = frame["position"].map(POS_INDEX).to_numpy(int)
    # Defensive actions only exist under rulesets that score them; dispersion fixed at 4.
    dc_p90 = rows["dc_p90"].to_numpy(float)
    players = PlayerFixtureParams(
        player_code=frame["player_code"].to_numpy(np.int64),
        fixture_id=frame["fixture_id"].to_numpy(np.int64),
        gameweek=frame["target_gw"].to_numpy(np.int64),
        team_code=frame["team_code"].to_numpy(np.int64),
        is_home=frame["is_home"].to_numpy(bool),
        position=pos_rows.astype(np.int64),
        p_start=np.clip(p_start, 0, 1),
        p_sub=np.clip(p_sub, 0, 1),
        start_buckets=mp.start_buckets,
        sub_buckets=mp.sub_buckets,
        goal_share=rows["goal_share"].to_numpy(float),
        assist_share=rows["assist_share"].to_numpy(float),
        og_propensity=rows["og_p90"].to_numpy(float) + 1e-4,
        saves_p90=np.where(pos_rows == 0, rows["saves_p90"].to_numpy(float), 0.0),
        dc_p90=np.where(pos_rows == 0, 0.0, dc_p90),
        dc_dispersion=np.full(len(frame), 4.0),
        yellow_p90=rows["yellow_p90"].to_numpy(float),
        red_p90=rows["red_p90"].to_numpy(float),
        pen_miss_p90=rows["pen_miss_p90"].to_numpy(float),
        pen_save_p90=np.where(pos_rows == 0, rows["pen_save_p90"].to_numpy(float), 0.0),
        bps_offset=models.bps_offsets.reindex(frame["player_code"]).fillna(0.0).to_numpy(float),
    )
    fx = frame.drop_duplicates("fixture_id")
    home_rows = fx.assign(
        home=np.where(fx["is_home"], fx["team_code"], fx["opponent_team_code"]),
        away=np.where(fx["is_home"], fx["opponent_team_code"], fx["team_code"]),
    )
    mu = [
        team.expected_goals(int(h), int(a))
        for h, a in zip(home_rows["home"], home_rows["away"], strict=True)
    ]
    fixtures = FixtureParams(
        fixture_id=home_rows["fixture_id"].to_numpy(np.int64),
        gameweek=home_rows["target_gw"].to_numpy(np.int64),
        home_team=home_rows["home"].to_numpy(np.int64),
        away_team=home_rows["away"].to_numpy(np.int64),
        mu_home=np.array([m[0] for m in mu]),
        mu_away=np.array([m[1] for m in mu]),
    )
    return players, fixtures, mp


def forecast(
    cache: FeatureCache,
    cut: Cutoff,
    models: ForecastModels,
    sim_config: SimulationConfig | None = None,
) -> Forecast:
    ds = cache.ds
    view = PointInTimeView(ds, cut.cutoff)
    ff = cache.get(cut)
    season_fx = ds["fixtures"][ds["fixtures"]["season"] == cut.season]
    teams = sorted(set(season_fx["home_team_code"]))
    team = fit_team_strength(view.team_match(), cut.cutoff, config_for_season(cut.season), teams)
    players, fixtures, mp = build_params(ff, models, view, team)
    ruleset = load_ruleset(cut.season)
    glob = GlobalParams(
        p_assisted=models.globals["p_assisted"],
        own_goal_rate=models.globals["own_goal_rate"],
        league_mu=models.globals["league_mu"],
        saves_opp_elasticity=models.saves_elasticity,
        dc_opp_elasticity=models.dc_elasticity,
        bps=models.bps,
        ruleset_version=ruleset.ruleset_version,
        model_versions=models.versions,
    )
    if sim_config is None:
        sim_spec = points_spec()[0].simulation
        sim_config = SimulationConfig(
            n_sims=sim_spec.n_sims, seed=sim_spec.seed, max_goals=sim_spec.max_goals
        )
    cfg = sim_config
    sim = simulate(players, fixtures, glob, ruleset, cfg)
    view.assert_no_leakage()

    s = sim.summary()
    p_idx = np.searchsorted(sim.player_codes, players.player_code)
    g_idx = np.searchsorted(sim.gameweeks, players.gameweek)
    # Minutes-model probability of ≥ 1 start in the GW (doubles: 1 − Π(1 − p)), before the
    # simulator imposes lineup coherence; ``prob_start`` in the summary is the coherent value.
    no_start = np.ones((len(sim.player_codes), len(sim.gameweeks)))
    np.multiply.at(no_start, (p_idx, g_idx), 1.0 - players.p_start)
    start_model = 1.0 - no_start
    rows = []
    for pi, code in enumerate(sim.player_codes):
        for gi, gw in enumerate(sim.gameweeks):
            if not sim.has_fixture[pi, gi]:
                continue
            row = {
                "player_code": int(code),
                "gw": int(gw),
                "horizon": int(gw) - cut.gw,
                "start_probability_model": float(start_model[pi, gi]),
            }
            row.update({k: float(v[pi, gi]) for k, v in s.items()})
            row.update({f"xp_{k}": float(v[pi, gi]) for k, v in sim.components.items()})
            rows.append(row)
    summary = pd.DataFrame(rows)
    provenance = {
        "season": cut.season,
        "decision_gw": cut.gw,
        "cutoff": cut.cutoff.isoformat(),
        "feature_snapshot_id": ff.snapshot_id,
        "feature_version": FEATURE_VERSION,
        "data_snapshot_id": ds.snapshot_id,
        "model_versions": models.versions,
        "ruleset_version": ruleset.ruleset_version,
        "simulation_seed": cfg.seed,
        "n_simulations": cfg.n_sims,
        "max_source_available_at": str(view.log.max_available_at),
        "team_model": config_for_season(cut.season).model_dump(),
    }
    provenance["prediction_run_id"] = short_id("pred", provenance)
    return Forecast(
        summary=summary,
        simulation=sim,
        players=players,
        fixtures=fixtures,
        team_strength=team,
        minutes=mp,
        provenance=provenance,
    )
