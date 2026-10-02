"use client";

import { use } from "react";

import { Card, DistBar, FreshnessBanner, State, useApi } from "@/components/ui";
import type { ForecastRow, Freshness } from "@/lib/api";
import { money, pct, pts, when } from "@/lib/format";

interface Profile {
  player: { web_name: string; position: string; team_code: number; price: number };
  recent_matches: { gw: number; season: string; minutes: number; points: number; goals: number; assists: number; xg: number | null; xa: number | null; bonus: number }[];
  price_history: { observed_at: string; price: number }[];
  live: { status: string; news: string | null; chance_of_playing_next_round: number | null; captured_at: string }[];
  price_risk: {
    engine: { p_rise: number; p_fall: number; model: string } | null;
    official: { price_change_percent: number; captured_at: string; source: string } | null;
  };
  freshness: Freshness;
}

export default function PlayerDetail({ params }: { params: Promise<{ code: string }> }) {
  const { code } = use(params);
  const prof = useApi<Profile>(`/players/${code}`);
  const fc = useApi<{ gameweeks: ForecastRow[]; provenance: Record<string, unknown> }>(`/players/${code}/forecast`);
  const rows = fc.data?.gameweeks ?? [];
  const comps = rows.length ? Object.keys(rows[0] as object).filter((k) => k.startsWith("xp_")) : [];
  const max = Math.max(10, ...rows.map((r) => r.p90));
  return (
    <>
      <FreshnessBanner f={prof.data?.freshness} />
      <h1>{prof.data?.player.web_name ?? code} <span className="small">{prof.data?.player.position} · {money(prof.data?.player.price)}</span></h1>
      <State loading={prof.loading || fc.loading} error={prof.error ?? fc.error} />
      <div className="grid">
        <Card title="Forecast distribution by gameweek" testId="forecast">
          <table>
            <thead><tr><th>GW</th><th>p10 · IQR · mean · p90</th><th>Mean</th><th>P(≥6)</th><th>P(≥10)</th><th>P(start)</th><th>Minutes</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.gw}>
                  <td>GW{r.gw}</td>
                  <td><DistBar p10={r.p10} p25={r.p25} p75={r.p75} p90={r.p90} mean={r.mean} max={max} /></td>
                  <td>{pts(r.mean, 2)}</td>
                  <td>{pct(r.prob_6_plus)}</td>
                  <td>{pct(r.prob_10_plus)}</td>
                  <td>{pct(r.prob_start)}</td>
                  <td>{pts(r.expected_minutes, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="small">Joint Monte Carlo of the decomposed model; run <code>{String(fc.data?.provenance?.prediction_run_id ?? "")}</code>.</p>
        </Card>
        <Card title="Where the points come from (expected, per GW)">
          <table>
            <thead><tr><th>Component</th>{rows.map((r) => <th key={r.gw}>GW{r.gw}</th>)}</tr></thead>
            <tbody>
              {comps.map((c) => (
                <tr key={c}>
                  <td>{c.replace("xp_", "").replaceAll("_", " ")}</td>
                  {rows.map((r) => <td key={r.gw}>{pts(r[c as `xp_${string}`], 2)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card title="Recent matches (known at the cutoff)">
          <table>
            <thead><tr><th>Season GW</th><th>Min</th><th>Pts</th><th>G</th><th>A</th><th>xG</th><th>xA</th><th>Bonus</th></tr></thead>
            <tbody>
              {prof.data?.recent_matches.map((m) => (
                <tr key={`${m.season}-${m.gw}-${m.minutes}`}>
                  <td>{m.season} GW{m.gw}</td><td>{m.minutes}</td><td>{m.points}</td><td>{m.goals}</td><td>{m.assists}</td>
                  <td>{pts(m.xg, 2)}</td><td>{pts(m.xa, 2)}</td><td>{m.bonus}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card title="Price & availability">
          <ul className="clean small">
            {prof.data?.price_history.slice(-8).map((p) => <li key={p.observed_at}>{when(p.observed_at)} · {money(p.price)}</li>)}
          </ul>
          {prof.data?.live.map((l) => (
            <p key={l.captured_at} className="small">Live status <strong>{l.status}</strong> {l.news ? `— ${l.news}` : ""} (captured {when(l.captured_at)})</p>
          ))}
          <div data-testid="price-risk" className="small">
            <strong>Price change before the next deadline</strong>
            <div>
              Engine (calibrated): {prof.data?.price_risk.engine
                ? `rise ${pct(prof.data.price_risk.engine.p_rise)} · fall ${pct(prof.data.price_risk.engine.p_fall)}`
                : "unavailable"}
            </div>
            <div>
              Official predictor: {prof.data?.price_risk.official
                ? `${prof.data.price_risk.official.price_change_percent.toFixed(1)}% (captured ${when(prof.data.price_risk.official.captured_at)})`
                : "not in this snapshot"}
            </div>
            <div className="muted">Shown separately on purpose — the two are never blended.</div>
          </div>
        </Card>
      </div>
    </>
  );
}
