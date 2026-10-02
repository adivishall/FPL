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

    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise ConnectionError("egress denied")

    monkeypatch.setattr(app_mod.FplApiClient, "from_config", classmethod(lambda cls, cfg: boom()))
    r = client.post("/api/v1/squad/sync", json={"manager_key": "demo", "manager_id": 123})
    assert r.status_code == 503 and r.json()["degraded"] is True


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
        assert (
            c.post(
                "/api/v1/backtests", json={"season": "2025-26"}, headers={"X-API-Key": "wrong"}
            ).status_code
            == 403
        )
        codes = [c.get("/api/v1/gameweeks/current").status_code for _ in range(3)]
        assert 429 in codes  # 2 POSTs above already consumed bucket tokens
        assert c.get("/api/v1/gameweeks/current").headers.get("retry-after")
