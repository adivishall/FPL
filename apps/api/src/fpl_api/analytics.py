"""First-party product analytics for the beta (M1.1a).

Only named events from the allow-list below are stored, with a small set of allow-listed
properties (short strings, numbers, booleans). Events are keyed by the signed-in user, never by a
manager key, and never contain credentials or free text. Users can opt out (``analytics_opt_out``
on the account): their events are dropped server-side as well as not sent. The retention job
deletes events older than ``RETENTION_DAYS`` (``docs/DEPLOYMENT.md`` → *Product analytics*).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, delete, func, select

from fpl_storage import models as m
from fpl_storage.db import session_scope

EVENTS = frozenset(
    {
        "onboarding_started",
        "onboarding_completed",
        "manager_import_succeeded",
        "manager_import_failed",
        "squad_analysis_viewed",
        "recommendation_opened",
        "transfer_comparison_completed",
        "recommendation_followed",
        "recommendation_dismissed",
        "return_visit",
    }
)
PROPS = frozenset(
    {"page", "action", "gameweek", "latency_ms", "status", "warnings", "days_since", "source"}
)
RETENTION_DAYS = 90
MAX_STR = 64


def sanitise(props: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only allow-listed keys with scalar values; strings are truncated, nothing else."""
    out: dict[str, Any] = {}
    for k, v in (props or {}).items():
        if k not in PROPS:
            continue
        if isinstance(v, bool | int | float):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:MAX_STR]
    return out


def record(engine: Engine, user_id: str, event: str, props: dict[str, Any] | None) -> bool:
    """Store one event for a user; False when the event is unknown (nothing stored)."""
    if event not in EVENTS:
        return False
    with session_scope(engine) as s:
        opted_out = s.scalar(select(m.UserRow.analytics_opt_out).where(m.UserRow.id == user_id))
        if opted_out is None or opted_out:
            return False
        s.add(m.ProductEventRow(user_id=user_id, event=event, props_json=sanitise(props)))
    return True


def prune(engine: Engine, now: datetime | None = None, days: int = RETENTION_DAYS) -> int:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    with session_scope(engine) as s:
        res = s.execute(delete(m.ProductEventRow).where(m.ProductEventRow.created_at < cutoff))
        return int(getattr(res, "rowcount", 0) or 0)


def summary(engine: Engine, days: int = 28) -> dict[str, Any]:
    """Operator view: event counts and distinct users per event over the last ``days``."""
    since = datetime.now(UTC) - timedelta(days=days)
    with session_scope(engine) as s:
        rows = s.execute(
            select(
                m.ProductEventRow.event,
                func.count(),
                func.count(func.distinct(m.ProductEventRow.user_id)),
            )
            .where(m.ProductEventRow.created_at >= since)
            .group_by(m.ProductEventRow.event)
        ).all()
    return {
        "days": days,
        "events": {str(e): {"count": int(c), "users": int(u)} for e, c, u in rows},
    }
