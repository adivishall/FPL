"""Historical dataset adapter (vaastav/Fantasy-Premier-League), pinned to a commit SHA.

Two transports share one interface:
* ``GithubRawTransport`` — fetches ``raw.githubusercontent.com/<owner>/<repo>/<sha>/<path>``.
* ``LocalDirTransport`` — reads the same relative paths from a local directory (offline
  development, committed test excerpts). Payload provenance records which transport was used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from fpl_ingestion.http import HttpConfig, HttpFetcher
from fpl_storage.raw_store import RawPayload

SOURCE_NAME = "vaastav"
FILES = ("merged_gw", "fixtures", "teams", "players_raw")


class Transport(Protocol):
    def read(self, relative_path: str) -> tuple[bytes, str, datetime]:
        """Return (content, url, retrieved_at)."""


@dataclass
class GithubRawTransport:
    base_url: str  # https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League
    commit: str
    fetcher: HttpFetcher

    def read(self, relative_path: str) -> tuple[bytes, str, datetime]:
        url = f"{self.base_url}/{self.commit}/{relative_path}"
        res = self.fetcher.get(url)
        return res.content, url, res.retrieved_at


@dataclass
class LocalDirTransport:
    root: Path

    def read(self, relative_path: str) -> tuple[bytes, str, datetime]:
        path = self.root / relative_path
        return path.read_bytes(), path.resolve().as_uri(), datetime.now(UTC)


@dataclass
class HistoricalRepoSource:
    transport: Transport
    commit: str
    file_templates: dict[str, str]

    @classmethod
    def from_config(
        cls, cfg: dict[str, object], transport: Transport | None = None
    ) -> HistoricalRepoSource:
        repo = cfg["historical_repo"]
        assert isinstance(repo, dict)
        commit = str(repo["commit"])
        if transport is None:
            fetcher = HttpFetcher(
                HttpConfig(
                    allowed_hosts=("raw.githubusercontent.com",),
                    max_retries=5,
                    backoff_initial_seconds=1.0,
                    min_seconds_between_requests=0.2,
                )
            )
            transport = GithubRawTransport(str(repo["base_url"]), commit, fetcher)
        return cls(transport=transport, commit=commit, file_templates=dict(repo["files"]))

    def fetch(self, season: str, file_key: str) -> RawPayload:
        rel = self.file_templates[file_key].format(season=season)
        content, url, retrieved_at = self.transport.read(rel)
        return RawPayload(
            source=SOURCE_NAME,
            resource=f"{season}/{file_key}",
            url=url,
            retrieved_at=retrieved_at,
            content=content,
            content_type="text/csv",
            source_version=self.commit,
            schema_version="vaastav-csv-1",
        )

    def fetch_season(self, season: str) -> dict[str, RawPayload]:
        return {key: self.fetch(season, key) for key in FILES}
