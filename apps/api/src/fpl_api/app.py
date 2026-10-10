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
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError
from sqlalchemy import func, select, text

from fpl_api import analytics
from fpl_api import schemas as s
from fpl_api.accounts import (
    OPERATOR,
    AuthError,
    Principal,
    UserStore,
    manager_key_in_request,
    require_operator,
    require_owner,
    require_user,
)
from fpl_api.container import (
    AppServices,
    UnsupportedHorizon,
    build_recommendation,
    optimization_problem,
)
from fpl_api.copilot import lineup_and_captaincy
from fpl_api.jobs import JobBackend
from fpl_api.lineage import trace
from fpl_api.privacy import audit, delete_manager, export_manager
from fpl_api.security import Guard, hash_key, loggable_path
from fpl_api.services import ForecastUnavailable, sources_config
from fpl_api.settings import Settings
from fpl_decision.chips import plan_chips
from fpl_decision.paired import paired_gain, plan_samples
from fpl_decision.replacement import find_replacements
from fpl_decision.scenarios import ScenarioSpec, perturb_forecast, perturb_table
from fpl_domain.enums import ChipType, Position
from fpl_domain.errors import RuleViolation
from fpl_domain.squad import SquadPick, squad_violations
from fpl_domain.state import ChipStatus, ManagerState, initial_chips
from fpl_forecasting.model_config import price_spec
from fpl_forecasting.price_change import official_signal
from fpl_ingestion.http import HttpStatusError, SourceUnavailableError
from fpl_ingestion.manager_sync import reconstruct_state
from fpl_ingestion.sources.fpl_api import FplApiClient
from fpl_notifications.rules import load_notification_config
from fpl_notifications.store import UnsafeUrl, validate_webhook_url
from fpl_optimizer.milp import OptimizationError, build_and_solve
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
SECURITY_STATUS = {
    401: "auth_missing",
    403: "auth_invalid",
    413: "body_too_large",
    429: "rate_limited",
}
KEY_PATTERN = r"^[A-Za-z0-9_\-]+$"


