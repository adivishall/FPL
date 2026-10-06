"use client";

import { useState } from "react";

import { Badge, Card, State, useApi } from "@/components/ui";
import { type Job, jobFailureMessage, type Notification, post, waitForJob } from "@/lib/api";
import { when } from "@/lib/format";
import { loadSettings } from "@/lib/settings";

const TONE = { info: "neutral", warning: "warn", critical: "bad" } as const;

export default function Alerts() {
  const [settings] = useState(loadSettings);
  const key = encodeURIComponent(settings.managerKey);
  const n = useApi<{ notifications: Notification[] }>(`/notifications?manager_key=${key}`);
  const [msg, setMsg] = useState<string | null>(null);
  async function evaluate() {
    setMsg("Evaluating alert rules…");
    try {
      const r = await post<{ job_id: string }>("/notifications/evaluate", { manager_key: settings.managerKey });
      const j: Job = await waitForJob(r.job_id);
      setMsg(j.status === "succeeded" ? `Evaluated (${j.result_ref ?? ""} new/total).` : jobFailureMessage(j));
      await n.reload();
    } catch (e) {
      setMsg(String(e));
    }
  }
  async function read(id: string) {
    await post(`/notifications/read?manager_key=${key}`, { ids: [id] });
    await n.reload();
  }
  const items = n.data?.notifications ?? [];
  return (
    <>
      <h1>Alerts</h1>
      <Card testId="alerts">
        <p className="small">
          Alerts fire only when a change clears a materiality threshold (expected points at stake, or the probability
          that price moves make the plan unaffordable). Repeated evaluations of unchanged information never re-alert.
        </p>
        <button onClick={evaluate} data-testid="evaluate-alerts">Evaluate now</button>{" "}
        {msg ? <span role="status" data-testid="alerts-msg">{msg}</span> : null}
        <State loading={n.loading} error={n.error} empty={n.data && !items.length ? "No alerts — nothing material has changed." : undefined} />
        <ul className="alerts">
          {items.map((a) => (
            <li key={a.id} className={a.read_at ? "read" : ""}>
              <Badge tone={TONE[a.severity]}>{a.severity}</Badge> <strong>{a.title}</strong>{" "}
              <span className="small">{a.kind} · materiality {a.materiality.toFixed(2)} · {when(a.created_at)}</span>
              <p>{a.body}</p>
              {!a.read_at ? <button className="secondary" onClick={() => read(a.id)}>Mark read</button> : null}
            </li>
          ))}
        </ul>
      </Card>
    </>
  );
}
