"""Optimiser benchmark on realistic workloads (§28, §61, §79, §86.3; ADR-0007).

Workload: 12 real decision cutoffs (GW 6, 16, 26, 33 of 2023-24, 2024-25, 2025-26). At each,
the forecast models are trained on data before the cutoff and an 8-gameweek, 1,000-sample
forecast is simulated (the production configuration). Four realistic squad states per cutoff:

* ``optimal``  — the initial-squad optimum at this cutoff (1 free transfer);
* ``stale``    — the never-updated GW1 squad of the walk-forward replay (``hold`` strategy),
                 bought at GW1 prices (2 free transfers);
* ``active``   — the squad the ``engine_no_chips`` replay held entering this gameweek
                 (purchase price ≈ current price; 1 free transfer);
* ``random``   — a random legal £100.0m squad (5 free transfers: many moves are worthwhile).

Per state: transfer plans for horizons 1/3/5/8 (pooled candidates); chips open (horizon 5);
HOLD + 3 alternatives (horizon 5); pooled vs full player universe (objective cost of pruning);
a forced 1-second time limit on the hardest problem (8 GWs, chips open) to check that timed-out
incumbents are valid; two repeated solves (determinism); three solves on perturbed forecasts
(stability of the first-week action); and a greedy one-transfer heuristic as the simpler
baseline. Every returned plan is replayed through the independent validator (domain state
machine).

Usage:
  uv run python ml/experiments/optimizer_benchmark.py                 # all seasons, one process
  uv run python ml/experiments/optimizer_benchmark.py --season 2024-25 # one season's part
  uv run python ml/experiments/optimizer_benchmark.py --merge          # report from the parts
Parts are written to data/eval/optimizer_benchmark_<season>.json; running the three seasons as
parallel processes is recorded in the report (each solve is single-threaded).
Inputs: the pinned snapshot (config/backtest/default.yaml) and the replay records in data/eval/.
Outputs: ml/reports/optimizer_benchmark.{json,md}. Timings are specific to the machine recorded
in the report.
"""

from __future__ import annotations

import json
import os
import platform
import random
import subprocess
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import highspy
import numpy as np
import pandas as pd

from fpl_decision.inputs import player_table
from fpl_domain.config import load_versioned_config
from fpl_domain.enums import POSITIONS
from fpl_domain.rules import load_ruleset
from fpl_domain.squad import SquadPick
from fpl_domain.state import ManagerState, initial_chips
from fpl_forecasting.pipeline import forecast, train_forecast_models
from fpl_forecasting.walkforward import FeatureCache, cutoffs
from fpl_optimizer.alternatives import solve_with_alternatives
from fpl_optimizer.milp import OptimizationError, Solution, build_and_solve, solve_with_chips
from fpl_optimizer.pool import candidate_pool
from fpl_optimizer.problem import (
    OptimizationProblem,
    OptimizerConfig,
    PlayerTable,
    Preferences,
    load_optimizer_config,
)
from fpl_optimizer.validate import validate_solution
from fpl_simulation.engine import SimulationConfig
from fpl_storage.dataset import load_snapshot
from fpl_storage.pit import PointInTimeView

ROOT = Path(__file__).resolve().parents[2]
REP = ROOT / "ml" / "reports"
SEASONS = ("2023-24", "2024-25", "2025-26")
GWS = (6, 16, 26, 33)
MAX_H = 8
HORIZONS = (1, 3, 5, 8)
TIMEOUT_S = 1.0
PERTURB_SD = 0.05


class Bench:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def solve(
        self, kind: str, prob: OptimizationProblem, ctx: dict[str, Any], fn: Any = None
    ) -> Solution | None:
        t0 = time.perf_counter()
        try:
            sol = (fn or build_and_solve)(prob)
        except OptimizationError as exc:
            self.rows.append(
                {
                    **ctx,
                    "kind": kind,
                    "horizon": len(prob.gameweeks),
                    "seconds": time.perf_counter() - t0,
                    "status": f"error: {exc}",
                    "valid": False,
                }
            )
            return None
        wall = time.perf_counter() - t0
        v = validate_solution(prob, sol)
        self.rows.append(
            {
                **ctx,
                "kind": kind,
                "horizon": len(prob.gameweeks),
                "seconds": wall,
                "solver_seconds": float(sol.stats.get("solve_seconds", float("nan"))),
                "status": sol.status,
                "mip_gap": float(sol.stats.get("mip_gap", float("nan"))),
                "nodes": int(sol.stats.get("mip_node_count", 0)),
                "n_vars": int(sol.stats.get("n_vars", 0)),
                "candidates": int(prob.players.n),
                "objective": float(sol.objective),
                "valid": bool(v.valid),
                "issues": list(v.issues)[:3],
                "first_out": sorted(int(c) for c in sol.plans[0].transfers_out),
                "first_in": sorted(int(c) for c in sol.plans[0].transfers_in),
                "chips_used": [p.chip_id for p in sol.plans if p.chip_id],
            }
        )
        return sol