def create_app(settings: Settings | None = None, services: AppServices | None = None) -> FastAPI:
    settings = settings or Settings()
    svc = services or AppServices.build(settings)
    jobs = JobBackend(svc)
    users = (
        UserStore(svc.engine, timedelta(days=settings.session_ttl_days))
        if svc.engine is not None
        else None
    )
    guard = Guard(settings, users)
    metrics = svc.metrics

    app = FastAPI(
        title="FPL Decision Engine API",
        version="1.0.0",
        description="Decision-first Fantasy Premier League engine (see docs/API.md).",
    )
    app.state.services, app.state.jobs, app.state.settings = svc, jobs, settings
    app.state.users = users
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "DELETE"],
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
            request.state.principal = guard.principal(request)
            response: Response = await call_next(request)
        except HTTPException as exc:
            body: dict[str, Any] = {"detail": exc.detail}
            if isinstance(exc, AuthError):
                body["code"] = exc.code
            response = JSONResponse(body, status_code=exc.status_code, headers=exc.headers)
            kind = SECURITY_STATUS.get(exc.status_code)
            if kind:  # §75: log security-relevant events without secrets (no key material)
                metrics.security_events.labels(kind).inc()
                log.warning(
                    "security_event",
                    kind=kind,
                    method=request.method,
                    path=loggable_path(request.url.path),
                    client=guard.client_id(request)[:20],
                    request_id=rid,
                )
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unmatched"  # bounded label cardinality
        dt = time.perf_counter() - t0
        metrics.http_requests.labels(request.method, path, str(response.status_code)).inc()
        metrics.http_latency.labels(path).observe(dt)
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

    @app.exception_handler(AuthError)
    async def _auth(_: Request, exc: AuthError) -> JSONResponse:
        kind = SECURITY_STATUS.get(exc.status_code)
        if kind:
            metrics.security_events.labels(kind).inc()
        return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=exc.status_code)

    @app.exception_handler(UnsupportedHorizon)
    async def _horizon(_: Request, exc: UnsupportedHorizon) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": "unsupported_horizon"}, status_code=422)

    @app.exception_handler(RuleViolation)
    async def _rule(_: Request, exc: RuleViolation) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=422)

    @app.exception_handler(OptimizationError)
    async def _opt(_: Request, exc: OptimizationError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": "optimization"}, status_code=422)

    @app.exception_handler(LookupError)
    async def _missing(_: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    def principal_of(request: Request) -> Principal:
        return getattr(request.state, "principal", OPERATOR)

    async def authorize(request: Request) -> None:
        """Ownership of manager data is enforced here for every route that names a manager
        key (path, query or body); routes addressed by record id check inside."""
        key = await manager_key_in_request(request)
        if key is not None:
            require_owner(principal_of(request), users, key)

    def owner_of_rec(request: Request, rec_id: str) -> dict[str, Any]:
        need_db()
        assert svc.recs is not None
        rec = svc.recs.get(rec_id)
        if rec is None:
            raise HTTPException(404, f"recommendation {rec_id} not found")
        require_owner(principal_of(request), users, str(rec.get("manager_key") or ""))
        return rec

    r = APIRouter(prefix=API, dependencies=[Depends(authorize)])

    def fresh() -> dict[str, Any]:
        return svc.context().block(svc.data.snapshot_id)

    def live_source_status() -> dict[str, Any]:
        """Outcome of the latest live FPL capture (data_jobs), not an assumption (§82)."""
        if not settings.live_sync_enabled:
            return {"ok": False, "enabled": False, "note": "live FPL source disabled by config"}
        if svc.engine is None:
            return {"ok": False, "enabled": True, "note": "no database: capture history unknown"}
        try:
            with session_scope(svc.engine) as ses:
                last = ses.scalars(
                    select(m.DataJob)
                    .where(m.DataJob.job_type == "live_bootstrap")
                    .order_by(m.DataJob.started_at.desc())
                    .limit(1)
                ).first()
                ok_at = ses.scalar(
                    select(func.max(m.DataJob.completed_at)).where(
                        m.DataJob.job_type == "live_bootstrap", m.DataJob.status == "succeeded"
                    )
                )
        except Exception as exc:
            return {"ok": False, "enabled": True, "error": type(exc).__name__}
        if last is None:
            return {"ok": False, "enabled": True, "note": "no live capture attempted yet"}
        return {
            "ok": last.status == "succeeded",
            "enabled": True,
            "last_attempt_at": _jsonable(last.started_at),
            "last_status": last.status,
            "last_success_at": _jsonable(ok_at),
            "note": "latest live capture succeeded"
            if last.status == "succeeded"
            else "latest live capture failed; serving the last validated snapshot",
        }

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

    def warm_analysis(manager_key: str, state_id: str) -> None:
        """Queue the Copilot Home analysis for a changed squad so the page is instant. In
        inline mode (no queue) the Home route computes on first read instead."""
        if jobs.mode == "rq":
            jobs.submit("squad_analysis", {"manager_key": manager_key, "state_id": state_id})

    # ------------------------------------------------------------------ copilot home (M1.1a)
    @r.get("/copilot/home")
    def copilot_home(
        manager_key: str = Query(..., max_length=64, pattern=KEY_PATTERN),
    ) -> dict[str, Any]:
        """Everything Home shows, from the precomputed analysis of the manager's current squad
        on the current snapshot. When that analysis does not exist yet it is queued and the
        most recent one is served, clearly marked stale and pending; nothing heavy runs here."""
        need_db()
        assert svc.states is not None and svc.analyses is not None and svc.recs is not None
        ctx = svc.context()
        latest = svc.states.latest(manager_key)
        base: dict[str, Any] = {
            "season": ctx.season,
            "gameweek": ctx.gameweek,
            "deadline": ctx.deadline.isoformat(),
            "horizon_max": settings.horizon_max,
            "freshness": fresh(),
        }
        if latest is None:
            return {
                **base,
                "state_id": None,
                "analysis": None,
                "pending": False,
                "stale": False,
                "recommendation": None,
            }
        sid, _ = latest
        h = svc.horizon(None)
        fc = svc.forecast_for(ctx, h)  # precomputed in production; 503 (queued) when not yet
        fkey = str(fc.provenance.get("forecast_key", fc.run_id))
        snap = svc.data.snapshot_id
        analysis = svc.analyses.get(manager_key, sid, snap, fkey)
        pending = False
        job_id = None
        if analysis is None:
            job_id = jobs.submit("squad_analysis", {"manager_key": manager_key, "state_id": sid})
            analysis = svc.analyses.get(manager_key, sid, snap, fkey)  # inline mode: done now
            if analysis is None:
                pending = True
                analysis = svc.analyses.latest(manager_key)
        stale = analysis is not None and (
            analysis["state_id"] != sid or analysis["snapshot_id"] != snap
        )
        rec = svc.recs.current(manager_key, ctx.gameweek)
        summary = None
        if rec is not None:
            summary = {
                "id": rec["id"],
                "created_at": rec.get("created_at"),
                "decision": rec.get("decision"),
                "state_id": rec.get("state_id"),
                **state_staleness(rec),
            }
        return {
            **base,
            "state_id": sid,
            "snapshot_id": snap,
            "analysis": analysis,
            "pending": pending,
            "stale": stale,
            "job_id": job_id,
            "recommendation": summary,
        }

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
        checks["live_source"] = live_source_status()
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
            "horizon_max": settings.horizon_max,
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
        limit: int = Query(50, ge=1, le=1000),
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
        # engine probabilities and the official predictor side by side, never merged (ADR-0001 #20)
        price_risk: dict[str, Any] = {"engine": None, "official": None}
        probs = svc.price_probs(ctx)
        if probs is not None and code in set(probs["player_code"]):
            row = probs[probs["player_code"] == code].iloc[0]
            price_risk["engine"] = {
                "p_rise": float(row["p_rise"]),
                "p_fall": float(row["p_fall"]),
                "model": price_spec()[1].ref,
            }
        off = official_signal(view, ctx.season)
        off = off[off["player_code"] == code]
        if len(off):
            price_risk["official"] = {
                "price_change_percent": float(off["official_price_change_percent"].iloc[0]),
                "captured_at": _jsonable(off["official_captured_at"].iloc[0]),
                "source": "fantasy.premierleague.com bootstrap (official predictor)",
            }
        return {
            "player": _records(reg)[0],
            "price_risk": price_risk,
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
        warm_analysis(body.manager_key, sid)
        return {"state_id": sid, "state": state.model_dump(mode="json"), "freshness": fresh()}

    @r.get("/fpl/entry/{entry_id}")
    def fpl_entry(
        request: Request, entry_id: int = PathParam(..., ge=1, le=20_000_000)
    ) -> dict[str, Any]:
        """Preview a public FPL entry (team name, points, rank) so a user can confirm it is theirs
        before importing. Public data only; nothing is stored."""
        if principal_of(request).kind not in ("user", "operator"):
            raise AuthError(401, "sign in to import an FPL team", "session_required")
        if not settings.live_sync_enabled:
            raise HTTPException(
                503, "live sync disabled by configuration; enter the squad manually"
            )
        try:
            _, entry = FplApiClient.from_config(sources_config()).entry(entry_id)
        except HttpStatusError as exc:
            if exc.status == 404:
                raise HTTPException(404, f"no FPL manager with ID {entry_id}") from exc
            raise HTTPException(
                502, f"the FPL API refused the request (HTTP {exc.status})"
            ) from exc
        except SourceUnavailableError as exc:
            raise HTTPException(503, "live FPL API unavailable from this deployment") from exc
        except ValidationError as exc:
            log.warning("fpl_api_contract_violation", errors=exc.error_count())
            raise HTTPException(
                502, "the FPL API answered, but its payload failed validation"
            ) from exc
        return {
            "entry_id": entry.id,
            "team_name": entry.name,
            "started_event": entry.started_event,
            "current_event": entry.current_event,
            "overall_points": entry.summary_overall_points,
            "overall_rank": entry.summary_overall_rank,
            "source": "fantasy.premierleague.com (public entry endpoint)",
        }

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
        except HttpStatusError as exc:
            if exc.status == 404:
                raise HTTPException(404, f"no FPL manager with ID {body.manager_id}") from exc
            raise HTTPException(
                502, f"the FPL API refused the request (HTTP {exc.status})"
            ) from exc
        except SourceUnavailableError as exc:  # network policy, upstream outage, HTTP error
            return JSONResponse(
                {
                    "detail": "live FPL API unavailable from this deployment; serving degraded "
                    f"mode — enter the squad manually via POST {API}/squad",
                    "error": type(exc).__name__,
                    "degraded": True,
                },
                status_code=503,
            )
        except ValidationError as exc:  # the upstream payload broke its typed contract
            log.warning("fpl_api_contract_violation", errors=exc.error_count())
            return JSONResponse(
                {
                    "detail": "the FPL API answered, but its payload failed contract validation "
                    "(upstream schema change?); enter the squad manually meanwhile",
                    "error": "ValidationError",
                    "degraded": True,
                },
                status_code=502,
            )
        sid = svc.states.save(body.manager_key, report.state, "fpl_api")
        warm_analysis(body.manager_key, sid)
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
        pref = (
            Preferences(
                locked=frozenset(prefs.locked),
                banned=frozenset(prefs.banned),
                max_transfers_per_gw=prefs.max_transfers_per_gw,
            )
            if prefs
            else Preferences()
        )
        return ctx, fc, optimization_problem(svc, ctx, fc, st, h, profile, pref)

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
            "proven_optimal": sol.proven_optimal,
            "optimality": sol.optimality(),
            "config_ref": cfg.config_ref,
            "freshness": fresh(),
        }

    @r.post("/lineup")
    def lineup(body: s.LineupIn) -> dict[str, Any]:
        _, st = load_state(body.manager_key)
        ctx = svc.context()
        fc = svc.forecast_for(ctx, 1)
        chip = ChipType(body.chip) if body.chip else None
        return {**lineup_and_captaincy(svc, ctx, fc, st, chip), "freshness": fresh()}

    @r.post("/replacement")
    @r.post("/replacements")
    def replacement(body: s.ReplacementIn) -> dict[str, Any]:
        _, st = load_state(body.manager_key)
        ctx, fc, prob = _problem(st, body.profile, body.horizon)
        full = svc.table(ctx, fc, svc.horizon(body.horizon), st)
        prob = replace(prob, players=full)
        res = find_replacements(
            prob,
            body.out_player,
            fc.simulation,
            fc.summary,
            n_return=body.candidates,
            workers=settings.solver_workers,
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
                    "optimality": c.solution.optimality(),
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
        out["chips"] = [
            c.model_dump() for c in plan_chips(prob, base, fc, workers=settings.solver_workers)
        ]
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
        latest_sid, _ = load_state(body.manager_key)
        sid = body.state_id or latest_sid
        assert svc.states is not None
        if body.state_id and svc.states.get(sid, body.manager_key) is None:
            raise HTTPException(404, f"squad state {sid} is not stored for this manager")
        params = {**body.model_dump(exclude={"state_id"}), "state_id": sid}
        params["horizon"] = svc.horizon(body.horizon)
        job = jobs.submit("recommendation", params)
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
        return {**rec, **state_staleness(rec), "names": rec_names(rec), "freshness": fresh()}

    @r.get("/recommendations/{rec_id}")
    def get_rec(request: Request, rec_id: str) -> dict[str, Any]:
        rec = owner_of_rec(request, rec_id)
        return {**rec, **state_staleness(rec), "names": rec_names(rec), "freshness": fresh()}

    def state_staleness(rec: dict[str, Any]) -> dict[str, Any]:
        """Whether the manager's squad changed since this recommendation was computed. The
        record names the exact state it evaluated; a newer saved state means the plan no longer
        describes the current squad and should be regenerated."""
        latest = svc.states.latest(rec["manager_key"]) if svc.states is not None else None
        latest_id = latest[0] if latest else None
        used = rec.get("state_id")
        return {
            "stale_state": bool(used and latest_id and used != latest_id),
            "latest_state_id": latest_id,
        }

    def rec_names(rec: dict[str, Any]) -> dict[str, str | None]:
        """Names of every player an alternative sells or buys (incoming players are not in the
        manager's squad, so the UI cannot name them from the squad)."""
        codes = {
            int(c)
            for a in rec.get("alternatives") or []
            for c in [*(a.get("sells") or []), *(a.get("buys") or [])]
        }
        names = svc.data.names(svc.context().season)
        return {str(c): names.get(c) for c in sorted(codes)}

    @r.get("/decisions")
    def decisions(manager_key: str = Query(..., max_length=64)) -> dict[str, Any]:
        need_db()
        assert svc.recs is not None
        return {"journal": svc.recs.journal(manager_key)}

    @r.post("/decisions/{rec_id}/feedback")
    def feedback(request: Request, rec_id: str, body: s.FeedbackIn) -> dict[str, Any]:
        owner_of_rec(request, rec_id)
        assert svc.recs is not None
        try:
            svc.recs.feedback(rec_id, body.followed, body.note, body.realized_points)
        except KeyError as exc:
            raise HTTPException(404, f"recommendation {rec_id} not found") from exc
        return {"recorded": True}

    # ------------------------------------------------------------------ backtests & jobs
    @r.post("/backtests")
    def start_backtest(request: Request, body: s.BacktestIn) -> JSONResponse:
        require_operator(principal_of(request))
        job = jobs.submit("backtest", body.model_dump())
        return JSONResponse({"job_id": job, "job": jobs.get(job)}, status_code=202)

    @r.get("/backtests")
    def list_backtests() -> dict[str, Any]:
        published = None
        rep = settings.reports_path / "backtest.json"
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
        rep = settings.reports_path
        return {"reports": sorted(n for n in REPORTS if (rep / f"{n}.md").exists())}

    @r.get("/reports/{name}")
    def get_report(name: str) -> dict[str, Any]:
        if name not in REPORTS:  # allow-list: no path traversal into the filesystem
            raise HTTPException(404, f"unknown report {name}")
        rep = settings.reports_path
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
        figs = settings.reports_path / "figures"
        allowed = {p.name for p in figs.glob("*.svg")} if figs.exists() else set()
        if name not in allowed:
            raise HTTPException(404, f"unknown figure {name}")
        return Response((figs / name).read_text(encoding="utf-8"), media_type="image/svg+xml")

    @r.get("/models")
    def models_status() -> dict[str, Any]:
        rep = settings.reports_path
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
        if body.webhook_url:
            hosts = [*load_notification_config().delivery.webhook_allowed_hosts]
            try:
                validate_webhook_url(body.webhook_url, [*hosts, *settings.webhook_allowed_hosts])
            except UnsafeUrl as exc:
                metrics.security_events.labels("webhook_rejected").inc()
                log.warning("security_event", kind="webhook_rejected", reason=str(exc))
                raise HTTPException(422, f"webhook_url rejected: {exc}") from exc
        with session_scope(svc.engine) as ses:
            row = ses.get(m.ManagerSettingsRow, manager_key)
            if row is None:
                ses.add(
                    m.ManagerSettingsRow(manager_key=manager_key, settings_json=body.model_dump())
                )
            else:
                row.settings_json = body.model_dump()
        return {"manager_key": manager_key, "settings": body.model_dump()}

    # ------------------------------------------------------------------ notifications (§74)
    @r.post("/notifications/evaluate")
    def evaluate_notifications(body: s.AlertsIn) -> JSONResponse:
        need_db()
        job = jobs.submit("alerts", {"manager_key": body.manager_key, "at": _minute()})
        return JSONResponse({"job_id": job, "job": jobs.get(job)}, status_code=202)

    @r.get("/notifications")
    def list_notifications(
        manager_key: str = Query(..., max_length=64, pattern=KEY_PATTERN),
        unread_only: bool = False,
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        need_db()
        assert svc.notifications is not None
        return {
            "notifications": svc.notifications.fetch(manager_key, unread_only, limit),
            "freshness": fresh(),
        }

    @r.post("/notifications/read")
    def mark_read(
        body: s.MarkReadIn, manager_key: str = Query(..., max_length=64, pattern=KEY_PATTERN)
    ) -> dict[str, Any]:
        need_db()
        assert svc.notifications is not None
        return {"updated": svc.notifications.mark(manager_key, body.ids, "read_at")}

    # ------------------------------------------------------------------ accounts (beta)
    def need_users() -> UserStore:
        if users is None:
            raise HTTPException(503, "database not configured (FPL_DATABASE_URL)")
        return users

    @r.post("/auth/invites")
    def create_invite(request: Request, body: s.InviteIn) -> dict[str, Any]:
        """Operator-only: an invitation code, shown once (only its hash is stored)."""
        require_operator(principal_of(request))
        code = need_users().create_invite(body.label, body.days)
        return {"invite_code": code, "label": body.label, "days": body.days}

    @r.post("/auth/register")
    def register(body: s.RegisterIn) -> dict[str, Any]:
        u = need_users()
        uid = u.register(body.invite_code, body.email, body.password)
        token, who = u.login(body.email, body.password)
        audit(svc.engine, "auth.register", uid, "u:" + uid)
        return {"session_token": token, "user": u.user(who.user_id or uid)}

    @r.post("/auth/login")
    def login(body: s.LoginIn) -> dict[str, Any]:
        u = need_users()
        try:
            token, who = u.login(body.email, body.password)
        except AuthError:
            metrics.security_events.labels("login_failed").inc()
            log.warning("security_event", kind="login_failed")
            raise
        return {"session_token": token, "user": u.user(who.user_id or "")}

    @r.post("/auth/logout")
    def logout(request: Request) -> dict[str, Any]:
        require_user(principal_of(request))
        auth = request.headers.get("authorization", "")
        need_users().logout(auth[7:].strip())
        return {"signed_out": True}

    @r.get("/auth/me")
    def me(request: Request) -> dict[str, Any]:
        who = require_user(principal_of(request))
        user = need_users().user(who.user_id or "")
        if user is None:
            raise HTTPException(401, "session expired or invalid")
        return {"user": user}

    @r.post("/auth/me/preferences")
    def preferences(request: Request, body: s.AccountPreferencesIn) -> dict[str, Any]:
        who = require_user(principal_of(request))
        need_users().set_preferences(who.user_id or "", body.analytics_opt_out)
        return {"user": need_users().user(who.user_id or "")}

    @r.delete("/auth/me")
    def delete_account(request: Request) -> dict[str, Any]:
        """Erase the account and every manager it owns (data, then ownership, then user)."""
        who = require_user(principal_of(request))
        u = need_users()
        assert svc.engine is not None and who.user_id is not None
        erased: dict[str, int] = {}
        for mgr in u.managers(who.user_id):
            counts = delete_manager(svc.engine, mgr["manager_key"], _actor(request))
            for k, v in counts.items():
                erased[k] = erased.get(k, 0) + v
        account = u.delete_user(who.user_id)
        return {"deleted": {**erased, **{f"account_{k}": v for k, v in account.items()}}}

    @r.get("/auth/managers")
    def list_managers(request: Request) -> dict[str, Any]:
        who = require_user(principal_of(request))
        return {"managers": need_users().managers(who.user_id or "")}

    @r.post("/auth/managers")
    def create_manager(request: Request, body: s.ManagerCreateIn) -> dict[str, Any]:
        """A new manager key owned by the signed-in user; the only way a user gets one."""
        who = require_user(principal_of(request))
        key = need_users().create_manager(who.user_id or "", body.label)
        return {"manager_key": key, "label": body.label}

    # ------------------------------------------------------------------ product analytics
    @r.post("/events")
    def record_event(request: Request, body: s.EventIn) -> dict[str, Any]:
        """A first-party product event for the signed-in user (allow-listed names and
        properties only; dropped for users who opted out; pruned after 90 days)."""
        who = principal_of(request)
        if who.kind != "user" or who.user_id is None or svc.engine is None:
            return {"recorded": False}  # anonymous or operator traffic is never measured
        ok = analytics.record(svc.engine, who.user_id, body.event, dict(body.props))
        return {"recorded": ok}

    @r.get("/events/summary")
    def events_summary(request: Request, days: int = Query(28, ge=1, le=365)) -> dict[str, Any]:
        require_operator(principal_of(request))
        need_db()
        assert svc.engine is not None
        return analytics.summary(svc.engine, days)

    # ------------------------------------------------------------------ privacy (§75)
    @r.get("/managers/{manager_key}/export")
    def export_data(
        request: Request, manager_key: str = PathParam(..., max_length=64, pattern=KEY_PATTERN)
    ) -> dict[str, Any]:
        need_db()
        assert svc.engine is not None
        data = export_manager(svc.engine, manager_key)
        audit(svc.engine, "privacy.export_manager", manager_key, _actor(request))
        return {"exported_at": datetime.now(UTC).isoformat(), **_jsonable(data)}

    @r.delete("/managers/{manager_key}")
    def delete_data(
        request: Request, manager_key: str = PathParam(..., max_length=64, pattern=KEY_PATTERN)
    ) -> dict[str, Any]:
        need_db()
        assert svc.engine is not None
        counts = delete_manager(svc.engine, manager_key, _actor(request))
        if users is not None:
            users.release_manager(manager_key)
        log.info("privacy_delete", deleted=sum(counts.values()))
        return {"deleted": counts}

    # ------------------------------------------------------------------ traceability (§76.2)
    @r.get("/recommendations/{rec_id}/trace")
    def trace_rec(request: Request, rec_id: str = PathParam(..., max_length=80)) -> dict[str, Any]:
        owner_of_rec(request, rec_id)
        assert svc.engine is not None
        out = trace(svc.engine, rec_id)
        if out is None:
            raise HTTPException(404, f"recommendation {rec_id} not found")
        return _jsonable(out)

    @r.get("/jobs/{job_id}")
    def job_status(request: Request, job_id: str) -> dict[str, Any]:
        j = jobs.get(job_id)
        if j is None:
            raise HTTPException(404, f"job {job_id} not found")
        owner = jobs.manager_of(job_id)
        if owner:
            require_owner(principal_of(request), users, owner)
        return j

    app.include_router(r)

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        ctx = svc.context()
        age = None
        if ctx.latest_source_at is not None:
            age = (datetime.now(UTC) - ctx.latest_source_at).total_seconds() / 3600.0
        try:
            metrics.refresh(svc.engine, age, settings.reports_path)
        except Exception:  # a scrape must not fail because one gauge source is down
            log.warning("metrics_refresh_failed", exc_info=True)
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app


def _minute() -> str:
    """Evaluation bucket: identical evaluate requests within a minute share one job."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")


def _actor(request: Request) -> str | None:
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal) and principal.kind == "user" and principal.user_id:
        return "u:" + principal.user_id
    key = request.headers.get("x-api-key")
    return hash_key(key)[:16] if key else None


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
