"""Three-season walk-forward backtest report (§27, §28, §70; protocol docs/BACKTEST_PROTOCOL.md).

Reads the finished replays (data/eval/backtest_<season>.parquet) and the forecasts each decision
actually used (data/eval/forecasts_<season>.parquet) and writes ml/reports/backtest.{md,json}
plus figures. Every number is computed here from those records; nothing is hand-entered.
Unfavourable comparisons are reported exactly like favourable ones.

Usage: uv run python ml/experiments/backtest.py --report-only
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ENGINE = "engine"
BENCHMARKS = ["engine_no_chips", "single_gw_mc", "simple_xp", "form", "fpl_style_heuristic", "hold"]
# The report published in commit 03c14a7 was produced before schedule rule S1 existed (i.e. with
# the final-schedule look-ahead); it is archived verbatim and compared here, never cited.
SUPERSEDED = (
    Path(__file__).resolve().parents[2] / "ml/reports/archive/backtest_superseded_03c14a7.json"
)


def _superseded() -> dict[str, Any] | None:
    return json.loads(SUPERSEDED.read_text()) if SUPERSEDED.exists() else None


# ----------------------------------------------------------------------------- statistics


def paired_ci(d: np.ndarray, n_boot: int, seed: int) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), (n_boot, len(d)))
    s = d[idx].sum(axis=1)
    return float(np.quantile(s, 0.025)), float(np.quantile(s, 0.975))


def pooled_ci(by_season: list[np.ndarray], n_boot: int, seed: int) -> tuple[float, float]:
    """Season-stratified bootstrap of the summed difference (gameweeks resampled per season)."""
    rng = np.random.default_rng(seed)
    total = np.zeros(n_boot)
    for d in by_season:
        total += d[rng.integers(0, len(d), (n_boot, len(d)))].sum(axis=1)
    return float(np.quantile(total, 0.025)), float(np.quantile(total, 0.975))


def verdict(lo: float, hi: float) -> str:
    if lo > 0:
        return "engine better (CI > 0)"
    if hi < 0:
        return "engine worse (CI < 0)"
    return "inconclusive (CI includes 0)"


def reliability(p: np.ndarray, y: np.ndarray, bins: int = 10) -> tuple[float, list[dict[str, Any]]]:
    edges = np.linspace(0, 1, bins + 1)
    k = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    rows, ece = [], 0.0
    for b in range(bins):
        m = k == b
        if not m.any():
            continue
        conf, acc = float(p[m].mean()), float(y[m].mean())
        ece += m.mean() * abs(conf - acc)
        rows.append(
            {
                "bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}",
                "n": int(m.sum()),
                "predicted": conf,
                "observed": acc,
            }
        )
    return float(ece), rows


# ----------------------------------------------------------------------------- sections


def labels(pm: pd.DataFrame) -> pd.DataFrame:
    g = pm.groupby(["season", "gw", "player_code"], as_index=False).agg(
        points=("points", "sum"), minutes=("minutes", "sum"), starts=("starts", "sum")
    )
    g["started"] = (g["starts"].fillna(0) > 0).astype(float)
    g["played"] = (g["minutes"] > 0).astype(float)
    return g


def prediction_metrics(fc: pd.DataFrame, lab: pd.DataFrame) -> dict[str, Any]:
    j = fc.merge(lab, on=["season", "gw", "player_code"], how="left", indicator=True)
    unmatched = int((j["_merge"] == "left_only").sum())
    j = j[j["_merge"] == "both"]
    out: dict[str, Any] = {"unmatched_forecast_rows": unmatched, "by_season_horizon": []}
    for (season, h), grp in j.groupby(["season", "horizon"]):
        y, mu = grp["points"].to_numpy(float), grp["mean"].to_numpy(float)
        inside = (grp["points"] >= grp["p10"]) & (grp["points"] <= grp["p90"])
        strict = (grp["points"] > grp["p10"]) & (grp["points"] < grp["p90"])
        ps, st = grp["prob_start"].to_numpy(float), grp["started"].to_numpy(float)
        ece, _ = reliability(ps, st)
        eps = 1e-6
        out["by_season_horizon"].append(
            {
                "season": season,
                "horizon": int(h),
                "n": len(grp),
                "mae": float(np.abs(y - mu).mean()),
                "rmse": float(np.sqrt(((y - mu) ** 2).mean())),
                "bias": float((mu - y).mean()),
                "corr": float(np.corrcoef(mu, y)[0, 1]),
                "coverage_p10_p90_inclusive": float(inside.mean()),
                "coverage_p10_p90_strict": float(strict.mean()),
                "mean_width_p10_p90": float((grp["p90"] - grp["p10"]).mean()),
                "start_brier": float(((ps - st) ** 2).mean()),
                "start_log_loss": float(
                    -(
                        st * np.log(np.clip(ps, eps, 1))
                        + (1 - st) * np.log(np.clip(1 - ps, eps, 1))
                    ).mean()
                ),
                "start_ece": ece,
                "start_base_rate": float(st.mean()),
            }
        )
    h0 = j[j["horizon"] == 0]
    out["start_reliability_h0"] = reliability(
        h0["prob_start"].to_numpy(float), h0["started"].to_numpy(float)
    )[1]
    out["start_brier_climatology_h0"] = float(((h0["started"].mean() - h0["started"]) ** 2).mean())
    return out


def chips_table(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    eng = df[(df["strategy"] == ENGINE) & df["chip"].notna()]
    nochip = df[df["strategy"] == "engine_no_chips"].set_index(["season", "gw"])["points"]
    for r in eng.sort_values(["season", "gw"]).itertuples():
        kind = str(r.chip).rsplit("_", 1)[0]
        value = {
            "bench_boost": r.bench_points,  # points the bench added
            "triple_captain": r.captain_points,  # the extra armband multiple
        }.get(kind)
        rows.append(
            {
                "season": r.season,
                "gw": int(r.gw),
                "chip": r.chip,
                "points": int(r.points),
                "direct_chip_points": None if value is None else int(value),
                "engine_no_chips_same_gw": int(nochip.get((r.season, r.gw), 0)),
            }
        )
    return rows


def actions(df: pd.DataFrame) -> list[dict[str, Any]]:
    e = df[df["strategy"] == ENGINE].copy()

    def kind(r: Any) -> str:
        if isinstance(r.chip, str):
            return "CHIP"
        if r.hit_points > 0:
            return "HIT"
        return "TRANSFER" if r.transfers > 0 else "HOLD"

    e["action"] = [kind(r) for r in e.itertuples()]
    return (
        e.groupby(["season", "action"])
        .agg(gws=("gw", "count"), points=("points", "mean"))
        .reset_index()
        .to_dict("records")
    )


def stability(df: pd.DataFrame) -> list[dict[str, Any]]:
    out = []
    eng = df[df["strategy"] == ENGINE].sort_values(["season", "gw"])
    for season, grp in eng.groupby("season"):
        g = grp.reset_index(drop=True)
        rows = []
        for i in range(len(g) - 1):
            plan = g.loc[i, "planned_next"]
            if plan is None or isinstance(plan, float):
                continue
            planned = (sorted(int(c) for c in plan[0]), sorted(int(c) for c in plan[1]))
            actual = (
                sorted(int(c) for c in g.loc[i + 1, "transfers_out"]),
                sorted(int(c) for c in g.loc[i + 1, "transfers_in"]),
            )
            ip, ia = set(planned[1]), set(actual[1])
            rows.append(
                {
                    "planned_hold": not planned[1],
                    "acted": bool(actual[1]),
                    "as_planned": planned == actual,
                    "overlap": len(ip & ia) / len(ip | ia) if (ip or ia) else 1.0,
                }
            )
        c = pd.DataFrame(rows)
        out.append(
            {
                "season": season,
                "plans": len(c),
                "changed_share": float(1 - c["as_planned"].mean()),
                "planned_hold_kept": float((~c.loc[c["planned_hold"], "acted"]).mean())
                if c["planned_hold"].any()
                else None,
                "planned_move_executed_exactly": float(
                    c.loc[~c["planned_hold"], "as_planned"].mean()
                )
                if (~c["planned_hold"]).any()
                else None,
                "incoming_overlap": float(c["overlap"].mean()),
            }
        )
    return out


def round_trips(df: pd.DataFrame) -> list[dict[str, Any]]:
    out = []
    for (season, strat), g in df.sort_values("gw").groupby(["season", "strategy"]):
        bought: dict[int, int] = {}
        n = 0
        for r in g.itertuples():
            if isinstance(r.chip, str) and r.chip.startswith("free_hit"):
                continue
            n += sum(1 for c in r.transfers_out if int(c) in bought and r.gw - bought[int(c)] <= 3)
            bought.update({int(c): r.gw for c in r.transfers_in})
        out.append({"season": season, "strategy": strat, "round_trips": n})
    return out


def realised_transfers(df: pd.DataFrame, pts: pd.Series, k: int) -> pd.DataFrame:
    rows = []
    for r in df.itertuples():
        chip = r.chip if isinstance(r.chip, str) else ""
        if len(r.transfers_in) == 0 or chip.startswith(("wildcard", "free_hit")):
            continue
        gws = range(r.gw, min(r.gw + k, 39))
        got = sum(pts.get((r.season, g, int(c)), 0) for g in gws for c in r.transfers_in)
        lost = sum(pts.get((r.season, g, int(c)), 0) for g in gws for c in r.transfers_out)
        rows.append(
            {
                "strategy": r.strategy,
                "season": r.season,
                "gw": int(r.gw),
                "sold": [int(c) for c in r.transfers_out],
                "bought": [int(c) for c in r.transfers_in],
                "hit": int(r.hit_points),
                f"gain_{k}gw": int(got - lost - r.hit_points),
            }
        )
    return pd.DataFrame(rows)


def accounting(df: pd.DataFrame, seasons: list[str]) -> list[dict[str, Any]]:
    checks: list[tuple[str, Callable[[], bool]]] = [
        (
            "every strategy has GW1-38 exactly once per season",
            lambda: bool(
                df.groupby(["season", "strategy"])["gw"]
                .apply(lambda g: sorted(g) == list(range(1, 39)))
                .all()
            ),
        ),
        (
            "points = raw points - hit points",
            lambda: bool((df["points"] == df["raw_points"] - df["hit_points"]).all()),
        ),
        (
            "hit points are whole multiples of the hit cost (4)",
            lambda: bool((df["hit_points"] % 4 == 0).all()),
        ),
        (
            "no hits in chip weeks that make transfers free (WC/FH)",
            lambda: bool(
                (
                    df.loc[df["chip"].fillna("").str.match(r"(wildcard|free_hit)"), "hit_points"]
                    == 0
                ).all()
            ),
        ),
        (
            "hindsight-best lineup >= chosen lineup (same squad)",
            lambda: bool((df["hindsight_lineup_points"] >= df["raw_points"]).all()),
        ),
        (
            "hold strategy never transfers",
            lambda: bool((df.loc[df["strategy"] == "hold", "transfers"] == 0).all()),
        ),
        (
            "no data newer than the decision cutoff (look-ahead)",
            lambda: bool((df["data_age_hours"].dropna() >= 0).all()),
        ),
        ("single pinned snapshot for every record", lambda: df["snapshot_id"].nunique() == 1),
        (
            "each chip played at most once per strategy-season",
            lambda: not df.dropna(subset=["chip"]).duplicated(["season", "strategy", "chip"]).any(),
        ),
    ]
    return [{"check": name, "passed": bool(fn())} for name, fn in checks]


# ----------------------------------------------------------------------------- report


def build(
    out_dir: Path,
    rep: Path,
    ds: Any,
    schedule_rows: list[dict[str, Any]],
    bt_ref: str,
    n_boot: int,
    seed: int,
    extra: dict[str, Any],
) -> None:
    seasons = sorted(p.stem.split("_")[1] for p in out_dir.glob("backtest_*.parquet"))
    df = pd.concat(
        [pd.read_parquet(out_dir / f"backtest_{s}.parquet") for s in seasons], ignore_index=True
    )
    fc = pd.concat(
        [pd.read_parquet(out_dir / f"forecasts_{s}.parquet") for s in seasons], ignore_index=True
    )
    pm = ds["player_match"]
    lab = labels(pm[pm["season"].isin(seasons)])
    pts = lab.set_index(["season", "gw", "player_code"])["points"]
    reg = ds["players"]
    names = {
        (str(se), int(c)): str(n)
        for se, c, n in zip(reg["season"], reg["player_code"], reg["web_name"], strict=True)
    }
    df["lineup_regret"] = df["hindsight_lineup_points"] - df["raw_points"]

    summary = (
        df.groupby(["season", "strategy"])
        .agg(
            total_points=("points", "sum"),
            mean_gw=("points", "mean"),
            sd_gw=("points", "std"),
            hits=("hit_points", "sum"),
            transfers=("transfers", "sum"),
            captain_points=("captain_points", "sum"),
            bench_points=("bench_points", "sum"),
            lineup_regret=("lineup_regret", "sum"),
            validity=("valid", "mean"),
            runtime_s=("runtime_s", "mean"),
            chips=("chip", lambda c: ", ".join(f"{x}" for x in c.dropna())),
        )
        .reset_index()
    )
    agg = df.groupby("strategy").agg(
        total_points=("points", "sum"),
        hits=("hit_points", "sum"),
        transfers=("transfers", "sum"),
        validity=("valid", "mean"),
    )
    agg = agg.sort_values("total_points", ascending=False).reset_index()

    comps = []
    for other in BENCHMARKS:
        per = []
        for i, season in enumerate(seasons):
            s = df[df["season"] == season].pivot(index="gw", columns="strategy", values="points")
            d = (s[ENGINE] - s[other]).to_numpy(float)
            per.append(d)
            lo, hi = paired_ci(d, n_boot, seed + i)
            comps.append(
                {
                    "season": season,
                    "vs": other,
                    "difference": float(d.sum()),
                    "ci_low": lo,
                    "ci_high": hi,
                    "gws_better": int((d > 0).sum()),
                    "gws_worse": int((d < 0).sum()),
                    "verdict": verdict(lo, hi),
                }
            )
        lo, hi = pooled_ci(per, n_boot, seed + 100)
        allg = np.concatenate(per)
        comps.append(
            {
                "season": "all",
                "vs": other,
                "difference": float(allg.sum()),
                "ci_low": lo,
                "ci_high": hi,
                "gws_better": int((allg > 0).sum()),
                "gws_worse": int((allg < 0).sum()),
                "verdict": verdict(lo, hi),
            }
        )
    comp = pd.DataFrame(comps)

    gw_level = df.pivot_table(
        index=["season", "gw"], columns="strategy", values="points"
    ).reset_index()
    eng = df[df["strategy"] == ENGINE]
    nonchip = eng[eng["chip"].isna() & eng["expected_p10"].notna()]
    squad_cal = {
        "weeks": len(nonchip),
        "coverage_p10_p90": float(
            (
                (nonchip["raw_points"] >= nonchip["expected_p10"])
                & (nonchip["raw_points"] <= nonchip["expected_p90"])
            ).mean()
        ),
        "below_p10": float((nonchip["raw_points"] < nonchip["expected_p10"]).mean()),
        "above_p90": float((nonchip["raw_points"] > nonchip["expected_p90"]).mean()),
        "mean_expected": float(nonchip["expected_points"].mean()),
        "mean_actual": float(nonchip["raw_points"].mean()),
        "corr": float(np.corrcoef(nonchip["expected_points"], nonchip["raw_points"])[0, 1]),
    }
    tg1 = realised_transfers(df, pts, 1)
    tg4 = realised_transfers(df, pts, 4)
    pred = prediction_metrics(fc, lab)
    invalid = df[~df["valid"]][["season", "strategy", "gw", "notes"]]
    payload = {
        "snapshot_id": str(df["snapshot_id"].iloc[0]),
        "optimizer_config": str(df["optimizer_config"].iloc[0]),
        "backtest_config": bt_ref,
        "seasons": seasons,
        "summary": summary.to_dict("records"),
        "aggregate": agg.to_dict("records"),
        "engine_vs": comp.to_dict("records"),
        "gameweek_points": gw_level.to_dict("records"),
        "prediction": pred,
        "squad_calibration": squad_cal,
        "actions": actions(df),
        "chips": chips_table(df),
        "transfers_1gw": tg1.to_dict("records"),
        "transfers_4gw": tg4.to_dict("records"),
        "plan_stability": stability(df),
        "round_trips": round_trips(df),
        "accounting": accounting(df, seasons),
        "invalid_decisions": [{**r, "notes": list(r["notes"])} for r in invalid.to_dict("records")],
        "validity_rate": float(df["valid"].mean()),
        "lookahead_violations": int((df["data_age_hours"].dropna() < 0).sum()),
        "schedule_rule_s1": schedule_rows,
        "superseded_03c14a7": _superseded(),
        **extra,
    }
    (rep / "backtest.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")
    figures(df, seasons, rep / "figures")
    (rep / "backtest.md").write_text(markdown(payload, df, names, tg1, tg4) + "\n")


def figures(df: pd.DataFrame, seasons: list[str], figs: Path) -> None:
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
    fig, ax = plt.subplots(figsize=(8, 4))
    for other in ["single_gw_mc", "engine_no_chips", "hold", "fpl_style_heuristic"]:
        diffs = []
        for season in seasons:
            s = df[df["season"] == season].pivot(index="gw", columns="strategy", values="points")
            diffs.append((s[ENGINE] - s[other]).to_numpy(float))
        ax.plot(np.cumsum(np.concatenate(diffs)), label=f"engine − {other}")
    for k in range(1, len(seasons)):
        ax.axvline(38 * k - 0.5, color="grey", lw=0.5)
    ax.axhline(0, color="black", lw=0.5)
    ax.set_xlabel("gameweek (" + " | ".join(seasons) + ")")
    ax.set_ylabel("cumulative point difference")
    ax.set_title("Engine minus benchmark across three seasons", fontsize=10)
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(figs / "backtest_cumulative_difference.svg")
    plt.close(fig)


def markdown(
    p: dict[str, Any],
    df: pd.DataFrame,
    names: dict[tuple[str, int], str],
    tg1: pd.DataFrame,
    tg4: pd.DataFrame,
) -> str:
    seasons = p["seasons"]
    lines: list[str] = [
        "# Walk-forward backtest — three seasons",
        "",
        f"Seasons {', '.join(seasons)} · snapshot `{p['snapshot_id']}` (pinned in "
        f"`config/backtest/default.yaml`) · optimizer `{p['optimizer_config']}` · backtest config "
        f"`{p['backtest_config']}` · feature version `{p.get('feature_version', '?')}` · protocol "
        "`docs/BACKTEST_PROTOCOL.md` · generated by `ml/experiments/backtest.py"
        " --report-only` from "
        "the records in `data/eval/` (git-ignored; regenerate with the commands in the protocol).",
        "",
        "All strategies start each season from the same GW1 squad (initial-squad optimiser on the "
        "GW1 forecast) and are replayed through all 38 gameweeks with only information available "
        "at each decision cutoff (deadline − 90 min). Points are actual FPL points after hits, "
        "with automatic substitutions and armband rules. No overall-rank claims: the archive has "
        "no distribution of other managers' scores.",
        "",
        "> **Correction to earlier results.** The previously published report (commit `03c14a7`, "
        "archived in `ml/reports/archive/`) was produced while the fixture schedule leaked the "
        "future: archived seasons only hold "
        "the *final* schedule, so postponements (e.g. 2024-25 GW15 Everton v Liverpool, called off "
        "on match day) were visible weeks early. Schedule rule S1 (ADR-0004, "
        "`fpl_storage/schedule.py`) now shows the schedule as published at each cutoff; the "
        f"{len(p['schedule_rule_s1'])} reconstructed moves are listed in the appendix. "
        "Every number "
        "below comes from new replays under that rule; the old numbers should not be cited.",
        "",
        "## Season totals",
        "",
        "| season | strategy | points | mean/GW | SD/GW | hits | transfers | captain"
        " pts | bench pts | lineup regret | valid | s/decision | chips |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(p["summary"], key=lambda r: (r["season"], -r["total_points"])):
        lines.append(
            f"| {r['season']} | {r['strategy']} | {r['total_points']} | {r['mean_gw']:.1f} | "
            f"{r['sd_gw']:.1f} | {r['hits']} | {r['transfers']} | {r['captain_points']} | "
            f"{r['bench_points']} | {r['lineup_regret']} | {r['validity']:.1%} | "
            f"{r['runtime_s']:.1f} | {r['chips'] or '—'} |"
        )
    lines += [
        "",
        "## Aggregate over the three seasons (114 gameweeks)",
        "",
        "| strategy | points | hits | transfers | valid |",
        "|---|---|---|---|---|",
    ]
    for r in p["aggregate"]:
        lines.append(
            f"| {r['strategy']} | {r['total_points']} | {r['hits']} | {r['transfers']} | "
            f"{r['validity']:.1%} |"
        )
    lines += [
        "",
        "## Engine minus benchmark (95 % bootstrap CI)",
        "",
        "Per season: gameweeks resampled with replacement (4000 draws). `all`: season-stratified "
        "bootstrap of the 114-gameweek total. Gameweeks are treated as exchangeable, which ignores "
        "autocorrelation from shared squads (CIs are likely somewhat too narrow). 24 comparisons "
        "are shown without multiplicity correction: read"
        " single CIs that barely exclude 0 with care.",
        "",
        "| season | vs | difference | 95 % CI | GWs better | GWs worse | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in p["engine_vs"]:
        lines.append(
            f"| {r['season']} | {r['vs']} | {r['difference']:+.0f} | [{r['ci_low']:+.0f}, "
            f"{r['ci_high']:+.0f}] | {r['gws_better']} | {r['gws_worse']} | {r['verdict']} |"
        )
    old = p.get("superseded_03c14a7")
    if old:
        prev = {(r["season"], r["vs"]): r for r in old["engine_vs"]}
        lines += [
            "",
            "### What changed versus the superseded (pre-S1) report",
            "",
            "Same seasons, same strategies; the old replays could see postponements early. "
            "Verdicts that changed are in bold.",
            "",
            "| season | vs | old difference [CI] | old verdict | new difference [CI] "
            "| new verdict |",
            "|---|---|---|---|---|---|",
        ]
        for r in p["engine_vs"]:
            o = prev.get((r["season"], r["vs"]))
            if o is None:
                continue
            ov = verdict(o["ci_low"], o["ci_high"])
            changed = ov != r["verdict"]
            b = "**" if changed else ""
            lines.append(
                f"| {r['season']} | {r['vs']} | {o['difference']:+.0f} [{o['ci_low']:+.0f}, "
                f"{o['ci_high']:+.0f}] | {b}{ov}{b} | {r['difference']:+.0f} [{r['ci_low']:+.0f}, "
                f"{r['ci_high']:+.0f}] | {b}{r['verdict']}{b} |"
            )
        old_tot = {(r["season"], r["strategy"]): r["total_points"] for r in old["summary"]}
        new_tot = {(r["season"], r["strategy"]): r["total_points"] for r in p["summary"]}
        lines += [
            "",
            "Engine season totals, old → new: "
            + "; ".join(
                f"{se} {old_tot[(se, ENGINE)]} → {new_tot[(se, ENGINE)]}"
                for se in sorted({k[0] for k in old_tot})
                if (se, ENGINE) in new_tot
            )
            + ".",
        ]
    lines += ["", "![cumulative difference](figures/backtest_cumulative_difference.svg)", ""]
    lines += [*[f"![{s}](figures/backtest_cumulative_{s}.svg)" for s in seasons], ""]

    lines += [
        "## Forecasts that drove the decisions (out of sample, at each cutoff)",
        "",
        "Every player × target-gameweek forecast the replays used, joined to actual outcomes "
        "(double gameweeks summed). Integer outcomes make interval coverage discrete: "
        "*inclusive* counts p10 ≤ y ≤ p90, *strict* p10 < y < p90; a calibrated 80 % interval "
        "lies between them. Start probability = P(≥ 1 start in the gameweek).",
        "",
        "| season | horizon | n | MAE | RMSE | bias | corr | cover incl. | cover"
        " strict | width p10–p90 | start Brier | start ECE | start base rate |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in p["prediction"]["by_season_horizon"]:
        lines.append(
            f"| {r['season']} | {r['horizon']} | {r['n']} | {r['mae']:.3f} | {r['rmse']:.3f} | "
            f"{r['bias']:+.3f} | {r['corr']:.3f} | {r['coverage_p10_p90_inclusive']:.3f} | "
            f"{r['coverage_p10_p90_strict']:.3f} | {r['mean_width_p10_p90']:.2f} | "
            f"{r['start_brier']:.4f} | {r['start_ece']:.4f} | {r['start_base_rate']:.3f} |"
        )
    lines += [
        "",
        f"Forecast rows without an outcome row (player left the league): "
        f"{p['prediction']['unmatched_forecast_rows']}. Start-probability Brier at horizon 0 "
        f"vs a climatology (base-rate) forecast: see reliability table; climatology Brier "
        f"{p['prediction']['start_brier_climatology_h0']:.4f}.",
        "",
        "Start-probability reliability (horizon 0, all seasons):",
        "",
        "| bin | n | predicted | observed |",
        "|---|---|---|---|",
    ]
    for r in p["prediction"]["start_reliability_h0"]:
        lines.append(f"| {r['bin']} | {r['n']} | {r['predicted']:.3f} | {r['observed']:.3f} |")
    sc = p["squad_calibration"]
    lines += [
        "",
        f"Engine squad-level calibration ({sc['weeks']} non-chip weeks): actual points inside the "
        f"plan's simulated p10–p90 in {sc['coverage_p10_p90']:.0%} of weeks (below p10 "
        f"{sc['below_p10']:.0%}, above p90 {sc['above_p90']:.0%}); mean expected "
        f"{sc['mean_expected']:.1f} vs actual {sc['mean_actual']:.1f}; "
        f"correlation {sc['corr']:.2f}.",
        "",
        "## Recommendation outcomes (engine)",
        "",
        "| season | action | gameweeks | mean points |",
        "|---|---|---|---|",
    ]
    for r in p["actions"]:
        lines.append(f"| {r['season']} | {r['action']} | {r['gws']} | {r['points']:.1f} |")
    lines += [
        "",
        "## Chip decisions (engine)",
        "",
        "Direct chip points: bench points (Bench Boost) or the captain's points (the extra "
        "Triple Captain multiple). Wildcard / Free Hit value has no direct counterpart; the "
        "same-week score of `engine_no_chips` (a different squad by then) is a rough reference.",
        "",
        "| season | GW | chip | points | direct chip points | engine_no_chips same GW |",
        "|---|---|---|---|---|---|",
    ]
    for r in p["chips"]:
        lines.append(
            f"| {r['season']} | {r['gw']} | {r['chip']} | {r['points']} | "
            f"{r['direct_chip_points'] if r['direct_chip_points'] is not None else '—'} | "
            f"{r['engine_no_chips_same_gw']} |"
        )
    if len(tg4):
        e4, e1 = tg4[tg4["strategy"] == ENGINE], tg1[tg1["strategy"] == ENGINE]
        lines += [
            "",
            "## Engine transfers — every one, successful and failed (hindsight diagnostic)",
            "",
            f"{len(e4)} transfer weeks; realised gain over 1 GW: mean "
            f"{e1['gain_1gw'].mean():+.2f}, "
            f"positive in {(e1['gain_1gw'] > 0).mean():.0%}; over 4 GWs: mean "
            f"{e4['gain_4gw'].mean():+.2f}, positive in "
            f"{(e4['gain_4gw'] > 0).mean():.0%}. Realised "
            "gain = actual points of players in − players out (− hit); ignores lineup effects.",
            "",
            "| season | GW | out | in | hit | gain 1 GW | gain 4 GW |",
            "|---|---|---|---|---|---|---|",
        ]
        m = e4.merge(e1[["season", "gw", "gain_1gw"]], on=["season", "gw"], how="left")

        def who(season: str, codes: list[int]) -> str:
            return ", ".join(names.get((season, int(c)), str(c)) for c in codes)

        for r in m.itertuples():
            lines.append(
                f"| {r.season} | {r.gw} | {who(r.season, r.sold)} | {who(r.season, r.bought)} | "
                f"{r.hit} | {r.gain_1gw:+d} | {r.gain_4gw:+d} |"
            )
    lines += [
        "",
        "## Plan stability (engine)",
        "",
        "| season | plans | next-week plan changed | planned hold kept |"
        " planned move executed exactly | incoming overlap (Jaccard) |",
        "|---|---|---|---|---|---|",
    ]
    for r in p["plan_stability"]:
        f = lambda v: "—" if v is None else f"{v:.0%}"  # noqa: E731
        lines.append(
            f"| {r['season']} | {r['plans']} | {f(r['changed_share'])} | "
            f"{f(r['planned_hold_kept'])} | "
            f"{f(r['planned_move_executed_exactly'])} | {f(r['incoming_overlap'])} |"
        )
    trips = pd.DataFrame(p["round_trips"]).pivot(
        index="strategy", columns="season", values="round_trips"
    )
    lines += [
        "",
        "Round trips (a player sold ≤ 3 GWs after being bought; executed actions):",
        "",
        "| strategy | " + " | ".join(trips.columns) + " |",
        "|---|" + "---|" * len(trips.columns),
    ]
    for strat, row in trips.iterrows():
        lines.append(f"| {strat} | " + " | ".join(str(int(v)) for v in row) + " |")
    lines += [
        "",
        "## Validity, accounting and integrity checks",
        "",
        f"Decisions legal under the state machine: {p['validity_rate']:.2%}; look-ahead violations "
        f"(data newer than the cutoff): {p['lookahead_violations']}.",
        "",
        "| check | result |",
        "|---|---|",
    ]
    for c in p["accounting"]:
        lines.append(f"| {c['check']} | {'pass' if c['passed'] else '**FAIL**'} |")
    for r in p["invalid_decisions"]:
        lines.append(
            f"\nInvalid decision: {r['season']} {r['strategy']} GW{r['gw']}: "
            f"{'; '.join(r['notes'])}"
        )
    if p.get("reproducibility"):
        rp = p["reproducibility"]
        lines += ["", "## Reproducibility", "", rp["summary"], ""]
        for r in rp["by_season_strategy"]:
            lines.append(f"* {r['season']} {r['strategy']}: {r['status']}")
    if p.get("runtime"):
        lines += ["", "## Runtime", "", p["runtime"]]
    lines += [
        "",
        "## Appendix — schedule rule S1: fixtures moved after the schedule was published",
        "",
        "Each fixture is shown in its original round until `known_at` (the original round's "
        "deadline, or the announcement of its new date if earlier), hidden until `announced_at`, "
        "then shown in its final gameweek. Original rounds are recovered from the final "
        "schedule by an exact, uniqueness-checked assignment (every team plays once per round).",
        "",
        "| season | fixture | original GW | final GW | known at | announced at |",
        "|---|---|---|---|---|---|",
    ]
    for r in p["schedule_rule_s1"]:
        lines.append(
            f"| {r['season']} | {r['fixture']} | {r['original_gw']} | {r['final_gw']} | "
            f"{r['known_at'][:16]} | {r['announced_at'][:16]} |"
        )
    return "\n".join(lines)
