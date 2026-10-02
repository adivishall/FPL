"""Raw store and point-in-time view (§55.1, ADR-0004, §77.2 'never accesses future')."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fpl_storage.dataset import CanonicalDataset
from fpl_storage.pit import PointInTimeView, decision_cutoff
from fpl_storage.raw_store import RawPayload, RawStore
from tests.fixtures_util import SNAPSHOT_AT, fixture_dataset


def _payload(content: bytes) -> RawPayload:
    return RawPayload("src", "res", "https://x/y", datetime.now(UTC), content, "text/csv")


def test_raw_store_is_content_addressed_and_idempotent(tmp_path) -> None:
    store = RawStore(tmp_path)
    p = _payload(b"a,b\n1,2\n")
    path1, path2 = store.put(p), store.put(p)
    assert path1 == path2 and store.get("src", p.sha256) == p.content


def test_raw_store_detects_corruption(tmp_path) -> None:
    import gzip

    store = RawStore(tmp_path)
    p = _payload(b"payload")
    path = store.put(p)
    with gzip.open(path, "wb") as fh:
        fh.write(b"tampered")
    with pytest.raises(OSError, match="corrupt"):
        store.get("src", p.sha256)


# ---------------------------------------------------------------- PIT view

DS = fixture_dataset("2025-26", "2026-27")


def _cutoff(season: str, gw: int) -> pd.Timestamp:
    g = DS["gameweeks"]
    deadline = g[(g["season"] == season) & (g["gw"] == gw)]["deadline_at"].iloc[0]
    return decision_cutoff(deadline, 90)


def test_pit_hides_current_gameweek_results_and_prices() -> None:
    v = PointInTimeView(DS, _cutoff("2025-26", 3))
    pm = v.player_match(["2025-26"])
    assert pm["gw"].max() == 2
    assert {"price", "ownership_count", "transfers_in"}.isdisjoint(pm.columns)
    prices = v.price_observations("2025-26")
    assert (prices["observed_at"] < v.as_of).all()
    assert v.log.max_available_at <= v.as_of
    v.assert_no_leakage()


def test_gw_transfer_counts_visible_only_after_that_deadline() -> None:
    v = PointInTimeView(DS, _cutoff("2025-26", 3))
    assert set(v.gw_transfers("2025-26")["gw"]) == {1, 2}


def test_player_pool_policy_a1_a2() -> None:
    v = PointInTimeView(DS, _cutoff("2025-26", 3))
    pool = v.player_pool("2025-26", 3)
    assert {"prior_observation"} <= set(pool["price_source"])
    assert pool["position"].notna().all()
    gw1 = PointInTimeView(DS, _cutoff("2025-26", 1)).player_pool("2025-26", 1)
    assert set(gw1["price_source"]) == {"first_appearance"}


def test_live_snapshot_drives_pool_when_available() -> None:
    v = PointInTimeView(DS, _cutoff("2026-27", 2))
    assert v.as_of > pd.Timestamp(SNAPSHOT_AT)
    pool = v.player_pool("2026-27", 2)
    assert len(pool) == 616 and set(pool["price_source"]) == {"live_snapshot"}
    before = PointInTimeView(DS, pd.Timestamp(SNAPSHOT_AT) - timedelta(seconds=1))
    assert before.latest_snapshot("2026-27").empty


def test_finalized_only_policy_is_stricter() -> None:
    t = _cutoff("2025-26", 3)
    prov = PointInTimeView(DS, t, "provisional_ok").player_match(["2025-26"])
    fin = PointInTimeView(DS, t, "finalized_only").player_match(["2025-26"])
    assert len(fin) <= len(prov)


def test_naive_as_of_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        PointInTimeView(DS, datetime(2025, 9, 1))


def _perturb_future(ds: CanonicalDataset, as_of: pd.Timestamp, seed: int) -> CanonicalDataset:
    """Randomly corrupt every value that only becomes available after ``as_of``."""
    rng = np.random.default_rng(seed)
    frames = {t: df.copy() for t, df in ds.frames.items()}
    pm = frames["player_match"]
    fut = pm["kickoff_at"] >= as_of  # price/ownership observed at kickoff; stats even later
    for c in ("points", "minutes", "goals", "ownership_count", "bonus"):
        pm.loc[fut, c] = rng.integers(0, 99, fut.sum())
    # Policy A2: a player's *first-appearance* price is available at the cutoff of that GW
    # (initial prices are fixed before the player's first deadline). Only prices whose
    # availability is after ``as_of`` under that rule are future data.
    gws = frames["gameweeks"].set_index(["season", "gw"])["deadline_at"]
    first = pm.sort_values("kickoff_at").groupby(["season", "player_code"]).head(1).index
    price_avail = pm["kickoff_at"].copy()
    first_gw_dl = gws.reindex(pd.MultiIndex.from_frame(pm.loc[first, ["season", "gw"]])).to_numpy()
    price_avail.loc[first] = pd.to_datetime(first_gw_dl, utc=True) - pd.Timedelta(minutes=90)
    pfut = price_avail > as_of
    pm.loc[pfut, "price"] = rng.integers(0, 99, pfut.sum())
    dl = frames["gameweeks"].set_index(["season", "gw"])["deadline_at"]
    gw_dl = pd.Series(dl.reindex(pd.MultiIndex.from_frame(pm[["season", "gw"]])).to_numpy())
    tfut = (gw_dl > as_of).to_numpy()
    pm.loc[tfut, "transfers_in"] = rng.integers(0, 10**6, tfut.sum())
    fx = frames["fixtures"]
    rfut = fx["result_available_at"] > as_of
    fx.loc[rfut, "home_score"] = rng.integers(0, 9, rfut.sum())
    tm = frames["team_match"]
    tm.loc[tm["available_at"] > as_of, "goals_for"] = 42
    sn = frames["player_snapshots"]
    sn.loc[sn["available_at"] > as_of, "price"] = 199
    return CanonicalDataset(frames)


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(gw=st.integers(min_value=1, max_value=5), seed=st.integers(0, 10_000))
def test_future_perturbation_never_changes_pit_outputs(gw: int, seed: int) -> None:
    """§77.2: historical feature inputs never access future timestamps."""
    as_of = _cutoff("2025-26", gw)
    a, b = PointInTimeView(DS, as_of), PointInTimeView(_perturb_future(DS, as_of, seed), as_of)
    pd.testing.assert_frame_equal(a.player_match(), b.player_match())
    pd.testing.assert_frame_equal(a.results(), b.results())
    pd.testing.assert_frame_equal(a.price_observations("2025-26"), b.price_observations("2025-26"))
    pd.testing.assert_frame_equal(a.gw_transfers("2025-26"), b.gw_transfers("2025-26"))
    pd.testing.assert_frame_equal(a.team_match(), b.team_match())
    pd.testing.assert_frame_equal(a.player_pool("2025-26", gw), b.player_pool("2025-26", gw))
