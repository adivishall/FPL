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
from rq import Queue, SimpleWorker, Worker
from rq.timeouts import JobTimeoutException

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
    from fpl_api.services import is_serving_ready

    assert is_serving_ready(svc.settings, svc.data.snapshot_id)  # API may promote it now
    # the worker computed and cached the forecast; the next tick finds it
    assert worker_cli.run_task("forecast_precompute", svc, backend, "b2")["status"] == "cached"
    # a failing job is recorded as failed (and counted), never lost
    bad = backend.submit("alerts", {"manager_key": "nobody", "at": "x"})
    monkeypatch.setattr(jobs_mod, "evaluate_alerts", lambda *_a, **_k: 1 / 0)
    SimpleWorker([Queue("fpl", connection=conn)], connection=conn).work(burst=True)
    failed = backend.get(bad)
    assert failed is not None and failed["status"] == "failed"
    assert "ZeroDivisionError" in failed["error"]
    assert failed["failure"] == "error"  # the job's own code raised: not an interruption
    # RQ's timeout alarm inside the work horse is an interruption, not a code error
    slow = backend.submit("alerts", {"manager_key": "nobody", "at": "y"})

    def _timeout(*_a: object, **_k: object) -> None:
        raise JobTimeoutException("Task exceeded maximum timeout value (900 seconds)")

    monkeypatch.setattr(jobs_mod, "evaluate_alerts", _timeout)
    SimpleWorker([Queue("fpl", connection=conn)], connection=conn).work(burst=True)
    timed = backend.get(slow)
    assert timed is not None and timed["status"] == "failed"
    assert timed["failure"] == "interrupted" and timed["error"].startswith("interrupted:")
    svc.metrics.refresh(svc.engine, None, tmp_path)
    assert svc.metrics.jobs_failed.labels("alerts", "error")._value.get() >= 1
    assert svc.metrics.jobs_failed.labels("alerts", "interrupted")._value.get() >= 1


