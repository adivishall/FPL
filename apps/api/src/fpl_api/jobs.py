"""Asynchronous jobs (ADR-0009): durable job records in PostgreSQL, execution inline or via RQ.

``JobBackend.submit`` records the job (``queued``) and either executes it immediately (inline
backend: tests, single-process development) or enqueues ``fpl_api.jobs.run_job`` on Redis for
the worker. Every job kind is a plain function of (services, params) → result reference, so both
backends run the same code. Identical requests are de-duplicated by a request hash while queued
or running.
"""

from __future__ import annotations

import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import redis
from rq import Queue
from sqlalchemy import select

from fpl_api.container import AppServices, build_recommendation
from fpl_api.settings import Settings
from fpl_backtest.runner import default_strategies, run_season
from fpl_domain.hashing import content_hash
from fpl_optimizer.problem import load_optimizer_config
from fpl_simulation.engine import SimulationConfig
from fpl_storage import models as m
from fpl_storage.db import session_scope

JobFn = Callable[[AppServices, dict[str, Any]], str]


def _job_forecast(svc: AppServices, p: dict[str, Any]) -> str:
    ctx = svc.context()
    fc = svc.forecasts.get(
        p.get("season", ctx.season),
        p.get("gameweek", ctx.gameweek),
        svc.horizon(p.get("horizon")),
        p.get("n_sims"),
        compute=True,
    )
    return str(fc.provenance.get("forecast_key", fc.run_id))


def _job_recommendation(svc: AppServices, p: dict[str, Any]) -> str:
    if svc.states is None:
        raise RuntimeError("database required")
    latest = svc.states.latest(p["manager_key"])
    if latest is None:
        raise LookupError(f"no squad stored for {p['manager_key']}")
    state_id, state = latest
    rec_id, _ = build_recommendation(
        svc,
        p["manager_key"],
        state,
        state_id,
        profile=p.get("profile", "default"),
        horizon=p.get("horizon"),
        n_alternatives=p.get("n_alternatives", 3),
        run_stability=p.get("stability", True),
        run_scenarios=p.get("scenarios", True),
        run_chips=p.get("chips", True),
    )
    return str(rec_id)


def _job_backtest(svc: AppServices, p: dict[str, Any]) -> str:
    if svc.engine is None:
        raise RuntimeError("database required")
    cfg = load_optimizer_config(p.get("profile", "default"))
    season = p["season"]
    seasons = sorted(s for s in svc.data.ds["gameweeks"]["season"].unique() if s < season)
    df = run_season(
        svc.data.ds,
        season,
        default_strategies(cfg),
        seasons,
        horizon=svc.horizon(p.get("horizon")),
        retrain_every=p.get("retrain_every", 4),
        sim=SimulationConfig(n_sims=min(p.get("n_sims", 500), svc.settings.n_sims_max)),
        gameweeks=p.get("gameweeks"),
        cache_root=svc.settings.feature_store_dir,
        initial_cfg=cfg,
    )
    bt_id = "bt_" + content_hash({"p": p, "snap": svc.data.snapshot_id})[:16]
    summary = (df.groupby("strategy")["points"].sum().sort_values(ascending=False)).to_dict()
    with session_scope(svc.engine) as s:
        if s.get(m.BacktestRunRow, bt_id) is None:
            s.add(
                m.BacktestRunRow(
                    id=bt_id,
                    season=season,
                    from_gw=int(df["gw"].min()),
                    to_gw=int(df["gw"].max()),
                    cutoff_policy={"buffer_minutes": 90, "pit": "available_at <= cutoff"},
                    benchmark=",".join(sorted(df["strategy"].unique())),
                    model_version={"optimizer": cfg.config_ref},
                    data_snapshot_id=svc.data.snapshot_id,
                    config_json=p,
                    status="succeeded",
                    completed_at=datetime.now(UTC),
                    results_json={
                        "season_points": summary,
                        "records": df.drop(columns=["notes"]).to_dict("records"),
                    },
                )
            )
    return bt_id


JOB_KINDS: dict[str, JobFn] = {
    "forecast": _job_forecast,
    "recommendation": _job_recommendation,
    "backtest": _job_backtest,
}


class JobBackend:
    def __init__(self, svc: AppServices) -> None:
        self.svc = svc
        self._queue = None
        if svc.settings.redis_url:
            self._queue = Queue("fpl", connection=redis.Redis.from_url(svc.settings.redis_url))

    @property
    def mode(self) -> str:
        return "rq" if self._queue is not None else "inline"

    def submit(self, kind: str, params: dict[str, Any]) -> str:
        if kind not in JOB_KINDS:
            raise ValueError(f"unknown job kind {kind}")
        if self.svc.engine is None:
            raise RuntimeError("database required for jobs")
        h = content_hash({"kind": kind, "params": params, "snap": self.svc.data.snapshot_id})
        with session_scope(self.svc.engine) as s:
            dup = s.scalars(
                select(m.JobRow).where(
                    m.JobRow.request_hash == h,
                    m.JobRow.status.in_(("queued", "running", "succeeded")),
                )
            ).first()
            if dup is not None:
                return dup.id
            job_id = "job_" + uuid.uuid4().hex[:16]
            s.add(
                m.JobRow(id=job_id, kind=kind, status="queued", request_hash=h, request_json=params)
            )
        if self._queue is not None:
            self._queue.enqueue("fpl_api.jobs.run_job", job_id, job_timeout=6 * 3600)
        else:
            execute(self.svc, job_id)
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        if self.svc.engine is None:
            return None
        with session_scope(self.svc.engine) as s:
            r = s.get(m.JobRow, job_id)
            if r is None:
                return None
            return {
                "id": r.id,
                "kind": r.kind,
                "status": r.status,
                "progress": r.progress,
                "result_ref": r.result_ref,
                "error": r.error,
                "created_at": r.created_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }


def execute(svc: AppServices, job_id: str) -> None:
    assert svc.engine is not None
    with session_scope(svc.engine) as s:
        r = s.get(m.JobRow, job_id)
        if r is None:
            raise KeyError(job_id)
        r.status, r.started_at = "running", datetime.now(UTC)
        kind, params = r.kind, dict(r.request_json)
    try:
        ref = JOB_KINDS[kind](svc, params)
        status, error = "succeeded", None
    except Exception as exc:  # recorded, surfaced through GET /jobs/{id}
        ref, status = None, "failed"
        error = f"{type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=3)
    with session_scope(svc.engine) as s:
        r = s.get(m.JobRow, job_id)
        assert r is not None
        r.status, r.result_ref, r.error = status, ref, error
        r.finished_at, r.progress = datetime.now(UTC), 1.0


def run_job(job_id: str) -> None:
    """RQ entry point (worker process)."""
    execute(AppServices.build(Settings()), job_id)
