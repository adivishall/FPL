"""Decision journal with prospective outcome evaluation (M1.4), end to end on a real database:
decisions are recorded against a stored recommendation or a Transfer Workbench plan with
immutable decision-time evidence, survive the source becoming historical, are evaluated only
when the official results are final (pending -> partial -> final), idempotently, with a revision
when official data changes; first-party picks are compared, never used to rewrite the record;
and every route stays with the manager who owns the decision (path, query, body).

The module promotes snapshots with synthetic GW2 results exactly as production does (export,
ready marker, swap): partial results first, then all of them, then a corrected score.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from fpl_api import journal as journal_mod
from fpl_api.app import create_app
from fpl_api.container import AppServices
from fpl_api.journal import DecisionEvidence, JournalStore, run_evaluations
from fpl_api.services import mark_serving_ready
from fpl_api.settings import Settings
from fpl_storage import models as m
from fpl_storage.dataset import CanonicalDataset, export_snapshot
from fpl_storage.db import session_scope
from tests.fixtures_util import fixture_dataset, with_gameweek_results
from tests.integration.test_identity import _bearer, _manager, _register

KEY, OTHER = "jr-demo", "jr-other"
SEASON, GW = "2026-27", 2
GEN = {"horizon": 2, "stability": False, "scenarios": False, "chips": False}
DS_A = fixture_dataset("2024-25", "2025-26", "2026-27")
BEFORE = datetime(2026, 8, 27, 12, tzinfo=UTC)  # the GW2 deadline is 2026-08-28T17:30Z
AFTER = datetime(2026, 9, 10, 12, tzinfo=UTC)


def pts(code: int) -> int:
    return code % 7 + 1


@pytest.fixture(scope="module")
def world(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    from tests.conftest import run_alembic

    name = f"jr_{uuid.uuid4().hex[:10]}"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    assert run_alembic(url, "upgrade", "head").returncode == 0
    tmp = tmp_path_factory.mktemp("journal")
    root = tmp / "snapshots"
    a = export_snapshot(DS_A, root)
    st = Settings(
        database_url=url,
        snapshots_root=root,
        serve_ready_snapshots_only=True,
        snapshot_check_seconds=0,
        artifact_dir=tmp / "artifacts",
        feature_store_dir=tmp / "features",
        n_sims=200,
        horizon_default=2,
        horizon_max=2,
        forecast_horizon=2,
        sync_horizon_limit=2,
    )
    mark_serving_ready(st, a.name, "fc_a")
    svc = AppServices.build(st)
    clock = {"now": BEFORE}
    mp = pytest.MonkeyPatch()
    mp.setattr(journal_mod, "clock", lambda: clock["now"])
    with TestClient(create_app(st, svc)) as client:
        yield {"client": client, "svc": svc, "settings": st, "root": root, "clock": clock}
    mp.undo()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture(scope="module")
def squad(world: dict[str, Any]) -> list[int]:
    r = world["client"].post("/api/v1/optimize/squad", json={"budget": 1000, "horizon": 2})
    assert r.status_code == 200, r.text
    codes = [int(p["player_code"]) for p in r.json()["squad"]]
    for key in (KEY, OTHER):
        s = world["client"].post(
            "/api/v1/squad",
            json={
                "manager_key": key,
                "picks": [{"player_code": c} for c in codes],
                "bank": 5,
                "free_transfers": 1,
            },
        )
        assert s.status_code == 200, s.text
    return codes


def _generate(client: TestClient, key: str = KEY, **headers: Any) -> dict[str, Any]:
    r = client.post(
        "/api/v1/recommendations/generate",
        json={"manager_key": key, **GEN},
        headers=headers.get("headers"),
    )
    assert r.status_code == 202, r.text
    job = client.get(f"/api/v1/jobs/{r.json()['job_id']}", headers=headers.get("headers"))
    assert job.json()["status"] == "succeeded", job.json()
    rec_id = job.json()["result_ref"]
    rec = client.get(f"/api/v1/recommendations/{rec_id}", headers=headers.get("headers")).json()
    assert rec["id"] == rec_id
    return dict(rec)


def _record(client: TestClient, status: int = 201, **body: Any) -> dict[str, Any]:
    r = client.post("/api/v1/journal", json={"manager_key": KEY, **body})
    assert r.status_code == status, r.text
    return dict(r.json())


def _promote(world: dict[str, Any], ds: CanonicalDataset) -> str:
    time.sleep(1.1)  # manifests order by creation time
    snap = export_snapshot(ds, world["root"]).name
    mark_serving_ready(world["settings"], snap, f"fc_{snap}")
    world["svc"].data.refresh()
    assert world["svc"].data.snapshot_id == snap
    return snap


def _evaluations(svc: AppServices, entry_id: str) -> list[m.DecisionEvaluationRow]:
    with session_scope(svc.engine) as s:
        rows = s.scalars(
            select(m.DecisionEvaluationRow)
            .where(m.DecisionEvaluationRow.entry_id == entry_id)
            .order_by(m.DecisionEvaluationRow.id)
        ).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def _xi(lineup: dict[str, Any], captain: int) -> int:
    """Points of a stored XI when everybody plays (no substitutions): the armband doubles."""
    return sum(pts(c) for c in lineup["starters"]) + pts(captain)


def test_a_decision_recorded_from_a_recommendation_keeps_its_decision_time_evidence(
    world: dict[str, Any], squad: list[int]
) -> None:
    client = world["client"]
    rec = _generate(client)
    out = _record(
        client,
        source="recommendation",
        source_id=rec["id"],
        choice="recommended",
        rationale="I trust the plan",
    )
    e = out["entry"]
    step = rec["chosen"]["timeline"][0]
    assert out["created"] is True and out["warning"] is None  # recorded against a current plan
    assert e["adherence"] == "followed" and e["action"] == step["action"] == e["recommended_action"]
    assert e["prospective"] is True and e["state"] == "active"
    assert (e["captain"], e["vice_captain"]) == (
        rec["lineup"]["captain"],
        rec["lineup"]["vice_captain"],
    )
    assert e["rationale"] == "I trust the plan"
    assert e["source"] == {"kind": "recommendation", "id": rec["id"], "plan": e["source"]["plan"]}
    full = client.get(f"/api/v1/journal/{e['entry_id']}").json()
    ev = full["evidence"]
    assert ev["recommended"]["starters"] == rec["lineup"]["starters"]
    assert ev["recommended"]["expected"]["mean"] == pytest.approx(step["expected_points"])
    assert ev["recommended"]["gain_vs_hold"] == rec["chosen"]["gain_1gw"]
    assert ev["hold"]["lineup_source"] in ("stored", "reconstructed") and ev["hold"]["starters"]
    assert ev["data_snapshot_id"] == rec["data_snapshot_id"]
    assert ev["prediction_run_id"] == rec["watch"]["prediction_run_id"]
    assert ev["deadline_at"].startswith("2026-08-28T17:30")
    assert ev["chosen"] == ev["recommended"] and ev["source_state"] == "current"
    assert full["source_now"]["state"] == "current" and full["links"]["trace"].endswith("/trace")
    # the same submission again is the same entry, not a second one
    again = _record(
        client,
        status=200,
        source="recommendation",
        source_id=rec["id"],
        choice="recommended",
        rationale="I trust the plan",
    )
    assert again["created"] is False and again["entry"]["entry_id"] == e["entry_id"]
    listed = client.get("/api/v1/journal", params={"manager_key": KEY}).json()["entries"]
    assert [x["entry_id"] for x in listed] == [e["entry_id"]]
    world["rec"] = rec


def test_a_change_of_mind_before_the_deadline_supersedes_and_after_it_is_refused(
    world: dict[str, Any], squad: list[int]
) -> None:
    client, rec = world["client"], world["rec"]
    first = client.get("/api/v1/journal", params={"manager_key": KEY}).json()["entries"][0]
    starters = rec["lineup"]["starters"]
    cap = next(
        c for c in starters if c not in (rec["lineup"]["captain"], rec["lineup"]["vice_captain"])
    )
    changed = _record(
        client,
        source="recommendation",
        source_id=rec["id"],
        choice="recommended",
        captain=cap,
        vice_captain=rec["lineup"]["captain"],
    )["entry"]
    assert changed["adherence"] == "modified" and changed["captain"] == cap
    rows = client.get(
        "/api/v1/journal", params={"manager_key": KEY, "include_superseded": True}
    ).json()["entries"]
    by_id = {x["entry_id"]: x for x in rows}
    assert by_id[first["entry_id"]]["state"] == "superseded"
    assert by_id[first["entry_id"]]["superseded_by"] == changed["entry_id"]
    # a bench player cannot captain; a run's plan cannot be chosen from a recommendation
    bench = rec["lineup"]["bench"][1]
    _record(
        client,
        422,
        source="recommendation",
        source_id=rec["id"],
        choice="recommended",
        captain=bench,
    )
    _record(client, 422, source="recommendation", source_id=rec["id"], choice="plan", plan="1:best")
    world["clock"]["now"] = AFTER
    try:  # after the deadline the decision recorded before it stands
        r = client.post(
            "/api/v1/journal",
            json={
                "manager_key": KEY,
                "source": "recommendation",
                "source_id": rec["id"],
                "choice": "hold",
            },
        )
        assert r.status_code == 409 and "deadline" in r.text
    finally:
        world["clock"]["now"] = BEFORE


def test_a_workbench_plan_can_be_recorded_with_its_stored_lineups(
    world: dict[str, Any], squad: list[int]
) -> None:
    client = world["client"]
    run = client.post(
        "/api/v1/workbench/evaluate",
        json={
            "manager_key": KEY,
            "horizon": 2,
            "plans": [{"kind": "hold"}, {"kind": "best", "label": "best", "alternatives": 1}],
        },
    ).json()
    assert run["status"] == "succeeded", run
    plans = run["result"]["result"]["plans"]
    key = f"1:{plans[1]['label']}"
    e = _record(client, source="workbench", source_id=run["id"], choice="plan", plan=key)["entry"]
    full = client.get(f"/api/v1/journal/{e['entry_id']}").json()
    ev = full["evidence"]
    assert (
        ev["hold"]["lineup_source"] == "stored"
        and ev["hold"]["starters"] == plans[0]["lineup"]["starters"]
    )
    assert ev["chosen"]["starters"] == plans[1]["lineup"]["starters"]
    assert ev["chosen"]["expected"]["mean"] == pytest.approx(plans[1]["expected_points_1gw"])
    assert ev["forecast_key"] == run["forecast_key"] and ev["source_kind"] == "workbench"
    assert full["links"]["workbench_run"] == f"/workbench/runs/{run['id']}"
    _record(client, 422, source="workbench", source_id=run["id"], choice="plan", plan="9:nope")
    world["entry"], world["run"] = e, run


def test_deferred_and_unrecorded_actions_are_never_evaluated_as_actions(
    world: dict[str, Any], squad: list[int]
) -> None:
    client, svc = world["client"], world["svc"]
    world["clock"]["now"] = BEFORE
    rec = _generate(client, OTHER)
    body = {"manager_key": OTHER, "source": "recommendation", "source_id": rec["id"]}
    d = client.post("/api/v1/journal", json={**body, "choice": "defer"}).json()["entry"]
    assert d["action"] == "DEFER" and d["adherence"] == "deferred"
    run_evaluations(svc, OTHER, d["entry_id"], BEFORE)
    assert client.get(f"/api/v1/journal/{d['entry_id']}").json()["evaluation_status"] == "pending"
    conf = client.post(f"/api/v1/journal/{d['entry_id']}/confirm")
    assert conf.status_code == 409  # nothing recorded to confirm
    o = client.post("/api/v1/journal", json={**body, "choice": "other"}).json()["entry"]
    assert o["action"] == "UNKNOWN" and o["adherence"] == "rejected"
    run_evaluations(svc, OTHER, o["entry_id"], AFTER)
    got = client.get(f"/api/v1/journal/{o['entry_id']}").json()
    # no result yet: the reference totals (holding, the recommendation) are still to come, so
    # the entry stays in the hourly sweep instead of settling as "unavailable" now
    assert (
        got["evaluation_status"] == "pending"
        and "not recorded" in got["evaluations"][0]["detail"]["reason"]
    )
    assert got["evaluations"][0]["realized_points"] is None  # unknown stays unknown
    assert OTHER in JournalStore(svc.engine).due_managers(AFTER)
    world["clock"]["now"] = AFTER
    world["unknown"] = o


def test_first_party_picks_are_compared_and_reported_beside_the_record(
    world: dict[str, Any], squad: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A squad imported with an FPL ID: after the deadline the official picks are read and
    compared with the recorded action — matching here, then differing on the captain."""
    from fpl_ingestion.sources import fpl_api_schemas as s
    from fpl_ingestion.sources.fpl_api import FplApiClient

    client, svc = world["client"], world["svc"]
    key = "jr-fpl"
    state = svc.states.get(svc.states.latest(OTHER)[0], OTHER)
    assert state is not None
    world["clock"]["now"] = BEFORE
    sid = svc.states.save(key, state.model_copy(update={"manager_id": 4242}), "fpl_sync")
    assert svc.states.get(sid, key).manager_id == 4242
    rec = _generate(client, key)
    e = client.post(
        "/api/v1/journal",
        json={
            "manager_key": key,
            "source": "recommendation",
            "source_id": rec["id"],
            "choice": "recommended",
        },
    ).json()["entry"]
    ev = client.get(f"/api/v1/journal/{e['entry_id']}").json()["evidence"]
    players = svc.data.ds["players"]
    element = {
        int(r.player_code): int(r.source_id)
        for r in players[players["season"] == SEASON].itertuples(index=False)
    }
    order = [*ev["chosen"]["starters"], *ev["chosen"]["bench"]]
    captain = {"c": ev["chosen"]["captain"]}

    def picks(self: Any, entry_id: int, gameweek: int) -> tuple[Any, s.EntryPicks]:
        assert (entry_id, gameweek) == (4242, GW)
        return None, s.EntryPicks(
            active_chip=None,
            picks=[
                s.ApiPick(
                    element=element[c],
                    position=i + 1,
                    multiplier=2 if c == captain["c"] else (1 if i < 11 else 0),
                    is_captain=c == captain["c"],
                    is_vice_captain=c == ev["chosen"]["vice_captain"],
                )
                for i, c in enumerate(order)
            ],
            entry_history=s.ApiHistoryEvent(
                event=GW,
                points=0,
                total_points=0,
                bank=0,
                value=1000,
                event_transfers=0,
                event_transfers_cost=0,
                points_on_bench=0,
            ),
        )

    monkeypatch.setattr(FplApiClient, "entry_picks", picks)
    run_evaluations(svc, key, e["entry_id"], AFTER)
    got = client.get(f"/api/v1/journal/{e['entry_id']}").json()
    assert got["verification"] == "matches", got["verification_detail"]
    assert got["adherence"] == "followed"  # the record is not rewritten by the check
    with session_scope(svc.engine) as ses:  # check again with another captain in FPL
        ses.execute(
            m.DecisionJournalRow.__table__.update()
            .where(m.DecisionJournalRow.entry_id == e["entry_id"])
            .values(verification=None)
        )
    captain["c"] = next(
        c
        for c in ev["chosen"]["starters"]
        if c not in (ev["chosen"]["captain"], ev["chosen"]["vice_captain"])
    )
    run_evaluations(svc, key, e["entry_id"], AFTER)
    got = client.get(f"/api/v1/journal/{e['entry_id']}").json()
    assert got["verification"] == "differs"
    assert got["verification_detail"]["differences"] == ["captain"]
    assert got["captain"] == ev["chosen"]["captain"]  # what was recorded stays recorded


