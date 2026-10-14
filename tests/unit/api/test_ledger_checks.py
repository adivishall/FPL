"""The forecast record's prospective test (contract §2.3), branch by branch: a forecast is in the
live record only when it was promoted and its data exported at or before the decision cutoff,
the stored file is the promoted one, and it is the forecast the promotion names. Capture time
plays no part; a moved deadline is flagged and judged against the earlier cutoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from fpl_api.ledger import data_checks, prospective_checks

CUTOFF = datetime(2026, 10, 17, 8, 30, tzinfo=UTC)
SHA = "a" * 64


def promo(**kw: Any) -> dict[str, Any]:
    base = {
        "snapshot_id": "snap_x",
        "forecast_key": "fc_x",
        "prediction_run_id": "pred_x",
        "promoted_at": CUTOFF - timedelta(minutes=28),
        "snapshot_created_at": CUTOFF - timedelta(minutes=30),
        "artifact_sha256": SHA,
    }
    return {**base, **kw}


def prov(**kw: Any) -> dict[str, Any]:
    base = {
        "season": "2026-27",
        "decision_gw": 7,
        "cutoff": CUTOFF.isoformat(),
        "prediction_run_id": "pred_x",
        "data_snapshot_id": "snap_x",
        "forecast_key": "fc_x",
    }
    return {**base, **kw}


def failed(checks: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(k for k, v in checks.items() if not v["ok"])


def test_a_forecast_promoted_before_the_cutoff_passes() -> None:
    c = prospective_checks(promo(), SHA, prov(), "2026-27", 7, CUTOFF)
    assert failed(c) == [] and "deadline_moved" not in c
    # exactly at the cutoff still counts as before it
    at = prospective_checks(promo(promoted_at=CUTOFF), SHA, prov(), "2026-27", 7, CUTOFF)
    assert failed(at) == []


def test_a_promotion_or_export_after_the_cutoff_is_not_in_the_live_record() -> None:
    late = promo(promoted_at=CUTOFF + timedelta(seconds=1))
    assert failed(prospective_checks(late, SHA, prov(), "2026-27", 7, CUTOFF)) == [
        "promoted_before_cutoff"
    ]
    # promoted in time, but built from data exported after the cutoff (corrected rows,
    # backdated schedule changes): not prospective either
    exported = promo(snapshot_created_at=CUTOFF + timedelta(minutes=1))
    assert failed(prospective_checks(exported, SHA, prov(), "2026-27", 7, CUTOFF)) == [
        "snapshot_exported_before_cutoff"
    ]


def test_a_file_other_than_the_promoted_one_is_rejected() -> None:
    c = prospective_checks(promo(), "b" * 64, prov(), "2026-27", 7, CUTOFF)
    assert failed(c) == ["file_unchanged_since_promotion"]
    for field, value in (
        ("prediction_run_id", "pred_other"),
        ("data_snapshot_id", "snap_other"),
        ("forecast_key", "fc_other"),
        ("decision_gw", 8),
    ):
        c = prospective_checks(promo(), SHA, prov(**{field: value}), "2026-27", 7, CUTOFF)
        assert failed(c) == ["identity_matches_promotion"], field


def test_a_moved_deadline_is_flagged_and_judged_on_the_earlier_cutoff() -> None:
    earlier = CUTOFF - timedelta(hours=1)  # FPL brought the deadline forward after the forecast
    c = prospective_checks(promo(), SHA, prov(), "2026-27", 7, earlier)
    assert "deadline_moved" in c
    assert failed(c) == ["promoted_before_cutoff", "snapshot_exported_before_cutoff"]
    # promoted before both cutoffs: still prospective, with the move recorded
    early = promo(
        promoted_at=earlier - timedelta(minutes=5),
        snapshot_created_at=earlier - timedelta(minutes=7),
    )
    c = prospective_checks(early, SHA, prov(), "2026-27", 7, CUTOFF + timedelta(hours=2))
    assert failed(c) == [] and c["deadline_moved"]["ok"]


def _ds(news: list[str | None], statuses: list[str], result_rows: int) -> dict[str, pd.DataFrame]:
    return {
        "player_snapshots": pd.DataFrame(
            {"season": "2026-27", "player_code": range(len(news)), "news_added": news}
        ),
        "fixtures": pd.DataFrame(
            {"season": "2026-27", "gw": 7, "fixture_id": range(len(statuses)), "status": statuses}
        ),
        "player_match": pd.DataFrame(
            {"season": "2026-27", "gw": [7] * result_rows, "player_code": range(result_rows)}
        ),
    }


def test_the_data_itself_must_predate_the_cutoff() -> None:
    """Clock-independent evidence (a laptop VM's clock can lag after sleep): FPL's own news
    stamps and the gameweek's results."""
    ok = data_checks(
        _ds(["2026-10-17T07:00:00Z", None], ["scheduled"] * 2, 0), "2026-27", 7, CUTOFF
    )
    assert all(v["ok"] for v in ok.values())
    late = data_checks(_ds(["2026-10-17T08:31:00Z"], ["scheduled"], 0), "2026-27", 7, CUTOFF)
    assert not late["no_news_after_cutoff"]["ok"]
    played = data_checks(_ds([None], ["provisional", "scheduled"], 0), "2026-27", 7, CUTOFF)
    assert not played["no_results_of_the_gameweek"]["ok"]
    rows = data_checks(_ds([None], ["scheduled"], 3), "2026-27", 7, CUTOFF)
    assert not rows["no_results_of_the_gameweek"]["ok"]
