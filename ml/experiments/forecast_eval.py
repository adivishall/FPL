"""Walk-forward evaluation of the decomposed Monte Carlo forecaster (§57.2, §59, §70).

Window: every gameweek of 2023-24, 2024-25 and 2025-26 as a decision cutoff (the same 114
cutoffs as the baseline report); targets are the next 5 gameweeks; history from 2022-23 onward.
Models are retrained every 4 gameweeks and at each season start using only data available at
that cutoff. Compared against: the direct LightGBM regressor, the four point baselines, the
climatology distribution and the rate-based minutes baseline.

Usage: uv run python ml/experiments/forecast_eval.py [snapshot_dir] [--report-only]
       (--report-only rebuilds the report from data/eval/forecast_eval_rows.parquet)
Outputs: ml/reports/forecast_eval.{json,md}, ml/reports/figures/forecast_*.svg,
         data/eval/forecast_eval_rows.parquet (per-row predictions; not committed)
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
from pinned import pinned_snapshot

from fpl_forecasting import metrics as fm
from fpl_forecasting.forecast_eval import (
    ForecastEvalConfig,
    distribution_metrics,
    evaluate_forecasts,
    gate_metrics,
    minutes_metrics,
    paired_cutoff_bootstrap,
    point_metrics,
)
from fpl_forecasting.governance import evaluate_gates
from fpl_forecasting.model_config import minutes_spec, points_spec
from fpl_forecasting.walkforward import cutoffs
from fpl_storage.dataset import load_snapshot

ROOT = Path(__file__).resolve().parents[2]
EVAL_SEASONS = ["2023-24", "2024-25", "2025-26"]
HISTORY_SEASONS = ["2022-23", *EVAL_SEASONS]
# Outcomes of superseded gate versions on this same evaluation (never deleted).
GATE_HISTORY = [
    {
        "config": "models/points@1.0.0#10df3bc6",
        "passed": False,
        "failed_gates": {"interval_80_coverage": {"value": 0.9382, "target": "in [0.75, 0.9]"}},
        "diagnosis": "inclusive [p10, p90] over-covers integer outcomes (40% of rows have "
        "p10 = p90); randomised-PIT central coverage 0.803 → mis-specified gate, replaced in 1.1.0",
    }
]


def _md_table(df: pd.DataFrame, cols: list[str], fmt: str = "{:.3f}") -> list[str]:
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        cells = [
            fmt.format(r[c]) if isinstance(r[c], float | np.floating) else str(r[c]) for c in cols
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _reliability_plot(tables: dict[str, pd.DataFrame], title: str, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(4.5, 4.5))
    ax.plot([0, 1], [0, 1], color="#888", lw=1, ls="--", label="perfect")
    for name, t in tables.items():
        ax.plot(t["mean_pred"], t["observed"], marker="o", ms=3, label=name)
    ax.set_xlabel("predicted probability")
    ax.set_ylabel("observed frequency")
    ax.set_title(title, fontsize=10)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _pit_plot(rows: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(5, 3.2))
    for col, name in (("pit_mc", "Monte Carlo"), ("pit_climatology", "climatology")):
        h = np.histogram(rows[col], bins=10, range=(0, 1))[0] / len(rows)
        ax.step(np.linspace(0.05, 0.95, 10), h, where="mid", label=name)
    ax.axhline(0.1, color="#888", ls="--", lw=1)
    ax.set_xlabel("randomised PIT")
    ax.set_ylabel("share of rows")
    ax.set_title("PIT histogram (uniform ⇔ calibrated), horizon 0", fontsize=10)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    report_only = "--report-only" in sys.argv
    snap = Path(args[0]) if args else pinned_snapshot()
    ds = load_snapshot(snap)
    cfg = ForecastEvalConfig()
    rep = ROOT / "ml" / "reports"
    out_dir = ROOT / "data" / "eval"
    rows_path = out_dir / "forecast_eval_rows.parquet"
    if report_only:
        rows = pd.read_parquet(rows_path)
        prev_path = rep / "forecast_eval.json"
        if prev_path.exists():
            prev = json.loads(prev_path.read_text())
            windows, trainings = prev["windows"], prev["trainings"]
            elapsed = prev["elapsed_seconds"]
        else:  # reconstruct the schedule from the stored rows (fitted globals not available)
            sched = rows[["season", "trained_at_gw"]].drop_duplicates()
            trainings = [{"season": s, "gw": int(g)} for s, g in sched.itertuples(index=False)]
            n_cut = rows[["season", "decision_gw"]].drop_duplicates().shape[0]
            windows = {
                "n_eval_cutoffs": n_cut,
                "horizon": cfg.horizon,
                "retrain_every": cfg.retrain_every,
                "n_sims": cfg.sim.n_sims,
                "simulation_seed": cfg.sim.seed,
                "data_snapshot_id": ds.snapshot_id,
            }
            el = [a for a in sys.argv if a.startswith("--elapsed=")]
            elapsed = float(el[0].split("=", 1)[1]) if el else float("nan")
    else:
        t0 = time.time()
        res = evaluate_forecasts(
            ds,
            cutoffs(ds, EVAL_SEASONS),
            cutoffs(ds, HISTORY_SEASONS),
            cfg,
            cache_root=ROOT / "data" / "feature-store",
            progress=lambda m: print(f"[{time.time() - t0:7.0f}s] {m}", flush=True),
        )
        elapsed = time.time() - t0
        rows, windows, trainings = res.rows, res.windows, res.trainings
        out_dir.mkdir(parents=True, exist_ok=True)
        rows.to_parquet(rows_path, index=False)

    pm, dm, mm = point_metrics(rows), distribution_metrics(rows), minutes_metrics(rows)
    h0 = rows[rows["horizon"] == 0]
    comparisons = {}
    for pop, sub in (("all", rows), ("regulars", rows[rows["regular"]])):
        for other in ("direct_lgbm", "fpl_style", "ppg_availability", "recent_form"):
            comparisons[f"{pop}: rmse(mc_mean) - rmse({other})"] = paired_cutoff_bootstrap(
                sub, "pred_mc_mean", f"pred_{other}"
            )
    comparisons["all: crps(mc) - crps(climatology)"] = paired_cutoff_bootstrap(
        rows, "crps_mc", "crps_climatology", metric="mean"
    )
    rel6 = {
        "Monte Carlo": fm.reliability_table(h0["prob_6_plus"], h0["points"] >= 6),
        "climatology": fm.reliability_table(h0["clim_prob_6_plus"], h0["points"] >= 6),
    }
    rels = {
        "minutes model": fm.reliability_table(h0["prob_start"], h0["act_starts"] > 0),
        "rate baseline": fm.reliability_table(h0["base_p_start"], h0["act_starts"] > 0),
    }
    figs = ROOT / "ml" / "reports" / "figures"
    figs.mkdir(parents=True, exist_ok=True)
    _reliability_plot(rel6, "P(points ≥ 6), horizon 0", figs / "forecast_reliability_6plus.svg")
    _reliability_plot(rels, "P(start), horizon 0", figs / "forecast_reliability_start.svg")
    _pit_plot(h0, figs / "forecast_pit.svg")

    flat = gate_metrics(pm, dm, mm)
    pspec, pref = points_spec()
    mspec, mref = minutes_spec()
    gates = {
        "points": evaluate_gates(flat, pspec.promotion_gates, pref.ref),
        "minutes": evaluate_gates(flat, mspec.promotion_gates, mref.ref),
    }
    payload = {
        "windows": windows,
        "elapsed_seconds": round(elapsed, 1),
        "n_rows": len(rows),
        "trainings": trainings,
        "gate_metrics": flat,
        "gate_history": GATE_HISTORY,
        "promotion_gates": {k: v.model_dump() for k, v in gates.items()},
        "point_metrics": pm.to_dict(orient="records"),
        "distribution_metrics": dm.to_dict(orient="records"),
        "minutes_metrics": mm.to_dict(orient="records"),
        "paired_comparisons": comparisons,
        "reliability_6_plus_h0": {k: v.to_dict(orient="records") for k, v in rel6.items()},
        "reliability_start_h0": {k: v.to_dict(orient="records") for k, v in rels.items()},
    }
    (rep / "forecast_eval.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")

    lines = [
        "# Forecast evaluation — decomposed Monte Carlo model vs benchmarks",
        "",
        f"Snapshot `{ds.snapshot_id}` · {windows['n_eval_cutoffs']} decision cutoffs "
        f"({', '.join(EVAL_SEASONS)}) · horizon {cfg.horizon} · retrain every "
        f"{cfg.retrain_every} GWs · {cfg.sim.n_sims} simulations (seed {cfg.sim.seed}) · "
        f"{len(rows):,} player × GW rows · {elapsed / 60:.0f} min.",
        "Generated by `ml/experiments/forecast_eval.py`; per-row predictions in "
        "`data/eval/forecast_eval_rows.parquet`.",
        "",
        "Gates: minutes@1.0.0 and points@1.0.0 were fixed before this evaluation; points@1.1.0 "
        "corrects one mis-specified gate after it (see *Gate history*).",
        "",
        "Populations: *all* = every registered player with a fixture; *regulars* = recent start "
        "rate ≥ 0.5. Primary point metric: RMSE (mean-optimal); MAE favours median-like "
        "forecasts and is reported for completeness.",
        "",
        "## Point accuracy (horizon 0)",
        "",
    ]
    for pop in ("all", "regulars"):
        t = pm[(pm["population"] == pop) & (pm["horizon"] == 0)].sort_values("rmse")
        lines += [
            f"**{pop}**",
            "",
            *_md_table(t, ["forecaster", "n", "rmse", "mae", "bias", "spearman"]),
            "",
        ]
    lines += ["## Point accuracy by horizon (RMSE, all players)", ""]
    piv = pm[pm["population"] == "all"].pivot(index="forecaster", columns="horizon", values="rmse")
    piv.columns = [f"h{c}" for c in piv.columns]
    lines += [*_md_table(piv.reset_index(), ["forecaster", *piv.columns]), ""]
    lines += [
        "## Paired comparisons (bootstrap over decision cutoffs, 95% CI; negative = MC better)",
        "",
        "| comparison | difference | 95% CI | cutoffs |",
        "|---|---|---|---|",
    ]
    for k, v in comparisons.items():
        lines.append(
            f"| {k} | {v['difference']:+.4f} | [{v['ci_low']:+.4f}, {v['ci_high']:+.4f}] "
            f"| {v['n_cutoffs']} |"
        )
    lines += ["", "## Distributional quality (horizon 0)", ""]
    t = dm[dm["horizon"] == 0]
    lines += [
        *_md_table(
            t,
            [
                "population",
                "model",
                "crps",
                "brier_6",
                "log_loss_6",
                "ece_6",
                "brier_10",
                "coverage_80",
                "pit_coverage_80",
                "pit_coverage_50",
                "pit_max_dev",
            ],
        ),
        "",
    ]
    lines += ["## Minutes (horizon 0)", ""]
    lines += [
        *_md_table(
            mm[mm["horizon"] == 0],
            [
                "population",
                "model",
                "brier_start",
                "log_loss_start",
                "ece_start",
                "minutes_mae",
                "minutes_rmse",
            ],
        ),
        "",
    ]
    for name, g in gates.items():
        lines += [
            f"## Promotion gates — {name} (`{g.config_ref}`): {'PASSED' if g.passed else 'FAILED'}",
            "",
            "| gate | metric | value | target | result |",
            "|---|---|---|---|---|",
        ]
        for ch in g.checks:
            v = "—" if ch.value is None else f"{ch.value:.4f}"
            lines.append(
                f"| {ch.name} | {ch.metric} | {v} | {ch.target} | "
                f"{'pass' if ch.passed else 'FAIL'} |"
            )
        lines.append("")
    lines += ["## Gate history (superseded versions on this evaluation)", ""]
    for gh in GATE_HISTORY:
        lines.append(
            f"- `{gh['config']}`: {'PASSED' if gh['passed'] else 'FAILED'} — "
            + "; ".join(
                f"{k} = {v['value']} (target {v['target']})" for k, v in gh["failed_gates"].items()
            )
            + f". {gh['diagnosis']}."
        )
    lines.append("")
    lines += [
        "## Figures",
        "",
        "![P(≥6) reliability](figures/forecast_reliability_6plus.svg)",
        "![P(start) reliability](figures/forecast_reliability_start.svg)",
        "![PIT](figures/forecast_pit.svg)",
        "",
    ]
    (rep / "forecast_eval.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
