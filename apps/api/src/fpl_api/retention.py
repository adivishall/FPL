"""Retention of live-refresh snapshots and their derived caches (bounded ``/data``).

Every hourly live refresh exports a canonical snapshot (~3 MB); once a forecast is computed
for it, a feature cache (~30 MB) and a forecast (~7 MB) follow. Unpruned, that is ~1 GB a day.
``prune`` applies this policy to the snapshots under ``snapshots_root``:

* **Kept with caches:** the ``snapshot_retention_keep`` newest snapshots (manifest
  ``created_at``), the snapshot the API serves (newest with a precomputed serving forecast) and
  the evaluation snapshots pinned in ``config/backtest/*.yaml``.
* **Kept, caches dropped:** any other snapshot that a stored recommendation, backtest run or
  registered model references. Its data stays so the decision can be traced and re-derived;
  feature and forecast caches are recomputable from it.
* **Deleted:** every other snapshot with its feature cache and serving marker; forecast and
  price caches that no kept serving forecast uses, once older than
  ``artifact_retention_hours``; incomplete exports (no manifest) and temporary files left by
  interrupted writers, once older than an hour.

Database rows are never deleted. Failures to delete are collected and raised at the end, so
the job fails visibly (``failure=error``) after doing everything it could.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from sqlalchemy import Engine, select

from fpl_api.analytics import prune as prune_events
from fpl_api.services import REPO_ROOT, is_serving_ready
from fpl_api.settings import Settings
from fpl_domain.config import load_versioned_config
from fpl_domain.rules.loader import config_root
from fpl_storage import models as m
from fpl_storage.db import session_scope

log = structlog.get_logger("fpl_api.retention")
STRAY_AGE_S = 3600.0  # an export or temporary file this old is not in progress any more


class RetentionError(RuntimeError):
    pass


@dataclass
class RetentionReport:
    kept: list[str] = field(default_factory=list)
    kept_data_only: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    deleted_files: int = 0
    freed_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    deleted_events: int = 0  # product-analytics rows past their retention

    def summary(self) -> str:
        return (
            f"retention: deleted {len(self.deleted)} snapshots + {self.deleted_files} cache "
            f"files ({self.freed_bytes / 1e6:.0f} MB); kept {len(self.kept)} "
            f"(+{len(self.kept_data_only)} referenced)"
        )


def pinned_snapshots() -> set[str]:
    """Evaluation snapshots pinned by the backtest configurations."""
    out = set()
    for path in sorted((config_root() / "backtest").glob("*.yaml")):
        sid = load_versioned_config("backtest", path.stem).data.get("snapshot_id")
        if sid:
            out.add(str(sid))
    return out


def referenced_snapshots(engine: Engine | None) -> set[str]:
    """Snapshots that stored decisions, backtests or models point at (traceability)."""
    if engine is None:
        return set()
    with session_scope(engine) as s:
        refs: set[str | None] = set()
        for col in (
            m.RecommendationRow.data_snapshot_id,
            m.BacktestRunRow.data_snapshot_id,
            m.ModelRegistryRow.data_snapshot_id,
        ):
            refs |= set(s.scalars(select(col).distinct()).all())
    return {r for r in refs if r}


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def prune(settings: Settings, engine: Engine | None, now: float | None = None) -> RetentionReport:
    now = time.time() if now is None else now
    rep = RetentionReport()
    if engine is not None:  # product analytics: the only database rows retention deletes
        rep.deleted_events = prune_events(engine)
    root = settings.snapshots_root or REPO_ROOT / "data" / "snapshots"
    keep_n = settings.snapshot_retention_keep
    if keep_n <= 0 or not root.exists():
        return rep  # retention disabled, or nothing exported yet

    def remove(p: Path) -> None:
        try:
            try:
                size = _size(p)
            except OSError:  # files vanishing while measured must not block the deletion
                size = 0
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            rep.freed_bytes += size
        except FileNotFoundError:
            pass  # removed concurrently: the goal is reached
        except OSError as exc:
            rep.errors.append(f"{p}: {exc}")

    complete: list[Path] = []
    partial: list[Path] = []
    for p in root.glob("snap_*"):
        (complete if (p / "manifest.json").exists() else partial).append(p)
    try:
        complete.sort(key=lambda p: json.loads((p / "manifest.json").read_text())["created_at"])
    except (OSError, ValueError, KeyError) as exc:  # never guess an order when deleting
        raise RetentionError(f"unreadable snapshot manifest under {root}: {exc}") from exc
    ids = [p.name for p in complete]
    ready = [s for s in ids if is_serving_ready(settings, s)]
    with_caches = set(ids[-keep_n:]) | set(ready[-1:]) | pinned_snapshots()
    referenced = referenced_snapshots(engine)
    for p in complete:
        if p.name in with_caches:
            rep.kept.append(p.name)
        elif p.name in referenced:
            rep.kept_data_only.append(p.name)
        else:
            remove(p)
            rep.deleted.append(p.name)
    for p in partial:  # an export interrupted before its manifest was published
        if now - p.stat().st_mtime > STRAY_AGE_S:
            remove(p)
            rep.deleted_files += 1

    art = settings.artifact_dir
    if settings.feature_store_dir is not None and settings.feature_store_dir.exists():
        for d in settings.feature_store_dir.glob("snap_*"):
            if d.name not in with_caches:
                remove(d)
                rep.deleted_files += 1
    serving_keys = set()
    for marker in (art / "serving").glob("*.ready"):
        if marker.stem in with_caches:
            serving_keys.add(json.loads(marker.read_text()).get("forecast_key"))
        else:  # its forecast is no longer kept, so it is no longer "ready"
            remove(marker)
            rep.deleted_files += 1
    max_age = settings.artifact_retention_hours * 3600.0
    for sub, pattern in (("forecasts", "*.joblib"), ("prices", "*.parquet")):
        for f in (art / sub).glob(pattern):
            if f.stem not in serving_keys and now - f.stat().st_mtime > max_age:
                remove(f)
                rep.deleted_files += 1
    for sub in ("forecasts", "prices", "serving"):
        for f in (art / sub).glob("*.tmp*"):  # left by a writer killed mid-write
            if now - f.stat().st_mtime > STRAY_AGE_S:
                remove(f)
                rep.deleted_files += 1
    log.info(
        "retention",
        deleted=rep.deleted,
        kept=len(rep.kept),
        kept_data_only=len(rep.kept_data_only),
        deleted_files=rep.deleted_files,
        freed_mb=round(rep.freed_bytes / 1e6, 1),
        errors=len(rep.errors),
    )
    if rep.errors:
        raise RetentionError(f"{len(rep.errors)} paths not deleted: {rep.errors[:3]}")
    return rep
