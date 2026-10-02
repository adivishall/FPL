"""Content-addressed, write-once store for raw source payloads (§6.1, §55.1 'Raw capture').

Payloads are stored unchanged (gzip-compressed on disk) under
``<root>/<source>/<sha[:2]>/<sha>.gz``. Writing the same bytes twice is a no-op, so ingestion
is idempotent at the raw layer and any canonical row can be traced back to exact source bytes.
"""

from __future__ import annotations

import gzip
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fpl_domain.hashing import sha256_hex


@dataclass(frozen=True, slots=True)
class RawPayload:
    source: str
    resource: str  # logical name, e.g. "2025-26/merged_gw" or "bootstrap-static"
    url: str
    retrieved_at: datetime
    content: bytes
    content_type: str
    source_version: str | None = None  # e.g. git commit for the historical repository
    schema_version: str = "1"

    @property
    def sha256(self) -> str:
        return sha256_hex(self.content)

    @property
    def snapshot_id(self) -> str:
        """Identity of (source, resource, content): the same bytes under a different logical
        resource are a different snapshot record but share one stored blob."""
        return f"raw_{sha256_hex(f'{self.source}|{self.resource}|{self.sha256}')[:24]}"


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, source: str, sha256: str) -> Path:
        return self.root / source / sha256[:2] / f"{sha256}.gz"

    def put(self, payload: RawPayload) -> Path:
        path = self.path_for(payload.source, payload.sha256)
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        with gzip.open(tmp, "wb", compresslevel=6) as fh:
            fh.write(payload.content)
        os.replace(tmp, path)  # atomic publish; never a partially written file
        return path

    def get(self, source: str, sha256: str) -> bytes:
        path = self.path_for(source, sha256)
        with gzip.open(path, "rb") as fh:
            data = fh.read()
        if sha256_hex(data) != sha256:
            raise OSError(f"raw payload {path} is corrupt (hash mismatch)")
        return data

    def exists(self, source: str, sha256: str) -> bool:
        return self.path_for(source, sha256).exists()
