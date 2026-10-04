"""Service container and the decision workflows shared by the API and the worker."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import structlog
from sqlalchemy import Engine

from fpl_api.lineage import LineageStore
from fpl_api.observability import Metrics
from fpl_api.services import (
    CurrentContext,
    DataService,
    ForecastService,
    PriceService,
    RecommendationStore,
    StateStore,
)
from fpl_api.settings import Settings
from fpl_api.watch import plan_watch
from fpl_decision.engine import DecisionContext, RecommendationPackage, recommend
from fpl_decision.inputs import player_table
from fpl_decision.render import render_markdown
from fpl_domain.state import ManagerState, with_current_clubs
from fpl_forecasting.pipeline import Forecast
from fpl_notifications.store import NotificationStore
from fpl_optimizer.pool import candidate_pool
from fpl_optimizer.problem import (
    OptimizationProblem,
    OptimizerConfig,
    PlayerTable,
    Preferences,
    load_optimizer_config,
)
from fpl_storage.db import make_engine

log = structlog.get_logger("fpl_api")


@dataclass
class AppServices:
    settings: Settings
    data: DataService
    forecasts: ForecastService
    engine: Engine | None
    states: StateStore | None
    recs: RecommendationStore | None
    prices: PriceService | None = None
    notifications: NotificationStore | None = None
    metrics: Metrics = field(default_factory=Metrics)
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, settings: Settings, dataset: Any = None) -> AppServices:
        data = DataService(settings, dataset)
        engine = make_engine(settings.database_url) if settings.database_url else None
        metrics = Metrics()
        forecasts = ForecastService(settings, data, metrics)
        if engine is not None:
            lineage = LineageStore(engine, settings)
            forecasts.on_forecast = lambda fc, h, path: lineage.record_forecast(
                fc, data.ds, h, path
            )
        return cls(
            settings=settings,
            data=data,
            forecasts=forecasts,
            metrics=metrics,
            engine=engine,
            states=StateStore(engine) if engine else None,
            recs=RecommendationStore(engine) if engine else None,
            prices=PriceService(settings, data),
            notifications=NotificationStore(engine) if engine else None,
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

    def price_probs(self, ctx: CurrentContext) -> pd.DataFrame | None:
        if self.prices is None:
            return None
        try:
            return self.prices.probabilities(ctx.season, ctx.gameweek, ctx.cutoff)
        except Exception:  # price risk is supporting evidence; its absence is reported, not fatal
            log.warning("price_model_unavailable", exc_info=True)
            return None

    def table(
        self, ctx: CurrentContext, fc: Forecast, horizon: int, state: ManagerState | None = None
    ) -> PlayerTable:
        pool = self.data.view(ctx.cutoff).player_pool(ctx.season, ctx.gameweek)
        gws = tuple(range(ctx.gameweek, min(ctx.gameweek + horizon, 39)))
        names = self.data.names(ctx.season)
        pool = pool.assign(web_name=pool["player_code"].map(names))
        return player_table(fc.summary, pool, gws, state)


def optimization_problem(
    svc: AppServices,
    ctx: CurrentContext,
    fc: Forecast,
    state: ManagerState,
    horizon: int,
    profile: str = "default",
    preferences: Preferences | None = None,
) -> OptimizationProblem:
    """The transfer-planning problem on the candidate pool (one definition for API + alerts)."""
    table = svc.table(ctx, fc, horizon, state)
    state = with_current_clubs(state, table.teams_by_code())
    cfg = load_optimizer_config(profile)
    prefs = preferences or Preferences()
    pool = candidate_pool(
        table, state.codes, cfg.pool, must_include=prefs.forced_in, discount=cfg.objective.discount
    )
    return OptimizationProblem(
        state=state,
        ruleset=svc.data.ruleset(ctx.season),
        players=pool,
        gameweeks=tuple(range(ctx.gameweek, ctx.gameweek + table.horizon)),
        config=cfg,
        preferences=prefs,
    )


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
    state = with_current_clubs(state, table.teams_by_code())
    names = svc.data.names(ctx.season)
    cfg = config or load_optimizer_config(profile)
    prices = svc.price_probs(ctx)
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
        price_probs=prices,
    )
    t0 = time.perf_counter()
    try:
        pkg = recommend(
            dctx,
            n_alternatives=n_alternatives,
            run_stability=run_stability,
            run_scenarios=run_scenarios,
            run_chips=run_chips,
        )
    except Exception:
        svc.metrics.recommendations.labels("failure").inc()
        raise
    svc.metrics.optimization_seconds.labels("recommendation").observe(time.perf_counter() - t0)
    for stage in ("optimise", "stability", "scenarios", "chips"):
        if stage in pkg.timings:
            svc.metrics.optimization_seconds.labels(stage).observe(pkg.timings[stage])
    svc.metrics.optimization_status.labels("valid" if pkg.chosen.valid else "invalid").inc()
    if ctx.degraded_reasons:
        pkg.assumptions.extend(ctx.degraded_reasons)
    if prices is None or prices.empty:
        pkg.assumptions.append("price-change probabilities unavailable: price risk not assessed")
    rec_id = None
    if persist and svc.recs is not None:
        watch = plan_watch(svc, ctx, fc, state, pkg)
        rec_id = svc.recs.save(
            pkg, manager_key, state_id, render_markdown(pkg, names), watch, fc.run_id
        )
    svc.metrics.recommendations.labels("success").inc()
    return rec_id, pkg
