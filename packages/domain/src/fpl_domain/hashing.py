"""Stable content hashing for reproducibility (§49.1, §76.2).

All persisted runs reference inputs by a SHA-256 of a *canonical* JSON encoding: sorted keys,
no insignificant whitespace, floats rendered with ``repr`` precision, datetimes as ISO-8601 UTC.
Two semantically identical inputs therefore always hash identically across processes.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


def _canonical(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return _canonical(obj.model_dump(mode="python"))
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            raise ValueError("naive datetimes are not allowed in hashed payloads")
        return obj.astimezone(UTC).isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return repr(obj)
        return float(repr(obj))
    if isinstance(obj, Mapping):
        return {str(k): _canonical(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted((_canonical(v) for v in obj), key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
        return [_canonical(v) for v in obj]
    if hasattr(obj, "tolist"):  # numpy scalars / arrays without importing numpy
        return _canonical(obj.tolist())
    return obj


def canonical_json(obj: Any) -> str:
    """Deterministic JSON string for ``obj``."""
    return json.dumps(_canonical(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def content_hash(obj: Any, *, length: int = 64) -> str:
    """SHA-256 of the canonical JSON of ``obj`` (optionally truncated)."""
    return sha256_hex(canonical_json(obj))[:length]


def short_id(prefix: str, obj: Any, length: int = 16) -> str:
    """Human-friendly content-derived identifier, e.g. ``snap_3f9a…``."""
    return f"{prefix}_{content_hash(obj, length=length)}"
