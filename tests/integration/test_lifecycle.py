"""Result lifecycle (M1.3): snapshot promotion invalidates affected results exactly once, a
job that finishes against obsolete inputs is stored as historical and never replaces the
current result, every read says whether a result is current and why not, "what changed" is
built from stored inputs only, and none of it crosses manager boundaries.

The module runs a real promotion: the API serves snapshot A from a snapshots directory with a
ready marker, snapshot B (one owned player's availability changed) is exported and marked
ready, the service swaps to it exactly as in production, and the promotion event handler
runs as the forecast job would run it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fpl_api import lifecycle as lc
from fpl_api.app import create_app
from fpl_api.container import AppServices
from fpl_api.jobs import _job_recommendation, _job_workbench
from fpl_api.lifecycle import (
    FINALISED_STALE,
    SNAPSHOT_PROMOTED,
    SQUAD_CHANGED,
    can_transition,
    invalidate_for_snapshot,
)
from fpl_api.services import mark_serving_ready
from fpl_api.settings import Settings
from fpl_storage import models as m
from fpl_storage.dataset import CanonicalDataset, export_snapshot
from fpl_storage.db import session_scope
from tests.fixtures_util import fixture_dataset
from tests.integration.test_identity import _bearer, _manager, _register

KEY = "lc-demo"
OTHER = "lc-other"
GEN = {"horizon": 2, "stability": False, "scenarios": False, "chips": False}
DS_A = fixture_dataset("2024-25", "2025-26", "2026-27")


def _settings(db: str, tmp: Path, root: Path) -> Settings:
    return Settings(
        database_url=db,
        snapshots_root=root,
        serve_ready_snapshots_only=True,
        snapshot_check_seconds=0,
        artifact_dir=tmp / "artifacts",
        feature_store_dir=tmp / "features",
        n_sims=200,
        horizon_default=2,
        horizon_max=2,
        forecast_horizon=2,
        sync_horizon_limit=2,
    )


@pytest.fixture(scope="module")
def world(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    import uuid

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from tests.conftest import run_alembic

    name = f"lc_{uuid.uuid4().hex[:10]}"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    assert run_alembic(url, "upgrade", "head").returncode == 0
    tmp = tmp_path_factory.mktemp("lifecycle")
    root = tmp / "snapshots"
    a = export_snapshot(DS_A, root)
    st = _settings(url, tmp, root)
    mark_serving_ready(st, a.name, "fc_a")  # A is the served snapshot
    svc = AppServices.build(st)
    app = create_app(st, svc)
    with TestClient(app) as c:
        yield {"client": c, "svc": svc, "settings": st, "root": root, "a": a.name}
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture(scope="module")
def squad(world: dict[str, Any]) -> list[int]:
    r = world["client"].post("/api/v1/optimize/squad", json={"budget": 1000, "horizon": 2})
    assert r.status_code == 200, r.text
    return [int(p["player_code"]) for p in r.json()["squad"]]


def _save(client: TestClient, codes: list[int], bank: int, key: str = KEY, **headers: Any) -> str:
    r = client.post(
        "/api/v1/squad",
        json={
            "manager_key": key,
            "picks": [{"player_code": c} for c in codes],
            "bank": bank,
            "free_transfers": 1,
        },
        headers=headers.get("headers"),
    )
    assert r.status_code == 200, r.text
    return str(r.json()["state_id"])


def _generate(client: TestClient, key: str = KEY, **extra: Any) -> str:
    r = client.post("/api/v1/recommendations/generate", json={"manager_key": key, **GEN, **extra})
    assert r.status_code == 202, r.text
    job = client.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "succeeded", job
    return str(job["result_ref"])


def _evaluate(client: TestClient, key: str = KEY, **extra: Any) -> dict[str, Any]:
    r = client.post(
        "/api/v1/workbench/evaluate",
        json={"manager_key": key, "horizon": 2, "plans": [{"kind": "hold"}], **extra},
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


def _current(client: TestClient, key: str = KEY) -> dict[str, Any]:
    r = client.get("/api/v1/recommendations/current", params={"manager_key": key})
    assert r.status_code == 200, r.text
    return dict(r.json())


def _row(svc: AppServices, rec_id: str) -> m.RecommendationRow:
    with session_scope(svc.engine) as s:
        row = s.get(m.RecommendationRow, rec_id)
        assert row is not None
        s.expunge(row)
        return row


def _run_row(svc: AppServices, run_id: str) -> m.WorkbenchRunRow:
    with session_scope(svc.engine) as s:
        row = s.get(m.WorkbenchRunRow, run_id)
        assert row is not None
        s.expunge(row)
        return row


def _register_snapshot(svc: AppServices, sid: str, as_of: datetime) -> None:
    with session_scope(svc.engine) as s:
        if s.get(m.DataSnapshot, sid) is None:
            s.add(
                m.DataSnapshot(
                    id=sid,
                    as_of=as_of,
                    seasons=[],
                    tables={},
                    storage_uri="test://",
                    validated=True,
                )
            )


def _snapshot_b(squad: list[int]) -> tuple[CanonicalDataset, int]:
    """Snapshot A with one owned player's availability changed (a real input difference)."""
    ps = DS_A["player_snapshots"].copy()
    code = squad[3]
    mask = ps["player_code"] == code
    assert mask.any()
    ps.loc[mask, "status"] = "i"
    ps.loc[mask, "chance_of_playing_next_round"] = 25.0
    ps.loc[mask, "news"] = "Knee injury - 25% chance of playing"
    frames = {**DS_A.frames, "player_snapshots": ps}
    return CanonicalDataset(frames, dict(DS_A.meta)), code


