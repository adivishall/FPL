"""Asynchronous jobs (ADR-0009): durable job records in PostgreSQL, execution inline or via RQ.

``JobBackend.submit`` records the job (``queued``) and either executes it immediately (inline
backend: tests, single-process development) or enqueues ``fpl_api.jobs.run_job`` on Redis for
the worker. Every job kind is a plain function of (services, params) → result reference, so both
backends run the same code. Identical requests are de-duplicated by a request hash while queued
or running — but only within the kind's lease: a job whose worker died (OOM kill, container
replaced) never blocks its request forever; it is marked failed ("abandoned") and the request is
re-submitted as a new attempt. RQ's failure callback records failures RQ raises inside the work
horse (e.g. job timeouts) immediately; a SIGKILLed horse (OOM) runs no callback in RQ 2.x and
is recovered by the lease. Failed jobs are terminal and observable via
``GET /jobs/{id}``; retrying means submitting again (the scheduler does so every bucket).

Every failure is classified (``failure``): ``error`` — the job's own code raised (a bug or bad
input; the traceback is recorded) — or ``interrupted`` — the job never finished because its
process was killed, lost or timed out (OOM, container replaced, host sleep / VM clock jump).
An interrupted job wrote no result: every job publishes its outcome only at the end (database
transaction, atomic file renames), so re-submitting it is always safe.
"""

from __future__ import annotations

import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import redis
import structlog
from rq import Callback, Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.timeouts import JobTimeoutException
from sqlalchemy import select

from fpl_api.alerts import evaluate_alerts
from fpl_api.container import AppServices, build_recommendation
from fpl_api.retention import prune
from fpl_api.services import mark_serving_ready
from fpl_api.settings import Settings
from fpl_backtest.runner import default_strategies, run_season
from fpl_domain.hashing import content_hash
from fpl_optimizer.problem import load_optimizer_config
from fpl_simulation.engine import SimulationConfig
from fpl_storage import models as m
from fpl_storage.db import session_scope

JobFn = Callable[[AppServices, dict[str, Any]], str]
log = structlog.get_logger("fpl_api.jobs")


def _job_forecast(svc: AppServices, p: dict[str, Any]) -> str:
    ctx = svc.context()
    snapshot = svc.data.snapshot_id
    season, gw = p.get("season", ctx.season), p.get("gameweek", ctx.gameweek)
    fc = svc.forecasts.get(season, gw, svc.horizon(p.get("horizon")), p.get("n_sims"), compute=True)
    key = str(fc.provenance.get("forecast_key", fc.run_id))
    serving = (season, gw) == (ctx.season, ctx.gameweek) and p.get("n_sims") is None
    if serving and fc.provenance.get("data_snapshot_id") == snapshot:
        mark_serving_ready(svc.settings, snapshot, key)  # the API may now promote it
    return key


def _job_recommendation(svc: AppServices, p: dict[str, Any]) -> str:
    if svc.states is None:
        raise RuntimeError("database required")
    state_id = p.get("state_id")
    if not state_id:
        raise LookupError(f"recommendation job for {p['manager_key']} names no squad state")
    state = svc.states.get(state_id, p["manager_key"])
    if state is None:
        # Never substitute another state for the one the request named (it may have been
        # erased since): the result would be traced to a squad that was not evaluated.
        raise LookupError(f"squad state {state_id} is not available for {p['manager_key']}")
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


def _job_retention(svc: AppServices, p: dict[str, Any]) -> str:
    return prune(svc.settings, svc.engine).summary()


def _job_alerts(svc: AppServices, p: dict[str, Any]) -> str:
    out = evaluate_alerts(svc, p["manager_key"])
    return f"alerts:{len(out['new_ids'])}/{len(out['alerts'])}"


JOB_KINDS: dict[str, JobFn] = {
    "alerts": _job_alerts,
    "forecast": _job_forecast,
    "recommendation": _job_recommendation,
    "backtest": _job_backtest,
    "retention": _job_retention,
}
# Upper bound on a healthy run of each kind: RQ's job timeout and the de-duplication lease.
JOB_LEASE: dict[str, timedelta] = {
    "alerts": timedelta(minutes=15),
    "forecast": timedelta(minutes=30),
    "recommendation": timedelta(minutes=30),
    "backtest": timedelta(hours=6),
    "retention": timedelta(minutes=15),
}
ABANDONED = "abandoned: no result within the job lease (worker lost or killed)"
RQ_GRACE = timedelta(minutes=2)
_INTERRUPTED = ("abandoned:", "interrupted:")


def failure_kind(error: str | None) -> str | None:
    """``interrupted`` (environment: process killed, lost or timed out) or ``error`` (the job's
    code raised) for a failed job's recorded error; None if there is no error."""
    if error is None:
        return None
    return "interrupted" if error.startswith(_INTERRUPTED) else "error"


