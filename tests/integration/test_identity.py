# ruff: noqa: F811  (module-scoped API client and squad fixtures are shared with test_api)
"""Invite-only accounts, sessions and manager ownership (M1.1a): registration and login rules,
and — above all — that one user can never read, change, plan for, export or erase another's
manager data, whatever route they try, while operators and the owner still can."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from fpl_api.accounts import hash_password, verify_password
from fpl_storage import models as m
from fpl_storage.db import session_scope
from tests.integration.test_api import client, squad  # noqa: F401  (module fixtures)

PW = "correct horse battery"


def _invite(client: TestClient, **kw: Any) -> str:
    r = client.post("/api/v1/auth/invites", json={"label": "test", **kw})
    assert r.status_code == 200, r.text
    return str(r.json()["invite_code"])


def _register(client: TestClient, email: str, code: str | None = None) -> dict[str, Any]:
    r = client.post(
        "/api/v1/auth/register",
        json={"invite_code": code or _invite(client), "email": email, "password": PW},
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _manager(client: TestClient, token: str, squad: list[int], bank: int = 5) -> str:
    key = client.post("/api/v1/auth/managers", json={}, headers=_bearer(token)).json()[
        "manager_key"
    ]
    r = client.post(
        "/api/v1/squad",
        json={
            "manager_key": key,
            "picks": [{"player_code": c} for c in squad],
            "bank": bank,
            "free_transfers": 1,
        },
        headers=_bearer(token),
    )
    assert r.status_code == 200, r.text
    return str(key)


def test_password_hashes_are_salted_scrypt_and_verify() -> None:
    a, b = hash_password(PW), hash_password(PW)
    assert a != b and a.startswith("scrypt$") and PW not in a
    assert verify_password(PW, a) and verify_password(PW, b)
    assert not verify_password("wrong password!", a)
    assert not verify_password(PW, "not-a-hash")


def test_registration_needs_a_valid_unused_invite(client: TestClient) -> None:
    r = client.post(
        "/api/v1/auth/register",
        json={"invite_code": "inv_nope_nope", "email": "a@example.test", "password": PW},
    )
    assert r.status_code == 403 and r.json()["code"] == "invite_invalid"
    code = _invite(client)
    weak = client.post(
        "/api/v1/auth/register",
        json={"invite_code": code, "email": "a@example.test", "password": "short"},
    )
    assert weak.status_code == 422
    first = _register(client, "First@Example.test", code)
    assert first["user"]["email"] == "first@example.test" and first["session_token"]
    again = client.post(
        "/api/v1/auth/register",
        json={"invite_code": code, "email": "b@example.test", "password": PW},
    )
    assert again.status_code == 410 and again.json()["code"] == "invite_used"
    dup = client.post(
        "/api/v1/auth/register",
        json={"invite_code": _invite(client), "email": "first@example.test", "password": PW},
    )
    assert dup.status_code == 409
    expired = _invite(client, days=1)
    with session_scope(client.app.state.services.engine) as s:
        s.execute(update(m.InviteRow).values(expires_at=datetime.now(UTC) - timedelta(days=2)))
    r = client.post(
        "/api/v1/auth/register",
        json={"invite_code": expired, "email": "c@example.test", "password": PW},
    )
    assert r.status_code == 410 and r.json()["code"] == "invite_expired"


def test_login_logout_and_session_expiry(client: TestClient) -> None:
    _register(client, "login@example.test")
    bad = client.post(
        "/api/v1/auth/login", json={"email": "login@example.test", "password": "nope"}
    )
    assert bad.status_code == 401 and bad.json()["code"] == "login_failed"
    unknown = client.post(
        "/api/v1/auth/login", json={"email": "ghost@example.test", "password": PW}
    )
    assert unknown.status_code == 401
    ok = client.post("/api/v1/auth/login", json={"email": "LOGIN@example.test", "password": PW})
    assert ok.status_code == 200
    token = ok.json()["session_token"]
    me = client.get("/api/v1/auth/me", headers=_bearer(token))
    assert me.status_code == 200 and me.json()["user"]["email"] == "login@example.test"
    assert client.get("/api/v1/auth/me", headers=_bearer("not-a-token")).status_code == 401
    assert client.post("/api/v1/auth/logout", json={}, headers=_bearer(token)).status_code == 200
    assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 401
    # expiry: a session past its lifetime is refused even though it was never revoked
    token2 = client.post(
        "/api/v1/auth/login", json={"email": "login@example.test", "password": PW}
    ).json()["session_token"]
    assert client.get("/api/v1/auth/me", headers=_bearer(token2)).status_code == 200
    users = client.app.state.users
    users.revoke_all(me.json()["user"]["id"])  # also clears the session cache
    with session_scope(client.app.state.services.engine) as s:
        s.execute(
            update(m.SessionRow).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1), revoked_at=None
            )
        )
    assert client.get("/api/v1/auth/me", headers=_bearer(token2)).status_code == 401


def test_users_cannot_touch_each_others_managers(client: TestClient, squad: list[int]) -> None:
    alice = _register(client, "alice@example.test")["session_token"]
    bob = _register(client, "bob@example.test")["session_token"]
    a_key = _manager(client, alice, squad)
    b_key = _manager(client, bob, squad, bank=15)
    assert a_key != b_key
    # a user only sees their own managers
    assert [
        x["manager_key"]
        for x in client.get("/api/v1/auth/managers", headers=_bearer(alice)).json()["managers"]
    ] == [a_key]
    # alice generates a recommendation; bob must not see, trace, rate or erase it
    gen = {
        "manager_key": a_key,
        "horizon": 2,
        "stability": False,
        "scenarios": False,
        "chips": False,
    }
    r = client.post("/api/v1/recommendations/generate", json=gen, headers=_bearer(alice))
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert (
        client.get(f"/api/v1/jobs/{job_id}", headers=_bearer(alice)).json()["status"] == "succeeded"
    )
    rec = client.get(
        f"/api/v1/recommendations/current?manager_key={a_key}", headers=_bearer(alice)
    ).json()
    rec_id = rec["id"]

    def bob_tries(method: str, path: str, **kw: Any) -> int:
        return int(client.request(method, path, headers=_bearer(bob), **kw).status_code)

    forbidden = {
        "GET squad": bob_tries("GET", f"/api/v1/squad?manager_key={a_key}"),
        "POST squad": bob_tries(
            "POST",
            "/api/v1/squad",
            json={
                "manager_key": a_key,
                "picks": [{"player_code": c} for c in squad],
                "bank": 0,
                "free_transfers": 1,
            },
        ),
        "GET settings": bob_tries("GET", f"/api/v1/settings?manager_key={a_key}"),
        "POST settings": bob_tries(
            "POST", f"/api/v1/settings?manager_key={a_key}", json={"horizon": 2}
        ),
        "generate": bob_tries("POST", "/api/v1/recommendations/generate", json=gen),
        "current": bob_tries("GET", f"/api/v1/recommendations/current?manager_key={a_key}"),
        "by id": bob_tries("GET", f"/api/v1/recommendations/{rec_id}"),
        "trace": bob_tries("GET", f"/api/v1/recommendations/{rec_id}/trace"),
        "feedback": bob_tries(
            "POST", f"/api/v1/decisions/{rec_id}/feedback", json={"followed": "followed"}
        ),
        "journal": bob_tries("GET", f"/api/v1/decisions?manager_key={a_key}"),
        "job": bob_tries("GET", f"/api/v1/jobs/{job_id}"),
        "notifications": bob_tries("GET", f"/api/v1/notifications?manager_key={a_key}"),
        "lineup": bob_tries("POST", "/api/v1/lineup", json={"manager_key": a_key}),
        "export": bob_tries("GET", f"/api/v1/managers/{a_key}/export"),
        "erase": bob_tries("DELETE", f"/api/v1/managers/{a_key}"),
        "unowned key": bob_tries("GET", "/api/v1/squad?manager_key=m_000000000000dead"),
    }
    assert all(v == 403 for v in forbidden.values()), forbidden
    # alice's data is intact and still hers; the owner and the operator both still have access
    assert (
        client.get(f"/api/v1/squad?manager_key={a_key}", headers=_bearer(alice)).status_code == 200
    )
    assert (
        client.get(f"/api/v1/recommendations/{rec_id}", headers=_bearer(alice)).status_code == 200
    )
    assert (
        client.get(f"/api/v1/squad?manager_key={a_key}").status_code == 200
    )  # operator (no key required here)
    # bob's own data works for bob
    assert client.get(f"/api/v1/squad?manager_key={b_key}", headers=_bearer(bob)).status_code == 200
    # export is scoped: alice's export never contains bob's state and vice versa
    a_export = client.get(f"/api/v1/managers/{a_key}/export", headers=_bearer(alice)).json()
    assert b_key not in str(a_export)
    # erasing alice's manager releases the key: it is no longer hers to use
    assert client.delete(f"/api/v1/managers/{a_key}", headers=_bearer(alice)).status_code == 200
    assert (
        client.get(f"/api/v1/squad?manager_key={a_key}", headers=_bearer(alice)).status_code == 403
    )


def test_account_deletion_erases_every_owned_manager(client: TestClient, squad: list[int]) -> None:
    carol = _register(client, "carol@example.test")["session_token"]
    k1, k2 = _manager(client, carol, squad), _manager(client, carol, squad, bank=20)
    r = client.delete("/api/v1/auth/me", headers=_bearer(carol))
    assert r.status_code == 200, r.text
    assert r.json()["deleted"]["account_managers"] == 2
    assert client.get("/api/v1/auth/me", headers=_bearer(carol)).status_code == 401
    login = client.post("/api/v1/auth/login", json={"email": "carol@example.test", "password": PW})
    assert login.status_code == 401
    for k in (k1, k2):  # operator view: the manager data is gone
        assert client.get(f"/api/v1/squad?manager_key={k}").status_code == 404


@pytest.mark.parametrize("path", ["/api/v1/auth/invites"])
def test_operator_only_routes_refuse_users(client: TestClient, path: str) -> None:
    dave = _register(client, "dave@example.test")["session_token"]
    r = client.post(path, json={"label": "x"}, headers=_bearer(dave))
    assert r.status_code == 403 and r.json()["code"] == "operator_required"
