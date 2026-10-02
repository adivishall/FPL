"""P1 research (§13, §5): weighted ensemble of point forecasts and conformal intervals.

Both use the stored walk-forward predictions (``data/eval/forecast_eval_rows.parquet``,
produced by ``forecast_eval.py``), so they add no new information access.

* **Ensemble** — w·MC mean + (1 − w)·direct LightGBM, with w chosen by RMSE on *earlier seasons
  only* (rolling origin), evaluated on the next season.
* **Conformal intervals with temporal care** — for each decision cutoff, nonconformity scores are
  computed on rows from the previous K cutoffs *whose outcomes are already known at this cutoff*
  (target GW < decision GW of the same season); split-conformal quantiles give
  (a) CQR-adjusted MC intervals [p10 − q, p90 + q] and (b) symmetric residual intervals around
  the MC mean. Coverage and width are compared with the raw MC [p10, p90] interval.

Usage: uv run python ml/experiments/ensemble_conformal.py
Outputs: ml/reports/ensemble_conformal.{json,md}
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
K_CAL = 8
ALPHA = 0.2


def rmse(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.sqrt(np.mean((p - y) ** 2)))


def ensemble(rows: pd.DataFrame) -> dict[str, object]:
    out: dict[str, object] = {}
    seasons = sorted(rows["season"].unique())
    grid = np.linspace(0, 1, 21)
    for s in seasons[1:]:
        tr, te = rows[rows["season"] < s], rows[rows["season"] == s]
        yt = tr["points"].to_numpy(float)
        best = min(
            grid,
            key=lambda w: rmse(
                w * tr["pred_mc_mean"].to_numpy() + (1 - w) * tr["pred_direct_lgbm"].to_numpy(),
                yt,
            ),
        )
        y = te["points"].to_numpy(float)
        ens = best * te["pred_mc_mean"].to_numpy() + (1 - best) * te["pred_direct_lgbm"].to_numpy()
        out[s] = {
            "weight_mc": float(best),
            "rmse_ensemble": rmse(ens, y),
            "rmse_mc": rmse(te["pred_mc_mean"].to_numpy(), y),
            "rmse_direct": rmse(te["pred_direct_lgbm"].to_numpy(), y),
            "n": len(te),
        }
    return out


def conformal(rows: pd.DataFrame) -> pd.DataFrame:
    recs = []
    for season, srows in rows.groupby("season"):
        cuts = sorted(srows["decision_gw"].unique())
        for i, gw in enumerate(cuts):
            test = srows[srows["decision_gw"] == gw]
            prev = srows[srows["decision_gw"].isin(cuts[max(0, i - K_CAL) : i])]
            cal = prev[prev["target_gw"] < gw]  # outcomes known at this cutoff
            if len(cal) < 500:
                continue
            for h, t in test.groupby("horizon"):
                c = cal[cal["horizon"] == h]
                if len(c) < 200:
                    continue
                y_c = c["points"].to_numpy(float)
                s_cqr = np.maximum(c["p10"].to_numpy() - y_c, y_c - c["p90"].to_numpy())
                s_abs = np.abs(y_c - c["mean"].to_numpy())
                level = min(1.0, np.ceil((len(c) + 1) * (1 - ALPHA)) / len(c))
                q_cqr = float(np.quantile(s_cqr, level, method="higher"))
                q_abs = float(np.quantile(s_abs, level, method="higher"))
                y = t["points"].to_numpy(float)
                lo_raw, hi_raw = t["p10"].to_numpy(), t["p90"].to_numpy()
                lo_c, hi_c = lo_raw - q_cqr, hi_raw + q_cqr
                lo_a, hi_a = t["mean"].to_numpy() - q_abs, t["mean"].to_numpy() + q_abs
                for name, lo, hi in (
                    ("mc_quantiles", lo_raw, hi_raw),
                    ("cqr_adjusted", lo_c, hi_c),
                    ("residual_conformal", lo_a, hi_a),
                ):
                    recs.append(
                        {
                            "season": season,
                            "decision_gw": gw,
                            "horizon": int(h),
                            "method": name,
                            "n": len(t),
                            "covered": float(np.sum((y >= lo) & (y <= hi))),
                            "width": float(np.sum(hi - lo)),
                            "q": q_cqr if name == "cqr_adjusted" else q_abs,
                        }
                    )
    return pd.DataFrame(recs)


def main() -> None:
    rows = pd.read_parquet(ROOT / "data" / "eval" / "forecast_eval_rows.parquet")
    h0 = rows[rows["horizon"] == 0]
    ens = ensemble(h0)
    conf = conformal(rows)
    agg = conf.groupby(["method", "horizon"])[["covered", "width", "n"]].sum()
    agg["coverage"] = agg["covered"] / agg["n"]
    agg["mean_width"] = agg["width"] / agg["n"]
    agg = agg.reset_index()
    payload = {
        "ensemble": ens,
        "conformal": agg[["method", "horizon", "n", "coverage", "mean_width"]].to_dict("records"),
        "k_calibration_cutoffs": K_CAL,
        "alpha": ALPHA,
    }
    rep = ROOT / "ml" / "reports"
    (rep / "ensemble_conformal.json").write_text(json.dumps(payload, indent=2) + "\n")
    lines = [
        "# P1 research — ensemble and conformal intervals",
        "",
        "Inputs: stored walk-forward predictions from `forecast_eval.py` (114 cutoffs). "
        "Generated by `ml/experiments/ensemble_conformal.py`.",
        "",
        "## Weighted ensemble (MC mean ⊕ direct LightGBM), horizon 0, rolling-origin weight",
        "",
        "| test season | weight on MC (chosen earlier) | RMSE ensemble | RMSE MC | RMSE direct |",
        "|---|---|---|---|---|",
    ]
    for s, v in ens.items():
        assert isinstance(v, dict)
        lines.append(
            f"| {s} | {v['weight_mc']:.2f} | {v['rmse_ensemble']:.4f} | {v['rmse_mc']:.4f} | "
            f"{v['rmse_direct']:.4f} |"
        )
    lines += [
        "",
        "## Conformal intervals (target coverage ≥ 0.80; calibration = previous "
        f"{K_CAL} cutoffs' rows with known outcomes)",
        "",
        "| method | horizon | n | coverage | mean width |",
        "|---|---|---|---|---|",
    ]
    for r in agg.itertuples():
        lines.append(
            f"| {r.method} | {r.horizon} | {r.n:,} | {r.coverage:.3f} | {r.mean_width:.2f} |"
        )
    lines += [
        "",
        "Interpretation is in `docs/MODEL_CARD.md` §4 (adoption decisions are recorded there, "
        "not here).",
    ]
    (rep / "ensemble_conformal.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
