"""The forecast record (M1.5), end to end on a real database and a real forecast: the forecast
job commits to a serving forecast (file hash, export time, database clock) before the ready
marker; after the cutoff the forecast served then is frozen — rows, context, baselines and exact
distributions — only once, whatever repeats; it outlives the forecast cache, the snapshot and the
process; and nothing is ever recomputed in place of a forecast that was not there.

The synthetic 2026-27 season's GW2 deadline is 2026-08-28T17:30Z (cutoff 16:00Z). Its forecast
is computed now, so the test moves its recorded promotion and export times to before that
cutoff — the record of a forecast promoted then — and every check reads those recorded times.
"""

from __future__ import annotations

import functools
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import joblib
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url

from fpl_api import jobs as jobs_mod
from fpl_api import ledger
from fpl_api.container import AppServices
from fpl_api.jobs import JobBackend
from fpl_api.ledger import (
    capture,
    due_gameweeks,
    forecast_path,
    load_checkpoint,
    pending_candidates,
    sha256_file,
)
from fpl_api.provenance import code_digest
from fpl_api.retention import prune
from fpl_api.services import mark_serving_ready
from fpl_api.settings import Settings
from fpl_domain.rules import load_ruleset
from fpl_forecasting import distributions as dist
from fpl_storage import models as m
from fpl_storage.dataset import export_snapshot
from fpl_storage.db import session_scope
from tests.fixtures_util import fixture_dataset, with_gameweek_results

SEASON = "2026-27"
DS = fixture_dataset("2024-25", "2025-26", "2026-27")
CUT = {  # decision cutoffs of the synthetic season (deadline − 90 min)
    int(r.gw): pd.Timestamp(r.deadline_at).to_pydatetime() - timedelta(minutes=90)
    for r in DS["gameweeks"][DS["gameweeks"]["season"] == SEASON].itertuples()
}


def _load_gameweeks(engine: Any) -> None:
    """The ``gameweeks`` table as the live ingestion keeps it (official deadlines)."""
    g = DS["gameweeks"][DS["gameweeks"]["season"] == SEASON]
    with session_scope(engine) as s:
        season = m.SeasonRow(
            season_code=SEASON, ruleset_version=load_ruleset(SEASON).ruleset_version
        )
        s.add(season)
        s.flush()
        for r in g.itertuples():
            s.add(
                m.GameweekRow(
                    season_id=season.id,
                    gw=int(r.gw),
                    deadline_at=pd.Timestamp(r.deadline_at).to_pydatetime(),
                    started_at=pd.Timestamp(r.first_kickoff_at).to_pydatetime(),
                    status=str(r.status),
                    deadline_source="official",
                )
            )


@pytest.fixture(scope="module")
def world(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    from tests.conftest import run_alembic

    name = f"ldg_{uuid.uuid4().hex[:10]}"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    assert run_alembic(url, "upgrade", "head").returncode == 0
    tmp = tmp_path_factory.mktemp("ledger")
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
        snapshot_retention_keep=1,
    )
    svc = AppServices.build(st)
    assert svc.engine is not None
    _load_gameweeks(svc.engine)
    key = jobs_mod._job_forecast(svc, {})  # computes, records the promotion, marks it ready
    fc = joblib.load(forecast_path(st, key))
    assert fc.decision_gw == 2
    yield {
        "svc": svc,
        "settings": st,
        "engine": svc.engine,
        "snap": a.name,
        "key": key,
        "fc": fc,
        "root": root,
    }
    svc.engine.dispose()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def _points(code: int, shift: int) -> int:
    return code % 5 + shift


def _backdate(engine: Any, snapshot: str, promoted: datetime, exported: datetime) -> None:
    with session_scope(engine) as s:
        s.execute(
            update(m.ForecastPromotionRow)
            .where(m.ForecastPromotionRow.snapshot_id == snapshot)
            .values(promoted_at=promoted, snapshot_created_at=exported)
        )


