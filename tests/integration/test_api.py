"""API contract tests (§32, §72, §75) against PostgreSQL and the real data excerpt.

The excerpt's latest season is 2026-27 with one live snapshot (valid for GW2), so the API serves
GW2 decisions in degraded mode (the snapshot is older than the freshness SLA) — exactly the
behaviour the product must show when the live source is unreachable.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fpl_api.app import create_app
from fpl_api.container import AppServices
from fpl_api.security import hash_key
from fpl_api.settings import Settings
from tests.fixtures_util import fixture_dataset

DS = fixture_dataset("2024-25", "2025-26", "2026-27")


def _settings(db: str | None, tmp: Path, **kw) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(
        database_url=db,
        artifact_dir=tmp / "artifacts",
        n_sims=200,
        horizon_default=2,
        horizon_max=2,
        forecast_horizon=2,
        sync_horizon_limit=2,
        **kw,
    )


@pytest.fixture(scope="module")
def client(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    import uuid

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from tests.conftest import run_alembic

    name = f"api_{uuid.uuid4().hex[:10]}"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    assert run_alembic(url, "upgrade", "head").returncode == 0
    tmp = tmp_path_factory.mktemp("api")
    settings = _settings(url, tmp)
    app = create_app(settings, AppServices.build(settings, DS))
    with TestClient(app) as c:
        yield c
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture(scope="module")
def squad(client: TestClient) -> list[int]:
    r = client.post("/api/v1/optimize/squad", json={"budget": 1000, "horizon": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["squad"]) == 15 and body["bank_after"] >= 0
    codes = [p["player_code"] for p in body["squad"]]
    r = client.post(
        "/api/v1/squad",
        json={
            "manager_key": "demo",
            "picks": [{"player_code": c} for c in codes],
            "bank": body["bank_after"],
            "free_transfers": 1,
        },
    )
    assert r.status_code == 200, r.text
    return codes


def test_health_reports_degraded_mode_honestly(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["dataset"]["ok"] and body["checks"]["database"]["ok"]
    assert body["checks"]["live_source"]["ok"] is False
    fr = body["freshness"]
    assert fr["season"] == "2026-27" and fr["gameweek"] == 2
    assert fr["degraded"] and body["status"] == "degraded"
    assert client.get("/api/v1/system/health").json()["checks"] == body["checks"]
    assert "x-request-id" in r.headers


def test_health_live_source_reflects_the_latest_capture(client: TestClient) -> None:
    """The live-source check reports recorded capture outcomes, never a hard-coded claim."""
    from datetime import UTC, datetime, timedelta

    from fpl_storage import models as m
    from fpl_storage.db import session_scope

    engine = client.app.state.services.engine  # type: ignore[attr-defined]
    t0 = datetime.now(UTC)

    def record(job_id: str, status: str, at: datetime) -> None:
        with session_scope(engine) as s:
            s.add(
                m.DataJob(
                    id=job_id,
                    job_type="live_bootstrap",
                    source="fpl_api",
                    params={},
                    status=status,
                    started_at=at,
                    completed_at=at,
                )
            )

    try:
        record("job_live_test_ok", "succeeded", t0 - timedelta(minutes=5))
        live = client.get("/api/v1/health").json()["checks"]["live_source"]
        assert live["ok"] is True and live["last_status"] == "succeeded"
        record("job_live_test_fail", "failed", t0)
        live = client.get("/api/v1/health").json()["checks"]["live_source"]
        assert live["ok"] is False and live["last_status"] == "failed"
        assert live["last_success_at"] is not None  # the last good capture is still reported
    finally:
        with session_scope(engine) as s:
            for j in ("job_live_test_ok", "job_live_test_fail"):
                row = s.get(m.DataJob, j)
                if row is not None:
                    s.delete(row)


def test_current_gameweek_and_players(client: TestClient) -> None:
    gw = client.get("/api/v1/gameweeks/current").json()
    assert gw["gameweek"] == 2 and gw["decision_cutoff"] < gw["deadline"]
    r = client.get("/api/v1/players", params={"position": "MID", "limit": 5, "sort": "xp"})
    assert r.status_code == 200, r.text
    ps = r.json()["players"]
    assert len(ps) == 5 and all(p["position"] == "MID" for p in ps)
    assert [p["xp"] for p in ps] == sorted((p["xp"] for p in ps), reverse=True)
    code = ps[0]["player_code"]
    f = client.get(f"/api/v1/players/{code}/forecast").json()
    assert f["gameweeks"] and f["gameweeks"][0]["gw"] == 2
    assert f["provenance"]["prediction_run_id"].startswith("pred_")
    prof = client.get(f"/api/v1/players/{code}").json()
    assert prof["player"]["player_code"] == code and "freshness" in prof
    pr = prof["price_risk"]  # engine model and official predictor, side by side (ADR-0001 #20)
    assert pr["official"] is not None and isinstance(pr["official"]["price_change_percent"], float)
    assert pr["engine"] is not None and 0 <= pr["engine"]["p_rise"] <= 1
    assert pr["engine"]["model"].startswith("models/price_change@")
    assert client.get("/api/v1/players/99999999").status_code == 404


def test_manual_squad_validation(client: TestClient, squad: list[int]) -> None:
    bad = client.post(
        "/api/v1/squad",
        json={"manager_key": "bad", "picks": [{"player_code": c} for c in squad[:14]], "bank": 0},
    )
    assert bad.status_code == 422
    dup = client.post(
        "/api/v1/squad",
        json={"manager_key": "bad", "picks": [{"player_code": squad[0]}] * 15, "bank": 0},
    )
    assert dup.status_code == 422
    got = client.get("/api/v1/squad", params={"manager_key": "demo"}).json()
    assert sorted(p["player_code"] for p in got["state"]["squad"]) == sorted(squad)
    assert client.get("/api/v1/squad", params={"manager_key": "nobody"}).status_code == 404


def test_lineup_optimize_replacement(client: TestClient, squad: list[int]) -> None:
    lu = client.post("/api/v1/lineup", json={"manager_key": "demo"}).json()
    assert len(lu["lineup"]["starters"]) == 11
    assert lu["captaincy"]["expected"] in lu["lineup"]["starters"]
    opt = client.post(
        "/api/v1/optimize", json={"manager_key": "demo", "horizon": 2, "alternatives": 1}
    )
    assert opt.status_code == 200, opt.text
    o = opt.json()
    assert o["decision"]["action"] in ("HOLD", "TRANSFER", "HIT", "CHIP")
    assert o["hold"]["label"] == "hold"
    out = lu["lineup"]["starters"][3]
    rep = client.post(
        "/api/v1/replacements",
        json={"manager_key": "demo", "out_player": out, "horizon": 2, "candidates": 3},
    )
    assert rep.status_code == 200, rep.text
    cands = rep.json()["candidates"]
    assert cands and all(c["out"]["player_id"] == out for c in cands)
    assert all(0 <= c["probability_positive"] <= 1 for c in cands)
    # /replacement and /replacements share one handler (ADR-0001 #3)
    assert (
        client.post(
            "/api/v1/replacement", json={"manager_key": "demo", "out_player": out, "horizon": 2}
        ).status_code
        == 200
    )


def test_recommendation_lifecycle_and_journal(client: TestClient, squad: list[int]) -> None:
    r = client.post(
        "/api/v1/recommendations/generate",
        json={
            "manager_key": "demo",
            "horizon": 2,
            "stability": False,
            "scenarios": True,
            "chips": False,
        },
    )
    assert r.status_code == 202
    job = client.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "succeeded", job
    cur = client.get("/api/v1/recommendations/current", params={"manager_key": "demo"})
    assert cur.status_code == 200, cur.text
    rec = cur.json()
    assert rec["id"] == job["result_ref"] and rec["status"] == "active"
    assert rec["decision"]["action"] in ("HOLD", "TRANSFER", "HIT", "CHIP")
    assert "## Reproducibility" in rec["markdown"]
    assert rec["freshness"]["degraded"] is True
    assert any("stale" in a or "expired" in a for a in rec["assumptions"])
    detail = client.get(f"/api/v1/recommendations/{rec['id']}").json()
    assert detail["optimizer_run_id"] == rec["optimizer_run_id"]
    fb = client.post(
        f"/api/v1/decisions/{rec['id']}/feedback", json={"followed": "followed", "note": "did it"}
    )
    assert fb.status_code == 200
    j = client.get("/api/v1/decisions", params={"manager_key": "demo"}).json()["journal"]
    assert j[0]["id"] == rec["id"] and j[0]["feedback"][0]["followed"] == "followed"
    # identical request → same job (de-duplicated), same recommendation
    again = client.post(
        "/api/v1/recommendations/generate",
        json={
            "manager_key": "demo",
            "horizon": 2,
            "stability": False,
            "scenarios": True,
            "chips": False,
        },
    )
    assert again.json()["job_id"] == r.json()["job_id"]


def test_what_if_and_chips(client: TestClient, squad: list[int]) -> None:
    lu = client.post("/api/v1/lineup", json={"manager_key": "demo"}).json()["lineup"]
    cap = lu["captain"]
    w = client.post(
        "/api/v1/what-if",
        json={
            "manager_key": "demo",
            "horizon": 2,
            "scenarios": [{"kind": "injury_shock", "players": [cap], "gameweeks": [2]}],
        },
    )
    assert w.status_code == 200, w.text
    sc = w.json()["scenarios"][0]
    assert sc["hold_points_scenario"] <= sc["hold_points_base"] + 1e-9
    ch = client.post(
        "/api/v1/chips/simulate",
        json={"manager_key": "demo", "horizon": 2, "chip_id": "bench_boost_1", "gameweek": 2},
    )
    assert ch.status_code == 200, ch.text
    body = ch.json()
    assert body["what_if"]["chip_id"] == "bench_boost_1"
    assert {c["chip_id"] for c in body["chips"]} >= {"bench_boost_1", "triple_captain_1"}


def test_sync_degrades_when_live_source_is_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fpl_api.app as app_mod
    from fpl_ingestion.http import SourceUnavailableError
    from fpl_ingestion.sources import fpl_api_schemas as fs

    def fail(exc: Exception):  # type: ignore[no-untyped-def]
        def boom(*_a, **_k):  # type: ignore[no-untyped-def]
            raise exc

        monkeypatch.setattr(app_mod.FplApiClient, "from_config", classmethod(lambda c, f: boom()))

    body = {"manager_key": "demo", "manager_id": 123}
    fail(SourceUnavailableError("egress denied"))  # what the HTTP layer raises on outages
    r = client.post("/api/v1/squad/sync", json=body)
    assert r.status_code == 503 and r.json()["degraded"] is True
    try:  # upstream schema drift: reported as such, not as an outage
        fs.ApiEntry.model_validate({})
    except Exception as exc:
        fail(exc)
    r = client.post("/api/v1/squad/sync", json=body)
    assert r.status_code == 502 and "contract validation" in r.json()["detail"]
    fail(RuntimeError("reconstruction bug"))  # a genuine bug is not disguised as an outage
    with pytest.raises(RuntimeError, match="reconstruction bug"):
        client.post("/api/v1/squad/sync", json=body)


def test_reports_models_settings(client: TestClient) -> None:
    rl = client.get("/api/v1/reports").json()["reports"]
    assert "forecast_eval" in rl
    rep = client.get("/api/v1/reports/forecast_eval").json()
    assert rep["markdown"].startswith("# Forecast evaluation")
    assert client.get("/api/v1/reports/..%2Fsecrets").status_code == 404
    assert (
        client.get("/api/v1/reports/figures/forecast_pit.svg")
        .headers["content-type"]
        .startswith("image/svg+xml")
    )
    models = client.get("/api/v1/models").json()
    assert models["forecast_eval"]["promotion_gates"]["points"]["passed"] is True
    assert (
        client.post(
            "/api/v1/settings",
            params={"manager_key": "demo"},
            json={"horizon": 6, "profile": "conservative"},
        ).status_code
        == 200
    )
    got = client.get("/api/v1/settings", params={"manager_key": "demo"}).json()
    assert got["settings"]["horizon"] == 6 and got["settings"]["profile"] == "conservative"


def test_metrics_and_validation(client: TestClient) -> None:
    assert client.post("/api/v1/squad", json={"manager_key": "x y"}).status_code == 422
    m = client.get("/metrics")
    assert m.status_code == 200 and "fpl_http_requests_total" in m.text


def test_api_key_and_rate_limit(tmp_path: Path) -> None:
    key = "s3cret-key"
    st = _settings(
        None,
        tmp_path,
        require_api_key=True,
        api_keys_sha256=[hash_key(key)],
        rate_limit_per_minute=3,
    )
    app = create_app(st, AppServices.build(st, DS))
    with TestClient(app) as c:
        assert c.post("/api/v1/backtests", json={"season": "2025-26"}).status_code == 401
        # personal-data reads are privileged even though they are GETs (§75)
        assert c.get("/api/v1/managers/demo/export").status_code == 401
        assert (
            c.post(
                "/api/v1/backtests", json={"season": "2025-26"}, headers={"X-API-Key": "wrong"}
            ).status_code
            == 403
        )
        codes = [c.get("/api/v1/gameweeks/current").status_code for _ in range(3)]
        assert 429 in codes  # the 3 requests above already consumed bucket tokens
        assert c.get("/api/v1/gameweeks/current").headers.get("retry-after")
        sec = c.get("/metrics").text
        assert 'fpl_security_events_total{kind="auth_missing"} 2.0' in sec
        assert 'fpl_security_events_total{kind="rate_limited"}' in sec


def test_manager_linked_reads_need_a_key_reference_reads_do_not(tmp_path: Path) -> None:
    key = "s3cret-key"
    st = _settings(None, tmp_path, require_api_key=True, api_keys_sha256=[hash_key(key)])
    app = create_app(st, AppServices.build(st, DS))
    private = [
        "/api/v1/squad?manager_key=demo",
        "/api/v1/settings?manager_key=demo",
        "/api/v1/notifications?manager_key=demo",
        "/api/v1/decisions?manager_key=demo",
        "/api/v1/recommendations/current?manager_key=demo",
        "/api/v1/recommendations/rec_x",
        "/api/v1/recommendations/rec_x/trace",
        "/api/v1/jobs/job_x",
        "/api/v1/managers/demo/export",
    ]
    with TestClient(app) as c:
        for path in private:
            assert c.get(path).status_code == 401, path
            assert c.get(path, headers={"X-API-Key": "wrong"}).status_code == 403, path
            assert c.get(path, headers={"X-API-Key": key}).status_code not in (401, 403), path
        for path in ("/api/v1/health", "/api/v1/gameweeks/current", "/api/v1/reports"):
            assert c.get(path).status_code == 200, path


# ----------------------------------------------------------------------------- M13: §74–§76


@pytest.fixture(scope="module")
def rec(client: TestClient, squad: list[int]) -> dict:  # type: ignore[type-arg]
    body = {
        "manager_key": "demo",
        "horizon": 2,
        "stability": False,
        "scenarios": True,
        "chips": False,
    }
    job = client.post("/api/v1/recommendations/generate", json=body).json()["job_id"]
    assert client.get(f"/api/v1/jobs/{job}").json()["status"] == "succeeded"
    return client.get("/api/v1/recommendations/current", params={"manager_key": "demo"}).json()


def test_traceability_chain_is_complete(client: TestClient, rec: dict) -> None:  # type: ignore[type-arg]
    t = client.get(f"/api/v1/recommendations/{rec['id']}/trace")
    assert t.status_code == 200, t.text
    body = t.json()
    assert body["complete"] is True, [c for c in body["checks"] if not c["ok"]]
    ch = body["chain"]
    assert list(ch) == [
        "recommendation",
        "optimization_run",
        "prediction_run",
        "feature_snapshot",
        "data_snapshot",
        "source_retrieval",
    ]
    assert ch["optimization_run"]["id"] == rec["optimizer_run_id"]
    assert ch["prediction_run"]["id"] == rec["watch"]["prediction_run_id"]
    assert ch["data_snapshot"]["id"] == rec["snapshot_id"]
    fs = ch["feature_snapshot"]
    assert fs["max_source_available_at"] <= fs["cutoff_at"] and fs["n_rows"] > 0
    assert (
        ch["source_retrieval"]["commit"] and ch["source_retrieval"]["pinned_commit_matches_config"]
    )
    assert client.get("/api/v1/recommendations/rec_nope/trace").status_code == 404


def test_notifications_dedupe_and_materiality(client: TestClient, rec: dict) -> None:  # type: ignore[type-arg]
    from datetime import timedelta

    from fpl_api.alerts import evaluate_alerts

    svc = client.app.state.services  # type: ignore[attr-defined]
    ctx = svc.context()
    out = evaluate_alerts(svc, "demo", now=ctx.deadline - timedelta(minutes=70), deliver=False)
    kinds = [a["kind"] for a in out["alerts"]]
    assert kinds.count("deadline") == 1 and len(out["new_ids"]) == len(out["alerts"])
    # the snapshot has not changed since the recommendation → no change alerts are invented
    assert "squad_change" not in kinds and "fixture_change" not in kinds
    assert "no new information" in out["skipped"]["invalidation"]
    again = evaluate_alerts(svc, "demo", now=ctx.deadline - timedelta(minutes=60), deliver=False)
    assert again["new_ids"] == []  # same state → same dedupe keys → nothing new
    listed = client.get("/api/v1/notifications", params={"manager_key": "demo"}).json()
    dl = [n for n in listed["notifications"] if n["kind"] == "deadline"]
    assert len(dl) == 1 and dl[0]["evidence"]["config_ref"].startswith("notifications/default@")
    r = client.post(
        "/api/v1/notifications/read", params={"manager_key": "demo"}, json={"ids": [dl[0]["id"]]}
    )
    assert r.json()["updated"] == 1
    unread = client.get(
        "/api/v1/notifications", params={"manager_key": "demo", "unread_only": True}
    ).json()["notifications"]
    assert dl[0]["id"] not in {n["id"] for n in unread}
    job = client.post("/api/v1/notifications/evaluate", json={"manager_key": "demo"})
    assert job.status_code == 202
    j = client.get(f"/api/v1/jobs/{job.json()['job_id']}").json()
    assert j["status"] == "succeeded" and j["result_ref"].startswith("alerts:")


def test_post_gameweek_review_on_real_results(client: TestClient, squad: list[int]) -> None:
    """A GW1 recommendation (built with GW1 information only) reviewed against GW1 results."""
    from dataclasses import replace

    import pandas as pd

    from fpl_api.alerts import evaluate_alerts
    from fpl_api.container import build_recommendation
    from fpl_storage.pit import decision_cutoff

    svc = client.app.state.services  # type: ignore[attr-defined]
    ctx2 = svc.context()
    g = svc.data.ds["gameweeks"]
    dl1 = pd.Timestamp(g[(g["season"] == ctx2.season) & (g["gw"] == 1)]["deadline_at"].iloc[0])
    buffer = svc.data.ruleset(ctx2.season).timing.decision_buffer_minutes
    ctx1 = replace(
        ctx2,
        gameweek=1,
        deadline=dl1.to_pydatetime(),
        cutoff=decision_cutoff(dl1, buffer).to_pydatetime(),
    )
    _, st = svc.states.latest("demo")
    st1 = st.model_copy(update={"gameweek": 1})
    sid = svc.states.save("review", st1, "manual")
    orig = svc.data.current
    svc.data.current = lambda now=None: ctx1
    try:
        rid, pkg = build_recommendation(
            svc,
            "review",
            st1,
            sid,
            horizon=2,
            run_stability=False,
            run_scenarios=False,
            run_chips=False,
        )
    finally:
        svc.data.current = orig
    assert pkg.gameweek == 1 and rid is not None
    out = evaluate_alerts(svc, "review", deliver=False)
    rep = [a for a in out["alerts"] if a["kind"] == "post_gameweek"]
    assert len(rep) == 1, out["skipped"]
    ev = rep[0]["evidence"]
    pm = svc.data.ds["player_match"]
    pm = pm[(pm["season"] == ctx2.season) & (pm["gw"] == 1)].groupby("player_code")["points"].sum()
    assert len(ev["players"]) == 15
    for p in ev["players"]:
        assert p["actual"] == int(pm.get(p["player_code"], 0))
    assert ev["expected"] == pytest.approx(pkg.chosen.timeline[0].expected_points)
    assert rep[0]["dedupe_key"] == f"postgw:{ctx2.season}:1"


def test_settings_reject_unsafe_webhook_and_bad_timezone(client: TestClient) -> None:
    bad = client.post(
        "/api/v1/settings",
        params={"manager_key": "demo"},
        json={"webhook_url": "https://169.254.169.254/latest/meta-data"},
    )
    assert bad.status_code == 422 and "allow-list" in bad.json()["detail"]
    tz = client.post(
        "/api/v1/settings", params={"manager_key": "demo"}, json={"timezone": "Mars/Olympus"}
    )
    assert tz.status_code == 422


def test_privacy_export_and_delete(client: TestClient, squad: list[int]) -> None:
    from sqlalchemy import select

    from fpl_api.privacy import key_digest
    from fpl_storage import models as m
    from fpl_storage.db import session_scope

    key = "privacy-demo"
    assert (
        client.post(
            "/api/v1/squad",
            json={"manager_key": key, "picks": [{"player_code": c} for c in squad], "bank": 0},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/settings", params={"manager_key": key}, json={"horizon": 3}
        ).status_code
        == 200
    )
    exp = client.get(f"/api/v1/managers/{key}/export").json()
    assert exp["counts"]["manager_state"] == 1 and exp["counts"]["manager_settings"] == 1
    assert exp["manager_state"][0]["state_json"]["manager_key"] == key
    gone = client.delete(f"/api/v1/managers/{key}").json()["deleted"]
    assert gone["manager_state"] == 1 and gone["manager_settings"] == 1
    after = client.get(f"/api/v1/managers/{key}/export").json()
    assert all(v == 0 for v in after["counts"].values())
    assert client.get("/api/v1/squad", params={"manager_key": key}).status_code == 404
    svc = client.app.state.services  # type: ignore[attr-defined]
    with session_scope(svc.engine) as s:
        rows = s.scalars(
            select(m.AuditLog).where(m.AuditLog.entity_id == key_digest(key)[:32])
        ).all()
        assert {r.action for r in rows} >= {"privacy.export_manager", "privacy.delete_manager"}
        assert all(key not in str(r.details_json) for r in rows)  # identifier not retained
    assert client.get("/api/v1/managers/bad%20key/export").status_code == 422


def test_observability_metrics(client: TestClient, rec: dict) -> None:  # type: ignore[type-arg]
    text = client.get("/metrics").text
    for name in (
        "fpl_http_request_seconds_bucket",
        "fpl_forecast_cache_total",
        "fpl_optimization_seconds_bucket",
        "fpl_recommendations_total",
        "fpl_data_freshness_hours",
        'fpl_jobs{status="queued"}',
        "fpl_model_metric",
    ):
        assert name in text, name
    assert 'fpl_recommendations_total{outcome="success"}' in text