def _expired(r: m.JobRow, now: datetime) -> bool:
    since = r.started_at if r.status == "running" and r.started_at else r.created_at
    return now - since > JOB_LEASE[r.kind]


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
        now = datetime.now(UTC)
        with session_scope(self.svc.engine) as s:
            dups = s.scalars(
                select(m.JobRow)
                .where(
                    m.JobRow.request_hash == h,
                    m.JobRow.status.in_(("queued", "running", "succeeded")),
                )
                .order_by(m.JobRow.created_at.desc())
            ).all()
            for dup in dups:
                if dup.status == "succeeded" or not _expired(dup, now):
                    return dup.id
                dup.status, dup.error, dup.finished_at = "failed", ABANDONED, now
                self.svc.metrics.job_failures.labels(dup.kind, "interrupted").inc()
                log.warning("job_interrupted", job_id=dup.id, kind=dup.kind, error=ABANDONED)
            job_id = "job_" + uuid.uuid4().hex[:16]
            s.add(
                m.JobRow(id=job_id, kind=kind, status="queued", request_hash=h, request_json=params)
            )
        if self._queue is not None:
            self._queue.enqueue(
                "fpl_api.jobs.run_job",
                job_id,
                job_id=job_id,  # same id in Redis: the reaper can ask RQ about this job
                job_timeout=int(JOB_LEASE[kind].total_seconds()),
                on_failure=Callback(on_rq_failure),
            )
        else:
            execute(self.svc, job_id)
        return job_id

    def reap_expired(self, now: datetime | None = None) -> int:
        """Close every queued/running job past its lease (worker lost). Requests are keyed by
        snapshot, so after a snapshot swap nobody re-submits them — the scheduler sweeps."""
        if self.svc.engine is None:
            return 0
        now = now or datetime.now(UTC)
        n = 0
        with session_scope(self.svc.engine) as s:
            for r in s.scalars(select(m.JobRow).where(m.JobRow.status.in_(("queued", "running")))):
                lost = self._rq_lost(r, now)
                if r.kind in JOB_LEASE and (lost or _expired(r, now)):
                    r.status, r.finished_at = "failed", now
                    r.error = f"abandoned: {lost}" if lost else ABANDONED
                    self.svc.metrics.job_failures.labels(r.kind, "interrupted").inc()
                    log.warning("job_interrupted", job_id=r.id, kind=r.kind, error=r.error)
                    n += 1
        return n

    def _rq_lost(self, r: m.JobRow, now: datetime) -> str | None:
        """Why RQ no longer runs this job (worker killed, container replaced), if it doesn't."""
        if self._queue is None or now - r.created_at < RQ_GRACE:
            return None  # inline backend, or just submitted (enqueue may still be in flight)
        try:
            status = Job.fetch(r.id, connection=self._queue.connection).get_status()
        except NoSuchJobError:
            return "unknown to the queue (lost from Redis)"
        name = getattr(status, "value", status)
        return f"queue reports {name}" if name in ("failed", "stopped", "canceled") else None

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
                "failure": failure_kind(r.error) if r.status == "failed" else None,
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
    except JobTimeoutException as exc:  # RQ's alarm inside the work horse: not a code error
        ref, status = None, "failed"
        error = f"interrupted: exceeded the {JOB_LEASE[kind]} job timeout ({exc})"
        svc.metrics.job_failures.labels(kind, "interrupted").inc()
    except Exception as exc:  # recorded, surfaced through GET /jobs/{id}
        ref, status = None, "failed"
        error = f"{type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=3)
        svc.metrics.job_failures.labels(kind, "error").inc()
    with session_scope(svc.engine) as s:
        r = s.get(m.JobRow, job_id)
        assert r is not None
        r.status, r.result_ref, r.error = status, ref, error
        r.finished_at, r.progress = datetime.now(UTC), 1.0


@lru_cache(maxsize=1)
def services() -> AppServices:
    """One service container per worker process (dataset, caches and metrics are reused)."""
    return AppServices.build(Settings())


def run_job(job_id: str) -> None:
    """RQ entry point (worker process)."""
    execute(services(), job_id)


def on_rq_failure(job: Any, connection: Any, typ: Any, value: Any, tb: Any) -> None:
    """RQ failure callback (e.g. job timeout inside the work horse) before ``execute`` recorded
    an outcome — record it now so the failure is visible and the request can be re-submitted."""
    job_id = job.args[0] if job.args else None
    svc = services()
    if job_id is None or svc.engine is None:
        return
    with session_scope(svc.engine) as s:
        r = s.get(m.JobRow, job_id)
        if r is None or r.status in ("succeeded", "failed"):
            return
        r.status, r.finished_at = "failed", datetime.now(UTC)
        name = getattr(typ, "__name__", typ)
        if isinstance(typ, type) and issubclass(typ, JobTimeoutException):
            r.error = f"interrupted: exceeded the {JOB_LEASE.get(r.kind)} job timeout ({value})"
        else:
            r.error = f"worker failure: {name}: {value}"
        reason = failure_kind(r.error) or "error"
        svc.metrics.job_failures.labels(r.kind, reason).inc()
        log.warning("job_failed_in_worker", job_id=r.id, kind=r.kind, failure=reason)