def test_pre_journal_feedback_keeps_its_deadline_and_self_reported_points(
    world: dict[str, Any], squad: list[int]
) -> None:
    """A decision migrated from pre-M1.4 feedback (no evidence, prospectiveness unknown): its
    entry page has no evidence instead of failing, it never enters the hourly sweep, a
    decision recorded before the deadline cannot be replaced after it (review findings), and
    the compatibility route keeps the points a manager types in on a repeat submission."""
    client, svc = world["client"], world["svc"]
    key = "jr-legacy"
    state = svc.states.get(svc.states.latest(OTHER)[0], OTHER)
    assert state is not None
    world["clock"]["now"] = BEFORE
    svc.states.save(key, state, "manual")
    rec = _generate(client, key)
    with session_scope(svc.engine) as s:
        s.add(
            m.DecisionJournalRow(
                entry_id="jd_legacy_test",
                manager_key=key,
                recommendation_id=rec["id"],
                season=SEASON,
                gameweek=int(rec["gameweek"]),
                state_id=rec["state_id"],
                recommended_action=rec["decision"]["action"],
                action=rec["decision"]["action"],
                followed="followed",
                state="active",
                recorded_at=BEFORE - timedelta(hours=1),
                evidence_json={},
                evaluation_status="unavailable",
                verification="unavailable",
                verification_json={"reason": "recorded before the journal", "retryable": False},
                review_json={},
            )
        )
    legacy = client.get("/api/v1/journal/jd_legacy_test")
    assert legacy.status_code == 200 and legacy.json()["evidence"] is None
    assert key not in JournalStore(svc.engine).due_managers(AFTER)
    world["clock"]["now"] = AFTER
    late = client.post(
        "/api/v1/journal",
        json={
            "manager_key": key,
            "source": "recommendation",
            "source_id": rec["id"],
            "choice": "hold",
        },
    )
    assert late.status_code == 409 and "deadline" in late.text
    world["clock"]["now"] = BEFORE
    fb = {"followed": "followed", "note": "did it"}
    first = client.post(f"/api/v1/decisions/{rec['id']}/feedback", json=fb)
    assert first.status_code == 200, first.text
    again = client.post(
        f"/api/v1/decisions/{rec['id']}/feedback", json={**fb, "realized_points": 64}
    ).json()
    assert again["entry_id"] == first.json()["entry_id"]
    entry = client.get(f"/api/v1/journal/{again['entry_id']}").json()
    assert entry["self_reported_points"] == 64 and entry["evidence"] is not None
    rows = client.get("/api/v1/decisions", params={"manager_key": key}).json()["journal"]
    feedback = next(r for r in rows if r["id"] == rec["id"])["feedback"]
    states = {f["entry_id"]: f["state"] for f in feedback}
    assert states == {"jd_legacy_test": "superseded", again["entry_id"]: "active"}
    assert next(f for f in feedback if f["state"] == "active")["realized_points"] == 64
    # only the decision that stands is evaluated
    replaced = client.post("/api/v1/journal/jd_legacy_test/evaluate")
    assert replaced.status_code == 409
    world["clock"]["now"] = AFTER


