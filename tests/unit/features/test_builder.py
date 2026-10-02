"""Point-in-time feature builder (§56, §77.2 'Historical feature generation never accesses future
timestamps', §7)."""

from __future__ import annotations

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fpl_domain.rules import load_ruleset
from fpl_features.builder import build_features
from fpl_features.registry import FEATURE_NAMES, FEATURE_VERSION, REGISTRY, validate_frame
from fpl_features.version_lock import LOCK, fingerprint
from fpl_storage.pit import PointInTimeView, decision_cutoff
from tests.fixtures_util import ROOT, fixture_dataset
from tests.unit.storage.test_raw_store_and_pit import _perturb_future

DS = fixture_dataset("2024-25", "2025-26", "2026-27")
RS = load_ruleset("2025-26")


def _cut(season: str, gw: int) -> pd.Timestamp:
    g = DS["gameweeks"]
    return decision_cutoff(g[(g["season"] == season) & (g["gw"] == gw)]["deadline_at"].iloc[0], 90)


def _build(ds, season: str, gw: int, horizon: int = 3):
    return build_features(
        PointInTimeView(ds, _cut(season, gw)), season, gw, horizon, load_ruleset(season)
    )


def test_every_registered_feature_is_built_and_in_range() -> None:
    ff = _build(DS, "2025-26", 4)
    assert set(FEATURE_NAMES) <= set(ff.frame.columns)
    assert validate_frame(ff.frame) == []
    assert ff.max_source_available_at is not None
    assert ff.max_source_available_at <= ff.cutoff
    assert set(ff.frame["target_gw"]) == {4, 5}  # excerpt schedule ends at GW5
    assert not ff.frame.duplicated(["player_code", "fixture_id"]).any()


def test_feature_values_match_hand_computation() -> None:
    ff = _build(DS, "2025-26", 4)
    pm = PointInTimeView(DS, _cut("2025-26", 4)).player_match()
    rows = ff.frame[(ff.frame["horizon"] == 0) & ff.frame["mins_ewm_long"].notna()]
    row = rows.sort_values("mins_ewm_long").iloc[-1]
    hist = pm[pm["player_code"] == row["player_code"]].sort_values("kickoff_at")
    assert row["mins_last1"] == hist["minutes"].iloc[-1]
    assert row["pts_last1"] == hist["points"].iloc[-1]
    assert row["n_prior_matches"] == len(hist)
    cur = hist[(hist["season"] == "2025-26") & (hist["minutes"] > 0)]
    assert row["ppg_season"] == pytest.approx(cur["points"].sum() / len(cur))
    w = 0.5 ** (pd.Series(range(len(hist)))[::-1].to_numpy() / 10.0)
    assert row["mins_ewm_long"] == pytest.approx((hist["minutes"] * w).sum() / w.sum())


def test_zero_minute_streak_and_days_since_appearance() -> None:
    ff = _build(DS, "2025-26", 4).frame
    streaky = ff[ff["zero_min_streak"] >= 3]
    assert (streaky["mins_last1"] == 0).all()
    played_last = ff[ff["mins_last1"] > 0]
    assert (played_last["zero_min_streak"] == 0).all()


def test_live_signals_only_with_snapshot() -> None:
    hist = _build(DS, "2025-26", 4).frame
    assert hist["status_flag"].isna().all()
    live = _build(DS, "2026-27", 2).frame
    assert live["status_flag"].notna().all()
    assert live["penalty_taker"].sum() > 10


@settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(gw=st.integers(2, 5), seed=st.integers(0, 1000))
def test_future_perturbation_never_changes_features(gw: int, seed: int) -> None:
    cut = _cut("2025-26", gw)
    a = _build(DS, "2025-26", gw).frame
    b = _build(_perturb_future(DS, cut, seed), "2025-26", gw).frame
    pd.testing.assert_frame_equal(a, b)


def test_feature_version_bumped_when_feature_code_changes() -> None:
    assert LOCK.get(FEATURE_VERSION) == fingerprint(), (
        "builder/registry changed: bump FEATURE_VERSION and update fpl_features.version_lock"
    )


def test_registry_metadata_complete() -> None:
    names = [f.name for f in REGISTRY]
    assert len(names) == len(set(names))
    for f in REGISTRY:
        assert f.definition and f.source and f.timestamp_policy and f.missing_policy


def test_data_dictionary_contains_generated_feature_table() -> None:
    from fpl_features.registry import dictionary_markdown

    doc = (ROOT / "docs" / "DATA_DICTIONARY.md").read_text(encoding="utf-8")
    assert dictionary_markdown() in doc, "regenerate the feature table in DATA_DICTIONARY.md"
