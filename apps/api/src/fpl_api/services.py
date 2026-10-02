"""Application services: data plane (PIT snapshot), forecasts (cached), app state (PostgreSQL).

* **Data** is read from a reproducible canonical snapshot through ``PointInTimeView`` — the API
  serves exactly what a decision at the current cutoff may know.
* **Forecasts** are keyed by (data snapshot, feature version, model config refs, season, GW,
  horizon, sample count, seed); a new snapshot/model/config changes the key (cache invalidation,
  ADR-0009). They are cached in memory and on disk; when ``forecast_on_demand`` is off the API
  only serves precomputed forecasts (the worker computes them).
* **App state** (manager states, recommendations, optimisation runs, journal, jobs) is persisted
  in PostgreSQL; recommendation packages are immutable records.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
import yaml
from sqlalchemy import Engine, select, update

from fpl_api.settings import Settings
from fpl_decision.engine import RecommendationPackage
from fpl_domain.enums import FreshnessStatus
from fpl_domain.freshness import Freshness, FreshnessSla, evaluate_freshness
from fpl_domain.hashing import short_id
from fpl_domain.rules import Ruleset, load_ruleset
from fpl_domain.rules.loader import config_root
from fpl_domain.state import ManagerState
from fpl_features.registry import FEATURE_VERSION
from fpl_forecasting.model_config import minutes_spec, points_spec
from fpl_forecasting.pipeline import Forecast, forecast, train_forecast_models
from fpl_forecasting.walkforward import Cutoff, FeatureCache, cutoffs
from fpl_simulation.engine import SimulationConfig
from fpl_storage import models as m
from fpl_storage.dataset import CanonicalDataset, load_snapshot
from fpl_storage.db import session_scope
from fpl_storage.pit import PointInTimeView, decision_cutoff

REPO_ROOT = Path(__file__).resolve().parents[4]


def sources_config() -> dict[str, Any]:
    with (config_root() / "sources.yaml").open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


class ForecastUnavailable(RuntimeError):
    """No precomputed forecast for the requested key (and on-demand computation is off)."""


@dataclass(frozen=True)
class CurrentContext:
    season: str
    gameweek: int
    deadline: datetime
    cutoff: datetime
    freshness: Freshness
    latest_source_at: datetime | None
    degraded_reasons: tuple[str, ...]

    def block(self, snapshot_id: str) -> dict[str, Any]:
        """The freshness block attached to every data-serving response (§6, §82)."""
        return {
            "data_snapshot_id": snapshot_id,
            "season": self.season,
            "gameweek": self.gameweek,
            "decision_cutoff": self.cutoff.isoformat(),
            "latest_source_at": self.latest_source_at.isoformat()
            if self.latest_source_at
            else None,
            "status": self.freshness.status.value,
            "message": self.freshness.message,
            "degraded": bool(self.degraded_reasons),
            "degraded_reasons": list(self.degraded_reasons),
        }


class DataService:
    def __init__(self, settings: Settings, dataset: CanonicalDataset | None = None) -> None:
        self.settings = settings
        self._ds = dataset
        self._lock = threading.Lock()

    @property
    def ds(self) -> CanonicalDataset:
        if self._ds is None:
            with self._lock:
                if self._ds is None:
                    path = (
                        self.settings.snapshot_dir
                        or sorted((REPO_ROOT / "data" / "snapshots").glob("snap_*"))[-1]
                    )
                    self._ds = load_snapshot(path)
        return self._ds

    @property
    def snapshot_id(self) -> str:
        return self.ds.snapshot_id

    def ruleset(self, season: str) -> Ruleset:
        return load_ruleset(season)

    def current(self, now: datetime | None = None) -> CurrentContext:
        now = now or datetime.now(UTC)
        ds = self.ds
        gws = ds["gameweeks"]
        season = str(sorted(gws["season"].unique())[-1])
        g = gws[gws["season"] == season].sort_values("gw")
        stamps = []
        snaps = ds["player_snapshots"]
        snaps = snaps[snaps["season"] == season]
        if len(snaps):
            stamps.append(pd.Timestamp(snaps["available_at"].max()))
        pm = ds["player_match"]
        pm = pm[pm["season"] == season]
        if len(pm):
            stamps.append(pd.Timestamp(pm["available_at"].max()))
        latest = max(stamps) if stamps else None
        upcoming = g[g["deadline_at"] > latest] if latest is not None else g
        row = upcoming.iloc[0] if len(upcoming) else g.iloc[-1]
        rs = self.ruleset(season)
        deadline = pd.Timestamp(row["deadline_at"]).to_pydatetime()
        cutoff = decision_cutoff(deadline, rs.timing.decision_buffer_minutes).to_pydatetime()
        sla_cfg = sources_config()["freshness_sla"]["bootstrap"]
        fr = evaluate_freshness(
            "bootstrap",
            latest.to_pydatetime() if latest is not None else None,
            now,
            FreshnessSla(**sla_cfg),
        )
        reasons: list[str] = []
        if fr.status in (FreshnessStatus.STALE, FreshnessStatus.EXPIRED):
            reasons.append(f"data {fr.status.value}: serving the last validated snapshot")
        if not self.settings.live_sync_enabled:
            reasons.append("live FPL source disabled by configuration")
        return CurrentContext(
            season=season,
            gameweek=int(row["gw"]),
            deadline=deadline,
            cutoff=cutoff,
            freshness=fr,
            latest_source_at=latest.to_pydatetime() if latest is not None else None,
            degraded_reasons=tuple(reasons),
        )

    def view(self, cutoff: datetime) -> PointInTimeView:
        return PointInTimeView(self.ds, pd.Timestamp(cutoff))

    def names(self, season: str) -> dict[int, str]:
        p = self.ds["players"]
        p = p[p["season"] == season]
        return {int(c): str(n) for c, n in zip(p["player_code"], p["web_name"], strict=True)}

    def teams(self, season: str) -> dict[int, str]:
        t = self.ds["teams"]
        t = t[t["season"] == season]
        return {int(c): str(n) for c, n in zip(t["team_code"], t["short_name"], strict=True)}

    def history_cutoffs(self, season: str) -> list[Cutoff]:
        seasons = sorted(s for s in self.ds["gameweeks"]["season"].unique() if s <= season)
        return cutoffs(self.ds, seasons)


class ForecastService:
    def __init__(self, settings: Settings, data: DataService) -> None:
        self.settings = settings
        self.data = data
        self._mem: dict[str, Forecast] = {}
        self._lock = threading.Lock()

    def key(self, season: str, gw: int, horizon: int, n_sims: int, seed: int) -> str:
        return short_id(
            "fc",
            {
                "snapshot": self.data.snapshot_id,
                "features": FEATURE_VERSION,
                "points": points_spec()[1].ref,
                "minutes": minutes_spec()[1].ref,
                "season": season,
                "gw": gw,
                "horizon": horizon,
                "n_sims": n_sims,
                "seed": seed,
            },
        )

    def _path(self, key: str) -> Path:
        return self.settings.artifact_dir / "forecasts" / f"{key}.joblib"

    def get(
        self,
        season: str,
        gw: int,
        horizon: int,
        n_sims: int | None = None,
        compute: bool | None = None,
    ) -> Forecast:
        n = min(n_sims or self.settings.n_sims, self.settings.n_sims_max)
        seed = points_spec()[0].simulation.seed
        # one canonical forecast per gameweek serves every shorter horizon (no recomputation)
        horizon = max(horizon, self.settings.forecast_horizon)
        k = self.key(season, gw, horizon, n, seed)
        if k in self._mem:
            return self._mem[k]
        path = self._path(k)
        if path.exists():
            fc: Forecast = joblib.load(path)
            self._mem[k] = fc
            return fc
        allowed = self.settings.forecast_on_demand if compute is None else compute
        if not allowed:
            raise ForecastUnavailable(k)
        with self._lock:
            if k in self._mem:
                return self._mem[k]
            hist = self.data.history_cutoffs(season)
            cut = next(c for c in hist if c.season == season and c.gw == gw)
            cache = FeatureCache(self.data.ds, horizon, self.settings.feature_store_dir)
            models = train_forecast_models(cache, cut, hist)
            fc = forecast(cache, cut, models, SimulationConfig(n_sims=n, seed=seed))
            fc.provenance["forecast_key"] = k
            path.parent.mkdir(parents=True, exist_ok=True)
            joblib.dump(fc, path, compress=3)
            self._mem[k] = fc
            return fc

    def features(self, season: str, gw: int, horizon: int) -> pd.DataFrame:
        hist = self.data.history_cutoffs(season)
        cut = next(c for c in hist if c.season == season and c.gw == gw)
        h = max(horizon, self.settings.forecast_horizon)
        frame = FeatureCache(self.data.ds, h, self.settings.feature_store_dir).get(cut).frame
        return frame[frame["target_gw"] < gw + horizon]


# ----------------------------------------------------------------------------- app state


def ensure_season(session: Any, season: str) -> int:
    sid = session.scalar(select(m.SeasonRow.id).where(m.SeasonRow.season_code == season))
    if sid is None:
        row = m.SeasonRow(season_code=season, ruleset_version=load_ruleset(season).ruleset_version)
        session.add(row)
        session.flush()
        sid = row.id
    return int(sid)


@dataclass
class StateStore:
    engine: Engine
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def save(self, manager_key: str, state: ManagerState, source: str) -> str:
        sid = short_id("mstate", {"key": manager_key, "state": state.state_hash})
        with session_scope(self.engine) as s:
            if s.get(m.ManagerStateRow, sid) is None:
                season_id = ensure_season(s, state.season)
                payload = state.model_dump(mode="json")
                payload["manager_key"] = manager_key
                s.add(
                    m.ManagerStateRow(
                        id=sid,
                        manager_id=state.manager_id,
                        season_id=season_id,
                        gw=state.gameweek,
                        captured_at=datetime.now(UTC),
                        bank=state.bank,
                        free_transfers=state.free_transfers,
                        source=source,
                        state_json=payload,
                        state_hash=state.state_hash,
                    )
                )
        return sid

    def latest(self, manager_key: str) -> tuple[str, ManagerState] | None:
        with session_scope(self.engine) as s:
            row = s.scalars(
                select(m.ManagerStateRow)
                .where(m.ManagerStateRow.state_json["manager_key"].astext == manager_key)
                .order_by(m.ManagerStateRow.captured_at.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            payload = {k: v for k, v in row.state_json.items() if k != "manager_key"}
            return row.id, ManagerState.model_validate(payload)


@dataclass
class RecommendationStore:
    engine: Engine

    def save(
        self, pkg: RecommendationPackage, manager_key: str, state_id: str | None, markdown: str
    ) -> str:
        rec_id = pkg.decision_id.replace("dec_", "rec_", 1)
        with session_scope(self.engine) as s:
            if s.get(m.RecommendationRow, rec_id) is not None:
                return rec_id
            season_id = ensure_season(s, pkg.season)
            s.execute(
                update(m.RecommendationRow)
                .where(
                    m.RecommendationRow.payload_json["manager_key"].astext == manager_key,
                    m.RecommendationRow.gw == pkg.gameweek,
                    m.RecommendationRow.status == "active",
                )
                .values(status="superseded")
            )
            alts = [a.gain_horizon.probability_positive for a in pkg.alternatives]
            conf = pkg.decision.get("confidence")
            if conf is None:  # HOLD: probability that no considered move beats holding
                conf = 1.0 - max(alts) if alts else 1.0
            opt = m.OptimizationRunRow(
                id=pkg.optimizer_run_id,
                season_id=season_id,
                gw=pkg.gameweek,
                input_hash=pkg.optimizer_run_id,
                input_state_id=state_id,
                ruleset_version=pkg.ruleset_version,
                ruleset_hash=pkg.ruleset_version,
                solver="highs",
                objective_version=pkg.config_refs.get("optimizer", ""),
                config_json=pkg.config_refs,
                horizon=len(pkg.chosen.timeline),
                seed=0,
                status="succeeded",
                objective_value=pkg.chosen.objective,
                runtime_ms=int(1000 * pkg.timings.get("optimise", 0.0)),
                solution_json={"chosen": pkg.chosen.model_dump(mode="json")},
                validation_json={"valid": pkg.chosen.valid},
            )
            if s.get(m.OptimizationRunRow, opt.id) is None:
                s.add(opt)
                s.flush()
            payload = pkg.model_dump(mode="json")
            payload["manager_key"] = manager_key
            payload["markdown"] = markdown
            s.add(
                m.RecommendationRow(
                    id=rec_id,
                    manager_state_id=state_id,
                    season_id=season_id,
                    gw=pkg.gameweek,
                    action_type=pkg.decision["action"],
                    status="active",
                    expected_value=float(pkg.decision["expected_points"]),
                    expected_gain_vs_hold=float(pkg.decision["expected_gain_vs_hold"]),
                    downside=float(pkg.chosen.gain_horizon.p10),
                    confidence=float(min(max(conf, 0.0), 1.0)),
                    confidence_label=_label(float(conf)),
                    stability=pkg.stability.label if pkg.stability else None,
                    explanation_json=pkg.explanation.model_dump(mode="json"),
                    payload_json=payload,
                    optimization_run_id=opt.id,
                    data_snapshot_id=pkg.snapshot_id,
                    ruleset_version=pkg.ruleset_version,
                    model_versions=pkg.model_versions,
                    degraded=bool(pkg.assumptions and "stale" in " ".join(pkg.assumptions)),
                )
            )
            s.flush()  # parent row before its actions (no ORM relationship orders the inserts)
            for t, step in enumerate(pkg.chosen.timeline):
                s.add(
                    m.RecommendationActionRow(
                        recommendation_id=rec_id,
                        gw=step.gameweek,
                        sequence=t,
                        captain_code=step.captain,
                        vice_captain_code=step.vice_captain,
                        lineup_json={"out": step.transfers_out, "in": step.transfers_in},
                        chip_type=step.chip,
                    )
                )
        return rec_id

    def get(self, rec_id: str) -> dict[str, Any] | None:
        with session_scope(self.engine) as s:
            row = s.get(m.RecommendationRow, rec_id)
            if row is None:
                return None
            return {
                "id": row.id,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                **row.payload_json,
            }

    def current(self, manager_key: str, gw: int) -> dict[str, Any] | None:
        with session_scope(self.engine) as s:
            row = s.scalars(
                select(m.RecommendationRow)
                .where(
                    m.RecommendationRow.payload_json["manager_key"].astext == manager_key,
                    m.RecommendationRow.gw == gw,
                    m.RecommendationRow.status == "active",
                )
                .order_by(m.RecommendationRow.created_at.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            return {
                "id": row.id,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                **row.payload_json,
            }

    def journal(self, manager_key: str, limit: int = 50) -> list[dict[str, Any]]:
        with session_scope(self.engine) as s:
            rows = s.scalars(
                select(m.RecommendationRow)
                .where(m.RecommendationRow.payload_json["manager_key"].astext == manager_key)
                .order_by(m.RecommendationRow.created_at.desc())
                .limit(limit)
            ).all()
            out = []
            for r in rows:
                fb = s.scalars(
                    select(m.DecisionJournalRow)
                    .where(m.DecisionJournalRow.recommendation_id == r.id)
                    .order_by(m.DecisionJournalRow.recorded_at.desc())
                ).all()
                out.append(
                    {
                        "id": r.id,
                        "gameweek": r.gw,
                        "action": r.action_type,
                        "status": r.status,
                        "expected_gain_vs_hold": r.expected_gain_vs_hold,
                        "confidence": r.confidence,
                        "confidence_label": r.confidence_label,
                        "stability": r.stability,
                        "created_at": r.created_at.isoformat(),
                        "feedback": [
                            {
                                "followed": f.followed,
                                "note": f.user_note,
                                "realized_points": f.realized_points,
                                "recorded_at": f.recorded_at.isoformat(),
                            }
                            for f in fb
                        ],
                    }
                )
            return out

    def feedback(
        self, rec_id: str, followed: str, note: str | None, realized_points: float | None
    ) -> None:
        with session_scope(self.engine) as s:
            if s.get(m.RecommendationRow, rec_id) is None:
                raise KeyError(rec_id)
            s.add(
                m.DecisionJournalRow(
                    recommendation_id=rec_id,
                    followed=followed,
                    user_note=note,
                    realized_points=realized_points,
                )
            )


def _label(p: float) -> str:
    return "high" if p >= 0.75 else ("medium" if p >= 0.6 else "low")
