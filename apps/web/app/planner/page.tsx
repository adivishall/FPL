"use client";

import { useState } from "react";

import { Badge, Card, PlayerName, State, useApi } from "@/components/ui";
import type { Recommendation, SquadState } from "@/lib/api";
import { money, pct, pts, signed } from "@/lib/format";
import { loadSettings } from "@/lib/settings";

export default function FuturePlanner() {
  const [settings] = useState(loadSettings);
  const rec = useApi<Recommendation>(`/recommendations/current?manager_key=${settings.managerKey}`);
  const squad = useApi<SquadState>(`/squad?manager_key=${settings.managerKey}`);
  const r = rec.data;
  const names = squad.data?.names;
  return (
    <>
      <h1>Future Planner</h1>
      <State loading={rec.loading} error={rec.error} />
      {r ? (
        <div className="grid">
          <Card title="Recommended path" wide testId="path">
            <table>
              <thead><tr><th>GW</th><th>Action</th><th>Moves</th><th>Chip</th><th>xP</th><th>Δ vs hold</th><th>P(Δ&gt;0)</th><th>FT</th><th>Bank</th><th>Captain</th><th>Fixtures</th></tr></thead>
              <tbody>
                {r.chosen.timeline.map((s) => (
                  <tr key={s.gameweek}>
                    <td>GW{s.gameweek}</td>
                    <td><Badge tone="neutral">{s.action}</Badge></td>
                    <td>{s.transfers_out.length ? <>{s.transfers_out.map((c) => <PlayerName key={c} code={c} names={names} />)} → {s.transfers_in.map((c) => <PlayerName key={c} code={c} />)}</> : "—"}</td>
                    <td>{s.chip ?? "—"}</td>
                    <td>{pts(s.expected_points)}</td>
                    <td>{signed(s.expected_delta_vs_hold)}</td>
                    <td>{pct(s.probability_delta_positive)}</td>
                    <td>{s.free_transfers}</td>
                    <td>{money(s.bank_after)}</td>
                    <td><PlayerName code={s.captain} names={names} /></td>
                    <td className="small">{s.squad_doubles.length ? `${s.squad_doubles.length} double` : ""} {s.squad_blanks.length ? `${s.squad_blanks.length} blank` : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {r.stability ? <p className="small">Stability: <strong>{r.stability.label}</strong> — the recommended first move stays near-optimal in {pct(r.stability.share_near_optimal)} of {r.stability.perturbations} forecast perturbations.</p> : null}
          </Card>
          <Card title="Chip windows" wide testId="chips">
            <table>
              <thead><tr><th>Chip</th><th>Window</th><th>Recommendation</th><th>Best GW</th><th>Value now</th><th>Value of waiting</th><th>Reason</th></tr></thead>
              <tbody>
                {r.chips.map((c) => (
                  <tr key={c.chip_id}>
                    <td>{c.chip_id}</td>
                    <td>GW{c.window[0]}–{c.window[1]}</td>
                    <td><Badge tone={c.recommendation === "play now" ? "good" : "neutral"}>{c.recommendation}</Badge></td>
                    <td>{c.best_gameweek ? `GW${c.best_gameweek}` : "—"}</td>
                    <td>{signed(c.value_now, 1)}</td>
                    <td>{signed(c.value_of_waiting, 1)}</td>
                    <td className="small">{c.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
          <Card title="Alternative sequences" wide>
            {r.alternatives.map((a) => (
              <div key={a.label} style={{ marginBottom: 10 }}>
                <strong>{a.label}</strong> <span className="small">objective {signed(a.objective_gain_vs_hold)} vs hold · paired gain {signed(a.gain_horizon.mean)} · P&gt;0 {pct(a.gain_horizon.probability_positive)}</span>
                <div className="timeline">
                  {a.timeline.map((s) => (
                    <div className="step" key={s.gameweek}>
                      GW{s.gameweek}: {s.transfers_out.length ? `${s.transfers_out.join(",")} → ${s.transfers_in.join(",")}` : s.action} {s.chip ?? ""}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </Card>
        </div>
      ) : null}
    </>
  );
}