def test_long_workbench_plan_labels_are_stored(world: dict[str, Any], squad: list[int]) -> None:
    """Review finding: plan keys derived from 64-character labels overflowed the column (500)."""
    svc = world["svc"]
    store = JournalStore(svc.engine)
    base = store.get(world["entry"]["entry_id"])
    assert base is not None
    ev = DecisionEvidence.model_validate(base["evidence"]).model_copy(
        update={"source_plan": "2:" + "x" * 64 + "-alt-1", "chosen": None}
    )
    entry, created = store.record("jr-long", ev, "DEFER", "deferred", None, BEFORE)
    assert created and entry["source"]["plan"] == ev.source_plan


def test_no_evaluation_before_the_results_exist(world: dict[str, Any], squad: list[int]) -> None:
    client, svc = world["client"], world["svc"]
    e = world["entry"]
    world["clock"]["now"] = AFTER
    r = client.post(f"/api/v1/journal/{e['entry_id']}/evaluate")
    assert r.status_code == 202, r.text
    assert r.json()["job"]["status"] == "succeeded"
    got = client.get(f"/api/v1/journal/{e['entry_id']}").json()
    assert got["evaluation_status"] == "pending"
    assert got["evaluations"][0]["realized_points"] is None
    # a manually entered squad has no FPL ID: nothing first-party to compare with
    assert got["verification"] == "unavailable"
    assert "entered manually" in got["verification_detail"]["reason"]
    assert len(_evaluations(svc, e["entry_id"])) == 1


