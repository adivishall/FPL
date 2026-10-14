"""Season evaluation of the live record (M1.5; ``docs/architecture/season-evaluation.md`` §3, §8).

Scores the frozen checkpoints of the forecast record (``fpl_api.ledger``) against official
results, using the single finality rule (``fpl_storage.finality``) and the statistics of
``fpl_forecasting.season_eval``:

* **Eligibility (§3.1).** A checkpoint row is scored only when its target gameweek is final,
  the checkpoint is ``prospective`` with its decision-time baselines, the ruleset is the
  season's, and the player's result rows are for exactly the fixtures the forecast assumed, at
  the forecast's club. Everything else is excluded and counted by reason; a missing result row
  is never read as 0.
* **Revisions (§3.2).** The input digest covers the evaluator and metric configuration, the
  checkpoints (id, state, row hash) and, per target gameweek, its finality state and — once
  final — the content hash of its result rows. A new ``season_evaluations`` row is written only
  when the digest changes, under an advisory lock, never from data older than the newest
  evaluation's; it names the evaluation it follows, the trigger and what changed.
* **Read-only elsewhere.** Nothing here writes manager state, recommendations, the journal or
  forecasts; the endpoints that show results read stored rows only.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import Engine, select, text

from fpl_api.ledger import KIND, Checkpoint, load_checkpoint, record_started, season_gameweeks
from fpl_api.provenance import code_digest
from fpl_api.services import DataService
from fpl_domain.rules import load_ruleset
from fpl_forecasting import season_eval as se
from fpl_storage import models as m
from fpl_storage.dataset import CanonicalDataset
from fpl_storage.db import session_scope
from fpl_storage.finality import gameweek_finality

log = structlog.get_logger("fpl_api.season")
SCOPE = "live"
HORIZONS = (0, 1, 2, 3, 4)
TRIGGERS = (
    "first",
    "new_gameweek_final",
    "outcome_correction",
    "evaluator_change",
    "record_change",  # a checkpoint captured, or a window's finality state moved on
)


def _h(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def current_season(ds: CanonicalDataset) -> str:
    return str(sorted(ds["gameweeks"]["season"].unique())[-1])


# ----------------------------------------------------------------------------- inputs


@dataclass
class Header:
    id: str
    gameweek: int
    state: str
    rows_sha256: str | None
    baselines: bool
    ruleset_version: str | None
    target_gameweeks: list[int]


def checkpoint_headers(engine: Engine, season: str) -> list[Header]:
    with session_scope(engine) as s:
        rows = s.scalars(
            select(m.ForecastCheckpointRow)
            .where(m.ForecastCheckpointRow.season == season, m.ForecastCheckpointRow.kind == KIND)
            .order_by(m.ForecastCheckpointRow.gameweek)
        ).all()
        return [
            Header(
                id=r.id,
                gameweek=int(r.gameweek),
                state=r.state,
                rows_sha256=r.rows_sha256,
                baselines=bool((r.checks or {}).get("baselines", {}).get("ok")),
                ruleset_version=(r.provenance_json or {}).get("ruleset_version"),
                target_gameweeks=[
                    int(g) for g in (r.populations_json or {}).get("target_gameweeks", [])
                ],
            )
            for r in rows
        ]


def _results(ds: CanonicalDataset, season: str, gw: int) -> pd.DataFrame:
    pm = ds["player_match"]
    return pm[(pm["season"] == season) & (pm["gw"] == gw)]


def window_states(ds: CanonicalDataset, season: str, gws: list[int]) -> dict[int, dict[str, Any]]:
    """Per target gameweek: its finality state and counts and, once final, the content hash of
    its result rows (a correction to official data changes it)."""
    out: dict[int, dict[str, Any]] = {}
    for gw in gws:
        fin = gameweek_finality(ds, season, gw)
        w: dict[str, Any] = {"gw": gw, "state": fin.state(), "counts": fin.counts()}
        if fin.final:
            r = _results(ds, season, gw)[
                ["player_code", "fixture_id", "team_code", "points", "minutes", "starts"]
            ].sort_values(["player_code", "fixture_id"])
            w["outcome_digest"] = hashlib.sha256(r.to_csv(index=False).encode()).hexdigest()
            w["digest_state"] = fin.digest_state()
            w["result_rows"] = len(r)
            w["points_total"] = int(r["points"].sum())
        out[gw] = w
    return out


def target_gameweeks(headers: list[Header]) -> list[int]:
    """Every gameweek a live-record checkpoint forecast (as stored with it)."""
    return sorted({g for h in headers if h.state == "prospective" for g in h.target_gameweeks})


def input_digest(headers: list[Header], windows: dict[int, dict[str, Any]]) -> str:
    """What the evaluation's numbers depend on (§3.2); the code digest is not part of it."""
    return _h(
        {
            "config": se.metric_config(),
            "checkpoints": [(h.id, h.state, h.rows_sha256, h.baselines) for h in headers],
            "windows": {
                str(g): (w["state"], w.get("outcome_digest"), w.get("digest_state"))
                for g, w in sorted(windows.items())
            },
        }
    )


