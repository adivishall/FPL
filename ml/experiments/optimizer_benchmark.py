"""Optimiser benchmark on real forecasts (§28 'optimiser sophistication', §80 performance).

At the 2025-26 GW20 decision cutoff: train the forecast models, forecast GW20–27 (horizon 8),
build a squad with the initial-squad optimiser, then measure for horizons 1, 3, 5, 8:

* solve time and status, pooled candidate set vs the full player universe, and the objective gap
  caused by pooling (pruning is a performance approximation — its cost must be measured);
* the same with all chip options open (Wildcard, Free Hit, Bench Boost, Triple Captain);
* HOLD + 3 alternatives (top-N) wall time;
* independent-validation pass rate over every solution produced.

Usage: uv run python ml/experiments/optimizer_benchmark.py [snapshot_dir]
Outputs: ml/reports/optimizer_benchmark.{json,md}
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

from fpl_decision.inputs import player_table
from fpl_domain.rules import load_ruleset
from fpl_domain.squad import SquadPick
from fpl_domain.state import ManagerState, initial_chips
from fpl_forecasting.pipeline import forecast, train_forecast_models
from fpl_forecasting.walkforward import FeatureCache, cutoffs
from fpl_optimizer.alternatives import solve_with_alternatives
from fpl_optimizer.milp import build_and_solve, solve_with_chips
from fpl_optimizer.pool import candidate_pool
from fpl_optimizer.problem import OptimizationProblem, load_optimizer_config
from fpl_optimizer.validate import validate_solution
from fpl_simulation.engine import SimulationConfig
from fpl_storage.dataset import load_snapshot
from fpl_storage.pit import PointInTimeView

ROOT = Path(__file__).resolve().parents[2]
SEASON, GW, MAX_H = "2025-26", 20, 8
CHIPS = ("wildcard_2", "free_hit_2", "bench_boost_2", "triple_captain_2")


def main() -> None:
    snap = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else sorted((ROOT / "data" / "snapshots").glob("snap_*"))[-1]
    )
    ds = load_snapshot(snap)
    rs = load_ruleset(SEASON)
    cfg = load_optimizer_config("default")
    hist = cutoffs(ds, ["2022-23", "2023-24", "2024-25", SEASON])
    cut = next(c for c in hist if c.season == SEASON and c.gw == GW)
    cache = FeatureCache(ds, MAX_H, ROOT / "data" / "feature-store")
    t0 = time.time()
    models = train_forecast_models(cache, cut, hist)
    fc = forecast(cache, cut, models, SimulationConfig(n_sims=1000))
    forecast_s = time.time() - t0
    gws = tuple(range(GW, GW + MAX_H))
    pool_df = PointInTimeView(ds, cut.cutoff).player_pool(SEASON, GW)
    table = player_table(fc.summary, pool_df, gws)
    empty = ManagerState(
        season=SEASON, gameweek=GW, squad=(), bank=1000, free_transfers=2, chips=initial_chips(rs)
    )
    init = OptimizationProblem(
        state=empty,
        ruleset=rs,
        players=_h(table, 1),
        gameweeks=gws[:1],
        config=cfg,
        initial_squad_mode=True,
    )
    t1 = time.time()
    init_sol = build_and_solve(init)
    init_s = time.time() - t1
    idx = table.index()
    pos = table.positions_map()
    picks = tuple(
        SquadPick(
            player_code=c,
            position=pos[c],
            team_code=int(table.team[idx[c]]),
            purchase_price=int(table.price[idx[c]]),
        )
        for c in init_sol.plans[0].squad
    )
    state = empty.model_copy(update={"squad": picks, "bank": init_sol.plans[0].bank_after})

    results = []
    n_valid = n_total = 0
    for h in (1, 3, 5, 8):
        full_t = _h(table, h)
        base = OptimizationProblem(
            state=state, ruleset=rs, players=full_t, gameweeks=gws[:h], config=cfg
        )
        pooled_t = candidate_pool(full_t, state.codes, cfg.pool, discount=cfg.objective.discount)
        for chips in (False, True):
            opts = dict.fromkeys(CHIPS, gws[:h]) if chips else {}
            row = {"horizon": h, "chips": chips, "n_full": full_t.n, "n_pool": pooled_t.n}
            for label, players in (("pool", pooled_t), ("full", full_t)):
                prob = replace(base, players=players, chip_options=opts)
                ts = time.time()
                sol = solve_with_chips(prob) if chips else build_and_solve(prob)
                row[f"{label}_seconds"] = round(time.time() - ts, 3)
                row[f"{label}_objective"] = sol.objective
                row[f"{label}_status"] = sol.status
                row[f"{label}_vars"] = sol.stats["n_vars"]
                row[f"{label}_mip_gap"] = sol.stats.get("mip_gap")
                v = validate_solution(prob, sol)
                n_total += 1
                n_valid += int(v.valid)
                if label == "pool":
                    row["first_gw_transfers"] = len(sol.plans[0].transfers_in)
                    row["chips_used"] = [p.chip_id for p in sol.plans if p.chip_id]
            row["pool_gap"] = row["full_objective"] - row["pool_objective"]
            results.append(row)
            print(row, flush=True)
    # forced single-chip solves (what the chip planner runs): one chip in one gameweek
    forced = []
    p5 = OptimizationProblem(
        state=state,
        ruleset=rs,
        players=candidate_pool(
            _h(table, 5), state.codes, cfg.pool, discount=cfg.objective.discount
        ),
        gameweeks=gws[:5],
        config=cfg,
    )
    base5 = build_and_solve(p5)
    for chip in CHIPS:
        for g in gws[:5]:
            pf = replace(p5, forced_chips={chip: g}, chip_options={chip: (g,)})
            ts = time.time()
            sol = build_and_solve(pf)
            v = validate_solution(pf, sol)
            n_total += 1
            n_valid += int(v.valid)
            forced.append(
                {
                    "chip": chip,
                    "gameweek": g,
                    "seconds": round(time.time() - ts, 3),
                    "status": sol.status,
                    "objective_gain": sol.objective - base5.objective,
                }
            )
    alt_prob = OptimizationProblem(
        state=state,
        ruleset=rs,
        players=candidate_pool(
            _h(table, 5), state.codes, cfg.pool, discount=cfg.objective.discount
        ),
        gameweeks=gws[:5],
        config=cfg,
    )
    ta = time.time()
    options = solve_with_alternatives(alt_prob, n_alternatives=3)
    alt_s = time.time() - ta
    n_total += len(options)
    n_valid += sum(o.validation.valid for o in options)

    payload = {
        "snapshot_id": ds.snapshot_id,
        "decision": f"{SEASON} GW{GW}",
        "optimizer_config": cfg.config_ref,
        "forecast_seconds": round(forecast_s, 1),
        "initial_squad_seconds": round(init_s, 3),
        "results": results,
        "alternatives": {
            "horizon": 5,
            "seconds": round(alt_s, 2),
            "options": [
                {
                    "label": o.label,
                    "gain_vs_hold": round(o.gain_vs_hold, 3),
                    "sells": sorted(o.sells),
                    "buys": sorted(o.buys),
                    "valid": o.validation.valid,
                }
                for o in options
            ],
        },
        "forced_chip_solves": forced,
        "validation_pass_rate": n_valid / n_total,
        "validated_solutions": n_total,
        "solver": cfg.solver.model_dump(),
    }
    rep = ROOT / "ml" / "reports"
    (rep / "optimizer_benchmark.json").write_text(json.dumps(payload, indent=2) + "\n")
    lines = [
        "# Optimiser benchmark (real forecasts)",
        "",
        f"Decision {SEASON} GW{GW}, snapshot `{ds.snapshot_id}`, config `{cfg.config_ref}`, "
        f"HiGHS single-thread, `mip_rel_gap` {cfg.solver.mip_rel_gap}. Squad built by the "
        f"initial-squad optimiser ({init_s:.2f} s); forecast (train + 1,000-sample simulation, "
        f"8 GWs) {forecast_s:.0f} s. Generated by `ml/experiments/optimizer_benchmark.py`.",
        "",
        "| horizon | chips open | pool size | pool solve (s) | pool status (MIP gap) | "
        "full universe | full solve (s) | full status (MIP gap) | objective gap (full − pool) | "
        "1st-GW transfers | chips used |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['horizon']} | {'yes' if r['chips'] else 'no'} | {r['n_pool']} | "
            f"{r['pool_seconds']:.2f} | {r['pool_status']} ({r['pool_mip_gap']:.4f}) | "
            f"{r['n_full']} | {r['full_seconds']:.2f} | {r['full_status']} "
            f"({r['full_mip_gap']:.4f}) | {r['pool_gap']:.4f} | {r['first_gw_transfers']} | "
            f"{', '.join(r['chips_used']) or '—'} |"
        )
    lines += [
        "",
        "Chip-open rows are warm-started from the no-chip optimum (`solve_with_chips`), so a "
        "time-limited incumbent is never worse than playing no chip; the decision engine values "
        "chips with *forced* single-chip solves instead:",
        "",
        "| chip | GW | solve (s) | status | objective gain vs no chip |",
        "|---|---|---|---|---|",
    ]
    for f in forced:
        lines.append(
            f"| {f['chip']} | {f['gameweek']} | {f['seconds']:.2f} | {f['status']} | "
            f"{f['objective_gain']:+.2f} |"
        )
    lines += [
        "",
        f"HOLD + 3 alternatives (horizon 5, pooled): {alt_s:.2f} s.",
        "",
        "| option | gain vs HOLD (objective) | sells | buys |",
        "|---|---|---|---|",
    ]
    for o in payload["alternatives"]["options"]:  # type: ignore[index]
        lines.append(f"| {o['label']} | {o['gain_vs_hold']:+.3f} | {o['sells']} | {o['buys']} |")
    lines += [
        "",
        f"Independent validation: {n_valid}/{n_total} solutions valid "
        f"({100 * n_valid / n_total:.1f} %).",
    ]
    (rep / "optimizer_benchmark.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def _h(table, h):  # type: ignore[no-untyped-def]
    """Restrict a player table to the first ``h`` gameweeks."""
    return replace(
        table, ev=table.ev[:, :h].copy(), q10=None if table.q10 is None else table.q10[:, :h].copy()
    )


if __name__ == "__main__":
    main()