def _h(table: PlayerTable, h: int) -> PlayerTable:
    return replace(
        table, ev=table.ev[:, :h].copy(), q10=None if table.q10 is None else table.q10[:, :h].copy()
    )


def _picks(codes: list[int], table: PlayerTable, prices: dict[int, int]) -> tuple[SquadPick, ...]:
    idx, pos = table.index(), table.positions_map()
    return tuple(
        SquadPick(
            player_code=c,
            position=pos[c],
            team_code=int(table.team[idx[c]]),
            purchase_price=int(prices.get(c, table.price[idx[c]])),
        )
        for c in codes
    )


def _state(season: str, gw: int, picks: tuple[SquadPick, ...], bank: int, ft: int) -> ManagerState:
    rs = load_ruleset(season)
    return ManagerState(
        season=season,
        gameweek=gw,
        squad=picks,
        bank=bank,
        free_transfers=ft,
        chips=initial_chips(rs),
    )


def _random_squad(table: PlayerTable, rng: random.Random, budget: int = 1000) -> list[int]:
    quota = load_ruleset("2025-26").squad.positions
    pos = table.positions_map()
    by = {p: [int(c) for c in table.code if pos[int(c)] is p] for p in POSITIONS}
    idx = table.index()
    while True:
        chosen: list[int] = []
        clubs: Counter[int] = Counter()
        for p in POSITIONS:
            cands = list(by[p])
            rng.shuffle(cands)
            for c in cands:
                if sum(1 for x in chosen if pos[x] is p) == quota.get(p):
                    break
                t = int(table.team[idx[c]])
                if clubs[t] < 3:
                    chosen.append(c)
                    clubs[t] += 1
        if len(chosen) == 15 and sum(int(table.price[idx[c]]) for c in chosen) <= budget:
            return chosen


def _replay_squad(season: str, gw: int, strategy: str) -> list[int] | None:
    f = ROOT / "data" / "eval" / f"backtest_{season}.parquet"
    if not f.exists():
        return None
    df = pd.read_parquet(f)
    r = df[(df["strategy"] == strategy) & (df["gw"] == gw)].iloc[0]
    squad = {int(c) for c in [*r["starters"], *r["bench"]]}
    squad = (squad - {int(c) for c in r["transfers_in"]}) | {int(c) for c in r["transfers_out"]}
    return sorted(squad)


def _greedy(prob: OptimizationProblem, cfg: OptimizerConfig) -> tuple[int, int] | None:
    """Simpler heuristic: the single affordable, club-legal same-position swap with the largest
    discounted-horizon expected-points gain (or no transfer)."""
    t, st = prob.players, prob.state
    disc = np.array([cfg.objective.discount**k for k in range(t.horizon)])
    score = t.ev @ disc
    idx, pos = t.index(), t.positions_map()
    prices = {int(c): int(p) for c, p in zip(t.code, t.price, strict=True)}
    sell = st.selling_values(prices, prob.ruleset)
    clubs = Counter(int(t.team[idx[p.player_code]]) for p in st.squad)
    owned = set(st.codes)
    best, gain = None, 0.0
    for p in st.squad:
        o = p.player_code
        for raw in t.code:
            c = int(raw)
            if c in owned or pos[c] is not pos[o] or prices[c] > st.bank + sell[o]:
                continue
            tc = int(t.team[idx[c]])
            if tc != int(t.team[idx[o]]) and clubs[tc] >= 3:
                continue
            g = float(score[idx[c]] - score[idx[o]])
            if g > gain:
                best, gain = (o, c), g
    return best


