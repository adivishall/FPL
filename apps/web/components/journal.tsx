"use client";

import Link from "next/link";
import { useState } from "react";

import { Badge, PlayerName } from "@/components/ui";
import { track } from "@/lib/analytics";
import { ApiError, post, type EvaluationStatus, type JournalEntry, type JournalRecordResult } from "@/lib/api";

// Decision journal (M1.4): what Copilot recommended, what you decided, what happened. The
// journal stores the decision-time evidence when you record a decision; the outcome is
// evaluated by the API once the official results are final. The page never computes either.

export function evaluationLabel(s: EvaluationStatus): string {
  return s === "pending" ? "awaiting results" : s === "partial" ? "partial" : s === "final" ? "final" : "unavailable";
}

export function EvaluationBadge({ status, testId }: { status: EvaluationStatus; testId?: string }) {
  // the evaluation's completeness, not a verdict on the decision
  const tone = status === "final" ? "good" : status === "unavailable" ? "warn" : "neutral";
  return (
    <span data-testid={testId ?? "evaluation-status"} data-state={status}>
      <Badge tone={tone}>{evaluationLabel(status)}</Badge>
    </span>
  );
}

export function AdherenceBadge({ adherence }: { adherence: JournalEntry["adherence"] }) {
  if (!adherence) return <span className="muted">—</span>;
  // neutral on purpose: following Copilot is not "right" and changing the plan is not "wrong"
  const label = adherence === "followed" ? "followed Copilot" : adherence === "modified" ? "changed the plan" : adherence === "rejected" ? "did not follow" : "deciding later";
  return <Badge tone="neutral">{label}</Badge>;
}

export function ActionSummary({ e }: { e: JournalEntry }) {
  if (e.action === "DEFER") return <span>decide later</span>;
  if (e.action === "UNKNOWN") return <span className="muted">something else (not recorded)</span>;
  return (
    <span>
      <Badge tone={e.action === "HOLD" ? "neutral" : "good"}>{e.action}</Badge>{" "}
      {e.sells.length ? (
        <>
          {e.sells.map((c) => <PlayerName key={c} code={c} names={e.names} />)} → {e.buys.map((c) => <PlayerName key={c} code={c} names={e.names} />)}
          {e.hit_points ? ` (−${e.hit_points})` : ""}{" "}
        </>
      ) : null}
      {e.captain ? (
        <span className="small">
          C <PlayerName code={e.captain} names={e.names} />
        </span>
      ) : null}
    </span>
  );
}

type Choice = "recommended" | "hold" | "plan" | "defer" | "other";

/** Record a decision against a stored recommendation or one plan of a workbench run. */
export function RecordDecision(props: {
  managerKey: string;
  source: "recommendation" | "workbench";
  sourceId: string;
  plan?: string; // a workbench plan key "<index>:<label>"
  starters?: number[];
  captain?: number | null;
  viceCaptain?: number | null;
  names?: Record<string, string | null>;
  current: boolean;
}) {
  const [captain, setCaptain] = useState<string>("");
  const [vice, setVice] = useState<string>("");
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<JournalRecordResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  async function record(choice: Choice) {
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      // the selectors describe the plan shown here; holding keeps HOLD's own armband (as shown)
      const armband = choice === "recommended" || choice === "plan";
      const r = await post<JournalRecordResult>("/journal", {
        manager_key: props.managerKey,
        source: props.source,
        source_id: props.sourceId,
        choice,
        plan: choice === "plan" ? props.plan : undefined,
        captain: armband && captain ? Number(captain) : undefined,
        vice_captain: armband && vice ? Number(vice) : undefined,
        rationale: rationale.trim() || undefined,
      });
      setMsg(r);
      track("journal_recorded", { kind: choice, source: props.source });
    } catch (e) {
      setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  }
  const starters = props.starters ?? [];
  const label = (c: number) => props.names?.[String(c)] ?? String(c);
  return (
    <div className="record" data-testid="record-decision">
      <strong>Record your decision</strong>
      <p className="small">
        The journal keeps what was known now — this {props.source === "recommendation" ? "recommendation" : "plan"}, its expected points and the
        data it used — and compares it with the official results after the gameweek.
      </p>
      {!props.current ? (
        <p className="error" role="alert" data-testid="record-historical">
          This result is historical: Copilot&apos;s current advice may differ. Regenerate or evaluate again before you decide.
        </p>
      ) : null}
      {starters.length ? (
        <div className="row small">
          <label>
            Captain{" "}
            <select value={captain} onChange={(e) => setCaptain(e.target.value)} data-testid="record-captain">
              <option value="">as planned ({props.captain ? label(props.captain) : "—"})</option>
              {starters.map((c) => <option key={c} value={c}>{label(c)}</option>)}
            </select>
          </label>
          <label>
            Vice{" "}
            <select value={vice} onChange={(e) => setVice(e.target.value)} data-testid="record-vice">
              <option value="">as planned ({props.viceCaptain ? label(props.viceCaptain) : "—"})</option>
              {starters.map((c) => <option key={c} value={c}>{label(c)}</option>)}
            </select>
          </label>
          <span className="muted">applies to this {props.source === "recommendation" ? "plan" : "move"}; &ldquo;I&apos;m holding&rdquo; uses the armband Copilot chose for holding</span>
        </div>
      ) : null}
      <label className="small">
        Why (optional){" "}
        <input value={rationale} maxLength={1000} onChange={(e) => setRationale(e.target.value)} placeholder="e.g. I expect the new signing to start" data-testid="record-rationale" />
      </label>
      <div className="row">
        {props.source === "workbench" ? (
          <button onClick={() => void record("plan")} disabled={busy || !props.plan} data-testid="record-plan">I&apos;m making this move</button>
        ) : (
          <button onClick={() => void record("recommended")} disabled={busy} data-testid="record-recommended">I&apos;m following this</button>
        )}
        <button className="secondary" onClick={() => void record("hold")} disabled={busy} data-testid="record-hold">I&apos;m holding</button>
        <button className="secondary" onClick={() => void record("defer")} disabled={busy} data-testid="record-defer">I&apos;ll decide later</button>
        <button className="secondary" onClick={() => void record("other")} disabled={busy} data-testid="record-other">I did something else</button>
      </div>
      {err ? <p className="error" role="alert">{err}</p> : null}
      {msg ? (
        <p className="small" role="status" data-testid="record-msg">
          {msg.created ? "Recorded in your journal" : "Already in your journal"} ·{" "}
          <Link href={`/journal/entry/${msg.entry.entry_id}`} data-testid="record-link">open the entry</Link>
          {msg.warning ? <span className="error"> {msg.warning}</span> : null}
        </p>
      ) : null}
    </div>
  );
}
