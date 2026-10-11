"""Result lifecycle (M1.3): is a stored result still current, and if not, what changed?

Three stored results read as "the current answer": a recommendation (``recommendations``), a
Transfer Workbench run (``workbench_runs``) and the Copilot Home squad analysis
(``squad_analyses``). Each records the inputs it was computed from — the manager's squad state
(``manager_state.id``), the data snapshot (``data_snapshot_id``) and the forecast
(``prediction_run_id`` / ``forecast_key``) — and stays immutable afterwards. Currency is a
property of those inputs, judged against one **reference**: the promoted snapshot (the newest
with a ready serving forecast, ``DataService.promoted_snapshot_id``), read from disk the same
way in the API and in a worker that already computes on a newer, not yet promoted export.

* **Snapshot promotion** (the forecast job marks a newer snapshot's serving forecast ready)
  makes every result on a snapshot *older* than the promoted one *invalidated*: a persisted
  transition recorded with the old and new snapshot ids and the time it was detected
  (``invalidate_for_snapshot``, idempotent; the scheduler re-runs it, so a sweep that failed
  is completed later). Order is the snapshots' export time (manifest, else the registered
  row): a result on a *newer* snapshot than the reference is never invalidated. The API
  derives the same verdict at read time (and records it if the sweep missed the row), so a
  result is never served as current merely because the event handler did not run.
* **A newer squad state** makes a result *stale* for as long as that state is not the latest.
  This is derived, not persisted, for runs and analyses: re-saving an earlier squad makes it
  the latest again (content-addressed ids), and then a result computed for it is current
  again.
* **Supersession**: a newer recommendation for the same manager and gameweek (by request
  time, under a per-gameweek lock) supersedes the current one; a recommendation requested
  earlier but finished later is stored superseded and never replaces it.
* A job that finishes after its inputs moved on stores its result as historical (the stores'
  finalisation checks) and never replaces the newer current result.
* **Re-activation**: when the inputs of a historical recommendation or run are exactly the
  current ones again (the same squad re-saved, the same snapshot still promoted), asking the
  same question returns that identical result as current again, its identifiers unchanged —
  never a result whose snapshot is older than the reference.

``lifecycle_block`` is the verdict every affected response carries; ``explain_changes`` is the
detailed "what changed" (stored-state diff, snapshot-to-snapshot diff when the superseded
snapshot is still on disk, forecast versions) — computed from persisted data only, never
fabricated when the old inputs are gone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import structlog
from sqlalchemy import Engine, func, select, update

from fpl_api.services import REPO_ROOT, ForecastUnavailable
from fpl_domain.state import ManagerState
from fpl_storage import models as m
from fpl_storage.dataset import load_snapshot
from fpl_storage.db import session_scope
from fpl_storage.pit import PointInTimeView

log = structlog.get_logger("fpl_api.lifecycle")

SNAPSHOT_PROMOTED = "snapshot_promoted"
SQUAD_CHANGED = "squad_changed"
SUPERSEDED = "superseded"
FINALISED_STALE = "inputs_changed_before_finalised"
INPUTS_MISMATCH = "computed_on_other_inputs"  # a job computed other inputs than its run names
STATES = ("current", "stale", "superseded", "pending", "failed")
TRANSITIONS: dict[str, tuple[str, ...]] = {  # persisted recommendation statuses
    "active": ("superseded", "invalidated"),
    # re-activation only when the result's inputs are the current ones again
    # (``reactivation_allowed``): never for a result on an older snapshot
    "superseded": ("active",),
    "invalidated": ("active",),
}
LAZY_KINDS = ("recommendation", "workbench_run")  # the results with a persisted verdict


def can_transition(status: str, to: str) -> bool:
    return to in TRANSITIONS.get(status, ())


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return (ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)).isoformat()


def _ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        ts = v
    else:
        try:
            ts = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    return ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)


def reason(kind: str, detail: str, old: Any = None, new: Any = None, at: Any = None) -> dict:
    return {"kind": kind, "detail": detail, "old": old, "new": new, "detected_at": at}


# ------------------------------------------------------------------ snapshot order


def snapshot_path(settings: Any, snapshot_id: str) -> Path | None:
    """Where an exported snapshot lives, if it is still stored."""
    if settings is None:
        return None
    pinned = getattr(settings, "snapshot_dir", None)
    if pinned is not None and Path(pinned).name == snapshot_id:
        return Path(pinned)
    root = getattr(settings, "snapshots_root", None) or (REPO_ROOT / "data" / "snapshots")
    p = Path(root) / snapshot_id
    return p if (p / "manifest.json").exists() else None


_CREATED: dict[str, datetime] = {}  # manifest export time by path (manifests are immutable)


def snapshot_as_of(
    settings: Any, snapshot_id: str, session: Any = None, engine: Engine | None = None
) -> datetime | None:
    """When a snapshot was exported — the order snapshots are promoted in: its manifest on
    disk, else its registered row (whose ``as_of`` is that manifest time); None when neither
    exists any more."""
    path = snapshot_path(settings, snapshot_id)
    if path is not None:
        key = str(path)
        ts = _CREATED.get(key)
        if ts is None:
            ts = _ts(_manifest(path).get("created_at"))
            if ts is not None:
                _CREATED[key] = ts
        if ts is not None:
            return ts
    q = select(m.DataSnapshot.as_of).where(m.DataSnapshot.id == snapshot_id)
    if session is not None:
        return _ts(session.scalar(q))
    if engine is not None:
        try:
            with session_scope(engine) as s:
                return _ts(s.scalar(q))
        except Exception:  # unknown time: the caller treats the order as unproven
            log.warning("snapshot_as_of_failed", snapshot_id=snapshot_id, exc_info=True)
    return None


def snapshot_is_older(
    settings: Any,
    snapshot_id: str | None,
    reference: str | None,
    session: Any = None,
    engine: Engine | None = None,
) -> bool:
    """Is ``snapshot_id`` provably older than ``reference`` (the promoted snapshot)? A result
    on a newer snapshot — a worker computes on an export before the API promotes it — is not.
    A snapshot that is neither stored nor registered counts as older: retention removes only
    snapshots older than the served one, and the reference itself is on disk. In a pinned
    deployment every other snapshot is stale (nothing newer will be promoted)."""
    if not snapshot_id or not reference or snapshot_id == reference:
        return False
    if getattr(settings, "snapshot_dir", None) is not None:
        # a pinned deployment (``FPL_SNAPSHOT_DIR``, e.g. a data rollback) never promotes: a
        # result on any other snapshot — older or newer — is not on the data served
        return True
    ref = snapshot_as_of(settings, reference, session, engine)
    if ref is None:
        return False  # nothing can be proven against a reference of unknown time
    own = snapshot_as_of(settings, snapshot_id, session, engine)
    return own is None or own < ref


def follow_promotion(svc: Any, reference: str | None = None) -> str:
    """The promoted snapshot; and when this process serves ready snapshots only (the API) but
    still has an older one loaded, look again on the next access instead of waiting for the
    periodic check — so a regeneration right after a promotion runs on the promoted data."""
    ref = reference or svc.serving_snapshot_id()
    data = svc.data
    if (
        not data.pinned
        and svc.settings.serve_ready_snapshots_only
        and data.path is not None
        and data.path.name != ref
    ):
        data.refresh()
    return str(ref)


# ------------------------------------------------------------------ events (persisted)


def _promotion_payload(column: Any, promoted: str, now: datetime) -> Any:
    return func.jsonb_build_object(
        "kind",
        SNAPSHOT_PROMOTED,
        "detail",
        "a newer data snapshot was promoted to serving",
        "old",
        column,
        "new",
        promoted,
        "detected_at",
        now.isoformat(),
    )


def invalidate_for_snapshot(
    engine: Engine, promoted: str, settings: Any, detected_at: datetime | None = None
) -> dict[str, int]:
    """Snapshot promotion: every current recommendation and every succeeded workbench run on
    a snapshot *older* than the promoted one becomes historical, in one transaction. Results
    on a newer snapshot (computed by a worker on an export that is not promoted yet) are left
    alone. Idempotent — rows already invalidated are untouched, so the same promotion may be
    processed any number of times (the scheduler re-runs it)."""
    now = detected_at or datetime.now(UTC)
    with session_scope(engine) as s:
        candidates = set(
            s.scalars(
                select(m.RecommendationRow.data_snapshot_id)
                .where(
                    m.RecommendationRow.status == "active",
                    m.RecommendationRow.invalidated_at.is_(None),
                    m.RecommendationRow.data_snapshot_id != promoted,
                )
                .distinct()
            )
        ) | set(
            s.scalars(
                select(m.WorkbenchRunRow.data_snapshot_id)
                .where(
                    m.WorkbenchRunRow.status == "succeeded",
                    m.WorkbenchRunRow.invalidated_at.is_(None),
                    m.WorkbenchRunRow.data_snapshot_id != promoted,
                )
                .distinct()
            )
        )
        older = sorted(
            sid
            for sid in candidates
            if sid and snapshot_is_older(settings, sid, promoted, session=s)
        )
        out = {"recommendations": 0, "workbench_runs": 0}
        if older:
            recs = s.execute(
                update(m.RecommendationRow)
                .where(
                    m.RecommendationRow.status == "active",
                    m.RecommendationRow.invalidated_at.is_(None),
                    m.RecommendationRow.data_snapshot_id.in_(older),
                )
                .values(
                    status="invalidated",
                    invalidated_at=now,
                    invalidation_json=_promotion_payload(
                        m.RecommendationRow.data_snapshot_id, promoted, now
                    ),
                )
                .execution_options(synchronize_session=False)
            )
            runs = s.execute(
                update(m.WorkbenchRunRow)
                .where(
                    m.WorkbenchRunRow.status == "succeeded",
                    m.WorkbenchRunRow.invalidated_at.is_(None),
                    m.WorkbenchRunRow.data_snapshot_id.in_(older),
                )
                .values(
                    invalidated_at=now,
                    invalidation_json=_promotion_payload(
                        m.WorkbenchRunRow.data_snapshot_id, promoted, now
                    ),
                )
                .execution_options(synchronize_session=False)
            )
            out = {
                "recommendations": int(getattr(recs, "rowcount", 0) or 0),
                "workbench_runs": int(getattr(runs, "rowcount", 0) or 0),
            }
    if out["recommendations"] or out["workbench_runs"]:
        log.info("results_invalidated", promoted=promoted, **out)
    return out


def latest_state_id(session: Any, manager_key: str) -> str | None:
    return session.scalar(
        select(m.ManagerStateRow.id)
        .where(m.ManagerStateRow.state_json["manager_key"].astext == manager_key)
        .order_by(m.ManagerStateRow.captured_at.desc())
        .limit(1)
    )


def finalisation_check(
    session: Any,
    manager_key: str,
    state_id: str | None,
    snapshot_id: str | None,
    serving_snapshot_id: str | None,
    settings: Any = None,
    check_state: bool = True,
) -> dict[str, Any] | None:
    """Inside the transaction that stores a result: are its inputs still the current ones?
    Returns the invalidation record to store with it when they are not (the result is then
    historical from birth and never replaces the current one), else None.

    ``serving_snapshot_id`` is the promoted snapshot; only a result on an *older* snapshot is
    stale. ``check_state`` is off for workbench runs, whose squad staleness stays derived (a
    re-saved squad makes the run current again)."""
    now = datetime.now(UTC).isoformat()
    if check_state:
        latest = latest_state_id(session, manager_key)
        if state_id and latest and latest != state_id:
            return reason(
                FINALISED_STALE,
                "the squad was saved again while this result was being computed",
                {"state_id": state_id},
                {"state_id": latest},
                now,
            )
    if snapshot_is_older(settings, snapshot_id, serving_snapshot_id, session=session):
        return reason(
            FINALISED_STALE,
            "a newer data snapshot was promoted while this result was being computed",
            {"snapshot_id": snapshot_id},
            {"snapshot_id": serving_snapshot_id},
            now,
        )
    return None


def record_lazy_invalidation(
    engine: Engine, kind: str, result_id: str, snapshot_id: str, reference: str
) -> None:
    """The API found a current-looking result on a snapshot older than the promoted one (the
    promotion sweep did not reach it): persist the verdict it already derived. One conditional
    UPDATE — it changes nothing if the row was superseded, invalidated or re-computed in the
    meantime. Best effort: the response is marked stale whether or not this write succeeds."""
    if kind not in LAZY_KINDS:
        return
    try:
        with session_scope(engine) as s:
            now = datetime.now(UTC)
            payload = reason(
                SNAPSHOT_PROMOTED,
                "a newer data snapshot is being served (detected when the result was read)",
                snapshot_id,
                reference,
                now.isoformat(),
            )
            if kind == "recommendation":
                s.execute(
                    update(m.RecommendationRow)
                    .where(
                        m.RecommendationRow.id == result_id,
                        m.RecommendationRow.status == "active",
                        m.RecommendationRow.invalidated_at.is_(None),
                        m.RecommendationRow.data_snapshot_id == snapshot_id,
                    )
                    .values(status="invalidated", invalidated_at=now, invalidation_json=payload)
                    .execution_options(synchronize_session=False)
                )
            else:
                s.execute(
                    update(m.WorkbenchRunRow)
                    .where(
                        m.WorkbenchRunRow.id == result_id,
                        m.WorkbenchRunRow.status == "succeeded",
                        m.WorkbenchRunRow.invalidated_at.is_(None),
                        m.WorkbenchRunRow.data_snapshot_id == snapshot_id,
                    )
                    .values(invalidated_at=now, invalidation_json=payload)
                    .execution_options(synchronize_session=False)
                )
    except Exception:  # the derived verdict already protects the response
        log.warning("lazy_invalidation_failed", kind=kind, result_id=result_id, exc_info=True)


def reactivation_allowed(
    session: Any,
    manager_key: str,
    state_id: str | None,
    snapshot_id: str | None,
    reference: str | None,
    settings: Any,
    check_state: bool = True,
) -> bool:
    """A historical result may become current again only when its inputs are exactly the
    current ones: its squad state is the latest again and its snapshot is not older than the
    promoted one (a snapshot-invalidated result never qualifies)."""
    return (
        finalisation_check(
            session, manager_key, state_id, snapshot_id, reference, settings, check_state
        )
        is None
    )


def _names_snapshot(inv: dict[str, Any] | None) -> bool:
    """Does a persisted invalidation record a snapshot change (rather than a squad change or a
    run computed on other inputs than it names)?"""
    if not inv:
        return False
    if inv.get("kind") == SNAPSHOT_PROMOTED:
        return True
    old, new = inv.get("old"), inv.get("new")
    return (
        inv.get("kind") == FINALISED_STALE
        and isinstance(old, dict)
        and "snapshot_id" in old
        and old.get("snapshot_id") != (new or {}).get("snapshot_id")
    )


# ------------------------------------------------------------------ verdicts (derived)

_MISSING: Any = object()


def _state_reason(
    svc: Any, manager_key: str, state_id: str | None, latest: Any = _MISSING
) -> tuple[dict | None, Any]:
    if latest is _MISSING:
        latest = svc.states.latest(manager_key) if svc.states is not None else None
    latest_id = latest[0] if latest else None
    if state_id and latest_id and latest_id != state_id:
        return (
            reason(
                SQUAD_CHANGED,
                "the squad was saved again since this result was computed",
                state_id,
                latest_id,
                _iso(getattr(latest[1], "as_of", None)) if latest else None,
            ),
            latest_id,
        )
    return None, latest_id


def lifecycle_block(
    svc: Any,
    kind: str,
    result_id: str,
    manager_key: str,
    status: str,
    state_id: str | None,
    snapshot_id: str | None,
    invalidation: dict[str, Any] | None,
    job_status: str | None = None,
    reference: str | None = None,
    latest: Any = _MISSING,
) -> dict[str, Any]:
    """The verdict a response carries: ``state`` in current | stale | superseded | pending |
    failed, ``current`` (actionable now), the reasons with their identifiers, and the next
    action. Derived from the persisted result and the promoted snapshot (``reference``; the
    journal passes it once for all its rows, with the manager's ``latest`` state)."""
    ref = follow_promotion(svc, reference)
    reasons: list[dict[str, Any]] = []
    snapshot_stale = _names_snapshot(invalidation)
    if invalidation:
        reasons.append(dict(invalidation))
    if (
        not snapshot_stale
        and snapshot_id
        and snapshot_is_older(svc.settings, snapshot_id, ref, engine=svc.engine)
    ):
        snapshot_stale = True
        reasons.append(
            reason(
                SNAPSHOT_PROMOTED,
                "a newer data snapshot is being served than the one this result used",
                snapshot_id,
                ref,
                None,
            )
        )
        if (
            not invalidation
            and job_status in (None, "succeeded")
            and status in ("active", "succeeded")
            and svc.engine is not None
        ):
            record_lazy_invalidation(svc.engine, kind, result_id, snapshot_id, ref)
    sr, latest_id = _state_reason(svc, manager_key, state_id, latest)
    if sr:
        reasons.append(sr)
    if job_status in ("queued", "running"):
        state = "pending"
    elif job_status == "failed":
        state = "failed"
    elif status == "superseded":
        state = "superseded"
        reasons.insert(
            0,
            reason(SUPERSEDED, "a newer recommendation for this gameweek replaced it", result_id),
        )
    elif reasons or status == "invalidated":
        state = "stale"
    else:
        state = "current"
    return {
        "state": state,
        "current": state == "current",
        "reasons": reasons,
        "state_id": state_id,
        "latest_state_id": latest_id,
        "snapshot_id": snapshot_id,
        "served_snapshot_id": ref,  # the promoted snapshot the verdict is judged against
        "snapshot_stale": snapshot_stale,
        "next_action": None
        if state in ("current", "pending")
        else ("retry" if state == "failed" else "regenerate"),
        "evaluated_at": datetime.now(UTC).isoformat(),
    }


# ------------------------------------------------------------------ what changed (detailed)


def _manifest(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        return dict(json.loads((path / "manifest.json").read_text()))
    except (OSError, ValueError):
        return {}


def _player_rows(view: PointInTimeView, season: str, codes: list[int]) -> pd.DataFrame:
    ls = view.latest_snapshot(season)
    if ls.empty:
        return ls
    ls = ls[ls["player_code"].isin(codes)].set_index("player_code")
    return ls


def _f(v: Any) -> Any:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v.item() if hasattr(v, "item") else v


def state_changes(svc: Any, manager_key: str, old_id: str, new_id: str) -> dict[str, Any]:
    """What differs between two stored squad states of the same manager."""
    old = svc.states.get(old_id, manager_key)
    new = svc.states.get(new_id, manager_key)
    if old is None or new is None:
        return {
            "available": False,
            "reason": "a squad state is no longer stored",
            "old_state_id": old_id,
            "new_state_id": new_id,
        }
    names = svc.data.names(new.season)
    a, b = set(old.codes), set(new.codes)
    return {
        "available": True,
        "old_state_id": old_id,
        "new_state_id": new_id,
        "players_out": [{"player_code": c, "name": names.get(c, str(c))} for c in sorted(a - b)],
        "players_in": [{"player_code": c, "name": names.get(c, str(c))} for c in sorted(b - a)],
        "bank": {"old": old.bank, "new": new.bank},
        "free_transfers": {"old": old.free_transfers, "new": new.free_transfers},
        "source": {"old": old.source, "new": new.source},
        "captured_at": _iso(new.as_of),
    }


def snapshot_changes(
    svc: Any,
    new_svc: Any,
    old_id: str,
    codes: list[int],
    gameweeks: list[int],
    old_cutoff: str | None = None,
    old_season: str | None = None,
) -> dict[str, Any]:
    """What differs between the snapshot a result used and the promoted one (``new_svc``
    computes on it) for the players and gameweeks the result depends on: availability, news,
    price, fixture counts and the tables whose content changed. The old snapshot is read as the
    result read it — at its own decision cutoff and season — and the new one at the promoted
    snapshot's current cutoff. Honest when the old snapshot is no longer on disk."""
    new_id = new_svc.data.snapshot_id
    out: dict[str, Any] = {"old_snapshot_id": old_id, "new_snapshot_id": new_id}
    old_path = snapshot_path(svc.settings, old_id)
    new_path = snapshot_path(svc.settings, new_id)
    old_man, new_man = _manifest(old_path), _manifest(new_path)
    out["old_as_of"] = _iso(snapshot_as_of(svc.settings, old_id, engine=svc.engine))
    out["new_as_of"] = _iso(snapshot_as_of(svc.settings, new_id, engine=svc.engine))
    if old_path is None:
        out["available"] = False
        out["reason"] = (
            f"the superseded snapshot {old_id} is no longer stored (retention), so its inputs "
            "cannot be compared; the result is historical because a newer snapshot is served"
        )
        return out
    try:
        old_ds = load_snapshot(old_path)
    except Exception as exc:  # unreadable export: say so rather than guess
        out["available"] = False
        out["reason"] = f"the superseded snapshot could not be read: {exc}"
        return out
    ot, nt = old_man.get("tables") or {}, new_man.get("tables") or {}
    # table-level differences need both manifests; otherwise say "unknown", never "all"
    out["tables_changed"] = (
        sorted(
            t
            for t in set(ot) | set(nt)
            if (ot.get(t) or {}).get("sha256") != (nt.get(t) or {}).get("sha256")
        )
        if ot and nt
        else None
    )
    nctx = new_svc.context()
    nseason, oseason = nctx.season, old_season or nctx.season
    ocut = pd.Timestamp(old_cutoff) if old_cutoff else pd.Timestamp(nctx.cutoff)
    out["old_cutoff"], out["new_cutoff"] = ocut.isoformat(), pd.Timestamp(nctx.cutoff).isoformat()
    ov, nv = PointInTimeView(old_ds, ocut), new_svc.data.view(nctx.cutoff)
    names = new_svc.data.names(nseason)
    orow, nrow = _player_rows(ov, oseason, codes), _player_rows(nv, nseason, codes)
    if codes and orow.empty:
        out["available"] = False
        out["reason"] = (
            f"the superseded snapshot {old_id} holds no player data for {oseason} at the "
            "result's decision cutoff, so the players' inputs cannot be compared"
        )
        return out
    players = []
    for c in codes:
        o = orow.loc[c] if (not orow.empty and c in orow.index) else None
        n = nrow.loc[c] if (not nrow.empty and c in nrow.index) else None
        if o is None and n is None:
            continue
        diff: dict[str, Any] = {}
        for col in ("status", "chance_of_playing_next_round", "news", "price"):
            a = _f(o[col]) if o is not None and col in o.index else None
            b = _f(n[col]) if n is not None and col in n.index else None
            if a != b and not (a in (None, "") and b in (None, "")):
                diff[col] = {"old": a, "new": b}
        if diff:
            players.append({"player_code": c, "name": names.get(c, str(c)), **diff})
    out["players"] = players
    out["fixtures"] = []
    if oseason != nseason:
        out["season_changed"] = {"old": oseason, "new": nseason}
    else:
        try:
            ofx, nfx = ov.fixtures(oseason), nv.fixtures(nseason)
            for gw in gameweeks:
                a, b = ofx[ofx["gw"] == gw], nfx[nfx["gw"] == gw]
                if set(a["fixture_id"]) != set(b["fixture_id"]):
                    out["fixtures"].append(
                        {"gameweek": gw, "old_fixtures": len(a), "new_fixtures": len(b)}
                    )
        except Exception:  # fixtures are optional evidence
            out["fixtures"] = []
    out["available"] = True
    return out


def forecast_changes(
    svc: Any, new_svc: Any, old_prediction_run_id: str | None, horizon: int
) -> dict[str, Any]:
    """The forecast the result used against the promoted snapshot's serving forecast."""
    try:
        fc = new_svc.forecast_for(new_svc.context(), horizon)
    except ForecastUnavailable:
        return {
            "available": False,
            "old_prediction_run_id": old_prediction_run_id,
            "reason": "the forecast for the promoted snapshot is not computed yet",
        }
    out: dict[str, Any] = {
        "available": True,
        "old_prediction_run_id": old_prediction_run_id,
        "new_prediction_run_id": fc.run_id,
        "changed": bool(old_prediction_run_id) and old_prediction_run_id != fc.run_id,
        "new_model_versions": {
            k: str(v) for k, v in fc.provenance.get("model_versions", {}).items()
        },
    }
    if old_prediction_run_id and svc.engine is not None:
        with session_scope(svc.engine) as s:
            pr = s.get(m.PredictionRunRow, old_prediction_run_id)
            if pr is not None:
                out["old_model_versions"] = dict(pr.model_versions or {})
                out["old_cutoff_at"] = _iso(pr.cutoff_at)
    return out


def explain_changes(
    svc: Any,
    lifecycle: dict[str, Any],
    manager_key: str,
    codes: list[int],
    gameweeks: list[int],
    old_prediction_run_id: str | None,
    horizon: int,
    old_cutoff: str | None = None,
    old_season: str | None = None,
) -> dict[str, Any]:
    """The detailed "what changed" for a result whose lifecycle verdict is ``lifecycle``: the
    stored-state diff when the squad changed, and — when the result's snapshot is older than
    the promoted one — the snapshot and forecast differences, read from the promoted snapshot
    even while this process still has an older one loaded."""
    kinds = {r["kind"] for r in lifecycle["reasons"]}
    out: dict[str, Any] = {
        "lifecycle": lifecycle,
        "state": None,
        "snapshot": None,
        "forecast": None,
    }
    if SQUAD_CHANGED in kinds or (
        lifecycle["state_id"]
        and lifecycle["latest_state_id"]
        and lifecycle["state_id"] != lifecycle["latest_state_id"]
    ):
        out["state"] = state_changes(
            svc, manager_key, lifecycle["state_id"], lifecycle["latest_state_id"]
        )
    old_snap, ref = lifecycle["snapshot_id"], lifecycle["served_snapshot_id"]
    if lifecycle.get("snapshot_stale") and old_snap and ref and old_snap != ref:
        try:
            new_svc = svc.at_snapshot(ref)
        except LookupError as exc:  # the promoted snapshot is not readable here
            out["snapshot"] = {
                "available": False,
                "old_snapshot_id": old_snap,
                "new_snapshot_id": ref,
                "reason": str(exc),
            }
            return out
        out["snapshot"] = snapshot_changes(
            svc, new_svc, old_snap, codes, gameweeks, old_cutoff, old_season
        )
        out["forecast"] = forecast_changes(svc, new_svc, old_prediction_run_id, horizon)
    return out


def is_current_state(svc: Any, manager_key: str, state_id: str | None) -> bool:
    _r, latest = _state_reason(svc, manager_key, state_id)
    return bool(state_id) and (latest is None or latest == state_id)


def manager_state_as_of(st: ManagerState) -> str | None:
    return _iso(getattr(st, "as_of", None))
