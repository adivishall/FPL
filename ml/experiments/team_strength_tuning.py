"""Rolling-origin hyper-parameter selection for the team strength model (§57.2, §7 'Model tuning').

For each target season S, the configuration is selected by walk-forward match log-likelihood on
ALL seasons strictly before S (never on S itself), then evaluated on S. The production
configuration (for 2026-27) is selected on every completed season. This also defines which
configuration a backtest of season S may use (no tuning leakage).

A first single-split run (validate 2023-24 → test 2024-25/2025-26) selected half-life 60 /
α 0.35 / prior SD 0.6, which scored *worse* on test than the hand-set default (−2.934 vs −2.924
log-lik/match): single-season selection overfits season noise. Rolling-origin selection over
expanding windows is the protocol kept.

Usage: uv run python ml/experiments/team_strength_tuning.py [snapshot_dir]
Outputs: ml/reports/team_strength_tuning.{json,md}; config values are copied into
config/models/team_strength.yaml by hand after review (documented there).
"""

from __future__ import annotations

import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np
from pinned import pinned_snapshot

from fpl_forecasting.team_eval import evaluate_team_model
from fpl_forecasting.team_strength import TeamStrengthConfig
from fpl_storage.dataset import load_snapshot

ROOT = Path(__file__).resolve().parents[2]
SEASONS = ["2023-24", "2024-25", "2025-26"]
GRID = list(itertools.product([60.0, 120.0, 240.0], [0.0, 0.35, 0.7], [0.2, 0.35, 0.6]))


def main() -> None:
    snap = Path(sys.argv[1]) if len(sys.argv) > 1 else pinned_snapshot()
    ds = load_snapshot(snap)
    t0 = time.time()
    # Per-season walk-forward log-likelihood for every config (computed once).
    per_season: dict[tuple[float, float, float], dict[str, float]] = {}
    n_by_season: dict[str, int] = {}
    for cfg_t in GRID:
        cfg = TeamStrengthConfig(half_life_days=cfg_t[0], goals_weight=cfg_t[1], prior_sd=cfg_t[2])
        per_season[cfg_t] = {}
        for s in SEASONS:
            r = evaluate_team_model(ds, [s], cfg)
            per_season[cfg_t][s] = r.log_lik
            n_by_season[s] = r.n_matches
        print(cfg_t, {k: round(v, 4) for k, v in per_season[cfg_t].items()}, flush=True)

    def select(train: list[str]) -> tuple[float, float, float]:
        def score(c: tuple[float, float, float]) -> float:
            w = np.array([n_by_season[s] for s in train], dtype=float)
            return float(np.average([per_season[c][s] for s in train], weights=w))

        return max(GRID, key=score)

    folds = []
    default = TeamStrengthConfig()
    for i, target in enumerate(SEASONS[1:], start=1):
        chosen = select(SEASONS[:i])
        cfg = TeamStrengthConfig(
            half_life_days=chosen[0], goals_weight=chosen[1], prior_sd=chosen[2]
        )
        sel = evaluate_team_model(ds, [target], cfg).as_dict()
        dflt = evaluate_team_model(ds, [target], default).as_dict()
        goals_only = evaluate_team_model(
            ds, [target], cfg.model_copy(update={"goals_weight": 1.0})
        ).as_dict()
        folds.append(
            {
                "target": target,
                "tuned_on": SEASONS[:i],
                "selected": cfg.model_dump(),
                "selected_metrics": sel,
                "default_metrics": dflt,
                "goals_only_metrics": goals_only,
            }
        )
    prod = select(SEASONS)
    prod_cfg = TeamStrengthConfig(half_life_days=prod[0], goals_weight=prod[1], prior_sd=prod[2])
    report = {
        "protocol": "rolling-origin: select on all seasons before the target, evaluate on target",
        "snapshot": snap.name,
        "grid": [list(g) for g in GRID],
        "per_season_loglik": {str(list(k)): v for k, v in per_season.items()},
        "folds": folds,
        "production_selected_on": SEASONS,
        "production": prod_cfg.model_dump(),
        "runtime_s": round(time.time() - t0, 1),
    }
    out = ROOT / "ml" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "team_strength_tuning.json").write_text(json.dumps(report, indent=2))
    md = [
        "# Team strength model — rolling-origin tuning",
        "",
        f"Snapshot `{snap.name}`; grid of {len(GRID)} configs (half-life days × goals weight α "
        "× prior SD); walk-forward over every GW; selection by match log-likelihood on all "
        "seasons *before* the target.",
        "",
        "| Target | Tuned on | Selected (hl, α, sd) | log-lik selected | default | goals-only "
        "| league avg | CS Brier selected | league avg |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for f in folds:
        s, d, g = f["selected_metrics"], f["default_metrics"], f["goals_only_metrics"]
        c = f["selected"]
        md.append(
            f"| {f['target']} | {', '.join(f['tuned_on'])} | ({c['half_life_days']:g}, "
            f"{c['goals_weight']:g}, {c['prior_sd']:g}) | {s['log_lik']:.4f} | "
            f"{d['log_lik']:.4f} | {g['log_lik']:.4f} | {s['log_lik_league_avg']:.4f} | "
            f"{s['cs_brier']:.4f} | {s['cs_brier_league_avg']:.4f} |"
        )
    md += [
        "",
        f"Production (2026-27) configuration, selected on {', '.join(SEASONS)}: "
        f"half-life {prod_cfg.half_life_days:g} d, α {prod_cfg.goals_weight:g}, prior SD "
        f"{prod_cfg.prior_sd:g}.",
    ]
    (out / "team_strength_tuning.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
