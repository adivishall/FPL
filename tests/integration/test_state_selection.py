# ruff: noqa: F811  (module-scoped API client and squad fixtures are shared with test_api)
"""A recommendation evaluates exactly the squad state it was asked to evaluate (BUILD_STATUS
defect 41): never the latest state by accident, never another manager's, never a vanished one;
and a response says when the squad has changed since the plan was computed."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from fpl_api.jobs import _job_recommendation
from tests.integration.test_api import client, squad  # noqa: F401  (module fixtures)

KEY = "state-sel"
GEN = {"horizon": 2, "stability": False, "scenarios": False, "chips": False}


def _save(client: TestClient, codes: list[int], bank: int) -> str:
    r = client.post(
        "/api/v1/squad",
        json={
            "manager_key": KEY,
            "picks": [{"player_code": c} for c in codes],
            "bank": bank,
            "free_transfers": 1,
        },
    )
    assert r.status_code == 200, r.text
    latest = client.app.state.services.states.latest(KEY)
    assert latest is not None
    return str(latest[0])


def _generate(client: TestClient, **extra: Any) -> dict[str, Any]:
    r = client.post("/api/v1/recommendations/generate", json={"manager_key": KEY, **GEN, **extra})
    assert r.status_code == 202, r.text
    job = client.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "succeeded", job
    return {"job_id": r.json()["job_id"], "rec_id": job["result_ref"]}


def _current(client: TestClient) -> dict[str, Any]:
    r = client.get("/api/v1/recommendations/current", params={"manager_key": KEY})
    assert r.status_code == 200, r.text
    return r.json()


def test_recommendation_is_traced_to_the_state_it_evaluated(
    client: TestClient, squad: list[int]
) -> None:
    sid_a = _save(client, squad, bank=5)
    first = _generate(client)
    rec = _current(client)
    assert rec["id"] == first["rec_id"]
    assert rec["state_id"] == sid_a
    assert rec["stale_state"] is False and rec["latest_state_id"] == sid_a

    # The squad changes (same players, different bank): the plan is for a squad that no longer
    # exists and says so; the explicit old state is still evaluable and is not re-pointed.
    sid_b = _save(client, squad, bank=15)
    assert sid_b != sid_a
    rec = _current(client)
    assert rec["state_id"] == sid_a and rec["stale_state"] is True
    assert rec["latest_state_id"] == sid_b
    explicit = _generate(client, state_id=sid_a)
    assert explicit["job_id"] == first["job_id"]  # same request → same job, still state A
    detail = client.get(f"/api/v1/recommendations/{explicit['rec_id']}").json()
    assert detail["state_id"] == sid_a and detail["stale_state"] is True

    # A state that does not exist (or is not this manager's) is refused up front.
    r = client.post(
        "/api/v1/recommendations/generate",
        json={"manager_key": KEY, **GEN, "state_id": "mstate_0000000000"},
    )
    assert r.status_code == 404, r.text

    # Without an explicit state the latest one is used: a new job, a new record, the old
    # record superseded.
    second = _generate(client)
    assert second["job_id"] != first["job_id"]
    rec = _current(client)
    assert rec["id"] == second["rec_id"] and rec["state_id"] == sid_b
    assert rec["stale_state"] is False
    old = client.get(f"/api/v1/recommendations/{first['rec_id']}").json()
    assert old["status"] == "superseded"


def test_queued_job_keeps_its_state_when_the_squad_changes(
    client: TestClient, squad: list[int]
) -> None:
    svc = client.app.state.services
    sid_b = _save(client, squad, bank=15)
    sid_c = _save(client, squad, bank=25)  # becomes the latest before the job runs
    assert svc.states.latest(KEY)[0] == sid_c
    rec_id = _job_recommendation(svc, {"manager_key": KEY, "state_id": sid_b, **GEN})
    rec = client.get(f"/api/v1/recommendations/{rec_id}").json()
    assert rec["state_id"] == sid_b, "the job must evaluate the state it was given"
    assert rec["stale_state"] is True and rec["latest_state_id"] == sid_c


def test_resaving_an_earlier_squad_makes_it_the_latest_again(
    client: TestClient, squad: list[int]
) -> None:
    sid_a = _save(client, squad, bank=5)
    sid_b = _save(client, squad, bank=15)
    assert sid_a != sid_b
    assert _save(client, squad, bank=5) == sid_a  # content-addressed id, now latest again
    assert client.app.state.services.states.latest(KEY)[0] == sid_a


def test_job_fails_clearly_when_its_state_is_unavailable(
    client: TestClient, squad: list[int]
) -> None:
    svc = client.app.state.services
    sid = _save(client, squad, bank=5)
    with pytest.raises(LookupError, match="names no squad state"):
        _job_recommendation(svc, {"manager_key": KEY, **GEN})
    with pytest.raises(LookupError, match="not available"):
        _job_recommendation(svc, {"manager_key": "someone-else", "state_id": sid, **GEN})
    assert client.delete(f"/api/v1/managers/{KEY}").status_code == 200
    with pytest.raises(LookupError, match="not available"):
        _job_recommendation(svc, {"manager_key": KEY, "state_id": sid, **GEN})


def test_horizons_beyond_the_deployment_are_refused_not_clamped(
    client: TestClient, squad: list[int]
) -> None:
    _save(client, squad, bank=5)
    r = client.post(
        "/api/v1/recommendations/generate", json={"manager_key": KEY, **GEN, "horizon": 3}
    )
    assert r.status_code == 422 and r.json()["code"] == "unsupported_horizon", r.text
    assert client.get("/api/v1/gameweeks/current").json()["horizon_max"] == 2
