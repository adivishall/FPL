"""Walk-forward backtest of the decision engine against benchmarks (§27, §28, §70).

Usage:
  uv run python ml/experiments/backtest.py --run-only 2023-24      # one season (resumable)
  uv run python ml/experiments/backtest.py 2023-24 2024-25 2025-26 # run, then report
  uv run python ml/experiments/backtest.py --report-only           # rebuild report

Parameters (pinned snapshot, horizon, retraining cadence, samples) come from
config/backtest/default.yaml. Per season, every strategy starts from the same GW1 squad and is
replayed through all 38 gameweeks with the protocol in docs/BACKTEST_PROTOCOL.md. Progress is
checkpointed after every gameweek in data/eval/checkpoints/<season>/ (an interrupted run resumes
there); finished records go to data/eval/backtest_<season>.parquet and the forecasts used at each
cutoff to data/eval/forecasts_<season>.parquet; the report to ml/reports/backtest.{md,json} and
ml/reports/figures/.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

from fpl_backtest.runner import default_strategies, run_season
from fpl_domain.config import load_versioned_config
from fpl_optimizer.problem import load_optimizer_config
from fpl_simulation.engine import SimulationConfig
from fpl_storage.dataset import load_snapshot
from fpl_storage.schedule import reconstruct

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "eval"
REP = ROOT / "ml" / "reports"
ENGINE = "engine"
BT = load_versioned_config("backtest", "default")
# planning horizon (gameweeks) of each strategy's optimiser; hold never optimises transfers
PLAN_HORIZON = {
    "engine": int(BT.data["horizon"]),
    "engine_no_chips": int(BT.data["horizon"]),
    "single_gw_mc": 1,
    "simple_xp": 1,
    "form": 1,
    "fpl_style_heuristic": 1,
}


def _dataset():  # type: ignore[no-untyped-def]
    snap = ROOT / "data" / "snapshots" / str(BT.data["snapshot_id"])
    if not snap.exists():
        raise SystemExit(
            f"pinned snapshot {snap.name} not found: run `fpl-ingest historical --all` and "
            "`fpl-ingest export-snapshot` (config/backtest/default.yaml pins the snapshot)"
        )
    ds = load_snapshot(snap)  # verifies every table against the manifest hash
    assert ds.snapshot_id == BT.data["snapshot_id"], ds.snapshot_id
    return ds


def schedule_audit(ds) -> list[dict]:  # type: ignore[no-untyped-def,type-arg]
    """Schedule rule S1 for every season a backtest reads: moved fixtures, original rounds and
    when each move is treated as known. Refuses to proceed unless each is proven unique."""
    rows = []
    for season in BT.data["history_seasons"]:
        fx = ds["fixtures"]
        rec = reconstruct(season, fx[fx["season"] == season], ds["gameweeks"])
        if rec.status != "reconstructed":
            raise SystemExit(f"{season}: schedule reconstruction {rec.status}; refusing to run")
        teams = ds["teams"][ds["teams"]["season"] == season].set_index("team_code")["short_name"]
        f = fx[fx["season"] == season].set_index("fixture_id")
        for fid, mv in sorted(rec.moves.items(), key=lambda kv: (kv[1].original_gw, kv[0])):
            rows.append(
                {
                    "season": season,
                    "fixture": f"{teams[f.loc[fid, 'home_team_code']]} v "
                    f"{teams[f.loc[fid, 'away_team_code']]}",
                    "original_gw": mv.original_gw,
                    "final_gw": mv.final_gw,
                    "known_at": mv.known_at.isoformat(),
                    "announced_at": mv.announced_at.isoformat(),
                }
            )
    return rows


def run(seasons: list[str]) -> None:
    ds = _dataset()
    schedule_audit(ds)
    p = BT.data
    cfg = load_optimizer_config(str(p["optimizer_profile"]))
    OUT.mkdir(parents=True, exist_ok=True)
    for season in seasons:
        hist = [s for s in p["history_seasons"] if s < season]
        t0 = time.time()
        ckpt = OUT / "checkpoints" / season
        df = run_season(
            ds,
            season,
            default_strategies(cfg, int(p["horizon"])),
            hist,
            horizon=int(p["horizon"]),
            retrain_every=int(p["retrain_every"]),
            sim=SimulationConfig(n_sims=int(p["n_sims"])),
            cache_root=ROOT / "data" / "feature-store",
            progress=lambda m, t0=t0: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True),
            initial_cfg=cfg,
            checkpoint_dir=ckpt,
        )
        if sorted(df["gw"].unique()) != list(range(1, 39)):
            raise SystemExit(f"{season}: incomplete replay {sorted(df['gw'].unique())}")
        df["snapshot_id"] = ds.snapshot_id
        df["optimizer_config"] = cfg.config_ref
        df["backtest_config"] = BT.ref
        df.to_parquet(OUT / f"backtest_{season}.parquet", index=False)
        fc = pd.concat(
            [pd.read_parquet(f) for f in sorted((ckpt / "forecasts").glob("gw*.parquet"))],
            ignore_index=True,
        )
        fc.to_parquet(OUT / f"forecasts_{season}.parquet", index=False)
        print(f"{season}: finished in {time.time() - t0:.0f}s", flush=True)


def _club_moves(ds, season: str, rec: pd.DataFrame, gw: int) -> list[str]:  # type: ignore[no-untyped-def]
    """Owned players entering ``gw`` whose club at its cutoff differs from their first club of
    the season (mid-season moves change the club-limit arithmetic)."""
    from fpl_forecasting.walkforward import cutoffs  # noqa: PLC0415
    from fpl_storage.pit import PointInTimeView  # noqa: PLC0415

    prev = rec[rec["gw"] == gw - 1]
    if prev.empty:
        return []
    r = prev.iloc[0]
    squad = {int(c) for c in [*r["starters"], *r["bench"]]}
    cut = next(c.cutoff for c in cutoffs(ds, [season]) if c.gw == gw)
    pool = PointInTimeView(ds, cut).player_pool(season, gw).set_index("player_code")["team_code"]
    pm = ds["player_match"]
    pm = pm[pm["season"] == season].sort_values("kickoff_at")
    first = pm.groupby("player_code")["team_code"].first()
    reg = ds["players"]
    names = reg[reg["season"] == season].set_index("player_code")["web_name"]
    return [
        str(names.get(c, c))
        for c in sorted(squad)
        if c in pool.index and c in first.index and int(pool[c]) != int(first[c])
    ]


def _reproducibility(baseline: Path, ds) -> dict | None:  # type: ignore[no-untyped-def,type-arg]
    """Compare these records with an earlier run's (decision columns and points)."""
    cols = [
        "transfers_out",
        "transfers_in",
        "starters",
        "bench",
        "captain",
        "vice_captain",
        "chip",
        "points",
    ]
    rows = []
    for f in sorted(OUT.glob("backtest_*.parquet")):
        old_f = baseline / f.name
        if not old_f.exists():
            continue
        new, old = pd.read_parquet(f), pd.read_parquet(old_f)
        for strat in sorted(new["strategy"].unique()):
            a = new[new["strategy"] == strat].sort_values("gw").reset_index(drop=True)
            b = old[old["strategy"] == strat].sort_values("gw").reset_index(drop=True)
            same = [
                all(
                    str(list(x)) == str(list(y))
                    if hasattr(x, "__len__") and not isinstance(x, str)
                    else (x == y or (pd.isna(x) and pd.isna(y)))
                    for x, y in zip(a.loc[i, cols], b.loc[i, cols], strict=True)
                )
                for i in range(len(a))
            ]
            first = next((int(a.loc[i, "gw"]) for i, ok in enumerate(same) if not ok), None)
            season = f.stem.split("_")[1]
            status = "identical in all 38 gameweeks"
            if first is not None:
                moved = _club_moves(ds, season, a, first)
                reach = first + PLAN_HORIZON.get(strat, 0) - 1 >= 38
                causes = []
                if moved:
                    causes.append(f"owned player(s) who had changed club: {', '.join(moved)}")
                if reach:
                    causes.append("the plan horizon reaches GW38 (season-end valuation fix)")
                why = (
                    "; " + "; ".join(causes) + " — rules changed between the runs"
                    if causes
                    else "; no rule change applies here — unexplained"
                )
                status = (
                    f"diverges from GW{first} (points {int(a['points'].sum())} vs "
                    f"{int(b['points'].sum())}){why}"
                )
            rows.append({"season": season, "strategy": strat, "status": status})
    if not rows:
        return None
    n_same = sum(r["status"].startswith("identical") for r in rows)
    return {
        "baseline": str(baseline.relative_to(ROOT)),
        "by_season_strategy": rows,
        "summary": f"Re-running the replays from scratch reproduced {n_same} of {len(rows)} "
        f"strategy-seasons decision-for-decision against `{baseline.relative_to(ROOT)}` "
        "(an earlier full run of the same pinned inputs; differences are listed with their first "
        "gameweek and cause).",
    }


