"""Idempotent persistence helpers for canonical data and operational records (§55).

``upsert_canonical`` implements the ingestion contract: running the same job twice never
duplicates logical records; changed rows are updated *and* their previous version is written to
``record_revisions`` (provisional → final, upstream corrections), so nothing is silently lost.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Table, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from fpl_domain.hashing import content_hash
from fpl_storage import models as m

_CHUNK = 2000


@dataclass
class UpsertResult:
    table: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    revisions: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "revisions": self.revisions,
            "conflicts": len(self.conflicts),
        }


def _chunks(seq: Sequence[Any], n: int = _CHUNK) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def row_hash(row: dict[str, Any], exclude: Iterable[str] = ()) -> str:
    skip = {"content_hash", "raw_snapshot_id", *exclude}
    return content_hash({k: v for k, v in row.items() if k not in skip})


def upsert_canonical(
    session: Session,
    model: type[m.Base],
    rows: Sequence[dict[str, Any]],
    natural_key: Sequence[str],
    *,
    raw_snapshot_id: str | None = None,
    hash_exclude: Iterable[str] = (),
    source_priority: dict[str, int] | None = None,
) -> UpsertResult:
    """Insert new rows, update changed rows (recording revisions), skip identical rows.

    With ``source_priority`` (and a ``source`` column), an incoming row from a *lower*-priority
    source never overwrites a differing row from a higher-priority source: the existing value is
    kept and the disagreement is returned in ``result.conflicts`` (§6 data-quality rule, §55.2
    'Conflict'). Equal or higher priority updates normally, with revision history.
    """
    table = cast(Table, model.__table__)
    result = UpsertResult(table=table.name)
    if not rows:
        return result
    exclude = tuple(hash_exclude)
    has_hash_col = "content_hash" in table.c
    prepared: dict[tuple[Any, ...], dict[str, Any]] = {}
    hashes: dict[tuple[Any, ...], str] = {}
    for r in rows:
        row = dict(r)
        h = row_hash(row, exclude)
        if has_hash_col:
            row["content_hash"] = h
        if raw_snapshot_id is not None and "raw_snapshot_id" in table.c:
            row.setdefault("raw_snapshot_id", raw_snapshot_id)
        key = tuple(row[k] for k in natural_key)
        if key in hashes and hashes[key] != h:
            raise ValueError(f"{table.name}: conflicting rows for natural key {key}")
        prepared[key], hashes[key] = row, h
    compared_cols = [
        c
        for c in next(iter(prepared.values()))
        if c not in {"content_hash", "raw_snapshot_id", *exclude}
    ]

    key_cols = [table.c[k] for k in natural_key]
    existing: dict[tuple[Any, ...], tuple[str, dict[str, Any]]] = {}
    keys = list(prepared)
    for chunk in _chunks(keys, 1000):
        stmt = select(table).where(tuple_(*key_cols).in_(list(chunk)))
        for rec in session.execute(stmt).mappings():
            k = tuple(rec[c] for c in natural_key)
            old = dict(rec)
            existing[k] = (row_hash({c: old[c] for c in compared_cols}, exclude), old)

    to_insert, to_update = [], []
    for key, row in prepared.items():
        if key not in existing:
            to_insert.append(row)
        elif existing[key][0] != hashes[key]:
            old = existing[key][1]
            if (
                source_priority is not None
                and "source" in row
                and source_priority.get(str(old.get("source")), 0)
                > source_priority.get(str(row["source"]), 0)
            ):
                diff = {
                    c: {"kept": _jsonable({c: old[c]})[c], "rejected": row[c]}
                    for c in compared_cols
                    if c != "source" and old.get(c) != row.get(c)
                }
                result.conflicts.append(
                    {
                        "table": table.name,
                        "key": [str(k) for k in key],
                        "kept_source": old.get("source"),
                        "rejected_source": row["source"],
                        "fields": _jsonable_nested(diff),
                    }
                )
                continue
            to_update.append((key, row))
        else:
            result.unchanged += 1

    # PostgreSQL allows at most 65,535 bind parameters per statement.
    rows_per_stmt = max(1, 30_000 // max(1, len(to_insert[0]) if to_insert else 1))
    for chunk in _chunks(to_insert, rows_per_stmt):
        session.execute(pg_insert(table).values(list(chunk)))
    result.inserted = len(to_insert)

    for key, row in to_update:
        old_hash, old = existing[key]
        session.add(
            m.RecordRevision(
                table_name=table.name,
                natural_key="|".join(str(k) for k in key),
                old_hash=old_hash,
                new_hash=hashes[key],
                old_payload=_jsonable(old),
                raw_snapshot_id=raw_snapshot_id,
            )
        )
        cond = [table.c[k] == v for k, v in zip(natural_key, key, strict=True)]
        values = {k: v for k, v in row.items() if k not in natural_key}
        session.execute(update(table).where(*cond).values(**values))
    result.updated = result.revisions = len(to_update)
    session.flush()
    return result


def _jsonable_nested(d: dict[str, Any]) -> dict[str, Any]:
    return {k: _jsonable(v) if isinstance(v, dict) else v for k, v in d.items()}


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


# ------------------------------------------------------------------ lookups


def season_id(session: Session, season_code: str) -> int:
    sid = session.scalar(select(m.SeasonRow.id).where(m.SeasonRow.season_code == season_code))
    if sid is None:
        raise LookupError(f"season {season_code} not loaded")
    return int(sid)


def team_ids_by_code(session: Session, sid: int) -> dict[int, int]:
    rows = session.execute(select(m.TeamRow.code, m.TeamRow.id).where(m.TeamRow.season_id == sid))
    return {int(c): int(i) for c, i in rows}


def fixture_ids_by_source(session: Session, sid: int) -> dict[int, int]:
    rows = session.execute(
        select(m.FixtureRow.source_id, m.FixtureRow.id).where(m.FixtureRow.season_id == sid)
    )
    return {int(s): int(i) for s, i in rows}


# ------------------------------------------------------------------ operational records


@dataclass
class JobRecorder:
    """Writes the ``data_jobs`` lifecycle and data-quality events for one pipeline run."""

    session: Session
    job_type: str
    source: str
    params: dict[str, Any] = field(default_factory=dict)
    job_id: str = field(default_factory=lambda: f"job_{uuid.uuid4().hex[:20]}")

    def start(self) -> str:
        self.session.add(
            m.DataJob(
                id=self.job_id,
                job_type=self.job_type,
                source=self.source,
                params=self.params,
                status="running",
                started_at=datetime.now(UTC),
                row_counts={},
                errors=[],
            )
        )
        self.session.flush()
        return self.job_id

    def finish(self, status: str, row_counts: dict[str, Any], errors: list[Any]) -> None:
        self.session.execute(
            update(m.DataJob)
            .where(m.DataJob.id == self.job_id)
            .values(
                status=status, completed_at=datetime.now(UTC), row_counts=row_counts, errors=errors
            )
        )
        self.session.flush()

    def record_issue(
        self,
        *,
        issue_code: str,
        severity: str,
        entity_type: str,
        entity_id: str | None,
        details: dict[str, Any],
    ) -> None:
        self.session.add(
            m.DataQualityEventRow(
                id=f"dq_{uuid.uuid4().hex[:24]}",
                source=self.source,
                severity=severity,
                detected_at=datetime.now(UTC),
                entity_type=entity_type,
                entity_id=entity_id,
                issue_code=issue_code,
                details_json=details,
                job_id=self.job_id,
            )
        )


def record_raw_snapshot(session: Session, payload_meta: dict[str, Any]) -> None:
    """Insert raw snapshot metadata if this exact payload has not been recorded yet."""
    stmt = pg_insert(m.RawSnapshot).values(**payload_meta).on_conflict_do_nothing()
    session.execute(stmt)
