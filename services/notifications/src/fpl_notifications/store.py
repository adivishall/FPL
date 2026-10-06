"""Persistence and delivery of alerts (§74, §75).

* :class:`NotificationStore` writes alerts to ``notifications`` with ``ON CONFLICT DO NOTHING`` on
  (manager_key, dedupe_key): re-evaluating unchanged inputs never produces a second alert.
* :class:`WebhookChannel` posts new alerts to a manager-configured HTTPS endpoint. Because the
  URL is user-supplied it is an SSRF vector (§75): only HTTPS, only allow-listed hosts, no
  credentials in the URL, no redirects, and every resolved address must be public.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.dialects.postgresql import insert

from fpl_domain.hashing import short_id
from fpl_notifications.rules import Alert
from fpl_storage import models as m
from fpl_storage.db import session_scope


class NotificationStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def record(self, manager_key: str, alerts: Sequence[Alert], config_ref: str = "") -> list[str]:
        """Insert alerts; returns the ids of those that were new (duplicates are skipped)."""
        new: list[str] = []
        with session_scope(self.engine) as s:
            for a in alerts:
                nid = short_id("ntf", {"manager": manager_key, "key": a.dedupe_key})
                stmt = (
                    insert(m.NotificationRow)
                    .values(
                        id=nid,
                        manager_key=manager_key,
                        kind=a.kind,
                        severity=a.severity,
                        title=a.title[:255],
                        body=a.body,
                        materiality=float(a.materiality),
                        evidence_json={**a.evidence, "config_ref": config_ref},
                        dedupe_key=a.dedupe_key[:128],
                    )
                    .on_conflict_do_nothing(index_elements=["manager_key", "dedupe_key"])
                    .returning(m.NotificationRow.id)
                )
                if s.execute(stmt).scalar() is not None:
                    new.append(nid)
        return new

    def fetch(
        self, manager_key: str, unread_only: bool = False, limit: int = 50
    ) -> list[dict[str, Any]]:
        with session_scope(self.engine) as s:
            q = select(m.NotificationRow).where(m.NotificationRow.manager_key == manager_key)
            if unread_only:
                q = q.where(m.NotificationRow.read_at.is_(None))
            rows = s.scalars(q.order_by(m.NotificationRow.created_at.desc()).limit(limit)).all()
            return [_row(r) for r in rows]

    def mark(self, manager_key: str, ids: Sequence[str], field: str) -> int:
        if field not in ("read_at", "delivered_at"):
            raise ValueError(field)
        with session_scope(self.engine) as s:
            res = s.execute(
                update(m.NotificationRow)
                .where(
                    m.NotificationRow.manager_key == manager_key,
                    m.NotificationRow.id.in_(list(ids)),
                )
                .values({field: datetime.now(UTC)})
            )
            return int(getattr(res, "rowcount", 0) or 0)

    def delete_manager(self, manager_key: str) -> int:
        with session_scope(self.engine) as s:
            res = s.execute(
                delete(m.NotificationRow).where(m.NotificationRow.manager_key == manager_key)
            )
            return int(getattr(res, "rowcount", 0) or 0)


def _row(r: m.NotificationRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "kind": r.kind,
        "severity": r.severity,
        "title": r.title,
        "body": r.body,
        "materiality": r.materiality,
        "evidence": r.evidence_json,
        "created_at": r.created_at.isoformat(),
        "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None,
        "read_at": r.read_at.isoformat() if r.read_at else None,
    }


# ----------------------------------------------------------------------------- webhook delivery


class UnsafeUrl(ValueError):
    """A user-supplied delivery URL failed the SSRF policy."""


Resolver = Callable[[str, int], list[str]]
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")  # "global" per IANA, but maps onto IPv4


def _resolve(host: str, port: int) -> list[str]:
    return sorted({str(i[4][0]) for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def validate_webhook_url(
    url: str, allowed_hosts: Sequence[str], resolver: Resolver = _resolve
) -> str:
    """Return the URL if it is safe to POST to, else raise :class:`UnsafeUrl`.

    The host allow-list is the primary control (DNS answers can change between this check and
    the connection); the public-address check is defence in depth.
    """
    try:
        parts = urlsplit(url)
        port = parts.port  # parsed lazily: raises on out-of-range or non-numeric ports
    except ValueError as exc:  # e.g. "https://[::1", ":99999", ":abc"
        raise UnsafeUrl("malformed webhook URL") from exc
    if parts.scheme != "https":
        raise UnsafeUrl("only https webhooks are allowed")
    if parts.username or parts.password:
        raise UnsafeUrl("credentials in webhook URLs are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host not in {h.lower() for h in allowed_hosts}:
        raise UnsafeUrl(f"host '{host}' is not on the webhook allow-list")
    if port not in (None, 443):
        raise UnsafeUrl("only the default https port is allowed")
    try:
        addrs = resolver(host, 443)
    except OSError as exc:
        raise UnsafeUrl(f"cannot resolve '{host}'") from exc
    if not addrs:
        raise UnsafeUrl(f"'{host}' has no addresses")
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64:  # judge the embedded IPv4
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if not ip.is_global or ip.is_multicast:
            raise UnsafeUrl(f"'{host}' resolves to a non-public address")
    return url


class WebhookChannel:
    def __init__(
        self,
        url: str,
        allowed_hosts: Sequence[str],
        timeout: float = 5.0,
        client: httpx.Client | None = None,
        resolver: Resolver = _resolve,
    ) -> None:
        self.url = validate_webhook_url(url, allowed_hosts, resolver)
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def send(self, manager_key: str, alert: Alert) -> bool:
        payload = {"manager_key": manager_key, **asdict(alert)}
        try:
            r = self._client.post(self.url, json=payload)
        except httpx.HTTPError:
            return False
        return 200 <= r.status_code < 300
