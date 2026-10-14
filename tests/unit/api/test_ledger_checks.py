"""The forecast record's prospective test (contract §2.3), branch by branch: a forecast is in the
live record only when FPL's server time shows its file existed at or before the decision cutoff,
the stored file is the promoted one, it is the forecast the promotion names, and its data shows
no leakage. Without FPL's time it is ``prospective_unverified`` at best — never prospective on
the host's clock or on the absence of late news. Capture time plays no part; a moved deadline is
flagged and judged against the earlier cutoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from fpl_api.ledger import TIME_CHECK, data_checks, local_clock, prospective_checks, state_of

CUTOFF = datetime(2026, 10, 17, 8, 30, tzinfo=UTC)
SHA = "a" * 64
ATTESTED = CUTOFF - timedelta(minutes=27)


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


def checks(existed_by: datetime | None = ATTESTED, **kw: Any) -> dict[str, dict[str, Any]]:
    return prospective_checks(
        kw.pop("promo", promo()),
        kw.pop("sha", SHA),
        kw.pop("prov", prov()),
        "2026-27",
        7,
        kw.pop("cutoff", CUTOFF),
        existed_by,
    )


def test_fpl_server_time_before_the_cutoff_makes_it_prospective() -> None:
    c = checks()
    assert failed(c) == [] and "deadline_moved" not in c
    assert state_of(c) == ("prospective", [])
    assert "27 min before" in c[TIME_CHECK]["detail"]
    # exactly at the cutoff still counts as before it
    assert failed(checks(CUTOFF)) == []


def test_without_fpl_server_time_it_is_unverified_whatever_the_hosts_clock_says() -> None:
    """The host's clock puts the promotion 28 minutes before the cutoff and the data shows no
    late news — neither is evidence of time."""
    c = checks(None)
    assert failed(c) == [TIME_CHECK]
    assert state_of(c) == ("prospective_unverified", [TIME_CHECK])
    # FPL's time only after the cutoff (e.g. a later existence attestation): still unproven
    late = checks(CUTOFF + timedelta(minutes=40))
    assert state_of(late)[0] == "prospective_unverified"
    assert "40 min after" in late[TIME_CHECK]["detail"]


def test_a_file_other_than_the_promoted_one_is_rejected() -> None:
    c = checks(sha="b" * 64)
    assert failed(c) == ["file_unchanged_since_promotion"]
    assert state_of(c)[0] == "live_unverified"
    # a failed integrity check is never softened to "unverified timing"
    assert state_of(checks(None, sha="b" * 64))[0] == "live_unverified"
    for field, value in (
        ("prediction_run_id", "pred_other"),
        ("data_snapshot_id", "snap_other"),
        ("forecast_key", "fc_other"),
        ("decision_gw", 8),
    ):
        c = checks(prov=prov(**{field: value}))
        assert failed(c) == ["identity_matches_promotion"], field


def test_a_moved_deadline_is_flagged_and_judged_on_the_earlier_cutoff() -> None:
    earlier = CUTOFF - timedelta(hours=1)  # FPL brought the deadline forward after the forecast
    c = checks(cutoff=earlier)
    assert "deadline_moved" in c
    assert failed(c) == [TIME_CHECK]
    # attested before both cutoffs: still prospective, with the move recorded
    c = checks(earlier - timedelta(minutes=5), cutoff=CUTOFF + timedelta(hours=2))
    assert failed(c) == [] and c["deadline_moved"]["ok"]


def test_the_hosts_clock_is_recorded_but_never_decides() -> None:
    ok = local_clock(promo(), CUTOFF)
    assert ok["ok"] and "host clocks" in ok["detail"]
    # a host clock running ahead puts the promotion after the cutoff; FPL's time still decides
    ahead = promo(promoted_at=CUTOFF + timedelta(minutes=3))
    assert not local_clock(ahead, CUTOFF)["ok"]
    c = checks(promo=ahead)
    c["info"] = {"local_clock": local_clock(ahead, CUTOFF)}
    assert state_of(c) == ("prospective", [])


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


def test_leakage_in_the_data_disqualifies_but_its_absence_proves_no_time() -> None:
    """FPL news stamps and the gameweek's results detect post-cutoff data (a failure means
    leakage); a pass is not evidence of when the forecast existed."""
    ok = data_checks(
        _ds(["2026-10-17T07:00:00Z", None], ["scheduled"] * 2, 0), "2026-27", 7, CUTOFF
    )
    assert all(v["ok"] for v in ok.values())
    assert state_of({**checks(None), **ok})[0] == "prospective_unverified"
    late = data_checks(_ds(["2026-10-17T08:31:00Z"], ["scheduled"], 0), "2026-27", 7, CUTOFF)
    assert not late["no_news_after_cutoff"]["ok"]
    assert state_of({**checks(), **late})[0] == "live_unverified"
    played = data_checks(_ds([None], ["provisional", "scheduled"], 0), "2026-27", 7, CUTOFF)
    assert not played["no_results_of_the_gameweek"]["ok"]
    rows = data_checks(_ds([None], ["scheduled"], 3), "2026-27", 7, CUTOFF)
    assert not rows["no_results_of_the_gameweek"]["ok"]
