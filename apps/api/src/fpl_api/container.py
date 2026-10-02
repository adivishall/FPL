"""Service container and the decision workflows shared by the API and the worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine

from fpl_api.services import (
    CurrentContext,
    DataService,
    ForecastService,
    RecommendationStore,
    StateStore,
)
from fpl_api.settings import Settings
from fpl_decision.engine import DecisionContext, RecommendationPackage, recommend
from fpl_decision.inputs import player_table
from fpl_decision.render import render_markdown
from fpl_domain.state import ManagerState
from fpl_forecasting.pipeline import Forecast
from fpl_optimizer.problem import OptimizerConfig, PlayerTable, Preferences, load_optimizer_config
from fpl_storage.db import make_engine


@dataclass
class AppServices:
    settings: Settings
    data: DataService
    forecasts: ForecastService
    engine: Engine | None
    states: StateStore | None
    recs: RecommendationStore | None
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, settings: Settings, dataset: Any = None) -> AppServices:
        data = DataService(settings, dataset)
        engine = make_engine(settings.database_url) if settings.database_url else None
        return cls(
            settings=settings,
            data=data,
            forecasts=ForecastService(settings, data),
            engine=engine,
            states=StateStore(engine) if engine else None,
            recs=RecommendationStore(engine) if engine else None,
        )

    # ------------------------------------------------------------------ shared building blocks
    def context(self) -> CurrentContext:
        return self.data.current()

    def horizon(self, requested: int | None) -> int:
        h = requested or self.settings.horizon_default
        return max(1, min(h, self.settings.horizon_max))

    def forecast_for(
        self, ctx: CurrentContext, horizon: int, n_sims: int | None = None
    ) -> Forecast:
        return self.forecasts.get(ctx.season, ctx.gameweek, horizon, n_sims)

    def table(
        self, ctx: CurrentContext, fc: Forecast, horizon: int, state: ManagerState | None = None
    ) -> PlayerTable:
        pool = self.data.view(ctx.cutoff).player_pool(ctx.season, ctx.gameweek)
        gws = tuple(range(ctx.gameweek, min(ctx.gameweek + horizon, 39)))
        names = self.data.names(ctx.season)
        pool = pool.assign(web_name=pool["player_code"].map(names))
        return player_table(fc.summary, pool, gws, state)


def build_recommendation(
    svc: AppServices,
    manager_key: str,
    state: ManagerState,
    state_id: str | None,
    profile: str = "default",
    horizon: int | None = None,
    n_alternatives: int = 3,
    run_stability: bool = True,
    run_scenarios: bool = True,
    run_chips: bool = True,
    preferences: Preferences | None = None,
    config: OptimizerConfig | None = None,
    persist: bool = True,
) -> tuple[str | None, RecommendationPackage]:
    ctx = svc.context()
    h = svc.horizon(horizon)
    fc = svc.forecast_for(ctx, h)
    table = svc.table(ctx, fc, h, state)
    names = svc.data.names(ctx.season)
    cfg = config or load_optimizer_config(profile)
    dctx = DecisionContext(
        state=state,
        ruleset=svc.data.ruleset(ctx.season),
        forecast=fc,
        players=table,
        config=cfg,
        gameweeks=tuple(range(ctx.gameweek, ctx.gameweek + table.horizon)),
        features=svc.forecasts.features(ctx.season, ctx.gameweek, h),
        names=names,
        preferences=preferences or Preferences(),
    )
    pkg = recommend(
        dctx,
        n_alternatives=n_alternatives,
        run_stability=run_stability,
        run_scenarios=run_scenarios,
        run_chips=run_chips,
    )
    if ctx.degraded_reasons:
        pkg.assumptions.extend(ctx.degraded_reasons)
    rec_id = None
    if persist and svc.recs is not None:
        rec_id = svc.recs.save(pkg, manager_key, state_id, render_markdown(pkg, names))
    return rec_id, pkg
