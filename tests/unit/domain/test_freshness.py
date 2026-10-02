from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fpl_domain.enums import FreshnessStatus
from fpl_domain.freshness import FreshnessSla, evaluate_freshness

SLA = FreshnessSla(stale_after_hours=12, expired_after_hours=72)
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("age_h", "status"),
    [
        (0, FreshnessStatus.FRESH),
        (11.9, FreshnessStatus.FRESH),
        (12, FreshnessStatus.STALE),
        (71, FreshnessStatus.STALE),
        (72, FreshnessStatus.EXPIRED),
        (24 * 35, FreshnessStatus.EXPIRED),
    ],
)
def test_status_thresholds(age_h: float, status: FreshnessStatus) -> None:
    f = evaluate_freshness("bootstrap", NOW - timedelta(hours=age_h), NOW, SLA)
    assert f.status is status
    assert f.age_seconds == pytest.approx(age_h * 3600)


def test_unknown_when_never_ingested() -> None:
    f = evaluate_freshness("bootstrap", None, NOW, SLA)
    assert f.status is FreshnessStatus.UNKNOWN and f.age_seconds is None


def test_current_environment_snapshot_is_expired() -> None:
    """The latest real snapshot (2026-08-28) is >1 month old on 2026-10-02 → degraded mode."""
    f = evaluate_freshness("bootstrap", datetime(2026, 8, 28, 9, 47, 53, tzinfo=UTC), NOW, SLA)
    assert f.status is FreshnessStatus.EXPIRED
    assert "last validated snapshot" in f.message