def main(seasons: tuple[str, ...] = SEASONS, part: bool = False) -> None:
    bt = load_versioned_config("backtest", "default").data
    ds = load_snapshot(ROOT / "data" / "snapshots" / str(bt["snapshot_id"]))
    cfg = load_optimizer_config("default")
    bench = Bench()
    forecasts: list[dict[str, Any]] = []
    rng = random.Random(20261005)  # noqa: S311 — reproducible workload sampling, not security
    hist = cutoffs(ds, ["2022-23", *SEASONS])
    cache = FeatureCache(ds, MAX_H, ROOT / "data" / "feature-store")
    t_all = time.time()
    cpu0 = time.process_time()
    for season in seasons:
        rs = load_ruleset(season)
        gw1 = next(c for c in hist if c.season == season and c.gw == 1)
        pool1 = PointInTimeView(ds, gw1.cutoff).player_pool(season, 1)
        gw1_prices = {
            int(c): int(p)
            for c, p in zip(pool1["player_code"], pool1["price"], strict=True)
            if pd.notna(p)
        }
        for gw in GWS:
            cut = next(c for c in hist if c.season == season and c.gw == gw)
            history = [c for c in hist if c.cutoff <= cut.cutoff]
            t0 = time.time()
            models = train_forecast_models(cache, cut, history)
            fc = forecast(cache, cut, models, SimulationConfig(n_sims=1000))
            forecasts.append({"season": season, "gw": gw, "seconds": time.time() - t0})
            gws = tuple(g for g in range(gw, gw + MAX_H) if g <= 38)
            pool_df = PointInTimeView(ds, cut.cutoff).player_pool(season, gw)
            table = player_table(fc.summary, pool_df, gws)
            prices = {int(c): int(p) for c, p in zip(table.code, table.price, strict=True)}
            init = OptimizationProblem(
                state=_state(season, gw, (), 1000, 0),
                ruleset=rs,
                players=_h(table, 1),
                gameweeks=gws[:1],
                config=cfg,
                initial_squad_mode=True,
            )
            isol = bench.solve("initial_squad", init, {"season": season, "gw": gw, "squad": "-"})
            assert isol is not None
            states: dict[str, ManagerState] = {
                "optimal": _state(
                    season,
                    gw,
                    _picks(list(isol.plans[0].squad), table, prices),
                    isol.plans[0].bank_after,
                    1,
                )
            }
            for name, strat, ft, pp in (
                ("stale", "hold", 2, gw1_prices),
                ("active", "engine_no_chips", 1, prices),
            ):
                codes = _replay_squad(season, gw, strat)
                if codes is None or any(c not in prices for c in codes):
                    continue  # a player left the league: that squad would have been changed
                picks = _picks(codes, table, pp)
                value = sum(p.purchase_price for p in picks)
                states[name] = _state(season, gw, picks, max(0, 1000 - value), ft)
            rnd = _random_squad(table, rng)
            states["random"] = _state(
                season, gw, _picks(rnd, table, prices), 1000 - sum(prices[c] for c in rnd), 5
            )
            for sname, st in states.items():
                ctx = {"season": season, "gw": gw, "squad": sname}
                full = _h(table, len(gws))
                pooled = candidate_pool(full, st.codes, cfg.pool, discount=cfg.objective.discount)
                for h in HORIZONS:
                    p = OptimizationProblem(
                        state=st, ruleset=rs, players=_h(pooled, h), gameweeks=gws[:h], config=cfg
                    )
                    bench.solve("transfers", p, ctx)
                p5 = OptimizationProblem(
                    state=st, ruleset=rs, players=_h(pooled, 5), gameweeks=gws[:5], config=cfg
                )
                usable = {c.chip_id: tuple(g for g in gws[:5] if c.usable_in(g)) for c in st.chips}
                usable = {k: v for k, v in usable.items() if v}
                bench.solve("chips_open", replace(p5, chip_options=usable), ctx, solve_with_chips)
                ta = time.perf_counter()
                opts = solve_with_alternatives(p5, n_alternatives=3)
                bench.rows.append(
                    {
                        **ctx,
                        "kind": "hold_plus_3_alternatives",
                        "horizon": 5,
                        "seconds": time.perf_counter() - ta,
                        "status": f"{len(opts)} options",
                        "valid": all(o.validation.valid for o in opts),
                    }
                )
                if sname in ("active", "random"):
                    bench.solve("full_universe", replace(p5, players=_h(full, 5)), ctx)
                tight = cfg.model_copy(
                    update={
                        "solver": cfg.solver.model_copy(update={"time_limit_seconds": TIMEOUT_S})
                    }
                )
                p8c = OptimizationProblem(
                    state=st,
                    ruleset=rs,
                    players=pooled,
                    gameweeks=gws,
                    config=tight,
                    chip_options={
                        k: tuple(g for g in gws if st.chip(k).usable_in(g)) for k in usable
                    },
                )
                bench.solve("forced_timeout", p8c, ctx)
                for _ in range(2):
                    bench.solve("repeat", p5, ctx)
                noise = np.random.default_rng(len(bench.rows))
                for _ in range(3):
                    mult = np.exp(noise.normal(0.0, PERTURB_SD, p5.players.ev.shape[0]))[:, None]
                    pert = replace(p5, players=replace(p5.players, ev=p5.players.ev * mult))
                    bench.solve("perturbed", pert, ctx)
                move = _greedy(p5, cfg)
                pref = (
                    Preferences(hold_first_gw=True)
                    if move is None
                    else Preferences(
                        forced_out=frozenset({move[0]}),
                        forced_in=frozenset({move[1]}),
                        max_transfers_per_gw=1,
                    )
                )
                bench.solve("greedy_heuristic", replace(p5, preferences=pref), ctx)
                print(f"[{time.time() - t_all:6.0f}s] {season} GW{gw} {sname}", flush=True)
    run = {
        "seasons": list(seasons),
        "wall_seconds": time.time() - t_all,
        "cpu_seconds": time.process_time() - cpu0,
        "power": _power(),
    }
    if part:
        out = ROOT / "data" / "eval" / f"optimizer_benchmark_{seasons[0]}.json"
        out.write_text(
            json.dumps({"rows": bench.rows, "forecasts": forecasts, "run": run}, default=str)
        )
        return
    write(bench.rows, forecasts, cfg, ds.snapshot_id, run["wall_seconds"], [run])


