"use client";

import Link from "next/link";
import { use, useState } from "react";

import { ActionSummary, AdherenceBadge, EvaluationBadge, evaluationLabel } from "@/components/journal";
import { LifecycleBadge } from "@/components/lifecycle";
import { Badge, Card, PlayerName, State, useApi } from "@/components/ui";
import { ApiError, post, waitForJob, type JournalEntry, type JournalPlanEvidence, type JournalScore } from "@/lib/api";
import { pts, signed, when } from "@/lib/format";

// One journal decision (M1.4): what you decided, what Copilot recommended and why, the evidence
// as it was when you decided (never updated), and the outcome once the results are final.

function PlanLine({ p, names }: { p: JournalPlanEvidence; names: Record<string, string | null> }) {
  return (
    <span>
      <Badge tone={p.action === "HOLD" ? "neutral" : "good"}>{p.action}</Badge>{" "}
      {p.sells.length ? (
        <>
          {p.sells.map((c) => <PlayerName key={c} code={c} names={names} />)} → {p.buys.map((c) => <PlayerName key={c} code={c} names={names} />)}{" "}
        </>
      ) : null}
      {p.captain ? (
        <span className="small">
          captain <PlayerName code={p.captain} names={names} />
          {p.vice_captain ? <> (vice <PlayerName code={p.vice_captain} names={names} />)</> : null}
        </span>
      ) : null}
      {p.hit_points ? <span className="small"> · hit −{p.hit_points}</span> : null}
    </span>
  );
}

function Expected({ p }: { p: JournalPlanEvidence }) {
  if (!p.expected) return <span className="muted">not recorded</span>;
  const e = p.expected;
  return (
    <span>
      {pts(e.mean)} expected{e.p10 !== null && e.p10 !== undefined && e.p90 !== null && e.p90 !== undefined ? ` (80% range ${pts(e.p10, 0)}–${pts(e.p90, 0)})` : ""}
      {p.gain_vs_hold ? ` · vs hold ${signed(p.gain_vs_hold.mean, 1)} (${signed(p.gain_vs_hold.p10, 0)} to ${signed(p.gain_vs_hold.p90, 0)}; P>0 ${Math.round(100 * p.gain_vs_hold.probability_positive)}%)` : ""}
    </span>
  );
}

function Score({ s, label }: { s: JournalScore | null | undefined; label: string }) {
  return (
    <tr>
      <td>{label}</td>
      <td>{s ? <strong>{s.points}</strong> : <span className="muted">—</span>}</td>
      <td className="small">{s ? `${s.gross} before ${s.hit_points} hit pts · armband ×${s.multiplier}${s.auto_subs.length ? ` · ${s.auto_subs.length} auto-sub(s)` : ""}` : "not scored"}</td>
    </tr>
  );
}

