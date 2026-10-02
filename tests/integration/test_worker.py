"""Queue path end to end: API job submission → Redis/RQ → worker → PostgreSQL (ADR-0009)."""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import redis
from rq import Queue, SimpleWorker

from fpl_api.container import AppServices
from fpl_api.jobs import JobBackend
from fpl_api.settings import Settings
from tests.fixtures_util import fixture_dataset

pytestmark = pytest.mark.skipif(shutil.which("redis-server") is None, reason="no redis-server")


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        ["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
    )
    url = f"redis://127.0.0.1:{port}/0"
    for _ in range(100):
        try:
            if redis.Redis.from_url(url).ping():
                break
        except redis.ConnectionError:
            time.sleep(0.05)
    yield url
    proc.terminate()
    proc.wait(10)


def test_rq_worker_executes_jobs_and_scheduler_is_idempotent(
    fresh_db: str, redis_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fpl_api.jobs as jobs_mod
    from fpl_worker import cli as worker_cli

    st = Settings(
        database_url=fresh_db,
        redis_url=redis_url,
        artifact_dir=tmp_path,
        n_sims=200,
        horizon_default=2,
        horizon_max=2,
        forecast_horizon=2,
    )
    svc = AppServices.build(st, fixture_dataset("2024-25", "2025-26", "2026-27"))
    monkeypatch.setattr(jobs_mod, "services", lambda: svc)  # the worker's container
    backend = JobBackend(svc)
    assert backend.mode == "rq"

    # scheduler tick: alerts for every manager with a stored squad (none yet → no jobs),
    # forecast precompute → one queued job; a second tick in the same bucket submits nothing new
    out = worker_cli.run_task("forecast_precompute", svc, backend, "b1")
    assert out["status"] == "submitted"
    job_id = out["job_id"]
    again = worker_cli.run_task("forecast_precompute", svc, backend, "b1")
    assert again["job_id"] == job_id  # de-duplicated by request hash
    assert backend.get(job_id)["status"] == "queued"

    conn = redis.Redis.from_url(redis_url)
    SimpleWorker([Queue("fpl", connection=conn)], connection=conn).work(burst=True)
    done = backend.get(job_id)
    assert done is not None and done["status"] == "succeeded", done
    assert done["result_ref"].startswith("fc_")
    # the worker computed and cached the forecast; the next tick finds it
    assert worker_cli.run_task("forecast_precompute", svc, backend, "b2")["status"] == "cached"
    # a failing job is recorded as failed (and counted), never lost
    bad = backend.submit("alerts", {"manager_key": "nobody", "at": "x"})
    monkeypatch.setattr(jobs_mod, "evaluate_alerts", lambda *_a, **_k: 1 / 0)
    SimpleWorker([Queue("fpl", connection=conn)], connection=conn).work(burst=True)
    failed = backend.get(bad)
    assert failed is not None and failed["status"] == "failed"
    assert "ZeroDivisionError" in failed["error"]
