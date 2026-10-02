"""Alert evaluation for one manager (§74): assemble rule inputs from the point-in-time data plane.

A recommendation stores a *watch* block — the inputs it was built on (player availability and
start probabilities, expected points, fixture counts per team × gameweek, prices, the planned
buys/sells and the bank after the plan). Evaluating alerts recomputes the same inputs at the
current cutoff and hands both to the pure rules in :mod:`fpl_notifications.rules`; the rules
apply the materiality thresholds and the store de-duplicates. Nothing here invents a change:
with an unchanged snapshot the inputs are identical and no change alert can fire.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import numpy as np
import structlog

from fpl_api.container import AppServices, optimization_problem
from fpl_api.services import CurrentContext, ForecastUnavailable
from fpl_api.watch import fixture_counts, player_statuses
from fpl_decision.paired import paired_gain, plan_samples
from fpl_domain.enums import ChipType, Position
from fpl_domain.hashing import short_id
from fpl_domain.squad import Lineup
from fpl_domain.state import ManagerState
from fpl_forecasting.pipeline import Forecast
from fpl_notifications.rules import (
    Alert,
    NotificationConfig,
    PlayerOutcome,
    PlayerStatus,
    Sale,
    deadline_reminder,
    fixture_changes,
    load_notification_config,
    post_gameweek_report,
    price_risk,
    recommendation_invalidation,
    squad_changes,
)
from fpl_notifications.store import UnsafeUrl, WebhookChannel
from fpl_optimizer.alternatives import hold_problem
from fpl_optimizer.milp import OptimizationError, build_and_solve
from fpl_optimizer.problem import Preferences
from fpl_simulation.lineup_eval import score_lineup
from fpl_storage import models as m
from fpl_storage.db import session_scope

log = structlog.get_logger("fpl_api.alerts")
COMPONENTS = ("goals", "assists", "clean_sheets", "bonus", "saves", "yellow_cards", "red_cards")


def _statuses_from_json(d: dict[str, Any]) -> dict[int, PlayerStatus]:
    return {
        int(c): PlayerStatus(**{**v, "recent_starts": tuple(v.get("recent_starts", ()))})
        for c, v in d.items()
    }


# ----------------------------------------------------------------------------- evaluation


def manager_settings(svc: AppServices, manager_key: str) -> dict[str, Any]:
    if svc.engine is None:
        return {}
    with session_scope(svc.engine) as s:
        row = s.get(m.ManagerSettingsRow, manager_key)
        return dict(row.settings_json) if row else {}


def evaluate_alerts(
    svc: AppServices,
    manager_key: str,
    now: datetime | None = None,
    cfg: NotificationConfig | None = None,
    deliver: bool = True,
) -> dict[str, Any]:
    """Run every §74 rule for a manager; record new alerts (de-duplicated) and deliver them."""
    if svc.states is None or svc.recs is None or svc.notifications is None:
        raise RuntimeError("database required for notifications")
    cfg = cfg or load_notification_config()
    now = now or datetime.now(UTC)
    ctx = svc.data.current(now)
    prefs = manager_settings(svc, manager_key)
    names = svc.data.names(ctx.season)
    alerts: list[Alert] = []
    skipped: dict[str, str] = {}

    rec = svc.recs.current(manager_key, ctx.gameweek)
    pending = rec["explanation"]["decision"] if rec else None
    alerts += deadline_reminder(
        ctx.season,
        ctx.gameweek,
        ctx.deadline,
        now,
        cfg.deadline,
        timezone=prefs.get("timezone"),
        pending=pending,
    )

    latest = svc.states.latest(manager_key)
    watch = (rec or {}).get("watch") or {}
    if latest is None:
        skipped["plan_rules"] = "no squad stored"
    elif not watch:
        skipped["plan_rules"] = "no active recommendation for the current gameweek"
    else:
        try:
            alerts += _plan_alerts(
                svc, ctx, latest[1], rec or {}, watch, cfg, prefs, names, skipped
            )
        except ForecastUnavailable:
            skipped["plan_rules"] = "forecast for the current cutoff not computed yet"
    try:
        alerts += _post_gameweek(svc, ctx, manager_key, cfg, names, skipped)
    except ForecastUnavailable:
        skipped["post_gameweek"] = "forecast for the finished gameweek not computed yet"

    new = svc.notifications.record(manager_key, alerts, cfg.config_ref)
    delivered = _deliver(svc, manager_key, cfg, prefs, alerts, new) if deliver else []
    return {
        "manager_key": manager_key,
        "evaluated_at": now.isoformat(),
        "season": ctx.season,
        "gameweek": ctx.gameweek,
        "config_ref": cfg.config_ref,
        "alerts": [asdict(a) for a in alerts],
        "new_ids": new,
        "delivered_ids": delivered,
        "skipped": skipped,
        "freshness": ctx.block(svc.data.snapshot_id),
    }


def _plan_alerts(
    svc: AppServices,
    ctx: CurrentContext,
    state: ManagerState,
    rec: dict[str, Any],
    watch: dict[str, Any],
    cfg: NotificationConfig,
    prefs: dict[str, Any],
    names: dict[int, str],
    skipped: dict[str, str],
) -> list[Alert]:
    out: list[Alert] = []
    gws = [g for g in watch["gameweeks"] if g >= ctx.gameweek]
    if not gws:
        skipped["plan_rules"] = "the saved plan's gameweeks have passed"
        return out
    fc = svc.forecasts.get(ctx.season, ctx.gameweek, len(gws), compute=None)
    codes = sorted(int(c) for c in watch["players"])
    before = _statuses_from_json(watch["players"])
    after = player_statuses(svc, ctx, fc, codes, gws, cfg.squad_change.benching_window)
    starters = rec.get("lineup", {}).get("starters", [])
    if prefs.get("notify_injuries", True):
        out += squad_changes(
            ctx.season,
            ctx.gameweek,
            list(state.codes),
            starters,
            before,
            after,
            cfg.squad_change,
            names,
        )
    else:
        skipped["squad_change"] = "disabled in manager settings"

    team_of = {int(c): int(t) for c, t in watch["team_of"].items()}
    old_fx = {
        (int(k.split(":")[0]), int(k.split(":")[1])): int(n) for k, n in watch["fixtures"].items()
    }
    out += fixture_changes(
        ctx.season,
        rec["id"],
        gws,
        codes,
        team_of,
        old_fx,
        fixture_counts(svc, ctx, gws),
        {int(c): float(v) for c, v in watch["xp_per_match"].items()},
        cfg.fixture_change,
        names,
        svc.data.teams(ctx.season),
    )

    probs = svc.price_probs(ctx)
    if probs is None or probs.empty:
        skipped["price_risk"] = "price-change probabilities unavailable"
    elif watch["buys"] or watch["sells"]:
        pp = probs.set_index("player_code")
        pool = svc.data.view(ctx.cutoff).player_pool(ctx.season, ctx.gameweek)
        price_now = dict(
            zip(pool["player_code"].astype(int), pool["price"].astype(int), strict=True)
        )
        held = {p.player_code: p for p in state.squad}
        sales = [
            Sale(c, held[c].purchase_price, price_now.get(c, held[c].purchase_price))
            for c in watch["sells"]
            if c in held
        ]
        # cost changes already realised since the plan was made eat into the bank first
        realised = sum(
            price_now.get(c, watch["prices"].get(str(c), 0)) - watch["prices"].get(str(c), 0)
            for c in watch["buys"]
        )
        out += price_risk(
            ctx.season,
            ctx.gameweek,
            {c: price_now.get(c, 0) for c in watch["buys"]},
            sales,
            int(watch["bank_after"]) - int(realised),
            pp["p_rise"].to_dict(),
            pp["p_fall"].to_dict(),
            cfg.price_risk,
            svc.data.ruleset(ctx.season).pricing.selling_price_rule,
            names,
        )

    if fc.run_id == watch.get("prediction_run_id"):
        skipped["invalidation"] = "no new information since the recommendation was generated"
    else:
        out += _invalidation(svc, ctx, fc, state, rec, len(gws), cfg, prefs, names)
    return out


def _invalidation(
    svc: AppServices,
    ctx: CurrentContext,
    fc: Forecast,
    state: ManagerState,
    rec: dict[str, Any],
    horizon: int,
    cfg: NotificationConfig,
    prefs: dict[str, Any],
    names: dict[int, str],
) -> list[Alert]:
    """Re-solve on the fresh forecast: best plan vs the saved first-GW move, paired vs hold.

    The saved move is re-evaluated as "these transfers this gameweek, best continuation after"
    (forced_in/forced_out), so its value is if anything optimistic: the regret is a lower bound
    and the rule errs towards silence rather than spurious alerts.
    """
    profile = prefs.get("profile", "default")
    prob = optimization_problem(svc, ctx, fc, state, horizon, profile)
    first = rec["chosen"]["timeline"][0]
    saved = prob
    if rec["decision"]["action"] == "HOLD":
        saved = hold_problem(prob)
    else:
        saved = optimization_problem(
            svc,
            ctx,
            fc,
            state,
            horizon,
            profile,
            Preferences(
                forced_out=frozenset(first["transfers_out"]),
                forced_in=frozenset(first["transfers_in"]),
            ),
        )
    try:
        best_sol = build_and_solve(prob)
        hold_sol = build_and_solve(hold_problem(prob))
        saved_sol = build_and_solve(saved)
    except OptimizationError as exc:  # e.g. a planned buy left the game
        return recommendation_invalidation(
            ctx.season,
            ctx.gameweek,
            rec["id"],
            rec["decision"]["action"],
            -1e9,
            0.0,
            0.0,
            "a feasible re-plan",
            cfg.invalidation,
            [f"saved move infeasible: {exc}"],
        )
    pos = prob.players.positions_map()
    rs = prob.ruleset
    hs = plan_samples(fc.simulation, hold_sol.plans, pos, rs)
    best = paired_gain(plan_samples(fc.simulation, best_sol.plans, pos, rs), hs)
    sv = paired_gain(plan_samples(fc.simulation, saved_sol.plans, pos, rs), hs)
    inv = cfg.invalidation
    if prefs.get("notify_min_gain") is not None:  # the manager's own materiality threshold
        inv = inv.model_copy(update={"min_gain_points": float(prefs["notify_min_gain"])})
    step = best_sol.plans[0]
    desc = (
        "hold"
        if not step.transfers_in
        else "transfer "
        + ", ".join(names.get(c, str(c)) for c in step.transfers_out)
        + " → "
        + ", ".join(names.get(c, str(c)) for c in step.transfers_in)
    )
    return recommendation_invalidation(
        ctx.season,
        ctx.gameweek,
        rec["id"],
        rec["decision"]["action"],
        sv.mean,
        sv.probability_positive,
        best.mean,
        desc,
        inv,
        [f"forecast {rec.get('watch', {}).get('prediction_run_id')} → {fc.run_id}"],
    )


def _post_gameweek(
    svc: AppServices,
    ctx: CurrentContext,
    manager_key: str,
    cfg: NotificationConfig,
    names: dict[int, str],
    skipped: dict[str, str],
) -> list[Alert]:
    """Forecast vs actual for the last finished gameweek's recommended lineup."""
    assert svc.recs is not None
    gw = ctx.gameweek - 1
    rec = svc.recs.latest_for_gameweek(manager_key, gw) if gw >= 1 else None
    if rec is None:
        skipped["post_gameweek"] = f"no recommendation stored for GW{gw}"
        return []
    view = svc.data.view(ctx.cutoff)
    pm = view.player_match([ctx.season])
    pm = pm[pm["gw"] == gw]
    if pm.empty:
        skipped["post_gameweek"] = f"GW{gw} results not final yet"
        return []
    fc = svc.forecasts.get(ctx.season, gw, svc.settings.forecast_horizon, compute=None)
    lu = rec["lineup"]
    chip = rec["chosen"]["timeline"][0].get("chip")
    chip_t = ChipType(chip.rsplit("_", 1)[0]) if chip else None
    lineup = Lineup(
        starters=tuple(lu["starters"]),
        bench=tuple(lu["bench"]),
        captain=lu["captain"],
        vice_captain=lu["vice_captain"],
    )
    act = pm.groupby("player_code")[["points", "minutes", *COMPONENTS]].sum()
    summ = fc.summary[fc.summary["gw"] == gw].set_index("player_code")
    rs = svc.data.ruleset(ctx.season)
    players = svc.data.ds["players"]
    players = players[players["season"] == ctx.season].set_index("player_code")["position"]
    squad = [*lineup.starters, *lineup.bench]
    pos = {c: Position(players[c]) for c in squad}
    one = {c: np.array([int(act["points"].get(c, 0))]) for c in squad}
    mins = {c: np.array([int(act["minutes"].get(c, 0))]) for c in squad}
    total = int(score_lineup(lineup, pos, one, mins, rs, chip_t)[0])
    total -= int(rec["chosen"]["timeline"][0]["hit_points"])
    cap_mult = 3 if chip_t is ChipType.TRIPLE_CAPTAIN else 2
    outcomes = []
    for c in squad:
        mult = (
            cap_mult
            if c == lineup.captain
            else (1 if c in lineup.starters or chip_t is ChipType.BENCH_BOOST else 0)
        )
        r = summ.loc[c] if c in summ.index else None
        outcomes.append(
            PlayerOutcome(
                player_code=c,
                starter=c in lineup.starters,
                multiplier=mult,
                expected=float(r["mean"]) if r is not None else 0.0,
                p10=float(r["p10"]) if r is not None else 0.0,
                p90=float(r["p90"]) if r is not None else 0.0,
                prob_play=float(r["prob_play"]) if r is not None else 0.0,
                actual=int(act["points"].get(c, 0)),
                minutes=int(act["minutes"].get(c, 0)),
                components={k: float(act[k].get(c, 0)) for k in COMPONENTS},
            )
        )
    d = rec["decision"]
    return post_gameweek_report(
        ctx.season,
        gw,
        outcomes,
        float(rec["chosen"]["timeline"][0]["expected_points"]),
        float(d.get("p10", 0.0)),
        float(d.get("p90", 0.0)),
        total,
        cfg.post_gameweek,
        names,
    )


def _deliver(
    svc: AppServices,
    manager_key: str,
    cfg: NotificationConfig,
    prefs: dict[str, Any],
    alerts: list[Alert],
    new_ids: list[str],
) -> list[str]:
    url = prefs.get("webhook_url")
    if not url or not new_ids:
        return []
    assert svc.notifications is not None
    try:
        hosts = [*cfg.delivery.webhook_allowed_hosts, *svc.settings.webhook_allowed_hosts]
        ch = WebhookChannel(url, hosts, cfg.delivery.webhook_timeout_seconds)
    except UnsafeUrl as exc:
        log.warning("security_event", kind="webhook_rejected", reason=str(exc))
        svc.metrics.security_events.labels("webhook_rejected").inc()
        return []
    by_id = {short_id("ntf", {"manager": manager_key, "key": a.dedupe_key}): a for a in alerts}
    sent = [i for i in new_ids if i in by_id and ch.send(manager_key, by_id[i])]
    svc.notifications.mark(manager_key, sent, "delivered_at")
    return sent
