"""Code provenance for evaluation artifacts (M1.5).

Forecasts record model, feature, ruleset and configuration versions, but not the code that
produced or scored them, and the images carry no git metadata. ``code_digest`` is a content
hash of the installed first-party Python sources: two processes report the same digest exactly
when they run the same code, wherever it was installed from. ``build_revision`` is what the
image build was told (``FPL_BUILD_REVISION``: a git SHA, ``-dirty`` when the tree had
uncommitted changes); it is informational and ``unrecorded`` when not given — never inferred.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from functools import cache
from pathlib import Path

FIRST_PARTY = (
    "fpl_domain",
    "fpl_storage",
    "fpl_features",
    "fpl_forecasting",
    "fpl_simulation",
    "fpl_optimizer",
    "fpl_decision",
    "fpl_backtest",
    "fpl_ingestion",
    "fpl_notifications",
    "fpl_api",
    "fpl_worker",
)


def _package_dir(name: str) -> Path | None:
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


@cache
def code_digest() -> str:
    """``src-<16 hex>``: SHA-256 over every first-party module's relative path and bytes."""
    h = hashlib.sha256()
    for name in FIRST_PARTY:
        root = _package_dir(name)
        if root is None:
            h.update(f"{name}:missing\n".encode())
            continue
        for path in sorted(root.rglob("*.py")):
            h.update(f"{name}/{path.relative_to(root).as_posix()}\n".encode())
            h.update(path.read_bytes())
    return "src-" + h.hexdigest()[:16]


def build_revision() -> str:
    """The revision the image build declared, or ``unrecorded``."""
    return (os.environ.get("FPL_BUILD_REVISION") or "").strip()[:64] or "unrecorded"