def _power() -> str:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, check=False)  # noqa: S607
    except OSError:
        return "unknown"
    return (
        "AC" if "AC Power" in out.stdout else ("battery" if "Battery" in out.stdout else "unknown")
    )


def merge() -> None:
    parts = [
        json.loads((ROOT / "data" / "eval" / f"optimizer_benchmark_{s}.json").read_text())
        for s in SEASONS
    ]
    bt = load_versioned_config("backtest", "default").data
    cfg = load_optimizer_config("default")
    rows = [r for p in parts for r in p["rows"]]
    forecasts = [f for p in parts for f in p["forecasts"]]
    runs = [p["run"] for p in parts]
    write(rows, forecasts, cfg, str(bt["snapshot_id"]), max(r["wall_seconds"] for r in runs), runs)


def _machine() -> dict[str, Any]:
    def sh(cmd: list[str]) -> str:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
            return out.stdout.strip()
        except OSError:
            return ""

    mem = sh(["sysctl", "-n", "hw.memsize"])
    return {
        "platform": platform.platform(),
        "cpu": sh(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor(),
        "logical_cpus": os.cpu_count(),
        "memory_gb": round(int(mem) / 2**30, 1) if mem.isdigit() else None,
        "python": platform.python_version(),
        "highs": highspy.Highs().version(),
    }


def pct(xs: pd.Series) -> dict[str, float]:
    a = xs.dropna().to_numpy(float)
    if not len(a):
        return {"n": 0}
    return {
        "n": len(a),
        "p50": float(np.percentile(a, 50)),
        "p95": float(np.percentile(a, 95)),
        "max": float(a.max()),
        "mean": float(a.mean()),
    }


def write(
    rows: list[dict[str, Any]],
    forecasts: list[dict[str, Any]],
    cfg: OptimizerConfig,
    snapshot: str,
    total_s: float,
    runs: list[dict[str, Any]] | None = None,
) -> None:
    df = pd.DataFrame(rows)
    key = ["season", "gw", "squad"]
    tr = df[df["kind"] == "transfers"]
    timing = []
    for (kind, h), g in df.groupby(["kind", df["horizon"].fillna(0).astype(int)]):
        timing.append(
            {
                "kind": kind,
                "horizon": int(h),
                **pct(g["seconds"]),
                "time_limit_hits": int(g["status"].astype(str).str.contains("Time limit").sum()),
                # validity is a property of a returned plan; solves without one count separately
                "no_plan": int(g["status"].astype(str).str.startswith("error:").sum()),
                "valid_rate": float(
                    g.loc[~g["status"].astype(str).str.startswith("error:"), "valid"]
                    .astype(bool)
                    .mean()
                ),
            }
        )
    t5 = tr[tr["horizon"] == 5].set_index(key)["objective"]
    full = df[df["kind"] == "full_universe"].set_index(key)["objective"]
    pool_gap = (full - t5.reindex(full.index)).dropna()
    greedy = df[df["kind"] == "greedy_heuristic"].set_index(key)["objective"]
    milp_vs_greedy = (t5 - greedy.reindex(t5.index)).dropna()
    chips = df[df["kind"] == "chips_open"].set_index(key)["objective"]
    chip_gain = (chips - t5.reindex(chips.index)).dropna()
    to = df[df["kind"] == "forced_timeout"]
    base = tr[tr["horizon"] == 5].set_index(key)
    det: list[bool] = []
    stab: list[bool] = []
    for k, g in df[df["kind"] == "repeat"].groupby(key):
        b = base.loc[k]
        det.append(
            all(
                r.first_in == b["first_in"]
                and r.first_out == b["first_out"]
                and abs(r.objective - b["objective"]) < 1e-6
                for r in g.itertuples()
            )
        )
    for k, g in df[df["kind"] == "perturbed"].groupby(key):
        b = base.loc[k]
        stab.extend(
            r.first_in == b["first_in"] and r.first_out == b["first_out"] for r in g.itertuples()
        )
    no_inc = df["status"].astype(str).str.startswith("error:")
    returned = df[~no_inc]
    payload = {
        "snapshot_id": snapshot,
        "optimizer_config": cfg.config_ref,
        "solver": cfg.solver.model_dump(),
        "machine": _machine(),
        "total_seconds": total_s,
        "runs": runs or [],
        "states": int(df[df["squad"] != "-"].drop_duplicates(key).shape[0]),
        "solves": int((df["kind"] != "hold_plus_3_alternatives").sum()),
        "validated": len(returned),
        "valid_rate": float(returned["valid"].astype(bool).mean()),
        "no_incumbent": int(no_inc.sum()),
        "invalid": returned[~returned["valid"].astype(bool)][
            [*key, "kind", "status", "issues"]
        ].to_dict("records"),
        "timing": timing,
        "forecast_seconds": pct(pd.Series([f["seconds"] for f in forecasts])),
        "pool_gap_objective": pct(pool_gap),
        "pool_gap_zero_share": float((pool_gap.abs() < 1e-6).mean()) if len(pool_gap) else None,
        "milp_minus_greedy_objective": pct(milp_vs_greedy),
        "milp_beats_greedy_share": float((milp_vs_greedy > 1e-6).mean()),
        "chip_open_gain_objective": pct(chip_gain),
        "forced_timeout": {
            "runs": len(to),
            "time_limit_hits": int(to["status"].astype(str).str.contains("Time limit").sum()),
            "no_incumbent": int(to["status"].astype(str).str.startswith("error:").sum()),
            "valid_incumbents": int(to["valid"].astype(bool).sum()),
        },
        "deterministic_share": float(np.mean(det)) if det else None,
        "perturbation_same_first_action_share": float(np.mean(stab)) if stab else None,
        "transfers_by_squad": tr[tr["horizon"] == 5]
        .groupby("squad")["first_in"]
        .apply(lambda s: float(np.mean([len(x) for x in s])))
        .to_dict(),
        "rows": rows,
    }
    REP.mkdir(parents=True, exist_ok=True)
    (REP / "optimizer_benchmark.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")
    (REP / "optimizer_benchmark.md").write_text(markdown(payload) + "\n")
    print(markdown(payload))


def _fmt(d: dict[str, float], k: str, spec: str = ".2f") -> str:
    return "—" if not d or d.get("n", 0) == 0 else format(d[k], spec)


def markdown(p: dict[str, Any]) -> str:
    m, to = p["machine"], p["forced_timeout"]
    lines = [
        "# Optimiser benchmark — realistic workloads",
        "",
        f"Snapshot `{p['snapshot_id']}` · config `{p['optimizer_config']}` · HiGHS {m['highs']} "
        f"single-thread, `mip_rel_gap` {p['solver']['mip_rel_gap']}, time limit "
        f"{p['solver']['time_limit_seconds']} s · generated by "
        f"`ml/experiments/optimizer_benchmark.py` in {p['total_seconds'] / 60:.0f} min.",
        "",
        "Runs: "
        + "; ".join(
            f"{', '.join(r['seasons'])} — wall {r['wall_seconds'] / 60:.0f} min, CPU "
            f"{r['cpu_seconds'] / 60:.0f} min ({r['power']} power)"
            for r in p["runs"]
        )
        + (
            ". Seasons ran as parallel processes (single-threaded solver each)."
            if len(p["runs"]) > 1
            else "."
        ),
        "",
        f"Machine: {m['cpu']}, {m['logical_cpus']} logical CPUs, {m['memory_gb']} GB, "
        f"{m['platform']}, Python {m['python']}. Development-machine timings, not production "
        "guarantees: the solver is single-threaded, so per-solve times transfer roughly to one "
        "core of similar speed.",
        "",
        f"Workload: {p['states']} squad states (12 cutoffs × optimal / stale / active / random "
        f"squads), {p['solves']} MILP solves. Forecast per cutoff (train + 8-GW, 1,000-sample "
        f"simulation): p50 {_fmt(p['forecast_seconds'], 'p50', '.0f')} s, max "
        f"{_fmt(p['forecast_seconds'], 'max', '.0f')} s.",
        "",
        "## Validity",
        "",
        f"{p['valid_rate']:.2%} of returned plans pass independent validation (replay through "
        f"the domain state machine, legality *and* objective recomputation; {p['validated']} "
        f"checked). Solves that found no feasible plan within their time limit (no incumbent, "
        f"so nothing to validate; the decision engine then falls back to HOLD): "
        f"{p['no_incumbent']}.",
    ]
    lines += [f"* INVALID: {r}" for r in p["invalid"]]
    lines += [
        "",
        "## Solve time (wall clock, including model build and plan extraction)",
        "",
        "| kind | horizon | n | p50 s | p95 s | max s | time-limit hits | no plan "
        "| valid (of returned) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(p["timing"], key=lambda r: (r["kind"], r["horizon"])):
        lines.append(
            f"| {r['kind']} | {r['horizon'] or '—'} | {r['n']} | {r['p50']:.2f} | "
            f"{r['p95']:.2f} | {r['max']:.2f} | {r['time_limit_hits']} | {r.get('no_plan', 0)} | "
            f"{r['valid_rate']:.0%} |"
        )
    pool = (
        f"* Candidate pooling (default pool vs the full player universe, horizon 5): objective "
        f"lost p50 {_fmt(p['pool_gap_objective'], 'p50', '.3f')}, max "
        f"{_fmt(p['pool_gap_objective'], 'max', '.3f')} points; identical optimum in "
        f"{p['pool_gap_zero_share']:.0%} of states."
        if p["pool_gap_zero_share"] is not None
        else "* Pooling gap: not measured."
    )
    lines += [
        "",
        "## Objective quality",
        "",
        pool,
        f"* MILP vs the greedy one-transfer heuristic (horizon 5, same objective): MILP higher "
        f"by p50 {_fmt(p['milp_minus_greedy_objective'], 'p50')}, mean "
        f"{_fmt(p['milp_minus_greedy_objective'], 'mean')}, max "
        f"{_fmt(p['milp_minus_greedy_objective'], 'max')} objective points; strictly better in "
        f"{p['milp_beats_greedy_share']:.0%} of states (equal where at most one transfer is "
        "optimal).",
        f"* Opening all usable chips (horizon 5): objective gain p50 "
        f"{_fmt(p['chip_open_gain_objective'], 'p50')}, max "
        f"{_fmt(p['chip_open_gain_objective'], 'max')} (warm-started from the no-chip optimum, "
        "so never negative by construction).",
        f"* Forced {TIMEOUT_S:.0f}-second limit on 8-GW chip-open problems without a warm start "
        f"(harsher than production, which warm-starts chip problems from the no-chip optimum): "
        f"{to['time_limit_hits']} of {to['runs']} hit the limit; {to['no_incumbent']} found no "
        f"feasible plan in time; {to['valid_incumbents']} of the "
        f"{to['runs'] - to['no_incumbent']} returned incumbents were valid after polishing.",
        "",
        "## Determinism and stability",
        "",
        f"* Re-solving identical inputs reproduced the same first-week action and objective in "
        f"{p['deterministic_share']:.0%} of states (HiGHS single thread, fixed seed).",
        f"* With every player's forecast multiplied by lognormal noise (σ = {PERTURB_SD}), the "
        f"first-week action was unchanged in {p['perturbation_same_first_action_share']:.0%} of "
        "solves — a measure of how close competing moves are, not an error.",
        "* Mean first-week transfers at horizon 5 by squad type: "
        + ", ".join(f"{k} {v:.1f}" for k, v in p["transfers_by_squad"].items())
        + ".",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if "--merge" in sys.argv:
        merge()
    elif "--season" in sys.argv:
        main((sys.argv[sys.argv.index("--season") + 1],), part=True)
    else:
        main()