def test_transitions_are_explicit() -> None:
    assert can_transition("active", "superseded") and can_transition("active", "invalidated")
    assert not can_transition("superseded", "invalidated")
    assert not can_transition("invalidated", "superseded")
    assert not can_transition("active", "active")
    # re-activation exists, guarded by reactivation_allowed (the inputs must be the current
    # ones again: never a result on an older snapshot) — exercised below
    assert can_transition("superseded", "active") and can_transition("invalidated", "active")


def test_results_on_the_served_snapshot_are_current(
    world: dict[str, Any], squad: list[int]
) -> None:
    client, svc, a = world["client"], world["svc"], world["a"]
    assert svc.data.snapshot_id == a
    sid = _save(client, squad, bank=5)
    rec_id = _generate(client)
    rec = _current(client)
    assert rec["id"] == rec_id and rec["status"] == "active" and rec["data_snapshot_id"] == a
    assert rec["lifecycle"] == {
        **rec["lifecycle"],
        "state": "current",
        "current": True,
        "reasons": [],
        "state_id": sid,
        "latest_state_id": sid,
        "snapshot_id": a,
        "served_snapshot_id": a,
        "next_action": None,
    }
    run = _evaluate(client)
    assert run["lifecycle"]["state"] == "current" and run["invalidated_at"] is None
    home = client.get("/api/v1/copilot/home", params={"manager_key": KEY}).json()
    assert (
        home["lifecycle"]["state"] == "current" and home["recommendation"]["lifecycle"]["current"]
    )
    # a second manager's results on the same snapshot, and one unregistered snapshot row that
    # is *not* promoted, change nothing
    _save(client, squad, bank=9, key=OTHER)
    _generate(client, OTHER)
    _register_snapshot(svc, "snap_registered_not_promoted", datetime.now(UTC) + timedelta(days=1))
    assert _current(client)["lifecycle"]["current"] is True


def test_a_newer_squad_state_makes_the_result_stale_with_the_state_diff(
    world: dict[str, Any], squad: list[int]
) -> None:
    client = world["client"]
    rec = _current(client)
    old_state = rec["state_id"]
    new_state = _save(client, squad, bank=15)
    assert new_state != old_state
    rec = _current(client)
    assert rec["status"] == "active"  # nothing persisted: the state may become latest again
    assert rec["lifecycle"]["state"] == "stale" and rec["lifecycle"]["current"] is False
    assert [r["kind"] for r in rec["lifecycle"]["reasons"]] == [SQUAD_CHANGED]
    assert rec["lifecycle"]["reasons"][0] == {
        **rec["lifecycle"]["reasons"][0],
        "old": old_state,
        "new": new_state,
    }
    ch = client.get(f"/api/v1/recommendations/{rec['id']}/changes").json()
    assert ch["state"]["available"] is True and ch["state"]["bank"] == {"old": 5, "new": 15}
    assert ch["state"]["players_in"] == [] and ch["state"]["players_out"] == []
    assert ch["snapshot"] is None  # the snapshot did not change: nothing is invented
    # re-saving the earlier squad makes it the latest again: the result is current again
    assert _save(client, squad, bank=5) == old_state
    assert _current(client)["lifecycle"]["current"] is True


