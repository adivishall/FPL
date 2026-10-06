"""``fpl-worker``: the RQ job worker and the scheduler (ADR-0009, §74, §80).

* ``fpl-worker work`` consumes the ``fpl`` queue (jobs submitted by the API or the scheduler).
* ``fpl-worker schedule`` ticks every ``tick_seconds`` and submits the due tasks from
  ``config/schedules/default.yaml``. Each submission carries its time bucket, so a task is
  submitted at most once per bucket however many schedulers run or restart.
* ``fpl-worker run-once <task>`` runs one task immediately (operations / smoke tests).
* ``fpl-worker healthcheck work|schedule`` is the container liveness probe: the worker's own RQ
  heartbeat in Redis must be within RQ's key expiry; the scheduler must have ticked recently
  (heartbeat file).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import redis
import structlog
from prometheus_client import CollectorRegistry, multiprocess, start_http_server
from pydantic import BaseModel, ConfigDict
from rq import Queue, Worker
from rq.defaults import DEFAULT_WORKER_TTL
from rq.utils import utcparse
from sqlalchemy import select

from fpl_api.container import AppServices
from fpl_api.jobs import JobBackend, services
from fpl_api.services import ForecastUnavailable
from fpl_domain.config import load_versioned_config
from fpl_ingestion import cli as ingest_cli
from fpl_ingestion.logs import configure_logging
from fpl_storage import models as m
from fpl_storage.db import session_scope

log = structlog.get_logger("fpl_worker")
# liveness marker only (its mtime is read; contents are never trusted)
SCHEDULER_HEARTBEAT = Path(
    os.environ.get("FPL_SCHEDULER_HEARTBEAT") or Path(tempfile.gettempdir()) / "fpl-scheduler.alive"
)
# RQ heartbeats every job-monitoring interval while busy but only once per blocking dequeue
# (worker_ttl - 15 s) while idle, and lets the key expire worker_ttl + 60 s after a beat.
WORKER_HEARTBEAT_MAX_AGE_S = float(DEFAULT_WORKER_TTL + 60)


class TaskSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    every_minutes: int
    enabled: bool = True


class Schedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str
    tick_seconds: int = 60
    tasks: dict[str, TaskSpec]


def load_schedule(name: str = "default") -> Schedule:
    return Schedule.model_validate(load_versioned_config("schedules", name).data)


def bucket(now: datetime, every_minutes: int) -> str:
    """The time bucket a run belongs to (idempotency key for scheduled submissions)."""
    epoch_min = int(now.timestamp() // 60)
    start = epoch_min - epoch_min % every_minutes
    return datetime.fromtimestamp(start * 60, UTC).strftime("%Y-%m-%dT%H:%M")


def due_tasks(schedule: Schedule, last: dict[str, str], now: datetime) -> list[tuple[str, str]]:
    """(task, bucket) pairs whose current bucket has not been submitted yet."""
    out = []
    for name, spec in schedule.tasks.items():
        if not spec.enabled:
            continue
        b = bucket(now, spec.every_minutes)
        if last.get(name) != b:
            out.append((name, b))
    return out


# ----------------------------------------------------------------------------- tasks


def task_forecast_precompute(svc: AppServices, jobs: JobBackend, b: str) -> dict[str, Any]:
    ctx = svc.context()
    try:
        svc.forecasts.get(ctx.season, ctx.gameweek, svc.settings.forecast_horizon, compute=False)
        return {"status": "cached", "season": ctx.season, "gameweek": ctx.gameweek}
    except ForecastUnavailable:
        job = jobs.submit("forecast", {"season": ctx.season, "gameweek": ctx.gameweek})
        return {"status": "submitted", "job_id": job}


def task_alerts(svc: AppServices, jobs: JobBackend, b: str) -> dict[str, Any]:
    if svc.engine is None:
        return {"status": "skipped", "reason": "no database"}
    with session_scope(svc.engine) as s:
        keys = sorted(
            {
                str(k)
                for k in s.scalars(select(m.ManagerStateRow.state_json["manager_key"].astext)).all()
                if k
            }
        )
    ids = [jobs.submit("alerts", {"manager_key": k, "at": b}) for k in keys]
    return {"status": "submitted", "managers": len(keys), "jobs": ids}


def task_live_refresh(svc: AppServices, jobs: JobBackend, b: str) -> dict[str, Any]:
    ctx = svc.context()
    rc = ingest_cli.main(["live", "--season", ctx.season])
    if rc != 0:  # degraded mode: keep serving the last validated snapshot (ADR-0001 #5)
        return {"status": "degraded", "exit_code": rc}
    rc = ingest_cli.main(["export-snapshot"])
    if rc != 0:
        return {"status": "failed", "exit_code": rc}
    # precompute the new snapshot's serving forecast now, so the API can promote it
    svc.data.refresh()
    return {
        "status": "succeeded",
        "exit_code": rc,
        "forecast": task_forecast_precompute(svc, jobs, b),
    }


def task_retention(svc: AppServices, jobs: JobBackend, b: str) -> dict[str, Any]:
    # a job (not inline): its outcome is recorded, counted and alerted on like any other
    return {"status": "submitted", "job_id": jobs.submit("retention", {"at": b})}


TASKS: dict[str, Callable[[AppServices, JobBackend, str], dict[str, Any]]] = {
    "forecast_precompute": task_forecast_precompute,
    "alerts": task_alerts,
    "live_refresh": task_live_refresh,
    "retention": task_retention,
}


def run_task(name: str, svc: AppServices, jobs: JobBackend, b: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        out = TASKS[name](svc, jobs, b)
    except Exception as exc:  # one failing task never stops the scheduler
        log.exception("scheduled_task_failed", task=name, bucket=b)
        out = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    out["seconds"] = round(time.perf_counter() - t0, 2)
    log.info("scheduled_task", task=name, bucket=b, **out)
    return out


# ----------------------------------------------------------------------------- commands


def _work(args: argparse.Namespace) -> int:
    serve_worker_metrics()  # first: metric objects created below write into its directory
    svc = services()
    if not svc.settings.redis_url:
        raise SystemExit("FPL_REDIS_URL is required for the worker")
    conn = redis.Redis.from_url(svc.settings.redis_url)
    Worker([Queue("fpl", connection=conn)], connection=conn, name=worker_name()).work(
        burst=args.burst
    )
    return 0


def worker_name() -> str:
    """Named after the container (hostname) so its liveness probe can find its own heartbeat,
    and unique per start: after a crash the restarted container (same hostname, PID 1) must not
    collide with its own stale registration, which lives in Redis until its heartbeat expires."""
    return f"{socket.gethostname()}.{os.getpid()}.{uuid.uuid4().hex[:8]}"


def serve_worker_metrics(port: int | None = None) -> bool:
    """Expose the metrics of every job this worker ran (§76.1).

    RQ runs each job in a forked work horse that exits afterwards, so in-process counters would
    be lost. With ``PROMETHEUS_MULTIPROC_DIR`` set (compose sets it for the worker), every horse
    writes its counters to memory-mapped files there and this parent process serves their
    aggregate on ``FPL_WORKER_METRICS_PORT`` (default 9101) for Prometheus to scrape.
    """
    path = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not path:
        return False
    d = Path(path)
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.db"):  # counters of a previous container run
        f.unlink()
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    start_http_server(
        port or int(os.environ.get("FPL_WORKER_METRICS_PORT", "9101")), registry=registry
    )
    return True


def _schedule(args: argparse.Namespace) -> int:
    sched = load_schedule(args.schedule)
    svc = services()
    jobs = JobBackend(svc)
    last: dict[str, str] = {}
    while True:
        now = datetime.now(UTC)
        reaped = jobs.reap_expired(now)
        if reaped:
            log.warning("jobs_abandoned", count=reaped)
        for name, b in due_tasks(sched, last, now):
            run_task(name, svc, jobs, b)
            last[name] = b
        SCHEDULER_HEARTBEAT.touch()
        if args.once:
            return 0
        time.sleep(sched.tick_seconds)


def _healthcheck(args: argparse.Namespace) -> int:
    """Exit 0 when this container's process is alive and making progress."""
    if args.role == "schedule":
        sched = load_schedule("default")
        try:
            age = time.time() - SCHEDULER_HEARTBEAT.stat().st_mtime
        except FileNotFoundError:
            return 1
        # a tick can include a long task (live refresh); allow a few ticks plus slack
        return 0 if age <= 3 * sched.tick_seconds + 600 else 1
    url = os.environ.get("FPL_REDIS_URL")
    if not url:
        return 1
    conn = redis.Redis.from_url(url, socket_timeout=3)
    # This container's own worker keys, not RQ's global worker set: after the host sleeps past
    # the key's TTL, RQ drops the name from that set while the live worker's next heartbeat
    # recreates the key, so the set can be empty although the worker keeps consuming jobs.
    for key in conn.scan_iter(
        match=f"{Worker.redis_worker_namespace_prefix}{socket.gethostname()}.*"
    ):
        raw = conn.hget(key, "last_heartbeat")
        if raw is None:
            continue
        beat = utcparse(raw.decode() if isinstance(raw, bytes) else raw)
        age = (datetime.now(UTC) - beat.replace(tzinfo=beat.tzinfo or UTC)).total_seconds()
        if age <= WORKER_HEARTBEAT_MAX_AGE_S:
            return 0
    return 1


def _run_once(args: argparse.Namespace) -> int:
    svc = services()
    out = run_task(args.task, svc, JobBackend(svc), bucket(datetime.now(UTC), 1))
    print(json.dumps(out, default=str))
    return 0 if out["status"] in ("succeeded", "submitted", "cached", "skipped") else 4


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fpl-worker")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("work", help="consume the job queue")
    w.add_argument("--burst", action="store_true", help="exit when the queue is empty")
    w.set_defaults(func=_work)
    s = sub.add_parser("schedule", help="submit scheduled tasks")
    s.add_argument("--schedule", default="default")
    s.add_argument("--once", action="store_true", help="one tick, then exit")
    s.set_defaults(func=_schedule)
    r = sub.add_parser("run-once", help="run one scheduled task now")
    r.add_argument("task", choices=sorted(TASKS))
    r.set_defaults(func=_run_once)
    hc = sub.add_parser("healthcheck", help="container liveness probe")
    hc.add_argument("role", choices=["work", "schedule"])
    hc.set_defaults(func=_healthcheck)
    args = p.parse_args(argv)
    configure_logging(os.environ.get("FPL_LOG_LEVEL", "INFO"))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
