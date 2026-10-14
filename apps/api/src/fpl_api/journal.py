"""Decision journal with prospective outcome evaluation (M1.4).

A manager records what they decided for one gameweek against a stored recommendation or a
Transfer Workbench plan. The record keeps four things apart:

* **what Copilot recommended** (the source's recommended plan),
* **what the manager said they would do** (the chosen action: a plan, HOLD, "decide later" or
  "something else"), with an optional rationale,
* **what the manager confirms they actually did** (an explicit confirmation), and
* **what first-party data shows they did** (the official FPL picks of that gameweek, when the
  squad was imported with an FPL ID) — reported, never used to rewrite the record.

Decision-time evidence (lineups, positions, clubs and fixtures, expected points with their
uncertainty, the paired gain against HOLD, the system's rationale, identifiers of the state,
snapshot and forecast) is copied into the record when it is written (``DecisionEvidence``):
recommendations and runs can later be invalidated (M1.3), re-queued or erased, and forecasts are
pruned within a day, so the journal never depends on them to remember what was known.

After the gameweek, the evaluator scores the recorded action, HOLD and the recommended plan on
the official per-fixture points with the domain rules (auto-substitutions, vice-captain fallback,
chips; ``fpl_domain.squad.gameweek_points``) and compares them with the expectations stored at
decision time. It is final only when FPL has finalised the gameweek and every relevant fixture
is recorded; revised official data writes a new evaluation that keeps the previous one.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fpl_domain.enums import Position
from fpl_domain.hashing import content_hash, short_id
from fpl_domain.rules import Ruleset, load_ruleset
from fpl_domain.squad import Lineup, gameweek_points
from fpl_optimizer.lineup import best_lineup
from fpl_optimizer.problem import load_optimizer_config
from fpl_storage import models as m
from fpl_storage.db import session_scope
from fpl_storage.finality import gameweek_finality

log = structlog.get_logger("fpl_api.journal")
JournalRow = m.DecisionJournalRow
EvaluationRow = m.DecisionEvaluationRow

EVIDENCE_VERSION = "journal-evidence-1"
EVALUATOR_VERSION = "journal-eval-1"
ACTIONS = ("HOLD", "TRANSFER", "HIT", "CHIP", "DEFER", "UNKNOWN")
ADHERENCE = ("followed", "modified", "rejected", "deferred")
EVALUATION_STATES = ("pending", "partial", "final", "unavailable")
CHOICES = ("recommended", "hold", "plan", "defer", "other")
CAVEAT = (
    "One gameweek is a single draw from the forecast distribution: a sound decision can lose "
    "points to the alternative and a poor one can gain them. This compares what happened with "
    "what was expected when you decided; it does not grade the decision."
)


def clock() -> datetime:
    """The journal's notion of "now" (decision deadlines; replaced in tests)."""
    return datetime.now(UTC)


