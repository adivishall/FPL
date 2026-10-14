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

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
import structlog
import yaml
from sqlalchemy import Engine, select, text

from fpl_api.observability import Metrics
from fpl_api.provenance import build_revision, code_digest
from fpl_api.settings import Settings
from fpl_decision.engine import RecommendationPackage
from fpl_domain.enums import FreshnessStatus
from fpl_domain.freshness import Freshness, FreshnessSla, evaluate_freshness
from fpl_domain.hashing import short_id
from fpl_domain.rules import Ruleset, load_ruleset
from fpl_domain.rules.loader import config_root
from fpl_domain.state import ManagerState
from fpl_features.registry import FEATURE_VERSION
from fpl_forecasting.model_config import minutes_spec, points_spec, price_spec
from fpl_forecasting.pipeline import Forecast, forecast, train_forecast_models
from fpl_forecasting.price_change import PriceChangeModel, decision_features, training_rows
from fpl_forecasting.walkforward import Cutoff, FeatureCache, cutoffs
from fpl_simulation.engine import SimulationConfig
from fpl_storage import models as m
from fpl_storage.dataset import CanonicalDataset, load_snapshot
from fpl_storage.db import session_scope
from fpl_storage.pit import PointInTimeView, decision_cutoff

REPO_ROOT = Path(__file__).resolve().parents[4]
log = structlog.get_logger("fpl_api")


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


