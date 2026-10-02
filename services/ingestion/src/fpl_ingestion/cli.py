"""``fpl-ingest`` command-line interface.

Examples::

    fpl-ingest historical --all                 # pinned historical seasons → PostgreSQL
    fpl-ingest historical --season 2025-26 --local-dir data/fixtures/vaastav
    fpl-ingest live --season 2026-27            # live FPL API capture (degrades gracefully)
    fpl-ingest export-snapshot                  # reproducible Parquet snapshot (+ DB record)
    fpl-ingest dq-report --limit 50
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fpl_domain.config import load_versioned_config
from fpl_ingestion.logs import configure_logging
from fpl_ingestion.pipeline import ingest_historical_season, ingest_live
from fpl_ingestion.sources.fpl_api import FplApiClient
from fpl_ingestion.sources.historical import HistoricalRepoSource, LocalDirTransport
from fpl_storage import models as m
from fpl_storage.dataset import export_snapshot, load_from_db
from fpl_storage.db import make_engine, session_scope
from fpl_storage.raw_store import RawStore


def data_dir() -> Path:
    return Path(os.environ.get("FPL_DATA_DIR", "data"))


def _cmd_historical(args: argparse.Namespace) -> int:
    cfg = load_versioned_config("", "sources").data
    seasons = cfg["historical_repo"]["seasons"] if args.all else args.season
    if not seasons:
        print("specify --season or --all", file=sys.stderr)
        return 2
    transport = LocalDirTransport(Path(args.local_dir)) if args.local_dir else None
    source = HistoricalRepoSource.from_config(cfg, transport)
    engine = make_engine()
    store = RawStore(data_dir() / "raw")
    worst = 0
    for season in seasons:
        res = ingest_historical_season(engine, season, source, store, cfg)
        print(
            json.dumps(
                {
                    "season": season,
                    "status": res.status,
                    "job_id": res.job_id,
                    "issues": [f"{i.severity.value}:{i.code}" for i in res.issues],
                    "counts": res.counts,
                },
                default=str,
            )
        )
        worst = max(worst, {"succeeded": 0, "quarantined": 3, "failed": 4}.get(res.status, 1))
    return worst


def _cmd_live(args: argparse.Namespace) -> int:
    cfg = load_versioned_config("", "sources").data
    client = FplApiClient.from_config(cfg)
    res = ingest_live(make_engine(), client, RawStore(data_dir() / "raw"), args.season)
    print(
        json.dumps({"status": res.status, "job_id": res.job_id, "counts": res.counts}, default=str)
    )
    return 0 if res.status == "succeeded" else 4


def _cmd_export(args: argparse.Namespace) -> int:
    engine = make_engine()
    ds = load_from_db(engine, args.seasons or None)
    cfg = load_versioned_config("", "sources")
    path = export_snapshot(ds, Path(args.out), {"sources_config": cfg.ref})
    with session_scope(engine) as s:
        s.execute(
            pg_insert(m.DataSnapshot)
            .values(
                id=ds.snapshot_id,
                as_of=datetime.now(UTC),
                seasons=ds.seasons(),
                tables={t: {"sha256": h} for t, h in ds.table_hashes.items()},
                storage_uri=str(path),
                source_versions={
                    "sources_config": cfg.ref,
                    "historical_repo": cfg.data["historical_repo"]["name"],
                    "commit": cfg.data["historical_repo"]["commit"],
                },
                validated=True,
            )
            .on_conflict_do_nothing()
        )
    print(json.dumps({"snapshot_id": ds.snapshot_id, "path": str(path)}))
    return 0


def _cmd_dq(args: argparse.Namespace) -> int:
    engine = make_engine()
    with session_scope(engine) as s:
        rows = (
            s.execute(
                select(m.DataQualityEventRow)
                .order_by(m.DataQualityEventRow.detected_at.desc())
                .limit(args.limit)
            )
            .scalars()
            .all()
        )
        for r in rows:
            print(
                f"{r.detected_at:%Y-%m-%d %H:%M} {r.severity:8s} {r.source:10s} "
                f"{r.issue_code:32s} {r.details_json.get('message', '')}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fpl-ingest")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("historical", help="ingest pinned historical seasons")
    h.add_argument("--season", action="append", default=[])
    h.add_argument("--all", action="store_true")
    h.add_argument("--local-dir", help="read the same file layout from a local directory")
    h.set_defaults(func=_cmd_historical)
    lv = sub.add_parser("live", help="capture live FPL API state")
    lv.add_argument("--season", required=True)
    lv.set_defaults(func=_cmd_live)
    ex = sub.add_parser("export-snapshot", help="export a reproducible Parquet snapshot")
    ex.add_argument("--seasons", nargs="*")
    ex.add_argument("--out", default=str(data_dir() / "snapshots"))
    ex.set_defaults(func=_cmd_export)
    dq = sub.add_parser("dq-report", help="recent data-quality events")
    dq.add_argument("--limit", type=int, default=50)
    dq.set_defaults(func=_cmd_dq)
    args = p.parse_args(argv)
    configure_logging(os.environ.get("FPL_LOG_LEVEL", "INFO"))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
