"use client";

import { use } from "react";

import { Badge, Card, State, useApi } from "@/components/ui";
import type { Recommendation, Trace } from "@/lib/api";

const STAGES: [string, string][] = [
  ["recommendation", "Recommendation"],
  ["optimization_run", "Optimisation run"],
  ["prediction_run", "Prediction set"],
  ["feature_snapshot", "Feature snapshot"],
  ["data_snapshot", "Canonical data snapshot"],
  ["source_retrieval", "Source retrieval"],
];

function stageSummary(key: string, v: Record<string, unknown>): string {
  const pick = (...ks: string[]) => ks.filter((k) => v[k] !== undefined && v[k] !== null).map((k) => `${k}: ${String(v[k])}`);
  switch (key) {
    case "recommendation":
      return pick("action", "gameweek", "created_at").join(" · ");
    case "optimization_run":
      return pick("solver", "status", "objective_value", "runtime_ms", "objective_version").join(" · ");
    case "prediction_run":
      return pick("cutoff_at", "n_simulations", "simulation_seed", "horizon").join(" · ");
    case "feature_snapshot":
      return pick("feature_version", "cutoff_at", "max_source_available_at", "n_rows").join(" · ");
    case "data_snapshot":
      return pick("as_of", "storage_uri").join(" · ");
    case "source_retrieval":
      return pick("historical_repo", "commit", "licence").join(" · ");
    default:
      return "";
  }
}

export default function JournalEntry({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const rec = useApi<Recommendation>(`/recommendations/${id}`);
  const tr = useApi<Trace>(`/recommendations/${id}/trace`);
  return (
    <>
      <h1>Recommendation {id}</h1>
      <State loading={rec.loading} error={rec.error} />
      {tr.data ? (
        <Card title="Traceability (§76.2): read back from the lineage tables" testId="trace" wide>
          <p>
            <Badge tone={tr.data.complete ? "good" : "bad"}>{tr.data.complete ? "chain complete" : "chain broken"}</Badge>{" "}
            {tr.data.checks.filter((c) => c.ok).length}/{tr.data.checks.length} integrity checks pass
          </p>
          <ol className="trace">
            {STAGES.map(([k, label]) => {
              const v = tr.data?.chain[k];
              if (!v) return null;
              return (
                <li key={k}>
                  <strong>{label}</strong> <code>{String(v.id ?? v.commit ?? "")}</code>
                  <div className="small">{stageSummary(k, v)}</div>
                </li>
              );
            })}
          </ol>
          <details>
            <summary>Integrity checks</summary>
            <ul>
              {tr.data.checks.map((c) => (
                <li key={c.check}>
                  <Badge tone={c.ok ? "good" : "bad"}>{c.ok ? "ok" : "fail"}</Badge> {c.check}{" "}
                  {c.detail ? <span className="small">{c.detail}</span> : null}
                </li>
              ))}
            </ul>
          </details>
        </Card>
      ) : (
        <State loading={tr.loading} error={tr.error} />
      )}
      {rec.data ? (
        <Card title="Explanation (rendered from stored evidence; immutable record)">
          <pre className="md">{rec.data.markdown}</pre>
        </Card>
      ) : null}
    </>
  );
}
