"""Alert rules (§74): pure functions from typed inputs to materiality-filtered alerts.

Each rule returns zero or more :class:`Alert`. An alert is produced only when its materiality —
expected FPL points at stake (or a probability, for price risk) — clears the configured
threshold, so small fluctuations never notify. Every alert carries a ``dedupe_key`` that names
the underlying *state* (not the time it was seen): re-evaluating unchanged inputs yields the
same key, and the store's unique constraint turns repeats into no-ops. A further deterioration
(e.g. doubtful → injured) is a new state and therefore a new alert.

The rules never fetch data; callers (API service, worker schedule) assemble the inputs from the
point-in-time data plane, so every alert is reproducible from its evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from fpl_domain.config import load_versioned_config
from fpl_domain.hashing import content_hash
from fpl_domain.rules.model import SellingPriceRule
from fpl_domain.squad import selling_price

Severity = Literal["info", "warning", "critical"]
UNAVAILABLE = frozenset({"i", "s", "u", "n"})


# ----------------------------------------------------------------------------- configuration


class _Cfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DeadlineCfg(_Cfg):
    lead_hours: tuple[float, ...] = (24.0, 2.0)
    default_timezone: str = "Europe/London"


class SquadChangeCfg(_Cfg):
    min_points_impact: float = 1.0
    chance_drop: float = 25.0
    role_change_start_prob: float = 0.25
    benching_min_recent_starts: int = 3
    benching_window: int = 4


class PriceRiskCfg(_Cfg):
    min_probability: float = Field(0.3, ge=0.0, le=1.0)


class FixtureChangeCfg(_Cfg):
    min_points_impact: float = 1.5


class InvalidationCfg(_Cfg):
    min_gain_points: float = 2.0
    min_probability_positive: float = 0.5


class PostGameweekCfg(_Cfg):
    miss_points: float = 4.0
    max_items: int = 6


class DeliveryCfg(_Cfg):
    webhook_allowed_hosts: tuple[str, ...] = ()
    webhook_timeout_seconds: float = 5.0


class NotificationConfig(_Cfg):
    version: str = "adhoc"
    deadline: DeadlineCfg = DeadlineCfg()
    squad_change: SquadChangeCfg = SquadChangeCfg()
    price_risk: PriceRiskCfg = PriceRiskCfg()
    fixture_change: FixtureChangeCfg = FixtureChangeCfg()
    invalidation: InvalidationCfg = InvalidationCfg()
    post_gameweek: PostGameweekCfg = PostGameweekCfg()
    delivery: DeliveryCfg = DeliveryCfg()
    config_ref: str = ""


def load_notification_config(name: str = "default") -> NotificationConfig:
    vc = load_versioned_config("notifications", name)
    return NotificationConfig.model_validate({**vc.data, "config_ref": vc.ref})


# ----------------------------------------------------------------------------- alert


@dataclass(frozen=True)
class Alert:
    kind: str
    severity: Severity
    title: str
    body: str
    materiality: float
    dedupe_key: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _name(names: Mapping[int, str] | None, c: int) -> str:
    return (names or {}).get(c, str(c))


# ----------------------------------------------------------------------------- deadline


def deadline_reminder(
    season: str,
    gw: int,
    deadline: datetime,
    now: datetime,
    cfg: DeadlineCfg,
    timezone: str | None = None,
    pending: str | None = None,
) -> list[Alert]:
    """One reminder per lead window, from the official (UTC) deadline shown in local time."""
    if deadline.tzinfo is None or now.tzinfo is None:
        raise ValueError("deadline and now must be timezone-aware")
    if now >= deadline:
        return []
    left = deadline - now
    windows = sorted(h for h in cfg.lead_hours if left <= timedelta(hours=h))
    if not windows:
        return []
    h = windows[0]
    tz = ZoneInfo(timezone or cfg.default_timezone)
    local = deadline.astimezone(tz)
    mins = int(left.total_seconds() // 60)
    body = (
        f"GW{gw} deadline {local:%a %d %b %H:%M} {local.tzname()} "
        f"(in {mins // 60}h {mins % 60:02d}m)."
    )
    body += (
        f" Pending recommendation: {pending}" if pending else " No recommendation generated yet."
    )
    return [
        Alert(
            kind="deadline",
            severity="warning" if h <= 2 else "info",
            title=f"GW{gw} deadline in under {h:g}h",
            body=body,
            materiality=1.0,
            dedupe_key=f"deadline:{season}:{gw}:{h:g}h",
            evidence={
                "deadline_utc": deadline.isoformat(),
                "deadline_local": local.isoformat(),
                "timezone": str(tz),
                "window_hours": h,
            },
        )
    ]


# ----------------------------------------------------------------------------- squad changes


@dataclass(frozen=True)
class PlayerStatus:
    """What the data plane knew about a player at one cutoff."""

    status: str = "a"  # FPL status code a/d/i/s/u/n
    chance: float | None = None  # FPL chance of playing next round (%)
    news: str = ""
    start_prob: float | None = None  # forecast P(start) for the next gameweek
    xp_next: float = 0.0  # expected points, next gameweek
    xp: float = 0.0  # expected points, plan horizon
    recent_starts: tuple[bool, ...] = ()  # oldest → newest finished matches

    def availability(self) -> float:
        if self.chance is not None:
            return max(0.0, min(1.0, self.chance / 100.0))
        if self.status in UNAVAILABLE:
            return 0.0
        return 0.5 if self.status == "d" else 1.0


def squad_changes(
    season: str,
    gw: int,
    owned: Sequence[int],
    starters: Sequence[int],
    before: Mapping[int, PlayerStatus],
    after: Mapping[int, PlayerStatus],
    cfg: SquadChangeCfg,
    names: Mapping[int, str] | None = None,
) -> list[Alert]:
    """Injury / suspension / doubt, role change and unexpected benching of owned players."""
    out: list[Alert] = []
    xi = set(starters)
    for c in owned:
        b, a = before.get(c), after.get(c)
        if b is None or a is None:
            continue
        n = _name(names, c)
        drop = b.availability() - a.availability()
        if 100.0 * drop >= cfg.chance_drop:
            impact = max(drop * b.xp_next, b.xp - a.xp)
            if impact >= cfg.min_points_impact:
                kind = {"i": "injury", "s": "suspension", "u": "unavailable", "n": "unavailable"}
                k = kind.get(a.status, "doubt")
                chance = "?" if a.chance is None else f"{a.chance:g}%"
                out.append(
                    Alert(
                        kind="squad_change",
                        severity="critical" if a.availability() == 0 and c in xi else "warning",
                        title=f"{n}: {k} ({chance} chance of playing)",
                        body=(
                            f"{n} {'(starter)' if c in xi else '(bench)'} — status {b.status}→"
                            f"{a.status}. {a.news or 'No news text.'} About {impact:.1f} expected "
                            "points over the plan horizon are at stake."
                        ),
                        materiality=round(impact, 3),
                        dedupe_key=f"avail:{season}:{gw}:{c}:{a.status}:{a.chance}",
                        evidence={
                            "player_code": c,
                            "subtype": k,
                            "before": {"status": b.status, "chance": b.chance, "xp": b.xp},
                            "after": {"status": a.status, "chance": a.chance, "xp": a.xp},
                            "news": a.news,
                        },
                    )
                )
                continue  # the availability alert subsumes a role change for the same player
        if b.start_prob is not None and a.start_prob is not None:
            d = a.start_prob - b.start_prob
            if abs(d) >= cfg.role_change_start_prob:
                per_start = b.xp_next / max(b.start_prob, 0.1)
                impact = abs(d) * per_start
                if impact >= cfg.min_points_impact:
                    up = d > 0
                    out.append(
                        Alert(
                            kind="squad_change",
                            severity="info" if up else "warning",
                            title=f"{n}: role {'up' if up else 'down'} "
                            f"(P(start) {b.start_prob:.0%}→{a.start_prob:.0%})",
                            body=(
                                f"The minutes model now gives {n} a {a.start_prob:.0%} chance to "
                                f"start (was {b.start_prob:.0%}); ≈{impact:.1f} expected points "
                                "next gameweek."
                            ),
                            materiality=round(impact, 3),
                            dedupe_key=f"role:{season}:{gw}:{c}:{'up' if up else 'down'}",
                            evidence={
                                "player_code": c,
                                "subtype": "role_change",
                                "start_prob_before": b.start_prob,
                                "start_prob_after": a.start_prob,
                            },
                        )
                    )
                    continue
        w = cfg.benching_window
        rs = a.recent_starts[-w:]
        if (
            len(rs) == w
            and not rs[-1]
            and sum(rs[:-1]) >= cfg.benching_min_recent_starts
            and a.status == "a"
        ):
            impact = b.xp_next * (b.start_prob if b.start_prob is not None else 1.0)
            if impact >= cfg.min_points_impact:
                out.append(
                    Alert(
                        kind="squad_change",
                        severity="warning",
                        title=f"{n}: unexpectedly benched",
                        body=(
                            f"{n} started {sum(rs[:-1])} of the previous {w - 1} matches but did "
                            f"not start the last one while fit. ≈{impact:.1f} expected points "
                            "next gameweek depend on a return to the XI."
                        ),
                        materiality=round(impact, 3),
                        dedupe_key=f"bench:{season}:{gw}:{c}",
                        evidence={
                            "player_code": c,
                            "subtype": "unexpected_benching",
                            "recent_starts": list(rs),
                        },
                    )
                )
    return out


# ----------------------------------------------------------------------------- price risk


@dataclass(frozen=True)
class Sale:
    player_code: int
    purchase_price: int
    current_price: int


def _cost_distribution(steps: Sequence[dict[int, float]]) -> dict[int, float]:
    dist = {0: 1.0}
    for st in steps:
        nxt: dict[int, float] = {}
        for v, p in dist.items():
            for dv, q in st.items():
                if q > 0:
                    nxt[v + dv] = nxt.get(v + dv, 0.0) + p * q
        dist = nxt
    return dist


def price_risk(
    season: str,
    gw: int,
    buys: Mapping[int, int],
    sales: Sequence[Sale],
    bank_after_plan: int,
    p_rise: Mapping[int, float],
    p_fall: Mapping[int, float],
    cfg: PriceRiskCfg,
    rule: SellingPriceRule = SellingPriceRule.HALF_PROFIT_FLOOR,
    names: Mapping[int, str] | None = None,
) -> list[Alert]:
    """P(the planned transfers become unaffordable before the deadline).

    Each player changes price by at most one step before the next deadline (FPL changes are
    ±£0.1m per night; the classifiers predict the change to the next gameweek). A buy's cost
    moves with its price; a sale's proceeds move with the *selling* price (half-profit rule),
    which a fall reduces by 0 or 1 tenth. The exact distribution of the extra cost is a
    convolution of the per-player three-point distributions (independence assumed).
    """
    steps: list[dict[int, float]] = []
    for c in buys:
        r, f = p_rise.get(c, 0.0), p_fall.get(c, 0.0)
        steps.append({1: r, -1: f, 0: max(0.0, 1.0 - r - f)})
    for s in sales:
        r, f = p_rise.get(s.player_code, 0.0), p_fall.get(s.player_code, 0.0)
        sp = selling_price(s.purchase_price, s.current_price, rule)
        lose = sp - selling_price(s.purchase_price, s.current_price - 1, rule)
        gain = selling_price(s.purchase_price, s.current_price + 1, rule) - sp
        st: dict[int, float] = {0: max(0.0, 1.0 - r - f)}
        st[lose] = st.get(lose, 0.0) + f  # proceeds fall → extra cost
        st[-gain] = st.get(-gain, 0.0) + r
        steps.append(st)
    if not steps:
        return []
    dist = _cost_distribution(steps)
    p_bad = sum(p for v, p in dist.items() if v > bank_after_plan)
    if p_bad < cfg.min_probability:
        return []
    risky = sorted(buys, key=lambda c: -p_rise.get(c, 0.0))
    plan = content_hash({"buys": sorted(buys), "sells": sorted(s.player_code for s in sales)})[:12]
    return [
        Alert(
            kind="price_risk",
            severity="warning" if p_bad < 0.6 else "critical",
            title=f"Planned transfer at risk from price changes ({p_bad:.0%})",
            body=(
                f"With £{bank_after_plan / 10:.1f}m left after the plan, there is a {p_bad:.0%} "
                "chance that price moves before the deadline make it unaffordable. Highest rise "
                "risk: "
                + ", ".join(f"{_name(names, c)} {p_rise.get(c, 0.0):.0%}" for c in risky[:3])
                + ". Consider transferring earlier or keeping a buffer."
            ),
            materiality=round(p_bad, 4),
            dedupe_key=f"price:{season}:{gw}:{plan}:{int(p_bad * 10)}",
            evidence={
                "bank_after_plan": bank_after_plan,
                "p_unaffordable": p_bad,
                "buys": {str(c): {"p_rise": p_rise.get(c), "p_fall": p_fall.get(c)} for c in buys},
                "sales": [
                    {"player_code": s.player_code, "p_fall": p_fall.get(s.player_code)}
                    for s in sales
                ],
                "model": "price_change_lgbm",
            },
        )
    ]


# ----------------------------------------------------------------------------- fixtures


def fixture_changes(
    season: str,
    plan_id: str,
    gameweeks: Sequence[int],
    squad: Sequence[int],
    team_of: Mapping[int, int],
    before: Mapping[tuple[int, int], int],
    after: Mapping[tuple[int, int], int],
    xp_per_match: Mapping[int, float],
    cfg: FixtureChangeCfg,
    names: Mapping[int, str] | None = None,
    team_names: Mapping[int, str] | None = None,
) -> list[Alert]:
    """Blank/double/postponement changes for the planned squad's teams inside the plan horizon.

    ``before``/``after`` map (team, gameweek) → number of fixtures at the plan's cutoff and now.
    """
    changes: list[dict[str, Any]] = []
    impact = 0.0
    teams = {team_of[c] for c in squad if c in team_of}
    for gw in gameweeks:
        for t in sorted(teams):
            d = after.get((t, gw), 0) - before.get((t, gw), 0)
            if d == 0:
                continue
            affected = [c for c in squad if team_of.get(c) == t]
            pts = sum(abs(d) * xp_per_match.get(c, 0.0) for c in affected)
            impact += pts
            changes.append(
                {
                    "gameweek": gw,
                    "team": t,
                    "team_name": (team_names or {}).get(t, str(t)),
                    "fixtures_before": before.get((t, gw), 0),
                    "fixtures_after": after.get((t, gw), 0),
                    "players": affected,
                    "points_at_stake": round(pts, 2),
                }
            )
    if impact < cfg.min_points_impact:
        return []
    lines = [
        f"GW{ch['gameweek']} {ch['team_name']}: {ch['fixtures_before']}→{ch['fixtures_after']} "
        f"fixture(s) ({', '.join(_name(names, c) for c in ch['players'])})"
        for ch in changes
    ]
    return [
        Alert(
            kind="fixture_change",
            severity="warning",
            title=f"Fixture changes affect your plan (≈{impact:.1f} pts)",
            body="; ".join(lines) + ". Re-run the planner before acting on the saved plan.",
            materiality=round(impact, 3),
            dedupe_key=f"fixture:{season}:{plan_id}:{content_hash(changes)[:12]}",
            evidence={"changes": changes},
        )
    ]


# ----------------------------------------------------------------------------- invalidation


def recommendation_invalidation(
    season: str,
    gw: int,
    rec_id: str,
    saved_action: str,
    saved_gain_now: float,
    saved_prob_positive_now: float,
    best_gain_now: float,
    best_description: str,
    cfg: InvalidationCfg,
    triggers: Sequence[str] = (),
) -> list[Alert]:
    """Re-evaluated on fresh forecasts: does the saved recommendation still stand?

    ``saved_gain_now`` / ``best_gain_now`` are paired mean gains vs holding on the *new* samples
    (same horizon, same objective); ``saved_prob_positive_now`` is P(saved plan beats hold).
    """
    regret = best_gain_now - saved_gain_now
    reasons = []
    if regret >= cfg.min_gain_points:
        reasons.append(f"a different plan ({best_description}) is now {regret:.1f} pts better")
    weak = saved_action != "HOLD" and saved_prob_positive_now < cfg.min_probability_positive
    if weak:
        reasons.append(
            f"the recommended move now beats holding with only {saved_prob_positive_now:.0%} "
            "probability"
        )
    if not reasons:
        return []
    return [
        Alert(
            kind="invalidation",
            severity="critical",
            title=f"GW{gw} recommendation no longer holds",
            body="New information changed the decision: "
            + "; ".join(reasons)
            + ". "
            + (f"Triggers: {'; '.join(triggers)}. " if triggers else "")
            + "Generate a fresh recommendation before the deadline.",
            materiality=round(max(regret, -saved_gain_now if weak else 0.0, 0.0), 3),
            dedupe_key=f"invalid:{rec_id}",
            evidence={
                "recommendation_id": rec_id,
                "saved_action": saved_action,
                "saved_gain_vs_hold_now": saved_gain_now,
                "saved_prob_positive_now": saved_prob_positive_now,
                "best_gain_vs_hold_now": best_gain_now,
                "regret": regret,
                "triggers": list(triggers),
            },
        )
    ]


# ----------------------------------------------------------------------------- post-GW report


@dataclass(frozen=True)
class PlayerOutcome:
    player_code: int
    starter: bool
    multiplier: int  # 0 bench, 1 starter, 2 captain, 3 triple captain
    expected: float
    p10: float
    p90: float
    prob_play: float
    actual: int
    minutes: int
    components: Mapping[str, float] = field(default_factory=dict)


def _explain(o: PlayerOutcome) -> str:
    if o.minutes == 0:
        return (
            f"did not play (forecast P(play) {o.prob_play:.0%}); expected {o.expected:.1f}"
            if o.prob_play >= 0.5
            else f"did not play, as the forecast suggested was likely (P(play) {o.prob_play:.0%})"
        )
    ev = ", ".join(f"{k} {v:g}" for k, v in o.components.items() if v)
    tail = f"; {ev}" if ev else ""
    if o.actual >= o.p90 and o.actual > o.expected:
        return f"hauled {o.actual} vs {o.expected:.1f} expected (≥ 90th pct {o.p90:g}{tail})"
    if o.actual <= o.p10 and o.actual < o.expected:
        return f"blanked {o.actual} vs {o.expected:.1f} expected (≤ 10th pct {o.p10:g}{tail})"
    side = "above" if o.actual > o.expected else "below"
    return f"{o.actual} vs {o.expected:.1f} expected — {side}, inside the 80% range{tail}"


def post_gameweek_report(
    season: str,
    gw: int,
    outcomes: Sequence[PlayerOutcome],
    squad_expected: float,
    squad_p10: float,
    squad_p90: float,
    actual_total: int,
    cfg: PostGameweekCfg,
    names: Mapping[int, str] | None = None,
) -> list[Alert]:
    """Forecast vs actual for the manager's gameweek, explaining the material misses."""
    scored = sorted(outcomes, key=lambda o: -abs((o.actual - o.expected) * max(o.multiplier, 1)))
    misses = [
        o
        for o in scored
        if abs(o.actual - o.expected) * max(o.multiplier, 1) >= cfg.miss_points
        or (o.minutes == 0 and o.prob_play >= 0.5 and o.multiplier > 0)
    ][: cfg.max_items]
    inside = squad_p10 <= actual_total <= squad_p90
    head = (
        f"GW{gw}: {actual_total} pts vs {squad_expected:.1f} expected "
        f"(80% range {squad_p10:g}–{squad_p90:g}; {'inside' if inside else 'outside'})."
    )
    lines = [
        f"{_name(names, o.player_code)}{' (C)' if o.multiplier >= 2 else ''}"
        f"{' (bench)' if o.multiplier == 0 else ''}: {_explain(o)}"
        for o in misses
    ]
    return [
        Alert(
            kind="post_gameweek",
            severity="info",
            title=f"GW{gw} review: {actual_total} vs {squad_expected:.1f} expected",
            body=head + (" Largest misses — " + "; ".join(lines) + "." if lines else ""),
            materiality=round(abs(actual_total - squad_expected), 3),
            dedupe_key=f"postgw:{season}:{gw}",
            evidence={
                "expected": squad_expected,
                "p10": squad_p10,
                "p90": squad_p90,
                "actual": actual_total,
                "inside_80": inside,
                "players": [
                    {
                        "player_code": o.player_code,
                        "multiplier": o.multiplier,
                        "expected": o.expected,
                        "actual": o.actual,
                        "minutes": o.minutes,
                        "explanation": _explain(o),
                    }
                    for o in outcomes
                ],
            },
        )
    ]