def _count(engine: Any, model: Any, *where: Any) -> int:
    with session_scope(engine) as s:
        return int(s.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def test_the_forecast_job_commits_to_the_forecast_before_marking_it_ready(
    world: dict[str, Any],
) -> None:
    st, key, fc = world["settings"], world["key"], world["fc"]
    with session_scope(world["engine"]) as s:
        promo = s.get(m.ForecastPromotionRow, world["snap"])
        assert promo is not None
        assert (promo.season, promo.gameweek, promo.forecast_key) == (SEASON, 2, key)
        assert promo.prediction_run_id == fc.provenance["prediction_run_id"]
        assert promo.artifact_sha256 == sha256_file(forecast_path(st, key))
        # the digest of the code that computed the numbers travels with the forecast
        assert promo.code_digest == fc.provenance["code_digest"] == code_digest()
    assert (st.artifact_dir / "serving" / f"{world['snap']}.ready").exists()
    # the forecast job runs again for the same snapshot: the first promotion is kept
    first = promo.promoted_at
    jobs_mod._job_forecast(world["svc"], {})
    with session_scope(world["engine"]) as s:
        assert s.get(m.ForecastPromotionRow, world["snap"]).promoted_at == first  # type: ignore[union-attr]
    # record the promotion as having happened before the GW2 cutoff (see the module docstring)
    _backdate(
        world["engine"], world["snap"], CUT[2] - timedelta(hours=2), CUT[2] - timedelta(hours=3)
    )


def test_a_gameweek_is_due_only_after_its_cutoff_and_only_once_the_record_exists(
    world: dict[str, Any],
) -> None:
    eng = world["engine"]
    assert due_gameweeks(eng, CUT[2] - timedelta(minutes=1)) == []
    due = due_gameweeks(eng, CUT[2] + timedelta(minutes=1))
    # GW1's cutoff precedes the first recorded promotion: it predates the record, never due
    assert [g.gameweek for g in due] == [2]
    assert due[0].cutoff == CUT[2]
    assert capture(eng, world["settings"], world["svc"].data, SEASON, 1, CUT[2]) == "before_record"
    assert (
        capture(eng, world["settings"], world["svc"].data, SEASON, 2, CUT[2] - timedelta(seconds=1))
        == "not_due"
    )
    assert _count(eng, m.ForecastCheckpointRow) == 0


def test_retention_keeps_an_uncaptured_candidate_whatever_its_age(world: dict[str, Any]) -> None:
    st, eng = world["settings"], world["engine"]
    assert pending_candidates(eng) == {world["snap"]: world["key"]}
    # newer exports push the candidate out of the newest snapshots; its forecast file is old
    time.sleep(1.1)
    newer = []
    for i in (1, 2):  # later exports with GW2 results (different content, different ids)
        time.sleep(1.1)  # manifests order by creation time
        ds = with_gameweek_results(DS, SEASON, 2, functools.partial(_points, shift=i))
        newer.append(export_snapshot(ds, world["root"]).name)
    assert world["snap"] not in newer and len(set(newer)) == 2
    path = forecast_path(st, world["key"])
    t = time.time() - 3 * 86400
    os.utime(path, (t, t))
    (st.artifact_dir / "serving" / f"{world['snap']}.ready").unlink()  # even without a marker
    rep = prune(st, eng)
    assert world["snap"] in rep.kept and path.exists()
    assert (world["root"] / world["snap"] / "manifest.json").exists()
    mark_serving_ready(st, world["snap"], world["key"])


def test_capture_freezes_the_forecast_served_at_the_cutoff(world: dict[str, Any]) -> None:
    eng, fc = world["engine"], world["fc"]
    cid = capture(
        eng, world["settings"], world["svc"].data, SEASON, 2, CUT[2] + timedelta(minutes=5)
    )
    ck = load_checkpoint(eng, cid)
    h = ck.header
    assert h.state == "prospective", (h.reasons, h.checks)
    assert all(v["ok"] for v in h.checks.values()), h.checks
    assert (h.season, h.gameweek, h.kind, h.cutoff_at) == (SEASON, 2, "live", CUT[2])
    assert h.prediction_run_id == fc.provenance["prediction_run_id"]
    assert h.artifact_sha256 == sha256_file(forecast_path(world["settings"], world["key"]))
    assert h.code_digest == fc.provenance["code_digest"]
    assert h.capture_code_digest == code_digest()
    assert h.build_revision == "unrecorded"  # the test build declared none: never inferred
    assert h.promoted_at is not None and h.snapshot_created_at is not None
    assert h.promoted_at < h.cutoff_at and h.snapshot_created_at < h.cutoff_at

    # every row is the forecast's own number, for every horizon
    s = fc.summary.sort_values(["player_code", "gw"]).reset_index(drop=True)
    r = ck.rows.sort_values(["player_code", "target_gw"]).reset_index(drop=True)
    assert len(r) == len(s) == h.n_rows
    for col in ("mean", "p10", "p90", "std", "prob_6_plus", "expected_minutes"):
        assert np.array_equal(r[col].to_numpy(), s[col].to_numpy()), col
    assert np.array_equal(r["start_probability"].to_numpy(), s["prob_start"].to_numpy())
    assert np.allclose(
        [c["goals"] for c in r["components_json"]], s["xp_goals"].to_numpy(), rtol=0, atol=0
    )
    # decision-time context and baselines, fitted on the forecast's own snapshot
    h0 = r[r["horizon"] == 0]
    assert h0["pred_ppg_availability"].notna().all() and h0["base_p_start"].notna().all()
    assert r["fixture_ids"].notna().all() and r["team_code"].notna().all()
    # unknown for players without earlier matches, as in the feature frame (never filled here)
    assert r["start_rate_long"].notna().mean() > 0.9
    assert (
        r["p_start_before_availability"]
        >= r["components_json"].map(lambda c: c["start_probability_model"]) - 1e-9
    ).all()
    assert h.populations_json["h0_rows"] == len(h0)

    # the distributions are the forecast's samples exactly (as counts)
    sim = fc.simulation
    pi = np.searchsorted(sim.player_codes, r["player_code"].to_numpy())
    gi = np.searchsorted(sim.gameweeks, r["target_gw"].to_numpy())
    want = np.sort(sim.points[:, pi, gi].T.astype(float), axis=1)
    assert ck.mc_counts is not None and ck.mc_lo is not None
    assert np.array_equal(dist.decode(ck.mc_lo, ck.mc_counts), want)
    assert ck.clim_counts is not None and ck.clim_counts.sum(axis=1).min() > 0
    world["cid"] = cid


def test_a_concurrent_capture_finds_the_first_one(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    eng, cid = world["engine"], world["cid"]
    n_rows, n_blobs = _count(eng, m.PredictionRow), _count(eng, m.ArtifactBlob)
    real, calls = ledger._existing, {"n": 0}

    def racing(engine: Any, season: str, gw: int) -> str | None:
        calls["n"] += 1  # the other writer commits between this check and our insert
        return None if calls["n"] == 1 else real(engine, season, gw)

    monkeypatch.setattr(ledger, "_existing", racing)
    assert capture(eng, world["settings"], world["svc"].data, SEASON, 2) == cid
    assert calls["n"] == 2  # rebuilt everything, lost the insert race, returned the winner
    assert _count(eng, m.PredictionRow) == n_rows and _count(eng, m.ArtifactBlob) == n_blobs


def test_capturing_again_writes_nothing_and_a_recomputed_forecast_is_a_new_version(
    world: dict[str, Any],
) -> None:
    eng, cid, svc = world["engine"], world["cid"], world["svc"]
    n_rows = _count(eng, m.PredictionRow)
    n_blobs = _count(eng, m.ArtifactBlob)
    assert capture(eng, world["settings"], svc.data, SEASON, 2) == cid
    backend = JobBackend(world["svc"])
    job = backend.submit("forecast_capture", {"season": SEASON, "gameweek": 2})
    assert backend.get(job)["result_ref"] == cid  # type: ignore[index]
    assert _count(eng, m.ForecastCheckpointRow) == 1
    assert _count(eng, m.PredictionRow) == n_rows
    assert _count(eng, m.ArtifactBlob) == n_blobs
    assert pending_candidates(eng, CUT[2] + timedelta(minutes=10)) == {}
    assert 2 not in [g.gameweek for g in due_gameweeks(eng)]
    # the newest export gets its own forecast: a distinct promotion; the checkpoint is unchanged
    svc.data.refresh()
    newer_key = jobs_mod._job_forecast(svc, {})
    assert newer_key != world["key"]
    with session_scope(eng) as s:
        promos = s.scalars(select(m.ForecastPromotionRow)).all()
        assert {p.forecast_key for p in promos} >= {world["key"], newer_key}
        assert len({p.artifact_sha256 for p in promos}) == len(promos)
        h = s.get(m.ForecastCheckpointRow, cid)
        assert h is not None and h.forecast_key == world["key"]
    assert capture(eng, world["settings"], svc.data, SEASON, 2) == cid


def test_the_checkpoint_outlives_the_cache_the_snapshot_and_the_process(
    world: dict[str, Any],
) -> None:
    st, eng, cid = world["settings"], world["engine"], world["cid"]
    before = load_checkpoint(eng, cid)
    # retention prunes the forecast cache and the snapshot once captured; restart the services
    forecast_path(st, world["key"]).unlink()
    shutil.rmtree(st.feature_store_dir, ignore_errors=True)
    fresh = AppServices.build(st)
    assert fresh.engine is not None
    after = load_checkpoint(fresh.engine, cid)
    pd.testing.assert_frame_equal(
        before.rows.drop(columns=["id"]), after.rows.drop(columns=["id"]), check_exact=True
    )
    assert np.array_equal(before.mc_counts, after.mc_counts)  # type: ignore[arg-type]
    assert after.header.rows_sha256 == before.header.rows_sha256
    fresh.engine.dispose()


def test_a_gameweek_without_its_own_forecast_is_missed_and_never_recomputed(
    world: dict[str, Any],
) -> None:
    eng, st, data = world["engine"], world["settings"], world["svc"].data
    # at GW3's cutoff the API still served the GW2 forecast: GW3 had none of its own
    cid3 = capture(eng, st, data, SEASON, 3, CUT[3] + timedelta(minutes=5))
    # a GW4 forecast promoted before GW4's cutoff whose file is gone by the capture
    with session_scope(eng) as s:
        s.add(
            m.ForecastPromotionRow(
                snapshot_id="snap_gone",
                season=SEASON,
                gameweek=4,
                forecast_key="fc_gone",
                prediction_run_id="pred_gone",
                snapshot_created_at=CUT[4] - timedelta(hours=2),
                artifact_sha256="0" * 64,
                promoted_at=CUT[4] - timedelta(hours=1),
            )
        )
    cid4 = capture(eng, st, data, SEASON, 4, CUT[4] + timedelta(minutes=5))
    with session_scope(eng) as s:
        h3, h4 = s.get(m.ForecastCheckpointRow, cid3), s.get(m.ForecastCheckpointRow, cid4)
        assert h3 is not None and h4 is not None
        assert h3.state == h4.state == "missed"
        assert h3.reasons == ["the forecast served at the cutoff was for GW2"]
        assert h4.reasons == ["the forecast file was deleted before capture"]
        assert h4.forecast_key == "fc_gone" and h4.n_rows == 0
    assert _count(eng, m.PredictionRow, m.PredictionRow.checkpoint_id.in_([cid3, cid4])) == 0
    assert capture(eng, st, data, SEASON, 4) == cid4  # missed stays missed


def test_rows_of_a_checkpoint_cannot_be_deleted_from_under_it(world: dict[str, Any]) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), session_scope(world["engine"]) as s:
        s.execute(text("DELETE FROM forecast_checkpoints WHERE id = :id"), {"id": world["cid"]})