export default function JournalEntryPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const e = useApi<JournalEntry>(`/journal/${id}`, [id]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  async function evaluate() {
    setBusy("queued");
    setErr(null);
    try {
      const r = await post<{ job_id: string }>(`/journal/${id}/evaluate`, {});
      const job = await waitForJob(r.job_id, (j) => setBusy(j.status));
      if (job.status === "failed") throw new Error(job.error ?? "the evaluation failed");
      await e.reload();
    } catch (x) {
      setErr(x instanceof ApiError ? `${x.status}: ${x.message}` : String(x));
    } finally {
      setBusy(null);
    }
  }
  async function confirm() {
    setErr(null);
    try {
      await post(`/journal/${id}/confirm`, {});
      await e.reload();
    } catch (x) {
      setErr(x instanceof ApiError ? `${x.status}: ${x.message}` : String(x));
    }
  }
  const d = e.data;
  // a decision migrated from pre-M1.4 feedback has no decision-time evidence (null)
  const ev = d?.evidence ?? null;
  const active = d?.state === "active";
  const names = d?.names ?? {};
  const last = d?.evaluations?.[0] ?? null;
  // a replaced decision is not evaluated further: its last check is shown only when it had
  // settled (final or unavailable), never as an outcome still to come
  const latest = d?.state === "active" || (last && (last.status === "final" || last.status === "unavailable")) ? last : null;
  return (
    <>
      <h1>Decision · GW{d?.gameweek ?? ""}</h1>
      <State loading={e.loading} error={e.error} />
      {d ? (
        <div className="grid">
          <Card title="Your decision" testId="entry-decision">
            <p data-testid="entry-action"><ActionSummary e={d} /> <AdherenceBadge adherence={d.adherence} /></p>
            {d.state === "superseded" ? (
              <p className="small" data-testid="entry-superseded">
                Replaced by <Link href={`/journal/entry/${d.superseded_by}`}>a later decision</Link>; only the decision that stands is evaluated.
              </p>
            ) : null}
            {d.rationale ? <p className="small">“{d.rationale}”</p> : null}
            {d.self_reported_points !== null && d.self_reported_points !== undefined ? (
              <p className="small" data-testid="entry-self-reported">You reported {d.self_reported_points} points (your own figure; not used by the evaluation).</p>
            ) : null}
            <div className="kv small">
              <span className="muted">Recorded</span>
              <span>
                {when(d.recorded_at)} {d.prospective === false ? <Badge tone="warn">after the deadline</Badge> : d.prospective ? <Badge tone="neutral">before the deadline</Badge> : null}
              </span>
              <span className="muted">You confirmed it</span>
              <span data-testid="entry-confirmed">
                {d.confirmed_at ? `yes, ${when(d.confirmed_at)}` : d.action === "DEFER" || d.action === "UNKNOWN" ? "—" : (
                  <button className="secondary" onClick={() => void confirm()} disabled={d.state !== "active"} data-testid="confirm-decision">I made this move in FPL</button>
                )}
              </span>
              <span className="muted">FPL check</span>
              <span data-testid="verification" data-state={d.verification ?? "none"}>
                {d.verification === "matches"
                  ? "your official FPL picks match this decision"
                  : d.verification === "differs"
                    ? `your official FPL picks differ: ${(d.verification_detail?.differences ?? []).join(", ")}`
                    : d.verification === "observed"
                      ? "no action was recorded; your official picks are shown below"
                      : d.verification === "unavailable"
                        ? d.verification_detail?.reason ?? "not available"
                        : "after the deadline, when the official picks exist"}
              </span>
            </div>
            {d.verification_detail?.actual ? (
              <p className="small" data-testid="verification-actual">
                FPL shows: {d.verification_detail.actual.transfers_out.length ? (
                  <>{d.verification_detail.actual.transfers_out.map((c) => <PlayerName key={c} code={c} names={names} />)} → {d.verification_detail.actual.transfers_in.map((c) => <PlayerName key={c} code={c} names={names} />)}; </>
                ) : "no transfers; "}
                captain {d.verification_detail.actual.captain ? <PlayerName code={d.verification_detail.actual.captain} names={names} /> : "—"}
                {d.verification_detail.actual.chip ? `; chip ${d.verification_detail.actual.chip}` : ""}
              </p>
            ) : null}
          </Card>
          {!ev ? (
            <Card title="When you decided" testId="evidence-legacy">
              <p className="small" data-testid="entry-legacy">
                Recorded before the journal kept decision-time evidence: Copilot&apos;s recommendation is still linked below, but the
                plans and expectations as they were when you decided were not stored, so the outcome cannot be compared with them.
              </p>
              <p className="small">
                Source: {d.source.kind === "recommendation" ? <Link href={`/journal/${d.source.id}`}>recommendation {d.source.id} (trace)</Link> : <>workbench run {d.source.id}</>}
                {d.recommended_action ? <> · recommended <strong>{d.recommended_action}</strong></> : null}
              </p>
            </Card>
          ) : null}
          {ev ? (
          <Card title="What Copilot recommended" testId="entry-recommended">
            <p><PlanLine p={ev.recommended} names={names} /></p>
            <p className="small">{ev.system_rationale.decision}</p>
            {ev.system_rationale.why.length ? (
              <ul className="clean small">{ev.system_rationale.why.map((w, i) => <li key={i}>{w}</li>)}</ul>
            ) : null}
            {ev.system_rationale.downside.length ? (
              <p className="small">What could go wrong: {ev.system_rationale.downside.join(" · ")}</p>
            ) : null}
            {ev.system_rationale.optimality ? <p className="small">Optimiser: {ev.system_rationale.optimality}</p> : null}
            <p className="small" data-testid="entry-source">
              Source: {d.source.kind === "recommendation" ? (
                <Link href={`/journal/${d.source.id}`}>recommendation {d.source.id} (trace)</Link>
              ) : (
                <>workbench run {d.source.id}</>
              )}
              {" · when you decided it was "}<strong>{ev.source_state === "current" ? "current" : "historical"}</strong>
            </p>
            {d.source_now ? (
              <div data-testid="entry-source-now" data-state={d.source_now.state}>
                <span className="small">Now: </span>
                <LifecycleBadge lc={d.source_now} testId="entry-source-lifecycle" />
              </div>
            ) : <p className="small muted">The source is no longer stored; this record keeps what it said.</p>}
          </Card>
          ) : null}
          {ev ? (
          <Card title="When you decided" testId="evidence" wide>
            <div className="kv small">
              <span className="muted">Your plan</span><span>{ev.chosen ? <><PlanLine p={ev.chosen} names={names} /> · <Expected p={ev.chosen} /></> : "no plan recorded"}</span>
              <span className="muted">Holding</span><span><Expected p={ev.hold} />{ev.hold.lineup_source === "reconstructed" ? ` · XI rebuilt from decision-time expectations${ev.hold.armband_as_stored === false ? " (with its own armband: the stored captain was not in it)" : ""}` : ""}</span>
              <span className="muted">Recommended</span><span><Expected p={ev.recommended} /></span>
              <span className="muted">Data</span><span>snapshot {ev.data_snapshot_id ?? "—"}{ev.data_as_of ? ` · source data as of ${when(ev.data_as_of)}` : ""}{ev.cutoff ? ` · decision cutoff ${when(ev.cutoff)}` : ""}</span>
              <span className="muted">Forecast</span><span>{ev.prediction_run_id ?? "—"}{ev.forecast_key ? ` · ${ev.forecast_key}` : ""}</span>
              <span className="muted">Deadline</span><span>{ev.deadline_at ? when(ev.deadline_at) : "—"}</span>
            </div>
            <p className="small muted">These are the expectations stored when you recorded the decision; newer forecasts never replace them.</p>
          </Card>
          ) : null}
          <Card title="Outcome" testId="outcome" wide>
            <p>
              {active ? (
                <EvaluationBadge status={d.evaluation_status} testId="outcome-status" />
              ) : (
                <span className="small muted" data-testid="outcome-status" data-state="replaced">
                  Replaced by a later decision: only the decision that stands is evaluated
                  {last && !latest ? ` (last check ${when(last.evaluated_at)}: ${evaluationLabel(last.status)})` : ""}.
                </span>
              )}{" "}
              {active && ev ? (
                <button className="secondary" onClick={() => void evaluate()} disabled={!!busy} data-testid="evaluate-now">
                  {busy ? `Evaluating… (${busy})` : d.evaluations?.length ? "Check again" : "Evaluate now"}
                </button>
              ) : null}
            </p>
            {err ? <p className="error" role="alert">{err}</p> : null}
            {!last ? (
              <p className="small muted" data-testid="outcome-none">
                {!ev
                  ? "Not evaluated: there is no decision-time evidence to compare the outcome with."
                  : active
                    ? "Not evaluated yet: the results are checked every hour, or now with the button."
                    : "Not evaluated: a later decision replaced this one before it was."}
              </p>
            ) : null}
            {latest?.detail.reason ? <p className="small" data-testid="outcome-reason">{latest.detail.reason}</p> : null}
            {latest && (latest.detail.chosen || latest.detail.hold || latest.detail.recommended) ? (
              <table data-testid="outcome-scores">
                <thead><tr><th></th><th>Points</th><th></th></tr></thead>
                <tbody>
                  <Score s={latest.detail.chosen} label="Your decision" />
                  <Score s={latest.detail.hold} label="Holding" />
                  <Score s={latest.detail.recommended} label="Copilot's plan" />
                </tbody>
              </table>
            ) : null}
            {latest?.detail.statements?.length ? (
              <ul className="clean" data-testid="outcome-statements">{latest.detail.statements.map((t, i) => <li key={i}>{t}</li>)}</ul>
            ) : null}
            {latest?.detail.caveat ? <p className="small muted" data-testid="outcome-caveat">{latest.detail.caveat}</p> : null}
            {latest?.detail.players?.length ? (
              <details>
                <summary>Players</summary>
                <table className="small">
                  <thead><tr><th>Player</th><th>Role</th><th>Min</th><th>Pts</th><th>Counted</th><th>xP when you decided</th></tr></thead>
                  <tbody>
                    {latest.detail.players.map((p) => (
                      <tr key={p.player_code}>
                        <td><PlayerName code={p.player_code} names={names} /></td>
                        <td>{p.role}</td><td>{p.minutes}</td><td>{p.points}</td><td>{p.counted ? "yes" : ""}</td>
                        <td>{p.xp_next !== null && p.xp_next !== undefined ? pts(p.xp_next) : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </details>
            ) : null}
            {d.evaluations && d.evaluations.length > 1 ? (
              <details data-testid="history">
                <summary>Evaluation history ({d.evaluations.length})</summary>
                <ul className="clean small">
                  {d.evaluations.map((x) => (
                    <li key={x.id}>
                      {when(x.evaluated_at)} · {x.status}
                      {x.realized_points !== null ? ` · ${x.realized_points} pts` : ""}
                      {x.revises_id ? ` · revises #${x.revises_id} (official results changed)` : ""} · snapshot {x.data_snapshot_id ?? "—"}
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </Card>
        </div>
      ) : null}
    </>
  );
}
