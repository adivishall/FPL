"""Scheduler bucketing: each task is submitted once per time bucket (idempotent restarts)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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


def test_scheduler_liveness_follows_its_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import time

    from fpl_worker import cli

    beat = tmp_path / "alive"
    monkeypatch.setattr(cli, "SCHEDULER_HEARTBEAT", beat)
    probe = ["healthcheck", "schedule"]
    assert cli.main(probe) == 1  # never ticked
    beat.touch()
    assert cli.main(probe) == 0
    old = time.time() - 3 * 60 * 60
    os.utime(beat, (old, old))
    assert cli.main(probe) == 1  # stuck scheduler is reported dead


def test_results_are_due_when_missing_then_daily_while_correctable() -> None:
    import pandas as pd

    from fpl_worker.cli import results_due

    last_ko = pd.Timestamp("2026-09-28T19:00Z")
    gws = pd.DataFrame(
        {
            "season": "2026-27",
            "gw": [1, 2, 3],
            "status": ["finalized", "provisional", "upcoming"],
            "last_kickoff_at": [pd.Timestamp("2026-08-24T19:00Z"), last_ko, pd.NaT],
        }
    )
    have_gw1 = pd.DataFrame(
        {"season": ["2026-27"], "gw": [1], "fixture_id": [1], "player_code": [1]}
    )
    now = last_ko + pd.Timedelta(hours=10)
    assert results_due(gws, have_gw1, "2026-27", None, now) == "results missing for GW2-GW2"
    both = pd.concat([have_gw1, have_gw1.assign(gw=2, fixture_id=2)])
    assert results_due(gws, both, "2026-27", now - pd.Timedelta(hours=2), now) is None  # today
    assert results_due(gws, both, "2026-27", now - pd.Timedelta(hours=25), now) is not None
    later = last_ko + pd.Timedelta(days=5)  # outside the correction window: nothing to do
    assert results_due(gws, both, "2026-27", now - pd.Timedelta(days=3), later) is None
