"""Lineage records and the traceability chain (§76.2).

Every forecast the service serves is registered as relational lineage — ``data_snapshots`` →
``feature_snapshots`` → ``prediction_runs`` — and every stored recommendation references its
optimisation run, which references the prediction run. :func:`trace` walks the chain backwards:

    recommendation → optimisation run → prediction set → feature snapshot
                   → canonical data snapshot → source retrieval

Each link is read from the database (not re-derived), with integrity checks that the identifiers
agree; the last link adds the pinned source revision from the versioned source configuration and
any raw-capture records the ingestion pipeline stored for it.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, select

from fpl_api.services import ensure_season, sources_config
from fpl_api.settings import Settings
from fpl_domain.config import load_versioned_config
from fpl_features.registry import FEATURE_VERSION
from fpl_forecasting.pipeline import Forecast
from fpl_storage import models as m
from fpl_storage.dataset import CanonicalDataset
from fpl_storage.db import session_scope


def _ts(v: Any, default: datetime) -> datetime:
    try:
        t = pd.Timestamp(v)
    except (TypeError, ValueError):
        return default
    if pd.isna(t):
        return default
    return (t.tz_localize(UTC) if t.tzinfo is None else t).to_pydatetime()


def feature_lineage_id(feature_snapshot_id: str, data_snapshot_id: str) -> str:
    return f"{feature_snapshot_id}@{data_snapshot_id}"


class LineageStore:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings
        self._done: set[str] = set()
        self._lock = threading.Lock()

    def feature_path(self, fc: Forecast, ds: CanonicalDataset, horizon: int) -> Path | None:
        root = self.settings.feature_store_dir
        if root is None:
            return None
        p = fc.provenance
        return (
            Path(root)
            / str(p["data_snapshot_id"])
            / str(p.get("feature_version", FEATURE_VERSION))
            / f"{p['season']}_gw{int(p['decision_gw']):02d}_h{horizon}.parquet"
        )

    def record_forecast(
        self, fc: Forecast, ds: CanonicalDataset, horizon: int, artifact: Path | None
    ) -> None:
        """Idempotently register the forecast's lineage rows."""
        p = fc.provenance
        if p["prediction_run_id"] in self._done:
            return
        with self._lock, session_scope(self.engine) as s:
            season_id = ensure_season(s, p["season"])
            cutoff = _ts(p["cutoff"], datetime.now(UTC))
            snap = p["data_snapshot_id"]
            # metadata can only come from the dataset the forecast was built on (exported
            # snapshots are registered by `fpl-ingest export-snapshot` already)
            if s.get(m.DataSnapshot, snap) is None and ds.snapshot_id == snap:
                meta = ds.meta or {}
                src = sources_config()["historical_repo"]
                s.add(
                    m.DataSnapshot(
                        id=snap,
                        as_of=_ts(meta.get("created_at"), datetime.now(UTC)),
                        seasons=ds.seasons(),
                        tables=meta.get("tables")
                        or {t: {"sha256": h} for t, h in ds.table_hashes.items()},
                        storage_uri=str(meta.get("path", "memory://dataset")),
                        source_versions={
                            "sources_config": meta.get("sources_config"),
                            "historical_repo": src["name"],
                            "commit": src["commit"],
                        },
                        validated=True,
                    )
                )
                s.flush()
            # Feature ids are content hashes: identical features can be built from several data
            # snapshots (e.g. hourly live captures). The lineage row is "this feature content as
            # materialised from this data snapshot", so the walk back finds the right snapshot.
            fid = feature_lineage_id(p["feature_snapshot_id"], snap)
            if s.get(m.FeatureSnapshotRow, fid) is None:
                fp = self.feature_path(fc, ds, horizon)
                exists = fp is not None and fp.exists()
                sha = (
                    hashlib.sha256(fp.read_bytes()).hexdigest()
                    if exists and fp is not None
                    else hashlib.sha256(fid.encode()).hexdigest()
                )
                n_rows = (
                    len(pd.read_parquet(fp, columns=["player_code"]))
                    if exists and fp is not None
                    else int(p.get("feature_rows", 0))
                )
                max_src = min(_ts(p.get("max_source_available_at"), cutoff), cutoff)
                s.add(
                    m.FeatureSnapshotRow(
                        id=fid,
                        season_id=season_id,
                        target_gw=int(p["decision_gw"]),
                        cutoff_at=cutoff,
                        feature_version=str(p.get("feature_version", FEATURE_VERSION)),
                        data_snapshot_id=snap,
                        max_source_available_at=max_src,
                        n_rows=n_rows,
                        storage_uri=str(fp) if exists else "memory://features",
                        sha256=sha,
                    )
                )
                s.flush()
            pid = p["prediction_run_id"]
            if s.get(m.PredictionRunRow, pid) is None:
                s.add(
                    m.PredictionRunRow(
                        id=pid,
                        season_id=season_id,
                        base_gw=int(p["decision_gw"]),
                        horizon=horizon,
                        cutoff_at=cutoff,
                        feature_snapshot_id=fid,
                        model_versions={k: str(v) for k, v in p["model_versions"].items()},
                        ruleset_version=str(p["ruleset_version"]),
                        simulation_seed=int(p["simulation_seed"]),
                        n_simulations=int(p["n_simulations"]),
                        config_json={"team_model": p.get("team_model")},
                        samples_uri=str(artifact) if artifact else None,
                    )
                )
            self._done.add(pid)


def _sources_ref() -> str:
    return load_versioned_config("", "sources").ref


