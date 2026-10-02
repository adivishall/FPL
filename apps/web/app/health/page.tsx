"use client";

import { Badge, Card, FreshnessBanner, State, useApi } from "@/components/ui";
import type { Freshness } from "@/lib/api";

interface Health {
  status: string;
  checks: Record<string, { ok: boolean; note?: string; error?: string; mode?: string; snapshot_id?: string }>;
  freshness: Freshness;
}

interface Gate { name: string; metric: string; value: number | null; target: string; passed: boolean }

export default function DataHealth() {
  const h = useApi<Health>("/health");
  const dq = useApi<{ events: Record<string, unknown>[] }>("/data-quality");
  const models = useApi<Record<string, { promotion_gates?: Record<string, { passed: boolean; checks: Gate[]; config_ref: string }> | { passed: boolean; checks: Gate[]; config_ref: string } } | Record<string, unknown> | null>>("/models");
  const gates = (models.data?.forecast_eval as { promotion_gates?: Record<string, { passed: boolean; checks: Gate[]; config_ref: string }> } | undefined)?.promotion_gates ?? {};
  const price = (models.data?.price_change as { promotion_gates?: { passed: boolean; checks: Gate[]; config_ref: string } } | undefined)?.promotion_gates;
  const allGates = { ...gates, ...(price ? { price_change: price } : {}) };
  return (
    <>
      <FreshnessBanner f={h.data?.freshness} />
      <h1>Data Health</h1>
      <div className="grid">
        <Card title="Services" testId="services">
          <State loading={h.loading} error={h.error} />
          {h.data ? (
            <table>
              <tbody>
                {Object.entries(h.data.checks).map(([k, v]) => (
                  <tr key={k}>
                    <td>{k}</td>
                    <td><Badge tone={v.ok ? "good" : "warn"}>{v.ok ? "ok" : "unavailable"}</Badge></td>
                    <td className="small">{v.note ?? v.error ?? v.mode ?? v.snapshot_id ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </Card>
        <Card title="Model status (promotion gates)" testId="models">
          {Object.entries(allGates).map(([name, g]) => (
            <div key={name} style={{ marginBottom: 8 }}>
              <strong>{name}</strong> <Badge tone={g.passed ? "good" : "bad"}>{g.passed ? "passed" : "failed"}</Badge> <code>{g.config_ref}</code>
              <ul className="clean small">
                {g.checks.map((c) => <li key={c.name}>{c.passed ? "✓" : "✗"} {c.name}: {c.value?.toFixed(4) ?? "—"} ({c.target})</li>)}
              </ul>
            </div>
          ))}
        </Card>
        <Card title="Data-quality incidents" wide>
          <State loading={dq.loading} error={dq.error} empty={dq.data && !dq.data.events.length ? "No incidents recorded in this database (ingestion quality gates run in the data pipeline; see docs/DATA_DICTIONARY.md)." : undefined} />
          <table>
            <tbody>
              {dq.data?.events.map((e, i) => (
                <tr key={i}><td className="small">{JSON.stringify(e)}</td></tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
    </>
  );
}