def test_promotion_invalidates_affected_results_once_and_keeps_history(
    world: dict[str, Any], squad: list[int]
) -> None:
    client, svc, root, a = world["client"], world["svc"], world["root"], world["a"]
    rec_a = _current(client)
    run_a = _evaluate(client)
    other_rec = _current(client, OTHER)["id"]
    # a superseded record stays superseded (no illegal transition), an unrelated manager's
    # earlier record is invalidated like every other result on the old snapshot
    first_other = other_rec
    _save(client, squad, bank=11, key=OTHER)
    second_other = _generate(client, OTHER)
    assert second_other != first_other
    assert _row(svc, first_other).status == "superseded"

    # --- promotion: export B, mark its forecast ready, let the service swap (as in production)
    ds_b, changed_code = _snapshot_b(squad)
    time.sleep(1.1)  # manifests order by creation time
    b = export_snapshot(ds_b, root).name
    assert b != a
    mark_serving_ready(world["settings"], b, "fc_b")
    svc.data.refresh()
    assert svc.data.snapshot_id == b
    st = world["settings"]
    counts = invalidate_for_snapshot(svc.engine, b, st)  # the forecast job's promotion event
    assert counts["recommendations"] >= 2 and counts["workbench_runs"] >= 1
    assert invalidate_for_snapshot(svc.engine, b, st) == {"recommendations": 0, "workbench_runs": 0}
    world["b"] = b

    row = _row(svc, rec_a["id"])
    assert row.status == "invalidated" and row.invalidated_at is not None
    assert row.invalidation_json == {
        **row.invalidation_json,
        "kind": SNAPSHOT_PROMOTED,
        "old": a,
        "new": b,
    }
    assert row.data_snapshot_id == a and row.payload_json["decision"] == rec_a["decision"]
    assert _row(svc, first_other).status == "superseded"  # untouched
    assert _row(svc, first_other).invalidated_at is None
    run_row = _run_row(svc, run_a["id"])
    assert run_row.status == "succeeded" and run_row.invalidated_at is not None
    assert run_row.result_json["result"]["plans"][0]["label"] == "hold"  # result preserved
    first_at = row.invalidated_at

    # --- reads: historical, with the cause, never current
    rec = _current(client)
    assert rec["id"] == rec_a["id"] and rec["status"] == "invalidated"
    assert rec["lifecycle"]["state"] == "stale" and rec["lifecycle"]["current"] is False
    r0 = rec["lifecycle"]["reasons"][0]
    assert r0["kind"] == SNAPSHOT_PROMOTED and r0["old"] == a and r0["new"] == b
    assert r0["detected_at"] and rec["lifecycle"]["next_action"] == "regenerate"
    run = client.get(f"/api/v1/workbench/runs/{run_a['id']}").json()
    assert run["lifecycle"]["state"] == "stale" and run["stale_snapshot"] is True
    assert run["lifecycle"]["reasons"][0]["kind"] == SNAPSHOT_PROMOTED
    tr = client.get(f"/api/v1/recommendations/{rec_a['id']}/trace")
    assert tr.status_code == 200, tr.text
    trace = tr.json()["chain"]
    assert trace["recommendation"]["data_snapshot_id"] == a
    assert trace["recommendation"]["status"] == "invalidated"
    assert tr.json()["complete"] is True  # the lineage chain of a historical result is intact

    # --- what changed: both snapshots are on disk, so the real input differences are shown
    ch = client.get(f"/api/v1/recommendations/{rec_a['id']}/changes").json()
    snap = ch["snapshot"]
    assert (
        snap["available"] is True and snap["old_snapshot_id"] == a and snap["new_snapshot_id"] == b
    )
    assert snap["old_as_of"] and snap["new_as_of"] and snap["old_as_of"] < snap["new_as_of"]
    assert "player_snapshots" in snap["tables_changed"]
    changed = {p["player_code"]: p for p in snap["players"]}
    assert changed[changed_code]["status"] == {"old": "a", "new": "i"}
    assert changed[changed_code]["chance_of_playing_next_round"]["new"] == 25.0
    assert ch["forecast"]["available"] is True and ch["forecast"]["changed"] is True
    assert ch["forecast"]["old_prediction_run_id"] != ch["forecast"]["new_prediction_run_id"]
    wch = client.get(f"/api/v1/workbench/runs/{run_a['id']}/changes").json()
    assert wch["snapshot"]["available"] is True and wch["lifecycle"]["state"] == "stale"

    # --- regenerate: the new result is current on B; the old one stays historical
    new_id = _generate(client)
    assert new_id != rec_a["id"]
    rec = _current(client)
    assert rec["id"] == new_id and rec["status"] == "active" and rec["data_snapshot_id"] == b
    assert rec["lifecycle"]["current"] is True
    old = client.get(f"/api/v1/recommendations/{rec_a['id']}").json()
    assert old["status"] == "invalidated" and old["lifecycle"]["state"] == "stale"
    assert _row(svc, rec_a["id"]).invalidated_at == first_at  # history untouched
    new_run = _evaluate(client)
    assert new_run["id"] != run_a["id"] and new_run["lifecycle"]["current"] is True
    home = client.get("/api/v1/copilot/home", params={"manager_key": KEY}).json()
    assert home["recommendation"]["id"] == new_id and home["recommendation"]["lifecycle"]["current"]


