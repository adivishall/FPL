"""Alert rules (§74) replayed on real data — ml/reports/alerts_audit.{md,json}.

The production rule functions (``fpl_notifications.rules``) are fed with real inputs instead of
synthetic ones:

* squads: the ``engine_no_chips`` walk-forward replays of 2023-24, 2024-25, 2025-26;
* role change / unexpected benching: P(start) and expected points from the forecasts the replay
  actually used at consecutive cutoffs, and each player's real start history (finished matches
  before the cutoff only);
* fixture changes: the schedule as published at consecutive cutoffs (rule S1), so real
  postponements, blanks and doubles reach the planned squad's clubs;
* price risk: the engine's calibrated rise/fall probabilities at the cutoff for the replay's
  real transfers (plan assumed to spend the whole bank — the tightest case);
* post-gameweek review: forecast vs actual points of the fielded XI;
* deadline reminders: the official deadlines captured from the live FPL API, in three time
  zones (including a daylight-saving transition).

Dedupe is checked by re-evaluating unchanged inputs; the materiality filter by re-running with
thresholds set to zero. Historical seasons have no injury news, so the availability path
(status/chance drops) can only be exercised on live captures and in the unit tests.

Usage: uv run python ml/experiments/alerts_audit.py [--live-snapshot DIR]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from pinned import pinned_snapshot

from fpl_api.services import DataService, PriceService
from fpl_api.settings import Settings
from fpl_forecasting.walkforward import cutoffs
from fpl_notifications.rules import (
    PlayerOutcome,
    PlayerStatus,
    Sale,
    deadline_reminder,
    fixture_changes,
    load_notification_config,
    post_gameweek_report,
    price_risk,
    squad_changes,
)
from fpl_notifications.store import UnsafeUrl, validate_webhook_url
from fpl_storage.dataset import load_snapshot
from fpl_storage.pit import PointInTimeView

ROOT = Path(__file__).resolve().parents[2]
REP = ROOT / "ml" / "reports"
SEASONS = ("2023-24", "2024-25", "2025-26")
STRAT = "engine_no_chips"
PRICE_SAMPLE = 6  # transfer weeks per season with the price model retrained at the cutoff


def _squad_before(r: Any) -> list[int]:
    s = {int(c) for c in [*r.starters, *r.bench]}
    return sorted((s - {int(c) for c in r.transfers_in}) | {int(c) for c in r.transfers_out})


def _fixture_counts(view: PointInTimeView, season: str, gws: range) -> dict[tuple[int, int], int]:
    f = view.fixtures(season)
    f = f[f["gw"].isin(list(gws))]
    out: Counter[tuple[int, int]] = Counter()
    for r in f.itertuples():
        out[(int(r.home_team_code), int(r.gw))] += 1
        out[(int(r.away_team_code), int(r.gw))] += 1
    return dict(out)


def _status(
    fc: pd.DataFrame, spm: pd.DataFrame, cuts: dict[int, pd.Timestamp], g: int, c: int
) -> PlayerStatus:
    """What the data plane knew about player ``c`` at the GW ``g`` cutoff (no news history)."""
    f = fc[(fc["decision_gw"] == g) & (fc["player_code"] == c)]
    nxt = f[f["gw"] == g]
    hist = spm[(spm["player_code"] == c) & (spm["available_at"] <= cuts[g])]
    return PlayerStatus(
        status="a",
        start_prob=float(nxt["prob_start"].iloc[0]) if len(nxt) else None,
        xp_next=float(nxt["mean"].sum()),
        xp=float(f["mean"].sum()),
        recent_starts=tuple(bool(x) for x in (hist["starts"].fillna(0) > 0)),
    )


def main() -> None:
    args = sys.argv[1:]
    live = Path(args[args.index("--live-snapshot") + 1]) if "--live-snapshot" in args else None
    ds = load_snapshot(pinned_snapshot())
    cfg = load_notification_config()
    zero = cfg.model_copy(
        update={
            "squad_change": cfg.squad_change.model_copy(update={"min_points_impact": 0.0}),
            "fixture_change": cfg.fixture_change.model_copy(update={"min_points_impact": 0.0}),
        }
    )
    pm = ds["player_match"]
    teams = ds["teams"]
    prices = PriceService(Settings(), DataService(Settings(), ds))
    alerts: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    unfiltered: Counter[str] = Counter()
    dedupe_ok = True
    post: list[dict[str, Any]] = []
    price_rows: list[dict[str, Any]] = []
    for season in SEASONS:
        rec = pd.read_parquet(ROOT / "data" / "eval" / f"backtest_{season}.parquet")
        rec = rec[rec["strategy"] == STRAT].set_index("gw")
        fc = pd.read_parquet(ROOT / "data" / "eval" / f"forecasts_{season}.parquet")
        cuts = {c.gw: c.cutoff for c in cutoffs(ds, [season])}
        names = dict(
            zip(
                ds["players"].loc[ds["players"]["season"] == season, "player_code"],
                ds["players"].loc[ds["players"]["season"] == season, "web_name"],
                strict=True,
            )
        )
        tnames = dict(
            zip(
                teams.loc[teams["season"] == season, "team_code"],
                teams.loc[teams["season"] == season, "short_name"],
                strict=True,
            )
        )
        spm = pm[pm["season"] == season].sort_values("kickoff_at")
        price_weeks = 0
        for gw in range(2, 39):
            r, prev = rec.loc[gw], rec.loc[gw - 1]
            owned = _squad_before(r)
            starters = [int(c) for c in prev.starters]

            before = {c: _status(fc, spm, cuts, gw - 1, c) for c in owned}
            after = {c: _status(fc, spm, cuts, gw, c) for c in owned}
            got = squad_changes(season, gw, owned, starters, before, after, cfg.squad_change, names)
            again = squad_changes(
                season, gw, owned, starters, before, after, cfg.squad_change, names
            )
            dedupe_ok &= [a.dedupe_key for a in got] == [a.dedupe_key for a in again]
            unfiltered.update(
                a.evidence.get("subtype", a.kind)
                for a in squad_changes(
                    season, gw, owned, starters, before, after, zero.squad_change, names
                )
            )
            # fixture changes inside the plan horizon between consecutive cutoffs
            horizon = range(gw, min(gw + 5, 39))
            b_cnt = _fixture_counts(PointInTimeView(ds, cuts[gw - 1]), season, horizon)
            a_cnt = _fixture_counts(PointInTimeView(ds, cuts[gw]), season, horizon)
            team_of = {
                int(c): int(t)
                for c, t in zip(spm["player_code"], spm["team_code"], strict=True)
                if int(c) in owned
            }
            xpm = {c: before[c].xp_next for c in owned}
            fx = fixture_changes(
                season,
                f"plan:{gw}",
                list(horizon),
                owned,
                team_of,
                b_cnt,
                a_cnt,
                xpm,
                cfg.fixture_change,
                names,
                tnames,
            )
            unfiltered.update(
                "fixture_change"
                for _ in fixture_changes(
                    season,
                    f"plan:{gw}",
                    list(horizon),
                    owned,
                    team_of,
                    b_cnt,
                    a_cnt,
                    xpm,
                    zero.fixture_change,
                    names,
                    tnames,
                )
            )
            # price risk for real transfers (tightest bank), a sample of transfer weeks
            pr: list[Any] = []
            if len(r.transfers_in) and price_weeks < PRICE_SAMPLE:
                price_weeks += 1
                probs = prices.probabilities(season, gw, cuts[gw].to_pydatetime())
                pool = (
                    PointInTimeView(ds, cuts[gw]).player_pool(season, gw).set_index("player_code")
                )
                cur = {int(c): int(p) for c, p in pool["price"].dropna().items()}
                buys = {int(c): cur[int(c)] for c in r.transfers_in if int(c) in cur}
                sales = [
                    Sale(int(c), cur.get(int(c), 0), cur.get(int(c), 0)) for c in r.transfers_out
                ]
                p_up = dict(zip(probs["player_code"], probs["p_rise"], strict=True))
                p_dn = dict(zip(probs["player_code"], probs["p_fall"], strict=True))
                pr = price_risk(season, gw, buys, sales, 0, p_up, p_dn, cfg.price_risk, names=names)
                price_rows.append(
                    {
                        "season": season,
                        "gw": gw,
                        "buys": [names.get(c, c) for c in buys],
                        "max_p_rise_buy": max((p_up.get(c, 0.0) for c in buys), default=0.0),
                        "alert": bool(pr),
                        "probability": pr[0].materiality if pr else None,
                    }
                )
            # post-gameweek review of the fielded XI
            f0 = fc[(fc["decision_gw"] == gw) & (fc["gw"] == gw)].set_index("player_code")
            act = spm[spm["gw"] == gw].groupby("player_code")[["points", "minutes"]].sum()
            outs = []
            bench = {int(x) for x in r.bench}
            for raw in [*r.starters, *r.bench]:
                c = int(raw)
                mult = 0 if c in bench else (2 if c == int(r.captain) else 1)
                e = f0.loc[c] if c in f0.index else None
                outs.append(
                    PlayerOutcome(
                        player_code=c,
                        starter=mult > 0,
                        multiplier=mult,
                        expected=float(e["mean"]) if e is not None else 0.0,
                        p10=float(e["p10"]) if e is not None else 0.0,
                        p90=float(e["p90"]) if e is not None else 0.0,
                        prob_play=float(e["prob_play"]) if e is not None else 0.0,
                        actual=int(act["points"].get(c, 0)),
                        minutes=int(act["minutes"].get(c, 0)),
                    )
                )
            rev = post_gameweek_report(
                season,
                gw,
                outs,
                float(r.expected_points),
                float(r.expected_p10),
                float(r.expected_p90),
                int(r.raw_points),
                cfg.post_gameweek,
                names,
            )
            post.append(
                {
                    "season": season,
                    "gw": gw,
                    "items": rev[0].body.count(";") + 1 if "Largest misses" in rev[0].body else 0,
                }
            )
            for a in [*got, *fx, *pr, *rev]:
                kind = a.evidence.get("subtype", a.kind)
                counts[kind] += 1
                if kind != "post_gameweek":
                    alerts.append(
                        {
                            "season": season,
                            "gw": gw,
                            "kind": kind,
                            "title": a.title,
                            "materiality": a.materiality,
                            "dedupe_key": a.dedupe_key,
                        }
                    )
    keys = [a["dedupe_key"] for a in alerts]
    deadline = deadline_checks(live, cfg) if live else []
    webhook = []
    for url, hosts in (
        ("https://hooks.invalid/x", ["hooks.invalid"]),  # .invalid never resolves (RFC 6761)
        ("https://localhost/x", ["localhost"]),
        ("https://10.1.2.3/x", ["10.1.2.3"]),
        ("http://example.org/x", ["example.org"]),
    ):
        try:
            validate_webhook_url(url, hosts)
            webhook.append({"url": url, "result": "accepted"})
        except UnsafeUrl as exc:
            webhook.append({"url": url, "result": f"rejected: {exc}"})
    payload = {
        "squads": STRAT,
        "seasons": list(SEASONS),
        "counts": dict(counts),
        "unfiltered_candidates": dict(unfiltered),
        "dedupe_reevaluation_identical": bool(dedupe_ok),
        "unique_dedupe_keys": len(set(keys)) == len(keys),
        "alerts": alerts,
        "price_risk": price_rows,
        "post_gameweek_with_explained_misses": sum(1 for p in post if p["items"]),
        "post_gameweek_reviews": len(post),
        "deadline": deadline,
        "webhook_failure_paths": webhook,
    }
    (REP / "alerts_audit.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")
    (REP / "alerts_audit.md").write_text(markdown(payload) + "\n")
    print(markdown(payload))


def deadline_checks(live: Path, cfg: Any) -> list[dict[str, Any]]:
    lds = load_snapshot(live)
    g = lds["gameweeks"]
    g = g[(g["season"] == g["season"].max()) & (g["deadline_source"] == "official")]
    rows = []
    upcoming = g[g["status"] == "upcoming"].sort_values("gw")
    # the next deadline and the first one after the UK clocks go back (BST → GMT)
    picks = [upcoming.iloc[0]]
    after_dst = upcoming[upcoming["deadline_at"] > pd.Timestamp("2026-10-25T01:00Z")]
    if len(after_dst):
        picks.append(after_dst.iloc[0])
    for row in picks:
        dl = pd.Timestamp(row["deadline_at"]).to_pydatetime()
        for tz in ("Europe/London", "Asia/Kolkata", "America/New_York"):
            for lead in (30, 20, 20, 1.5, -0.1):
                now = dl - timedelta(hours=lead)
                out = deadline_reminder(row["season"], int(row["gw"]), dl, now, cfg.deadline, tz)
                rows.append(
                    {
                        "gw": int(row["gw"]),
                        "deadline_utc": dl.isoformat(),
                        "timezone": tz,
                        "hours_before": lead,
                        "alert": out[0].title if out else None,
                        "local": out[0].evidence["deadline_local"] if out else None,
                        "dedupe_key": out[0].dedupe_key if out else None,
                    }
                )
    return rows


def markdown(p: dict[str, Any]) -> str:
    c, u = p["counts"], p["unfiltered_candidates"]
    lines = [
        "# Alert rules on real data (§74)",
        "",
        f"Production rule functions fed with real inputs from the `{p['squads']}` walk-forward "
        f"replays of {', '.join(p['seasons'])} (see the module docstring of "
        "`ml/experiments/alerts_audit.py` for every input's provenance). Thresholds: "
        "`config/notifications/default.yaml`.",
        "",
        "| alert | fired | candidates before the materiality filter |",
        "|---|---|---|",
    ]
    for k in (
        "role_change",
        "unexpected_benching",
        "fixture_change",
        "price_risk",
        "post_gameweek",
    ):
        lines.append(f"| {k} | {c.get(k, 0)} | {u.get(k, '—') if k in u else '—'} |")
    lines += [
        "",
        f"* Re-evaluating unchanged inputs produced identical dedupe keys: "
        f"{'yes' if p['dedupe_reevaluation_identical'] else '**no**'}; every fired alert has a "
        f"distinct key: {'yes' if p['unique_dedupe_keys'] else '**no**'} (the store's unique "
        "constraint turns repeats into no-ops).",
        f"* Post-gameweek reviews: {p['post_gameweek_reviews']}; "
        f"{p['post_gameweek_with_explained_misses']} name at least one material miss.",
        "* Candidate counts are per alert type with every threshold at zero. One alert per "
        "player and week: a role change takes precedence over benching, so with the filter off "
        "some benched players are reported as role changes instead and the benching candidate "
        "count can be lower than the number fired.",
        "* Availability (injury / suspension / doubt) alerts need status history, which the "
        "archive does not have: exercised only on live captures and in unit tests.",
        "",
        "## Examples (first 15 non-review alerts)",
        "",
        "| season | GW | kind | title | materiality |",
        "|---|---|---|---|---|",
    ]
    for a in p["alerts"][:15]:
        lines.append(
            f"| {a['season']} | {a['gw']} | {a['kind']} | {a['title']} | {a['materiality']:.2f} |"
        )
    fx = [a for a in p["alerts"] if a["kind"] == "fixture_change"]
    if fx:
        lines += ["", "Fixture-change alerts (real schedule changes reaching the squad):", ""]
        lines += [f"* {a['season']} GW{a['gw']}: {a['title']}" for a in fx[:12]]
    lines += [
        "",
        "## Price risk on real transfers (plan spends the whole bank)",
        "",
        "| season | GW | buys | max P(rise) of a buy | alert | P(unaffordable) |",
        "|---|---|---|---|---|---|",
    ]
    for r in p["price_risk"]:
        pr = "—" if r["probability"] is None else f"{r['probability']:.2f}"
        lines.append(
            f"| {r['season']} | {r['gw']} | {', '.join(map(str, r['buys']))} | "
            f"{r['max_p_rise_buy']:.2f} | {'yes' if r['alert'] else 'no'} | {pr} |"
        )
    if p["deadline"]:
        lines += [
            "",
            "## Deadline reminders (official deadlines from the live FPL API)",
            "",
            "The 20 h check runs twice on purpose: the repeat yields the same dedupe key, so the "
            "store sends the reminder once. The second deadline is the first after UK clocks go "
            "back (BST → GMT).",
            "",
            "| GW | deadline (UTC) | time zone | hours before | alert | local time | dedupe key |",
            "|---|---|---|---|---|---|---|",
        ]
        for d in p["deadline"]:
            lines.append(
                f"| {d['gw']} | {d['deadline_utc'][:16]} | {d['timezone']} | "
                f"{d['hours_before']} | {d['alert'] or '—'} | {(d['local'] or '—')[:25]} | "
                f"{d['dedupe_key'] or '—'} |"
            )
    lines += ["", "## Webhook failure paths (no request is sent)", ""]
    lines += [f"* `{w['url']}` → {w['result']}" for w in p["webhook_failure_paths"]]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
