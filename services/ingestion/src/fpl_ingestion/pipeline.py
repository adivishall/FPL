"""Ingestion pipeline orchestration (§6 pipeline table, §55).

extract → raw capture → validate (contracts) → quality gates → canonicalise → load → audit

Failure semantics:
* source unreachable → job ``failed``; canonical data untouched (last good snapshot keeps
  serving, §82);
* critical quality issue → job ``quarantined``; canonical data untouched;
* otherwise → job ``succeeded``; issues recorded as ``data_quality_events``.
Every step is idempotent: re-running a job with the same source revision changes nothing.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import structlog
from sqlalchemy import Engine

from fpl_domain.config import load_versioned_config
from fpl_domain.enums import Severity
from fpl_domain.rules import load_ruleset
from fpl_ingestion.canonical import CanonicalSeason, PitPolicy, canonicalize
from fpl_ingestion.contracts import CONTRACTS, Issue
from fpl_ingestion.http import SourceUnavailableError
from fpl_ingestion.live import canonicalize_bootstrap
from fpl_ingestion.load import load_canonical
from fpl_ingestion.quality import SeasonFrames, run_quality_gates
from fpl_ingestion.sources.historical import FILES, HistoricalRepoSource
from fpl_storage.dataset import CanonicalDataset, load_from_db
from fpl_storage.db import session_scope
from fpl_storage.raw_store import RawPayload, RawStore
from fpl_storage.repositories import JobRecorder, record_raw_snapshot

log = structlog.get_logger(__name__)


@dataclass
class IngestionResult:
    job_id: str
    season: str
    status: str
    issues: list[Issue] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    raw_ids: dict[str, str] = field(default_factory=dict)
    canonical: CanonicalSeason | None = None

    @property
    def dataset(self) -> CanonicalDataset:
        if self.canonical is None:
            raise RuntimeError(f"season {self.season} produced no canonical data ({self.status})")
        return CanonicalDataset(self.canonical.frames())


def parse_and_validate(season: str, payloads: dict[str, RawPayload]) -> SeasonFrames:
    issues: list[Issue] = []
    parsed: dict[str, pd.DataFrame] = {}
    for key in FILES:
        df = pd.read_csv(io.BytesIO(payloads[key].content), low_memory=False)
        valid, iss = CONTRACTS[key].validate(df)
        parsed[key] = valid
        issues.extend(iss)
    return SeasonFrames(
        season=season,
        merged_gw=parsed["merged_gw"],
        fixtures=parsed["fixtures"],
        teams=parsed["teams"],
        players_raw=parsed["players_raw"],
        issues=issues,
    )


def _raw_meta(p: RawPayload, path: str, job_id: str) -> dict[str, Any]:
    return {
        "id": p.snapshot_id,
        "source": p.source,
        "resource": p.resource,
        "url": p.url,
        "source_version": p.source_version,
        "retrieved_at": p.retrieved_at,
        "sha256": p.sha256,
        "size_bytes": len(p.content),
        "content_type": p.content_type,
        "storage_uri": path,
        "schema_version": p.schema_version,
        "job_id": job_id,
    }


def ingest_historical_season(
    engine: Engine,
    season: str,
    source: HistoricalRepoSource,
    raw_store: RawStore,
    sources_cfg: dict[str, Any],
) -> IngestionResult:
    repo_cfg = sources_cfg["historical_repo"]
    observed_at = datetime.fromisoformat(
        str(repo_cfg["commit_published_at"]).replace("Z", "+00:00")
    )
    pit_cfg = repo_cfg.get("point_in_time_snapshots", {}).get(season)
    snapshot_at = (
        datetime.fromisoformat(str(pit_cfg["captured_at"]).replace("Z", "+00:00"))
        if pit_cfg
        else None
    )
    params = {
        "season": season,
        "commit": source.commit,
        "pit_policy": sources_cfg.get("point_in_time", {}),
    }

    with session_scope(engine) as s:
        job = JobRecorder(s, job_type="historical_season", source="vaastav", params=params)
        job.start()
    result = IngestionResult(job_id=job.job_id, season=season, status="running")

    def finish(status: str, errors: list[Any]) -> IngestionResult:
        with session_scope(engine) as s:
            rec = JobRecorder(s, "historical_season", "vaastav", params, job_id=result.job_id)
            for i in result.issues:
                rec.record_issue(
                    issue_code=i.code,
                    severity=i.severity.value,
                    entity_type=i.entity_type,
                    entity_id=i.entity_id,
                    details={"message": i.message, "count": i.count, **i.details},
                )
            rec.finish(status, result.counts, errors)
        result.status = status
        log.info(
            "ingest.finished",
            season=season,
            status=status,
            job_id=result.job_id,
            issues=len(result.issues),
        )
        return result

    try:
        payloads = source.fetch_season(season)
    except SourceUnavailableError as exc:
        return finish("failed", [{"stage": "extract", "error": str(exc)}])

    with session_scope(engine) as s:
        for key, p in payloads.items():
            path = raw_store.put(p)
            record_raw_snapshot(s, _raw_meta(p, str(path), result.job_id))
            result.raw_ids[key] = p.snapshot_id

    frames = parse_and_validate(season, payloads)
    if frames.gate_passed:
        frames = run_quality_gates(
            frames, sources_cfg.get("point_in_time", {}).get("derived_deadline_offset_minutes", 90)
        )
    result.issues = frames.issues
    if not frames.gate_passed:
        crit = [i.message for i in frames.issues if i.severity is Severity.CRITICAL]
        return finish("quarantined", [{"stage": "validate", "critical": crit}])

    ruleset = load_ruleset(season)
    canonical = canonicalize(
        frames,
        ruleset,
        PitPolicy.from_config(sources_cfg),
        observed_at,
        snapshot_captured_at=snapshot_at,
        source="vaastav",
        source_version=source.commit,
    )
    result.canonical = canonical
    conflicts: list[dict[str, Any]] = []
    try:
        with session_scope(engine) as s:
            result.counts = load_canonical(
                s,
                CanonicalDataset(canonical.frames()),
                result.raw_ids,
                source="vaastav",
                source_priority=sources_cfg.get("source_priority"),
                conflicts=conflicts,
            )
    except Exception as exc:  # load failures must be recorded, never swallowed silently
        log.exception("ingest.load_failed", season=season)
        return finish("failed", [{"stage": "load", "error": repr(exc)[:500]}])
    result.issues.extend(conflict_issues(conflicts))
    return finish("succeeded", [])


def conflict_issues(conflicts: list[dict[str, Any]]) -> list[Issue]:
    """One data-quality event per conflicting record (kept vs rejected values preserved)."""
    return [
        Issue(
            "source_conflict",
            Severity.WARNING,
            c["table"],
            f"{c['rejected_source']} disagrees with higher-priority {c['kept_source']}; "
            "higher-priority value kept",
            entity_id="|".join(c["key"]),
            details={"fields": c["fields"]},
        )
        for c in conflicts
    ]


def ingest_live(
    engine: Engine,
    client: Any,
    raw_store: RawStore,
    season: str,
) -> IngestionResult:
    """Capture bootstrap + fixtures from the live FPL API and load them canonically.

    On ``SourceUnavailableError`` the job is recorded as failed and nothing else changes: the
    API keeps serving the last validated snapshot with its freshness timestamp (§82).
    """
    with session_scope(engine) as s:
        job = JobRecorder(s, job_type="live_bootstrap", source="fpl_api", params={"season": season})
        job.start()
    result = IngestionResult(job_id=job.job_id, season=season, status="running")

    def finish(status: str, errors: list[Any]) -> IngestionResult:
        with session_scope(engine) as s:
            rec = JobRecorder(
                s, "live_bootstrap", "fpl_api", {"season": season}, job_id=result.job_id
            )
            for i in result.issues:
                rec.record_issue(
                    issue_code=i.code,
                    severity=i.severity.value,
                    entity_type=i.entity_type,
                    entity_id=i.entity_id,
                    details={"message": i.message, "count": i.count, **i.details},
                )
            rec.finish(status, result.counts, errors)
        result.status = status
        return result

    try:
        boot_raw, boot = client.bootstrap_static()
        fx_raw, fixtures = client.fixtures()
    except SourceUnavailableError as exc:
        return finish("failed", [{"stage": "extract", "error": str(exc)}])
    with session_scope(engine) as s:
        for key, p in (("bootstrap", boot_raw), ("fixtures", fx_raw)):
            path = raw_store.put(p)
            record_raw_snapshot(s, _raw_meta(p, str(path), result.job_id))
            result.raw_ids[key] = p.snapshot_id
    ruleset = load_ruleset(season)
    canonical, issues = canonicalize_bootstrap(boot, fixtures, boot_raw.retrieved_at, ruleset)
    result.issues.extend(issues)
    canonical.fixtures = preserve_schedule_first_seen(engine, canonical.fixtures, season)
    result.canonical = canonical
    raw_map = {
        "fixtures": fx_raw.snapshot_id,
        "teams": boot_raw.snapshot_id,
        "players_raw": boot_raw.snapshot_id,
    }
    conflicts: list[dict[str, Any]] = []
    try:
        with session_scope(engine) as s:
            result.counts = load_canonical(
                s,
                CanonicalDataset(canonical.frames()),
                raw_map,
                source="fpl_api",
                source_priority=_live_priority(),
                conflicts=conflicts,
            )
    except Exception as exc:
        log.exception("ingest_live.load_failed")
        return finish("failed", [{"stage": "load", "error": repr(exc)[:500]}])
    result.issues.extend(conflict_issues(conflicts))
    return finish("succeeded", [])


def _live_priority() -> dict[str, int]:
    return dict(load_versioned_config("", "sources").data.get("source_priority", {}))


def preserve_schedule_first_seen(
    engine: Engine, fixtures: pd.DataFrame, season: str
) -> pd.DataFrame:
    """Keep the earliest ``schedule_available_at`` for fixtures whose schedule is unchanged.

    A fixture's schedule is knowable from the first capture that showed it; a later capture of
    the *same* (gameweek, kickoff, teams) must not move that timestamp. A rescheduled fixture
    (different GW or kickoff) gets the new capture time (ADR-0004).
    """
    try:
        existing = load_from_db(engine, [season])["fixtures"]
    except Exception:  # pragma: no cover - first load
        return fixtures
    if existing.empty or fixtures.empty:
        return fixtures
    prev = existing.set_index("fixture_id")
    out = fixtures.copy()
    for i, r in out.iterrows():
        fid = int(r["fixture_id"])
        if fid not in prev.index:
            continue
        p = prev.loc[fid]
        same = (pd.isna(p["gw"]) and pd.isna(r["gw"])) or (
            p["gw"] == r["gw"] and pd.Timestamp(p["kickoff_at"]) == pd.Timestamp(r["kickoff_at"])
        )
        if same:
            out.at[i, "schedule_available_at"] = min(
                pd.Timestamp(p["schedule_available_at"]), pd.Timestamp(r["schedule_available_at"])
            )
    return out


def now_utc() -> datetime:
    return datetime.now(UTC)
