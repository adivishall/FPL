"""Plan *watch* baseline (§74): the inputs a recommendation was built on, at a cutoff.

Shared by the recommendation workflow (which stores the baseline) and alert evaluation (which
recomputes it at the current cutoff and compares).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from fpl_api.services import CurrentContext
from fpl_decision.engine import RecommendationPackage
from fpl_domain.state import ManagerState
from fpl_forecasting.pipeline import Forecast
from fpl_notifications.rules import PlayerStatus, load_notification_config

if TYPE_CHECKING:
    from fpl_api.container import AppServices


def _num(v: Any) -> float | None:
    return None if v is None or pd.isna(v) else float(v)


def player_statuses(
    svc: AppServices,
    ctx: CurrentContext,
    fc: Forecast,
    codes: list[int],
    gameweeks: list[int],
    window: int,
) -> dict[int, PlayerStatus]:
    view = svc.data.view(ctx.cutoff)
    pool = view.player_pool(ctx.season, ctx.gameweek).set_index("player_code")
    news = view.latest_snapshot(ctx.season)
    news = news.set_index("player_code")["news"] if len(news) else pd.Series(dtype=str)
    summ = fc.summary
    nxt = summ[summ["gw"] == gameweeks[0]].set_index("player_code")
    hor = summ[summ["gw"].isin(gameweeks)].groupby("player_code")["mean"].sum()
    seasons = sorted(s for s in svc.data.ds["gameweeks"]["season"].unique() if s <= ctx.season)
    pm = view.player_match(seasons[-2:])
    pm = pm[pm["player_code"].isin(codes)].sort_values("kickoff_at")
    recent = pm.groupby("player_code").tail(window).groupby("player_code")["starts"]
    starts = {int(c): tuple(bool(x > 0) for x in v.fillna(0)) for c, v in recent}
    out: dict[int, PlayerStatus] = {}
    for c in codes:
        st = pool["status"].get(c) if c in pool.index else None
        chance = _num(pool["chance_of_playing_next_round"].get(c)) if c in pool.index else None
        out[c] = PlayerStatus(
            status=str(st) if st is not None and not pd.isna(st) else "a",
            chance=chance,
            news=str(news.get(c) or "") if c in news.index else "",
            start_prob=_num(nxt["prob_start"].get(c)) if c in nxt.index else 0.0,
            xp_next=float(nxt["mean"].get(c, 0.0)) if c in nxt.index else 0.0,
            xp=float(hor.get(c, 0.0)),
            recent_starts=starts.get(c, ()),
        )
    return out


def fixture_counts(
    svc: AppServices, ctx: CurrentContext, gameweeks: list[int]
) -> dict[tuple[int, int], int]:
    fx = svc.data.view(ctx.cutoff).fixtures(ctx.season)
    fx = fx[fx["gw"].isin(gameweeks)]
    both = pd.concat(
        [
            fx[["home_team_code", "gw"]].set_axis(["team", "gw"], axis=1),
            fx[["away_team_code", "gw"]].set_axis(["team", "gw"], axis=1),
        ]
    )
    return {(int(t), int(g)): int(n) for (t, g), n in both.groupby(["team", "gw"]).size().items()}


def _watched(state: ManagerState, pkg: RecommendationPackage) -> list[int]:
    buys = [c for step in pkg.chosen.timeline for c in step.transfers_in]
    return sorted(set(state.codes) | set(buys))


def plan_watch(
    svc: AppServices,
    ctx: CurrentContext,
    fc: Forecast,
    state: ManagerState,
    pkg: RecommendationPackage,
    window: int | None = None,
) -> dict[str, Any]:
    """The inputs baseline stored with a recommendation (JSON-serialisable)."""
    window = window or load_notification_config().squad_change.benching_window
    gws = [step.gameweek for step in pkg.chosen.timeline]
    codes = _watched(state, pkg)
    statuses = player_statuses(svc, ctx, fc, codes, gws, window)
    pool = svc.data.view(ctx.cutoff).player_pool(ctx.season, ctx.gameweek).set_index("player_code")
    first = pkg.chosen.timeline[0]
    team_of = {c: int(pool["team_code"][c]) for c in codes if c in pool.index}
    counts = fixture_counts(svc, ctx, gws)
    xpm = _xp_per_match(fc, team_of, counts, gws)
    return {
        "cutoff": ctx.cutoff.isoformat(),
        "snapshot_id": svc.data.snapshot_id,
        "prediction_run_id": fc.run_id,
        "season": ctx.season,
        "gameweek": ctx.gameweek,
        "gameweeks": gws,
        "players": {str(c): asdict(s) for c, s in statuses.items()},
        "team_of": {str(c): t for c, t in team_of.items()},
        "fixtures": {f"{t}:{g}": n for (t, g), n in counts.items()},
        "prices": {str(c): int(pool["price"][c]) for c in codes if c in pool.index},
        "xp_per_match": {str(c): v for c, v in xpm.items()},
        "buys": list(first.transfers_in),
        "sells": list(first.transfers_out),
        "bank_after": int(first.bank_after),
    }


def _xp_per_match(
    fc: Forecast,
    team_of: dict[int, int],
    counts: dict[tuple[int, int], int],
    gws: list[int],
) -> dict[int, float]:
    """Mean expected points per *match* (a double gameweek's mean covers two matches)."""
    s = fc.summary
    s = s[s["player_code"].isin(list(team_of)) & s["gw"].isin(gws)]
    out: dict[int, list[float]] = {}
    for c, g, mean in zip(s["player_code"], s["gw"], s["mean"], strict=True):
        n = counts.get((team_of[int(c)], int(g)), 0)
        if n > 0:
            out.setdefault(int(c), []).append(float(mean) / n)
    return {c: float(np.mean(v)) for c, v in out.items()}
