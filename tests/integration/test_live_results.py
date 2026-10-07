"""Completed-match results of the live season (``element-summary`` histories, §6.1).

The archive's ``merged_gw`` *is* the API's per-player history, so the committed 2026-27 archive
excerpt can be served back as API payloads: the live path must reproduce the archive path's
canonical rows exactly, load nothing but results, stay idempotent, and never touch the
schedule or deadlines the bootstrap capture recorded.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import httpx
import pandas as pd
import pytest
import respx
from sqlalchemy import select

from fpl_api.services import DataService
from fpl_api.settings import Settings
from fpl_domain.rules import load_ruleset
from fpl_ingestion.canonical import PitPolicy, canonicalize
from fpl_ingestion.http import HttpConfig, HttpFetcher
from fpl_ingestion.live import results_frames
from fpl_ingestion.pipeline import ingest_live, ingest_live_results
from fpl_ingestion.quality import run_quality_gates
from fpl_ingestion.sources.fpl_api import FplApiClient
from fpl_storage import models as m
from fpl_storage.dataset import load_from_db
from fpl_storage.db import make_engine, session_scope
from fpl_storage.raw_store import RawStore
from tests.fixtures_util import API_DIR, SNAPSHOT_AT, VAASTAV_DIR, canonical_season, sources_cfg

BASE = "https://fantasy.premierleague.com/api"
SEASON = "2026-27"
ARCHIVE_ONLY = ("name", "position", "team", "GW", "xP")


def _histories() -> dict[int, list[dict[str, object]]]:
    """The archive's GW1 rows in the API's ``history`` shape (``GW`` is the API's ``round``)."""
    mg = pd.read_csv(VAASTAV_DIR / "data" / SEASON / "gws" / "merged_gw.csv")
    rows = json.loads(mg.to_json(orient="records"))
    out: dict[int, list[dict[str, object]]] = {}
    for r in rows:
        h = {k: v for k, v in r.items() if k not in ARCHIVE_ONLY}
        h["round"] = r["GW"]
        out.setdefault(int(r["element"]), []).append(h)
    return out


def _bootstrap() -> dict[str, object]:
    """The committed API-shaped bootstrap, with the team fields its reshaping dropped restored
    from the archive's ``teams.csv`` (a dump of the same API object; the live API sends them)."""
    boot = json.loads((API_DIR / "bootstrap-static.json").read_text())
    teams = pd.read_csv(VAASTAV_DIR / "data" / SEASON / "teams.csv").set_index("id")
    for t in boot["teams"]:
        for col in ("strength_overall_home", "strength_overall_away"):
            t.setdefault(col, int(teams.loc[t["id"], col]))
    return boot


BOOT = _bootstrap()
FIXTURES = json.loads((API_DIR / "fixtures.json").read_text())


def _mock_api(histories: dict[int, list[dict[str, object]]], *, broken: int | None = None) -> None:
    respx.get(f"{BASE}/bootstrap-static/").mock(return_value=httpx.Response(200, json=BOOT))
    respx.get(f"{BASE}/fixtures/").mock(return_value=httpx.Response(200, json=FIXTURES))

    def summary(request: httpx.Request) -> httpx.Response:
        m_ = re.search(r"/element-summary/(\d+)/", str(request.url))
        assert m_ is not None
        eid = int(m_.group(1))
        if broken is not None and eid == broken:
            return httpx.Response(503)
        body = {"fixtures": [], "history": histories.get(eid, []), "history_past": []}
        return httpx.Response(200, json=body)

    respx.get(url__regex=rf"{BASE}/element-summary/\d+/").mock(side_effect=summary)


def _client() -> FplApiClient:
    return FplApiClient(
        HttpFetcher(
            HttpConfig(
                allowed_hosts=("fantasy.premierleague.com",),
                max_retries=2,
                backoff_initial_seconds=0.001,
                backoff_max_seconds=0.002,
            )
        )
    )


def test_live_histories_reproduce_the_archive_rows() -> None:
    frames = run_quality_gates(results_frames(SEASON, BOOT, FIXTURES, _histories()))
    assert frames.gate_passed, [i.message for i in frames.issues]
    live = canonicalize(
        frames,
        load_ruleset(SEASON),
        PitPolicy.from_config(sources_cfg()),
        SNAPSHOT_AT,
        source="fpl_api",
    )
    archive = canonical_season(SEASON)
    key = ["fixture_id", "player_code"]
    for table in ("player_match", "team_match"):
        got = live.frames()[table].sort_values(
            key if table == "player_match" else ["fixture_id", "team_code"]
        )
        want = archive.frames()[table].sort_values(
            key if table == "player_match" else ["fixture_id", "team_code"]
        )
        pd.testing.assert_frame_equal(
            got.reset_index(drop=True), want.reset_index(drop=True), check_dtype=False
        )
    pm = live.frames()["player_match"]
    assert (pm["available_at"] > pm["kickoff_at"]).all()  # known only after the match


@pytest.mark.integration
@respx.mock
def test_live_results_load_only_results_idempotently(fresh_db: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine(fresh_db)
    store = RawStore(tmp_path / "raw")
    _mock_api(_histories())
    assert ingest_live(engine, _client(), store, SEASON).status == "succeeded"
    before = load_from_db(engine, [SEASON])
    assert before["player_match"].empty
    stale = DataService(Settings(), before).current(datetime(2026, 8, 25, tzinfo=UTC))
    assert any("match results missing" in r for r in stale.degraded_reasons)

    res = ingest_live_results(engine, _client(), store, SEASON)
    assert res.status == "succeeded", res.issues
    after = load_from_db(engine, [SEASON])
    archive = canonical_season(SEASON).frames()["player_match"]
    assert len(after["player_match"]) == len(archive) and set(after["player_match"]["gw"]) == {1}
    # results only: deadlines, schedule availability, players and teams stay as captured
    for table in ("gameweeks", "fixtures", "players", "teams", "player_snapshots"):
        pd.testing.assert_frame_equal(before[table], after[table], obj=table)
    # point-in-time price: the price at the fixture, not today's price
    row = after["player_match"].set_index(["fixture_id", "player_code"]).sort_index()
    csv = pd.read_csv(VAASTAV_DIR / "data" / SEASON / "gws" / "merged_gw.csv").iloc[0]
    code = next(e["code"] for e in BOOT["elements"] if e["id"] == csv["element"])
    assert row.loc[(csv["fixture"], code), "price"] == csv["value"]
    with session_scope(engine) as s:
        lineage = set(s.scalars(select(m.PlayerGwStatsRow.raw_snapshot_id)).all())
    assert lineage == {res.raw_ids["merged_gw"]}  # every row cites the captured bundle
    fresh = DataService(Settings(), after).current(datetime(2026, 8, 25, tzinfo=UTC))
    assert not any("match results missing" in r for r in fresh.degraded_reasons)

    again = ingest_live_results(engine, _client(), store, SEASON)
    assert again.status == "succeeded"
    for table in ("player_gw_stats", "team_gw_stats"):
        assert again.counts[table]["inserted"] == again.counts[table]["updated"] == 0, table


@pytest.mark.integration
@respx.mock
def test_bad_or_unavailable_results_change_nothing(fresh_db: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine(fresh_db)
    store = RawStore(tmp_path / "raw")
    hist = _histories()
    _mock_api(hist)
    assert ingest_live(engine, _client(), store, SEASON).status == "succeeded"
    # schema drift in the payload → quarantined by the archive's contract, nothing loaded
    eid = next(iter(hist))
    bad = {k: [dict(h) for h in v] for k, v in hist.items()}
    bad[eid][0]["total_points"] = "lots"
    respx.reset()
    _mock_api(bad)
    assert ingest_live_results(engine, _client(), store, SEASON).status == "quarantined"
    assert load_from_db(engine, [SEASON])["player_match"].empty
    # the source failing mid-way → failed, nothing loaded
    respx.reset()
    _mock_api(hist, broken=eid)
    assert ingest_live_results(engine, _client(), store, SEASON).status == "failed"
    assert load_from_db(engine, [SEASON])["player_match"].empty


@pytest.mark.integration
@respx.mock
def test_bootstrap_schema_drift_is_quarantined_not_left_running(fresh_db: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine(fresh_db)
    drifted = json.loads(json.dumps(BOOT))
    drifted["elements"][0]["now_cost"] = "a lot"
    respx.get(f"{BASE}/bootstrap-static/").mock(return_value=httpx.Response(200, json=drifted))
    respx.get(f"{BASE}/fixtures/").mock(return_value=httpx.Response(200, json=FIXTURES))
    res = ingest_live(engine, _client(), RawStore(tmp_path / "raw"), SEASON)
    assert res.status == "quarantined"
    with session_scope(engine) as s:
        assert s.scalars(select(m.DataJob.status)).all() == ["quarantined"]  # never "running"
