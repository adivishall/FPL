"""Season evaluation of the live record and the Season Review API (M1.5), on a real database:
a frozen checkpoint is scored only on final results — nothing while results are incomplete,
excluded rows counted by reason and never read as 0 — with the contract's statistics;
identical inputs write nothing; a corrected official result writes a revision naming the
previous one; a process on older data never writes; the page's endpoint is public, carries no
manager data, reads stored rows only, and the on-demand evaluation is the operator's.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from fpl_api import jobs as jobs_mod
from fpl_api import season
from fpl_api.app import create_app
from fpl_api.container import AppServices
from fpl_api.ledger import capture, load_checkpoint
from fpl_api.services import DataService
from fpl_api.settings import Settings
from fpl_storage import models as m
from fpl_storage.dataset import CanonicalDataset, export_snapshot
from fpl_storage.db import session_scope
from tests.fixtures_util import with_gameweek_results
from tests.integration.test_identity import _bearer, _register
from tests.integration.test_ledger import CUT, DS, SEASON, _backdate, _load_gameweeks

GW = 2


def pts(code: int) -> int:
    return code % 9 - 1  # −1 … 7: some hauls (≥ 6), some negative scores


@pytest.fixture(scope="module")
def world(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    from tests.conftest import run_alembic

    name = f"srv_{uuid.uuid4().hex[:10]}"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    assert run_alembic(url, "upgrade", "head").returncode == 0
    tmp = tmp_path_factory.mktemp("season")
    root = tmp / "snapshots"
    a = export_snapshot(DS, root)
    st = Settings(
        database_url=url,
        snapshots_root=root,
        snapshot_check_seconds=0,
        artifact_dir=tmp / "artifacts",
        feature_store_dir=tmp / "features",
        n_sims=200,
        horizon_default=2,
        horizon_max=2,
        forecast_horizon=2,
        sync_horizon_limit=2,
    )
    svc = AppServices.build(st)
    assert svc.engine is not None
    _load_gameweeks(svc.engine)
    jobs_mod._job_forecast(svc, {})
    earlier = CUT[GW] - timedelta(hours=2)
    _backdate(svc.engine, a.name, earlier, earlier - timedelta(hours=1))
    cid = capture(svc.engine, st, svc.data, SEASON, GW, CUT[GW] + timedelta(minutes=5))
    with TestClient(create_app(st, svc)) as client:
        yield {"svc": svc, "settings": st, "root": root, "cid": cid, "client": client}
    svc.engine.dispose()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def _promote(world: dict[str, Any], ds: CanonicalDataset) -> str:
    time.sleep(1.1)  # manifests order by creation time
    snap = export_snapshot(ds, world["root"]).name
    world["svc"].data.refresh()
    assert world["svc"].data.snapshot_id == snap
    return snap


def _evaluations(world: dict[str, Any]) -> list[m.SeasonEvaluationRow]:
    with session_scope(world["svc"].engine) as s:
        rows = s.scalars(select(m.SeasonEvaluationRow).order_by(m.SeasonEvaluationRow.seq)).all()
        for r in rows:
            s.expunge(r)
    return list(rows)


def _run(world: dict[str, Any]) -> str:
    return season.run(world["svc"].engine, world["svc"].data)


def _counts(world: dict[str, Any]) -> dict[str, int]:
    with session_scope(world["svc"].engine) as s:
        return {
            t: int(s.scalar(text(f"SELECT count(*) FROM {t}")) or 0)
            for t in (
                "manager_state",
                "recommendations",
                "decision_journal",
                "forecast_checkpoints",
                "predictions",
                "forecast_promotions",
            )
        }


def test_nothing_is_scored_before_the_results_and_repeats_write_nothing(
    world: dict[str, Any],
) -> None:
    before = _counts(world)
    out = _run(world)
    assert out.startswith("season_eval:")
    (ev,) = _evaluations(world)
    assert ev.trigger == "first" and ev.seq == 1 and ev.previous_id is None
    assert {w["gw"]: w["state"] for w in ev.windows_json} == {2: "not_started", 3: "not_started"}
    assert not any(w["scored"] for w in ev.windows_json)
    assert ev.metrics_json["results"] == {}
    assert ev.metrics_json["calibration_state"]["state"] == "deferred"
    assert ev.checkpoint_ids == [world["cid"]]
    assert _run(world) == f"unchanged:{ev.id}"  # identical inputs: nothing written
    assert _counts(world) == before  # read-only for manager state, decisions and forecasts


def test_incomplete_results_are_a_window_state_never_zeros(world: dict[str, Any]) -> None:
    fx = DS["fixtures"]
    ids = sorted(int(i) for i in fx[(fx["season"] == SEASON) & (fx["gw"] == GW)]["fixture_id"])
    _promote(world, with_gameweek_results(DS, SEASON, GW, pts, fixture_ids=ids[:4]))
    assert _run(world).startswith("season_eval:")
    ev = _evaluations(world)[-1]
    assert ev.trigger == "record_change"
    assert {w["gw"]: w["state"] for w in ev.windows_json}[2] == "in_progress"
    assert ev.metrics_json["results"] == {}
    _promote(world, with_gameweek_results(DS, SEASON, GW, pts, final=False))
    _run(world)
    assert {w["gw"]: w["state"] for w in _evaluations(world)[-1].windows_json}[2] == "provisional"


def _final_with_exclusions() -> tuple[CanonicalDataset, int, list[int]]:
    """GW2 final, except: one player's result rows are missing, and one fixture was postponed
    to GW5 (both of its clubs' forecasts assumed it)."""
    ds = with_gameweek_results(DS, SEASON, GW, pts)
    frames = {k: v.copy() for k, v in ds.frames.items()}
    fx, pm = frames["fixtures"], frames["player_match"]
    gw_fx = fx[(fx["season"] == SEASON) & (fx["gw"] == GW)]
    moved = gw_fx.iloc[-1]
    fx.loc[moved.name, ["gw", "status", "finalized"]] = [5, "scheduled", False]
    pm = pm[~((pm["season"] == SEASON) & (pm["fixture_id"] == moved["fixture_id"]))]
    gone = int(pm[(pm["season"] == SEASON) & (pm["gw"] == GW)]["player_code"].iloc[0])
    pm = pm[~((pm["season"] == SEASON) & (pm["gw"] == GW) & (pm["player_code"] == gone))]
    frames["player_match"] = pm.reset_index(drop=True)
    clubs = [int(moved["home_team_code"]), int(moved["away_team_code"])]
    return CanonicalDataset(frames, dict(ds.meta)), gone, clubs


def test_final_results_are_scored_with_the_contract_statistics(world: dict[str, Any]) -> None:
    ds, gone, moved_clubs = _final_with_exclusions()
    world["final_ds"] = ds
    _promote(world, ds)
    assert _run(world).startswith("season_eval:")
    ev = _evaluations(world)[-1]
    assert ev.trigger == "new_gameweek_final"
    assert ev.changes_json["newly_scored"] == [2]
    windows = {w["gw"]: w for w in ev.windows_json}
    assert windows[2]["state"] == "final" and windows[2]["scored"]
    assert windows[3]["state"] == "not_started" and not windows[3]["scored"]

    # recompute the primary statistics by hand from the stored checkpoint and the results
    ck = load_checkpoint(world["svc"].engine, world["cid"])
    rows = ck.rows[ck.rows["target_gw"] == GW]
    pm = ds["player_match"]
    real = pm[(pm["season"] == SEASON) & (pm["gw"] == GW)].groupby("player_code")["points"].sum()
    excluded = rows["player_code"].eq(gone) | rows["team_code"].isin(moved_clubs)
    ex = ev.exclusions_json["by_reason"]
    assert ex["outcome_missing"] == 1  # the missing rows are never read as 0 points
    assert ex["schedule_changed"] == int(rows["team_code"].isin(moved_clubs).sum()) > 0
    assert sum(ex.values()) == int(excluded.sum())
    kept = rows[~excluded]
    y = kept["player_code"].map(real).to_numpy(float)
    h0 = ev.metrics_json["results"]["all"]["h0"]
    assert h0["G"] == 1 and h0["rows"] == len(kept) and h0["gameweeks"] == [2]
    d_mse = np.mean((kept["mean"] - y) ** 2) - np.mean((kept["pred_ppg_availability"] - y) ** 2)
    assert h0["comparisons"]["C1"]["per_gameweek"] == pytest.approx([d_mse])
    assert h0["levels"]["citl_6"] == pytest.approx(np.mean(kept["prob_6_plus"] - (y >= 6)))
    assert h0["comparisons"]["C1"]["interval_95"] is None  # one gameweek: values only
    assert h0["status"] == "too_few_gameweeks" and h0["looks"] == {}
    assert "h1" not in ev.metrics_json["results"]["all"]  # GW3 is not final: no horizon 1 yet
    assert set(ev.metrics_json["ece_null"]["all"]) == {"p6", "start"}
    assert ev.metrics_json["calibration_state"]["state"] == "deferred"


def test_a_corrected_official_result_writes_a_revision(world: dict[str, Any]) -> None:
    ds: CanonicalDataset = world["final_ds"]
    frames = {k: v.copy() for k, v in ds.frames.items()}
    pm = frames["player_match"]
    i = pm.index[(pm["season"] == SEASON) & (pm["gw"] == GW)][0]
    pm.loc[i, "points"] = pm.loc[i, "points"] + 3  # a bonus correction
    prev = _evaluations(world)[-1]
    _promote(world, CanonicalDataset(frames, dict(ds.meta)))
    _run(world)
    ev = _evaluations(world)[-1]
    assert ev.trigger == "outcome_correction" and ev.previous_id == prev.id
    assert ev.seq == prev.seq + 1
    (rev,) = ev.changes_json["revised"]
    assert rev["gw"] == 2 and rev["points_total"][1] == rev["points_total"][0] + 3
    assert [e.seq for e in _evaluations(world)] == list(range(1, ev.seq + 1))  # all kept


def test_a_process_on_older_data_never_writes(world: dict[str, Any]) -> None:
    st: Settings = world["settings"]
    oldest = min(
        (p for p in world["root"].glob("snap_*")),
        key=lambda p: (p / "manifest.json").stat().st_mtime,
    )
    stale = DataService(st.model_copy(update={"snapshot_dir": oldest}))
    n = len(_evaluations(world))
    out = season.run(world["svc"].engine, stale)
    assert out.startswith("stale:") and len(_evaluations(world)) == n


def test_the_season_review_endpoint_is_public_and_reads_stored_rows(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = world["client"]

    def no_work(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the Season Review must not compute or submit anything")

    monkeypatch.setattr(world["svc"].forecasts, "get", no_work)
    monkeypatch.setattr(jobs_mod.JobBackend, "submit", no_work)
    r = client.get("/api/v1/season-review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["season"] == SEASON
    states = {g["gameweek"]: g["state"] for g in body["timeline"]["gameweeks"]}
    assert states[1] == "before_record" and states[2] == "prospective"
    ev = body["evaluation"]
    assert ev["metrics"]["results"]["all"]["h0"]["G"] == 1
    assert ev["trigger"] == "outcome_correction"
    ref = body["reference"]
    assert ref is not None and set(ref["all"]["seasons"]) == {"2023-24", "2024-25", "2025-26"}
    assert ref["all"]["gameweeks"] == [2]  # the same weeks as the live record
    assert body["calibration"]["state"] == "deferred"
    assert body["method"]["looks"] == [8, 16]
    assert "manager_key" not in r.text  # public: no manager data
    hist = client.get("/api/v1/season-review/evaluations").json()["evaluations"]
    assert [e["seq"] for e in hist] == sorted((e["seq"] for e in hist), reverse=True)


def test_on_demand_evaluation_is_the_operators(world: dict[str, Any]) -> None:
    client = world["client"]
    user = _register(client, "season-user@example.test")["session_token"]
    r = client.post("/api/v1/season-review/evaluate", headers=_bearer(user))
    assert r.status_code == 403
    ok = client.post("/api/v1/season-review/evaluate")  # tests run without keys: operator
    assert ok.status_code == 202 and ok.json()["job"]["status"] == "succeeded"
    assert ok.json()["job"]["result_ref"].startswith("unchanged:")
    with session_scope(world["svc"].engine) as s:
        assert s.scalar(select(func.count()).select_from(m.SeasonEvaluationRow)) == len(
            _evaluations(world)
        )


def test_the_scheduler_submits_only_when_the_inputs_changed(world: dict[str, Any]) -> None:
    from fpl_worker import cli as worker_cli

    svc = world["svc"]
    backend = jobs_mod.JobBackend(svc)
    out = worker_cli.run_task("season_eval", svc, backend, "b1")
    assert out["status"] == "skipped" and out["reason"] == "unchanged"
    assert not pd.isna(out["evaluation"])