def trace(engine: Engine, rec_id: str) -> dict[str, Any] | None:
    """Walk the lineage chain backwards from a stored recommendation."""
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    with session_scope(engine) as s:
        rec = s.get(m.RecommendationRow, rec_id)
        if rec is None:
            return None
        payload = rec.payload_json
        chain: dict[str, Any] = {
            "recommendation": {
                "id": rec.id,
                "gameweek": rec.gw,
                "action": rec.action_type,
                "status": rec.status,
                "created_at": rec.created_at.isoformat(),
                "data_snapshot_id": rec.data_snapshot_id,
                "ruleset_version": rec.ruleset_version,
                "model_versions": rec.model_versions,
                "manager_state_id": rec.manager_state_id,
            }
        }
        opt = (
            s.get(m.OptimizationRunRow, rec.optimization_run_id)
            if rec.optimization_run_id
            else None
        )
        check("optimization_run_exists", opt is not None, str(rec.optimization_run_id))
        pred_id = (opt.prediction_run_id if opt else None) or rec.prediction_run_id
        if opt is not None:
            chain["optimization_run"] = {
                "id": opt.id,
                "solver": opt.solver,
                "objective_version": opt.objective_version,
                "config": opt.config_json,
                "horizon": opt.horizon,
                "status": opt.status,
                "objective_value": opt.objective_value,
                "runtime_ms": opt.runtime_ms,
                "input_state_id": opt.input_state_id,
                "validation": opt.validation_json,
                "prediction_run_id": opt.prediction_run_id,
            }
            check(
                "optimization_input_state_matches",
                opt.input_state_id == rec.manager_state_id,
                f"{opt.input_state_id} vs {rec.manager_state_id}",
            )
        pred = s.get(m.PredictionRunRow, pred_id) if pred_id else None
        check("prediction_run_exists", pred is not None, str(pred_id))
        feat = None
        if pred is not None:
            chain["prediction_run"] = {
                "id": pred.id,
                "base_gw": pred.base_gw,
                "horizon": pred.horizon,
                "cutoff_at": pred.cutoff_at.isoformat(),
                "model_versions": pred.model_versions,
                "ruleset_version": pred.ruleset_version,
                "simulation_seed": pred.simulation_seed,
                "n_simulations": pred.n_simulations,
                "samples_uri": pred.samples_uri,
            }
            rec_models = {k: v for k, v in (rec.model_versions or {}).items() if k != "ruleset"}
            check(
                "model_versions_match",
                all(pred.model_versions.get(k) == v for k, v in rec_models.items()),
            )
            feat = s.get(m.FeatureSnapshotRow, pred.feature_snapshot_id)
        check("feature_snapshot_exists", feat is not None)
        snap = None
        if feat is not None:
            chain["feature_snapshot"] = {
                "id": feat.id,
                "feature_version": feat.feature_version,
                "cutoff_at": feat.cutoff_at.isoformat(),
                "max_source_available_at": feat.max_source_available_at.isoformat(),
                "n_rows": feat.n_rows,
                "storage_uri": feat.storage_uri,
                "sha256": feat.sha256,
            }
            check(
                "no_future_sources",
                feat.max_source_available_at <= feat.cutoff_at,
                "max_source_available_at <= cutoff_at",
            )
            # a live recommendation precedes its (future) decision cutoff; what must hold is that
            # nothing it used was published after it was made
            check(
                "sources_before_recommendation",
                feat.max_source_available_at <= rec.created_at,
                "max_source_available_at <= recommendation created_at",
            )
            snap = s.get(m.DataSnapshot, feat.data_snapshot_id) if feat.data_snapshot_id else None
        check("data_snapshot_exists", snap is not None)
        if snap is not None:
            chain["data_snapshot"] = {
                "id": snap.id,
                "as_of": snap.as_of.isoformat(),
                "seasons": snap.seasons,
                "tables": snap.tables,
                "storage_uri": snap.storage_uri,
                "source_versions": snap.source_versions,
                "validated": snap.validated,
            }
            check(
                "recommendation_snapshot_matches",
                snap.id == rec.data_snapshot_id,
                f"{snap.id} vs {rec.data_snapshot_id}",
            )
            sv = snap.source_versions or {}
            commit = sv.get("commit")
            if commit is None and sv.get("sources_config") == _sources_ref():
                commit = sources_config()["historical_repo"]["commit"]  # same config version
            raws = s.scalars(
                select(m.RawSnapshot)
                .where(m.RawSnapshot.source_version == commit)
                .order_by(m.RawSnapshot.resource)
            ).all()
            src = sources_config()["historical_repo"]
            chain["source_retrieval"] = {
                "historical_repo": src["name"],
                "base_url": src["base_url"],
                "commit": commit,
                "pinned_commit_matches_config": commit == src["commit"],
                "licence": src.get("licence"),
                "files": src.get("files"),
                "raw_captures": [
                    {
                        "id": r.id,
                        "resource": r.resource,
                        "url": r.url,
                        "sha256": r.sha256,
                        "retrieved_at": r.retrieved_at.isoformat(),
                    }
                    for r in raws
                ],
                "note": None
                if raws
                else "raw-capture records live in the ingestion database; this deployment has "
                "the content-addressed raw store and the pinned revision only",
            }
            check("pinned_source_revision", commit == src["commit"], str(commit))
    watch = payload.get("watch") or {}
    if watch.get("prediction_run_id") and "prediction_run" in chain:
        check(
            "watch_prediction_run_matches",
            watch["prediction_run_id"] == chain["prediction_run"]["id"],
        )
    return {
        "recommendation_id": rec_id,
        "chain": chain,
        "checks": checks,
        "complete": all(c["ok"] for c in checks),
    }