# ----------------------------------------------------------------------------- scoring


def outcomes(ds: CanonicalDataset, season: str, gw: int) -> pd.DataFrame:
    """Per player: the fixture ids and clubs of their result rows and the summed outcome."""
    r = _results(ds, season, gw)
    if r.empty:
        return pd.DataFrame(
            columns=["player_code", "fixture_ids", "teams", "y", "minutes_actual", "starts_actual"]
        )
    g = r.groupby("player_code")
    return pd.DataFrame(
        {
            "fixture_ids": g["fixture_id"].agg(lambda x: ",".join(str(int(i)) for i in sorted(x))),
            "teams": g["team_code"].agg(lambda x: ",".join(str(int(i)) for i in sorted(set(x)))),
            "y": g["points"].sum().astype(float),
            "minutes_actual": g["minutes"].sum().astype(float),
            "starts_actual": g["starts"].sum().astype(float),
        }
    ).reset_index()


@dataclass
class Scored:
    rows: pd.DataFrame  # standardised score inputs + population flags, eligible rows only
    exclusions: Counter[str] = field(default_factory=Counter)
    as_served_se: list[float] = field(default_factory=list)  # schedule-changed, realised
    clubs_gained_fixture: int = 0
    per_checkpoint: dict[str, dict[str, Any]] = field(default_factory=dict)


