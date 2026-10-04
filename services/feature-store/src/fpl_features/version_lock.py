"""Pin of the feature-code fingerprint to FEATURE_VERSION (cache-invalidation guard).

Feature snapshots are cached by ``FEATURE_VERSION``. If ``builder.py`` or ``registry.py`` — or
the point-in-time rules they read through (``fpl_storage.pit`` / ``fpl_storage.schedule``) —
change without a version bump, cached snapshots would silently be stale. A unit test recomputes
the fingerprint and fails unless this lock is updated together with a version bump.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import fpl_storage

_FILES = ("builder.py", "registry.py")
_PIT_FILES = ("pit.py", "schedule.py")
LOCK: dict[str, str] = {
    "1.0.0": "c0d1d06c2a09eab7",  # builder + registry only
    "1.1.0": "dc8f04f4502bf1db",  # + point-in-time rules; schedule reconstruction (rule S1)
    # 1.2.0: schedule solver short-circuit + node/time limits (performance only: identical
    # reconstructions of every archived season, verified; backtest runs used 1.1.0)
    "1.2.0": "9e48b34967dae610",
}


def fingerprint() -> str:
    """Hash of the modules' ASTs: formatting/comment changes do not count, code changes do."""
    here = Path(__file__).parent
    pit = Path(fpl_storage.__file__).parent
    h = hashlib.sha256()
    for path in [here / n for n in _FILES] + [pit / n for n in _PIT_FILES]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        h.update(ast.dump(tree, annotate_fields=False).encode())
    return h.hexdigest()[:16]


if __name__ == "__main__":
    print(fingerprint())
