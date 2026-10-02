"""Manager-linked data: export and erasure (§75), and the security/audit trail.

Manager-linked rows are found by the pseudonymous ``manager_key`` (and the public FPL entry id
recorded on synced states). Export returns every such row; erasure deletes them in foreign-key
order inside one transaction and leaves an audit record that stores only a SHA-256 of the key,
so the erasure is provable without retaining the identifier.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import Engine, delete, select

from fpl_storage import models as m
from fpl_storage.db import session_scope


def key_digest(manager_key: str) -> str:
    return hashlib.sha256(manager_key.encode("utf-8")).hexdigest()


def _rows(rows: Any) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = {}
        for c in r.__table__.columns:
            v = getattr(r, c.name)
            d[c.name] = v.isoformat() if hasattr(v, "isoformat") else v
        out.append(d)
    return out


def _linked(s: Any, manager_key: str) -> dict[str, Any]:
    states = s.scalars(
        select(m.ManagerStateRow).where(
            m.ManagerStateRow.state_json["manager_key"].astext == manager_key
        )
    ).all()
    state_ids = [x.id for x in states]
    manager_ids = sorted({x.manager_id for x in states if x.manager_id is not None})
    recs = s.scalars(
        select(m.RecommendationRow).where(
            m.RecommendationRow.payload_json["manager_key"].astext == manager_key
        )
    ).all()
    rec_ids = [x.id for x in recs]
    opt_ids = sorted({x.optimization_run_id for x in recs if x.optimization_run_id})
    return {
        "states": states,
        "state_ids": state_ids,
        "manager_ids": manager_ids,
        "recs": recs,
        "rec_ids": rec_ids,
        "opt_ids": opt_ids,
    }


def export_manager(engine: Engine, manager_key: str) -> dict[str, Any]:
    with session_scope(engine) as s:
        ln = _linked(s, manager_key)
        sid, rid, mid = ln["state_ids"], ln["rec_ids"], ln["manager_ids"]
        data: dict[str, Any] = {
            "manager_key": manager_key,
            "manager_state": _rows(ln["states"]),
            "manager_squad": _rows(
                s.scalars(select(m.ManagerSquadRow).where(m.ManagerSquadRow.state_id.in_(sid)))
            ),
            "manager_transfers": _rows(
                s.scalars(
                    select(m.ManagerTransferRow).where(m.ManagerTransferRow.manager_id.in_(mid))
                )
            ),
            "manager_chips": _rows(
                s.scalars(select(m.ManagerChipRow).where(m.ManagerChipRow.manager_id.in_(mid)))
            ),
            "manager_settings": _rows(
                s.scalars(
                    select(m.ManagerSettingsRow).where(
                        m.ManagerSettingsRow.manager_key == manager_key
                    )
                )
            ),
            "recommendations": _rows(ln["recs"]),
            "recommendation_actions": _rows(
                s.scalars(
                    select(m.RecommendationActionRow).where(
                        m.RecommendationActionRow.recommendation_id.in_(rid)
                    )
                )
            ),
            "decision_journal": _rows(
                s.scalars(
                    select(m.DecisionJournalRow).where(
                        m.DecisionJournalRow.recommendation_id.in_(rid)
                    )
                )
            ),
            "optimization_runs": _rows(
                s.scalars(
                    select(m.OptimizationRunRow).where(m.OptimizationRunRow.id.in_(ln["opt_ids"]))
                )
            ),
            "notifications": _rows(
                s.scalars(
                    select(m.NotificationRow).where(m.NotificationRow.manager_key == manager_key)
                )
            ),
            "jobs": _rows(
                s.scalars(
                    select(m.JobRow).where(
                        m.JobRow.request_json["manager_key"].astext == manager_key
                    )
                )
            ),
        }
    data["counts"] = {k: len(v) for k, v in data.items() if isinstance(v, list)}
    return data


def delete_manager(engine: Engine, manager_key: str, actor: str | None = None) -> dict[str, int]:
    """Erase every manager-linked row; returns the number of rows deleted per table."""
    counts: dict[str, int] = {}

    def run(name: str, stmt: Any, s: Any) -> None:
        res = s.execute(stmt)
        counts[name] = int(getattr(res, "rowcount", 0) or 0)

    with session_scope(engine) as s:
        ln = _linked(s, manager_key)
        sid, rid, mid = ln["state_ids"], ln["rec_ids"], ln["manager_ids"]
        run(
            "recommendation_actions",
            delete(m.RecommendationActionRow).where(
                m.RecommendationActionRow.recommendation_id.in_(rid)
            ),
            s,
        )
        run(
            "decision_journal",
            delete(m.DecisionJournalRow).where(m.DecisionJournalRow.recommendation_id.in_(rid)),
            s,
        )
        run(
            "recommendations", delete(m.RecommendationRow).where(m.RecommendationRow.id.in_(rid)), s
        )
        # an optimisation run is manager-linked when its input state is; shared runs (another
        # manager's recommendation referencing the same content-addressed run) are kept
        still_used = set(
            s.scalars(
                select(m.RecommendationRow.optimization_run_id).where(
                    m.RecommendationRow.optimization_run_id.in_(ln["opt_ids"])
                )
            ).all()
        )
        own_runs = [o for o in ln["opt_ids"] if o not in still_used]
        run(
            "transfer_candidates",
            delete(m.TransferCandidateRow).where(m.TransferCandidateRow.run_id.in_(own_runs)),
            s,
        )
        run(
            "optimization_runs",
            delete(m.OptimizationRunRow).where(m.OptimizationRunRow.id.in_(own_runs)),
            s,
        )
        run(
            "manager_squad", delete(m.ManagerSquadRow).where(m.ManagerSquadRow.state_id.in_(sid)), s
        )
        run("manager_state", delete(m.ManagerStateRow).where(m.ManagerStateRow.id.in_(sid)), s)
        run(
            "manager_transfers",
            delete(m.ManagerTransferRow).where(m.ManagerTransferRow.manager_id.in_(mid)),
            s,
        )
        run(
            "manager_chips", delete(m.ManagerChipRow).where(m.ManagerChipRow.manager_id.in_(mid)), s
        )
        run(
            "manager_settings",
            delete(m.ManagerSettingsRow).where(m.ManagerSettingsRow.manager_key == manager_key),
            s,
        )
        run(
            "notifications",
            delete(m.NotificationRow).where(m.NotificationRow.manager_key == manager_key),
            s,
        )
        run(
            "jobs",
            delete(m.JobRow).where(m.JobRow.request_json["manager_key"].astext == manager_key),
            s,
        )
        s.add(
            m.AuditLog(
                id="aud_" + uuid.uuid4().hex[:20],
                actor_type="api_key" if actor else "anonymous",
                actor_id=actor,
                action="privacy.delete_manager",
                entity_type="manager",
                entity_id=key_digest(manager_key)[:32],
                details_json={"deleted": counts},
            )
        )
    return counts


def audit(
    engine: Engine | None, action: str, entity: str, actor: str | None, **details: Any
) -> None:
    if engine is None:
        return
    with session_scope(engine) as s:
        s.add(
            m.AuditLog(
                id="aud_" + uuid.uuid4().hex[:20],
                actor_type="api_key" if actor else "anonymous",
                actor_id=actor,
                action=action,
                entity_type="manager",
                entity_id=key_digest(entity)[:32],
                details_json=details,
            )
        )