class JournalRefused(Exception):
    """A request the journal cannot honour (``status`` is the HTTP status to answer with)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ decision-time evidence


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Expectation(_Model):
    """Expected squad points for the gameweek, net of hits (decision-time forecast)."""

    mean: float
    p10: float | None = None
    p50: float | None = None
    p90: float | None = None


class GainExpectation(_Model):
    """Paired gain against HOLD for the gameweek on the same simulation samples."""

    mean: float
    p10: float
    p50: float
    p90: float
    probability_positive: float


class PlanEvidence(_Model):
    label: str
    action: str
    sells: list[int]
    buys: list[int]
    hit_points: int
    chip: str | None
    starters: list[int] | None
    bench: list[int] | None
    captain: int | None
    vice_captain: int | None
    lineup_source: Literal["stored", "reconstructed", "unavailable"]
    lineup_note: str | None = None
    # False when a rebuilt XI did not contain the stored captain and vice, so its own armband
    # was used (None: the lineup and armband were stored together)
    armband_as_stored: bool | None = None
    expected: Expectation | None = None
    gain_vs_hold: GainExpectation | None = None

    def lineup(self) -> Lineup | None:
        if not self.starters or not self.bench or self.captain is None or self.vice_captain is None:
            return None
        return Lineup(
            starters=tuple(self.starters),
            bench=tuple(self.bench),
            captain=self.captain,
            vice_captain=self.vice_captain,
        )

    def same_moves(self, other: PlanEvidence) -> bool:
        return (
            sorted(self.sells) == sorted(other.sells)
            and sorted(self.buys) == sorted(other.buys)
            and self.chip == other.chip
        )


class SystemRationale(_Model):
    decision: str
    why: list[str] = Field(default_factory=list)
    downside: list[str] = Field(default_factory=list)
    optimality: str | None = None
    confidence: dict[str, float] | None = None
    stability: str | None = None


class DecisionEvidence(_Model):
    """Everything the journal needs later, copied when the decision is recorded."""

    version: str = EVIDENCE_VERSION
    source_kind: Literal["recommendation", "workbench"]
    source_id: str
    source_plan: str | None
    source_state: str  # the source's lifecycle verdict when the decision was recorded
    source_reasons: list[str]
    recorded_at: str
    season: str
    gameweek: int
    deadline_at: str | None
    cutoff: str | None
    state_id: str | None
    data_snapshot_id: str | None
    prediction_run_id: str | None
    forecast_key: str | None
    ruleset_version: str | None
    model_versions: dict[str, str]
    data_as_of: str | None
    squad_before: list[int]
    positions: dict[str, str]
    teams: dict[str, int]
    fixtures: dict[str, list[int]]
    recommended: PlanEvidence
    hold: PlanEvidence
    chosen: PlanEvidence | None
    captain_override: bool = False
    offered_captain: int | None = None  # the chosen plan's own armband, before an override
    offered_vice_captain: int | None = None
    system_rationale: SystemRationale
    player_expectations: dict[str, dict[str, float | None]] = Field(default_factory=dict)


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def _gain(g: dict[str, Any] | None) -> GainExpectation | None:
    if not g:
        return None
    return GainExpectation(
        mean=float(g["mean"]),
        p10=float(g["p10"]),
        p50=float(g["p50"]),
        p90=float(g["p90"]),
        probability_positive=float(g["probability_positive"]),
    )


def _season_frame(ds: Any, table: str, season: str) -> pd.DataFrame:
    df = ds[table]
    return df[df["season"] == season]


def _deadline(ds: Any, season: str, gw: int) -> datetime | None:
    g = _season_frame(ds, "gameweeks", season)
    g = g[g["gw"] == gw]
    if g.empty or pd.isna(g["deadline_at"].iloc[0]):
        return None
    ts = pd.Timestamp(g["deadline_at"].iloc[0])
    return (ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")).to_pydatetime()


def _fixtures_by_team(ds: Any, season: str, gw: int, teams: set[int]) -> dict[str, list[int]]:
    fx = _season_frame(ds, "fixtures", season)
    fx = fx[fx["gw"] == gw]
    out: dict[str, list[int]] = {str(t): [] for t in sorted(teams)}
    for r in fx.itertuples(index=False):
        for t in (int(r.home_team_code), int(r.away_team_code)):
            if t in teams:
                out[str(t)].append(int(r.fixture_id))
    return out


def _players_meta(ds: Any, season: str, codes: set[int]) -> tuple[dict[int, str], dict[int, int]]:
    p = _season_frame(ds, "players", season)
    p = p[p["player_code"].isin(list(codes))]
    pos = {int(r.player_code): str(r.position) for r in p.itertuples(index=False)}
    team = {int(r.player_code): int(r.team_code) for r in p.itertuples(index=False)}
    return pos, team


def _reconstruct_hold_lineup(
    codes: list[int],
    positions: dict[int, str],
    xp: dict[int, float],
    captain: int | None,
    vice: int | None,
    profile: str,
    rs: Ruleset,
) -> tuple[list[int], list[int], int, int, bool] | None:
    """HOLD's XI and bench when the recommendation did not store them (it stores HOLD's captain
    and vice only): the best lineup on the decision-time expected points of the unchanged squad,
    the backtest's HOLD convention, with the stored armband when both players start in it
    (the last element says whether it did). None when any squad player's expectation is
    missing."""
    if any(c not in xp or c not in positions for c in codes):
        return None
    pos = {c: Position(positions[c]) for c in codes}
    weights = load_optimizer_config(profile).objective
    lu = best_lineup(codes, pos, xp, xp, weights, rs).lineup
    starters, bench = list(lu.starters), list(lu.bench)
    if captain in starters and vice in starters and captain != vice:
        return starters, bench, int(captain), int(vice), True
    return starters, bench, lu.captain, lu.vice_captain, False


def _profile_of(config_refs: dict[str, Any]) -> str:
    ref = str(config_refs.get("optimizer", "optimizer/default@"))
    name = ref.split("/", 1)[-1].split("@", 1)[0]
    return name or "default"


def evidence_from_recommendation(
    svc: Any, source: dict[str, Any], state: Any, lifecycle: dict[str, Any], now: datetime
) -> tuple[DecisionEvidence, dict[str, PlanEvidence]]:
    """Decision-time evidence of a stored recommendation and the plans a manager can choose
    from it: the recommended plan (its lineup is stored) and HOLD (its XI is reconstructed
    from the decision-time expected points; captain and vice are stored)."""
    rec = source
    season, gw = str(rec["season"]), int(rec["gameweek"])
    ds = svc.data.ds
    rs = load_ruleset(season)
    watch = rec.get("watch") or {}
    chosen_opt, hold_opt = rec["chosen"], rec["hold"]
    step, hstep = chosen_opt["timeline"][0], hold_opt["timeline"][0]
    decision = rec.get("decision") or {}
    squad_before = [int(p.player_code) for p in state.squad]
    pos = {int(p.player_code): str(p.position.value) for p in state.squad}
    # clubs at decision time: the saved squad may predate a club move, and the recommendation
    # was computed on the current clubs (``watch.team_of``), which therefore win
    teams = {int(p.player_code): int(p.team_code) for p in state.squad}
    buys = {int(c) for c in step.get("transfers_in", [])}
    extra_pos, extra_team = _players_meta(ds, season, buys - set(pos))
    pos.update(extra_pos)
    teams.update(extra_team)
    teams.update({int(c): int(t) for c, t in (watch.get("team_of") or {}).items()})
    lineup = rec.get("lineup") or {}
    recommended = PlanEvidence(
        label=str(chosen_opt.get("label", "best")),
        action=str(step["action"]),
        sells=[int(c) for c in step.get("transfers_out", [])],
        buys=sorted(buys),
        hit_points=int(step.get("hit_points", 0)),
        chip=step.get("chip"),
        starters=[int(c) for c in lineup.get("starters", [])] or None,
        bench=[int(c) for c in lineup.get("bench", [])] or None,
        captain=lineup.get("captain", step.get("captain")),
        vice_captain=lineup.get("vice_captain", step.get("vice_captain")),
        lineup_source="stored" if lineup else "unavailable",
        expected=Expectation(
            mean=float(step["expected_points"]),
            p10=_f(step.get("p10")),
            p50=_f(decision.get("p50")),
            p90=_f(step.get("p90")),
        ),
        gain_vs_hold=_gain(chosen_opt.get("gain_1gw")),
    )
    hold_expected = Expectation(
        mean=float(hstep["expected_points"]), p10=_f(hstep.get("p10")), p90=_f(hstep.get("p90"))
    )
    if recommended.action == "HOLD" and recommended.starters:
        hold = recommended.model_copy(update={"label": "hold", "gain_vs_hold": None})
    else:
        xp = {
            int(c): float(v["xp_next"])
            for c, v in (watch.get("players") or {}).items()
            if v.get("xp_next") is not None
        }
        rebuilt = _reconstruct_hold_lineup(
            squad_before,
            pos,
            xp,
            hstep.get("captain"),
            hstep.get("vice_captain"),
            _profile_of(rec.get("config_refs") or {}),
            rs,
        )
        hold = PlanEvidence(
            label="hold",
            action="HOLD",
            sells=[],
            buys=[],
            hit_points=int(hstep.get("hit_points", 0)),
            chip=None,
            starters=rebuilt[0] if rebuilt else None,
            bench=rebuilt[1] if rebuilt else None,
            captain=rebuilt[2] if rebuilt else hstep.get("captain"),
            vice_captain=rebuilt[3] if rebuilt else hstep.get("vice_captain"),
            lineup_source="reconstructed" if rebuilt else "unavailable",
            lineup_note=(
                "HOLD's XI and bench were not stored with the recommendation: rebuilt from the "
                "decision-time expected points of the unchanged squad"
                + (
                    " (captain and vice as stored)"
                    if rebuilt[4]
                    else " (the stored captain or vice-captain is not in the rebuilt XI, so its "
                    "own armband is used)"
                )
                if rebuilt
                else "HOLD's XI was not stored and the decision-time expectations are incomplete"
            ),
            armband_as_stored=rebuilt[4] if rebuilt else None,
            expected=hold_expected,
        )
    expl = rec.get("explanation") or {}
    rationale = SystemRationale(
        decision=str(expl.get("decision") or decision.get("action", "")),
        why=[str(d.get("text", "")) for d in expl.get("primary_drivers", [])][:4],
        downside=[str(d.get("description", "")) for d in expl.get("downside_scenarios", [])][:4],
        optimality=decision.get("optimality"),
        confidence={k: float(v) for k, v in (expl.get("confidence") or {}).items()} or None,
        stability=(rec.get("stability") or {}).get("label"),
    )
    involved = set(teams.values())
    deadline = _deadline(ds, season, gw)
    ev = DecisionEvidence(
        source_kind="recommendation",
        source_id=str(rec["id"]),
        source_plan=recommended.label,
        source_state=str(lifecycle.get("state", "unknown")),
        source_reasons=[str(r.get("detail", "")) for r in lifecycle.get("reasons", [])],
        recorded_at=now.isoformat(),
        season=season,
        gameweek=gw,
        deadline_at=deadline.isoformat() if deadline else None,
        cutoff=watch.get("cutoff"),
        state_id=rec.get("state_id"),
        data_snapshot_id=rec.get("data_snapshot_id") or rec.get("snapshot_id"),
        prediction_run_id=watch.get("prediction_run_id") or rec.get("prediction_run_id"),
        forecast_key=None,
        ruleset_version=rec.get("ruleset_version"),
        model_versions={k: str(v) for k, v in (rec.get("model_versions") or {}).items()},
        data_as_of=(expl.get("data_timestamp") or None),
        squad_before=squad_before,
        positions={str(c): p for c, p in pos.items()},
        teams={str(c): t for c, t in teams.items()},
        fixtures=_fixtures_by_team(ds, season, gw, involved),
        recommended=recommended,
        hold=hold,
        chosen=None,
        system_rationale=rationale,
        player_expectations={
            str(c): {"xp_next": _f(v.get("xp_next")), "start_prob": _f(v.get("start_prob"))}
            for c, v in (watch.get("players") or {}).items()
        },
    )
    return ev, {"recommended": recommended, "hold": hold}


def _first_action(p: dict[str, Any]) -> str:
    return str((p.get("timeline") or [{}])[0].get("action") or p.get("action") or "HOLD")


def workbench_recommended_index(plans: list[dict[str, Any]]) -> int:
    """The plan the workbench would recommend: the best-ranked valid plan that makes a move now
    and clears the decision thresholds with a positive gain (the decision engine's rule: a plan
    that holds this gameweek is never the recommended move), else HOLD."""
    for i, p in enumerate(plans):
        positive = float(p.get("objective_gain_vs_hold", 0.0)) > 0
        moves_now = _first_action(p) != "HOLD"
        if i and p.get("valid") and p.get("passes_thresholds") and positive and moves_now:
            return i
    return 0


def _plan_from_workbench(p: dict[str, Any]) -> PlanEvidence:
    step = (p.get("timeline") or [{}])[0]
    lu = p.get("lineup") or {}
    return PlanEvidence(
        label=str(p["label"]),
        action=_first_action(p),
        sells=[int(c) for c in p.get("sells", [])],
        buys=[int(c) for c in p.get("buys", [])],
        hit_points=int(p.get("hit_now", step.get("hit_points", 0))),
        chip=step.get("chip"),
        starters=[int(c) for c in lu.get("starters", [])] or None,
        bench=[int(c) for c in lu.get("bench", [])] or None,
        captain=lu.get("captain", step.get("captain")),
        vice_captain=lu.get("vice_captain", step.get("vice_captain")),
        lineup_source="stored" if lu else "unavailable",
        expected=Expectation(
            mean=float(p["expected_points_1gw"]), p10=_f(step.get("p10")), p90=_f(step.get("p90"))
        ),
        gain_vs_hold=None if p.get("kind") == "hold" else _gain(p.get("gain_1gw")),
    )


def evidence_from_workbench(
    svc: Any, source: dict[str, Any], state: Any, lifecycle: dict[str, Any], now: datetime
) -> tuple[DecisionEvidence, dict[str, PlanEvidence]]:
    """Decision-time evidence of a succeeded workbench run; every evaluated plan (HOLD first)
    stores its full lineup, so any valid plan can be chosen."""
    run = source
    payload = run.get("result") or {}
    res = payload.get("result") or {}
    plans = list(res.get("plans") or [])
    if not plans:
        raise JournalRefused(409, "this workbench run has no evaluated plans to record")
    season = str(payload.get("season") or res.get("season"))
    gw = int(res["gameweek"])
    ds = svc.data.ds
    squad_before = [int(p.player_code) for p in state.squad]
    pos = {int(p.player_code): str(p.position.value) for p in state.squad}
    teams = {int(p.player_code): int(p.team_code) for p in state.squad}
    buys = {int(c) for p in plans for c in p.get("buys", [])}
    pos.update(_players_meta(ds, season, buys - set(pos))[0])
    # clubs at decision time (the run was computed on the current clubs; the saved squad may
    # predate a club move)
    teams.update(_players_meta(ds, season, set(squad_before) | buys)[1])
    # only plans that passed the independent rules replay can be chosen (HOLD always does)
    options = {
        f"{i}:{p['label']}": _plan_from_workbench(p)
        for i, p in enumerate(plans)
        if i == 0 or p.get("valid")
    }
    rec_i = workbench_recommended_index(plans)
    recommended = options[f"{rec_i}:{plans[rec_i]['label']}"]
    hold = options[f"0:{plans[0]['label']}"]
    rp = plans[rec_i]
    deadline = _deadline(ds, season, gw)
    forecast = payload.get("forecast") or {}
    ev = DecisionEvidence(
        source_kind="workbench",
        source_id=str(run["id"]),
        source_plan=f"{rec_i}:{rp['label']}",
        source_state=str(lifecycle.get("state", "unknown")),
        source_reasons=[str(r.get("detail", "")) for r in lifecycle.get("reasons", [])],
        recorded_at=now.isoformat(),
        season=season,
        gameweek=gw,
        deadline_at=deadline.isoformat() if deadline else None,
        cutoff=None if str(forecast.get("cutoff")) in ("None", "") else forecast.get("cutoff"),
        state_id=run.get("state_id"),
        # what the plans were computed on (the payload) — a run computed on other inputs than
        # it names is stored with that reason, and its evidence must name the real ones
        data_snapshot_id=payload.get("snapshot_id") or run.get("snapshot_id"),
        prediction_run_id=payload.get("prediction_run_id"),
        forecast_key=payload.get("forecast_key") or run.get("forecast_key"),
        ruleset_version=res.get("ruleset_version"),
        model_versions={k: str(v) for k, v in (forecast.get("model_versions") or {}).items()},
        data_as_of=forecast.get("latest_source_at"),
        squad_before=squad_before,
        positions={str(c): p for c, p in pos.items()},
        teams={str(c): t for c, t in teams.items()},
        fixtures=_fixtures_by_team(ds, season, gw, set(teams.values())),
        recommended=recommended,
        hold=hold,
        chosen=None,
        system_rationale=SystemRationale(
            decision=str(rp.get("reason") or rp.get("label")),
            why=[str(rp.get("reason", ""))] if rp.get("reason") else [],
            optimality=rp.get("optimality"),
        ),
        player_expectations={
            str(mv["player_code"]): {
                "xp_next": _f(mv.get("xp_next")),
                "start_prob": _f(mv.get("p_start_next")),
            }
            for p in plans
            for mv in [*(p.get("moves_in") or []), *(p.get("moves_out") or [])]
        },
    )
    return ev, {"recommended": recommended, "hold": hold, **options}


def adherence(recommended: PlanEvidence, chosen: PlanEvidence | None, choice: str) -> str:
    """How the recorded action relates to the recommendation, derived — never assumed."""
    if choice == "defer":
        return "deferred"
    if chosen is None:
        return "rejected"
    if chosen.same_moves(recommended):
        same_armband = (chosen.captain, chosen.vice_captain) == (
            recommended.captain,
            recommended.vice_captain,
        )
        return "followed" if same_armband else "modified"
    if chosen.action == "HOLD" or recommended.action == "HOLD":
        return "rejected"
    return "modified"


def choose(
    options: dict[str, PlanEvidence],
    choice: str,
    plan: str | None,
    captain: int | None,
    vice_captain: int | None,
) -> tuple[PlanEvidence | None, str, bool, PlanEvidence | None]:
    """The chosen plan, its action, whether the armband was changed, and the plan as offered
    (before an armband change). Only plans whose content is stored can be chosen: a choice the
    journal cannot reconstruct is recorded as "something else" (action UNKNOWN), never guessed."""
    if choice == "defer":
        return None, "DEFER", False, None
    if choice == "other":
        return None, "UNKNOWN", False, None
    if choice == "plan":
        if not plan or plan not in options or plan in ("recommended", "hold"):
            raise JournalRefused(
                422, f"plan {plan!r} is not one of this run's evaluated plans that passed the rules"
            )
        offered = options[plan]
    elif choice in ("recommended", "hold"):
        offered = options[choice]
    else:
        raise JournalRefused(422, f"unknown choice {choice!r}")
    chosen, override = offered, False
    if captain is not None or vice_captain is not None:
        starters = offered.starters or []
        cap = captain if captain is not None else offered.captain
        vc = vice_captain if vice_captain is not None else offered.vice_captain
        if not starters or cap not in starters or vc not in starters or cap == vc:
            raise JournalRefused(
                422, "captain and vice-captain must be two different starters of the chosen plan"
            )
        override = (cap, vc) != (offered.captain, offered.vice_captain)
        if override:
            chosen = offered.model_copy(update={"captain": cap, "vice_captain": vc})
    return chosen, chosen.action, override, offered


# ------------------------------------------------------------------ storage


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return (ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)).isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)


def _evaluation(e: m.DecisionEvaluationRow) -> dict[str, Any]:
    return {
        "id": e.id,
        "seq": e.seq,
        "status": e.status,
        "evaluator_version": e.evaluator_version,
        "data_snapshot_id": e.data_snapshot_id,
        "realized_points": e.realized_points,
        "realized_hold_points": e.realized_hold_points,
        "realized_recommended_points": e.realized_recommended_points,
        "detail": dict(e.detail_json or {}),
        "revises_id": e.revises_id,
        "evaluated_at": _iso(e.evaluated_at),
    }


def _entry(r: m.DecisionJournalRow) -> dict[str, Any]:
    return {
        "entry_id": r.entry_id,
        "manager_key": r.manager_key,
        "state": r.state,
        "superseded_by": r.superseded_by,
        "season": r.season,
        "gameweek": r.gameweek,
        "deadline_at": _iso(r.deadline_at),
        "prospective": r.prospective,
        "recorded_at": _iso(r.recorded_at),
        "source": {
            "kind": "recommendation" if r.recommendation_id else "workbench",
            "id": r.recommendation_id or r.workbench_run_id,
            "plan": r.source_plan,
        },
        "state_id": r.state_id,
        "data_snapshot_id": r.data_snapshot_id,
        "prediction_run_id": r.prediction_run_id,
        "recommended_action": r.recommended_action,
        "action": r.action,
        "sells": list(r.sells or []),
        "buys": list(r.buys or []),
        "hit_points": r.hit_points,
        "captain": r.captain,
        "vice_captain": r.vice_captain,
        "chip": r.chip,
        "adherence": r.followed,
        "rationale": r.user_note,
        "confirmed_at": _iso(r.confirmed_at),
        "verification": r.verification,
        "verification_detail": dict(r.verification_json) if r.verification_json else None,
        "verified_at": _iso(r.verified_at),
        "evaluation_status": r.evaluation_status,
        "evaluated_at": _iso(r.evaluated_at),
        "evidence": dict(r.evidence_json or {}),
        "self_reported_points": r.realized_points,  # pre-M1.4 field, typed by the manager
    }


REVISION_WINDOW = timedelta(days=7)  # a final evaluation is re-checked for official revisions
VERIFY_RETRY_WINDOW = timedelta(days=7)  # a failed first-party check is retried after the deadline


def due_clause(now: datetime) -> Any:
    """Active decisions whose evaluation or verification can still change: pending or partial
    (a "something else" decision stays partial until its reference totals are final), final
    ones evaluated within the revision window, not yet verified, or whose check failed for a
    reason worth retrying (within its window). Terminal states — final and settled, unavailable
    with a settled verification — leave the sweep."""
    j = JournalRow
    retryable = j.verification_json["retryable"].as_boolean()
    return (
        j.evaluation_status.in_(("pending", "partial"))
        | ((j.evaluation_status == "final") & (j.evaluated_at > now - REVISION_WINDOW))
        | j.verification.is_(None)
        | (
            (j.verification == "unavailable")
            & retryable.is_(True)
            & (j.deadline_at > now - VERIFY_RETRY_WINDOW)
        )
    )


def _lock(s: Any, manager_key: str, season: str, gw: int) -> None:
    """Serialise the writers of one manager's decision for one gameweek."""
    s.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"journal:{manager_key}:{season}:{gw}"},
    )


