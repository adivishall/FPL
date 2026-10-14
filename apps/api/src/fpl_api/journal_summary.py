"""A manager's own recorded decisions, summarised (M1.5; ``season-evaluation.md`` §9).

Computed on read from the M1.4 journal (no table of its own, so nothing new to export or erase),
for one manager only — the route applies the journal's ownership checks:

* **Counts** of everything recorded, including decisions recorded after the deadline (counted,
  never scored), replaced ones and those without decision-time evidence.
* **Outcome rows** only for active decisions recorded before the deadline whose evaluation is
  final; realised points are the evaluator's, from official results — never a value the manager
  typed. Per decision first; "first gameweek only".
* **Range counts** against the binomial typical range for that many decisions, beside the
  published past-season split of the same check (``ml/reports/backtest.json``
  ``squad_calibration``); totals as paired sums with their n; an interval only from six scored
  non-HOLD decisions (t-interval and exact sign-flip test, as for the live record).
* **Armband changes.** A decision-time expectation belongs to the plan as the engine built it,
  with its own captain; when the manager changed the armband (or HOLD's stored armband was not
  kept) the row is shown but left out of the range counts and the paired sums, as the journal's
  own evaluation does.

Nothing here grades decisions or estimates the effect of following Copilot.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from scipy import stats
from sqlalchemy import Engine, select

from fpl_forecasting import season_eval as se
from fpl_storage import models as m
from fpl_storage.db import session_scope

MIN_FOR_INTERVAL = 6
LIMITS = (
    "You choose which decisions to record and which advice to follow, so these are not a sample "
    "of anything.",
    "A decision that differs from the recommendation may use information Copilot did not have.",
    "Only the plans stored when you decided are scored; other alternatives are not.",
    "Each decision is scored on its first gameweek only; later weeks of a transfer are not "
    "counted.",
    "A handful of decisions, in gameweeks that share fixtures and luck, cannot show a trend.",
    "Nothing here estimates the effect of following Copilot.",
)


def typical_range(n: int, p: float = 0.8) -> list[int]:
    """The 10th–90th percentile of Binomial(n, p): how many of n outcomes inside an honest 80 %
    range would be unsurprising."""
    return [int(stats.binom.ppf(0.1, n, p)), int(stats.binom.ppf(0.9, n, p))]


def _f(v: Any) -> float | None:
    return float(v) if v is not None else None


def past_season_split(backtest_report: Path | None) -> dict[str, Any] | None:
    """The published engine squad-level range check (non-chip weeks, 2023-26)."""
    if backtest_report is None:
        return None
    try:
        sc = json.loads(backtest_report.read_text()).get("squad_calibration") or {}
    except (OSError, ValueError):
        return None
    if not sc:
        return None
    return {
        "below": float(sc["below_p10"]),
        "inside": float(sc["coverage_p10_p90"]),
        "above": float(sc["above_p90"]),
        "weeks": int(sc["weeks"]),
        "source": "ml/reports/backtest.md (engine plans, non-chip weeks, 2023-26)",
    }


def summary(
    engine: Engine,
    manager_key: str,
    season: str | None = None,
    backtest_report: Path | None = None,
) -> dict[str, Any]:
    with session_scope(engine) as s:
        q = select(m.DecisionJournalRow).where(m.DecisionJournalRow.manager_key == manager_key)
        if season is not None:
            q = q.where(m.DecisionJournalRow.season == season)
        entries = s.scalars(
            q.order_by(m.DecisionJournalRow.gameweek, m.DecisionJournalRow.id)
        ).all()
        latest: dict[str, m.DecisionEvaluationRow] = {}
        for e in entries:
            ev = s.scalars(
                select(m.DecisionEvaluationRow)
                .where(m.DecisionEvaluationRow.entry_id == e.entry_id)
                .order_by(m.DecisionEvaluationRow.seq.desc())
                .limit(1)
            ).first()
            if ev is not None:
                latest[e.entry_id] = ev
        active = [e for e in entries if e.state == "active"]
        counts = {
            "recorded": len(entries),
            "active": len(active),
            "replaced": sum(e.state != "active" for e in entries),
            "recorded_after_deadline": sum(e.prospective is False for e in active),
            "without_decision_time_evidence": sum(not e.evidence_json for e in active),
            **{
                f"evaluation_{k}": v
                for k, v in Counter(e.evaluation_status for e in active).items()
            },
        }
        rows: list[dict[str, Any]] = []
        for e in active:
            ev = latest.get(e.entry_id)
            if not (e.prospective and e.evidence_json and ev is not None and ev.status == "final"):
                continue
            evidence = e.evidence_json or {}
            chosen = evidence.get("chosen") or {}
            expected = chosen.get("expected") or {}
            gain = chosen.get("gain_vs_hold") or {}
            detail = ev.detail_json or {}
            armband_changed = bool(evidence.get("captain_override"))
            hold_armband_kept = (evidence.get("hold") or {}).get("armband_as_stored") is not False
            rows.append(
                {
                    "entry_id": e.entry_id,
                    "gameweek": e.gameweek,
                    "action": e.action,
                    "recommended_action": e.recommended_action,
                    "adherence": e.followed,
                    "realised_points": _f(ev.realized_points),
                    "expected_points": _f(expected.get("mean")),
                    "expected_range": [_f(expected.get("p10")), _f(expected.get("p90"))],
                    "range": detail.get("expected_band"),
                    "difference_vs_hold": _f(detail.get("difference_vs_hold")),
                    "expected_difference_vs_hold": _f(gain.get("mean")),
                    "difference_range": detail.get("difference_band"),
                    # the expectation is the engine's plan with its own armband: comparable
                    # with the result only when the manager kept that armband
                    "comparable": not armband_changed,
                    "comparable_vs_hold": not armband_changed and hold_armband_kept,
                    "evaluation": {"id": ev.id, "seq": ev.seq, "evaluated_at": ev.evaluated_at},
                }
            )
        actions = dict(Counter(e.action for e in active))
        adherence = dict(Counter(e.followed or "unrecorded" for e in active))
    return {
        "manager_key": manager_key,
        "season": season,
        "counts": counts,
        "actions": actions,
        "adherence": adherence,
        "outcomes": _outcomes(rows, past_season_split(backtest_report)),
        "limits": list(LIMITS),
        "caveat": "Your own records, private to you. Behaviour is described, not graded.",
    }


def _outcomes(rows: list[dict[str, Any]], past: dict[str, Any] | None) -> dict[str, Any]:
    comparable = [r for r in rows if r["comparable"]]
    ranged = [r for r in comparable if r["range"] in ("below", "within", "above")]
    n = len(ranged)
    range_counts = {
        "below": sum(r["range"] == "below" for r in ranged),
        "inside": sum(r["range"] == "within" for r in ranged),
        "above": sum(r["range"] == "above" for r in ranged),
        "n": n,
        "typical_inside": typical_range(n) if n else None,
        "past_seasons": past,
        "armband_changed_excluded": len(rows) - len(comparable),
    }
    vs_expected = [
        r["realised_points"] - r["expected_points"]
        for r in comparable
        if r["realised_points"] is not None and r["expected_points"] is not None
    ]
    non_hold = [
        r["difference_vs_hold"]
        for r in rows
        if r["comparable_vs_hold"] and r["action"] != "HOLD" and r["difference_vs_hold"] is not None
    ]
    vs_hold: dict[str, Any] = {
        "n": len(non_hold),
        "sum": float(sum(non_hold)) if non_hold else None,
        "direction": {
            "positive": sum(d > 0 for d in non_hold),
            "zero": sum(d == 0 for d in non_hold),
            "negative": sum(d < 0 for d in non_hold),
        },
        "label": "sum of decision-gameweek differences (your action − holding), non-HOLD only",
    }
    if len(non_hold) >= MIN_FOR_INTERVAL:
        vs_hold["interval_95_mean"] = se.t_interval(non_hold)
        vs_hold["p_sign_flip"] = se.sign_flip_p(non_hold)
    else:
        vs_hold["interval_note"] = (
            f"an interval needs at least {MIN_FOR_INTERVAL} scored decisions other than HOLD"
        )
    return {
        "rows": rows,
        "range_counts": range_counts,
        "realised_minus_expected": {
            "n": len(vs_expected),
            "sum": float(sum(vs_expected)) if vs_expected else None,
            "label": "sum of decision-gameweek differences (realised − expected when you decided)",
        },
        "vs_hold": vs_hold,
    }
