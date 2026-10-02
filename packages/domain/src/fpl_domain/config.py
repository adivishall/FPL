"""Versioned configuration loading (§83).

Every configuration that affects a recommendation is loaded through ``load_versioned_config``.
The returned object carries the file's declared ``version`` and a content hash; both are
persisted with every model/optimiser/backtest run so the exact configuration is recoverable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field

from fpl_domain.hashing import content_hash
from fpl_domain.rules.loader import config_root

T = TypeVar("T", bound=BaseModel)


class VersionedConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    name: str
    version: str
    content_hash: str = Field(description="SHA-256 of the canonical parsed content")
    path: str
    data: dict[str, Any]

    @property
    def ref(self) -> str:
        """Compact reference stored with runs, e.g. ``optimizer/default@1.2.0#ab12cd34``."""
        return f"{self.kind}/{self.name}@{self.version}#{self.content_hash[:8]}"

    def parse(self, model: type[T]) -> T:
        payload = {k: v for k, v in self.data.items() if k != "version"}
        return model.model_validate(payload)


def load_versioned_config(kind: str, name: str, root: Path | None = None) -> VersionedConfig:
    base = root or config_root()
    path = base / kind / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"config {kind}/{name} not found at {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: config must be a mapping")
    version = data.get("version")
    if not isinstance(version, str):
        raise ValueError(f"{path}: every config must declare a string 'version'")
    return VersionedConfig(
        kind=kind,
        name=name,
        version=version,
        content_hash=content_hash(data),
        path=str(path),
        data=data,
    )


def config_from_dict(kind: str, name: str, data: dict[str, Any]) -> VersionedConfig:
    """Build a VersionedConfig from an in-memory override (e.g. API request customisation)."""
    version = str(data.get("version", "adhoc"))
    return VersionedConfig(
        kind=kind,
        name=name,
        version=version,
        content_hash=content_hash(data),
        path="<memory>",
        data=data,
    )