def _publish(path: Path, write: Callable[[Path], object]) -> None:
    """Write via a per-process temporary file and an atomic rename: a process killed mid-write
    (OOM, timeout, host sleep) leaves no truncated file under the cache key for others to load."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _ready_marker(settings: Settings, snapshot_id: str) -> Path:
    return settings.artifact_dir / "serving" / f"{snapshot_id}.ready"


def mark_serving_ready(settings: Settings, snapshot_id: str, forecast_key: str) -> None:
    """Record that the serving forecast of ``snapshot_id`` is precomputed (snapshot promotion)."""
    marker = json.dumps({"forecast_key": forecast_key})
    _publish(_ready_marker(settings, snapshot_id), lambda t: t.write_text(marker))


def is_serving_ready(settings: Settings, snapshot_id: str) -> bool:
    return _ready_marker(settings, snapshot_id).exists()


class DataService:
    """Serves the newest validated snapshot; picks up a newly exported one without a restart.

    A pinned ``snapshot_dir`` (or an injected dataset) is served as is. Otherwise the newest
    ``data/snapshots/snap_*`` is re-checked every ``snapshot_check_seconds`` and swapped in
    atomically; forecasts are keyed by snapshot id, so the swap invalidates them naturally.
    """

    def __init__(self, settings: Settings, dataset: CanonicalDataset | None = None) -> None:
        self.settings = settings
        self._ds = dataset
        self._pinned = dataset is not None or settings.snapshot_dir is not None
        self._path: Path | None = None
        self._checked = 0.0
        self._lock = threading.Lock()
        self._created: dict[str, str] = {}  # manifest created_at by snapshot (immutable)

    @property
    def root(self) -> Path:
        return self.settings.snapshots_root or (REPO_ROOT / "data" / "snapshots")

    @property
    def pinned(self) -> bool:
        """An injected dataset or a pinned ``snapshot_dir``: never swapped."""
        return self._pinned

    @property
    def path(self) -> Path | None:
        """The directory of the loaded snapshot (None before the first access or when the
        dataset was injected)."""
        return self._path

    def _created_at(self, p: Path) -> str:
        c = self._created.get(p.name)
        if c is None:
            c = str(json.loads((p / "manifest.json").read_text())["created_at"])
            self._created[p.name] = c
        return c

    def _newest(self, ready_only: bool | None = None) -> Path:
        if self.settings.snapshot_dir is not None:
            return self.settings.snapshot_dir
        root = self.root
        found = sorted(
            (p for p in root.glob("snap_*") if (p / "manifest.json").exists()),
            key=self._created_at,
        )
        if not found:
            raise FileNotFoundError(f"no canonical snapshot under {root}")
        if self.settings.serve_ready_snapshots_only if ready_only is None else ready_only:
            ready = [p for p in found if is_serving_ready(self.settings, p.name)]
            # first deployment: nothing is ready yet — serve the newest and compute on demand
            return ready[-1] if ready else found[-1]
        return found[-1]

    def refresh(self) -> None:
        """Look for a newer snapshot on the next access (e.g. right after an export). Not 0:
        the monotonic clock starts at boot, so on a host up for less than the check interval
        a "last checked at 0" would still look recent and the look would be skipped."""
        self._checked = float("-inf")

    def promoted_snapshot_id(self) -> str:
        """The snapshot results are judged against (M1.3 lifecycle): the newest one whose
        serving forecast is ready — exactly the one the API promotes — or the newest export
        while none is ready yet. Read from disk and independent of this process's own
        ``serve_ready_snapshots_only``, so the API and a worker that already computes on a
        newer, not yet promoted export agree on what is current."""
        if self._pinned:
            return self.snapshot_id
        return self._newest(ready_only=True).name

    def snapshot_path(self, snapshot_id: str) -> Path | None:
        """Where an exported snapshot lives, if it is still stored."""
        if (
            self.settings.snapshot_dir is not None
            and self.settings.snapshot_dir.name == snapshot_id
        ):
            return self.settings.snapshot_dir
        p = self.root / snapshot_id
        return p if (p / "manifest.json").exists() else None

    @property
    def ds(self) -> CanonicalDataset:
        stale = (
            not self._pinned
            and time.monotonic() - self._checked > self.settings.snapshot_check_seconds
        )
        if self._ds is None or stale:
            with self._lock:
                if self._ds is None or stale:
                    path = self._newest()
                    self._checked = time.monotonic()
                    if path != self._path:
                        ds = load_snapshot(path)  # verified against its manifest hash
                        ds.meta.setdefault("path", str(path))
                        if self._ds is not None:
                            log.info(
                                "snapshot_swapped", old=self._ds.snapshot_id, new=ds.snapshot_id
                            )
                        self._ds, self._path = ds, path
        assert self._ds is not None
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
        # a fresh bootstrap does not make the match history fresh: finished gameweeks whose
        # per-match results are not in the snapshot are reported, never silently skipped
        finished = set(g.loc[g["status"].isin(["finalized", "provisional"]), "gw"].astype(int))
        have = set(pm["gw"].astype(int)) if len(pm) else set()
        missing = sorted(finished - have)
        if missing:
            reasons.append(
                f"match results missing for finished GW{missing[0]}"
                + (f"–GW{missing[-1]}" if len(missing) > 1 else "")
                + f": forecasts use results through GW{max(have) if have else 0} only"
            )
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
        return history_cutoffs(self.ds, season)


def history_cutoffs(ds: CanonicalDataset, season: str) -> list[Cutoff]:
    seasons = sorted(s for s in ds["gameweeks"]["season"].unique() if s <= season)
    return cutoffs(ds, seasons)


class ForecastService:
    def __init__(
        self, settings: Settings, data: DataService, metrics: Metrics | None = None
    ) -> None:
        self.settings = settings
        self.data = data
        self.metrics = metrics or Metrics()
        # lineage hook (forecast, canonical horizon, artifact path); failures never block serving
        self.on_forecast: Callable[[Forecast, int, Path | None], None] | None = None
        self._mem: dict[str, Forecast] = {}
        self._lock = threading.Lock()
        # key -> monotonic time this process started computing it (the forecast record bounds a
        # computation's start only when it knows the computation happened here, after a time)
        self.computed: dict[str, float] = {}

    def key(
        self,
        season: str,
        gw: int,
        horizon: int,
        n_sims: int,
        seed: int,
        snapshot: str | None = None,
    ) -> str:
        return short_id(
            "fc",
            {
                "snapshot": snapshot or self.data.snapshot_id,
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
        ds = self.data.ds  # one dataset for the key and the computation (hot swap safe)
        k = self.key(season, gw, horizon, n, seed, ds.snapshot_id)
        if k in self._mem:
            self.metrics.forecast_cache.labels("memory").inc()
            return self._mem[k]
        path = self._path(k)
        if path.exists():
            fc: Forecast = joblib.load(path)
            self._mem[k] = fc
            self.metrics.forecast_cache.labels("disk").inc()
            self._lineage(fc, horizon, path)
            return fc
        allowed = self.settings.forecast_on_demand if compute is None else compute
        self.metrics.forecast_cache.labels("miss").inc()
        if not allowed:
            raise ForecastUnavailable(k)
        with self._lock:
            if k in self._mem:
                return self._mem[k]
            t0 = time.perf_counter()
            self.computed[k] = time.monotonic()
            try:
                hist = history_cutoffs(ds, season)
                cut = next(c for c in hist if c.season == season and c.gw == gw)
                cache = FeatureCache(ds, horizon, self.settings.feature_store_dir)
                models = train_forecast_models(cache, cut, hist)
                fc = forecast(cache, cut, models, SimulationConfig(n_sims=n, seed=seed))
            except Exception:
                self.metrics.forecast_failures.inc()
                raise
            self.metrics.forecast_seconds.observe(time.perf_counter() - t0)
            fc.provenance["forecast_key"] = k
            # which code computed these numbers: neither the key nor the run id includes it
            fc.provenance["code_digest"] = code_digest()
            fc.provenance["build_revision"] = build_revision()
            _publish(path, lambda t: joblib.dump(fc, t, compress=3))
            self._mem[k] = fc
            self._lineage(fc, horizon, path)
            return fc

    def _lineage(self, fc: Forecast, horizon: int, path: Path | None) -> None:
        if self.on_forecast is None:
            return
        try:
            self.on_forecast(fc, horizon, path)
        except Exception:
            log.warning("lineage_record_failed", run_id=fc.run_id, exc_info=True)

    def features(self, season: str, gw: int, horizon: int) -> pd.DataFrame:
        hist = self.data.history_cutoffs(season)
        cut = next(c for c in hist if c.season == season and c.gw == gw)
        h = max(horizon, self.settings.forecast_horizon)
        frame = FeatureCache(self.data.ds, h, self.settings.feature_store_dir).get(cut).frame
        return frame[frame["target_gw"] < gw + horizon]


class PriceService:
    """Calibrated P(price rise / fall before the next deadline) per player (§23, §57.1).

    Trained on every labelled price move visible at the decision cutoff (point-in-time view),
    keyed like forecasts by (snapshot, price-model config, season, GW) and cached in memory and on
    disk. When no history is visible the result is empty and callers treat price risk as unknown.
    """

    def __init__(self, settings: Settings, data: DataService) -> None:
        self.settings = settings
        self.data = data
        self._mem: dict[str, pd.DataFrame] = {}
        self._lock = threading.Lock()

    def key(self, season: str, gw: int) -> str:
        return short_id(
            "px",
            {
                "snapshot": self.data.snapshot_id,
                "model": price_spec()[1].ref,
                "s": season,
                "gw": gw,
            },
        )

    def probabilities(self, season: str, gw: int, cutoff: datetime) -> pd.DataFrame:
        k = self.key(season, gw)
        if k in self._mem:
            return self._mem[k]
        path = self.settings.artifact_dir / "prices" / f"{k}.parquet"
        with self._lock:
            if k in self._mem:
                return self._mem[k]
            if path.exists():
                out = pd.read_parquet(path)
            else:
                view = self.data.view(cutoff)
                seasons = sorted(
                    x for x in self.data.ds["gameweeks"]["season"].unique() if x <= season
                )
                rows = training_rows(view, seasons)
                feats = decision_features(view, season, gw)
                if rows.empty or feats.empty:
                    out = pd.DataFrame(columns=["player_code", "p_rise", "p_fall"])
                else:
                    spec = price_spec()[0]
                    out = PriceChangeModel(spec.params).fit(rows).predict(feats)
                _publish(path, lambda t: out.to_parquet(t, index=False))
            self._mem[k] = out
            return out


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
            existing = s.get(m.ManagerStateRow, sid)
            if existing is not None:
                # Content-addressed: saving an earlier squad again makes it the latest again.
                existing.captured_at = datetime.now(UTC)
            else:
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

    def get(self, state_id: str, manager_key: str) -> ManagerState | None:
        """The exact stored state — only if it belongs to ``manager_key``, never another's."""
        with session_scope(self.engine) as s:
            row = s.get(m.ManagerStateRow, state_id)
            if row is None or row.state_json.get("manager_key") != manager_key:
                return None
            payload = {k: v for k, v in row.state_json.items() if k != "manager_key"}
            return ManagerState.model_validate(payload)


