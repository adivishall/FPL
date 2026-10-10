# ruff: noqa: F811  (module-scoped API client and squad fixtures are shared with test_api)
"""Transfer Workbench API (M1.2): context, evaluation as a job bound to one squad state, the
same question answered once, stale detection when the squad changes, explicit state selection,
ownership on every route, and export / erasure with the manager."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.integration.test_api import client, squad  # noqa: F401  (module fixtures)
from tests.integration.test_identity import _bearer, _manager, _register

KEY = "wb-demo"


def _save(client: TestClient, codes: list[int], bank: int, ft: int = 1) -> str:
    r = client.post(
        "/api/v1/squad",
        json={
            "manager_key": KEY,
            "picks": [{"player_code": c} for c in codes],
            "bank": bank,
            "free_transfers": ft,
        },
    )
    assert r.status_code == 200, r.text
    return str(r.json()["state_id"])


def _mid(context: dict[str, Any]) -> int:
    return next(p["player_code"] for p in context["players"] if p["position"] == "MID")


def test_context_shows_the_squad_as_the_workbench_needs_it(
    client: TestClient, squad: list[int]
) -> None:
    sid = _save(client, squad, bank=12, ft=2)
    r = client.get("/api/v1/workbench/context", params={"manager_key": KEY})
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["state_id"] == sid and c["horizon_max"] == 2 and c["horizon"] >= 1
    assert c["state"] == {
        **c["state"],
        "bank": 12,
        "free_transfers": 2,
        "transfers_made": 0,
        "selling_value": sum(p["selling_price"] for p in c["players"]),
    }
    assert c["rules"]["hit_cost"] == 4 and c["rules"]["max_per_club"] == 3
    assert len(c["players"]) == 15 and {p["player_code"] for p in c["players"]} == set(squad)
    for p in c["players"]:
        assert p["selling_price"] <= max(p["price"], p["purchase_price"])
        assert p["xp_next"] >= 0 and 0.0 <= p["p_start_next"] <= 1.0
    assert "freshness" in c and c["forecast_key"]


def test_evaluate_runs_once_per_question_and_serves_whole_squad_plans(
    client: TestClient, squad: list[int]
) -> None:
    ctx = client.get("/api/v1/workbench/context", params={"manager_key": KEY}).json()
    out = _mid(ctx)
    body = {
        "manager_key": KEY,
        "horizon": 2,
        "plans": [
            {"kind": "hold"},
            {"kind": "replace", "out_player": out, "candidates": 2},
            {"kind": "best", "label": "best", "alternatives": 1},
        ],
    }
    r = client.post("/api/v1/workbench/evaluate", json=body)
    assert r.status_code == 200, r.text  # inline job backend: computed before answering
    run = r.json()
    assert run["status"] == "succeeded" and run["state_id"] == ctx["state_id"]
    assert run["stale_state"] is False and run["stale_snapshot"] is False
    res = run["result"]["result"]
    assert res["plans"][0]["label"] == "hold" and res["plans"][0]["action"] == "HOLD"
    assert all(p["valid"] for p in res["plans"])
    assert sum(p["kind"] == "replace" for p in res["plans"]) == 2
    assert {"best", "best-alt-1"} <= {p["label"] for p in res["plans"]}
    for p in res["plans"]:
        assert (
            abs(p["gross_gain_horizon"] - (p["gain_horizon"]["mean"] + p["hit_cost_vs_hold"]))
            < 1e-9
        )
        assert len(p["squad_after"]) == 15
    # the run names its inputs and every player it mentions
    assert run["result"]["snapshot_id"] == ctx["snapshot_id"]
    assert run["result"]["forecast_key"] == ctx["forecast_key"]
    assert run["result"]["prediction_run_id"] and run["result"]["forecast"]["n_simulations"] == 200
    for p in res["plans"]:
        for c in [*p["sells"], *p["buys"]]:
            assert str(c) in run["names"]
    # the same question on the same inputs is the same run, not a second computation
    again = client.post("/api/v1/workbench/evaluate", json=body)
    assert again.status_code == 200 and again.json()["id"] == run["id"]
    assert again.json()["job_id"] == run["job_id"]
    assert again.json()["result"]["computed_at"] == run["result"]["computed_at"]
    got = client.get(f"/api/v1/workbench/runs/{run['id']}")
    assert got.status_code == 200 and got.json()["result"]["run_id"] == run["id"]
    listed = client.get("/api/v1/workbench/runs", params={"manager_key": KEY}).json()["runs"]
    assert [x["id"] for x in listed][:1] == [run["id"]] and listed[0]["result"] is None
    job = client.get(f"/api/v1/jobs/{run['job_id']}").json()
    assert job["kind"] == "workbench" and job["status"] == "succeeded"
    assert job["result_ref"] == run["id"]


def test_a_changed_squad_marks_the_run_stale_and_states_can_be_named(
    client: TestClient, squad: list[int]
) -> None:
    old = client.get("/api/v1/workbench/context", params={"manager_key": KEY}).json()["state_id"]
    first = client.get("/api/v1/workbench/runs", params={"manager_key": KEY}).json()["runs"][0]
    new = _save(client, squad, bank=40, ft=1)
    assert new != old
    r = client.get(f"/api/v1/workbench/runs/{first['id']}").json()
    assert r["stale_state"] is True and r["latest_state_id"] == new and r["state_id"] == old
    # an explicit state id evaluates exactly that state, even though a newer one exists
    r = client.post(
        "/api/v1/workbench/evaluate",
        json={"manager_key": KEY, "state_id": old, "horizon": 2, "plans": [{"kind": "hold"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state_id"] == old and r.json()["stale_state"] is True
    assert r.json()["result"]["result"]["plans"][0]["label"] == "hold"
    missing = client.post(
        "/api/v1/workbench/evaluate",
        json={"manager_key": KEY, "state_id": "mstate_nope", "plans": [{"kind": "hold"}]},
    )
    assert missing.status_code == 404
    too_long = client.post(
        "/api/v1/workbench/evaluate",
        json={"manager_key": KEY, "horizon": 3, "plans": [{"kind": "hold"}]},
    )
    assert too_long.status_code == 422  # beyond this deployment's served horizon


def test_illegal_requests_come_back_as_exclusions_not_plans(
    client: TestClient, squad: list[int]
) -> None:
    ctx = client.get("/api/v1/workbench/context", params={"manager_key": KEY}).json()
    out = _mid(ctx)
    owned = next(c for c in squad if c != out)
    r = client.post(
        "/api/v1/workbench/evaluate",
        json={
            "manager_key": KEY,
            "plans": [{"kind": "transfer", "label": "bad", "sells": [out], "buys": [owned]}],
        },
    )
    assert r.status_code == 200, r.text
    res = r.json()["result"]["result"]
    assert [p["label"] for p in res["plans"]] == ["hold"]
    assert (
        res["excluded"][0]["label"] == "bad"
        and "already in the squad" in res["excluded"][0]["reason"]
    )


def test_runs_belong_to_their_owner_and_are_exported_and_erased(
    client: TestClient, squad: list[int]
) -> None:
    alice = _register(client, "wb-alice@example.test")["session_token"]
    bob = _register(client, "wb-bob@example.test")["session_token"]
    a_key = _manager(client, alice, squad)
    body = {"manager_key": a_key, "horizon": 1, "plans": [{"kind": "hold"}]}
    mine = client.post("/api/v1/workbench/evaluate", json=body, headers=_bearer(alice))
    assert mine.status_code == 200, mine.text
    run_id, job_id = mine.json()["id"], mine.json()["job_id"]
    # another signed-in user: the run, its job, the context and a new evaluation are all refused
    assert client.get(f"/api/v1/workbench/runs/{run_id}", headers=_bearer(bob)).status_code == 403
    assert client.get(f"/api/v1/jobs/{job_id}", headers=_bearer(bob)).status_code == 403
    assert (
        client.get(
            "/api/v1/workbench/context", params={"manager_key": a_key}, headers=_bearer(bob)
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/workbench/runs", params={"manager_key": a_key}, headers=_bearer(bob)
        ).status_code
        == 403
    )
    assert (
        client.post("/api/v1/workbench/evaluate", json=body, headers=_bearer(bob)).status_code
        == 403
    )
    assert client.get(f"/api/v1/workbench/runs/{run_id}", headers=_bearer(alice)).status_code == 200
    export = client.get(f"/api/v1/managers/{a_key}/export", headers=_bearer(alice)).json()
    assert [x["id"] for x in export["workbench_runs"]] == [run_id]
    assert client.delete(f"/api/v1/managers/{a_key}", headers=_bearer(alice)).status_code == 200
    assert client.get(f"/api/v1/workbench/runs/{run_id}").status_code == 404
