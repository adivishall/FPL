"""Snapshot retention: bounded disk use that never breaks evaluation or traceability."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from fpl_api import retention
from fpl_api.retention import RetentionError, prune, referenced_snapshots
from fpl_api.services import mark_serving_ready
from fpl_api.settings import Settings
from fpl_storage import models as m
from fpl_storage.db import session_scope

PINNED = "snap_pinned_eval"
OLD = 3 * 86400  # seconds: older than every age threshold


def _snapshot(root: Path, sid: str, created: str) -> None:
    (root / sid).mkdir(parents=True)
    (root / sid / "players.parquet").write_bytes(b"x" * 1000)
    (root / sid / "manifest.json").write_text(
        json.dumps({"snapshot_id": sid, "created_at": created})
    )


def _age(p: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(p, (t, t))


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Settings, Path]:
    root, art, fs = tmp_path / "snapshots", tmp_path / "artifacts", tmp_path / "feature-store"
    st = Settings(
        snapshots_root=root, artifact_dir=art, feature_store_dir=fs, snapshot_retention_keep=2
    )
    _snapshot(root, PINNED, "2026-08-01T00:00:00+00:00")
    for h in range(6):  # hourly live snapshots s0 (oldest) … s5 (newest)
        _snapshot(root, f"snap_s{h}", f"2026-10-06T0{h}:00:00+00:00")
        (fs / f"snap_s{h}").mkdir(parents=True)
        (fs / f"snap_s{h}" / "gw01.parquet").write_bytes(b"f" * 5000)
    (fs / PINNED).mkdir()
    # s3 is the snapshot the API serves (its forecast is ready); s4 and s5 are newer, not ready
    for sid in ("snap_s1", "snap_s3"):
        mark_serving_ready(st, sid, f"fc_{sid}")
        (art / "forecasts").mkdir(parents=True, exist_ok=True)
        cached = art / "forecasts" / f"fc_{sid}.joblib"
        cached.write_bytes(b"c" * 7000)
        _age(cached, OLD)
    stale = art / "forecasts" / "fc_custom_request.joblib"
    stale.write_bytes(b"c")
    _age(stale, OLD)
    (art / "forecasts" / "fc_fresh_request.joblib").write_bytes(b"c")
    torn = art / "forecasts" / "fc_x.joblib.tmp123"  # a writer was killed mid-write
    torn.write_bytes(b"partial")
    _age(torn, OLD)
    (root / "snap_interrupted").mkdir()  # export killed before its manifest was published
    (root / "snap_interrupted" / "players.parquet").write_bytes(b"x")
    _age(root / "snap_interrupted", OLD)
    (root / "snap_exporting").mkdir()  # an export in progress right now
    monkeypatch.setattr(retention, "pinned_snapshots", lambda: {PINNED})
    monkeypatch.setattr(retention, "referenced_snapshots", lambda _e: {"snap_s0"})
    return st, tmp_path


def test_policy_keeps_newest_serving_pinned_and_referenced(layout: tuple[Settings, Path]) -> None:
    st, tmp = layout
    root, art, fs = tmp / "snapshots", tmp / "artifacts", tmp / "feature-store"
    rep = prune(st, None)
    on_disk = {p.name for p in root.iterdir()}
    # newest 2 + the serving one + the pinned evaluation snapshot keep data and caches
    assert set(rep.kept) == {"snap_s4", "snap_s5", "snap_s3", PINNED}
    # referenced by a stored recommendation: the data stays (traceable), caches go
    assert rep.kept_data_only == ["snap_s0"] and "snap_s0" in on_disk
    assert not (fs / "snap_s0").exists()
    assert sorted(rep.deleted) == ["snap_s1", "snap_s2"]
    assert on_disk == {PINNED, "snap_s0", "snap_s3", "snap_s4", "snap_s5", "snap_exporting"}
    assert {p.name for p in fs.iterdir()} == {"snap_s3", "snap_s4", "snap_s5", PINNED}
    # the serving forecast survives its age; s1 lost its marker and forecast with its snapshot
    assert {p.name for p in (art / "serving").iterdir()} == {"snap_s3.ready"}
    assert {p.name for p in (art / "forecasts").iterdir()} == {
        "fc_snap_s3.joblib",
        "fc_fresh_request.joblib",  # young: may still be in use
    }
    assert rep.freed_bytes > 0 and "deleted 2 snapshots" in rep.summary()
    # idempotent: a second run finds nothing more to delete
    again = prune(st, None)
    assert again.deleted == [] and again.deleted_files == 0


def test_disabled_and_unreadable_manifest_delete_nothing(layout: tuple[Settings, Path]) -> None:
    st, tmp = layout
    root = tmp / "snapshots"
    before = sorted(p.name for p in root.iterdir())
    assert prune(st.model_copy(update={"snapshot_retention_keep": 0}), None).deleted == []
    (root / "snap_s2" / "manifest.json").write_text("{torn")
    with pytest.raises(RetentionError, match="unreadable snapshot manifest"):
        prune(st, None)  # never guesses an order when deleting
    assert sorted(p.name for p in root.iterdir()) == before


def test_deletion_failures_are_raised_not_swallowed(
    layout: tuple[Settings, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    st, _ = layout
    import shutil

    def denied(p: object, *a: object, **k: object) -> None:
        raise PermissionError("read-only volume")

    monkeypatch.setattr(shutil, "rmtree", denied)
    with pytest.raises(RetentionError, match="read-only volume"):
        prune(st, None)


def test_references_come_from_decisions_backtests_and_models(fresh_db: str) -> None:
    engine = create_engine(fresh_db)
    with session_scope(engine) as s:
        s.add(
            m.BacktestRunRow(
                id="bt_1",
                season="2023-24",
                from_gw=1,
                to_gw=38,
                cutoff_policy={},
                benchmark="hold",
                model_version={},
                data_snapshot_id="snap_bt",
                config_json={},
                status="succeeded",
            )
        )
    assert referenced_snapshots(engine) == {"snap_bt"}
    assert referenced_snapshots(None) == set()