@dataclass
class RecommendationStore:
    engine: Engine

    def save(
        self,
        pkg: RecommendationPackage,
        manager_key: str,
        state_id: str | None,
        markdown: str,
        watch: dict[str, Any] | None = None,
        prediction_run_id: str | None = None,
        input_hash: str | None = None,
        serving_snapshot_id: str | None = None,
        settings: Any = None,
        requested_at: datetime | None = None,
    ) -> str:
        """``serving_snapshot_id`` is the promoted snapshot when the result is finalised: a
        package computed for an older snapshot, or for a squad state that is no longer the
        manager's latest, is stored as historical (``invalidated``, with the reason) and never
        supersedes the current recommendation; one *requested* before the current one (an
        older job finishing later) is stored ``superseded`` (M1.3 lifecycle). Saving a package
        that is already stored re-activates it when its inputs are current again."""
        from fpl_api.lifecycle import finalisation_check  # noqa: PLC0415 (import cycle)

        rec_id = pkg.decision_id.replace("dec_", "rec_", 1)
        req = _aware(requested_at) or datetime.now(UTC)
        with session_scope(self.engine) as s:
            _lock_gameweek(s, manager_key, pkg.gameweek)
            existing = s.get(m.RecommendationRow, rec_id)
            if existing is not None:
                _reactivate(s, existing, serving_snapshot_id, settings, req)
                return rec_id
            season_id = ensure_season(s, pkg.season)
            stale = finalisation_check(
                s, manager_key, state_id, pkg.snapshot_id, serving_snapshot_id, settings
            )
            status = "invalidated"
            if stale is None:
                status = (
                    "active"
                    if _supersede_older(s, manager_key, pkg.gameweek, req, rec_id)
                    else "superseded"
                )
            alts = [a.gain_horizon.probability_positive for a in pkg.alternatives]
            conf = pkg.decision.get("confidence")
            if conf is None:  # HOLD: probability that no considered move beats holding
                conf = 1.0 - max(alts) if alts else 1.0
            if prediction_run_id and s.get(m.PredictionRunRow, prediction_run_id) is None:
                prediction_run_id = None  # lineage not registered (no FK target): stays in payload
            opt = m.OptimizationRunRow(
                id=pkg.optimizer_run_id,
                season_id=season_id,
                gw=pkg.gameweek,
                input_hash=input_hash or pkg.optimizer_run_id,  # content id of the computation
                input_state_id=state_id,
                prediction_run_id=prediction_run_id,
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
            payload["watch"] = watch or {}  # inputs baseline for re-optimisation alerts (§74)
            payload["requested_at"] = req.isoformat()  # orders competing jobs (M1.3)
            s.add(
                m.RecommendationRow(
                    id=rec_id,
                    manager_state_id=state_id,
                    season_id=season_id,
                    gw=pkg.gameweek,
                    action_type=pkg.decision["action"],
                    status=status,
                    invalidated_at=None if stale is None else datetime.now(UTC),
                    invalidation_json=stale,
                    expected_value=float(pkg.decision["expected_points"]),
                    expected_gain_vs_hold=float(pkg.decision["expected_gain_vs_hold"]),
                    downside=float(pkg.chosen.gain_horizon.p10),
                    confidence=float(min(max(conf, 0.0), 1.0)),
                    confidence_label=_label(float(conf)),
                    stability=pkg.stability.label if pkg.stability else None,
                    explanation_json=pkg.explanation.model_dump(mode="json"),
                    payload_json=payload,
                    optimization_run_id=opt.id,
                    prediction_run_id=prediction_run_id,
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

    def reactivate(
        self,
        rec_id: str,
        serving_snapshot_id: str | None,
        settings: Any = None,
        requested_at: datetime | None = None,
    ) -> bool:
        """Asking again for a recommendation that is already stored (the job is de-duplicated):
        it becomes the current one again when its inputs are exactly the current ones — the
        squad re-saved, its snapshot not older than the promoted one. True when it is active."""
        req = _aware(requested_at) or datetime.now(UTC)
        with session_scope(self.engine) as s:
            row = s.get(m.RecommendationRow, rec_id)
            if row is None:
                return False
            _lock_gameweek(s, str(row.payload_json.get("manager_key") or ""), row.gw)
            s.refresh(row)
            return _reactivate(s, row, serving_snapshot_id, settings, req)

    def get(self, rec_id: str) -> dict[str, Any] | None:
        with session_scope(self.engine) as s:
            row = s.get(m.RecommendationRow, rec_id)
            if row is None:
                return None
            return _rec_dict(row)

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
            return _rec_dict(row)

    def latest_for_gameweek(self, manager_key: str, gw: int) -> dict[str, Any] | None:
        """The most recent recommendation for a gameweek, whatever its status (post-GW review)."""
        with session_scope(self.engine) as s:
            row = s.scalars(
                select(m.RecommendationRow)
                .where(
                    m.RecommendationRow.payload_json["manager_key"].astext == manager_key,
                    m.RecommendationRow.gw == gw,
                )
                .order_by(m.RecommendationRow.created_at.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            return _rec_dict(row)

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
                        "state_id": r.manager_state_id,
                        "snapshot_id": r.data_snapshot_id,
                        "invalidated_at": r.invalidated_at.isoformat()
                        if r.invalidated_at
                        else None,
                        "invalidation": r.invalidation_json,
                        "feedback": [  # every decision recorded against it, newest first
                            {
                                "entry_id": f.entry_id,
                                "state": f.state,
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


def _rec_dict(row: m.RecommendationRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "status": row.status,
        "state_id": row.manager_state_id,
        "data_snapshot_id": row.data_snapshot_id,
        "created_at": row.created_at.isoformat(),
        "invalidated_at": row.invalidated_at.isoformat() if row.invalidated_at else None,
        "invalidation": row.invalidation_json,
        **row.payload_json,
    }


def _label(p: float) -> str:
    return "high" if p >= 0.75 else ("medium" if p >= 0.6 else "low")


def _aware(ts: datetime | str | None) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)


def _lock_gameweek(s: Any, manager_key: str, gw: int) -> None:
    """Serialise the writers of one manager's recommendation for one gameweek (the transaction
    holds the lock): two jobs finishing together can never both leave a current one."""
    s.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"recommendation:{manager_key}:{gw}"},
    )


def _requested(row: m.RecommendationRow) -> datetime:
    return (
        _aware(row.payload_json.get("requested_at"))
        or _aware(row.created_at)
        or datetime.min.replace(tzinfo=UTC)
    )


def _supersede_older(s: Any, manager_key: str, gw: int, req: datetime, keep: str) -> bool:
    """Supersede the current recommendation(s) for this gameweek that were requested before
    ``req``. False — and nothing changes — when one requested later is current: an older job
    that finishes last never replaces the answer to a newer request."""
    active = s.scalars(
        select(m.RecommendationRow).where(
            m.RecommendationRow.payload_json["manager_key"].astext == manager_key,
            m.RecommendationRow.gw == gw,
            m.RecommendationRow.status == "active",
            m.RecommendationRow.id != keep,
        )
    ).all()
    if any(_requested(r) > req for r in active):
        return False
    for r in active:
        r.status = "superseded"
    return True


def _reactivate(
    s: Any,
    row: m.RecommendationRow,
    serving_snapshot_id: str | None,
    settings: Any,
    req: datetime,
) -> bool:
    from fpl_api.lifecycle import reactivation_allowed  # noqa: PLC0415 (import cycle)

    manager_key = str(row.payload_json.get("manager_key") or "")
    if row.status == "active":  # asked again: it is the answer to the later request now
        if req > _requested(row):
            row.payload_json = {**row.payload_json, "requested_at": req.isoformat()}
        return True
    if not reactivation_allowed(
        s, manager_key, row.manager_state_id, row.data_snapshot_id, serving_snapshot_id, settings
    ):
        return False
    if not _supersede_older(s, manager_key, row.gw, req, row.id):
        return False
    log.info(
        "recommendation_reactivated",
        rec_id=row.id,
        previous_status=row.status,
        previous_invalidation=row.invalidation_json,
    )
    row.status, row.invalidated_at, row.invalidation_json = "active", None, None
    row.payload_json = {**row.payload_json, "requested_at": req.isoformat()}
    return True