def score_checkpoint(
    ck: Checkpoint, ds: CanonicalDataset, season: str, final_gws: set[int], out: Scored
) -> None:
    """Add a checkpoint's eligible rows (§3.1) to ``out``; count every exclusion."""
    h = ck.header
    rows = ck.rows.reset_index(drop=True)
    finals: list[int] = []
    stats: dict[str, Any] = {"rows": len(rows), "scored": 0, "final_target_gameweeks": finals}
    out.per_checkpoint[h.id] = stats
    if h.state != "prospective":
        return
    rs = (h.provenance_json or {}).get("ruleset_version")
    if rs != load_ruleset(season).ruleset_version:
        out.exclusions["ruleset_changed"] += len(rows)
        return
    if ck.clim_counts is None or ck.mc_counts is None or ck.mc_lo is None or ck.clim_lo is None:
        in_final = rows["target_gw"].isin(final_gws)
        out.exclusions["baselines_unavailable"] += int(in_final.sum())
        return
    fixtures = ds["fixtures"]
    for gw in sorted(int(x) for x in rows["target_gw"].unique()):
        if gw not in final_gws:
            continue
        finals.append(gw)
        idx = np.flatnonzero(rows["target_gw"].to_numpy() == gw)
        sub = rows.iloc[idx].merge(
            outcomes(ds, season, gw), on="player_code", how="left", suffixes=("", "_real")
        )
        # the club's final fixtures in the gameweek (postponements and moves included)
        fx = fixtures[(fixtures["season"] == season) & (fixtures["gw"] == gw)]
        club_fixtures = {
            int(team): ",".join(
                str(int(f))
                for f in sorted(
                    fx.loc[
                        (fx["home_team_code"] == team) | (fx["away_team_code"] == team),
                        "fixture_id",
                    ]
                )
            )
            for team in sub["team_code"].dropna().unique()
        }
        club_final = pd.Series(
            [club_fixtures.get(int(t), "") if pd.notna(t) else "" for t in sub["team_code"]],
            index=sub.index,
        )
        schedule_same = (club_final == sub["fixture_ids"]).to_numpy()
        have = sub["y"].notna().to_numpy()
        same_club = (sub["teams"] == sub["team_code"].astype("Int64").astype(str)).to_numpy()
        complete = (sub["fixture_ids_real"] == sub["fixture_ids"]).to_numpy()
        ok = schedule_same & have & same_club & complete
        changed = ~schedule_same
        out.exclusions["schedule_changed"] += int(changed.sum())
        out.exclusions["outcome_missing"] += int((schedule_same & ~have).sum())
        out.exclusions["club_changed"] += int((schedule_same & have & ~same_club).sum())
        # rows for only some of the forecast's fixtures: never completed with zeros
        out.exclusions["outcome_incomplete"] += int(
            (schedule_same & have & same_club & ~complete).sum()
        )
        served = changed & have
        if served.any():  # the "as served" sensitivity scores these on their realised values
            out.as_served_se += list(
                (sub.loc[served, "mean"].to_numpy(float) - sub.loc[served, "y"].to_numpy(float))
                ** 2
            )
        assumed_by_club: dict[int, set[int]] = {}
        for team, ids in zip(sub["team_code"], sub["fixture_ids"], strict=True):
            if pd.notna(team) and isinstance(ids, str) and ids:
                assumed_by_club.setdefault(int(team), set()).update(int(f) for f in ids.split(","))
        for team, real in club_fixtures.items():  # clubs that gained a fixture after the forecast
            if {int(f) for f in real.split(",") if f} - assumed_by_club.get(team, set()):
                out.clubs_gained_fixture += 1
        if not ok.any():
            continue
        sel = idx[ok]
        good = sub[ok].reset_index(drop=True)
        y = good["y"].to_numpy(float)
        mc = se.distribution_scores(ck.mc_lo, ck.mc_counts[sel], y)
        cl = se.distribution_scores(ck.clim_lo, ck.clim_counts[sel], y)
        good["crps_model"], good["crps_climatology"] = mc["crps"], cl["crps"]
        for k in ("pit80", "pit_lo", "pit_hi"):
            good[k] = mc[k]
        good["checkpoint_id"] = h.id
        good["checkpoint_gw"] = h.gameweek
        out.rows = pd.concat([out.rows, good], ignore_index=True)
        stats["scored"] += len(good)


def _populations(rows: pd.DataFrame) -> dict[str, pd.Series]:
    status = rows.get("status", pd.Series(index=rows.index, dtype=object))
    chance = pd.to_numeric(rows.get("chance_of_playing"), errors="coerce")
    flagged = status.isin(se.FLAGGED_STATUS).fillna(False) | (chance < 100).fillna(False)
    return {
        "all": pd.Series(True, index=rows.index),
        "regulars": (pd.to_numeric(rows["start_rate_long"], errors="coerce").fillna(0) >= 0.5),
        "flagged": flagged.astype(bool),
    }


def evaluate(scored: Scored, final: bool) -> dict[str, Any]:
    """Every summary of the scored rows: population × horizon windows (horizon 0 primary;
    horizons never pooled), the ECE null bands for horizon 0 and the calibration state."""
    rows = scored.rows
    results: dict[str, dict[str, Any]] = {}
    ece_null: dict[str, Any] = {}
    if not rows.empty:
        rows = rows.assign(horizon=rows["target_gw"] - rows["checkpoint_gw"])
        pops = _populations(rows)
        sc = se.row_scores(rows)
        for name, mask in pops.items():
            results[name] = {}
            for h in HORIZONS if name == "all" else (0,):
                sel = mask.to_numpy() & (rows["horizon"].to_numpy() == h)
                if not sel.any():
                    continue
                results[name][f"h{h}"] = se.summarize(se.gameweek_sums(sc[sel]), final=final)
            h0 = mask.to_numpy() & (rows["horizon"].to_numpy() == 0)
            if h0.any():
                ece_null[name] = {
                    "p6": se.ece_null_band(sc.loc[h0, "p6"]),
                    "start": se.ece_null_band(sc.loc[h0, "pstart"]),
                }
    primary = results.get("all", {}).get("h0", {"G": 0})
    return {
        "results": results,
        "ece_null": ece_null,
        "sensitivity": {
            "schedule_changed_rows": len(scored.as_served_se),
            "rmse_as_served_h0_all": _as_served(scored, rows),
        },
        "calibration_state": se.calibration_state(primary),
        "status_u_rows_h0": (
            int(((rows["status"] == "u") & (rows["horizon"] == 0)).sum())
            if not rows.empty and "status" in rows
            else 0
        ),
    }


