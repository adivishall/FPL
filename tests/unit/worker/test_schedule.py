"""Scheduler bucketing: each task is submitted once per time bucket (idempotent restarts)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fpl_worker.cli import Schedule, TaskSpec, bucket, due_tasks, load_schedule


def test_default_schedule_loads() -> None:
    s = load_schedule()
    assert {"live_refresh", "forecast_precompute", "alerts"} <= set(s.tasks)


def test_bucket_boundaries() -> None:
    t = datetime(2026, 9, 12, 10, 29, 59, tzinfo=UTC)
    assert bucket(t, 30) == "2026-09-12T10:00"
    assert bucket(t + timedelta(seconds=1), 30) == "2026-09-12T10:30"
    assert bucket(t, 60) == "2026-09-12T10:00"


def test_due_tasks_once_per_bucket_and_disabled_skipped() -> None:
    s = Schedule(
        version="t",
        tasks={"a": TaskSpec(every_minutes=15), "b": TaskSpec(every_minutes=60, enabled=False)},
    )
    t0 = datetime(2026, 9, 12, 10, 1, tzinfo=UTC)
    due = due_tasks(s, {}, t0)
    assert due == [("a", "2026-09-12T10:00")]
    last = dict(due)
    assert due_tasks(s, last, t0 + timedelta(minutes=10)) == []  # same bucket: nothing
    assert due_tasks(s, last, t0 + timedelta(minutes=14)) == [("a", "2026-09-12T10:15")]
