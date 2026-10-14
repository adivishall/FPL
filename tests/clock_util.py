"""A stand-in for FPL's server clock (``fpl_api.attestation``): the suite never calls FPL."""

from __future__ import annotations

from datetime import UTC, datetime

from fpl_api.attestation import ServerTime
from fpl_ingestion.http import SourceUnavailableError


class FakeFplClock:
    """FPL's server time as the forecast record reads it: a set time, the real one, or
    unreachable."""

    url = "https://fantasy.premierleague.com/api/event-status/"

    def __init__(self, at: datetime | None = None) -> None:
        self.at = at
        self.down = False

    def now(self) -> ServerTime:
        if self.down:
            raise SourceUnavailableError(f"{self.url} unavailable (test)")
        t = (self.at or datetime.now(UTC)).replace(microsecond=0)
        return ServerTime(self.url, t, 0, datetime.now(UTC), datetime.now(UTC))
