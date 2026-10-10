"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { useUser } from "@/components/auth";
import { Badge, Card, DistBar, FreshnessBanner, PlayerName, Prob, State, confidenceTone, useApi } from "@/components/ui";
import { track, trackReturnVisit } from "@/lib/analytics";
import { ApiError, jobFailureMessage, post, type HomeData, type HomePlayer, type Recommendation, type SquadState, type SyncResult, waitForJob } from "@/lib/api";
import { money, pct, pts, signed, when } from "@/lib/format";
import { effectiveHorizon, loadSettings } from "@/lib/settings";

// Copilot Home (M1.1a): "what should I look at first, and what can I do about it?" — situation,
// squad health, the recommended action, captain profiles, the squad and its fixtures, with the
// data's freshness and every number's source. Heavy work is precomputed (/copilot/home).

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

const SEVERITY: Record<string, "bad" | "warn" | "neutral"> = { bad: "bad", warn: "warn", info: "neutral" };
const STATUS: Record<string, string> = { a: "available", d: "doubtful", i: "injured", s: "suspended", u: "unavailable", n: "not in squad" };

function fdrClass(d: number | null): string {
  if (d === null) return "";
  return d >= 4 ? "fdr-hard" : d <= 2 ? "fdr-easy" : "fdr-mid";
}

