"""Out-of-sample check of the empirical-Bayes shrinkage strength for player event rates.

For several decision cutoffs, fit the rate model on point-in-time history, then score the
predicted per-player rate against the rate realised over the next 10 gameweeks (exposure-weighted
squared error). The fitted shrinkage κ is compared with κ scaled by {0, ¼, ½, 2, 4, ∞}:
0 = raw player rate, ∞ = position/price prior only. A well-estimated κ should be (near) optimal.

Usage: uv run python ml/experiments/rate_shrinkage_check.py [snapshot_dir]
Outputs: ml/reports/rate_shrinkage.{json,md}
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pinned import pinned_snapshot

from fpl_forecasting.rates import (
    RATES,
    RateConfig,
    fit_rate_model,
    history_with_team_xg,
    sufficient_stats,
)
from fpl_forecasting.walkforward import cutoffs
from fpl_storage.dataset import load_snapshot
from fpl_storage.pit import PointInTimeView

ROOT = Path(__file__).resolve().parents[2]
CUTS = [
    ("2023-24", 8),
    ("2023-24", 20),
    ("2024-25", 8),
    ("2024-25", 22),
    ("2025-26", 10),
    ("2025-26", 20),
]
MULTS = [0.0, 0.25, 0.5, 1.0, 2.0, 4.0, np.inf]
SCORED = ("goal_share", "assist_share", "saves_p90", "dc_p90", "yellow_p90")
POS = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
FUTURE_GWS = 10


def main() -> None:
    snap = Path(sys.argv[1]) if len(sys.argv) > 1 else pinned_snapshot()
    ds = load_snapshot(snap)
    allc = cutoffs(ds, sorted({s for s, _ in CUTS}))
    cfg = RateConfig()
    rows = []
    fitted = {}
    for season, gw in CUTS:
        cut = next(c for c in allc if c.season == season and c.gw == gw)
        view = PointInTimeView(ds, cut.cutoff)
        pool = view.player_pool(season, gw)
        positions = pool.set_index("player_code")["position"]
        lp = np.log(pool.set_index("player_code")["price"].astype(float))
        hist = history_with_team_xg(view)
        st = sufficient_stats(hist[hist["player_code"].isin(positions.index)], positions, cfg)
        model = fit_rate_model(st, positions, lp, cfg)
        fitted[f"{season} GW{gw}"] = {
            n: {
                "phi": model.phi[n].round(4).tolist(),
                "cv2": model.cv2[n].round(4).tolist(),
                "n_split": model.n_split[n].astype(int).tolist(),
            }
            for n, _, _ in RATES
            if n in SCORED
        }
        # Realised rates over the following gameweeks (evaluation only; no recency weighting).
        end = max(c.cutoff for c in allc if c.season == season and c.gw <= gw + FUTURE_GWS)
        fh = history_with_team_xg(PointInTimeView(ds, end + pd.Timedelta(days=7)))
        fh = fh[
            (fh["season"] == season)
            & (fh["gw"] >= gw)
            & (fh["gw"] < gw + FUTURE_GWS)
            & fh["player_code"].isin(st.index)
        ]
        fst = sufficient_stats(fh, positions, dataclasses.replace(cfg, half_life_matches=1e9))
        pos_idx = positions.reindex(st.index).map(POS).to_numpy(int)
        lpa = lp.reindex(st.index).fillna(lp.median()).to_numpy()
        for name, num, expo in RATES:
            if name not in SCORED:
                continue
            m = model.prior_mean(name, pos_idx, lpa)
            k0 = model.kappa(name, pos_idx, m)
            x, e = st[num].to_numpy(float), st[expo].to_numpy(float)
            fx = fst[num].reindex(st.index).to_numpy(float)
            fe = fst[expo].reindex(st.index).to_numpy(float)
            ok = fe > 1.0
            if name == "saves_p90":
                ok &= pos_idx == 0
            for mult in MULTS:
                pred = m if np.isinf(mult) else (x + mult * k0 * m) / (e + mult * k0 + 1e-12)
                okk = ok & np.isfinite(pred) & ((e > 0) | (mult > 0))
                err = np.sum(fe[okk] * (fx[okk] / fe[okk] - pred[okk]) ** 2) / np.sum(fe[okk])
                rows.append(
                    {
                        "cutoff": f"{season} GW{gw}",
                        "rate": name,
                        "mult": mult,
                        "err": err,
                        "n": int(okk.sum()),
                    }
                )
    d = pd.DataFrame(rows)
    t = d.groupby(["rate", "mult"])["err"].mean().unstack()
    rel = t.div(t[1.0], axis=0)
    out = {
        "snapshot_id": ds.snapshot_id,
        "cutoffs": [f"{s} GW{g}" for s, g in CUTS],
        "future_gameweeks": FUTURE_GWS,
        "relative_error": {r: {str(k): float(v) for k, v in rel.loc[r].items()} for r in rel.index},
        "best_multiplier": {r: str(rel.loc[r].idxmin()) for r in rel.index},
        "fitted": fitted,
    }
    rep = ROOT / "ml" / "reports"
    (rep / "rate_shrinkage.json").write_text(json.dumps(out, indent=2) + "\n")
    header = "| rate | " + " | ".join(
        "∞ (prior only)" if np.isinf(m) else f"κ×{m:g}" for m in MULTS
    )
    lines = [
        "# Rate shrinkage — out-of-sample check",
        "",
        f"Snapshot `{ds.snapshot_id}`; cutoffs {', '.join(out['cutoffs'])}; target = realised rate"
        f" over the next {FUTURE_GWS} GWs; exposure-weighted squared error, relative to the fitted"
        " κ (=1.000). κ×0 is the raw player rate, ∞ the position/price prior alone.",
        "Generated by `ml/experiments/rate_shrinkage_check.py`.",
        "",
        header + " |",
        "|---" * (len(MULTS) + 1) + "|",
    ]
    for r in rel.index:
        lines.append(f"| {r} | " + " | ".join(f"{v:.3f}" for v in rel.loc[r]) + " |")
    lines += [
        "",
        "Reading: values > 1 mean that multiplier predicts worse than the fitted κ. The fitted κ"
        " being the row minimum (or within noise of it) is evidence the split-half credibility"
        " estimator is well calibrated; it is a check, not a tuning loop — κ is never chosen"
        " from these numbers.",
    ]
    (rep / "rate_shrinkage.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
