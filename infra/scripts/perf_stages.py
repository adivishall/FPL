"""Stage timings of the heavy decision paths, and what parallel solves change (§80).

On one canonical snapshot and a deterministic squad (the initial-squad optimum, as in
``perf_probe.py``) this measures:

1. the cold forecast by phase — feature building, model training, forecast + Monte Carlo;
2. the replacement picker (one player, 5 GW) and the full recommendation (5 GW, default profile:
   alternatives, stability, scenarios, chips) with one solver process and with ``--workers``,
   the chip planner split into chip-week solves and fixture-shock re-simulations, and whether
   both runs produced identical output.

Caches go to a temporary directory; the snapshot is only read. Prints Markdown. The numbers are
specific to the machine or container they were measured on.

Usage (inside the deployment's worker image, e.g. 4 vCPUs; run the script from a file — worker
processes are spawned and re-import ``__main__``, which ``python -`` on stdin cannot provide):
  docker compose run --rm -T --no-deps -v "$PWD/infra/scripts:/scripts:ro" worker \
      python /scripts/perf_stages.py --snapshot /data/snapshots/<snapshot id> --workers 4 \
      > ml/reports/performance_stages.md
"""

from __future__ import annotations

import argparse
import os
import platform
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fpl_api.container import AppServices, build_recommendation, optimization_problem
from fpl_api.services import history_cutoffs
from fpl_api.settings import Settings
from fpl_decision import chips
from fpl_decision.replacement import find_replacements
from fpl_domain.squad import SquadPick
from fpl_domain.state import ManagerState, initial_chips
from fpl_forecasting.model_config import points_spec
from fpl_forecasting.pipeline import SUPPORTED_HORIZON, forecast, train_forecast_models
from fpl_forecasting.walkforward import FeatureCache
from fpl_optimizer.milp import build_and_solve
from fpl_optimizer.problem import OptimizationProblem, Preferences, load_optimizer_config
from fpl_simulation.engine import SimulationConfig


