"""Model registry against PostgreSQL: lineage, status machine, promotion and rollback (§71)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fpl_storage.db import make_engine, session_scope
from fpl_storage.registry import ArtifactStore, ModelRegistry, RegistryError


def _register(reg: ModelRegistry, version: str, payload: dict) -> None:
    reg.register(
        "points",
        version,
        payload,
        feature_version="1.0.0",
        training_window={"from": "2022-23", "to": "2025-26 GW20"},
        owner="tests",
        data_snapshot_id="snap_test",
        params={"n": 1},
    )


def test_registry_lifecycle(fresh_db: str, tmp_path: Path) -> None:
    engine = make_engine(fresh_db)
    store = ArtifactStore(tmp_path)
    with session_scope(engine) as s:
        reg = ModelRegistry(s, store)
        _register(reg, "1.0.0", {"w": [1, 2, 3]})
        _register(reg, "1.1.0", {"w": [4, 5, 6]})
        _register(reg, "1.2.0", {"w": [7]})
        with pytest.raises(RegistryError, match="only approved"):
            reg.promote("points", "1.0.0")
        assert reg.record_gates("points", "1.0.0", {"passed": True}) == "approved"
        assert reg.record_gates("points", "1.1.0", {"passed": True}) == "approved"
        assert reg.record_gates("points", "1.2.0", {"passed": False}) == "rejected"
        with pytest.raises(RegistryError, match="not allowed"):
            reg.record_gates("points", "1.2.0", {"passed": True})
        reg.promote("points", "1.0.0")
        reg.promote("points", "1.1.0")
        prod = reg.production("points")
        assert prod is not None and prod.version == "1.1.0"
        assert reg.get("points", "1.0.0").status == "approved"
        assert reg.load(prod) == {"w": [4, 5, 6]}

        restored = reg.rollback("points")
        assert restored.version == "1.0.0"
        assert reg.get("points", "1.1.0").status == "retired"
        assert [r.status for r in reg.history("points")] == ["production", "retired", "rejected"]
        assert reg.get("points", "1.0.0").metrics_json["gates"] == {"passed": True}


def test_artifacts_are_content_addressed_and_verified(fresh_db: str, tmp_path: Path) -> None:
    engine = make_engine(fresh_db)
    store = ArtifactStore(tmp_path)
    with session_scope(engine) as s:
        reg = ModelRegistry(s, store)
        _register(reg, "2.0.0", {"w": 1})
        row = reg.get("points", "2.0.0")
        path = tmp_path / row.artifact_uri
        assert path.name == f"{row.artifact_sha256}.joblib"
        path.write_bytes(path.read_bytes() + b"tamper")
        with pytest.raises(RegistryError, match="hash mismatch"):
            reg.load(row)
        with pytest.raises(RegistryError, match="nothing in production"):
            reg.rollback("points")
