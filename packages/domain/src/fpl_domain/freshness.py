"""Data freshness evaluation (§55.2 Freshness, §82 degraded mode)."""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from fpl_domain.enums import FreshnessStatus


class FreshnessSla(BaseModel):
    model_config = ConfigDict(frozen=True)
    stale_after_hours: float
    expired_after_hours: float


class Freshness(BaseModel):
    """Freshness block returned with every API response that serves data (§6 'Serve')."""

    model_config = ConfigDict(frozen=True)
    source: str
    last_success_at: datetime | None
    as_of: datetime
    age_seconds: float | None
    status: FreshnessStatus
    message: str


def evaluate_freshness(
    source: str, last_success_at: datetime | None, now: datetime, sla: FreshnessSla
) -> Freshness:
    if last_success_at is None:
        return Freshness(
            source=source,
            last_success_at=None,
            as_of=now,
            age_seconds=None,
            status=FreshnessStatus.UNKNOWN,
            message=f"no successful {source} snapshot recorded",
        )
    age = max(now - last_success_at, timedelta(0))
    hours = age.total_seconds() / 3600
    if hours >= sla.expired_after_hours:
        status, msg = (
            FreshnessStatus.EXPIRED,
            (
                f"{source} data is {hours:.0f}h old (expired after {sla.expired_after_hours:g}h); "
                "recommendations are served from the last validated snapshot with reduced "
                "confidence"
            ),
        )
    elif hours >= sla.stale_after_hours:
        status, msg = (
            FreshnessStatus.STALE,
            (f"{source} data is {hours:.1f}h old (stale after {sla.stale_after_hours:g}h)"),
        )
    else:
        status, msg = FreshnessStatus.FRESH, f"{source} data is {hours:.1f}h old"
    return Freshness(
        source=source,
        last_success_at=last_success_at,
        as_of=now,
        age_seconds=age.total_seconds(),
        status=status,
        message=msg,
    )
