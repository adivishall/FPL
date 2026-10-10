# ruff: noqa: F811  (module-scoped API client and squad fixtures are shared with test_api)
"""Copilot Home (M1.1a): the precomputed squad analysis is served for the manager's current
state and snapshot, every number has a source, it agrees with the lineup endpoint, it follows
the squad when it changes, it belongs to its owner, and erasure removes it."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from fpl_api.copilot import ANALYSIS_VERSION
from fpl_storage import models as m
from fpl_storage.db import session_scope
from tests.integration.test_api import client, squad  # noqa: F401  (module fixtures)
from tests.integration.test_identity import _bearer, _manager, _register

KEY = "home-demo"


def _save(client: TestClient, codes: list[int], bank: int) -> str:
    r = client.post(
        "/api/v1/squad",
        json={
            "manager_key": KEY,
            "picks": [{"player_code": c} for c in codes],
            "bank": bank,
            "free_transfers": 2,
        },
    )
    assert r.status_code == 200, r.text
    return str(r.json()["state_id"])


def test_home_without_a_squad_points_at_onboarding(client: TestClient) -> None:
    r = client.get("/api/v1/copilot/home", params={"manager_key": "nobody-yet"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state_id"] is None and body["analysis"] is None and body["pending"] is False
    assert body["horizon_max"] == 2 and "freshness" in body


def test_home_serves_a_versioned_analysis_of_the_current_squad(
    client: TestClient, squad: list[int]
) -> None:
    sid = _save(client, squad, bank=7)
    r = client.get("/api/v1/copilot/home", params={"manager_key": KEY})
    assert r.status_code == 200, r.text
    body = r.json()
    a = body["analysis"]
    assert body["state_id"] == sid and body["pending"] is False and body["stale"] is False
    assert a["version"] == ANALYSIS_VERSION and a["state_id"] == sid
    assert a["snapshot_id"] == body["snapshot_id"] and a["forecast_key"]
    assert a["horizon"] == 2 and a["gameweek"] == body["gameweek"]
    assert len(a["players"]) == 15 and {p["player_code"] for p in a["players"]} == set(squad)
    assert sum(p["starter"] for p in a["players"]) == 11
    bench = sorted(p["bench_slot"] for p in a["players"] if not p["starter"])
    assert bench == [1, 2, 3, 4]
    for p in a["players"]:
        assert 0.0 <= p["p_start_next"] <= 1.0 and p["p10_next"] <= p["xp_next"] <= p["p90_next"]
        assert p["price"] > 0 and p["position"] in {"GK", "DEF", "MID", "FWD"}
        assert len(p["per_gw"]) <= 2 and isinstance(p["fixtures"], list)
    assert a["state"] == {
        **a["state"],
        "bank": 7,
        "free_transfers": 2,
        "manager_id": None,
        "squad_value": sum(p["price"] for p in a["players"]),
    }
    # every health signal names its inputs and its source; the affordability note is always there
    kinds = {s["kind"] for s in a["health"]}
    assert "affordability" in kinds
    for s in a["health"]:
        assert s["severity"] in {"bad", "warn", "info"} and s["message"] and s["evidence"]
        assert s["source"] in a["sources"]
    # the captain profiles and lineup are exactly what POST /lineup serves
    lu = client.post("/api/v1/lineup", json={"manager_key": KEY}).json()
    assert a["lineup"] == lu["lineup"]
    assert a["captaincy"]["expected"] == lu["captaincy"]["expected"]
    assert a["captaincy"]["safe"] == lu["captaincy"]["safe"]
    assert a["lineup_expected_points"] == lu["expected_points"]
    # served from the store on the second read (no recompute): same computed_at
    again = client.get("/api/v1/copilot/home", params={"manager_key": KEY}).json()
    assert again["analysis"]["computed_at"] == a["computed_at"]
    with session_scope(client.app.state.services.engine) as s:
        n = s.scalar(
            select(func.count())
            .select_from(m.SquadAnalysisRow)
            .where(m.SquadAnalysisRow.manager_key == KEY)
        )
    assert n == 1


def test_home_follows_the_squad_when_it_changes(client: TestClient, squad: list[int]) -> None:
    first = client.get("/api/v1/copilot/home", params={"manager_key": KEY}).json()
    sid_b = _save(client, squad, bank=17)
    assert sid_b != first["state_id"]
    body = client.get("/api/v1/copilot/home", params={"manager_key": KEY}).json()
    assert body["state_id"] == sid_b and body["analysis"]["state_id"] == sid_b
    assert body["analysis"]["state"]["bank"] == 17 and body["stale"] is False


def test_home_is_owned_and_erased_with_the_manager(client: TestClient, squad: list[int]) -> None:
    alice = _register(client, "home-alice@example.test")["session_token"]
    bob = _register(client, "home-bob@example.test")["session_token"]
    a_key = _manager(client, alice, squad)
    mine = client.get("/api/v1/copilot/home", params={"manager_key": a_key}, headers=_bearer(alice))
    assert mine.status_code == 200 and mine.json()["analysis"]["manager_key"] == a_key
    theirs = client.get("/api/v1/copilot/home", params={"manager_key": a_key}, headers=_bearer(bob))
    assert theirs.status_code == 403
    assert client.delete(f"/api/v1/managers/{a_key}", headers=_bearer(alice)).status_code == 200
    with session_scope(client.app.state.services.engine) as s:
        n = s.scalar(
            select(func.count())
            .select_from(m.SquadAnalysisRow)
            .where(m.SquadAnalysisRow.manager_key == a_key)
        )
    assert n == 0