def _timed(fn: Any) -> tuple[Any, float]:
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="perf-stages-"))
    horizon, n_sims = 5, 1000

    def services(workers: int) -> AppServices:
        return AppServices.build(
            Settings(
                snapshot_dir=args.snapshot,
                artifact_dir=tmp / "artifacts",
                feature_store_dir=tmp / "features",
                n_sims=n_sims,
                horizon_default=horizon,
                horizon_max=SUPPORTED_HORIZON,
                solver_workers=workers,
            )
        )

    svc = services(1)
    ctx = svc.context()
    # 1. cold forecast: total, then training alone (features now cached) and the simulation
    _, t_cold = _timed(lambda: svc.forecast_for(ctx, SUPPORTED_HORIZON))
    ds = svc.data.ds
    hist = history_cutoffs(ds, ctx.season)
    cut = next(c for c in hist if c.season == ctx.season and c.gw == ctx.gameweek)
    cache = FeatureCache(ds, SUPPORTED_HORIZON, tmp / "features")
    models, t_train = _timed(lambda: train_forecast_models(cache, cut, hist))
    seed = points_spec()[0].simulation.seed
    _, t_sim = _timed(
        lambda: forecast(cache, cut, models, SimulationConfig(n_sims=n_sims, seed=seed))
    )

    # a deterministic squad: the initial-squad optimum for the horizon
    fc = svc.forecast_for(ctx, horizon)
    rs = svc.data.ruleset(ctx.season)
    empty = ManagerState(
        season=ctx.season,
        gameweek=ctx.gameweek,
        squad=(),
        bank=1000,
        free_transfers=1,
        chips=initial_chips(rs),
    )
    table = svc.table(ctx, fc, horizon)
    first = build_and_solve(
        OptimizationProblem(
            state=empty,
            ruleset=rs,
            players=table,
            gameweeks=tuple(range(ctx.gameweek, ctx.gameweek + horizon)),
            config=load_optimizer_config("default"),
            initial_squad_mode=True,
        )
    ).plans[0]
    idx, pos = table.index(), table.positions_map()
    state = empty.model_copy(
        update={
            "squad": tuple(
                SquadPick(
                    player_code=c,
                    position=pos[c],
                    team_code=int(table.team[idx[c]]),
                    purchase_price=int(table.price[idx[c]]),
                )
                for c in first.squad
            ),
            "bank": first.bank_after,
        }
    )
    out_player = first.squad[5]

    # 2. decision paths, serial and parallel; the chip planner split by instrumentation
    split = {"solves": 0.0, "resims": 0.0, "n_resims": 0}
    map_ordered, perturb_forecast = chips.map_ordered, chips.perturb_forecast

    def timed_map(fn: Any, tasks: Any, workers: int) -> Any:
        r, t = _timed(lambda: map_ordered(fn, tasks, workers))
        split["solves"] += t
        return r

    def timed_perturb(*a: Any, **k: Any) -> Any:
        r, t = _timed(lambda: perturb_forecast(*a, **k))
        split["resims"] += t
        split["n_resims"] += 1
        return r

    chips.map_ordered, chips.perturb_forecast = timed_map, timed_perturb  # type: ignore[assignment]
    runs: dict[int, dict[str, Any]] = {}
    for workers in (1, args.workers):
        s = services(workers)
        f = s.forecast_for(ctx, horizon)
        prob = optimization_problem(s, ctx, f, state, horizon, "default", Preferences())
        prob = replace(prob, players=s.table(ctx, f, horizon, state))
        rep, t_rep = _timed(
            lambda prob=prob, f=f, workers=workers: find_replacements(
                prob, out_player, f.simulation, f.summary, n_return=5, workers=workers
            )
        )
        split.update(solves=0.0, resims=0.0, n_resims=0)
        (_, pkg), t_rec = _timed(
            lambda s=s: build_recommendation(
                s, "perf", state, None, profile="default", horizon=horizon, persist=False
            )
        )
        runs[workers] = {
            "replacement": t_rep,
            "recommendation": t_rec,
            "stages": dict(pkg.timings),
            "chip_solves": split["solves"],
            "chip_resims": split["resims"],
            "n_resims": split["n_resims"],
            "rep": [(c.in_code, c.objective_gain, c.gain_horizon) for c in rep.candidates],
            "pkg": pkg.model_dump(mode="json", exclude={"timings", "generated_at"}),
            "label": pkg.decision["action"],
            "stability": pkg.stability.label if pkg.stability else "-",
        }
    one, many = runs[1], runs[args.workers]
    same_rep, same_pkg = one["rep"] == many["rep"], one["pkg"] == many["pkg"]

    w = args.workers
    rows = [
        ("Replacement picker (1 player, 5 GW)", one["replacement"], many["replacement"]),
        (
            "Full recommendation (5 GW, default profile)",
            one["recommendation"],
            many["recommendation"],
        ),
        ("— optimise (plan + alternatives)", one["stages"]["optimise"], many["stages"]["optimise"]),
        (
            "— stability (perturbed re-optimisations)",
            one["stages"]["stability"],
            many["stages"]["stability"],
        ),
        ("— scenarios", one["stages"]["scenarios"], many["stages"]["scenarios"]),
        ("— chips", one["stages"]["chips"], many["stages"]["chips"]),
        ("    chip-week solves (Wildcard / Free Hit)", one["chip_solves"], many["chip_solves"]),
        (
            f"    fixture-shock re-simulations ({one['n_resims']})",
            one["chip_resims"],
            many["chip_resims"],
        ),
    ]
    lines = [
        "# Decision-path stage timings (measured)",
        "",
        "Generated by `infra/scripts/perf_stages.py` on "
        f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC. {platform.platform()}, {os.cpu_count()} "
        f"logical CPUs, Python {platform.python_version()}. Numbers are specific to this machine "
        "or container.",
        "",
        f"Snapshot `{ds.snapshot_id}` ({ctx.season} GW{ctx.gameweek}); squad: the initial-squad "
        f"optimum for {horizon} gameweeks; {n_sims:,} simulations.",
        "",
        f"## Cold forecast ({fc.summary['player_code'].nunique()} players × "
        f"{SUPPORTED_HORIZON} GW × "
        f"{n_sims:,} samples)",
        "",
        "| phase | seconds |",
        "|---|---|",
        f"| feature building ({len(hist)} training cutoffs) | {t_cold - t_train - t_sim:.1f} |",
        f"| model training | {t_train:.1f} |",
        f"| forecast + Monte Carlo | {t_sim:.1f} |",
        f"| **total** | **{t_cold:.1f}** |",
        "",
        "Production computes it in the worker before a snapshot is promoted; no request waits "
        "for it.",
        "",
        f"## Decision paths: one solver process vs {w}",
        "",
        "Independent MILP solves (stability perturbations, chip weeks, replacement candidates) "
        f"run in `{w}` worker processes (`FPL_SOLVER_WORKERS`). Decision: {one['label']}; "
        f"stability: {one['stability']}.",
        "",
        f"| path | 1 process (s) | {w} processes (s) |",
        "|---|---|---|",
        *[f"| {name} | {a:.1f} | {b:.1f} |" for name, a, b in rows],
        "",
        f"Identical output with 1 and {w} processes: replacement candidates **{same_rep}**, "
        f"recommendation package (all fields but timings and timestamp) **{same_pkg}**.",
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
