"""Helpers to load the committed real-data excerpts through the production pipeline code."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import yaml

from fpl_domain.rules import load_ruleset
from fpl_ingestion.canonical import CanonicalSeason, PitPolicy, canonicalize
from fpl_ingestion.pipeline import parse_and_validate
from fpl_ingestion.quality import SeasonFrames, run_quality_gates
from fpl_ingestion.sources.historical import HistoricalRepoSource, LocalDirTransport
from fpl_storage.dataset import CanonicalDataset

ROOT = Path(__file__).resolve().parents[1]
VAASTAV_DIR = ROOT / "data" / "fixtures" / "vaastav"
API_DIR = ROOT / "data" / "fixtures" / "fpl_api"
SNAPSHOT_AT = datetime(2026, 8, 28, 9, 47, 53, tzinfo=UTC)


def sources_cfg() -> dict:
    return yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))


def local_source() -> HistoricalRepoSource:
    return HistoricalRepoSource.from_config(sources_cfg(), LocalDirTransport(VAASTAV_DIR))


def validated_frames(season: str) -> SeasonFrames:
    src = local_source()
    frames = parse_and_validate(season, src.fetch_season(season))
    return run_quality_gates(frames)


@lru_cache(maxsize=8)
def canonical_season(season: str) -> CanonicalSeason:
    cfg = sources_cfg()
    frames = validated_frames(season)
    snap = SNAPSHOT_AT if season == "2026-27" else None
    return canonicalize(
        frames,
        load_ruleset(season),
        PitPolicy.from_config(cfg),
        SNAPSHOT_AT,
        snapshot_captured_at=snap,
        source="vaastav",
        source_version=cfg["historical_repo"]["commit"],
    )


def fixture_dataset(*seasons: str) -> CanonicalDataset:
    parts = [CanonicalDataset(canonical_season(s).frames()) for s in seasons]
    return CanonicalDataset.concat(parts)
