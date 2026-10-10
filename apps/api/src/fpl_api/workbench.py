"""Transfer Workbench product layer (M1.2): a request becomes a stored run bound to the exact
squad state, data snapshot and forecast it is computed for; a background job evaluates it with
``fpl_decision.workbench``; the result is served to its owner with stale detection (a newer
squad state or snapshot) and the identifiers that trace it to its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import Engine, delete, select

from fpl_api.services import CurrentContext
from fpl_decision.workbench import WORKBENCH_VERSION, PlanSpec, evaluate_plans
from fpl_domain.hashing import short_id
from fpl_domain.state import ManagerState, with_current_clubs
from fpl_forecasting.pipeline import Forecast
from fpl_optimizer.problem import OptimizationProblem, Preferences, load_optimizer_config
from fpl_storage import models as m
from fpl_storage.db import session_scope

JOB_KIND = "workbench"


def _row(r: m.WorkbenchRunRow, result: bool = True) -> dict[str, Any]:
    return {
        "id": r.id,
        "manager_key": r.manager_key,
        "state_id": r.state_id,
        "snapshot_id": r.data_snapshot_id,
        "forecast_key": r.forecast_key,
        "version": r.version,
        "horizon": r.horizon,
        "profile": r.profile,
        "request": dict(r.request_json),
        "status": r.status,
        "job_id": r.job_id,
        "result": dict(r.result_json) if (result and r.result_json) else None,
        "error": r.error,
        "created_at": r.created_at.isoformat(),
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


@dataclass
class WorkbenchStore:
    engine: Engine

    @staticmethod
    def run_id(
        manager_key: str,
        state_id: str,
        snapshot_id: str,
        forecast_key: str,
        request: dict[str, Any],
    ) -> str:
        return short_id(
            "wb",
            {
                "m": manager_key,
                "s": state_id,
                "d": snapshot_id,
                "f": forecast_key,
                "v": WORKBENCH_VERSION,
                "r": request,
            },
        )

    def get(self, run_id: str) -> dict[str, Any] | None:
        with session_scope(self.engine) as s:
            r = s.get(m.WorkbenchRunRow, run_id)
            return _row(r) if r else None

    def create(
        self,
        manager_key: str,
        state_id: str,
        snapshot_id: str,
        forecast_key: str,
        horizon: int,
        profile: str,
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """The run for this question on these inputs, and whether it needs (re)computing: a
        queued, running or finished run is returned as is; a failed one is re-queued."""
        rid = self.run_id(manager_key, state_id, snapshot_id, forecast_key, request)
        with session_scope(self.engine) as s:
            r = s.get(m.WorkbenchRunRow, rid)
            if r is None:
                r = m.WorkbenchRunRow(
                    id=rid,
                    manager_key=manager_key,
                    state_id=state_id,
                    data_snapshot_id=snapshot_id,
                    forecast_key=forecast_key,
                    version=WORKBENCH_VERSION,
                    horizon=horizon,
                    profile=profile,
                    request_json=request,
                    status="queued",
                )
                s.add(r)
                s.flush()
                return _row(r), True
            if r.status == "failed":
                r.status, r.error, r.job_id, r.finished_at = "queued", None, None, None
                return _row(r), True
            return _row(r), False

    def recent(self, manager_key: str, limit: int = 10) -> list[dict[str, Any]]:
        with session_scope(self.engine) as s:
            rows = s.scalars(
                select(m.WorkbenchRunRow)
                .where(m.WorkbenchRunRow.manager_key == manager_key)
                .order_by(m.WorkbenchRunRow.created_at.desc())
                .limit(limit)
            ).all()
            return [_row(r, result=False) for r in rows]

    def set_job(self, run_id: str, job_id: str) -> None:
        with session_scope(self.engine) as s:
            r = s.get(m.WorkbenchRunRow, run_id)
            if r is not None:
                r.job_id = job_id

    def start(self, run_id: str) -> None:
        with session_scope(self.engine) as s:
            r = s.get(m.WorkbenchRunRow, run_id)
            if r is not None:
                r.status = "running"

    def finish(self, run_id: str, result: dict[str, Any]) -> None:
        with session_scope(self.engine) as s:
            r = s.get(m.WorkbenchRunRow, run_id)
            if r is not None:
                r.status, r.result_json, r.error = "succeeded", result, None
                r.finished_at = datetime.now(UTC)

    def fail(self, run_id: str, error: str) -> None:
        with session_scope(self.engine) as s:
            r = s.get(m.WorkbenchRunRow, run_id)
            if r is not None and r.status != "succeeded":
                r.status, r.error, r.finished_at = "failed", error[:2000], datetime.now(UTC)

    def export(self, manager_key: str) -> list[dict[str, Any]]:
        with session_scope(self.engine) as s:
            rows = s.scalars(
                select(m.WorkbenchRunRow).where(m.WorkbenchRunRow.manager_key == manager_key)
            ).all()
            return [_row(r) for r in rows]

    def delete_manager(self, manager_key: str) -> int:
        with session_scope(self.engine) as s:
            res = s.execute(
                delete(m.WorkbenchRunRow).where(m.WorkbenchRunRow.manager_key == manager_key)
            )
            return int(getattr(res, "rowcount", 0) or 0)


def workbench_problem(
    svc: Any, ctx: CurrentContext, fc: Forecast, st: ManagerState, horizon: int, profile: str
) -> OptimizationProblem:
    """The planning problem on the *full* player universe (every player with a forecast), so a
    plan may name any player; the workbench pools it per plan."""
    table = svc.table(ctx, fc, horizon, st)
    st = with_current_clubs(st, table.teams_by_code())
    return OptimizationProblem(
        state=st,
        ruleset=svc.data.ruleset(ctx.season),
        players=table,
        gameweeks=tuple(range(ctx.gameweek, ctx.gameweek + table.horizon)),
        config=load_optimizer_config(profile),
        preferences=Preferences(),
    )


def compute_workbench(svc: Any, run: dict[str, Any], st: ManagerState) -> dict[str, Any]:
    """Evaluate a stored run's plans for the state it names (the job body)."""
    ctx = svc.context()
    h = svc.horizon(run["horizon"])
    fc = svc.forecast_for(ctx, h)
    prob = workbench_problem(svc, ctx, fc, st, h, run["profile"])
    specs = [PlanSpec.model_validate(p) for p in run["request"]["plans"]]
    res = evaluate_plans(
        prob,
        fc,
        specs,
        svc.data.names(ctx.season),
        svc.data.teams(ctx.season),
        workers=svc.settings.solver_workers,
    )
    prov = fc.provenance
    return {
        "run_id": run["id"],
        "manager_key": run["manager_key"],
        "state_id": run["state_id"],
        "snapshot_id": svc.data.snapshot_id,
        "forecast_key": str(prov.get("forecast_key", fc.run_id)),
        "prediction_run_id": fc.run_id,
        "forecast": {
            "cutoff": str(prov.get("cutoff")),
            "latest_source_at": str(prov.get("max_source_available_at")),
            "n_simulations": prov.get("n_simulations"),
            "simulation_seed": prov.get("simulation_seed"),
            "model_versions": {k: str(v) for k, v in prov.get("model_versions", {}).items()},
        },
        "computed_at": datetime.now(UTC).isoformat(),
        "horizon": h,
        "profile": run["profile"],
        "result": res.model_dump(mode="json"),
    }


