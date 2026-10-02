"""FastAPI application — the union of the §32 and §72 endpoint lists (ADR-0001 #3).

Every data-serving response carries a ``freshness`` block (§6, §82). Expensive work is either
bounded (sample counts, horizons, pool sizes) or runs as a job (``202 Accepted`` + job id,
ADR-0009). Domain errors map to structured 4xx responses; nothing is fabricated when an input
source is missing — the response says what is missing.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import structlog
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from sqlalchemy import select, text

from fpl_api import schemas as s
from fpl_api.container import AppServices, build_recommendation
from fpl_api.jobs import JobBackend
from fpl_api.security import Guard
from fpl_api.services import REPO_ROOT, ForecastUnavailable, sources_config
from fpl_api.settings import Settings
from fpl_decision.captaincy import analyse_captaincy
from fpl_decision.chips import plan_chips
from fpl_decision.paired import paired_gain, plan_samples
from fpl_decision.replacement import find_replacements
from fpl_decision.scenarios import ScenarioSpec, perturb_forecast, perturb_table
from fpl_domain.enums import ChipType, Position
from fpl_domain.errors import RuleViolation
from fpl_domain.squad import SquadPick, squad_violations
from fpl_domain.state import ChipStatus, ManagerState, initial_chips
from fpl_ingestion.manager_sync import reconstruct_state
from fpl_ingestion.sources.fpl_api import FplApiClient
from fpl_optimizer.lineup import best_lineup
from fpl_optimizer.milp import OptimizationError, build_and_solve
from fpl_optimizer.pool import candidate_pool
from fpl_optimizer.problem import OptimizationProblem, Preferences, load_optimizer_config
from fpl_storage import models as m
from fpl_storage.db import session_scope

log = structlog.get_logger("fpl_api")
REPORTS = (
    "forecast_eval",
    "backtest",
    "price_change",
    "optimizer_benchmark",
    "rate_shrinkage",
    "feature_importance",
    "ensemble_conformal",
    "baselines",
    "team_strength_tuning",
)
API = "/api/v1"


def create_app(settings: Settings | None = None, services: AppServices | None = None) -> FastAPI:
    settings = settings or Settings()
    svc = services or AppServices.build(settings)
    jobs = JobBackend(svc)
    guard = Guard(settings)
    registry = CollectorRegistry()
    req_count = Counter(
        "fpl_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=registry
    )
    req_latency = Histogram(
        "fpl_http_request_seconds", "HTTP latency", ["route"], registry=registry
    )

    app = FastAPI(
        title="FPL Decision Engine API",
        version="1.0.0",
        description="Decision-first Fantasy Premier League engine (see docs/API.md).",
    )
    app.state.services, app.state.jobs, app.state.settings = svc, jobs, settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _envelope(request: Request, call_next: Any) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        t0 = time.perf_counter()
        try:
            guard.check_size(request)
            if request.url.path.startswith(API):
                guard.check_rate(request)  # before auth: throttles key guessing too
            guard.check_key(request)
            response: Response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        dt = time.perf_counter() - t0
        req_count.labels(request.method, path, str(response.status_code)).inc()
        req_latency.labels(path).observe(dt)
        response.headers["x-request-id"] = rid
        log.info(
            "request",
            method=request.method,
            path=path,
            status=response.status_code,
            ms=round(1000 * dt, 1),
            request_id=rid,
        )
        return response

    @app.exception_handler(ForecastUnavailable)
    async def _no_forecast(_: Request, exc: ForecastUnavailable) -> JSONResponse:
        job = jobs.submit("forecast", {}) if svc.engine is not None else None
        return JSONResponse(
            {
                "detail": "forecast not ready; computation queued",
                "job_id": job,
                "forecast_key": str(exc),
            },
            status_code=503,
            headers={"Retry-After": "60"},
        )

    @app.exception_handler(RuleViolation)
    async def _rule(_: Request, exc: RuleViolation) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=422)

    @app.exception_handler(OptimizationError)
    async def _opt(_: Request, exc: OptimizationError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": "optimization"}, status_code=422)

    @app.exception_handler(LookupError)
    async def _missing(_: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    r = APIRouter(prefix=API)

    def fresh() -> dict[str, Any]:
        return svc.context().block(svc.data.snapshot_id)

    def need_db() -> None:
        if svc.states is None:
            raise HTTPException(503, "database not configured (FPL_DATABASE_URL)")

    def load_state(manager_key: str) -> tuple[str, ManagerState]:
        need_db()
        assert svc.states is not None
        got = svc.states.latest(manager_key)
        if got is None:
            raise HTTPException(404, f"no squad stored for '{manager_key}'; POST {API}/squad")
        return got

    # ------------------------------------------------------------------ system
    @r.get("/health")
    @r.get("/system/health")
    def health() -> dict[str, Any]:
        checks: dict[str, Any] = {}
        try:
            checks["dataset"] = {"ok": True, "snapshot_id": svc.data.snapshot_id}
        except Exception as exc:  # pragma: no cover - startup misconfiguration
            raise HTTPException(503, f"dataset unavailable: {exc}") from exc
        if svc.engine is not None:
            try:
                with svc.engine.connect() as c:
                    c.execute(text("select 1"))
                checks["database"] = {"ok": True}
            except Exception as exc:
                checks["database"] = {"ok": False, "error": type(exc).__name__}
        else:
            checks["database"] = {"ok": False, "error": "not configured"}
        checks["jobs"] = {"ok": True, "mode": jobs.mode}
        checks["live_source"] = {
            "ok": False,
            "note": "fantasy.premierleague.com is not reachable from this deployment's network "
            "policy; serving the last validated snapshot (degraded mode, ADR-0001 #5)",
        }
        ctx = svc.context()
        status = (
            "ok"
            if all(c.get("ok") for k, c in checks.items() if k != "live_source")
            else "degraded"
        )
        if ctx.degraded_reasons:
            status = "degraded"
        return {"status": status, "checks": checks, "freshness": ctx.block(svc.data.snapshot_id)}

    @r.get("/data-quality")
    def data_quality(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        if svc.engine is not None:
            with session_scope(svc.engine) as ses:
                rows = ses.scalars(
                    select(m.DataQualityEventRow)
                    .order_by(m.DataQualityEventRow.id.desc())
                    .limit(limit)
                ).all()
                events = [
                    {c.name: _jsonable(getattr(x, c.name)) for c in x.__table__.columns}
                    for x in rows
                ]
        return {"freshness": fresh(), "events": events}

    # ------------------------------------------------------------------ gameweeks & players
    @r.get("/gameweeks/current")
    def current_gameweek() -> dict[str, Any]:
        ctx = svc.context()
        return {
            "season": ctx.season,
            "gameweek": ctx.gameweek,
            "deadline": ctx.deadline.isoformat(),
            "decision_cutoff": ctx.cutoff.isoformat(),
            "freshness": ctx.block(svc.data.snapshot_id),
        }

    @r.get("/players")
    def players(
        position: Position | None = None,
        team: int | None = None,
        max_price: int | None = Query(None, ge=35, le=200),
        q: str | None = Query(None, max_length=40),
        sort: str = Query("xp", pattern="^(xp|price|start|xp_next)$"),
        horizon: int | None = Query(None, ge=1, le=10),
        limit: int = Query(50, ge=1, le=300),
    ) -> dict[str, Any]:
        ctx = svc.context()
        h = svc.horizon(horizon)
        fc = svc.forecast_for(ctx, h)
        pool = svc.data.view(ctx.cutoff).player_pool(ctx.season, ctx.gameweek)
        names, teams = svc.data.names(ctx.season), svc.data.teams(ctx.season)
        sm = fc.summary
        nxt = sm[sm["gw"] == ctx.gameweek].set_index("player_code")
        sm = sm[sm["gw"] < ctx.gameweek + h]
        tot = sm.groupby("player_code")["mean"].sum()
        df = pool.assign(
            name=pool["player_code"].map(names),
            team=pool["team_code"].map(teams),
            xp_next=pool["player_code"].map(nxt["mean"]).fillna(0.0),
            p10_next=pool["player_code"].map(nxt["p10"]).fillna(0.0),
            p90_next=pool["player_code"].map(nxt["p90"]).fillna(0.0),
            start_probability=pool["player_code"].map(nxt["prob_start"]).fillna(0.0),
            xp=pool["player_code"].map(tot).fillna(0.0),
        )
        if position is not None:
            df = df[df["position"] == position.value]
        if team is not None:
            df = df[df["team_code"] == team]
        if max_price is not None:
            df = df[pd.to_numeric(df["price"], errors="coerce") <= max_price]
        if q:
            df = df[df["name"].fillna("").str.contains(q, case=False, regex=False)]
        key = {"xp": "xp", "xp_next": "xp_next", "price": "price", "start": "start_probability"}
        df = df.sort_values(key[sort], ascending=False).head(limit)
        cols = [
            "player_code",
            "name",
            "team",
            "team_code",
            "position",
            "price",
            "status",
            "xp",
            "xp_next",
            "p10_next",
            "p90_next",
            "start_probability",
        ]
        return {
            "horizon": h,
            "players": _records(df[[c for c in cols if c in df]]),
            "freshness": fresh(),
        }

    @r.get("/players/{code}")
    def player(code: int) -> dict[str, Any]:
        ctx = svc.context()
        view = svc.data.view(ctx.cutoff)
        reg = svc.data.ds["players"]
        reg = reg[(reg["season"] == ctx.season) & (reg["player_code"] == code)]
        if reg.empty:
            raise HTTPException(404, f"player {code} not registered in {ctx.season}")
        pm = view.player_match()
        hist = pm[pm["player_code"] == code].sort_values("kickoff_at").tail(8)
        prices = view.price_observations(ctx.season)
        prices = prices[prices["player_code"] == code].sort_values("observed_at")
        snap = view.latest_snapshot(ctx.season)
        live = snap[snap["player_code"] == code]
        return {
            "player": _records(reg)[0],
            "recent_matches": _records(
                hist[
                    [
                        "season",
                        "gw",
                        "kickoff_at",
                        "minutes",
                        "points",
                        "goals",
                        "assists",
                        "xg",
                        "xa",
                        "bonus",
                    ]
                ]
            ),
            "price_history": _records(prices[["observed_at", "price"]]),
            "live": _records(
                live.drop(columns=["set_piece_json", "official_price_signal_json"], errors="ignore")
            )
            if len(live)
            else [],
            "freshness": fresh(),
        }

    @r.get("/players/{code}/forecast")
    def player_forecast(
        code: int, horizon: int | None = Query(None, ge=1, le=10)
    ) -> dict[str, Any]:
        ctx = svc.context()
        fc = svc.forecast_for(ctx, svc.horizon(horizon))
        h = svc.horizon(horizon)
        rows = fc.summary[
            (fc.summary["player_code"] == code) & (fc.summary["gw"] < svc.context().gameweek + h)
        ].sort_values("gw")
        return {
            "player_code": code,
            "gameweeks": _records(rows),
            "provenance": _jsonable(fc.provenance),
            "freshness": fresh(),
        }

    # ------------------------------------------------------------------ squad
    @r.get("/squad")
    def get_squad(manager_key: str = Query(..., max_length=64)) -> dict[str, Any]:
        sid, st = load_state(manager_key)
        names = svc.data.names(st.season)
        return {
            "state_id": sid,
            "state": st.model_dump(mode="json"),
            "names": {str(p.player_code): names.get(p.player_code) for p in st.squad},
            "freshness": fresh(),
        }

    @r.post("/squad")
    def put_squad(body: s.SquadIn) -> dict[str, Any]:
        need_db()
        assert svc.states is not None
        ctx = svc.context()
        rs = svc.data.ruleset(ctx.season)
        pool = (
            svc.data.view(ctx.cutoff).player_pool(ctx.season, ctx.gameweek).set_index("player_code")
        )
        picks = []
        for p in body.picks:
            if p.player_code not in pool.index:
                raise HTTPException(
                    422,
                    f"player {p.player_code} is not selectable in {ctx.season} GW{ctx.gameweek}",
                )
            row = pool.loc[p.player_code]
            picks.append(
                SquadPick(
                    player_code=p.player_code,
                    position=Position(row["position"]),
                    team_code=int(row["team_code"]),
                    purchase_price=p.purchase_price or int(row["price"]),
                )
            )
        v = squad_violations(picks, rs, body.bank)
        if v:
            raise HTTPException(422, {"violations": [str(x) for x in v]})
        chips = tuple(
            c.model_copy(update={"status": ChipStatus.USED})
            if c.chip_id in set(body.chips_used)
            else c
            for c in initial_chips(rs)
        )
        state = ManagerState(
            season=ctx.season,
            gameweek=ctx.gameweek,
            squad=tuple(picks),
            bank=body.bank,
            free_transfers=body.free_transfers,
            chips=chips,
            source="manual",
            provenance="manual_entry",
        )
        sid = svc.states.save(body.manager_key, state, "manual")
        return {"state_id": sid, "state": state.model_dump(mode="json"), "freshness": fresh()}

    @r.post("/squad/sync")
    def sync_squad(body: s.SyncIn) -> JSONResponse:
        """Reconstruct the manager state from the public FPL API (no credentials, §6.1)."""
        if not settings.live_sync_enabled:
            raise HTTPException(503, "live sync disabled by configuration; use POST /squad")
        need_db()
        assert svc.states is not None
        ctx = svc.context()
        try:
            client = FplApiClient.from_config(sources_config())
            _, entry = client.entry(body.manager_id)
            _, history = client.entry_history(body.manager_id)
            _, transfers = client.entry_transfers(body.manager_id)
            _, boot = client.bootstrap_static()
            report = reconstruct_state(
                entry,
                history,
                lambda gw: client.entry_picks(body.manager_id, gw)[1],
                transfers,
                boot,
                svc.data.ruleset(ctx.season),
                ctx.gameweek,
                datetime.now(UTC),
            )
        except Exception as exc:  # network policy, upstream outage, schema drift …
            return JSONResponse(
                {
                    "detail": "live FPL API unavailable from this deployment; serving degraded "
                    f"mode — enter the squad manually via POST {API}/squad",
                    "error": type(exc).__name__,
                    "degraded": True,
                },
                status_code=503,
            )
        sid = svc.states.save(body.manager_key, report.state, "fpl_api")
        return JSONResponse(
            {
                "state_id": sid,
                "state": report.state.model_dump(mode="json"),
                "warnings": report.warnings,
                "freshness": fresh(),
            },
            status_code=200,
        )

    # ------------------------------------------------------------------ optimisation
    def _problem(
        st: ManagerState, profile: str, horizon: int | None, prefs: s.PreferencesIn | None = None
    ) -> tuple[Any, Any, OptimizationProblem]:
        ctx = svc.context()
        h = svc.horizon(horizon)
        fc = svc.forecast_for(ctx, h)
        table = svc.table(ctx, fc, h, st)
        cfg = load_optimizer_config(profile)
        pref = (
            Preferences(
                locked=frozenset(prefs.locked),
                banned=frozenset(prefs.banned),
                max_transfers_per_gw=prefs.max_transfers_per_gw,
            )
            if prefs
            else Preferences()
        )
        pool = candidate_pool(table, st.codes, cfg.pool, discount=cfg.objective.discount)
        prob = OptimizationProblem(
            state=st,
            ruleset=svc.data.ruleset(ctx.season),
            players=pool,
            gameweeks=tuple(range(ctx.gameweek, ctx.gameweek + table.horizon)),
            config=cfg,
            preferences=pref,
        )
        return ctx, fc, prob

    @r.post("/optimize")
    @r.post("/optimize/transfer")
    def optimize(body: s.OptimizeIn) -> Any:
        sid, st = load_state(body.manager_key)
        h = svc.horizon(body.horizon)
        if h > settings.sync_horizon_limit or body.chip_options:
            job = jobs.submit(
                "recommendation",
                {
                    "manager_key": body.manager_key,
                    "state_id": sid,
                    "profile": body.profile,
                    "horizon": h,
                    "stability": False,
                    "scenarios": False,
                    "chips": True,
                },
            )
            return JSONResponse({"job_id": job, "status": jobs.get(job)}, status_code=202)
        pref = Preferences(
            locked=frozenset(body.preferences.locked),
            banned=frozenset(body.preferences.banned),
            max_transfers_per_gw=body.preferences.max_transfers_per_gw,
        )
        _, pkg = build_recommendation(
            svc,
            body.manager_key,
            st,
            sid,
            profile=body.profile,
            horizon=h,
            n_alternatives=body.alternatives,
            run_stability=False,
            run_scenarios=False,
            run_chips=False,
            preferences=pref,
            persist=False,
        )
        return {
            "decision": pkg.decision,
            "hold": pkg.hold.model_dump(mode="json"),
            "chosen": pkg.chosen.model_dump(mode="json"),
            "alternatives": [a.model_dump(mode="json") for a in pkg.alternatives],
            "optimizer_run_id": pkg.optimizer_run_id,
            "config_refs": pkg.config_refs,
            "freshness": fresh(),
        }

    @r.post("/optimize/squad")
    def optimize_squad(body: s.SquadBuildIn) -> dict[str, Any]:
        ctx = svc.context()
        h = svc.horizon(body.horizon)
        fc = svc.forecast_for(ctx, h)
        table = svc.table(ctx, fc, h)
        rs = svc.data.ruleset(ctx.season)
        cfg = load_optimizer_config(body.profile)
        empty = ManagerState(
            season=ctx.season,
            gameweek=ctx.gameweek,
            squad=(),
            bank=body.budget,
            free_transfers=1,
            chips=initial_chips(rs),
        )
        prob = OptimizationProblem(
            state=empty,
            ruleset=rs,
            players=table,
            gameweeks=tuple(range(ctx.gameweek, ctx.gameweek + table.horizon)),
            config=cfg,
            initial_squad_mode=True,
        )
        sol = build_and_solve(prob)
        names = svc.data.names(ctx.season)
        first = sol.plans[0]
        return {
            "squad": [{"player_code": c, "name": names.get(c)} for c in first.squad],
            "lineup": first.lineup.model_dump(),
            "bank_after": first.bank_after,
            "expected_points_gw": first.expected_points,
            "objective": sol.objective,
            "config_ref": cfg.config_ref,
            "freshness": fresh(),
        }

    @r.post("/lineup")
    def lineup(body: s.LineupIn) -> dict[str, Any]:
        _, st = load_state(body.manager_key)
        ctx = svc.context()
        fc = svc.forecast_for(ctx, 1)
        table = svc.table(ctx, fc, 1, st)
        idx = table.index()
        ev = {c: float(table.ev[idx[c], 0]) if c in idx else 0.0 for c in st.codes}
        chip = ChipType(body.chip) if body.chip else None
        rs = svc.data.ruleset(ctx.season)
        pos = table.positions_map()
        ch = best_lineup(list(st.codes), pos, ev, ev, load_optimizer_config().objective, rs, chip)
        sim = fc.simulation
        have = set(sim.player_codes.tolist())
        gi = sim.gw_index(ctx.gameweek)
        pts = {
            c: sim.points[:, sim.index_of(c), gi].astype(np.int64)
            if c in have
            else np.zeros(sim.n_sims, np.int64)
            for c in st.codes
        }
        mins = {
            c: sim.minutes[:, sim.index_of(c), gi].astype(np.int64)
            if c in have
            else np.zeros(sim.n_sims, np.int64)
            for c in st.codes
        }
        cap = analyse_captaincy(ch.lineup, pos, pts, mins, rs, chip=chip)
        return {
            "lineup": ch.lineup.model_dump(),
            "expected_points": ch.expected_points,
            "captaincy": {
                "expected": cap.expected,
                "safe": cap.safe,
                "high_variance": cap.high_variance,
                "options": [o.__dict__ for o in cap.options],
                "beats_matrix": cap.beats_matrix.round(3).tolist(),
            },
            "freshness": fresh(),
        }

    @r.post("/replacement")
    @r.post("/replacements")
    def replacement(body: s.ReplacementIn) -> dict[str, Any]:
        _, st = load_state(body.manager_key)
        ctx, fc, prob = _problem(st, body.profile, body.horizon)
        full = svc.table(ctx, fc, svc.horizon(body.horizon), st)
        prob = replace(prob, players=full)
        res = find_replacements(
            prob, body.out_player, fc.simulation, fc.summary, n_return=body.candidates
        )
        names = svc.data.names(ctx.season)
        hold_id = res.hold.stats.get("status", "hold")
        return {
            "out": {"player_id": body.out_player, "name": names.get(body.out_player)},
            "universe_size": res.universe_size,
            "candidates": [
                {
                    **c.contract(f"hold_{body.manager_key}_{ctx.gameweek}"),
                    "name": names.get(c.in_code),
                    "price": c.price,
                    "objective_gain": c.objective_gain,
                    "screen_gain": c.screen_gain,
                    "start_probability": c.start_probability,
                    "upside_probability": c.upside_probability,
                    "follow_up": c.follow_up,
                    "selection_reason": c.selection_reason,
                    "bank_after": c.bank_after,
                    "valid": c.valid,
                }
                for c in res.candidates
            ],
            "notes": res.notes,
            "hold_status": hold_id,
            "freshness": fresh(),
        }

    @r.post("/chips/simulate")
    def chips(body: s.ChipSimIn) -> dict[str, Any]:
        _, st = load_state(body.manager_key)
        _, fc, prob = _problem(st, "default", body.horizon)
        base = build_and_solve(
            replace(prob, preferences=replace(prob.preferences, hold_first_gw=False))
        )
        out: dict[str, Any] = {"freshness": fresh()}
        if body.chip_id and body.gameweek:
            forced = replace(
                prob,
                forced_chips={body.chip_id: body.gameweek},
                chip_options={body.chip_id: (body.gameweek,)},
            )
            sol = build_and_solve(forced)
            pos = prob.players.positions_map()
            g = paired_gain(
                plan_samples(fc.simulation, sol.plans, pos, prob.ruleset),
                plan_samples(fc.simulation, base.plans, pos, prob.ruleset),
            )
            out["what_if"] = {
                "chip_id": body.chip_id,
                "gameweek": body.gameweek,
                "objective_gain": sol.objective - base.objective,
                "gain_mean": g.mean,
                "gain_p10": g.p10,
                "gain_p90": g.p90,
                "probability_positive": g.probability_positive,
                "plan": [
                    {
                        "gameweek": p.gameweek,
                        "out": list(p.transfers_out),
                        "in": list(p.transfers_in),
                        "chip": p.chip_id,
                    }
                    for p in sol.plans
                ],
            }
        out["chips"] = [c.model_dump() for c in plan_chips(prob, base, fc)]
        return out

    @r.post("/scenarios")
    @r.post("/what-if")
    @r.post("/simulate")
    def scenarios(body: s.WhatIfIn) -> dict[str, Any]:
        _, st = load_state(body.manager_key)
        _, fc, prob = _problem(st, "default", body.horizon)
        hold = build_and_solve(
            replace(prob, preferences=replace(prob.preferences, hold_first_gw=True))
        )
        move = None
        if body.sells or body.buys:
            move = build_and_solve(
                replace(
                    prob,
                    preferences=replace(
                        prob.preferences,
                        forced_out=frozenset(body.sells),
                        forced_in=frozenset(body.buys),
                    ),
                )
            )
        pos = prob.players.positions_map()
        results = []
        for i, sc in enumerate(body.scenarios):
            spec = ScenarioSpec(
                name=f"scenario_{i}",
                kind=sc.kind,
                players=tuple(sc.players),
                teams=tuple(sc.teams),
                gameweeks=tuple(sc.gameweeks),
                magnitude=sc.magnitude,
            )
            fc2 = perturb_forecast(fc, spec)
            h_base = plan_samples(fc.simulation, hold.plans, pos, prob.ruleset)
            h_scn = plan_samples(fc2.simulation, hold.plans, pos, prob.ruleset)
            entry: dict[str, Any] = {
                "kind": sc.kind,
                "players": sc.players,
                "teams": sc.teams,
                "hold_points_base": float(h_base.sum(axis=1).mean()),
                "hold_points_scenario": float(h_scn.sum(axis=1).mean()),
            }
            if sc.kind in ("price_shock", "conservative"):
                t2 = perturb_table(prob.players, spec)
                try:
                    best = build_and_solve(replace(prob, players=t2))
                    entry["reoptimized_first_action"] = {
                        "out": list(best.first.transfers_out),
                        "in": list(best.first.transfers_in),
                    }
                except OptimizationError as exc:
                    entry["infeasible"] = str(exc)
            if move is not None:
                mv = plan_samples(fc2.simulation, move.plans, pos, prob.ruleset)
                g = paired_gain(mv, h_scn)
                entry["move_gain_vs_hold"] = {
                    "mean": g.mean,
                    "p10": g.p10,
                    "p90": g.p90,
                    "probability_positive": g.probability_positive,
                }
            results.append(entry)
        return {"scenarios": results, "freshness": fresh()}

    # ------------------------------------------------------------------ recommendations
    @r.post("/recommendations/generate")
    def generate(body: s.RecommendationIn) -> JSONResponse:
        sid, _ = load_state(body.manager_key)
        job = jobs.submit(
            "recommendation",
            {**body.model_dump(), "state_id": sid, "horizon": svc.horizon(body.horizon)},
        )
        return JSONResponse({"job_id": job, "job": jobs.get(job)}, status_code=202)

    @r.get("/recommendations/current")
    def current_rec(manager_key: str = Query(..., max_length=64)) -> dict[str, Any]:
        need_db()
        assert svc.recs is not None
        ctx = svc.context()
        rec = svc.recs.current(manager_key, ctx.gameweek)
        if rec is None:
            raise HTTPException(
                404,
                f"no recommendation yet for GW{ctx.gameweek}; POST {API}/recommendations/generate",
            )
        return {**rec, "freshness": fresh()}

    @r.get("/recommendations/{rec_id}")
    def get_rec(rec_id: str) -> dict[str, Any]:
        need_db()
        assert svc.recs is not None
        rec = svc.recs.get(rec_id)
        if rec is None:
            raise HTTPException(404, f"recommendation {rec_id} not found")
        return {**rec, "freshness": fresh()}

    @r.get("/decisions")
    def decisions(manager_key: str = Query(..., max_length=64)) -> dict[str, Any]:
        need_db()
        assert svc.recs is not None
        return {"journal": svc.recs.journal(manager_key)}

    @r.post("/decisions/{rec_id}/feedback")
    def feedback(rec_id: str, body: s.FeedbackIn) -> dict[str, Any]:
        need_db()
        assert svc.recs is not None
        try:
            svc.recs.feedback(rec_id, body.followed, body.note, body.realized_points)
        except KeyError as exc:
            raise HTTPException(404, f"recommendation {rec_id} not found") from exc
        return {"recorded": True}

    # ------------------------------------------------------------------ backtests & jobs
    @r.post("/backtests")
    def start_backtest(body: s.BacktestIn) -> JSONResponse:
        job = jobs.submit("backtest", body.model_dump())
        return JSONResponse({"job_id": job, "job": jobs.get(job)}, status_code=202)

    @r.get("/backtests")
    def list_backtests() -> dict[str, Any]:
        published = None
        rep = REPO_ROOT / "ml" / "reports" / "backtest.json"
        if rep.exists():
            data = json.loads(rep.read_text())
            published = {
                "seasons": data.get("seasons"),
                "summary": data.get("summary"),
                "engine_vs": data.get("engine_vs"),
            }
        runs: list[dict[str, Any]] = []
        if svc.engine is not None:
            with session_scope(svc.engine) as ses:
                for b in ses.scalars(
                    select(m.BacktestRunRow).order_by(m.BacktestRunRow.started_at.desc()).limit(20)
                ):
                    runs.append(
                        {
                            "id": b.id,
                            "season": b.season,
                            "status": b.status,
                            "season_points": b.results_json.get("season_points"),
                        }
                    )
        return {"published_report": published, "runs": runs}

    @r.get("/backtests/{bt_id}")
    def get_backtest(bt_id: str) -> dict[str, Any]:
        need_db()
        assert svc.engine is not None
        with session_scope(svc.engine) as ses:
            b = ses.get(m.BacktestRunRow, bt_id)
            if b is None:
                raise HTTPException(404, f"backtest {bt_id} not found")
            return {
                "id": b.id,
                "season": b.season,
                "status": b.status,
                "from_gw": b.from_gw,
                "to_gw": b.to_gw,
                "config": b.config_json,
                "cutoff_policy": b.cutoff_policy,
                "data_snapshot_id": b.data_snapshot_id,
                "results": b.results_json,
            }

    # ------------------------------------------------------------------ reports, models, settings
    @r.get("/reports")
    def list_reports() -> dict[str, Any]:
        rep = REPO_ROOT / "ml" / "reports"
        return {"reports": sorted(n for n in REPORTS if (rep / f"{n}.md").exists())}

    @r.get("/reports/{name}")
    def get_report(name: str) -> dict[str, Any]:
        if name not in REPORTS:  # allow-list: no path traversal into the filesystem
            raise HTTPException(404, f"unknown report {name}")
        rep = REPO_ROOT / "ml" / "reports"
        md = rep / f"{name}.md"
        js = rep / f"{name}.json"
        if not md.exists():
            raise HTTPException(404, f"report {name} not generated")
        return {
            "name": name,
            "markdown": md.read_text(encoding="utf-8"),
            "data": json.loads(js.read_text()) if js.exists() else None,
        }

    @r.get("/reports/figures/{name}")
    def get_figure(name: str) -> Response:
        figs = REPO_ROOT / "ml" / "reports" / "figures"
        allowed = {p.name for p in figs.glob("*.svg")} if figs.exists() else set()
        if name not in allowed:
            raise HTTPException(404, f"unknown figure {name}")
        return Response((figs / name).read_text(encoding="utf-8"), media_type="image/svg+xml")

    @r.get("/models")
    def models_status() -> dict[str, Any]:
        rep = REPO_ROOT / "ml" / "reports"
        out: dict[str, Any] = {}
        for name in ("forecast_eval", "price_change"):
            path = rep / f"{name}.json"
            if path.exists():
                data = json.loads(path.read_text())
                out[name] = {
                    "promotion_gates": data.get("promotion_gates"),
                    "gate_history": data.get("gate_history"),
                    "gate_metrics": data.get("gate_metrics"),
                }
        ctx = svc.context()
        try:
            fc = svc.forecasts.get(ctx.season, ctx.gameweek, svc.horizon(None), compute=False)
            out["serving"] = _jsonable(fc.provenance)
        except ForecastUnavailable:
            out["serving"] = None
        return out

    @r.get("/settings")
    def get_settings(manager_key: str = Query(..., max_length=64)) -> dict[str, Any]:
        need_db()
        assert svc.engine is not None
        with session_scope(svc.engine) as ses:
            row = ses.get(m.ManagerSettingsRow, manager_key)
            return {
                "manager_key": manager_key,
                "settings": row.settings_json if row else s.ManagerSettingsIn().model_dump(),
            }

    @r.post("/settings")
    def put_settings(
        body: s.ManagerSettingsIn, manager_key: str = Query(..., max_length=64)
    ) -> dict[str, Any]:
        need_db()
        assert svc.engine is not None
        with session_scope(svc.engine) as ses:
            row = ses.get(m.ManagerSettingsRow, manager_key)
            if row is None:
                ses.add(
                    m.ManagerSettingsRow(manager_key=manager_key, settings_json=body.model_dump())
                )
            else:
                row.settings_json = body.model_dump()
        return {"manager_key": manager_key, "settings": body.model_dump()}

    @r.get("/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        j = jobs.get(job_id)
        if j is None:
            raise HTTPException(404, f"job {job_id} not found")
        return j

    app.include_router(r)

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, float) and not np.isfinite(v):
        return None
    return v


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        _jsonable(
            {
                k: (None if (isinstance(x, float) and np.isnan(x)) or x is pd.NA else x)
                for k, x in rec.items()
            }
        )
        for rec in df.to_dict("records")
    ]