def _as_served(scored: Scored, rows: pd.DataFrame) -> float | None:
    """RMSE over every scored row plus the schedule-changed ones on their realised values (all
    horizons): exclusions cannot make the forecast look better than it was to users."""
    if rows.empty and not scored.as_served_se:
        return None
    se_ok = ((rows["mean"] - rows["y"]) ** 2).tolist() if not rows.empty else []
    allv = se_ok + scored.as_served_se
    return float(np.sqrt(np.mean(allv))) if allv else None


# ----------------------------------------------------------------------------- the job


@dataclass
class Plan:
    season: str
    headers: list[Header]
    windows: dict[int, dict[str, Any]]
    digest: str
    last_gw: int


def plan(engine: Engine, ds: CanonicalDataset, season: str | None = None) -> Plan:
    season = season or current_season(ds)
    headers = checkpoint_headers(engine, season)
    g = ds["gameweeks"]
    last_gw = int(g.loc[g["season"] == season, "gw"].max())
    windows = window_states(ds, season, target_gameweeks(headers))
    return Plan(season, headers, windows, input_digest(headers, windows), last_gw)


def latest_evaluation(engine: Engine, season: str) -> m.SeasonEvaluationRow | None:
    with session_scope(engine) as s:
        row: m.SeasonEvaluationRow | None = s.scalars(
            select(m.SeasonEvaluationRow)
            .where(m.SeasonEvaluationRow.season == season, m.SeasonEvaluationRow.scope == SCOPE)
            .order_by(m.SeasonEvaluationRow.seq.desc())
            .limit(1)
        ).first()
        if row is not None:
            s.expunge(row)
    return row


def is_due(engine: Engine, data: DataService) -> bool:
    """The scheduler's check: there is something to evaluate and its digest is new."""
    p = plan(engine, data.ds)
    if not p.headers:
        return False
    last = latest_evaluation(engine, p.season)
    return last is None or last.input_digest != p.digest