def workbench_context(
    svc: Any, manager_key: str, state_id: str, st: ManagerState, fc: Forecast, ctx: CurrentContext
) -> dict[str, Any]:
    """What the workbench shows before any plan is evaluated: the squad with purchase and
    selling prices, next-gameweek and horizon expectations, availability, the bank, transfers
    and chips, and the current analysis's health signals as candidate weak spots."""
    season, gw = ctx.season, ctx.gameweek
    names, teams = svc.data.names(season), svc.data.teams(season)
    rs = svc.data.ruleset(season)
    view = svc.data.view(ctx.cutoff)
    pool = view.player_pool(season, gw).set_index("player_code")
    snap = view.latest_snapshot(season)
    snap = snap.set_index("player_code") if not snap.empty else snap
    prices = {
        p.player_code: int(pool.loc[p.player_code, "price"])
        if p.player_code in pool.index
        else p.purchase_price
        for p in st.squad
    }
    selling = st.selling_values(prices, rs)
    sm = fc.summary
    horizon = int(sm["gw"].max() - gw + 1) if len(sm) else 1
    sm = sm[(sm["gw"] >= gw) & (sm["gw"] < gw + horizon)]
    players = []
    for p in st.squad:
        code = p.player_code
        rows = sm[sm["player_code"] == code]
        nxt = rows[rows["gw"] == gw]
        live = snap.loc[code] if (not snap.empty and code in snap.index) else None
        status = str(live["status"]) if live is not None and "status" in live else "a"
        chance = live["chance_of_playing_next_round"] if live is not None else None
        players.append(
            {
                "player_code": code,
                "name": names.get(code, str(code)),
                "team": teams.get(p.team_code, str(p.team_code)),
                "team_code": p.team_code,
                "position": p.position.value,
                "purchase_price": p.purchase_price,
                "price": prices[code],
                "selling_price": selling[code],
                "xp_next": float(nxt["mean"].iloc[0]) if len(nxt) else 0.0,
                "xp_horizon": float(rows["mean"].sum()) if len(rows) else 0.0,
                "p_start_next": float(nxt["prob_start"].iloc[0]) if len(nxt) else 0.0,
                "status": status,
                "chance_of_playing": None if chance is None or pd.isna(chance) else float(chance),
            }
        )
    health: list[dict[str, Any]] = []
    if svc.analyses is not None:
        fkey = str(fc.provenance.get("forecast_key", fc.run_id))
        a = svc.analyses.get(manager_key, state_id, svc.data.snapshot_id, fkey)
        if a is not None:
            health = [
                {
                    "kind": h["kind"],
                    "severity": h["severity"],
                    "message": h["message"],
                    "players": h.get("players", []),
                }
                for h in a.get("health", [])
                if h.get("players")
            ]
    return {
        "manager_key": manager_key,
        "state_id": state_id,
        "season": season,
        "gameweek": gw,
        "horizon": horizon,
        "state": {
            "bank": st.bank,
            "free_transfers": st.free_transfers,
            "transfers_made": st.transfers_made,
            "manager_id": st.manager_id,
            "source": st.source,
            "chips_available": [c.chip_id for c in st.available_chips()],
            "squad_value": int(sum(prices.values())),
            "selling_value": int(sum(selling.values())),
        },
        "rules": {
            "hit_cost": rs.transfers.hit_cost,
            "max_per_club": rs.squad.max_per_club,
            "selling_price_rule": rs.pricing.selling_price_rule.value,
            "ruleset_version": rs.ruleset_version,
        },
        "players": players,
        "weak_spots": health,
        "forecast_key": str(fc.provenance.get("forecast_key", fc.run_id)),
        "snapshot_id": svc.data.snapshot_id,
    }
