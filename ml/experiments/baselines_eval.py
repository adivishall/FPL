"""Walk-forward evaluation of deterministic baselines (§28, §57.2, §70.3).

Window: every gameweek of 2023-24, 2024-25 and 2025-26 as decision cutoffs; targets are the
next 5 gameweeks (horizon 0–4); training history from 2022-23 onward (labels obey the cutoff).
Metric policy: RMSE is primary for expected-points forecasts (mean-optimal under heavy tails);
MAE is reported but favours median-like predictions; Spearman measures ranking quality.

Usage: uv run python ml/experiments/baselines_eval.py [snapshot_dir]
Outputs: ml/reports/baselines.{json,md}
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from fpl_forecasting.baselines import all_baselines
from fpl_forecasting.walkforward import cutoffs, evaluate_point_forecasters, summarize
from fpl_storage.dataset import load_snapshot

ROOT = Path(__file__).resolve().parents[2]
EVAL_SEASONS = ["2023-24", "2024-25", "2025-26"]
HISTORY_SEASONS = ["2022-23", *EVAL_SEASONS]
HORIZON = 5


def main() -> None:
    snap = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else sorted((ROOT / "data" / "snapshots").glob("snap_*"))[-1]
    )
    ds = load_snapshot(snap)
    t0 = time.time()
    res = evaluate_point_forecasters(
        ds,
        all_baselines(),
        cutoffs(ds, EVAL_SEASONS),
        cutoffs(ds, HISTORY_SEASONS),
        horizon=HORIZON,
        cache_root=ROOT / "data" / "feature-store",
    )
    summ = summarize(res.metrics)
    out = ROOT / "ml" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "snapshot": snap.name,
        "windows": res.windows,
        "runtime_s": round(time.time() - t0),
        "metric_policy": "RMSE primary (expected points); MAE reported; Spearman ranking",
        "by_season": res.metrics.to_dict("records"),
        "summary": summ.to_dict("records"),
    }
    (out / "baselines.json").write_text(json.dumps(report, indent=2, default=str))
    md = [
        "# Baseline forecasts — walk-forward evaluation",
        "",
        f"Snapshot `{snap.name}`; decision cutoffs: every GW of {', '.join(EVAL_SEASONS)} "
        f"({res.windows['n_eval_cutoffs']} cutoffs); targets: next {HORIZON} GWs; unit: "
        "player × target GW (double GWs summed); populations: all pool players with a fixture "
        "and *regulars* (recency-weighted start rate ≥ 0.5 at the cutoff).",
        "",
        "| Population | Horizon | Forecaster | n | RMSE | MAE | Bias | Spearman |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for r in summ.sort_values(["population", "horizon", "rmse"]).itertuples():
        md.append(
            f"| {r.population} | {r.horizon} | {r.forecaster} | {r.n} | {r.rmse:.3f} | "
            f"{r.mae:.3f} | {r.bias:+.3f} | {r.spearman:.3f} |"
        )
    (out / "baselines.md").write_text("\n".join(md) + "\n")
    print("\n".join(md[:4] + [x for x in md[4:] if "| 0 |" in x]))


if __name__ == "__main__":
    main()