def _runtime() -> str | None:
    lines = []
    for season in BT.data["seasons"]:
        log = ROOT / "data" / "logs" / f"backtest_{season}.log"
        if log.exists():
            done = [x for x in log.read_text().splitlines() if "finished in" in x]
            if done:
                lines.append(done[-1].strip())
    if not lines:
        return None
    return (
        "Wall time per season (three seasons run as parallel processes on the machine in "
        "`ml/reports/performance.md`): " + "; ".join(lines) + "."
    )


def report(compare_with: Path | None = None) -> None:
    import backtest_report  # noqa: PLC0415 — sibling module of this script

    from fpl_features.registry import FEATURE_VERSION  # noqa: PLC0415

    files = sorted(OUT.glob("backtest_*.parquet"))
    if not files:
        raise SystemExit("no backtest records")
    ds = _dataset()
    extra = {"feature_version": FEATURE_VERSION}
    if compare_with is not None and (rp := _reproducibility(compare_with, ds)) is not None:
        extra["reproducibility"] = rp
    if (rt := _runtime()) is not None:
        extra["runtime"] = rt
    b = BT.data["bootstrap"]
    backtest_report.build(
        OUT, REP, ds, schedule_audit(ds), BT.ref, int(b["n_boot"]), int(b["seed"]), extra
    )
    print((REP / "backtest.md").read_text()[:4000])


if __name__ == "__main__":
    argv = sys.argv[1:]
    cmp_dir = None
    if "--compare-with" in argv:
        i = argv.index("--compare-with")
        cmp_dir = (ROOT / argv[i + 1]).resolve()
        argv = argv[:i] + argv[i + 2 :]
    args = [a for a in argv if not a.startswith("--")]
    if "--report-only" not in argv:
        run(args or list(BT.data["seasons"]))
    if "--run-only" not in argv:
        report(cmp_dir)