def _changes(
    prev: m.SeasonEvaluationRow | None,
    windows: list[dict[str, Any]],
    ex: dict[str, Any],
    metrics: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    scored = {w["gw"] for w in windows if w.get("scored")}
    head = metrics["results"].get("all", {}).get("h0", {})
    headline = {
        "G": head.get("G", 0),
        "rmse_model": (head.get("levels") or {}).get("rmse", {}).get("model"),
        "crps_model": (head.get("levels") or {}).get("crps", {}).get("model"),
        "pit80": (head.get("levels") or {}).get("pit80"),
    }
    if prev is None:
        return "first", {
            "newly_scored": sorted(scored),
            "revised": [],
            "headline": {"after": headline},
        }
    prev_windows = {w["gw"]: w for w in prev.windows_json}
    prev_scored = {g for g, w in prev_windows.items() if w.get("scored")}
    revised = [
        {
            "gw": w["gw"],
            "result_rows": [prev_windows[w["gw"]].get("result_rows"), w.get("result_rows")],
            "points_total": [prev_windows[w["gw"]].get("points_total"), w.get("points_total")],
        }
        for w in windows
        if w.get("scored")
        and w["gw"] in prev_scored
        and w.get("outcome_digest") != prev_windows[w["gw"]].get("outcome_digest")
    ]
    prev_ex = prev.exclusions_json.get("by_reason", {})
    delta = {
        k: int(ex["by_reason"].get(k, 0)) - int(prev_ex.get(k, 0))
        for k in set(prev_ex) | set(ex["by_reason"])
    }
    changes = {
        "newly_scored": sorted(scored - prev_scored),
        "revised": revised,
        "exclusion_deltas": {k: v for k, v in delta.items() if v},
        "headline": {
            "before": prev.changes_json.get("headline", {}).get("after"),
            "after": headline,
        },
    }
    if prev.evaluator_version != se.EVALUATOR_VERSION:
        trigger = "evaluator_change"
    elif changes["newly_scored"]:
        trigger = "new_gameweek_final"
    elif revised:
        trigger = "outcome_correction"
    else:
        trigger = "record_change"
    return trigger, changes


def run(engine: Engine, data: DataService) -> str:
    """Evaluate the live record when its inputs changed; returns the evaluation id or why
    nothing was written. Everything is written in one transaction at the end."""
    ds = data.ds
    p = plan(engine, ds)
    if not p.headers:
        return "nothing_to_evaluate"
    last = latest_evaluation(engine, p.season)
    if last is not None and last.input_digest == p.digest:
        return f"unchanged:{last.id}"
    final_gws = {g for g, w in p.windows.items() if w["state"] == "final"}
    scored = Scored(rows=pd.DataFrame())
    for h in p.headers:
        ck = load_checkpoint(engine, h.id)
        score_checkpoint(ck, ds, p.season, final_gws, scored)
    metrics = evaluate(scored, final=p.last_gw in final_gws)
    windows = [
        {
            **w,
            "scored": bool(
                w["state"] == "final"
                and not scored.rows.empty
                and (scored.rows["target_gw"] == g).any()
            ),
        }
        for g, w in sorted(p.windows.items())
    ]
    exclusions = {
        "by_reason": dict(scored.exclusions),
        "clubs_gained_fixture": scored.clubs_gained_fixture,
        "status_u_rows_h0": metrics.pop("status_u_rows_h0"),
    }
    started = record_started(engine)
    snap_path = data.snapshot_path(ds.snapshot_id)
    created = (
        datetime.fromisoformat(json.loads((snap_path / "manifest.json").read_text())["created_at"])
        if snap_path is not None
        else None
    )
    metrics["checkpoints"] = scored.per_checkpoint
    metrics["record_started"] = started.isoformat() if started else None
    with session_scope(engine) as s:
        s.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"season_eval:{p.season}"}
        )
        prev: m.SeasonEvaluationRow | None = s.scalars(
            select(m.SeasonEvaluationRow)
            .where(m.SeasonEvaluationRow.season == p.season, m.SeasonEvaluationRow.scope == SCOPE)
            .order_by(m.SeasonEvaluationRow.seq.desc())
            .limit(1)
        ).first()
        if prev is not None and prev.input_digest == p.digest:
            return f"unchanged:{prev.id}"
        if (
            prev is not None
            and prev.snapshot_created_at is not None
            and created is not None
            and created < prev.snapshot_created_at
        ):
            return f"stale:{ds.snapshot_id} is older than the data of evaluation {prev.id}"
        trigger, changes = _changes(prev, windows, exclusions, metrics)
        row = m.SeasonEvaluationRow(
            season=p.season,
            scope=SCOPE,
            seq=(prev.seq + 1) if prev is not None else 1,
            previous_id=prev.id if prev is not None else None,
            trigger=trigger,
            input_digest=p.digest,
            evaluator_version=se.EVALUATOR_VERSION,
            metric_config_json=json.loads(json.dumps(se.metric_config())),
            data_snapshot_id=ds.snapshot_id,
            snapshot_created_at=created,
            code_digest=code_digest(),
            checkpoint_ids=[h.id for h in p.headers],
            windows_json=clean(windows),
            exclusions_json=clean(exclusions),
            metrics_json=clean(metrics),
            changes_json=clean(changes),
        )
        s.add(row)
        s.flush()
        eid = int(row.id)
    log.info("season_evaluation", season=p.season, id=eid, trigger=trigger, digest=p.digest[:12])
    return f"season_eval:{eid}"


def clean(v: Any) -> Any:
    """JSON-safe (JSONB rejects NaN): non-finite floats become None, numpy scalars Python
    values, tuples and sets lists, timestamps ISO strings."""
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [clean(x) for x in v]
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float):
        return v if np.isfinite(v) else None
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.isoformat()
    return v


# ----------------------------------------------------------------------------- reading