def test_partial_then_final_then_a_revision_without_touching_the_evidence(
    world: dict[str, Any], squad: list[int]
) -> None:
    client, svc = world["client"], world["svc"]
    e, rec = world["entry"], world["rec"]
    before = client.get(f"/api/v1/journal/{e['entry_id']}").json()["evidence"]
    fx = DS_A["fixtures"]
    gw_ids = sorted(int(i) for i in fx[(fx["season"] == SEASON) & (fx["gw"] == GW)]["fixture_id"])
    _promote(world, with_gameweek_results(DS_A, SEASON, GW, pts, fixture_ids=gw_ids[:5]))
    run_evaluations(svc, KEY, e["entry_id"], AFTER)
    got = client.get(f"/api/v1/journal/{e['entry_id']}").json()
    assert got["evaluation_status"] == "partial"
    assert got["evaluations"][0]["realized_points"] is None  # no totals from half the results

    _promote(world, with_gameweek_results(DS_A, SEASON, GW, pts))
    assert run_evaluations(svc, KEY, e["entry_id"], AFTER)[0] == 1
    got = client.get(f"/api/v1/journal/{e['entry_id']}").json()
    ev = got["evidence"]
    final = got["evaluations"][0]
    assert got["evaluation_status"] == "final" and final["status"] == "final"
    assert (
        final["realized_points"]
        == _xi(ev["chosen"], ev["chosen"]["captain"]) - ev["chosen"]["hit_points"]
    )
    assert final["realized_hold_points"] == _xi(ev["hold"], ev["hold"]["captain"])
    assert final["detail"]["provisional"] is False
    assert (
        final["detail"]["difference_vs_hold"]
        == final["realized_points"] - final["realized_hold_points"]
    )
    # the same results again write nothing
    assert run_evaluations(svc, KEY, e["entry_id"], AFTER)[0] == 0
    n = len(_evaluations(svc, e["entry_id"]))

    # an official correction: one starter's points change; a revision is added, the first kept
    star = ev["chosen"]["starters"][3]
    corrected = with_gameweek_results(DS_A, SEASON, GW, lambda c: pts(c) + (5 if c == star else 0))
    _promote(world, corrected)
    assert run_evaluations(svc, KEY, e["entry_id"], AFTER)[0] == 1
    rows = _evaluations(svc, e["entry_id"])
    assert len(rows) == n + 1 and rows[-1].revises_id == rows[-2].id
    assert rows[-1].realized_points == rows[-2].realized_points + 5
    assert rows[-1].detail_json["revision"]["previous_points"] == rows[-2].realized_points
    after = client.get(f"/api/v1/journal/{e['entry_id']}").json()
    assert after["evidence"] == before  # decision-time evidence never changes
    # the recommendation the first decision used is historical now; the journal still has it
    # the correction is withdrawn: the data is back to the first final state — that is a new
    # revision too, not "nothing changed" (review finding: it used to be dropped silently)
    # (snapshots are content-addressed: a later export with the same results differs elsewhere)
    restored = with_gameweek_results(DS_A, SEASON, GW, pts)
    fx_frame = restored["fixtures"].copy()
    other = fx_frame.index[fx_frame["season"] == "2024-25"][0]
    fx_frame.loc[other, "home_difficulty"] = 5 if fx_frame.loc[other, "home_difficulty"] != 5 else 4
    _promote(world, CanonicalDataset({**restored.frames, "fixtures": fx_frame}))
    assert run_evaluations(svc, KEY, e["entry_id"], AFTER)[0] == 1
    rows = _evaluations(svc, e["entry_id"])
    assert rows[-1].realized_points == rows[-3].realized_points
    assert rows[-1].revises_id == rows[-2].id
    assert [r.seq for r in rows] == list(range(1, len(rows) + 1))
    # a process still on older data never replaces an evaluation made on newer data
    res = journal_mod.legacy_unavailable("snap_older")
    store = JournalStore(svc.engine)
    assert store.save_evaluation(e["entry_id"], KEY, res, AFTER, lambda own, latest: True) is False
    assert len(_evaluations(svc, e["entry_id"])) == len(rows)
    # "something else": once the results are final it settles as unavailable, with holding's
    # and the recommendation's totals for reference (never a total for the unknown action)
    o = world["unknown"]
    run_evaluations(svc, OTHER, o["entry_id"], AFTER)
    got = client.get(f"/api/v1/journal/{o['entry_id']}").json()
    assert got["evaluation_status"] == "unavailable"
    last = got["evaluations"][0]
    assert last["realized_points"] is None and last["realized_hold_points"] is not None
    old = client.get(f"/api/v1/recommendations/{rec['id']}").json()
    assert old["lifecycle"]["current"] is False
    first = client.get(
        "/api/v1/journal", params={"manager_key": KEY, "include_superseded": True}
    ).json()["entries"]
    assert any(x["source"]["id"] == rec["id"] for x in first)


