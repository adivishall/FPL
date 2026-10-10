"""Copilot Home read model: a precomputed, versioned squad analysis (M1.1a).

Everything on the Home page that costs more than a table lookup is computed here, once per
(manager, squad state, data snapshot, forecast, analysis version), by the worker when a new
forecast is promoted or a squad changes — never inside a page request. The analysis only reads
the serving forecast the decision engine itself uses; it runs no solver and no simulation, so it
cannot disagree with a recommendation computed from the same inputs. Every number carries its
source in the payload (``sources``) and every health signal names the inputs behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, select

from fpl_api.services import CurrentContext
from fpl_decision.captaincy import analyse_captaincy
from fpl_domain.enums import ChipType
from fpl_domain.hashing import short_id
from fpl_domain.state import ManagerState
from fpl_forecasting.pipeline import Forecast
from fpl_optimizer.lineup import best_lineup
from fpl_optimizer.problem import load_optimizer_config
from fpl_storage import models as m
from fpl_storage.db import session_scope

ANALYSIS_VERSION = "squad-analysis-1"
FIXTURE_HORIZON = 5  # gameweeks of fixture outlook shown (bounded by the forecast horizon)

SOURCES = {
    "forecast": "the serving forecast (decomposed points model, joint Monte Carlo) — the same "
    "artefact the recommendation engine uses",
    "status": "player status, news and chance of playing: the latest FPL bootstrap capture in "
    "the data snapshot",
    "fixtures": "fixture list and difficulty: the FPL fixtures endpoint in the data snapshot "
    "(FPL's official difficulty rating 1–5)",
    "price_risk": "engine price-change probabilities (calibrated classifier), never blended "
    "with FPL's official predictor",
    "lineup": "starting XI, bench order and captain profiles: the lineup optimiser and paired "
    "captaincy re-scoring on the same forecast samples",
    "state": "squad, bank, free transfers and chips: the manager's stored state (imported from "
    "the FPL API or entered manually)",
}


def lineup_and_captaincy(
    svc: Any, ctx: CurrentContext, fc: Forecast, st: ManagerState, chip: ChipType | None = None
) -> dict[str, Any]:
    """The lineup optimiser's XI, bench, armband and captain profiles for a squad (shared by
    ``POST /lineup`` and the Home analysis so both always show the same answer)."""
    table = svc.table(ctx, fc, 1, st)
    idx = table.index()
    ev = {c: float(table.ev[idx[c], 0]) if c in idx else 0.0 for c in st.codes}
    rs = svc.data.ruleset(ctx.season)
    pos = table.positions_map()
    ch = best_lineup(list(st.codes), pos, ev, ev, load_optimizer_config().objective, rs, chip)
    sim = fc.simulation
    have = set(sim.player_codes.tolist())
    gi = sim.gw_index(ctx.gameweek)
    pts = {
        c: sim.points[:, sim.index_of(c), gi].astype(np.int64)
        if c in have
        else np.zeros(sim.n_sims, np.int64)
        for c in st.codes
    }
    mins = {
        c: sim.minutes[:, sim.index_of(c), gi].astype(np.int64)
        if c in have
        else np.zeros(sim.n_sims, np.int64)
        for c in st.codes
    }
    cap = analyse_captaincy(ch.lineup, pos, pts, mins, rs, chip=chip)
    return {
        "lineup": ch.lineup.model_dump(),
        "expected_points": ch.expected_points,
        "captaincy": {
            "expected": cap.expected,
            "safe": cap.safe,
            "high_variance": cap.high_variance,
            "options": [o.__dict__ for o in cap.options],
            "beats_matrix": cap.beats_matrix.round(3).tolist(),
        },
    }


def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return default if np.isnan(x) else x


def _fixture_outlook(
    fixtures: pd.DataFrame, season: str, gw_from: int, gw_to: int, team_names: dict[int, str]
) -> dict[int, list[dict[str, Any]]]:
    """Per team: the fixtures in [gw_from, gw_to] with opponent, venue and FPL difficulty."""
    f = fixtures
    if "season" in f.columns:
        f = f[f["season"] == season]
    f = f[(f["gw"] >= gw_from) & (f["gw"] <= gw_to)]
    out: dict[int, list[dict[str, Any]]] = {}
    for row in f.itertuples(index=False):
        home, away = int(row.home_team_code), int(row.away_team_code)
        gw = int(row.gw)
        out.setdefault(home, []).append(
            {
                "gw": gw,
                "opponent": team_names.get(away, str(away)),
                "opponent_code": away,
                "home": True,
                "difficulty": _f(getattr(row, "home_difficulty", None)),
            }
        )
        out.setdefault(away, []).append(
            {
                "gw": gw,
                "opponent": team_names.get(home, str(home)),
                "opponent_code": home,
                "home": False,
                "difficulty": _f(getattr(row, "away_difficulty", None)),
            }
        )
    for team_fixtures in out.values():
        team_fixtures.sort(key=lambda x: (x["gw"], x["opponent"]))
    return out


def compute_squad_analysis(
    svc: Any, manager_key: str, state_id: str, st: ManagerState, horizon: int
) -> dict[str, Any]:
    ctx = svc.context()
    fc = svc.forecast_for(ctx, horizon)
    season, gw = ctx.season, ctx.gameweek
    names, teams = svc.data.names(season), svc.data.teams(season)
    view = svc.data.view(ctx.cutoff)
    pool = view.player_pool(season, gw).set_index("player_code")
    snap = view.latest_snapshot(season)
    snap = snap.set_index("player_code") if not snap.empty else snap
    probs = svc.price_probs(ctx)
    probs = probs.set_index("player_code") if probs is not None else None
    sm = fc.summary
    sm = sm[(sm["gw"] >= gw) & (sm["gw"] < gw + horizon)]
    outlook = _fixture_outlook(
        svc.data.ds["fixtures"], season, gw, gw + min(horizon, FIXTURE_HORIZON) - 1, teams
    )
    lu = lineup_and_captaincy(svc, ctx, fc, st)
    starters = set(lu["lineup"]["starters"])
    bench_order = list(lu["lineup"]["bench"])

    players: list[dict[str, Any]] = []
    for p in st.squad:
        code = p.player_code
        rows = sm[sm["player_code"] == code].sort_values("gw")
        nxt = rows[rows["gw"] == gw]
        per_gw = [
            {
                "gw": int(r.gw),
                "xp": _f(r.mean, 0.0),
                "p10": _f(r.p10, 0.0),
                "p90": _f(r.p90, 0.0),
                "p_start": _f(r.prob_start, 0.0),
            }
            for r in rows.itertuples(index=False)
        ]
        live = snap.loc[code] if (not snap.empty and code in snap.index) else None
        status = str(live["status"]) if live is not None and "status" in live else "a"
        chance = _f(live["chance_of_playing_next_round"]) if live is not None else None
        news = str(live["news"]) if live is not None and isinstance(live.get("news"), str) else ""
        price_now = int(pool.loc[code, "price"]) if code in pool.index else p.purchase_price
        pr = probs.loc[code] if (probs is not None and code in probs.index) else None
        players.append(
            {
                "player_code": code,
                "name": names.get(code, str(code)),
                "team": teams.get(p.team_code, str(p.team_code)),
                "team_code": p.team_code,
                "position": p.position.value,
                "purchase_price": p.purchase_price,
                "price": price_now,
                "xp_next": _f(nxt["mean"].iloc[0], 0.0) if len(nxt) else 0.0,
                "p10_next": _f(nxt["p10"].iloc[0], 0.0) if len(nxt) else 0.0,
                "p90_next": _f(nxt["p90"].iloc[0], 0.0) if len(nxt) else 0.0,
                "p_start_next": _f(nxt["prob_start"].iloc[0], 0.0) if len(nxt) else 0.0,
                "xp_horizon": float(rows["mean"].sum()) if len(rows) else 0.0,
                "per_gw": per_gw,
                "status": status,
                "chance_of_playing": chance,
                "news": news,
                "starter": code in starters,
                "bench_slot": bench_order.index(code) + 1 if code in bench_order else None,
                "p_rise": _f(pr["p_rise"]) if pr is not None else None,
                "p_fall": _f(pr["p_fall"]) if pr is not None else None,
                "fixtures": outlook.get(p.team_code, []),
                "blank_next": not any(x["gw"] == gw for x in outlook.get(p.team_code, [])),
                "double_next": sum(x["gw"] == gw for x in outlook.get(p.team_code, [])) > 1,
            }
        )

    health = _health_signals(players, st, gw)
    xp_next = sum(x["xp_next"] for x in players if x["starter"])
    bench_xp = sum(x["xp_next"] for x in players if not x["starter"])
    return {
        "version": ANALYSIS_VERSION,
        "computed_at": datetime.now(UTC).isoformat(),
        "manager_key": manager_key,
        "state_id": state_id,
        "snapshot_id": svc.data.snapshot_id,
        "forecast_key": str(fc.provenance.get("forecast_key", fc.run_id)),
        "season": season,
        "gameweek": gw,
        "horizon": horizon,
        "state": {
            "bank": st.bank,
            "free_transfers": st.free_transfers,
            "manager_id": st.manager_id,
            "source": st.source,
            "chips": [c.model_dump(mode="json") for c in st.chips],
            "squad_value": int(sum(x["price"] for x in players)),
        },
        "players": players,
        "lineup": lu["lineup"],
        "lineup_expected_points": lu["expected_points"],
        "captaincy": lu["captaincy"],
        "totals": {"xi_xp_next": xp_next, "bench_xp_next": bench_xp},
        "health": health,
        "sources": SOURCES,
    }


def _health_signals(
    players: list[dict[str, Any]], st: ManagerState, gw: int
) -> list[dict[str, Any]]:
    """Interpretable signals, each naming its inputs; no composite score."""
    out: list[dict[str, Any]] = []
    unavailable = [
        p
        for p in players
        if p["status"] not in ("a",)
        or (p["chance_of_playing"] is not None and p["chance_of_playing"] < 75)
    ]
    for p in unavailable:
        chance = p["chance_of_playing"]
        out.append(
            {
                "kind": "availability",
                "severity": "bad"
                if p["status"] in ("i", "s", "u", "n") or (chance is not None and chance < 50)
                else "warn",
                "players": [p["player_code"]],
                "message": (
                    f"{p['name']}: status '{p['status']}'"
                    + (f", {chance:.0f}% chance of playing" if chance is not None else "")
                    + (f" — {p['news']}" if p["news"] else "")
                    + (" (starter)" if p["starter"] else " (bench)")
                ),
                "evidence": {
                    "status": p["status"],
                    "chance_of_playing": chance,
                    "p_start_next": p["p_start_next"],
                },
                "source": "status",
            }
        )
    for p in players:
        if p["starter"] and p["p_start_next"] < 0.6 and p not in unavailable:
            out.append(
                {
                    "kind": "minutes",
                    "severity": "warn",
                    "players": [p["player_code"]],
                    "message": (
                        f"{p['name']} starts in only {p['p_start_next']:.0%} of simulations "
                        "this gameweek but is in your XI"
                    ),
                    "evidence": {"p_start_next": p["p_start_next"], "xp_next": p["xp_next"]},
                    "source": "forecast",
                }
            )
    blanks = [p for p in players if p["blank_next"]]
    if blanks:
        out.append(
            {
                "kind": "fixtures",
                "severity": "warn" if any(p["starter"] for p in blanks) else "info",
                "players": [p["player_code"] for p in blanks],
                "message": f"{len(blanks)} player(s) have no fixture in GW{gw}: "
                + ", ".join(p["name"] for p in blanks),
                "evidence": {"gameweek": gw},
                "source": "fixtures",
            }
        )
    doubles = [p for p in players if p["double_next"]]
    if doubles:
        out.append(
            {
                "kind": "fixtures",
                "severity": "info",
                "players": [p["player_code"] for p in doubles],
                "message": f"{len(doubles)} player(s) have a double gameweek in GW{gw}: "
                + ", ".join(p["name"] for p in doubles),
                "evidence": {"gameweek": gw},
                "source": "fixtures",
            }
        )
    hard = []
    for p in players:
        diffs = [x["difficulty"] for x in p["fixtures"][:3] if x["difficulty"] is not None]
        if len(diffs) >= 2 and sum(diffs) / len(diffs) >= 4.0:
            hard.append((p, sum(diffs) / len(diffs)))
    if hard:
        out.append(
            {
                "kind": "fixtures",
                "severity": "info",
                "players": [p["player_code"] for p, _ in hard],
                "message": "Tough run (FPL difficulty ≥ 4 on average over the next 3): "
                + ", ".join(f"{p['name']} ({d:.1f})" for p, d in hard),
                "evidence": {"threshold": 4.0, "window": 3},
                "source": "fixtures",
            }
        )
    bench = [p for p in players if not p["starter"]]
    bench_starters = [p for p in bench if p["p_start_next"] >= 0.5]
    if bench and len(bench_starters) < 2:
        out.append(
            {
                "kind": "bench",
                "severity": "warn",
                "players": [p["player_code"] for p in bench],
                "message": (
                    f"Thin bench cover: only {len(bench_starters)} of {len(bench)} bench players "
                    "are likely to start (P(start) ≥ 50%)"
                ),
                "evidence": {
                    "bench_p_start": {str(p["player_code"]): p["p_start_next"] for p in bench}
                },
                "source": "forecast",
            }
        )
    weakest = min((p for p in players if p["starter"]), key=lambda p: p["xp_next"], default=None)
    best_bench = max(bench, key=lambda p: p["xp_next"], default=None)
    if weakest and best_bench and best_bench["xp_next"] > weakest["xp_next"] + 0.5:
        out.append(
            {
                "kind": "bench",
                "severity": "info",
                "players": [weakest["player_code"], best_bench["player_code"]],
                "message": (
                    f"{best_bench['name']} on the bench projects more "
                    f"({best_bench['xp_next']:.1f}) than {weakest['name']} in the XI "
                    f"({weakest['xp_next']:.1f}); the lineup "
                    "optimiser keeps the XI legal by formation"
                ),
                "evidence": {"xp_bench": best_bench["xp_next"], "xp_starter": weakest["xp_next"]},
                "source": "lineup",
            }
        )
    falls = [p for p in players if p["p_fall"] is not None and p["p_fall"] >= 0.3]
    rises = [p for p in players if p["p_rise"] is not None and p["p_rise"] >= 0.3]
    if falls:
        out.append(
            {
                "kind": "price",
                "severity": "warn",
                "players": [p["player_code"] for p in falls],
                "message": "Price-fall risk tonight (engine P(fall) ≥ 30%): "
                + ", ".join(f"{p['name']} ({p['p_fall']:.0%})" for p in falls),
                "evidence": {str(p["player_code"]): p["p_fall"] for p in falls},
                "source": "price_risk",
            }
        )
    if rises:
        out.append(
            {
                "kind": "price",
                "severity": "info",
                "players": [p["player_code"] for p in rises],
                "message": "Likely price rises (engine P(rise) ≥ 30%): "
                + ", ".join(f"{p['name']} ({p['p_rise']:.0%})" for p in rises),
                "evidence": {str(p["player_code"]): p["p_rise"] for p in rises},
                "source": "price_risk",
            }
        )
    by_pos: dict[str, list[dict[str, Any]]] = {}
    for p in players:
        by_pos.setdefault(p["position"], []).append(p)
    afford = {
        pos: st.bank + min(x["price"] for x in ps) for pos, ps in by_pos.items()
    }  # selling at the current price is an upper bound on what a like-for-like swap affords
    out.append(
        {
            "kind": "affordability",
            "severity": "info",
            "players": [],
            "message": f"Bank {st.bank / 10:.1f}m, {st.free_transfers} free transfer(s). Most you "
            "could pay per position by selling your cheapest there: "
            + ", ".join(f"{pos} {v / 10:.1f}m" for pos, v in sorted(afford.items())),
            "evidence": {
                "bank": st.bank,
                "free_transfers": st.free_transfers,
                "max_price_by_position": afford,
                "assumption": "selling price = current price (an upper bound)",
            },
            "source": "state",
        }
    )
    order = {"bad": 0, "warn": 1, "info": 2}
    out.sort(key=lambda x: order[x["severity"]])
    return out


@dataclass
class AnalysisStore:
    engine: Engine

    @staticmethod
    def _id(manager_key: str, state_id: str, snapshot_id: str, forecast_key: str) -> str:
        return short_id(
            "sqa",
            {
                "m": manager_key,
                "s": state_id,
                "d": snapshot_id,
                "f": forecast_key,
                "v": ANALYSIS_VERSION,
            },
        )

    def get(
        self, manager_key: str, state_id: str, snapshot_id: str, forecast_key: str
    ) -> dict[str, Any] | None:
        with session_scope(self.engine) as s:
            row = s.get(
                m.SquadAnalysisRow, self._id(manager_key, state_id, snapshot_id, forecast_key)
            )
            return dict(row.payload_json) if row else None

    def latest(self, manager_key: str) -> dict[str, Any] | None:
        with session_scope(self.engine) as s:
            row = s.scalars(
                select(m.SquadAnalysisRow)
                .where(m.SquadAnalysisRow.manager_key == manager_key)
                .order_by(m.SquadAnalysisRow.computed_at.desc())
                .limit(1)
            ).first()
            return dict(row.payload_json) if row else None

    def save(self, payload: dict[str, Any]) -> str:
        sid = self._id(
            payload["manager_key"],
            payload["state_id"],
            payload["snapshot_id"],
            payload["forecast_key"],
        )
        with session_scope(self.engine) as s:
            if s.get(m.SquadAnalysisRow, sid) is None:
                s.add(
                    m.SquadAnalysisRow(
                        id=sid,
                        manager_key=payload["manager_key"],
                        state_id=payload["state_id"],
                        data_snapshot_id=payload["snapshot_id"],
                        forecast_key=payload["forecast_key"],
                        version=ANALYSIS_VERSION,
                        payload_json=payload,
                    )
                )
        return sid

    def delete_manager(self, manager_key: str) -> int:
        from sqlalchemy import delete  # noqa: PLC0415

        with session_scope(self.engine) as s:
            res = s.execute(
                delete(m.SquadAnalysisRow).where(m.SquadAnalysisRow.manager_key == manager_key)
            )
            return int(getattr(res, "rowcount", 0) or 0)