export default function Home() {
  const [settings] = useState(loadSettings);
  const { user } = useUser();
  const key = settings.managerKey;
  const home = useApi<HomeData>(key ? `/copilot/home?manager_key=${key}` : null, [key]);
  const squad = useApi<SquadState>(key ? `/squad?manager_key=${key}` : null, [key]);
  const rec = useApi<Recommendation>(key ? `/recommendations/current?manager_key=${key}` : null, [key]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [refreshMsg, setRefreshMsg] = useState<string | null>(null);
  const names = squad.data?.names;
  const tracked = useRef<string | null>(null);

  useEffect(() => {
    trackReturnVisit();
  }, []);
  const computedAt = home.data?.analysis?.computed_at;
  const gameweek = home.data?.gameweek;
  useEffect(() => {
    if (computedAt && gameweek) track("squad_analysis_viewed", { gameweek });
  }, [computedAt, gameweek]);
  useEffect(() => {
    if (rec.data && tracked.current !== rec.data.id) {
      tracked.current = rec.data.id;
      track("recommendation_opened", { action: rec.data.decision.action });
    }
  }, [rec.data]);

  // a pending analysis (queued in the worker) is polled until it arrives
  const pending = home.data?.pending ?? false;
  const reloadHome = home.reload;
  useEffect(() => {
    if (!pending) return;
    const t = setTimeout(() => void reloadHome(), 5000);
    return () => clearTimeout(t);
  }, [pending, reloadHome]);

  async function generate() {
    setErr(null);
    setBusy("queued");
    try {
      const r = await post<{ job_id: string }>("/recommendations/generate", {
        manager_key: key, profile: settings.profile, horizon: await effectiveHorizon(settings),
      });
      const job = await waitForJob(r.job_id, (j) => setBusy(j.status));
      if (job.status === "failed") throw new Error(jobFailureMessage(job));
      await rec.reload();
      await home.reload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function refreshFromFpl() {
    const id = home.data?.analysis?.state.manager_id ?? squad.data?.state.manager_id;
    if (!id) return;
    setBusy("refreshing");
    setRefreshMsg(null);
    try {
      const r = await post<SyncResult>("/squad/sync", { manager_key: key, manager_id: id });
      setRefreshMsg(`Refreshed from the FPL API: ${r.state.squad.length} players, bank ${money(r.state.bank)}.`);
      await Promise.all([squad.reload(), home.reload(), rec.reload()]);
    } catch (e) {
      setRefreshMsg(e instanceof ApiError ? `Refresh failed (${e.status}): ${e.message}` : String(e));
    } finally {
      setBusy(null);
    }
  }

  const h = home.data;
  const a = h?.analysis ?? null;
  const r = rec.data;
  const noSquad = !!h && h.state_id === null;
  const captainOf = (code: number): HomePlayer | undefined => a?.players.find((p) => p.player_code === code);
  const gws = a ? Array.from(new Set(a.players.flatMap((p) => p.fixtures.map((f) => f.gw)))).sort((x, y) => x - y) : [];
  const teams = a ? Array.from(new Map(a.players.map((p) => [p.team_code, p.team])).entries()).sort((x, y) => x[1].localeCompare(y[1])) : [];

  return (
    <>
      <FreshnessBanner f={h?.freshness} />
      <h1>
        {h ? `${h.season} · Gameweek ${h.gameweek}` : "Copilot Home"}
        {h ? <span className="small"> · deadline {when(h.deadline)} (<Countdown iso={h.deadline} />)</span> : null}
      </h1>
      <State loading={home.loading && !h} error={home.error && !home.error.startsWith("404") ? home.error : null} />
      {!key || noSquad ? (
        <Card title="Get started" testId="get-started">
          <p>No squad stored for <code>{key || "this account"}</code>{user ? ` (${user.email})` : ""}.</p>
          <p>
            <Link href="/onboarding"><strong>Import your team with your FPL ID →</strong></Link>
            <span className="small"> · or <Link href="/squad">enter it manually</Link></span>
          </p>
        </Card>
      ) : null}
      {a ? (
        <div className="grid">
          <Card title="Situation" testId="situation">
            <div className="kv">
              <span className="muted">Squad</span>
              <span>
                {a.state.manager_id ? <>imported from the FPL API (entry {a.state.manager_id})</> : "entered manually"} · value {money(a.state.squad_value)} · bank {money(a.state.bank)} · {a.state.free_transfers} free transfer(s)
                {a.state.manager_id ? (
                  <>
                    {" "}
                    <button className="secondary" onClick={refreshFromFpl} disabled={!!busy} data-testid="home-refresh-fpl">Refresh from FPL</button>
                  </>
                ) : null}
              </span>
              <span className="muted">Chips</span>
              <span>{a.state.chips.filter((c) => c.status === "available").map((c) => c.chip_id).join(", ") || "none available"}</span>
              <span className="muted">This gameweek</span>
              <span>XI expected {pts(a.totals.xi_xp_next)} pts · bench {pts(a.totals.bench_xp_next)} pts (forecast)</span>
              <span className="muted">Analysis</span>
              <span data-testid="analysis-status">
                computed {when(a.computed_at)} on snapshot <code>{a.snapshot_id}</code>
                {h?.pending ? <> · <Badge tone="warn">updating</Badge> a newer analysis is being computed</> : null}
                {h?.stale && !h.pending ? <> · <Badge tone="warn">stale</Badge> for a previous squad or snapshot</> : null}
              </span>
            </div>
            {refreshMsg ? <p className="small" role="status" data-testid="home-refresh-msg">{refreshMsg}</p> : null}
          </Card>
          <Card title="Squad health" testId="health">
            {a.health.length ? (
              <ul className="clean">
                {a.health.map((s, i) => (
                  <li key={i} data-kind={s.kind}>
                    <Badge tone={SEVERITY[s.severity] ?? "neutral"}>{s.kind}</Badge> {s.message}{" "}
                    <span className="small muted">source: {s.source}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">No signals: everyone available, fixtures ordinary, bench covered.</p>
            )}
          </Card>
        </div>
      ) : null}
      {h?.pending && !a ? (
        <Card title="Analysing your squad" testId="analysis-pending">
          <p className="muted" role="status">The first analysis of this squad is being computed in the background; this page refreshes itself.</p>
        </Card>
      ) : null}
      <div className="grid">
        {key && !noSquad ? (
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
        ) : null}
        {a ? (
          <Card title="Captain profiles" testId="captain-profiles">
            <table>
              <thead><tr><th>Profile</th><th>Player</th><th>xP as captain</th><th>P(start)</th><th>P(haul ≥ 13)</th><th>P(beats expected pick)</th></tr></thead>
              <tbody>
                {([["expected", a.captaincy.expected], ["safe", a.captaincy.safe], ["high-variance", a.captaincy.high_variance]] as [string, number][]).map(([label, code]) => {
                  const opt = a.captaincy.options.find((o) => o.player_code === code);
                  const p = captainOf(code);
                  return (
                    <tr key={label}>
                      <td>{label}</td>
                      <td><PlayerName code={code} names={names} /></td>
                      <td>{opt ? pts(opt.mean_total) : "—"}</td>
                      <td>{p ? pct(p.p_start_next) : "—"}</td>
                      <td>{opt ? pct(opt.p_captain_haul) : "—"}</td>
                      <td>{opt ? pct(opt.p_beats_expected_choice) : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="small muted">Source: {a.sources.lineup}.</p>
          </Card>
        ) : null}
        {a ? (
          <Card title="Your squad" testId="home-squad" wide>
            <table>
              <thead><tr><th>Player</th><th>Pos</th><th>Club</th><th>Price</th><th>xP next (p10–p90)</th><th>P(start)</th><th>Status</th><th>Next fixtures</th></tr></thead>
              <tbody>
                {a.players.map((p) => (
                  <tr key={p.player_code} className={p.starter ? "" : "muted"} data-testid={`home-player-${p.player_code}`}>
                    <td><PlayerName code={p.player_code} names={names} />{p.starter ? "" : ` (bench ${p.bench_slot})`}</td>
                    <td>{p.position}</td>
                    <td>{p.team}</td>
                    <td>{money(p.price)}{p.price !== p.purchase_price ? <span className="small muted"> (bought {money(p.purchase_price)})</span> : null}</td>
                    <td>{pts(p.xp_next)} <DistBar p10={p.p10_next} p90={p.p90_next} mean={p.xp_next} max={15} /></td>
                    <td>{pct(p.p_start_next)}</td>
                    <td>
                      {p.status !== "a" || (p.chance_of_playing !== null && p.chance_of_playing < 100) ? (
                        <Badge tone={p.status === "a" || p.status === "d" ? "warn" : "bad"}>{STATUS[p.status] ?? p.status}{p.chance_of_playing !== null ? ` ${p.chance_of_playing}%` : ""}</Badge>
                      ) : (
                        <span className="small muted">available</span>
                      )}
                      {p.news ? <div className="small">{p.news}</div> : null}
                    </td>
                    <td>
                      {p.fixtures.length === 0 ? <span className="muted">blank</span> : p.fixtures.slice(0, 3).map((f, i) => (
                        <span key={i} className={`fdr ${fdrClass(f.difficulty)}`} title={`GW${f.gw} ${f.home ? "home" : "away"} v ${f.opponent}, FPL difficulty ${f.difficulty ?? "?"}`}>
                          GW{f.gw} {f.opponent} ({f.home ? "H" : "A"}){f.difficulty !== null ? ` ${f.difficulty}` : ""}
                        </span>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="small muted">Sources: forecast — {a.sources.forecast}; status — {a.sources.status}; fixtures — {a.sources.fixtures}.</p>
          </Card>
        ) : null}
        {a && gws.length ? (
          <Card title="Fixture outlook (FPL difficulty)" testId="fixture-outlook" wide>
            <table>
              <thead><tr><th>Club</th>{gws.map((g) => <th key={g}>GW{g}</th>)}</tr></thead>
              <tbody>
                {teams.map(([code, team]) => {
                  const fx = a.players.find((p) => p.team_code === code)?.fixtures ?? [];
                  return (
                    <tr key={code}>
                      <td>{team} <span className="small muted">({a.players.filter((p) => p.team_code === code).length})</span></td>
                      {gws.map((g) => {
                        const inGw = fx.filter((f) => f.gw === g);
                        return (
                          <td key={g}>
                            {inGw.length === 0 ? <span className="muted">—</span> : inGw.map((f, i) => (
                              <span key={i} className={`fdr ${fdrClass(f.difficulty)}`}>{f.opponent} ({f.home ? "H" : "A"}){f.difficulty !== null ? ` ${f.difficulty}` : ""}</span>
                            ))}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="small muted">Source: {a.sources.fixtures}. Colour: 1–2 easy, 3 medium, 4–5 hard, as rated by FPL.</p>
          </Card>
        ) : null}
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
                  {r.alternatives.map((alt) => (
                    <tr key={alt.label}>
                      <td>{alt.label}</td>
                      <td>{alt.sells.length ? <>{alt.sells.map((c) => <PlayerName key={c} code={c} names={{ ...names, ...r.names }} />)} → {alt.buys.map((c) => <PlayerName key={c} code={c} names={r.names} />)}</> : alt.action}</td>
                      <td>{signed(alt.gain_horizon.mean)}</td>
                      <td>{alt.action === "HOLD" ? <span className="muted">—</span> : <Badge tone={confidenceTone(alt.gain_horizon.probability_positive)}>{pct(alt.gain_horizon.probability_positive)}</Badge>}</td>
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
              <ul className="clean small">{r.explanation.assumptions.map((x) => <li key={x}>{x}</li>)}</ul>
              <p className="small">decision <code>{r.decision_id}</code> · optimiser run <code>{r.optimizer_run_id}</code> · snapshot <code>{r.snapshot_id}</code></p>
            </Card>
          </>
        ) : null}
      </div>
    </>
  );
}
