"""Request security: API keys, rate limiting, body-size limits (§35, §75).

* API keys are compared by SHA-256 hash in constant time (plaintext keys are never stored or
  logged). When ``require_api_key`` is on, every non-GET endpoint needs ``X-API-Key``.
* Token-bucket rate limiting per client (API key hash or client IP); expensive endpoints draw
  from a separate, smaller bucket. In-process buckets suffice for a single API replica; the
  limiter interface is where a Redis-backed limiter plugs in for multiple replicas.
* No FPL account credentials are accepted anywhere: manager sync uses the public manager id.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from fpl_api.settings import Settings

EXPENSIVE_PREFIXES = (
    "/api/v1/optimize",
    "/api/v1/replacement",
    "/api/v1/recommendations/generate",
    "/api/v1/chips",
    "/api/v1/scenarios",
    "/api/v1/what-if",
    "/api/v1/simulate",
    "/api/v1/backtests",
    "/api/v1/lineup",
)


# reads of manager-linked personal data need a key too when keys are required (§75): every
# GET that names a manager, and the resources that embed manager-linked records
PRIVILEGED_GET_PREFIXES = (
    "/api/v1/managers/",
    "/api/v1/squad",
    "/api/v1/settings",
    "/api/v1/notifications",
    "/api/v1/decisions",
    "/api/v1/recommendations",
    "/api/v1/jobs",
)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


_MANAGER_IN_PATH = re.compile(r"(/managers/)([^/]+)")


def loggable_path(path: str) -> str:
    """A request path safe to log: manager keys in the path are replaced by a short hash (the
    same pseudonym the audit log uses), so logs never hold the identifier itself."""
    return _MANAGER_IN_PATH.sub(lambda mt: mt.group(1) + "k:" + hash_key(mt.group(2))[:16], path)


@dataclass
class TokenBucket:
    rate_per_minute: int
    tokens: dict[str, tuple[float, float]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def take(self, client: str, now: float | None = None) -> tuple[bool, float]:
        now = time.monotonic() if now is None else now
        cap = float(self.rate_per_minute)
        refill = cap / 60.0
        with self.lock:
            tokens, last = self.tokens.get(client, (cap, now))
            tokens = min(cap, tokens + (now - last) * refill)
            if tokens < 1.0:
                self.tokens[client] = (tokens, now)
                return False, (1.0 - tokens) / refill
            self.tokens[client] = (tokens - 1.0, now)
            return True, 0.0


class Guard:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.general = TokenBucket(settings.rate_limit_per_minute)
        self.expensive = TokenBucket(settings.expensive_rate_limit_per_minute)

    def client_id(self, request: Request) -> str:
        key = request.headers.get("x-api-key")
        if key:
            return "k:" + hash_key(key)[:16]
        return "ip:" + (request.client.host if request.client else "unknown")

    def check_key(self, request: Request) -> None:
        if not self.settings.require_api_key or request.method == "OPTIONS":
            return
        if (
            request.method in ("GET", "HEAD")
            and not request.url.path.startswith(PRIVILEGED_GET_PREFIXES)
            and "manager_key" not in request.query_params
        ):
            return
        key = request.headers.get("x-api-key")
        if not key:
            raise HTTPException(status_code=401, detail="X-API-Key required")
        digest = hash_key(key)
        if not any(hmac.compare_digest(digest, h) for h in self.settings.api_keys_sha256):
            raise HTTPException(status_code=403, detail="invalid API key")

    def check_rate(self, request: Request) -> None:
        cid = self.client_id(request)
        ok, wait = self.general.take(cid)
        if ok and request.url.path.startswith(EXPENSIVE_PREFIXES) and request.method != "GET":
            ok, wait = self.expensive.take(cid)
        if not ok:
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={"Retry-After": str(max(1, int(wait) + 1))},
            )

    def check_size(self, request: Request) -> None:
        length = request.headers.get("content-length")
        if length and int(length) > self.settings.max_body_bytes:
            raise HTTPException(status_code=413, detail="request body too large")
