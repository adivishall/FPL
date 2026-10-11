"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { ActionSummary, AdherenceBadge, EvaluationBadge, evaluationLabel } from "@/components/journal";
import { lifecycleLabel, lifecycleTone } from "@/components/lifecycle";
import { Badge, Card, State, confidenceTone, useApi } from "@/components/ui";
import type { JournalEntry, Lifecycle } from "@/lib/api";
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
  feedback: { entry_id: string; state: "active" | "superseded"; followed: string | null; note: string | null; recorded_at: string }[];
}

export default function Journal() {
  const [settings] = useState(loadSettings);
  const [gw, setGw] = useState("");
  const [action, setAction] = useState("");
  const [evaluation, setEvaluation] = useState("");
  const [superseded, setSuperseded] = useState(false);
  const qs = new URLSearchParams({ manager_key: settings.managerKey });
  if (gw) qs.set("gameweek", gw);
  if (action) qs.set("action", action);
  if (evaluation) qs.set("evaluation", evaluation);
  if (superseded) qs.set("include_superseded", "true");
  const d = useApi<{ entries: JournalEntry[] }>(`/journal?${qs.toString()}`, [qs.toString()]);
  const j = useApi<{ journal: Entry[] }>(`/decisions?manager_key=${settings.managerKey}`);
  const all = useApi<{ entries: JournalEntry[] }>(`/journal?manager_key=${settings.managerKey}&include_superseded=true`);
  const gameweeks = useMemo(() => [...new Set((all.data?.entries ?? []).map((e) => e.gameweek))].sort((a, b) => b - a), [all.data]);
  // the decision that stands for each recommendation, and any decision ever recorded against it
  // (a later decision — possibly from the Transfer Workbench — may have replaced it)
  const [decidedFor, replacedFor] = useMemo(() => {
    const active = new Map<string, JournalEntry>();
    const replaced = new Map<string, JournalEntry>();
    for (const e of all.data?.entries ?? []) {
      if (e.source.kind !== "recommendation") continue;
      if (e.state === "active") active.set(e.source.id, e);
      else if (!replaced.has(e.source.id)) replaced.set(e.source.id, e);
    }
    return [active, replaced];
  }, [all.data]);
  return (
    <>
      <h1>Decision Journal</h1>
      <p className="small">
        What Copilot recommended, what you decided, and — once the official results are final — how it turned out against what was expected
        when you decided. One gameweek is a single draw from the forecast: a sound decision can lose points and a poor one can gain them.
      </p>
      <Card title="Your decisions" testId="decisions">
        <div className="row small">
          <label>Gameweek <select value={gw} onChange={(e) => setGw(e.target.value)} data-testid="filter-gameweek"><option value="">all</option>{gameweeks.map((g) => <option key={g} value={g}>GW{g}</option>)}</select></label>
          <label>Action <select value={action} onChange={(e) => setAction(e.target.value)} data-testid="filter-action"><option value="">all</option>{["HOLD", "TRANSFER", "HIT", "CHIP", "DEFER", "UNKNOWN"].map((a) => <option key={a} value={a}>{a}</option>)}</select></label>
          <label>Outcome <select value={evaluation} onChange={(e) => setEvaluation(e.target.value)} data-testid="filter-evaluation"><option value="">all</option>{["pending", "partial", "final", "unavailable"].map((a) => <option key={a} value={a}>{a}</option>)}</select></label>
          <label><input type="checkbox" checked={superseded} onChange={(e) => setSuperseded(e.target.checked)} data-testid="filter-superseded" /> include changed minds</label>
        </div>
        <State
          loading={d.loading}
          error={d.error}
          empty={d.data && !d.data.entries.length ? "No decisions recorded yet — record one from the Overview or the Transfer Workbench." : undefined}
        />
        {d.data?.entries.length ? (
          <table>
            <thead><tr><th>GW</th><th>Your decision</th><th>Copilot</th><th>Relation</th><th>Recorded</th><th>Outcome</th><th></th></tr></thead>
            <tbody>
              {d.data.entries.map((e) => {
                const ev = e.latest_evaluation;
                return (
                  <tr key={e.entry_id} data-testid={`decision-${e.entry_id}`} data-state={e.state}>
                    <td>GW{e.gameweek}</td>
                    <td><ActionSummary e={e} />{e.state === "superseded" ? <div className="small muted">replaced by a later decision</div> : null}</td>
                    <td className="small">{e.recommended_action ?? "—"}{e.source.kind === "workbench" ? " (workbench)" : ""}</td>
                    <td><AdherenceBadge adherence={e.adherence} /></td>
                    <td className="small">
                      {when(e.recorded_at)}{" "}
                      {e.prospective === false ? <Badge tone="warn">after the deadline</Badge> : e.prospective ? <Badge tone="neutral">before the deadline</Badge> : null}
                      {e.confirmed_at ? <div>confirmed</div> : null}
                      {e.verification === "matches" ? <div>FPL picks match</div> : e.verification === "differs" ? <div>FPL picks differ</div> : null}
                    </td>
                    <td>
                      {e.state === "superseded" ? (
                        // only the decision that stands is evaluated; a replaced one keeps its last check
                        <span className="small muted" data-testid={`decision-outcome-${e.entry_id}`} data-state="replaced">
                          replaced{ev ? ` · last check: ${evaluationLabel(ev.status)}` : " · not evaluated"}
                        </span>
                      ) : (
                        <EvaluationBadge status={e.evaluation_status} testId={`decision-outcome-${e.entry_id}`} />
                      )}
                      {e.state === "active" && ev && ev.realized_points !== null ? (
                        <div className="small">
                          {ev.realized_points} pts{ev.realized_hold_points !== null ? ` · hold ${ev.realized_hold_points} (${signed(ev.realized_points - ev.realized_hold_points, 0)})` : ""}
                          {ev.detail.provisional ? " · provisional" : ""}
                        </div>
                      ) : null}
                    </td>
                    <td><Link href={`/journal/entry/${e.entry_id}`} data-testid={`open-${e.entry_id}`}>Open</Link></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : null}
      </Card>
      <Card title="Recommendations" testId="journal">
        <State loading={j.loading} error={j.error} empty={j.data && !j.data.journal.length ? "No recommendations yet." : undefined} />
        <table>
          <thead><tr><th>Created</th><th>GW</th><th>Action</th><th>Gain vs hold</th><th>Confidence</th><th>Status</th><th>Your decision</th></tr></thead>
          <tbody>
            {j.data?.journal.map((e) => {
              const decided = decidedFor.get(e.id);
              const replaced = decided ? undefined : replacedFor.get(e.id);
              return (
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
                  <td className="small">
                    {decided ? (
                      <Link href={`/journal/entry/${decided.entry_id}`}><AdherenceBadge adherence={decided.adherence} /></Link>
                    ) : replaced ? (
                      <Link href={`/journal/entry/${replaced.entry_id}`} className="muted">replaced by a later decision</Link>
                    ) : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
    </>
  );
}
