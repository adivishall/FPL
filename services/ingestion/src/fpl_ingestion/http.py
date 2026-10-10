"""Resilient HTTP fetching for source adapters (§6 'Extract', §75 SSRF prevention).

* Exponential backoff with jitter on transient failures (connect/read errors, 429, 5xx).
* Client-side rate limiting (minimum interval between requests) to avoid hammering sources.
* Strict host allowlist: URLs are built from configuration, never from user input, and any URL
  whose host is not allowlisted is refused before a connection is attempted.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
import structlog
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = structlog.get_logger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class SourceUnavailableError(Exception):
    """The source could not be reached after all retries (triggers degraded mode, §82)."""


class HttpStatusError(SourceUnavailableError):
    """The source answered with a non-200 status that is not retried (404 for an unknown entry,
    403 …): still unavailable for the caller, but distinguishable from an outage."""

    def __init__(self, url: str, status: int) -> None:
        super().__init__(f"{url} returned HTTP {status}")
        self.status = status


class DisallowedHostError(ValueError):
    pass


class _RetryableHTTPError(Exception):
    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content: bytes
    content_type: str
    retrieved_at: datetime
    attempts: int


@dataclass
class HttpConfig:
    allowed_hosts: tuple[str, ...]
    timeout_seconds: float = 20.0
    max_retries: int = 5
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    min_seconds_between_requests: float = 0.0
    user_agent: str = "fpl-decision-engine/0.1"


class HttpFetcher:
    def __init__(self, config: HttpConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client or httpx.Client(
            timeout=config.timeout_seconds,
            headers={"User-Agent": config.user_agent, "Accept": "*/*"},
            follow_redirects=False,  # redirects could leave the allowlist
        )
        self._lock = threading.Lock()
        self._last_request = 0.0

    def _check_host(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise DisallowedHostError(f"only https URLs are allowed: {url}")
        if parsed.hostname not in self.config.allowed_hosts:
            raise DisallowedHostError(f"host {parsed.hostname!r} is not allowlisted")

    def _throttle(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_request
            wait = self.config.min_seconds_between_requests - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def get(self, url: str) -> FetchResult:
        self._check_host(url)
        attempts = {"n": 0}

        def _is_retryable(exc: BaseException) -> bool:
            return isinstance(exc, (_RetryableHTTPError, httpx.TransportError))

        @retry(
            reraise=False,
            stop=stop_after_attempt(self.config.max_retries),
            wait=wait_exponential_jitter(
                initial=self.config.backoff_initial_seconds, max=self.config.backoff_max_seconds
            ),
            retry=retry_if_exception(_is_retryable),
        )
        def _do() -> httpx.Response:
            attempts["n"] += 1
            self._throttle()
            resp = self._client.get(url)
            if resp.status_code in RETRYABLE_STATUS:
                log.warning(
                    "http.retryable_status", url=url, status=resp.status_code, attempt=attempts["n"]
                )
                raise _RetryableHTTPError(resp.status_code, url)
            return resp

        try:
            resp = _do()
        except RetryError as exc:
            msg = f"{url} unavailable after {attempts['n']} attempts"
            raise SourceUnavailableError(msg) from exc
        if resp.status_code != 200:
            raise HttpStatusError(url, resp.status_code)
        return FetchResult(
            url=url,
            status=resp.status_code,
            content=resp.content,
            content_type=resp.headers.get("content-type", "application/octet-stream").split(";")[0],
            retrieved_at=datetime.now(UTC),
            attempts=attempts["n"],
        )

    def close(self) -> None:
        self._client.close()
