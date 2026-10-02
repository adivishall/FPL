"""Walk-forward evaluation of the price-change model (§57.1, §66).

Decision cutoffs: every GW ≥ 3 of 2023-24, 2024-25, 2025-26. Models are retrained every 5 GWs
(and at each season start) on rows whose outcome is visible at the training cutoff (all earlier
seasons from 2022-23 plus the visible part of the current one). Targets: rise / fall of the price
between GW t and GW t+1. Compared against the naive trend baseline and the base rate.

Usage: uv run python ml/experiments/price_change_eval.py [snapshot_dir]
Outputs: ml/reports/price_change.{json,md}, ml/reports/figures/price_reliability.svg
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
from sklearn.metrics import roc_auc_score

from fpl_forecasting import metrics as fm
from fpl_forecasting.governance import evaluate_gates
from fpl_forecasting.model_config import price_spec
from fpl_forecasting.price_change import (
    PriceChangeModel,
    TrendBaseline,
    decision_features,
    training_rows,
)
from fpl_forecasting.walkforward import cutoffs
from fpl_storage.dataset import load_snapshot
from fpl_storage.pit import PointInTimeView

ROOT = Path(__file__).resolve().parents[2]
EVAL_SEASONS = ["2023-24", "2024-25", "2025-26"]
ALL_SEASONS = ["2022-23", *EVAL_SEASONS]
SPEC, SPEC_REF = price_spec()
RETRAIN_EVERY = SPEC.retrain_every_gameweeks


def main() -> None:
    snap = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else sorted((ROOT / "data" / "snapshots").glob("snap_*"))[-1]
    )
    ds = load_snapshot(snap)
    pm = ds["player_match"].sort_values("kickoff_at")
    price = pm.groupby(["season", "player_code", "gw"])["price"].last().astype(float)
    t0 = time.time()
    preds = []
    trainings = []
    for season in EVAL_SEASONS:
        model = base = None
        trained_gw = -99
        for c in cutoffs(ds, [season]):
            if c.gw < 3:
                continue
            view = PointInTimeView(ds, c.cutoff)
            if model is None or c.gw - trained_gw >= RETRAIN_EVERY:
                seasons = [s for s in ALL_SEASONS if s <= season]
                rows = training_rows(view, seasons)
                model = PriceChangeModel(SPEC.params).fit(rows)
                base = TrendBaseline().fit(rows)
                trained_gw = c.gw
                view.assert_no_leakage()
                trainings.append({"season": season, "gw": c.gw, "rows": len(rows)})
            feats = decision_features(view, season, c.gw)
            view.assert_no_leakage()
            if feats.empty:
                continue
            m = model.predict(feats)
            b = base.predict(feats)
            out = feats[["player_code", "price", "d_price_1", "net_ratio_1"]].copy()
            out["p_rise"], out["p_fall"] = m["p_rise"].to_numpy(), m["p_fall"].to_numpy()
            out["b_rise"], out["b_fall"] = b["p_rise"].to_numpy(), b["p_fall"].to_numpy()
            out["c_rise"], out["c_fall"] = base.prior
            # evaluation-only outcome
            p_t = price.reindex(pd.MultiIndex.from_product([[season], out["player_code"], [c.gw]]))
            p_n = price.reindex(
                pd.MultiIndex.from_product([[season], out["player_code"], [c.gw + 1]])
            )
            out["d_next"] = p_n.to_numpy() - p_t.to_numpy()
            out["season"], out["decision_gw"], out["trained_gw"] = season, c.gw, trained_gw
            preds.append(out[out["d_next"].notna()])
        print(f"[{time.time() - t0:6.0f}s] {season} done", flush=True)
    allp = pd.concat(preds, ignore_index=True)
    rec = []
    for season, g in [*allp.groupby("season"), ("all", allp)]:
        for event, sign in (("rise", 1), ("fall", -1)):
            y = (np.sign(g["d_next"]) == sign).astype(float).to_numpy()
            for model_name, col in (
                ("lgbm_calibrated", f"p_{event}"),
                ("trend_baseline", f"b_{event}"),
                ("base_rate", f"c_{event}"),
            ):
                p = np.clip(g[col].to_numpy(float), 1e-5, 1 - 1e-5)
                rec.append(
                    {
                        "season": season,
                        "event": event,
                        "model": model_name,
                        "n": len(g),
                        "event_rate": float(y.mean()),
                        "brier": fm.brier(p, y),
                        "log_loss": fm.log_loss(p, y),
                        "ece": fm.ece(p, y),
                        "auc": float(roc_auc_score(y, p)) if 0 < y.sum() < len(y) else float("nan"),
                    }
                )
    met = pd.DataFrame(rec)
    flat = {}
    for r in met[met["season"] == "all"].itertuples():
        suffix = "" if r.model == "lgbm_calibrated" else f"_{r.model}"
        for k in ("brier", "log_loss", "ece", "auc"):
            flat[f"{k}_{r.event}_all{suffix}"] = float(getattr(r, k))
    gates = evaluate_gates(flat, SPEC.promotion_gates, SPEC_REF.ref)
    rel = {
        f"{event} · {name}": fm.reliability_table(allp[col], np.sign(allp["d_next"]) == sign)
        for event, sign in (("rise", 1), ("fall", -1))
        for name, col in (("model", f"p_{event}"), ("trend", f"b_{event}"))
    }
    figs = ROOT / "ml" / "reports" / "figures"
    figs.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(4.8, 4.8))
    ax.plot([0, 0.6], [0, 0.6], color="#888", ls="--", lw=1)
    for k, t in rel.items():
        ax.plot(t["mean_pred"], t["observed"], marker="o", ms=3, label=k)
    ax.set_xlabel("predicted probability")
    ax.set_ylabel("observed frequency")
    ax.set_title("Price change reliability (all eval seasons)", fontsize=10)
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(figs / "price_reliability.svg")
    plt.close(fig)

    rep = ROOT / "ml" / "reports"
    payload = {
        "snapshot_id": ds.snapshot_id,
        "eval_seasons": EVAL_SEASONS,
        "retrain_every": RETRAIN_EVERY,
        "n_predictions": len(allp),
        "trainings": trainings,
        "config": SPEC_REF.ref,
        "metrics": met.to_dict(orient="records"),
        "gate_metrics": flat,
        "promotion_gates": gates.model_dump(),
        "reliability": {k: v.to_dict(orient="records") for k, v in rel.items()},
        "elapsed_seconds": round(time.time() - t0, 1),
    }
    (rep / "price_change.json").write_text(json.dumps(payload, indent=2) + "\n")
    lines = [
        "# Price-change model — walk-forward evaluation",
        "",
        f"Snapshot `{ds.snapshot_id}` · decision GWs ≥ 3 of {', '.join(EVAL_SEASONS)} · "
        f"retrain every {RETRAIN_EVERY} GWs · {len(allp):,} player × GW predictions.",
        "Target: direction of price(t+1) − price(t). Information set: GW rows ≤ t−1 only (the "
        "archive is gameweek-granular, so last-week transfer momentum is not observable — see "
        "limitations). Generated by `ml/experiments/price_change_eval.py`.",
        "",
        "| season | event | model | n | event rate | Brier | log loss | ECE | AUC |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in met.itertuples():
        lines.append(
            f"| {r.season} | {r.event} | {r.model} | {r.n} | {r.event_rate:.4f} | {r.brier:.5f} "
            f"| {r.log_loss:.4f} | {r.ece:.4f} | {r.auc:.3f} |"
        )
    lines += [
        "",
        f"## Promotion gates (`{SPEC_REF.ref}`, fixed before evaluation): "
        f"{'PASSED' if gates.passed else 'FAILED'}",
        "",
        "| gate | metric | value | target | result |",
        "|---|---|---|---|---|",
    ]
    for ch in gates.checks:
        v = "—" if ch.value is None else f"{ch.value:.5f}"
        lines.append(
            f"| {ch.name} | {ch.metric} | {v} | {ch.target} | {'pass' if ch.passed else 'FAIL'} |"
        )
    lines += ["", "![reliability](figures/price_reliability.svg)", ""]
    (rep / "price_change.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
