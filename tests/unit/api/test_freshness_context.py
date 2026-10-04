"""The freshness block never calls stale match history fresh (§6, §82)."""

from __future__ import annotations

from datetime import UTC, datetime

from fpl_api.services import DataService
from fpl_api.settings import Settings
from fpl_storage.dataset import CanonicalDataset
from tests.fixtures_util import fixture_dataset

DS = fixture_dataset("2026-27")
NOW = datetime(2026, 8, 29, tzinfo=UTC)


def test_complete_results_add_no_reason() -> None:
    ctx = DataService(Settings(), DS).current(NOW)
    assert not any("results missing" in r for r in ctx.degraded_reasons)


def test_finished_gameweek_without_results_is_reported() -> None:
    frames = dict(DS.frames)
    g = frames["gameweeks"].copy()
    g.loc[(g["season"] == "2026-27") & (g["gw"].isin([2, 3])), "status"] = "finalized"
    frames["gameweeks"] = g
    ctx = DataService(Settings(), CanonicalDataset(frames)).current(NOW)
    assert "match results missing for finished GW2–GW3: forecasts use results through GW1 only" in (
        ctx.degraded_reasons
    )
    assert ctx.block("snap_x")["degraded"] is True