def timeline(engine: Engine, season: str, now: datetime | None = None) -> dict[str, Any]:
    """Every gameweek of the season with its checkpoint state, for the page's timeline."""
    now = now or datetime.now(UTC)
    gws = season_gameweeks(engine, season)
    started = record_started(engine)
    with session_scope(engine) as s:
        cks = {
            int(r.gameweek): r
            for r in s.scalars(
                select(m.ForecastCheckpointRow).where(
                    m.ForecastCheckpointRow.season == season, m.ForecastCheckpointRow.kind == KIND
                )
            ).all()
        }
        items = []
        for g in gws:
            ck = cks.get(g.gameweek)
            if ck is not None:
                state = ck.state
            elif g.cutoff > now:
                state = "upcoming"
            elif started is None or g.cutoff < started:
                state = "before_record"
            else:
                state = "due"
            items.append(
                {
                    "gameweek": g.gameweek,
                    "deadline": g.deadline.isoformat(),
                    "cutoff": g.cutoff.isoformat(),
                    "first_kickoff": g.first_kickoff.isoformat() if g.first_kickoff else None,
                    "results_final_expected": (
                        g.final_expected.isoformat() if g.final_expected else None
                    ),
                    "state": state,
                    "checkpoint_id": ck.id if ck else None,
                    "captured_at": ck.captured_at.isoformat() if ck else None,
                    "promoted_at": ck.promoted_at.isoformat() if ck and ck.promoted_at else None,
                    "reasons": list(ck.reasons) if ck else [],
                    "rows": int(ck.n_rows) if ck else 0,
                    "prediction_run_id": ck.prediction_run_id if ck else None,
                    "build_revision": ck.build_revision if ck else None,
                    "code_digest": ck.code_digest if ck else None,
                }
            )
    upcoming = next((i for i in items if i["state"] in ("upcoming", "due")), None)
    return {
        "record_started": started.isoformat() if started else None,
        "gameweeks": items,
        "next_checkpoint": upcoming,
    }


# ----------------------------------------------------------------------------- past seasons

DEFAULT_REFERENCE_WINDOW = tuple(range(7, 15))  # the first live look's gameweeks (GW7–14)


def load_reference(path: Any) -> dict[str, Any] | None:
    """``ml/reports/season_reference.json`` (``ml/experiments/season_reference.py``)."""
    try:
        data: dict[str, Any] = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data


def reference_window(
    ref: dict[str, Any], gameweeks: list[int], population: str = "all"
) -> dict[str, Any]:
    """The past seasons over the same gameweek numbers, summarised by the same method: per
    season, levels and the mean per-gameweek difference of each primary comparison. Context,
    not a test — never pooled with the live record."""
    out: dict[str, Any] = {"gameweeks": gameweeks, "population": population, "seasons": {}}
    for season, pops in sorted((ref.get("seasons") or {}).items()):
        sums = pd.DataFrame(pops.get(population) or [])
        if sums.empty:
            continue
        sums = sums[sums["target_gw"].isin(gameweeks)]
        if sums.empty:
            continue
        w = se.summarize(sums)
        lv = w["levels"]
        out["seasons"][season] = {
            "G": w["G"],
            "rows": w["rows"],
            "rmse": lv["rmse"],
            "crps": lv["crps"],
            "brier_6": lv["brier_6"],
            "brier_start": lv["brier_start"],
            "pit80": lv["pit80"],
            "citl_6": lv["citl_6"],
            "citl_start": lv["citl_start"],
            "ece_6": lv["ece_6"],
            "ece_start": lv["ece_start"],
            "bias_model": lv["bias_model"],
            "comparisons": {
                k: {
                    "mean": w["comparisons"][k]["mean"],
                    "direction": w["comparisons"][k]["direction"],
                }
                for k in se.PRIMARY
            },
        }
    return out


def drift_signals(live: dict[str, Any], ref: dict[str, Any] | None) -> dict[str, Any] | None:
    """The governance policy's thresholds (``config/models/points.yaml: retraining``) read as
    descriptive monitoring signals on the live record: no action follows (contract §11)."""
    lv = live.get("levels")
    if not lv:
        return None
    seasons = (ref or {}).get("seasons") or {}
    past = [s["rmse"]["model"] for s in seasons.values() if s.get("rmse")]
    ratio = lv["rmse"]["model"] / (sum(past) / len(past)) if past else None
    return {
        "rmse_ratio_to_past_same_weeks": ratio,
        "rmse_ratio_threshold": 1.15,
        "abs_bias": abs(lv["bias_model"]),
        "abs_bias_threshold": 0.4,
        "ece_6": lv["ece_6"],
        "ece_threshold": 0.05,
        "note": "descriptive monitoring signals; serving refits on every forecast, nothing is "
        "changed by them",
    }
