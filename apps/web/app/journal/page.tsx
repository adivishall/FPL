"use client";

import Link from "next/link";
import { useState } from "react";

import { lifecycleLabel, lifecycleTone } from "@/components/lifecycle";
import { Badge, Card, State, confidenceTone, useApi } from "@/components/ui";
import { post, type Lifecycle } from "@/lib/api";
import { pct, signed, when } from "@/lib/format";
import { loadSettings } from "@/lib/settings";

interface Entry {
  id: string;
  gameweek: number;
  action: string;
  status: string;
  state_id?: string | null;
  snapshot_id?: string | null;
  invalidated_at?: string | null;
  invalidation?: { kind: string; detail: string; old: unknown; new: unknown; detected_at: string | null } | null;
  lifecycle: Lifecycle; // the same derived verdict Home and the recommendation pages show
  expected_gain_vs_hold: number;
  confidence: number;
  stability: string | null;
  created_at: string;
  feedback: { followed: string; note: string | null; recorded_at: string }[];
}

export default function Journal() {
  const [settings] = useState(loadSettings);
  const j = useApi<{ journal: Entry[] }>(`/decisions?manager_key=${settings.managerKey}`);
  async function fb(id: string, followed: string) {
    await post(`/decisions/${id}/feedback`, { followed });
    await j.reload();
  }
  return (
    <>
      <h1>Decision Journal</h1>
      <Card testId="journal">
        <State loading={j.loading} error={j.error} empty={j.data && !j.data.journal.length ? "No recommendations yet." : undefined} />
        <table>
          <thead><tr><th>Created</th><th>GW</th><th>Action</th><th>Gain vs hold</th><th>Confidence</th><th>Status</th><th>Feedback</th></tr></thead>
          <tbody>
            {j.data?.journal.map((e) => (
              <tr key={e.id}>
                <td><Link href={`/journal/${e.id}`}>{when(e.created_at)}</Link></td>
                <td>{e.gameweek}</td>
                <td>{e.action}</td>
                <td>{signed(e.expected_gain_vs_hold)}</td>
                <td><Badge tone={confidenceTone(e.confidence)}>{pct(e.confidence)}</Badge></td>
                <td data-testid={`journal-status-${e.id}`} data-state={e.lifecycle.state}>
                  <Badge tone={lifecycleTone(e.lifecycle)}>{lifecycleLabel(e.lifecycle)}</Badge>
                  {e.lifecycle.reasons.map((r, i) => (
                    <div className="small" key={i}>{r.detail}{r.detected_at ? ` (${when(r.detected_at)})` : ""}</div>
                  ))}
                </td>
                <td>
                  {e.feedback[0] ? e.feedback[0].followed : (
                    <span className="row">
                      <button className="secondary" onClick={() => fb(e.id, "followed")}>Followed</button>
                      <button className="secondary" onClick={() => fb(e.id, "ignored")}>Ignored</button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  );
}