def test_killed_work_horse_never_blocks_its_request_forever(
    fresh_db: str, redis_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the production failure: the OOM killer SIGKILLs the work horse, the job row
    stays 'running' and de-duplication would hand every identical request the dead job."""
    import os
    import signal
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    import fpl_api.jobs as jobs_mod
    from fpl_storage import models as m
    from fpl_storage.db import session_scope

    st = Settings(database_url=fresh_db, redis_url=redis_url, artifact_dir=tmp_path)
    svc = AppServices.build(st, fixture_dataset("2026-27"))
    monkeypatch.setattr(jobs_mod, "services", lambda: svc)
    monkeypatch.setitem(
        jobs_mod.JOB_KINDS, "alerts", lambda *_a: os.kill(os.getpid(), signal.SIGKILL)
    )
    backend = JobBackend(svc)
    params = {"manager_key": "victim", "at": "b1"}
    job_id = backend.submit("alerts", params)
    conn = redis.Redis.from_url(redis_url)
    # a forking worker: the horse is killed by the signal, the worker survives
    Worker([Queue("fpl", connection=conn)], connection=conn).work(burst=True)
    stuck = backend.get(job_id)
    assert stuck is not None and stuck["status"] == "running"
    # RQ itself knows the horse died: once past the short grace period the reaper closes the
    # job without waiting for the full lease
    soon = datetime.now(UTC) + jobs_mod.RQ_GRACE + timedelta(seconds=5)
    assert backend._rq_lost(_row(svc, job_id), soon) == "queue reports failed"
    # within the lease the request is still considered in flight
    assert backend.submit("alerts", params) == job_id
    # past the lease the dead job is closed as abandoned and a new attempt is created
    with session_scope(svc.engine) as s:  # type: ignore[arg-type]
        row = s.get(m.JobRow, job_id)
        assert row is not None
        row.started_at = datetime.now(UTC) - jobs_mod.JOB_LEASE["alerts"] - timedelta(minutes=1)
    retry = backend.submit("alerts", params)
    assert retry != job_id
    dead = backend.get(job_id)
    assert dead is not None and dead["status"] == "failed"
    assert dead["error"] == jobs_mod.ABANDONED and dead["failure"] == "interrupted"
    assert backend.get(retry)["status"] == "queued"  # type: ignore[index]
    # failures RQ itself raises (e.g. timeouts) are recorded through the callback
    jobs_mod.on_rq_failure(SimpleNamespace(args=[retry]), conn, TimeoutError, "too slow", None)
    timed_out = backend.get(retry)
    assert timed_out is not None and timed_out["status"] == "failed"
    assert "TimeoutError: too slow" in timed_out["error"] and timed_out["failure"] == "error"
    # RQ's own job timeout reported through the callback is an interruption
    nxt = backend.submit("alerts", {"manager_key": "victim2", "at": "b1"})
    jobs_mod.on_rq_failure(SimpleNamespace(args=[nxt]), conn, JobTimeoutException, "900 s", None)
    assert backend.get(nxt)["failure"] == "interrupted"  # type: ignore[index]
    # a dead job whose request is never re-submitted (snapshot changed) is swept by the
    # scheduler's reaper instead of staying 'running' forever
    orphan = backend.submit("alerts", {"manager_key": "orphan", "at": "b0"})
    assert backend.reap_expired() == 0  # within its lease
    later = datetime.now(UTC) + jobs_mod.JOB_LEASE["alerts"] + timedelta(minutes=1)
    assert backend.reap_expired(later) >= 1
    assert backend.get(orphan)["error"] == jobs_mod.ABANDONED  # type: ignore[index]


def _row(svc: AppServices, job_id: str):  # type: ignore[no-untyped-def]
    from fpl_storage import models as m
    from fpl_storage.db import session_scope

    with session_scope(svc.engine) as s:  # type: ignore[arg-type]
        row = s.get(m.JobRow, job_id)
        s.expunge(row)
        return row


def test_api_promotes_a_newer_snapshot_only_once_its_forecast_is_ready(tmp_path: Path) -> None:
    """Hot swap without restart, and without a 'forecast not ready' window (snapshot promotion)."""
    import json as _json
    import time as _time

    from fpl_api.services import DataService, mark_serving_ready
    from fpl_storage.dataset import export_snapshot

    root = tmp_path / "snapshots"
    old = export_snapshot(fixture_dataset("2026-27"), root)
    _time.sleep(1.1)  # manifests order by creation time
    new = export_snapshot(fixture_dataset("2025-26", "2026-27"), root)
    assert old.name != new.name
    plain = DataService(Settings(snapshots_root=root, snapshot_check_seconds=0))
    assert plain.snapshot_id == new.name  # default: newest snapshot (worker behaviour)
    st = Settings(
        snapshots_root=root,
        snapshot_check_seconds=0,
        artifact_dir=tmp_path / "artifacts",
        serve_ready_snapshots_only=True,
    )
    api = DataService(st)
    assert api.snapshot_id == new.name  # nothing ready yet (first deployment): newest
    mark_serving_ready(st, old.name, "fc_old")
    api.refresh()
    assert api.snapshot_id == old.name  # newer snapshot not ready: keep serving the ready one
    mark_serving_ready(st, new.name, "fc_new")
    api.refresh()
    assert api.snapshot_id == new.name  # promoted without a restart
    marker = st.artifact_dir / "serving" / f"{new.name}.ready"
    assert _json.loads(marker.read_text())["forecast_key"] == "fc_new"


def test_metrics_of_jobs_run_in_forked_work_horses_are_served(tmp_path: Path) -> None:
    """A counter incremented in a forked child (as RQ runs jobs) survives the child's exit and
    is served by the worker's metrics endpoint (multiprocess mode)."""
    import os
    import sys
    import textwrap

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script = textwrap.dedent(
        f"""
        import os, threading, urllib.request
        from unittest import mock
        from fpl_worker import cli
        # the real entry point: `fpl-worker work` (Redis/queue stubbed; metrics are real)
        started = threading.Event()
        class W:
            def __init__(self, *a, **k): pass
            def work(self, burst=False): started.set()
        with mock.patch.object(cli, "Worker", W), mock.patch.object(cli, "Queue"), \
             mock.patch.object(cli.redis.Redis, "from_url"), \
             mock.patch.dict(os.environ, {{"FPL_WORKER_METRICS_PORT": "{port}",
                                          "FPL_REDIS_URL": "redis://unused:6379/0"}}):
            assert cli.main(["work", "--burst"]) == 0 and started.is_set()
        pid = os.fork()
        if pid == 0:  # the "work horse"
            from fpl_api.observability import Metrics
            m = Metrics()
            m.recommendations.labels("succeeded").inc(3)
            m.optimization_status.labels("Optimal").inc()
            os._exit(0)
        os.waitpid(pid, 0)
        body = urllib.request.urlopen("http://127.0.0.1:{port}/metrics").read().decode()
        print([l for l in body.splitlines() if l.startswith(("fpl_recommendations_total",
                                                             "fpl_optimization_status_total"))])
        """
    )
    env = dict(os.environ, PROMETHEUS_MULTIPROC_DIR=str(tmp_path / "prom"))
    out = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert 'fpl_recommendations_total{outcome="succeeded"} 3.0' in out.stdout
    assert 'fpl_optimization_status_total{status="Optimal"} 1.0' in out.stdout


def test_restarted_worker_does_not_collide_with_its_stale_registration(redis_url: str) -> None:
    """Crash-loop regression: the dead worker's registration outlives it in Redis."""
    import socket as _socket

    from fpl_worker.cli import worker_name

    conn = redis.Redis.from_url(redis_url)
    q = Queue("fpl", connection=conn)
    dead = Worker([q], connection=conn, name=worker_name())
    dead.register_birth()  # the crashed process never registers its death
    with pytest.raises(ValueError, match="exists an active worker"):
        Worker([q], connection=conn, name=dead.name).register_birth()  # the old naming
    fresh = Worker([q], connection=conn, name=worker_name())
    fresh.register_birth()  # the restarted container gets a new identity
    assert fresh.name != dead.name
    assert fresh.name.startswith(_socket.gethostname() + ".")  # liveness probe still finds it
    for w in (dead, fresh):
        w.register_death()


def test_worker_liveness_survives_idle_gaps_and_a_lost_registry_entry(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an idle worker beats only once per blocking dequeue (~405 s), and after the
    host slept past the key TTL RQ dropped the live worker from its global set — the probe
    reported a working worker as dead in both cases."""
    from datetime import UTC, datetime, timedelta

    from rq.utils import utcformat

    from fpl_worker import cli as worker_cli

    monkeypatch.setenv("FPL_REDIS_URL", redis_url)
    conn = redis.Redis.from_url(redis_url)
    for k in conn.scan_iter(match="rq:worker*"):
        conn.delete(k)
    probe = ["healthcheck", "work"]
    assert worker_cli.main(probe) == 1  # no worker at all

    w = Worker([Queue("fpl", connection=conn)], connection=conn, name=worker_cli.worker_name())
    w.register_birth()
    try:

        def beat(seconds_ago: float) -> None:
            ts = datetime.now(UTC) - timedelta(seconds=seconds_ago)
            conn.hset(w.key, "last_heartbeat", utcformat(ts))

        beat(0)
        assert worker_cli.main(probe) == 0
        beat(400)  # idle: the next beat comes after the blocking dequeue times out
        assert worker_cli.main(probe) == 0
        conn.srem("rq:workers", w.key)  # what RQ's registry cleanup did after the host slept
        assert Worker.all(connection=conn) == []
        assert worker_cli.main(probe) == 0
        beat(worker_cli.WORKER_HEARTBEAT_MAX_AGE_S + 30)  # past RQ's own expiry: really dead
        assert worker_cli.main(probe) == 1
    finally:
        w.register_death()