def test_a_job_that_finishes_against_obsolete_inputs_is_historical_from_birth(
    world: dict[str, Any], squad: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, svc = world["client"], world["svc"]
    current = _current(client)
    assert current["lifecycle"]["current"] is True
    # promotion lands between the job's computation and its persistence: the serving snapshot
    # the store checks against is newer than the one the job computed with
    _register_snapshot(svc, "snap_promoted_meanwhile", datetime.now(UTC) + timedelta(hours=1))
    run = svc.workbench.create(
        KEY,
        current["state_id"],
        svc.data.snapshot_id,
        "fc_x",  # also: the run names a forecast the job will not compute on
        1,
        "default",
        {"plans": [{"kind": "hold"}], "horizon": 1, "profile": "default"},
    )[0]
    # only while the jobs finalise does the promoted reference name the newer snapshot
    monkeypatch.setattr(svc, "serving_snapshot_id", lambda: "snap_promoted_meanwhile")
    late = _job_recommendation(
        svc, {"manager_key": KEY, "state_id": current["state_id"], **GEN, "horizon": 1}
    )
    _job_workbench(svc, {"manager_key": KEY, "state_id": current["state_id"], "run_id": run["id"]})
    monkeypatch.undo()
    assert late != current["id"]
    row = _row(svc, late)
    assert row.status == "invalidated" and row.invalidation_json["kind"] == FINALISED_STALE
    assert row.invalidation_json["new"] == {"snapshot_id": "snap_promoted_meanwhile"}
    assert _row(svc, current["id"]).status == "active"  # never superseded by the late job
    assert _current(client)["id"] == current["id"]
    run_row = _run_row(svc, run["id"])
    assert run_row.status == "succeeded" and run_row.invalidation_json["kind"] == FINALISED_STALE
    got = client.get(f"/api/v1/workbench/runs/{run['id']}").json()
    assert (
        got["lifecycle"]["state"] == "stale"
        and got["result"]["result"]["plans"][0]["label"] == "hold"
    )
    after = _current(client)
    assert after["lifecycle"]["current"] is True, (after["id"], after["status"], after["lifecycle"])


def test_an_older_job_finishing_after_a_newer_one_cannot_replace_it(
    world: dict[str, Any], squad: list[int]
) -> None:
    client, svc = world["client"], world["svc"]
    state_x = _current(client)["state_id"]
    state_y = _save(client, squad, bank=33)
    newest = _generate(client)  # on Y, current
    assert _current(client)["id"] == newest
    # a different profile: a package the earlier race test did not already store (ids are
    # content-addressed, so the identical obsolete request would return that record)
    obsolete = {
        "manager_key": KEY,
        "state_id": state_x,
        **GEN,
        "horizon": 1,
        "profile": "aggressive",
    }
    late = _job_recommendation(svc, obsolete)
    row = _row(svc, late)
    debug = {
        "latest": svc.states.latest(KEY)[0],
        "x": state_x,
        "y": state_y,
        "served": svc.data.snapshot_id,
        "newest": svc.serving_snapshot_id(),
        "rec_snapshot": row.data_snapshot_id,
        "json": row.invalidation_json,
    }
    assert row.status == "invalidated" and row.invalidation_json["kind"] == FINALISED_STALE
    assert row.invalidation_json["old"] == {"state_id": state_x}, debug
    assert row.invalidation_json["new"] == {"state_id": state_y}
    assert _current(client)["id"] == newest and _current(client)["lifecycle"]["current"] is True
    # retrying the same obsolete request returns the same historical record, nothing more
    assert _job_recommendation(svc, obsolete) == late
    assert _current(client)["id"] == newest


def test_a_missed_promotion_is_never_served_as_current(
    world: dict[str, Any], squad: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler did not run (or its write failed): the read still says stale and records
    the verdict when it can."""
    client, svc = world["client"], world["svc"]
    rec_id = _current(client)["id"]
    with session_scope(svc.engine) as s:  # simulate a result the handler never reached
        row = s.get(m.RecommendationRow, rec_id)
        assert row is not None
        row.data_snapshot_id = "snap_long_gone"
    rec = _current(client)
    assert rec["lifecycle"]["state"] == "stale" and rec["lifecycle"]["current"] is False
    assert rec["lifecycle"]["reasons"][0]["kind"] == SNAPSHOT_PROMOTED
    assert _row(svc, rec_id).status == "invalidated"  # recorded lazily
    ch = client.get(f"/api/v1/recommendations/{rec_id}/changes").json()
    assert ch["snapshot"]["available"] is False and "no longer stored" in ch["snapshot"]["reason"]
    # the same read with a failing database write: the response is still not current
    run = _evaluate(client)
    with session_scope(svc.engine) as s:
        r = s.get(m.WorkbenchRunRow, run["id"])
        assert r is not None
        r.data_snapshot_id = "snap_long_gone"

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(lc, "session_scope", boom)
    got = client.get(f"/api/v1/workbench/runs/{run['id']}").json()
    assert got["lifecycle"]["state"] == "stale" and got["lifecycle"]["current"] is False
    monkeypatch.undo()
    assert _run_row(svc, run["id"]).invalidated_at is None  # the write really did not happen


def test_lifecycle_and_changes_stay_with_their_owner(
    world: dict[str, Any], squad: list[int]
) -> None:
    client = world["client"]
    alice = _register(client, "lc-alice@example.test")["session_token"]
    bob = _register(client, "lc-bob@example.test")["session_token"]
    a_key = _manager(client, alice, squad)
    rec = client.post(
        "/api/v1/recommendations/generate",
        json={"manager_key": a_key, **GEN},
        headers=_bearer(alice),
    )
    assert rec.status_code == 202
    job = client.get(f"/api/v1/jobs/{rec.json()['job_id']}", headers=_bearer(alice)).json()
    rec_id = job["result_ref"]
    run = client.post(
        "/api/v1/workbench/evaluate",
        json={"manager_key": a_key, "plans": [{"kind": "hold"}]},
        headers=_bearer(alice),
    ).json()
    for path in (
        f"/api/v1/recommendations/{rec_id}",
        f"/api/v1/recommendations/{rec_id}/changes",
        f"/api/v1/recommendations/{rec_id}/trace",
        f"/api/v1/workbench/runs/{run['id']}",
        f"/api/v1/workbench/runs/{run['id']}/changes",
    ):
        assert client.get(path, headers=_bearer(bob)).status_code == 403, path
        assert client.get(path, headers=_bearer(alice)).status_code == 200, path
    # bob saving a squad of his own changes nothing about alice's results
    b_key = _manager(client, bob, squad, bank=7)
    client.post(
        "/api/v1/squad",
        json={
            "manager_key": b_key,
            "picks": [{"player_code": c} for c in squad],
            "bank": 2,
            "free_transfers": 1,
        },
        headers=_bearer(bob),
    )
    mine = client.get(f"/api/v1/recommendations/{rec_id}", headers=_bearer(alice)).json()
    assert mine["lifecycle"]["current"] is True


# ------------------------------------------------------------------ audit regressions (M1.3)


def _worker(world: dict[str, Any]) -> AppServices:
    """A worker process as deployed: it loads every export at once (no ready filter) and
    shares the database, the artifacts and the snapshots directory with the API."""
    return AppServices.build(
        world["settings"].model_copy(update={"serve_ready_snapshots_only": False})
    )


def _snapshot_c(squad: list[int]) -> CanonicalDataset:
    """Snapshot B with a second owned player's availability changed."""
    ds_b, _ = _snapshot_b(squad)
    ps = ds_b["player_snapshots"].copy()
    mask = ps["player_code"] == squad[5]
    ps.loc[mask, "status"] = "d"
    ps.loc[mask, "chance_of_playing_next_round"] = 75.0
    ps.loc[mask, "news"] = "Hamstring - 75% chance of playing"
    return CanonicalDataset({**ds_b.frames, "player_snapshots": ps}, dict(ds_b.meta))


def test_a_result_on_a_newer_snapshot_than_the_promoted_one_stays_current(
    world: dict[str, Any], squad: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployed topology: after an export the worker computes on it at once while the API
    keeps serving the promoted snapshot. A result on the newer export is neither invalidated
    by a read nor at finalisation; a run the API names on the promoted snapshot is computed
    on exactly that snapshot; and promoting the export makes the older results historical."""
    client, svc, root, settings = world["client"], world["svc"], world["root"], world["settings"]
    served = svc.serving_snapshot_id()
    assert svc.data.snapshot_id == served == world["b"]
    sid = svc.states.latest(KEY)[0]
    time.sleep(1.1)  # manifests order by creation time
    c = export_snapshot(_snapshot_c(squad), root).name  # exported, its forecast not ready
    worker = _worker(world)
    assert worker.data.snapshot_id == c and worker.serving_snapshot_id() == served

    rec_c = _job_recommendation(
        worker, {"manager_key": KEY, "state_id": sid, **GEN, "profile": "conservative"}
    )
    row = _row(svc, rec_c)
    assert row.data_snapshot_id == c and row.status == "active" and row.invalidated_at is None
    got = client.get(f"/api/v1/recommendations/{rec_c}").json()
    assert got["lifecycle"]["state"] == "current", got["lifecycle"]
    assert got["lifecycle"]["served_snapshot_id"] == served
    assert _row(svc, rec_c).status == "active"  # the read persisted nothing
    assert svc.data.snapshot_id == served  # and the API did not serve the unpromoted export

    fc = svc.forecast_for(svc.context(), 2)
    fkey = str(fc.provenance.get("forecast_key", fc.run_id))
    request = {"plans": [{"kind": "hold"}], "horizon": 2, "profile": "default"}
    run, compute = svc.workbench.create(KEY, sid, served, fkey, 2, "default", request)
    assert compute
    _job_workbench(worker, {"manager_key": KEY, "state_id": sid, "run_id": run["id"]})
    rr = _run_row(svc, run["id"])
    assert rr.status == "succeeded" and rr.invalidated_at is None, rr.invalidation_json
    assert rr.result_json["snapshot_id"] == served and rr.result_json["forecast_key"] == fkey
    view = client.get(f"/api/v1/workbench/runs/{run['id']}").json()
    assert view["lifecycle"]["state"] == "current" and view["stale_snapshot"] is False

    # an API process that still has B loaded follows the promotion on its next verdict — also
    # on a host booted less than a check interval ago (the monotonic clock starts at boot)
    import types

    from fpl_api import services as services_mod

    clock = types.SimpleNamespace(monotonic=lambda: 120.0, perf_counter=time.perf_counter)
    monkeypatch.setattr(services_mod, "time", clock)
    lagging = AppServices.build(settings.model_copy(update={"snapshot_check_seconds": 3600.0}))
    assert lagging.data.snapshot_id == served
    mark_serving_ready(settings, c, "fc_c")  # C is promoted
    assert lagging.data.snapshot_id == served  # its periodic check has not run yet
    verdict = lc.lifecycle_block(
        lagging, "workbench_run", run["id"], KEY, "succeeded", sid, served, None, "succeeded"
    )
    assert verdict["state"] == "stale" and verdict["served_snapshot_id"] == c
    assert lagging.data.snapshot_id == c
    monkeypatch.undo()

    counts = invalidate_for_snapshot(svc.engine, c, settings)
    assert counts["workbench_runs"] >= 1
    assert _row(svc, rec_c).status == "active"  # on the promoted snapshot: untouched
    assert _run_row(svc, run["id"]).invalidated_at is not None
    got = client.get(f"/api/v1/recommendations/{rec_c}").json()
    assert got["lifecycle"]["current"] is True and svc.data.snapshot_id == c
    world["c"] = c


def test_the_same_question_on_inputs_that_are_current_again_gets_the_same_current_result(
    world: dict[str, Any], squad: list[int]
) -> None:
    client, svc = world["client"], world["svc"]
    p = _save(client, squad, bank=41)
    r1 = _generate(client)
    run1 = _evaluate(client)
    assert _current(client)["id"] == r1 and run1["lifecycle"]["current"] is True
    _save(client, squad, bank=42)
    r2 = _generate(client)
    assert r2 != r1 and _row(svc, r1).status == "superseded"
    stale_run = client.get(f"/api/v1/workbench/runs/{run1['id']}").json()
    assert stale_run["lifecycle"]["state"] == "stale" and stale_run["invalidated_at"] is None

    # the squad is saved back: the run is current again by itself; the recommendation once
    # the same question is asked again (its job is de-duplicated to the stored answer)
    assert _save(client, squad, bank=41) == p
    again = client.get(f"/api/v1/workbench/runs/{run1['id']}").json()
    assert again["lifecycle"]["current"] is True
    assert _current(client)["id"] == r2 and _current(client)["lifecycle"]["state"] == "stale"
    assert _generate(client) == r1
    rec = _current(client)
    assert rec["id"] == r1 and rec["status"] == "active" and rec["lifecycle"]["current"] is True
    assert _row(svc, r2).status == "superseded"
    journal = client.get("/api/v1/decisions", params={"manager_key": KEY}).json()["journal"]
    by_id = {e["id"]: e for e in journal}
    assert by_id[r1]["lifecycle"]["state"] == "current"
    assert by_id[r2]["lifecycle"]["state"] == "superseded"
    assert all(e["lifecycle"]["state"] != "current" for e in journal if e["id"] != r1)

    # a run that finishes after the squad was saved again: no persisted squad verdict, so it
    # is stale while the squad differs and current once that squad is the latest again
    fc = svc.forecast_for(svc.context(), 1)
    fkey = str(fc.provenance.get("forecast_key", fc.run_id))
    request = {"plans": [{"kind": "hold"}], "horizon": 1, "profile": "conservative"}
    run, _ = svc.workbench.create(KEY, p, svc.data.snapshot_id, fkey, 1, "conservative", request)
    _save(client, squad, bank=43)
    _job_workbench(svc, {"manager_key": KEY, "state_id": p, "run_id": run["id"]})
    rr = _run_row(svc, run["id"])
    assert rr.status == "succeeded" and rr.invalidated_at is None
    url = f"/api/v1/workbench/runs/{run['id']}"
    assert client.get(url).json()["lifecycle"]["state"] == "stale"
    _save(client, squad, bank=41)
    assert client.get(url).json()["lifecycle"]["current"] is True


def test_an_older_request_finishing_last_never_replaces_the_newer_answer(
    world: dict[str, Any], squad: list[int]
) -> None:
    client, svc = world["client"], world["svc"]
    sid = svc.states.latest(KEY)[0]
    newer = _generate(client, profile="conservative")
    assert _current(client)["id"] == newer
    late = _job_recommendation(  # same squad and snapshot, requested five minutes earlier
        svc,
        {
            "manager_key": KEY,
            "state_id": sid,
            **GEN,
            "profile": "aggressive",
            "_requested_at": datetime.now(UTC) - timedelta(minutes=5),
        },
    )
    row = _row(svc, late)
    assert row.status == "superseded" and row.invalidated_at is None
    assert _current(client)["id"] == newer
    got = client.get(f"/api/v1/recommendations/{late}").json()
    assert got["lifecycle"]["state"] == "superseded" and got["lifecycle"]["current"] is False


def test_a_workbench_job_delivered_again_never_reopens_or_overwrites_its_run(
    world: dict[str, Any],
) -> None:
    client, svc = world["client"], world["svc"]
    run = _evaluate(client)
    before = _run_row(svc, run["id"])
    out = _job_workbench(
        svc, {"manager_key": KEY, "state_id": run["state_id"], "run_id": run["id"]}
    )
    assert out == run["id"]
    after = _run_row(svc, run["id"])
    assert after.finished_at == before.finished_at and after.result_json == before.result_json
    assert svc.workbench.start(run["id"]) is False
    assert svc.workbench.finish(run["id"], {"overwritten": True}) is False
    assert _run_row(svc, run["id"]).result_json == before.result_json


def test_a_run_whose_enqueue_was_lost_is_queued_again(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fpl_decision.workbench import PlanSpec

    client, svc = world["client"], world["svc"]
    # the request created the run, then died before handing it a job
    fc = svc.forecast_for(svc.context(), 2)
    fkey = str(fc.provenance.get("forecast_key", fc.run_id))
    request = {
        "plans": [PlanSpec.model_validate({"kind": "hold"}).model_dump(mode="json")],
        "horizon": 2,
        "profile": "conservative",
    }
    sid = svc.states.latest(KEY)[0]
    run, _ = svc.workbench.create(KEY, sid, svc.data.snapshot_id, fkey, 2, "conservative", request)
    assert run["status"] == "queued" and run["job_id"] is None
    body = {
        "manager_key": KEY,
        "horizon": 2,
        "profile": "conservative",
        "plans": [{"kind": "hold"}],
    }
    soon = client.post("/api/v1/workbench/evaluate", json=body).json()
    assert soon["id"] == run["id"] and soon["status"] == "queued"  # within the grace: waits
    with session_scope(svc.engine) as s:
        r = s.get(m.WorkbenchRunRow, run["id"])
        assert r is not None
        r.created_at = datetime.now(UTC) - timedelta(minutes=10)
    again = client.post("/api/v1/workbench/evaluate", json=body)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == run["id"] and again.json()["status"] == "succeeded"

    # an enqueue that fails leaves the run failed (retryable), never queued without a job
    from fpl_api import jobs as jobs_mod

    def refuse(*_a: Any, **_k: Any) -> str:
        raise ConnectionError("queue unavailable")

    monkeypatch.setattr(jobs_mod.JobBackend, "submit", refuse)
    body = {"manager_key": KEY, "horizon": 2, "profile": "aggressive", "plans": [{"kind": "hold"}]}
    try:
        resp = client.post("/api/v1/workbench/evaluate", json=body)
        assert resp.status_code >= 500
    except ConnectionError:
        pass
    monkeypatch.undo()
    latest = svc.workbench.recent(KEY, 1)[0]
    assert latest["profile"] == "aggressive" and latest["status"] == "failed"
    assert "could not be queued" in (latest["error"] or "")
    retried = client.post("/api/v1/workbench/evaluate", json=body)
    assert retried.status_code == 200 and retried.json()["id"] == latest["id"]


def test_the_scheduler_completes_a_promotion_sweep_that_did_not_run(
    world: dict[str, Any],
) -> None:
    from fpl_worker.cli import task_forecast_precompute

    client, svc = world["client"], world["svc"]
    rec_id = _current(client)["id"]
    with session_scope(svc.engine) as s:  # a current result on an older snapshot: missed
        row = s.get(m.RecommendationRow, rec_id)
        assert row is not None
        row.data_snapshot_id = world["a"]

    class Jobs:
        def submit(self, kind: str, params: dict[str, Any]) -> str:
            return f"job_stub_{kind}"

    out = task_forecast_precompute(svc, Jobs(), "bucket")  # type: ignore[arg-type]
    assert out["invalidated"]["recommendations"] == 1
    row = _row(svc, rec_id)
    assert row.status == "invalidated" and row.invalidation_json["new"] == world["c"]
    again = task_forecast_precompute(svc, Jobs(), "bucket")  # type: ignore[arg-type]
    assert again["invalidated"] == {"recommendations": 0, "workbench_runs": 0}


# ------------------------------------------------------------------ fix-review regressions


def test_a_request_asked_again_while_its_job_waits_answers_the_later_request(
    world: dict[str, Any], squad: list[int]
) -> None:
    """J1 is requested, another question is answered after it, then J1's question is asked
    again (de-duplicated onto J1, still queued). When J1 finally runs, its answer counts as
    requested at the re-ask and is the current one."""
    from fpl_api.jobs import JobBackend, execute
    from fpl_domain.hashing import content_hash

    client, svc = world["client"], world["svc"]
    q = _save(client, squad, bank=55)
    jobs = JobBackend(svc)  # inline: no queue in the tests
    params = {"manager_key": KEY, "state_id": q, **GEN, "profile": "conservative"}
    h = content_hash({"kind": "recommendation", "params": params, "snap": svc.data.snapshot_id})
    j1 = "job_lc_waiting"
    with session_scope(svc.engine) as s:  # J1: requested ten minutes ago, still queued
        s.add(
            m.JobRow(
                id=j1,
                kind="recommendation",
                status="queued",
                request_hash=h,
                request_json=params,
                created_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
    other = _generate(client, profile="aggressive")  # answered meanwhile: current
    assert _current(client)["id"] == other
    again, reused = jobs.submit_reused("recommendation", params)  # J1's question again
    assert again == j1 and reused is True
    execute(svc, j1)
    r1 = jobs.get(j1)
    assert r1 is not None and r1["status"] == "succeeded", r1
    rec = _current(client)
    assert rec["id"] == r1["result_ref"] and rec["lifecycle"]["current"] is True
    assert _row(svc, other).status == "superseded"
    # a new question is not reported as reused: the route re-activates reused answers only,
    # never a fresh answer that was stored superseded because a later request was answered
    _job, reused = jobs.submit_reused("recommendation", {**params, "profile": "default"})
    assert reused is False


def test_a_job_for_a_run_that_was_given_up_on_fails_and_a_retry_computes_it(
    world: dict[str, Any],
) -> None:
    client, svc = world["client"], world["svc"]
    sid = svc.states.latest(KEY)[0]
    fc = svc.forecast_for(svc.context(), 1)
    fkey = str(fc.provenance.get("forecast_key", fc.run_id))
    request = {"plans": [{"kind": "hold"}], "horizon": 1, "profile": "aggressive"}
    run, _ = svc.workbench.create(KEY, sid, svc.data.snapshot_id, fkey, 1, "aggressive", request)
    svc.workbench.fail(run["id"], "abandoned: the job outlived its lease")  # the reaper's verdict
    with pytest.raises(RuntimeError, match="given up on"):
        _job_workbench(svc, {"manager_key": KEY, "state_id": sid, "run_id": run["id"]})
    assert _run_row(svc, run["id"]).status == "failed"

    # a run whose job ended without finishing it is failed by the next read (retryable)
    finished_job = _evaluate(client)["job_id"]
    queued, _ = svc.workbench.create(
        KEY,
        sid,
        svc.data.snapshot_id,
        fkey,
        1,
        "conservative",
        {**request, "profile": "conservative"},
    )
    svc.workbench.set_job(queued["id"], finished_job)
    view = client.get(f"/api/v1/workbench/runs/{queued['id']}").json()
    assert view["status"] == "failed" and "without computing" in (view["error"] or "")


def test_a_run_computed_on_other_inputs_is_never_made_current_and_is_computed_again(
    world: dict[str, Any],
) -> None:
    from fpl_api.lifecycle import INPUTS_MISMATCH

    client, svc = world["client"], world["svc"]
    sid = svc.states.latest(KEY)[0]
    request = {"plans": [{"kind": "hold"}], "horizon": 2, "profile": "default"}
    args = (KEY, sid, svc.data.snapshot_id, "fc_not_what_is_computed", 2, "default", request)
    run, _ = svc.workbench.create(*args)
    _job_workbench(svc, {"manager_key": KEY, "state_id": sid, "run_id": run["id"]})
    rr = _run_row(svc, run["id"])
    assert rr.status == "succeeded" and rr.invalidation_json["kind"] == INPUTS_MISMATCH
    view = client.get(f"/api/v1/workbench/runs/{run['id']}").json()
    assert view["lifecycle"]["state"] == "stale" and view["stale_snapshot"] is False
    ch = client.get(f"/api/v1/workbench/runs/{run['id']}/changes").json()
    assert ch["snapshot"] is None  # no snapshot difference is claimed
    again, compute = svc.workbench.create(*args)  # asking again recomputes, never re-activates
    assert compute is True and again["status"] == "queued" and again["result"] is None
    assert again["invalidation"] is None


def test_a_pinned_deployment_treats_results_on_any_other_snapshot_as_stale(
    world: dict[str, Any],
) -> None:
    """The documented data rollback pins an older snapshot (FPL_SNAPSHOT_DIR): results on the
    newer, rolled-back snapshot are no longer current, and the sweep invalidates them."""
    client, svc, root, settings = world["client"], world["svc"], world["root"], world["settings"]
    run = _evaluate(client)  # current, on the promoted snapshot C
    assert run["lifecycle"]["current"] is True and run["snapshot_id"] == world["c"]
    pinned = AppServices.build(settings.model_copy(update={"snapshot_dir": root / world["a"]}))
    assert pinned.serving_snapshot_id() == world["a"]
    verdict = lc.lifecycle_block(
        pinned,
        "workbench_run",
        run["id"],
        KEY,
        "succeeded",
        run["state_id"],
        world["c"],
        None,
        "succeeded",
    )
    assert verdict["state"] == "stale" and verdict["snapshot_stale"] is True
    assert verdict["served_snapshot_id"] == world["a"]
    current = _current(client)
    assert current["lifecycle"]["current"] is True and current["data_snapshot_id"] == world["c"]
    counts = invalidate_for_snapshot(svc.engine, world["a"], pinned.settings)
    assert counts["recommendations"] >= 1
    assert _row(svc, current["id"]).status == "invalidated"
    assert _run_row(svc, run["id"]).invalidated_at is not None
