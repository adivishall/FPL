"""Model registry and content-addressed artifact store (§34, §71, ADR-0008).

* Artifacts are serialised with joblib and stored at ``<root>/<model_name>/<sha256>.joblib``; a
  stored file is never overwritten and every load re-verifies the hash.
* Status machine per version::

      candidate ──gates pass──▶ approved ──promote──▶ production
          │                        ▲                      │
          └──gates fail──▶ rejected │                      ├─promote other─▶ approved
                                    └───── rollback ◀──────┘   (current → retired)

  Exactly one ``production`` version per model (enforced by a partial unique index as well).
  Promotion keeps the previous production version as ``approved``, so rollback is a single
  operation that restores it (§71 "Store the previous model so rollback is immediate").
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_storage.models import ModelRegistryRow

ALLOWED = {
    "candidate": {"approved", "rejected"},
    "approved": {"production", "retired"},
    "production": {"approved", "retired"},
    "rejected": set(),
    "retired": set(),
}


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredArtifact:
    uri: str
    sha256: str
    size_bytes: int


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def put(self, model_name: str, obj: Any) -> StoredArtifact:
        buf = io.BytesIO()
        joblib.dump(obj, buf, compress=3)
        data = buf.getvalue()
        sha = hashlib.sha256(data).hexdigest()
        path = self.root / model_name / f"{sha}.joblib"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return StoredArtifact(str(path.relative_to(self.root)), sha, len(data))

    def get(self, uri: str, sha256: str) -> Any:
        data = (self.root / uri).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            raise RegistryError(f"artifact {uri} hash mismatch: {actual} != {sha256}")
        return joblib.load(io.BytesIO(data))


class ModelRegistry:
    def __init__(self, session: Session, store: ArtifactStore) -> None:
        self.session, self.store = session, store

    # ------------------------------------------------------------------ queries
    def get(self, name: str, version: str) -> ModelRegistryRow:
        row = self.session.scalar(
            select(ModelRegistryRow).where(
                ModelRegistryRow.model_name == name, ModelRegistryRow.version == version
            )
        )
        if row is None:
            raise RegistryError(f"{name}@{version} not registered")
        return row

    def production(self, name: str) -> ModelRegistryRow | None:
        return self.session.scalar(
            select(ModelRegistryRow).where(
                ModelRegistryRow.model_name == name, ModelRegistryRow.status == "production"
            )
        )

    def history(self, name: str) -> list[ModelRegistryRow]:
        return list(
            self.session.scalars(
                select(ModelRegistryRow)
                .where(ModelRegistryRow.model_name == name)
                .order_by(ModelRegistryRow.created_at, ModelRegistryRow.id)
            )
        )

    def load(self, row: ModelRegistryRow) -> Any:
        return self.store.get(row.artifact_uri, row.artifact_sha256)

    # ------------------------------------------------------------------ mutations
    def register(
        self,
        name: str,
        version: str,
        obj: Any,
        *,
        feature_version: str,
        training_window: dict[str, Any],
        owner: str,
        ruleset_version: str | None = None,
        data_snapshot_id: str | None = None,
        params: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        code_version: str | None = None,
        parent_version: str | None = None,
    ) -> ModelRegistryRow:
        art = self.store.put(name, obj)
        row = ModelRegistryRow(
            model_name=name,
            version=version,
            artifact_uri=art.uri,
            artifact_sha256=art.sha256,
            feature_version=feature_version,
            ruleset_version=ruleset_version,
            training_window=training_window,
            data_snapshot_id=data_snapshot_id,
            params_json=params or {},
            metrics_json=metrics or {},
            status="candidate",
            owner=owner,
            code_version=code_version,
            parent_version=parent_version,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _transition(self, row: ModelRegistryRow, new: str) -> None:
        if new not in ALLOWED[row.status]:
            raise RegistryError(
                f"{row.model_name}@{row.version}: {row.status} → {new} is not allowed"
            )
        row.status = new

    def record_gates(self, name: str, version: str, gate_report: dict[str, Any]) -> str:
        """Attach a promotion-gate report; candidate → approved (all passed) or rejected."""
        row = self.get(name, version)
        row.metrics_json = {**(row.metrics_json or {}), "gates": gate_report}
        self._transition(row, "approved" if gate_report.get("passed") else "rejected")
        self.session.flush()
        return row.status

    def promote(self, name: str, version: str) -> ModelRegistryRow:
        row = self.get(name, version)
        if row.status != "approved":
            raise RegistryError(f"only approved versions can be promoted ({row.status})")
        current = self.production(name)
        if current is not None:
            self._transition(current, "approved")
            self.session.flush()  # release the one-production index before promoting
        self._transition(row, "production")
        row.promoted_at = datetime.now(UTC)
        self.session.flush()
        return row

    def rollback(self, name: str) -> ModelRegistryRow:
        """Retire the current production version and restore the previously promoted one."""
        current = self.production(name)
        if current is None:
            raise RegistryError(f"{name}: nothing in production to roll back")
        previous = self.session.scalar(
            select(ModelRegistryRow)
            .where(
                ModelRegistryRow.model_name == name,
                ModelRegistryRow.status == "approved",
                ModelRegistryRow.promoted_at.is_not(None),
                ModelRegistryRow.id != current.id,
            )
            .order_by(ModelRegistryRow.promoted_at.desc())
        )
        if previous is None:
            raise RegistryError(f"{name}: no previously promoted version to restore")
        self._transition(current, "retired")
        self.session.flush()
        self._transition(previous, "production")
        previous.promoted_at = datetime.now(UTC)
        self.session.flush()
        return previous
