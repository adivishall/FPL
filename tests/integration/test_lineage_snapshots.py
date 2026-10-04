"""Traceability across snapshot swaps (§76.2).

Live captures produce a new data snapshot every refresh, while the features at a decision cutoff
are often identical (content-addressed feature id). A recommendation made on the newer snapshot
must still trace back to *that* snapshot — regression for a broken chain seen in the deployed
stack — and a live recommendation made before its (future) decision cutoff is valid.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from fpl_api.app import create_app
from fpl_api.container import AppServices
from fpl_api.settings import Settings
from fpl_storage.dataset import CanonicalDataset
from tests.fixtures_util import fixture_dataset

DS1 = fixture_dataset("2025-26", "2026-27")


def _later_capture(ds: CanonicalDataset) -> CanonicalDataset:
    """The same data plus one news item published far after any cutoff: a different snapshot
    id, identical point-in-time features."""
    frames = dict(ds.frames)
    news = frames["news_signals"]
    row = news.iloc[[0]].copy()
    late = pd.Timestamp("2030-01-01", tz="UTC")
    for col in ("published_at", "available_at", "captured_at"):
        if col in row:
            row[col] = late
    if "news" in row:
        row["news"] = "late capture"
    frames["news_signals"] = pd.concat([news, row], ignore_index=True)
    return CanonicalDataset(frames)


def _recommend(ds: CanonicalDataset, db: str, tmp: Path) -> tuple[TestClient, dict]:  # type: ignore[type-arg]
    st = Settings(
        database_url=db,
        artifact_dir=tmp / "artifacts",
        n_sims=200,
        horizon_default=2,
        horizon_max=2,
        forecast_horizon=2,
        sync_horizon_limit=2,
    )
    c = TestClient(create_app(st, AppServices.build(st, ds)))
    sq = c.post("/api/v1/optimize/squad", json={"budget": 1000, "horizon": 2}).json()
    c.post(
        "/api/v1/squad",
        json={
            "manager_key": "trace",
            "picks": [{"player_code": p["player_code"]} for p in sq["squad"]],
            "bank": sq["bank_after"],
            "free_transfers": 1,
        },
    ).raise_for_status()
    body = {"manager_key": "trace", "horizon": 2, "stability": False, "scenarios": False}
    job = c.post("/api/v1/recommendations/generate", json={**body, "chips": False}).json()
    assert c.get(f"/api/v1/jobs/{job['job_id']}").json()["status"] == "succeeded"
    return c, c.get("/api/v1/recommendations/current", params={"manager_key": "trace"}).json()


def test_trace_follows_the_recommendations_own_snapshot(fresh_db: str, tmp_path: Path) -> None:
    ds2 = _later_capture(DS1)
    assert ds2.snapshot_id != DS1.snapshot_id
    _, rec1 = _recommend(DS1, fresh_db, tmp_path / "a")
    c2, rec2 = _recommend(ds2, fresh_db, tmp_path / "b")
    assert rec1["snapshot_id"] == DS1.snapshot_id and rec2["snapshot_id"] == ds2.snapshot_id
    for rec in (rec1, rec2):
        t = c2.get(f"/api/v1/recommendations/{rec['id']}/trace").json()
        assert t["complete"] is True, [x for x in t["checks"] if not x["ok"]]
        assert t["chain"]["data_snapshot"]["id"] == rec["snapshot_id"]
    f1 = c2.get(f"/api/v1/recommendations/{rec1['id']}/trace").json()["chain"]["feature_snapshot"]
    f2 = c2.get(f"/api/v1/recommendations/{rec2['id']}/trace").json()["chain"]["feature_snapshot"]
    # same feature content, two materialisations — one per data snapshot
    assert f1["id"].split("@")[0] == f2["id"].split("@")[0] and f1["id"] != f2["id"]
