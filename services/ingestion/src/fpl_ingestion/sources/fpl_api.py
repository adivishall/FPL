"""Official FPL API adapter (read-only, public endpoints, no credentials — §35, §75).

Each call returns ``(RawPayload, parsed_model)``: the raw bytes go to the raw store unchanged
and the parsed model has been validated against ``fpl_api_schemas``. Identifiers supplied by
users (manager ids, league ids, gameweeks) are validated as bounded integers and interpolated
into a fixed URL template on an allowlisted host, so no caller can make the service fetch an
arbitrary URL (SSRF, §75).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, TypeAdapter

from fpl_ingestion.http import HttpConfig, HttpFetcher
from fpl_ingestion.sources import fpl_api_schemas as s
from fpl_storage.raw_store import RawPayload

SOURCE_NAME = "fpl_api"
SCHEMA_VERSION = "fpl-api-2026-27"
M = TypeVar("M", bound=BaseModel)

MAX_ENTRY_ID = 50_000_000
MAX_LEAGUE_ID = 50_000_000


def _bounded(name: str, value: int, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ValueError(f"{name} must be an integer in [{lo}, {hi}]")
    return value


@dataclass
class FplApiClient:
    fetcher: HttpFetcher
    base_url: str = "https://fantasy.premierleague.com/api"

    @classmethod
    def from_config(cls, cfg: dict[str, object]) -> FplApiClient:
        api = cfg["fpl_api"]
        assert isinstance(api, dict)
        fetcher = HttpFetcher(
            HttpConfig(
                allowed_hosts=tuple(api["allowed_hosts"]),
                timeout_seconds=float(api["timeout_seconds"]),
                max_retries=int(api["max_retries"]),
                backoff_initial_seconds=float(api["backoff_initial_seconds"]),
                backoff_max_seconds=float(api["backoff_max_seconds"]),
                min_seconds_between_requests=float(api["min_seconds_between_requests"]),
                user_agent=str(api["user_agent"]),
            )
        )
        return cls(fetcher=fetcher, base_url=str(api["base_url"]))

    def _get(self, path: str, resource: str, model: type[M]) -> tuple[RawPayload, M]:
        res = self.fetcher.get(f"{self.base_url}/{path}")
        payload = RawPayload(
            source=SOURCE_NAME,
            resource=resource,
            url=res.url,
            retrieved_at=res.retrieved_at,
            content=res.content,
            content_type="application/json",
            source_version=None,
            schema_version=SCHEMA_VERSION,
        )
        return payload, model.model_validate(json.loads(res.content))

    def bootstrap_static(self) -> tuple[RawPayload, s.BootstrapStatic]:
        return self._get("bootstrap-static/", "bootstrap-static", s.BootstrapStatic)

    def fixtures(self) -> tuple[RawPayload, list[s.ApiFixture]]:
        res = self.fetcher.get(f"{self.base_url}/fixtures/")
        payload = RawPayload(
            SOURCE_NAME,
            "fixtures",
            res.url,
            res.retrieved_at,
            res.content,
            "application/json",
            None,
            SCHEMA_VERSION,
        )
        parsed = TypeAdapter(list[s.ApiFixture]).validate_python(json.loads(res.content))
        return payload, parsed

    def event_live(self, gameweek: int) -> tuple[RawPayload, s.EventLive]:
        gw = _bounded("gameweek", gameweek, 1, 60)
        return self._get(f"event/{gw}/live/", f"event-live/{gw}", s.EventLive)

    def entry(self, entry_id: int) -> tuple[RawPayload, s.ApiEntry]:
        eid = _bounded("entry_id", entry_id, 1, MAX_ENTRY_ID)
        return self._get(f"entry/{eid}/", f"entry/{eid}", s.ApiEntry)

    def entry_history(self, entry_id: int) -> tuple[RawPayload, s.EntryHistory]:
        eid = _bounded("entry_id", entry_id, 1, MAX_ENTRY_ID)
        return self._get(f"entry/{eid}/history/", f"entry/{eid}/history", s.EntryHistory)

    def entry_picks(self, entry_id: int, gameweek: int) -> tuple[RawPayload, s.EntryPicks]:
        eid = _bounded("entry_id", entry_id, 1, MAX_ENTRY_ID)
        gw = _bounded("gameweek", gameweek, 1, 60)
        return self._get(f"entry/{eid}/event/{gw}/picks/", f"entry/{eid}/picks/{gw}", s.EntryPicks)

    def entry_transfers(self, entry_id: int) -> tuple[RawPayload, list[s.ApiTransfer]]:
        eid = _bounded("entry_id", entry_id, 1, MAX_ENTRY_ID)
        res = self.fetcher.get(f"{self.base_url}/entry/{eid}/transfers/")
        payload = RawPayload(
            SOURCE_NAME,
            f"entry/{eid}/transfers",
            res.url,
            res.retrieved_at,
            res.content,
            "application/json",
            None,
            SCHEMA_VERSION,
        )
        return payload, TypeAdapter(list[s.ApiTransfer]).validate_python(json.loads(res.content))

    def league_standings(
        self, league_id: int, page: int = 1
    ) -> tuple[RawPayload, s.LeagueStandings]:
        lid = _bounded("league_id", league_id, 1, MAX_LEAGUE_ID)
        pg = _bounded("page", page, 1, 1000)
        return self._get(
            f"leagues-classic/{lid}/standings/?page_standings={pg}",
            f"league/{lid}/standings/{pg}",
            s.LeagueStandings,
        )
