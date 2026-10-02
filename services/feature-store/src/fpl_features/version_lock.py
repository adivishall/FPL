"""Pin of the feature-code fingerprint to FEATURE_VERSION (cache-invalidation guard).

Feature snapshots are cached by ``FEATURE_VERSION``. If ``builder.py`` or ``registry.py`` change
without a version bump, cached snapshots would silently be stale. A unit test recomputes the
fingerprint and fails unless this lock is updated together with a version bump.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

_FILES = ("builder.py", "registry.py")
LOCK: dict[str, str] = {
    "1.0.0": "c0d1d06c2a09eab7",
}


def fingerprint() -> str:
    """Hash of the modules' ASTs: formatting/comment changes do not count, code changes do."""
    here = Path(__file__).parent
    h = hashlib.sha256()
    for name in _FILES:
        tree = ast.parse((here / name).read_text(encoding="utf-8"))
        h.update(ast.dump(tree, annotate_fields=False).encode())
    return h.hexdigest()[:16]


if __name__ == "__main__":
    print(fingerprint())