@dataclass
class JournalStore:
    engine: Engine

    def record(
        self,
        manager_key: str,
        evidence: DecisionEvidence,
        action: str,
        adherence_: str,
        rationale: str | None,
        now: datetime,
        self_reported_points: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Store a decision (content-addressed: the same submission returns the same entry).
        A different decision for the same gameweek supersedes the active one — only before the
        deadline when the active one was itself recorded before it, so a decision made before
        the deadline is never replaced with hindsight. Returns (entry, created)."""
        chosen = evidence.chosen
        entry_id = short_id(
            "jd",
            {
                "m": manager_key,
                "src": [evidence.source_kind, evidence.source_id],
                "a": action,
                "plan": chosen.label if chosen else None,
                "sells": sorted(chosen.sells) if chosen else [],
                "buys": sorted(chosen.buys) if chosen else [],
                "cap": [chosen.captain, chosen.vice_captain] if chosen else None,
                "chip": chosen.chip if chosen else None,
                "adh": adherence_,
                "note": rationale or "",
            },
        )
        deadline = _parse(evidence.deadline_at)
        before_deadline = deadline is None or now < deadline
        with session_scope(self.engine) as s:
            _lock(s, manager_key, evidence.season, evidence.gameweek)
            existing = s.scalars(select(JournalRow).where(JournalRow.entry_id == entry_id)).first()
            active = s.scalars(
                select(JournalRow).where(
                    JournalRow.manager_key == manager_key,
                    JournalRow.season == evidence.season,
                    JournalRow.gameweek == evidence.gameweek,
                    JournalRow.state == "active",
                    JournalRow.entry_id != entry_id,
                )
            ).first()
            if existing is not None and existing.state == "active":
                if self_reported_points is not None:  # a manager statement, not evidence
                    existing.realized_points = self_reported_points
                return _entry(existing), False
            # a row migrated from pre-M1.4 feedback has no stored flag: it was prospective if
            # it was recorded before this gameweek's deadline
            was_prospective = active is not None and (
                active.prospective
                if active.prospective is not None
                else deadline is not None and active.recorded_at < deadline
            )
            if was_prospective and not before_deadline:
                raise JournalRefused(
                    409,
                    f"the GW{evidence.gameweek} deadline has passed: the decision recorded "
                    "before it stands and cannot be replaced",
                )
            target = entry_id
            if active is not None:
                active.state, active.superseded_by = "superseded", target
            if existing is not None:  # the same decision as an earlier one, made again
                existing.state, existing.superseded_by = "active", None
                if self_reported_points is not None:
                    existing.realized_points = self_reported_points
                return _entry(existing), False
            row = JournalRow(
                entry_id=entry_id,
                manager_key=manager_key,
                recommendation_id=evidence.source_id
                if evidence.source_kind == "recommendation"
                else None,
                workbench_run_id=evidence.source_id
                if evidence.source_kind == "workbench"
                else None,
                source_plan=chosen.label if chosen else evidence.source_plan,
                season=evidence.season,
                gameweek=evidence.gameweek,
                deadline_at=deadline,
                state_id=evidence.state_id,
                data_snapshot_id=evidence.data_snapshot_id,
                prediction_run_id=evidence.prediction_run_id,
                recommended_action=evidence.recommended.action,
                action=action,
                sells=sorted(chosen.sells) if chosen else [],
                buys=sorted(chosen.buys) if chosen else [],
                hit_points=chosen.hit_points if chosen else None,
                captain=chosen.captain if chosen else None,
                vice_captain=chosen.vice_captain if chosen else None,
                chip=chosen.chip if chosen else None,
                followed=adherence_,
                user_note=rationale,
                state="active",
                prospective=before_deadline,
                evidence_json=evidence.model_dump(mode="json"),
                evaluation_status="pending",
                realized_points=self_reported_points,
                review_json={},
            )
            s.add(row)
            s.flush()
            return _entry(row), True

    def get(self, entry_id: str) -> dict[str, Any] | None:
        with session_scope(self.engine) as s:
            r = s.scalars(
                select(m.DecisionJournalRow).where(m.DecisionJournalRow.entry_id == entry_id)
            ).first()
            if r is None:
                return None
            out = _entry(r)
            out["evaluations"] = [
                _evaluation(e)
                for e in s.scalars(
                    select(EvaluationRow)
                    .where(EvaluationRow.entry_id == entry_id)
                    .order_by(EvaluationRow.id.desc())
                )
            ]
            return out

    def entries(
        self,
        manager_key: str,
        gameweek: int | None = None,
        action: str | None = None,
        evaluation: str | None = None,
        include_superseded: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        q = select(JournalRow).where(JournalRow.manager_key == manager_key)
        if not include_superseded:
            q = q.where(JournalRow.state == "active")
        if gameweek is not None:
            q = q.where(JournalRow.gameweek == gameweek)
        if action is not None:
            q = q.where(JournalRow.action == action)
        if evaluation is not None:
            q = q.where(JournalRow.evaluation_status == evaluation)
        with session_scope(self.engine) as s:
            rows = s.scalars(
                q.order_by(JournalRow.recorded_at.desc(), JournalRow.id.desc()).limit(limit)
            ).all()
            out = []
            for r in rows:
                e = _entry(r)
                e.pop("evidence")
                last = s.scalars(
                    select(EvaluationRow)
                    .where(EvaluationRow.entry_id == r.entry_id)
                    .order_by(EvaluationRow.id.desc())
                    .limit(1)
                ).first()
                e["latest_evaluation"] = _evaluation(last) if last else None
                out.append(e)
            return out

    def confirm(self, entry_id: str, now: datetime) -> dict[str, Any]:
        with session_scope(self.engine) as s:
            r = s.scalars(
                select(m.DecisionJournalRow)
                .where(m.DecisionJournalRow.entry_id == entry_id)
                .with_for_update()
            ).first()
            if r is None:
                raise JournalRefused(404, f"journal entry {entry_id} not found")
            if r.state != "active":
                raise JournalRefused(409, "a superseded decision cannot be confirmed")
            if r.action in ("DEFER", "UNKNOWN"):
                raise JournalRefused(409, "there is no recorded action to confirm")
            if r.confirmed_at is None:
                r.confirmed_at = now
            return _entry(r)

    def due(self, manager_key: str, entry_id: str | None, now: datetime) -> list[dict[str, Any]]:
        """Active entries whose evaluation can still change (``due_clause``); one named entry
        is evaluated when asked whatever its state, if it is active."""
        q = select(JournalRow).where(
            JournalRow.manager_key == manager_key, JournalRow.state == "active"
        )
        q = (
            q.where(JournalRow.entry_id == entry_id)
            if entry_id is not None
            else q.where(due_clause(now))
        )
        with session_scope(self.engine) as s:
            return [_entry(r) for r in s.scalars(q.order_by(JournalRow.id)).all()]

    def due_managers(self, now: datetime) -> list[str]:
        """Managers with at least one due decision (the scheduler submits one job each)."""
        with session_scope(self.engine) as s:
            q = select(JournalRow.manager_key).where(JournalRow.state == "active", due_clause(now))
            return sorted(set(s.scalars(q.distinct()).all()))

    def save_evaluation(
        self,
        entry_id: str,
        manager_key: str,
        result: EvaluationResult,
        now: datetime,
        is_older: Callable[[str | None, str | None], bool] | None = None,
    ) -> bool:
        """Insert-only: inputs whose digest equals the latest evaluation's write nothing, and
        neither does a process evaluating on older data than the latest evaluation used
        (``is_older(own, latest)``); a new final result after an earlier final one is a
        revision of official data and records what it revises — also when the data returns to
        an earlier state."""
        with session_scope(self.engine) as s:
            r = s.scalars(
                select(JournalRow).where(JournalRow.entry_id == entry_id).with_for_update()
            ).first()
            if r is None:
                return False
            last = s.scalars(
                select(EvaluationRow)
                .where(EvaluationRow.entry_id == entry_id)
                .order_by(EvaluationRow.id.desc())
                .limit(1)
            ).first()
            if (
                last is not None
                and last.outcome_digest == result.digest
                and last.evaluator_version == EVALUATOR_VERSION
            ):
                r.evaluation_status = last.status
                return False
            if (
                last is not None
                and is_older is not None
                and is_older(result.data_snapshot_id, last.data_snapshot_id)
            ):
                return False  # never replace an evaluation made on newer data
            detail = dict(result.detail)
            revises = None
            if last is not None and last.status == "final":
                revises = last.id
                detail["revision"] = {
                    "revises": last.id,
                    "previous_points": last.realized_points,
                    "previous_hold_points": last.realized_hold_points,
                    "previous_evaluated_at": _iso(last.evaluated_at),
                    "note": "official results changed after the previous final evaluation",
                }
            res = s.execute(
                pg_insert(EvaluationRow)
                .values(
                    entry_id=entry_id,
                    manager_key=manager_key,
                    seq=(last.seq if last is not None else 0) + 1,
                    status=result.status,
                    evaluator_version=EVALUATOR_VERSION,
                    outcome_digest=result.digest,
                    data_snapshot_id=result.data_snapshot_id,
                    realized_points=result.realized_points,
                    realized_hold_points=result.realized_hold_points,
                    realized_recommended_points=result.realized_recommended_points,
                    detail_json=detail,
                    revises_id=revises,
                )
                .on_conflict_do_nothing(index_elements=["entry_id", "seq"])
            )
            if not getattr(res, "rowcount", 0):
                return False  # another writer appended after the same evaluation first
            r.evaluation_status, r.evaluated_at = result.status, now
            return True

    def set_verification(
        self, entry_id: str, verdict: str, detail: dict[str, Any], now: datetime
    ) -> None:
        with session_scope(self.engine) as s:
            s.execute(
                update(m.DecisionJournalRow)
                .where(m.DecisionJournalRow.entry_id == entry_id)
                .values(verification=verdict, verification_json=detail, verified_at=now)
                .execution_options(synchronize_session=False)
            )


# ------------------------------------------------------------------ evaluation


@dataclass(frozen=True)
class EvaluationResult:
    status: str
    digest: str
    realized_points: float | None
    realized_hold_points: float | None
    realized_recommended_points: float | None
    detail: dict[str, Any]
    data_snapshot_id: str | None


def _band(x: float | None, lo: float | None, hi: float | None) -> str | None:
    if x is None or lo is None or hi is None:
        return None
    return "below" if x < lo else ("above" if x > hi else "within")


_BAND_TEXT = {"below": "below", "within": "inside", "above": "above"}


def _digest(payload: dict[str, Any]) -> str:
    return content_hash({"evaluator": EVALUATOR_VERSION, **payload}, length=32)


def _result(
    status: str,
    reason: str,
    snapshot_id: str | None,
    extra: dict[str, Any] | None = None,
    digest_extra: dict[str, Any] | None = None,
) -> EvaluationResult:
    detail = {"evaluator": EVALUATOR_VERSION, "reason": reason, "caveat": CAVEAT, **(extra or {})}
    return EvaluationResult(
        status=status,
        digest=_digest({"status": status, "reason": reason, **(digest_extra or {})}),
        realized_points=None,
        realized_hold_points=None,
        realized_recommended_points=None,
        detail=detail,
        data_snapshot_id=snapshot_id,
    )


def _score(
    plan: PlanEvidence | None,
    positions: dict[int, Position],
    pts: dict[int, int],
    mins: dict[int, int],
    rs: Ruleset,
) -> dict[str, Any] | None:
    """Official gameweek points of a stored plan: the domain rules (auto-substitutions in bench
    order, vice-captain fallback, Bench Boost / Triple Captain), minus its hit points."""
    if plan is None:
        return None
    lu = plan.lineup()
    if lu is None:
        return None
    chip_type = rs.chips.by_id(plan.chip).type.value if plan.chip else None
    r = gameweek_points(lu, positions, pts, mins, rs, chip_type)
    gross = int(r["points"])  # type: ignore[call-overload]
    return {
        "label": plan.label,
        "points": gross - plan.hit_points,
        "gross": gross,
        "hit_points": plan.hit_points,
        "auto_subs": [list(x) for x in r["auto_subs"]],  # type: ignore[attr-defined]
        "armband": r["armband"],
        "multiplier": r["multiplier"],
        "counting": list(r["counting"]),  # type: ignore[call-overload]
    }


def evaluate_decision(
    ev: DecisionEvidence,
    action: str,
    ds: Any,
    now: datetime,
    snapshot_id: str | None,
) -> EvaluationResult:
    """Evaluate one recorded decision on the results in ``ds`` (pure: no database).

    * pending — no relevant match of the gameweek has been played yet (or the decision is
      deferred and the deadline has not passed);
    * partial — some relevant fixtures are not played or not recorded yet (no totals: missing
      players would be auto-substituted as if they had not played), or all are recorded but not
      final yet — FPL has not finalised the gameweek, or the rows were captured before the
      ruleset's finalisation lag (provisional totals, marked as such);
    * final — FPL finalised the gameweek, every relevant fixture is final and its result rows
      were captured after they were final;
    * unavailable — nothing can be evaluated honestly (no action recorded, or the chosen plan's
      lineup was not stored), with the reason. A "something else" decision gets there once the
      reference totals (holding, the recommended plan) are final, and is pending or partial
      until then, so the hourly sweep keeps it.

    Relevant: the fixtures of the decision-time clubs of the players in the scored lineups
    (chosen, HOLD, recommended); the outcome digest covers only those fixtures and players.
    """
    deadline = _parse(ev.deadline_at)
    if action == "DEFER":
        if deadline is not None and now >= deadline:
            return _result("unavailable", "no action was recorded before the deadline", snapshot_id)
        return _result("pending", "waiting for your decision", snapshot_id)
    season, gw = ev.season, ev.gameweek
    rs = load_ruleset(season)
    # the players the evaluation scores: the lineups of the chosen plan, HOLD and the
    # recommended plan (other plans of a workbench run are context, not outcomes)
    scored = {
        int(c)
        for plan in (ev.chosen, ev.hold, ev.recommended)
        if plan is not None
        for c in [*(plan.starters or []), *(plan.bench or [])]
    } or {int(c) for c in ev.positions}
    teams = {int(ev.teams[str(c)]) for c in scored if str(c) in ev.teams}
    fin = gameweek_finality(ds, season, gw, teams)  # the shared finality rule
    gw_status = fin.gameweek_status
    current = set(fin.fixtures)
    decided = {int(i) for t, ids in ev.fixtures.items() if int(t) in teams for i in ids}
    pm = _season_frame(ds, "player_match", season)
    pm = pm[pm["gw"] == gw]
    fixtures: dict[str, Any] = {
        **fin.counts(),
        "moved_out": sorted(decided - current),  # postponed or moved since the decision
        "moved_in": sorted(current - decided),  # (re)scheduled into this gameweek since then
    }
    fx_state = fin.digest_state()
    rows = pm[pm["player_code"].isin(list(scored))]
    row_state = sorted(
        (
            int(r.player_code),
            int(r.fixture_id),
            int(r.minutes),
            int(r.points),
            bool(r.finalized) if pd.notna(r.finalized) else False,
        )
        for r in rows.itertuples(index=False)
    )
    dig = {"fixtures": fx_state, "rows": row_state, "gw_status": gw_status, "action": action}
    unknown = action == "UNKNOWN" or ev.chosen is None
    not_recorded = (
        "what you actually did was not recorded, so it cannot be evaluated; holding and the "
        "recommended plan are scored for reference once the results are final"
    )
    if fin.state() == "not_started":
        return _result(
            "pending",
            not_recorded if unknown else "no relevant match of this gameweek has been played yet",
            snapshot_id,
            {"fixtures": fixtures},
            dig,
        )
    so_far = {
        str(c): {"minutes": int(v["minutes"]), "points": int(v["points"])}
        for c, v in rows.groupby("player_code")[["minutes", "points"]].sum().iterrows()
    }
    if not fin.complete:
        return _result(
            "partial",
            not_recorded
            if unknown
            else f"{fixtures['recorded']} of {len(current)} relevant fixtures recorded "
            "so far: totals wait until every one is in (a player whose match is still to come "
            "would otherwise count as not playing)",
            snapshot_id,
            {"fixtures": fixtures, "players_so_far": so_far},
            dig,
        )
    official, final = fin.officially_final, fin.final
    status = "final" if final else "partial"
    agg = rows.groupby("player_code")[["minutes", "points"]].sum()
    pts = {int(c): int(v) for c, v in agg["points"].items()}
    mins = {int(c): int(v) for c, v in agg["minutes"].items()}
    positions = {int(c): Position(p) for c, p in ev.positions.items()}
    chosen = _score(ev.chosen, positions, pts, mins, rs)
    hold = _score(ev.hold, positions, pts, mins, rs)
    recommended = _score(ev.recommended, positions, pts, mins, rs)
    statements: list[str] = []
    detail: dict[str, Any] = {
        "evaluator": EVALUATOR_VERSION,
        "provisional": not final,
        "fixtures": fixtures,
        "chosen": chosen,
        "hold": hold,
        "recommended": recommended,
        "caveat": CAVEAT,
    }
    if not final:
        statements.append(
            "Every relevant match is recorded but FPL has not finalised the gameweek yet: "
            "these totals are provisional (bonus points can still change)."
            if not official
            else "FPL has finalised the gameweek, but the results loaded so far were captured "
            "before they were final: these totals are provisional until the results are loaded "
            "again (bonus points can still change)."
        )
    if fixtures["moved_out"]:
        statements.append(
            f"{len(fixtures['moved_out'])} fixture(s) known when you decided moved out of this "
            "gameweek; FPL scores them in the gameweek they are played in."
        )
    if unknown:
        # the action itself is never scored; the reference totals stay provisional ("partial")
        # until the results are final, so the scheduler keeps the entry due until then
        detail["reason"] = not_recorded
        detail["statements"] = [
            *statements,
            *([f"Holding would have scored {hold['points']}."] if hold else []),
            *(
                [f"{ev.hold.lineup_note}."]
                if hold and ev.hold.lineup_source == "reconstructed"
                else []
            ),
            *(
                [f"The recommended plan would have scored {recommended['points']}."]
                if recommended
                else []
            ),
        ]
        status = "unavailable" if final else "partial"
        return EvaluationResult(
            status=status,
            digest=_digest({"status": status, **dig}),
            realized_points=None,
            realized_hold_points=float(hold["points"]) if hold else None,
            realized_recommended_points=float(recommended["points"]) if recommended else None,
            detail=detail,
            data_snapshot_id=snapshot_id,
        )
    c = ev.chosen
    if chosen is None or c is None:
        return _result(
            "unavailable",
            "the chosen plan's lineup was not stored, so its points cannot be computed",
            snapshot_id,
            {"fixtures": fixtures},
            dig,
        )
    statements.append(
        f"Your recorded action scored {chosen['points']} points"
        + (
            f" ({chosen['gross']} before {chosen['hit_points']} hit points)."
            if chosen["hit_points"]
            else "."
        )
    )
    if c.expected is not None:
        band = _band(chosen["points"], c.expected.p10, c.expected.p90)
        detail["expected_band"] = band
        rng = (
            f" (80% range {c.expected.p10:.0f}–{c.expected.p90:.0f})"
            if c.expected.p10 is not None and c.expected.p90 is not None
            else ""
        )
        statements.append(
            f"When you decided, {c.expected.mean:.1f} points were expected{rng}"
            + (f"; the result was {_BAND_TEXT[band]} that range." if band else ".")
            + (
                " That expectation is for the plan with its own captain; you changed the armband."
                if ev.captain_override
                else ""
            )
        )
    if hold is not None:
        diff = chosen["points"] - hold["points"]
        detail["difference_vs_hold"] = diff
        # the same squad, XI and bench as HOLD (a workbench plan that holds this week but was
        # solved on its own can field another XI or armband: that is a different action)
        same_xi = (
            c.action == "HOLD"
            and c.same_moves(ev.hold)
            and (c.starters, c.bench) == (ev.hold.starters, ev.hold.bench)
        )
        same_armband = (c.captain, c.vice_captain) == (ev.hold.captain, ev.hold.vice_captain)
        if same_xi and (same_armband or ev.captain_override):
            if same_armband:
                tail = "; the difference to holding is zero by construction."
            elif diff:
                tail = f" ({diff:+d} from your armband change)."
            else:
                tail = "; your armband change made no difference to the total this gameweek."
            statements.append(f"You held: holding scored {hold['points']}{tail}")
        else:
            text_ = f"Holding would have scored {hold['points']}: {diff:+d} for your action."
            gain = c.gain_vs_hold
            if (
                gain is not None
                and not ev.captain_override
                and ev.hold.armband_as_stored is not False
            ):
                gb = _band(float(diff), gain.p10, gain.p90)
                detail["difference_band"] = gb
                text_ += (
                    f" Expected difference when you decided: {gain.mean:+.1f} (80% range "
                    f"{gain.p10:+.0f} to {gain.p90:+.0f}; P(beats holding) "
                    f"{gain.probability_positive:.0%})"
                    + (f"; the realised difference was {_BAND_TEXT[gb]} that range." if gb else ".")
                )
            statements.append(text_)
        if ev.hold.lineup_source == "reconstructed":
            statements.append(f"{ev.hold.lineup_note}.")
    else:
        statements.append(f"HOLD could not be scored: {ev.hold.lineup_note or 'no lineup stored'}.")
    if recommended is not None and not (
        c.same_moves(ev.recommended)
        and (c.captain, c.vice_captain) == (ev.recommended.captain, ev.recommended.vice_captain)
    ):
        detail["difference_vs_recommended"] = chosen["points"] - recommended["points"]
        statements.append(
            f"The recommended plan would have scored {recommended['points']}: "
            f"{chosen['points'] - recommended['points']:+d} for your action."
        )
    if (
        ev.captain_override
        and ev.offered_captain is not None
        and ev.offered_vice_captain is not None
    ):
        plan_armband = c.model_copy(
            update={"captain": ev.offered_captain, "vice_captain": ev.offered_vice_captain}
        )
        cf = _score(plan_armband, positions, pts, mins, rs)
        if cf is not None:
            detail["plan_armband"] = cf
            statements.append(
                f"With the plan's own captain the same squad would have scored {cf['points']} "
                f"({chosen['points'] - cf['points']:+d} for your armband choice)."
            )
    cap = c.captain
    if cap is not None:
        if chosen["armband"] == cap:
            statements.append(
                f"Your captain played and wore the armband (×{chosen['multiplier']})."
            )
        elif chosen["armband"] is not None:
            statements.append(
                "Your captain did not play: the vice-captain took the armband "
                f"(×{chosen['multiplier']})."
            )
        else:
            statements.append("Neither your captain nor your vice-captain played: no armband.")
    if chosen["auto_subs"]:
        statements.append(f"{len(chosen['auto_subs'])} automatic substitution(s) were made.")
    squad = list(dict.fromkeys([*(c.starters or []), *(c.bench or [])]))
    detail["players"] = [
        {
            "player_code": p,
            "role": "starter" if p in (c.starters or []) else "bench",
            "minutes": mins.get(p, 0),
            "points": pts.get(p, 0),
            "counted": p in chosen["counting"],
            "xp_next": (ev.player_expectations.get(str(p)) or {}).get("xp_next"),
            "start_prob": (ev.player_expectations.get(str(p)) or {}).get("start_prob"),
        }
        for p in squad
    ]
    detail["statements"] = statements
    return EvaluationResult(
        status=status,
        digest=_digest({"status": status, **dig}),
        realized_points=float(chosen["points"]),
        realized_hold_points=float(hold["points"]) if hold else None,
        realized_recommended_points=float(recommended["points"]) if recommended else None,
        detail=detail,
        data_snapshot_id=snapshot_id,
    )


def legacy_unavailable(snapshot_id: str | None) -> EvaluationResult:
    return _result(
        "unavailable",
        "recorded before the journal kept decision-time evidence (feedback only)",
        snapshot_id,
    )


# ------------------------------------------------------------------ first-party verification


def verify_decision(
    ev: DecisionEvidence,
    action: str,
    picks: list[tuple[int, int, bool, bool]],
    active_chip: str | None,
) -> tuple[str, dict[str, Any]]:
    """Compare the recorded action with the official FPL picks of the gameweek: ``picks`` are
    (player_code, pick position 1–15, is_captain, is_vice). ``matches`` / ``differs`` for a
    recorded action, ``observed`` when nothing was recorded to compare with. Reported beside
    the record; it never changes what the manager recorded."""
    ordered = sorted(picks, key=lambda p: p[1])
    actual = {p[0] for p in ordered}
    before = set(ev.squad_before)
    cap = next((p[0] for p in ordered if p[2]), None)
    vice = next((p[0] for p in ordered if p[3]), None)
    seen = {
        "transfers_out": sorted(before - actual),
        "transfers_in": sorted(actual - before),
        "captain": cap,
        "vice_captain": vice,
        "chip": active_chip,
        "starters": [p[0] for p in ordered if p[1] <= 11],
    }
    detail: dict[str, Any] = {"source": f"FPL entry picks, GW{ev.gameweek}", "actual": seen}
    c = ev.chosen
    if action in ("DEFER", "UNKNOWN") or c is None:
        return "observed", detail
    planned = set(c.starters or []) | set(c.bench or [])
    if not planned:
        planned = (before - set(c.sells)) | set(c.buys)
    diffs = []
    if actual != planned:
        diffs.append("squad")
    if cap != c.captain:
        diffs.append("captain")
    if vice != c.vice_captain:
        diffs.append("vice_captain")
    chip_type = None
    if c.chip:
        chip_type = c.chip.rsplit("_", 1)[0]
    if (active_chip or None) != (chip_type or None):
        diffs.append("chip")
    detail["differences"] = diffs
    return ("matches" if not diffs else "differs"), detail


# ------------------------------------------------------------------ the evaluation job


def exported_before(settings: Any, engine: Any, own: str | None, other: str | None) -> bool:
    """Was snapshot ``own`` provably exported before ``other``? Export times only — unlike the
    lifecycle's staleness rule, a pinned deployment (a data rollback) is not "older than
    everything": an evaluation on the pinned snapshot replaces one made on an earlier export
    and never one made on a later export. Unknown times prove nothing (False)."""
    from fpl_api.lifecycle import snapshot_as_of  # noqa: PLC0415 (import cycle)

    if not own or not other or own == other:
        return False
    a = snapshot_as_of(settings, own, engine=engine)
    b = snapshot_as_of(settings, other, engine=engine)
    return a is not None and b is not None and a < b


def run_evaluations(
    svc: Any, manager_key: str, entry_id: str | None = None, now: datetime | None = None
) -> tuple[int, int]:
    """The ``journal`` job body: evaluate a manager's due decisions on the newest results this
    process has loaded, and check the official picks once the deadline has passed. Returns
    (evaluations written, decisions looked at); unchanged inputs write nothing."""
    if svc.engine is None:
        raise RuntimeError("database required")
    now = now or clock()
    store = JournalStore(svc.engine)
    due = store.due(manager_key, entry_id, now)
    if not due:
        return 0, 0
    ds, snap = svc.data.ds, svc.data.snapshot_id

    def is_older(own: str | None, latest: str | None) -> bool:
        return exported_before(svc.settings, svc.engine, own, latest)

    written = 0
    for e in due:
        if e["evidence"]:
            ev = DecisionEvidence.model_validate(e["evidence"])
            res = evaluate_decision(ev, e["action"], ds, now, snap)
        else:
            ev, res = None, legacy_unavailable(snap)
        written += store.save_evaluation(e["entry_id"], e["manager_key"], res, now, is_older)
        if ev is not None:
            _verify(svc, store, e, ev, ds, now)
    return written, len(due)


def _verify(
    svc: Any,
    store: JournalStore,
    e: dict[str, Any],
    ev: DecisionEvidence,
    ds: Any,
    now: datetime,
) -> None:
    from fpl_api.services import sources_config  # noqa: PLC0415 (import cycle)
    from fpl_ingestion.http import HttpStatusError, SourceUnavailableError  # noqa: PLC0415
    from fpl_ingestion.sources.fpl_api import FplApiClient  # noqa: PLC0415
    from fpl_ingestion.sources.fpl_api_schemas import CHIP_NAME_MAP  # noqa: PLC0415

    def unavailable(reason: str, retryable: bool) -> None:
        store.set_verification(
            e["entry_id"], "unavailable", {"reason": reason, "retryable": retryable}, now
        )

    current = e.get("verification")
    # a settled check stays; a retryable failure is retried (the sweep stops after its window,
    # an explicit "evaluate now" still retries)
    if current is not None and not (
        current == "unavailable" and (e.get("verification_detail") or {}).get("retryable")
    ):
        return
    deadline = _parse(ev.deadline_at)
    if deadline is None:
        unavailable(
            "the gameweek's deadline is not known, so its official picks cannot be read", False
        )
        return
    if now < deadline:
        return  # the official picks of a gameweek exist only after its deadline

    state = (
        svc.states.get(ev.state_id, e["manager_key"])
        if ev.state_id and svc.states is not None
        else None
    )
    manager_id = getattr(state, "manager_id", None)
    if manager_id is None:
        unavailable("the squad was entered manually: there is no FPL ID to check it against", False)
        return
    if not svc.settings.live_sync_enabled:
        unavailable("the live FPL source is disabled by configuration", True)
        return
    try:
        _, picks = FplApiClient.from_config(sources_config()).entry_picks(manager_id, ev.gameweek)
    except (HttpStatusError, SourceUnavailableError, ValueError) as exc:
        unavailable(f"the FPL API did not return the gameweek's picks ({type(exc).__name__})", True)
        return
    players = _season_frame(ds, "players", ev.season)
    by_element = {int(r.source_id): int(r.player_code) for r in players.itertuples(index=False)}
    mapped, unknown = [], []
    for pk in picks.picks:
        code = by_element.get(int(pk.element))
        if code is None:
            unknown.append(int(pk.element))
        else:
            mapped.append((code, int(pk.position), bool(pk.is_captain), bool(pk.is_vice_captain)))
    if unknown:
        unavailable(f"{len(unknown)} picked player(s) are not in the loaded data yet", True)
        return
    chip = CHIP_NAME_MAP.get(picks.active_chip) if picks.active_chip else None
    verdict, detail = verify_decision(ev, e["action"], mapped, chip)
    detail["retryable"] = False
    store.set_verification(e["entry_id"], verdict, detail, now)
