"""``fpl-worker``: the RQ job worker and the scheduler (ADR-0009, §74, §80).

* ``fpl-worker work`` consumes the ``fpl`` queue (jobs submitted by the API or the scheduler).
* ``fpl-worker schedule`` ticks every ``tick_seconds`` and submits the due tasks from
  ``config/schedules/default.yaml``. Each submission carries its time bucket, so a task is
  submitted at most once per bucket however many schedulers run or restart.
* ``fpl-worker run-once <task>`` runs one task immediately (operations / smoke tests).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import redis
import structlog
from pydantic import BaseModel, ConfigDict
from rq import Queue, Worker
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
    return {"status": "succeeded" if rc == 0 else "failed", "exit_code": rc}


TASKS: dict[str, Callable[[AppServices, JobBackend, str], dict[str, Any]]] = {
    "forecast_precompute": task_forecast_precompute,
    "alerts": task_alerts,
    "live_refresh": task_live_refresh,
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
    svc = services()
    if not svc.settings.redis_url:
        raise SystemExit("FPL_REDIS_URL is required for the worker")
    conn = redis.Redis.from_url(svc.settings.redis_url)
    Worker([Queue("fpl", connection=conn)], connection=conn).work(burst=args.burst)
    return 0


def _schedule(args: argparse.Namespace) -> int:
    sched = load_schedule(args.schedule)
    svc = services()
    jobs = JobBackend(svc)
    last: dict[str, str] = {}
    while True:
        now = datetime.now(UTC)
        for name, b in due_tasks(sched, last, now):
            run_task(name, svc, jobs, b)
            last[name] = b
        if args.once:
            return 0
        time.sleep(sched.tick_seconds)


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
    args = p.parse_args(argv)
    configure_logging(os.environ.get("FPL_LOG_LEVEL", "INFO"))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
