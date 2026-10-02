"""Walk-forward backtest of the decision engine against benchmarks (§27, §28, §70).

Usage:
  uv run python ml/experiments/backtest.py 2025-26 [2024-25 2023-24 …]   # run + report
  uv run python ml/experiments/backtest.py --report-only                  # rebuild report

Per season, every strategy starts from the same GW1 squad and is replayed through all 38
gameweeks with the protocol in docs/BACKTEST_PROTOCOL.md. Records go to
data/eval/backtest_<season>.parquet; the report to ml/reports/backtest.{md,json} and
ml/reports/figures/backtest_cumulative_<season>.svg.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from fpl_backtest.runner import default_strategies, run_season
from fpl_optimizer.problem import load_optimizer_config
from fpl_simulation.engine import SimulationConfig
from fpl_storage.dataset import load_snapshot

ROOT = Path(__file__).resolve().parents[2]
ALL_SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26"]
OUT = ROOT / "data" / "eval"
REP = ROOT / "ml" / "reports"
ENGINE = "engine"


def run(seasons: list[str]) -> None:
    ds = load_snapshot(sorted((ROOT / "data" / "snapshots").glob("snap_*"))[-1])
    cfg = load_optimizer_config("default")
    OUT.mkdir(parents=True, exist_ok=True)
    for season in seasons:
        hist = [s for s in ALL_SEASONS if s < season]
        t0 = time.time()
        df = run_season(
            ds,
            season,
            default_strategies(cfg),
            hist,
            horizon=5,
            retrain_every=4,
            sim=SimulationConfig(n_sims=1000),
            cache_root=ROOT / "data" / "feature-store",
            progress=lambda m, t0=t0: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True),
            initial_cfg=cfg,
        )
        df["snapshot_id"] = ds.snapshot_id
        df["optimizer_config"] = cfg.config_ref
        df.to_parquet(OUT / f"backtest_{season}.parquet", index=False)


def _realised_transfer_gain(df: pd.DataFrame, ds_points: pd.DataFrame, k: int) -> pd.DataFrame:
    """Hindsight diagnostic: actual points of players bought minus players sold over the next
    k gameweeks (including the transfer week), minus the hit paid that week."""
    pts = ds_points.set_index(["season", "gw", "player_code"])["points"]
    rows = []
    for r in df.itertuples():
        if not r.transfers_in or (r.chip or "").startswith(("wildcard", "free_hit")):
            continue
        gws = range(r.gw, min(r.gw + k, 39))
        got = sum(pts.get((r.season, g, c), 0) for g in gws for c in r.transfers_in)
        lost = sum(pts.get((r.season, g, c), 0) for g in gws for c in r.transfers_out)
        rows.append(
            {
                "strategy": r.strategy,
                "season": r.season,
                "gw": r.gw,
                "out": list(r.transfers_out),
                "in": list(r.transfers_in),
                "hit": r.hit_points,
                f"gain_{k}gw": got - lost - r.hit_points,
            }
        )
    return pd.DataFrame(rows)


def _paired_ci(diff_by_gw: np.ndarray, n_boot: int = 4000, seed: int = 0) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(diff_by_gw), (n_boot, len(diff_by_gw)))
    sums = diff_by_gw[idx].sum(axis=1)
    return float(np.quantile(sums, 0.025)), float(np.quantile(sums, 0.975))


def report() -> None:
    files = sorted(OUT.glob("backtest_*.parquet"))
    if not files:
        raise SystemExit("no backtest records")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    ds = load_snapshot(sorted((ROOT / "data" / "snapshots").glob("snap_*"))[-1])
    pm = ds["player_match"]
    ds_points = pm.groupby(["season", "gw", "player_code"], as_index=False)["points"].sum()
    seasons = sorted(df["season"].unique())
    df["lineup_regret"] = df["hindsight_lineup_points"] - df["raw_points"]
    summary = (
        df.groupby(["season", "strategy"])
        .agg(
            total_points=("points", "sum"),
            gameweeks=("gw", "count"),
            hits=("hit_points", "sum"),
            transfers=("transfers", "sum"),
            captain_points=("captain_points", "sum"),
            bench_points=("bench_points", "sum"),
            lineup_regret=("lineup_regret", "sum"),
            validity=("valid", "mean"),
            runtime_s=("runtime_s", "mean"),
            data_age_h=("data_age_hours", "mean"),
            chips=("chip", lambda c: ", ".join(f"{x}" for x in c.dropna())),
        )
        .reset_index()
    )
    comps = []
    for season in seasons:
        s = df[df["season"] == season].pivot(index="gw", columns="strategy", values="points")
        for other in s.columns:
            if other == ENGINE:
                continue
            d = (s[ENGINE] - s[other]).to_numpy(float)
            lo, hi = _paired_ci(d)
            comps.append(
                {
                    "season": season,
                    "vs": other,
                    "difference": float(d.sum()),
                    "ci_low": lo,
                    "ci_high": hi,
                    "gws_better": int((d > 0).sum()),
                    "gws_worse": int((d < 0).sum()),
                }
            )
    comp = pd.DataFrame(comps)
    tg1 = _realised_transfer_gain(df, ds_points, 1)
    tg4 = _realised_transfer_gain(df, ds_points, 4)
    eng = df[df["strategy"] == ENGINE].sort_values(["season", "gw"])
    churn = []
    for season, grp in eng.groupby("season"):
        g = grp.reset_index(drop=True)
        for i in range(len(g) - 1):
            plan = g.loc[i, "planned_next"]
            if plan is None:
                continue
            actual = [sorted(g.loc[i + 1, "transfers_out"]), sorted(g.loc[i + 1, "transfers_in"])]
            churn.append(
                {
                    "season": season,
                    "gw": int(g.loc[i + 1, "gw"]),
                    "changed": [sorted(plan[0]), sorted(plan[1])] != actual,
                }
            )
    churn_df = pd.DataFrame(churn)
    calib = eng.dropna(subset=["expected_points"])
    figs = REP / "figures"
    figs.mkdir(parents=True, exist_ok=True)
    for season in seasons:
        s = df[df["season"] == season].pivot(index="gw", columns="strategy", values="points")
        fig, ax = plt.subplots(figsize=(7, 4))
        for col in s.columns:
            ax.plot(s.index, s[col].cumsum(), label=col, lw=2 if col == ENGINE else 1)
        ax.set_xlabel("gameweek")
        ax.set_ylabel("cumulative points (after hits)")
        ax.set_title(f"Walk-forward backtest {season}", fontsize=10)
        ax.legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(figs / f"backtest_cumulative_{season}.svg")
        plt.close(fig)
    payload = {
        "snapshot_id": str(df["snapshot_id"].iloc[0]),
        "optimizer_config": str(df["optimizer_config"].iloc[0]),
        "seasons": seasons,
        "summary": summary.to_dict("records"),
        "engine_vs": comp.to_dict("records"),
        "transfers_1gw": tg1.to_dict("records"),
        "transfers_4gw": tg4.to_dict("records"),
        "plan_churn_rate": float(churn_df["changed"].mean()) if len(churn_df) else None,
        "engine_expected_vs_actual": {
            "mean_expected": float(calib["expected_points"].mean()) if len(calib) else None,
            "mean_actual": float(calib["raw_points"].mean()) if len(calib) else None,
            "corr": float(np.corrcoef(calib["expected_points"], calib["raw_points"])[0, 1])
            if len(calib) > 2
            else None,
        },
        "lookahead_violations": int((df["data_age_hours"].dropna() < 0).sum()),
        "validity_rate": float(df["valid"].mean()),
    }
    (REP / "backtest.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")
    lines = [
        "# Walk-forward backtest — decision engine vs benchmarks",
        "",
        f"Snapshot `{payload['snapshot_id']}` · optimizer `{payload['optimizer_config']}` · "
        f"seasons {', '.join(seasons)} · protocol `docs/BACKTEST_PROTOCOL.md` · generated by "
        "`ml/experiments/backtest.py`. All strategies start from the same GW1 squad; points are "
        "actual FPL points after hits (automatic substitutions and armband rules applied).",
        "",
        "Strategies: **engine** (decomposed MC forecast, 5-GW MILP, paired-gain thresholds vs "
        "HOLD, chip planner); **engine_no_chips** (same, chips never played); **single_gw_mc** "
        "(same forecast, 1-GW optimiser, no thresholds); **simple_xp** (ppg × availability "
        "forecast, 1-GW optimiser); **form** (recent-form forecast, 1-GW optimiser); "
        "**fpl_style_heuristic** (approximation of an official-style heuristic, 1-GW optimiser); "
        "**hold** (no transfers, XI/captain from the MC forecast). No overall-rank claims are "
        "made: the archive has no rank distribution.",
        "",
        "## Season totals",
        "",
        "| season | strategy | points | hits | transfers | captain pts | bench pts | lineup regret "
        "| valid | s/decision | chips |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in summary.sort_values(["season", "total_points"], ascending=[True, False]).itertuples():
        lines.append(
            f"| {r.season} | {r.strategy} | {r.total_points} | {r.hits} | {r.transfers} | "
            f"{r.captain_points} | {r.bench_points} | {r.lineup_regret} | {r.validity:.0%} | "
            f"{r.runtime_s:.1f} | {r.chips or '—'} |"
        )
    lines += [
        "",
        "## Engine minus benchmark (season points; 95 % bootstrap CI over gameweeks)",
        "",
        "| season | vs | difference | 95 % CI | GWs better | GWs worse |",
        "|---|---|---|---|---|---|",
    ]
    for r in comp.itertuples():
        lines.append(
            f"| {r.season} | {r.vs} | {r.difference:+.0f} | [{r.ci_low:+.0f}, "
            f"{r.ci_high:+.0f}] | {r.gws_better} | {r.gws_worse} |"
        )
    if len(tg4):
        e4 = tg4[tg4["strategy"] == ENGINE]
        e1 = tg1[tg1["strategy"] == ENGINE]
        lines += [
            "",
            "## Engine transfers — every one, successful and failed (hindsight diagnostic)",
            "",
            f"{len(e4)} transfer weeks; realised gain over 1 GW: mean "
            f"{e1['gain_1gw'].mean():+.2f}, positive in {(e1['gain_1gw'] > 0).mean():.0%}; over "
            f"4 GWs: mean {e4['gain_4gw'].mean():+.2f}, positive in "
            f"{(e4['gain_4gw'] > 0).mean():.0%}. Realised gain = actual points of players in − "
            "players out (− hit); it ignores lineup effects and is reported as a diagnostic only.",
            "",
            "| season | GW | out | in | hit | gain 1 GW | gain 4 GW |",
            "|---|---|---|---|---|---|---|",
        ]
        m = e4.merge(e1[["season", "gw", "gain_1gw"]], on=["season", "gw"], how="left")
        for r in m.itertuples():
            lines.append(
                f"| {r.season} | {r.gw} | {r.out} | {getattr(r, 'in')} | {r.hit} | "
                f"{r.gain_1gw:+d} | {r.gain_4gw:+d} |"
            )
    lines += [
        "",
        "## Diagnostics",
        "",
        f"* Plan churn: {payload['plan_churn_rate']:.0%} of the engine's planned next-week "
        "actions changed when the next week came (new information) — "
        "lower is more stable."
        if payload["plan_churn_rate"] is not None
        else "* Plan churn: n/a",
        f"* Engine expected vs actual squad points per GW: "
        f"{payload['engine_expected_vs_actual']['mean_expected']:.1f} vs "
        f"{payload['engine_expected_vs_actual']['mean_actual']:.1f} (corr "
        f"{payload['engine_expected_vs_actual']['corr']:.2f})."
        if payload["engine_expected_vs_actual"]["corr"] is not None
        else "",
        f"* Validity: {payload['validity_rate']:.1%} of decisions legal under the state machine; "
        f"look-ahead violations: {payload['lookahead_violations']} (data newer than the cutoff).",
        "",
        *[f"![{s}](figures/backtest_cumulative_{s}.svg)" for s in seasons],
        "",
    ]
    (REP / "backtest.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:60]))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--report-only" not in sys.argv:
        run(args or ["2025-26"])
    report()