def test_confirmation_is_the_managers_own_statement(
    world: dict[str, Any], squad: list[int]
) -> None:
    client = world["client"]
    e = world["entry"]
    r = client.post(f"/api/v1/journal/{e['entry_id']}/confirm")
    assert r.status_code == 200 and r.json()["entry"]["confirmed_at"]
    again = client.post(f"/api/v1/journal/{e['entry_id']}/confirm").json()["entry"]
    assert again["confirmed_at"] == r.json()["entry"]["confirmed_at"]  # set once


def test_journal_routes_stay_with_their_owner(world: dict[str, Any], squad: list[int]) -> None:
    client = world["client"]
    world["clock"]["now"] = BEFORE
    alice = _register(client, "jr-alice@example.test")["session_token"]
    bob = _register(client, "jr-bob@example.test")["session_token"]
    a_key = _manager(client, alice, squad)
    b_key = _manager(client, bob, squad, bank=7)
    rec = _generate(client, a_key, headers=_bearer(alice))
    body = {"source": "recommendation", "source_id": rec["id"], "choice": "recommended"}
    r = client.post("/api/v1/journal", json={"manager_key": a_key, **body}, headers=_bearer(alice))
    assert r.status_code == 201, r.text
    entry = r.json()["entry"]["entry_id"]
    for method, path, kw in (
        ("GET", "/api/v1/journal", {"params": {"manager_key": a_key}}),
        ("GET", f"/api/v1/journal/{entry}", {}),
        ("POST", f"/api/v1/journal/{entry}/confirm", {}),
        ("POST", f"/api/v1/journal/{entry}/evaluate", {}),
        ("POST", "/api/v1/journal", {"json": {"manager_key": a_key, **body}}),
        # bob's own key in the query does not vouch for alice's key in the body
        (
            "POST",
            "/api/v1/journal",
            {"params": {"manager_key": b_key}, "json": {"manager_key": a_key, **body}},
        ),
    ):
        got = client.request(method, path, headers=_bearer(bob), **kw)
        assert got.status_code == 403, (method, path, got.status_code, got.text)
    # bob naming alice's recommendation under his own key: not found for his manager
    stolen = client.post(
        "/api/v1/journal", json={"manager_key": b_key, **body}, headers=_bearer(bob)
    )
    assert stolen.status_code == 404
    listed = client.get("/api/v1/journal", params={"manager_key": b_key}, headers=_bearer(bob))
    assert listed.status_code == 200 and listed.json()["entries"] == []
    mine = client.get(f"/api/v1/journal/{entry}", headers=_bearer(alice))
    assert mine.status_code == 200
    world["clock"]["now"] = AFTER


def test_export_and_erasure_cover_the_journal(world: dict[str, Any], squad: list[int]) -> None:
    client, svc = world["client"], world["svc"]
    e = world["entry"]
    exported = client.get(f"/api/v1/managers/{KEY}/export").json()
    ids = {x["entry_id"] for x in exported["decision_journal"]}
    assert e["entry_id"] in ids and exported["counts"]["decision_evaluations"] >= 3
    assert all(x["manager_key"] == KEY for x in exported["decision_evaluations"])
    deleted = client.delete(f"/api/v1/managers/{KEY}").json()["deleted"]
    assert deleted["decision_journal"] >= 3 and deleted["decision_evaluations"] >= 3
    after = client.get(f"/api/v1/managers/{KEY}/export").json()
    assert after["counts"]["decision_journal"] == 0 and after["counts"]["decision_evaluations"] == 0
    with session_scope(svc.engine) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(m.DecisionJournalRow)
                .where(m.DecisionJournalRow.manager_key == KEY)
            )
            == 0
        )
    # other managers' decisions are untouched
    assert client.get("/api/v1/journal", params={"manager_key": OTHER}).json()["entries"]
