"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Badge, Card, DistBar, FreshnessBanner, PlayerName, Prob, State, confidenceTone, useApi } from "@/components/ui";
import { ApiError, jobFailureMessage, post, type Gameweek, type Recommendation, type SquadState, waitForJob } from "@/lib/api";
import { pct, pts, signed, when } from "@/lib/format";
import { effectiveHorizon, loadSettings } from "@/lib/settings";

function Countdown({ iso }: { iso: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const ms = new Date(iso).getTime() - now;
  if (ms <= 0) return <span className="muted">deadline passed (historical snapshot)</span>;
  const h = Math.floor(ms / 3.6e6);
  return <span>{h}h {Math.floor((ms % 3.6e6) / 6e4)}m</span>;
}

export default function Overview() {
  const [settings] = useState(loadSettings);
  const gw = useApi<Gameweek>("/gameweeks/current");
  const squad = useApi<SquadState>(`/squad?manager_key=${settings.managerKey}`);
  const rec = useApi<Recommendation>(`/recommendations/current?manager_key=${settings.managerKey}`);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const names = squad.data?.names;

  async function generate() {
    setErr(null);
    setBusy("queued");
    try {
      const r = await post<{ job_id: string }>("/recommendations/generate", {
        manager_key: settings.managerKey, profile: settings.profile, horizon: await effectiveHorizon(settings),
      });
      const job = await waitForJob(r.job_id, (j) => setBusy(j.status));
      if (job.status === "failed") throw new Error(jobFailureMessage(job));
      await rec.reload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const r = rec.data;
  return (
    <>
      <FreshnessBanner f={gw.data?.freshness} />
      <h1>
        {gw.data ? `${gw.data.season} · Gameweek ${gw.data.gameweek}` : "Overview"}
        {gw.data ? <span className="small"> · deadline {when(gw.data.deadline)} (<Countdown iso={gw.data.deadline} />)</span> : null}
      </h1>
      {!squad.data && !squad.loading ? (
        <Card title="Get started">
          <p>No squad stored for <code>{settings.managerKey}</code>.</p>
          <Link href="/squad">Enter or build your squad →</Link>
        </Card>
      ) : null}
      <div className="grid">
        <Card title="Recommended action" testId="decision" wide>
          <State loading={rec.loading} />
          {!r && !rec.loading ? (
            <div className="row">
              <span className="muted">No recommendation for this gameweek yet.</span>
              <button onClick={generate} disabled={!!busy || !squad.data} data-testid="generate">
                {busy ? `Working… (${busy})` : "Generate recommendation"}
              </button>
            </div>
          ) : null}
          {err ? <p className="error" role="alert">{err}</p> : null}
          {r?.stale_state ? (
            <p className="small" role="status" data-testid="stale-state">
              <Badge tone="warn">squad changed</Badge> This plan was computed for a previous version of your squad (state {r.state_id}); it does not describe the squad you have now.{" "}
              <button onClick={generate} disabled={!!busy}>{busy ? `Working… (${busy})` : "Regenerate for the current squad"}</button>
            </p>
          ) : null}
          {r ? (
            <>
              <div className="row">
                <Badge tone={r.decision.action === "HOLD" ? "neutral" : "good"}>{r.decision.action}</Badge>
                {r.decision.stability ? (
                  <Badge tone={r.decision.stability === "stable" ? "good" : "warn"}>{r.decision.stability}</Badge>
                ) : null}
                {r.decision.action === "HOLD" ? (
                  // P(holding beats holding) is 0 by definition: showing it read as "0% confidence"
                  <span className="small" data-testid="confidence">no move cleared the thresholds — see Why</span>
                ) : (
                  <>
                    <span className="small" data-testid="confidence">confidence = P(plan beats holding)</span>
                    <Prob p={r.explanation.confidence.probability_beats_hold} />
                  </>
                )}
                <button className="secondary" onClick={generate} disabled={!!busy}>
                  {busy ? `Re-running… (${busy})` : "Re-run"}
                </button>
              </div>
              <p className="decision" data-testid="decision-text">{r.explanation.decision}</p>
              <div className="kv">
                <span className="muted">This gameweek</span>
                <span>{pts(r.decision.expected_points)} expected squad points (p10 {pts(r.decision.p10, 0)} – p90 {pts(r.decision.p90, 0)})</span>
                <span className="muted">Gain vs holding</span>
                <span>
                  {signed(r.chosen.gain_1gw.mean)} this GW · {signed(r.decision.expected_gain_vs_hold)} over {r.chosen.timeline.length} GWs (80% interval {signed(r.explanation.confidence.gain_p10, 1)} to {signed(r.explanation.confidence.gain_p90, 1)})
                </span>
                <span className="muted">Captain</span>
                <span>
                  <PlayerName code={r.lineup.captain} names={names} /> (vice <PlayerName code={r.lineup.vice_captain} names={names} />)
                </span>
                <span className="muted">Optimiser</span>
                <span data-testid="optimality">
                  {r.decision.optimality && r.decision.optimality !== "proven optimal" ? <Badge tone="warn">time limit</Badge> : null}{" "}
                  {r.decision.optimality ?? "not recorded"}
                </span>
              </div>
            </>
          ) : null}
        </Card>
        {r ? (
          <>
            <Card title="Why" testId="why">
              {r.explanation.primary_drivers.length ? (
                <ul className="clean">
                  {r.explanation.primary_drivers.map((d) => (
                    <li key={d.evidence_ids.join()}>{d.text} <code>{d.evidence_ids.join(", ")}</code></li>
                  ))}
                </ul>
              ) : (
                <ul className="clean">{r.explanation.refusal_reasons.map((x) => <li key={x}>{x}</li>)}</ul>
              )}
              {r.explanation.constraints_binding.length ? (
                <>
                  <h2 style={{ marginTop: 12 }}>Binding constraints</h2>
                  <ul className="clean">{r.explanation.constraints_binding.map((c) => <li key={c}>{c}</li>)}</ul>
                </>
              ) : null}
            </Card>
            <Card title="What could go wrong" testId="downside">
              <ul className="clean">
                {r.explanation.downside_scenarios.map((d) => (
                  <li key={d.scenario}>
                    <strong>{d.scenario}</strong>: {d.description} →{" "}
                    {d.gain_vs_hold === null ? (d.feasible ? "re-optimised" : "move infeasible") : `${signed(d.gain_vs_hold)} pts vs holding`}
                  </li>
                ))}
              </ul>
            </Card>
            <Card title="If you do nothing">
              <p>Holding: {pts(r.hold.timeline[0]?.expected_points)} expected points this gameweek.</p>
              <h2>Alternatives</h2>
              <table>
                <thead><tr><th>Plan</th><th>Move</th><th>Gain (horizon)</th><th>P&gt;0</th></tr></thead>
                <tbody>
                  {r.alternatives.map((a) => (
                    <tr key={a.label}>
                      <td>{a.label}</td>
                      <td>{a.sells.length ? <>{a.sells.map((c) => <PlayerName key={c} code={c} names={{ ...names, ...r.names }} />)} → {a.buys.map((c) => <PlayerName key={c} code={c} names={r.names} />)}</> : a.action}</td>
                      <td>{signed(a.gain_horizon.mean)}</td>
                      <td>{a.action === "HOLD" ? <span className="muted">—</span> : <Badge tone={confidenceTone(a.gain_horizon.probability_positive)}>{pct(a.gain_horizon.probability_positive)}</Badge>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
            <Card title="Plan (next gameweeks)" wide>
              <div className="timeline">
                {r.chosen.timeline.map((s) => (
                  <div className="step" key={s.gameweek}>
                    <strong>GW{s.gameweek}</strong> <Badge tone="neutral">{s.action}</Badge>
                    <div className="small">{pts(s.expected_points)} xP · Δ {signed(s.expected_delta_vs_hold)}</div>
                    <DistBar p10={s.p10} p90={s.p90} mean={s.expected_points} max={100} />
                    {s.squad_doubles.length ? <div className="small">doubles: {s.squad_doubles.length}</div> : null}
                    {s.squad_blanks.length ? <div className="small">blanks: {s.squad_blanks.length}</div> : null}
                  </div>
                ))}
              </div>
            </Card>
            <Card title="Assumptions & reproducibility" wide>
              <ul className="clean small">{r.explanation.assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
              <p className="small">decision <code>{r.decision_id}</code> · optimiser run <code>{r.optimizer_run_id}</code> · snapshot <code>{r.snapshot_id}</code></p>
            </Card>
          </>
        ) : null}
      </div>
    </>
  );
}
