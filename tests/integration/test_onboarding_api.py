# ruff: noqa: F811  (module-scoped API client and squad fixtures are shared with test_api)
"""FPL-ID onboarding and first-party analytics (M1.1a): the entry preview distinguishes an unknown
ID from an outage, sync maps the same way, and product events are allow-listed, opt-out aware and
pruned on schedule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from fpl_api import analytics
from fpl_ingestion.http import HttpStatusError, SourceUnavailableError
from fpl_ingestion.sources import fpl_api_schemas as fs
from fpl_storage import models as m
from fpl_storage.db import session_scope
from tests.integration.test_api import client, squad  # noqa: F401  (module fixtures)
from tests.integration.test_identity import _bearer, _register


def _stub_client(monkeypatch: pytest.MonkeyPatch, entry: Any) -> None:
    """Replace the FPL client: ``entry`` is an ApiEntry to return or an exception to raise."""
    import fpl_api.app as app_mod

    class Stub:
        def entry(self, entry_id: int) -> tuple[Any, Any]:
            if isinstance(entry, Exception):
                raise entry
            return None, entry

    monkeypatch.setattr(app_mod.FplApiClient, "from_config", classmethod(lambda c, f: Stub()))


def test_entry_preview_distinguishes_unknown_id_from_outage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_entry = fs.ApiEntry(
        id=424242,
        name="Expected Goals FC",
        started_event=1,
        current_event=6,
        summary_overall_points=301,
        summary_overall_rank=123456,
    )
    _stub_client(monkeypatch, api_entry)
    r = client.get("/api/v1/fpl/entry/424242")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["team_name"] == "Expected Goals FC" and body["overall_points"] == 301
    assert "public" in body["source"]
    _stub_client(monkeypatch, HttpStatusError("https://x/entry/7/", 404))
    r = client.get("/api/v1/fpl/entry/7")
    assert r.status_code == 404 and "no FPL manager with ID 7" in r.json()["detail"]
    _stub_client(monkeypatch, SourceUnavailableError("egress denied"))
    assert client.get("/api/v1/fpl/entry/7").status_code == 503
    _stub_client(monkeypatch, HttpStatusError("https://x/entry/7/", 403))
    assert client.get("/api/v1/fpl/entry/7").status_code == 502
    assert client.get("/api/v1/fpl/entry/0").status_code == 422  # not a valid id


def test_sync_reports_an_unknown_id_as_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_client(monkeypatch, HttpStatusError("https://x/entry/9/", 404))
    r = client.post("/api/v1/squad/sync", json={"manager_key": "demo", "manager_id": 9})
    assert r.status_code == 404 and "no FPL manager with ID 9" in r.json()["detail"]


def test_product_events_are_allow_listed_opt_out_aware_and_pruned(client: TestClient) -> None:
    token = _register(client, "events@example.test")["session_token"]
    engine = client.app.state.services.engine
    # anonymous / operator traffic is never measured
    assert client.post("/api/v1/events", json={"event": "return_visit"}).json() == {
        "recorded": False
    }
    ok = client.post(
        "/api/v1/events",
        json={
            "event": "onboarding_completed",
            "props": {"page": "x" * 200, "secret": "no", "latency_ms": 12},
        },
        headers=_bearer(token),
    )
    assert ok.status_code == 200 and ok.json()["recorded"] is True
    unknown = client.post(
        "/api/v1/events", json={"event": "something_else"}, headers=_bearer(token)
    )
    assert unknown.json()["recorded"] is False
    bad_name = client.post("/api/v1/events", json={"event": "Drop Table"}, headers=_bearer(token))
    assert bad_name.status_code == 422
    with session_scope(engine) as s:
        rows = s.scalars(select(m.ProductEventRow)).all()
        assert [r.event for r in rows] == ["onboarding_completed"]
        assert rows[0].props_json == {
            "page": "x" * 64,
            "latency_ms": 12,
        }  # truncated, unknown key dropped
        assert rows[0].user_id is not None
    # opting out stops recording server-side too
    r = client.post(
        "/api/v1/auth/me/preferences", json={"analytics_opt_out": True}, headers=_bearer(token)
    )
    assert r.json()["user"]["analytics_opt_out"] is True
    r = client.post("/api/v1/events", json={"event": "return_visit"}, headers=_bearer(token))
    assert r.json()["recorded"] is False
    # operator summary, then retention
    summary = client.get("/api/v1/events/summary").json()
    assert summary["events"]["onboarding_completed"] == {"count": 1, "users": 1}
    assert client.get("/api/v1/events/summary", headers=_bearer(token)).status_code == 403
    with session_scope(engine) as s:
        s.execute(
            update(m.ProductEventRow).values(created_at=datetime.now(UTC) - timedelta(days=91))
        )
    assert analytics.prune(engine) == 1
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(m.ProductEventRow)) == 0
