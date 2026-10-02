"""End-to-end ingestion against PostgreSQL (§55 idempotency, §6 failure handling, §86.2)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select, text

from fpl_ingestion.http import SourceUnavailableError
from fpl_ingestion.pipeline import ingest_historical_season
from fpl_ingestion.sources.historical import HistoricalRepoSource, LocalDirTransport
from fpl_storage import models as m
from fpl_storage.dataset import CanonicalDataset, export_snapshot, load_from_db, load_snapshot
from fpl_storage.db import make_engine, session_scope
from fpl_storage.raw_store import RawStore
from tests.fixtures_util import VAASTAV_DIR, sources_cfg

pytestmark = pytest.mark.integration


class FlakyTransport:
    def read(self, relative_path: str) -> tuple[bytes, str, datetime]:
        raise SourceUnavailableError("upstream outage (simulated)")


class CorruptingTransport(LocalDirTransport):
    """Injects a conflicting duplicate row into merged_gw (must be quarantined)."""

    def read(self, relative_path: str) -> tuple[bytes, str, datetime]:
        content, url, ts = super().read(relative_path)
        if relative_path.endswith("merged_gw.csv"):
            lines = content.decode().splitlines()
            dup = lines[1].split(",")
            idx = lines[0].split(",").index("total_points")
            dup[idx] = str(int(dup[idx]) + 7)
            content = ("\n".join([*lines, ",".join(dup)]) + "\n").encode()
        return content, url, ts


def _source(transport: object) -> HistoricalRepoSource:
    return HistoricalRepoSource.from_config(sources_cfg(), transport)  # type: ignore[arg-type]


def _count(engine, model) -> int:
    with session_scope(engine) as s:
        return int(s.scalar(select(func.count()).select_from(model)))


def test_ingestion_is_idempotent_and_round_trips(fresh_db: str, tmp_path) -> None:
    engine = make_engine(fresh_db)
    store = RawStore(tmp_path / "raw")
    src = _source(LocalDirTransport(VAASTAV_DIR))
    first = ingest_historical_season(engine, "2025-26", src, store, sources_cfg())
    assert first.status == "succeeded"
    assert first.counts["player_gw_stats"]["inserted"] == len(first.dataset["player_match"])

    again = ingest_historical_season(engine, "2025-26", src, store, sources_cfg())
    assert again.status == "succeeded"
    for table, c in again.counts.items():
        assert c["inserted"] == 0 and c["updated"] == 0, table
    assert _count(engine, m.RecordRevision) == 0

    db = load_from_db(engine, ["2025-26"])
    assert db.snapshot_id == first.dataset.snapshot_id
    path = export_snapshot(db, tmp_path / "snapshots")
    assert load_snapshot(path).snapshot_id == db.snapshot_id
    with session_scope(engine) as s:
        jobs = s.execute(select(m.DataJob.status)).scalars().all()
        dq = s.execute(select(m.DataQualityEventRow.issue_code)).scalars().all()
        raws = s.execute(select(func.count()).select_from(m.RawSnapshot)).scalar_one()
    assert jobs == ["succeeded", "succeeded"]
    assert "duplicate_exact" in dq
    assert raws == 4  # same bytes on rerun → no new raw snapshot records


def test_conflicting_batch_is_quarantined_and_canonical_untouched(fresh_db: str, tmp_path) -> None:
    engine = make_engine(fresh_db)
    store = RawStore(tmp_path / "raw")
    good = ingest_historical_season(
        engine, "2025-26", _source(LocalDirTransport(VAASTAV_DIR)), store, sources_cfg()
    )
    before = load_from_db(engine).snapshot_id
    bad = ingest_historical_season(
        engine, "2025-26", _source(CorruptingTransport(VAASTAV_DIR)), store, sources_cfg()
    )
    assert good.status == "succeeded" and bad.status == "quarantined"
    assert any(i.code == "duplicate_conflicting" for i in bad.issues)
    assert load_from_db(engine).snapshot_id == before  # last good data still served
    with session_scope(engine) as s:
        crit = (
            s.execute(
                select(m.DataQualityEventRow).where(m.DataQualityEventRow.severity == "critical")
            )
            .scalars()
            .all()
        )
    assert crit and crit[0].job_id == bad.job_id


def test_source_outage_records_failed_job_without_touching_data(fresh_db: str, tmp_path) -> None:
    engine = make_engine(fresh_db)
    res = ingest_historical_season(
        engine, "2025-26", _source(FlakyTransport()), RawStore(tmp_path), sources_cfg()
    )
    assert res.status == "failed"
    assert _count(engine, m.PlayerGwStatsRow) == 0
    with session_scope(engine) as s:
        job = s.get(m.DataJob, res.job_id)
        assert job is not None and job.status == "failed" and job.errors[0]["stage"] == "extract"


def test_upstream_correction_is_updated_with_revision_history(fresh_db: str, tmp_path) -> None:
    engine = make_engine(fresh_db)
    store = RawStore(tmp_path / "raw")
    ingest_historical_season(
        engine, "2025-26", _source(LocalDirTransport(VAASTAV_DIR)), store, sources_cfg()
    )

    class CorrectedTransport(LocalDirTransport):
        def read(self, relative_path: str) -> tuple[bytes, str, datetime]:
            content, url, ts = super().read(relative_path)
            if relative_path.endswith("merged_gw.csv"):
                lines = content.decode().splitlines()
                header = lines[0].split(",")
                row = lines[1].split(",")
                row[header.index("bps")] = str(int(row[header.index("bps")]) + 1)
                lines[1] = ",".join(row)
                content = ("\n".join(lines) + "\n").encode()
            return content, url, ts

    res = ingest_historical_season(
        engine, "2025-26", _source(CorrectedTransport(VAASTAV_DIR)), store, sources_cfg()
    )
    assert res.status == "succeeded"
    assert res.counts["player_gw_stats"]["updated"] == 1
    with session_scope(engine) as s:
        revs = s.execute(select(m.RecordRevision)).scalars().all()
    assert len(revs) == 1 and revs[0].table_name == "player_gw_stats"


def test_db_constraint_blocks_future_feature_snapshot(fresh_db: str) -> None:
    engine = make_engine(fresh_db)
    with engine.begin() as c:
        c.execute(
            text(
                "insert into seasons (season_code, ruleset_version, schema_version) values "
                "('2025-26','2025-26.1',1)"
            )
        )
    with pytest.raises(Exception, match="no_future_sources"), engine.begin() as c:
        c.execute(
            text(
                "insert into feature_snapshots (id, season_id, target_gw, cutoff_at, "
                "feature_version, max_source_available_at, n_rows, storage_uri, sha256) values "
                "('f1', (select id from seasons), 3, '2025-08-29', 'v1', '2025-08-30', 1, 'x', 'y')"
            )
        )


def test_multi_season_dataset_concat(fresh_db: str, tmp_path) -> None:
    engine = make_engine(fresh_db)
    store = RawStore(tmp_path / "raw")
    src = _source(LocalDirTransport(VAASTAV_DIR))
    parts = []
    for season in ("2024-25", "2025-26", "2026-27"):
        r = ingest_historical_season(engine, season, src, store, sources_cfg())
        assert r.status == "succeeded", (season, r.issues)
        parts.append(r.dataset)
    assert load_from_db(engine).snapshot_id == CanonicalDataset.concat(parts).snapshot_id


def test_lower_priority_source_never_overwrites_higher_priority(fresh_db: str) -> None:
    from datetime import UTC
    from datetime import datetime as dt

    from fpl_storage.repositories import upsert_canonical

    engine = make_engine(fresh_db)
    prio = {"fpl_api": 100, "vaastav": 50}
    with session_scope(engine) as s:
        s.add(m.SeasonRow(season_code="2026-27", ruleset_version="2026-27.1", schema_version=1))
        s.flush()
        sid = s.scalar(select(m.SeasonRow.id))
        for code, sid_ in ((1, 1), (2, 2)):
            s.add(
                m.TeamRow(
                    season_id=sid,
                    source_id=sid_,
                    code=code,
                    name=f"T{code}",
                    short_name=f"T{code}",
                    strength_json={},
                )
            )
        s.flush()
        t1, t2 = s.execute(select(m.TeamRow.id).order_by(m.TeamRow.code)).scalars().all()

    def row(source: str, score: int) -> dict:
        ts = dt(2026, 8, 21, 19, tzinfo=UTC)
        return {
            "season_id": sid,
            "source_id": 1,
            "gw": 1,
            "kickoff_at": ts,
            "home_team_id": t1,
            "away_team_id": t2,
            "status": "final",
            "finalized": True,
            "home_score": score,
            "away_score": 0,
            "home_difficulty": 2,
            "away_difficulty": 4,
            "schedule_available_at": ts,
            "result_available_at": ts,
            "source": source,
        }

    key = ["season_id", "source_id"]
    with session_scope(engine) as s:
        upsert_canonical(s, m.FixtureRow, [row("fpl_api", 3)], key, source_priority=prio)
    with session_scope(engine) as s:
        res = upsert_canonical(s, m.FixtureRow, [row("vaastav", 2)], key, source_priority=prio)
    assert res.updated == 0 and len(res.conflicts) == 1
    assert res.conflicts[0]["fields"]["home_score"] == {"kept": 3, "rejected": 2}
    with session_scope(engine) as s:
        assert s.scalar(select(m.FixtureRow.home_score)) == 3
        res2 = upsert_canonical(s, m.FixtureRow, [row("fpl_api", 4)], key, source_priority=prio)
    assert res2.updated == 1 and not res2.conflicts
