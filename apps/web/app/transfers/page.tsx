"use client";

import { useState } from "react";

import { Badge, Card, PlayerName, State, confidenceTone, useApi } from "@/components/ui";
import { ApiError, post, type SquadState } from "@/lib/api";
import { money, pct, signed } from "@/lib/format";
import { effectiveHorizon, loadSettings } from "@/lib/settings";

interface Candidate {
  in: { player_id: number };
  name: string | null;
  action: string;
  expected_gain_1gw: number;
  expected_gain_horizon: number;
  probability_positive: number;
  p10_gain: number;
  p50_gain: number;
  p90_gain: number;
  transfer_cost: number;
  confidence: string;
  price: number;
  objective_gain: number;
  start_probability: number | null;
  selection_reason: string;
  follow_up: { gameweek: number; out: number[]; in: number[]; chip: string | null }[];
  bank_after: number;
  optimality?: string; // "proven optimal", or best found within the solver limit
}

export default function TransferLab() {
  const [settings] = useState(loadSettings);
  const squad = useApi<SquadState>(`/squad?manager_key=${settings.managerKey}`);
  const [out, setOut] = useState<number | null>(null);
  const [res, setRes] = useState<{ candidates: Candidate[]; universe_size: number; notes: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run(code: number) {
    setOut(code);
    setBusy(true);
    setErr(null);
    setRes(null);
    try {
      setRes(await post("/replacements", { manager_key: settings.managerKey, out_player: code, profile: settings.profile, horizon: await effectiveHorizon(settings), candidates: 6 }));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const names = squad.data?.names;
  return (
    <>
      <h1>Transfer Lab</h1>
      <div className="grid">
        <Card title="Choose the outgoing player">
          <State loading={squad.loading} error={squad.error} />
          <ul className="clean">
            {squad.data?.state.squad.map((p) => (
              <li key={p.player_code} className="row">
                <button className={out === p.player_code ? "" : "secondary"} onClick={() => run(p.player_code)} disabled={busy} data-testid={`out-${p.player_code}`}>
                  Replace
                </button>
                <span>{names?.[String(p.player_code)] ?? p.player_code}</span>
                <span className="small">{p.position} · bought {money(p.purchase_price)}</span>
              </li>
            ))}
          </ul>
        </Card>
        <Card title="Replacement candidates (whole-squad effect, paired vs holding)" testId="candidates">
          {busy ? <p role="status">Screening every affordable option, re-optimising the shortlist…</p> : null}
          {err ? <p className="error" role="alert">{err}</p> : null}
          {res ? (
            <>
              <p className="small">{res.universe_size} affordable, club-legal candidates screened.</p>
              <table>
                <thead>
                  <tr><th>In</th><th>Gain 1 GW</th><th>Gain horizon</th><th>p10 / p90</th><th>P&gt;0</th><th>Cost</th><th>Start</th><th>Why shortlisted</th></tr>
                </thead>
                <tbody>
                  {res.candidates.map((c) => (
                    <tr key={c.in.player_id}>
                      <td><PlayerName code={c.in.player_id} names={{ [String(c.in.player_id)]: c.name }} /> <span className="small">{money(c.price)}</span></td>
                      <td>{signed(c.expected_gain_1gw)}</td>
                      <td>{signed(c.expected_gain_horizon)}</td>
                      <td className="small">{signed(c.p10_gain, 0)} / {signed(c.p90_gain, 0)}</td>
                      <td><Badge tone={confidenceTone(c.probability_positive)}>{pct(c.probability_positive)}</Badge></td>
                      <td>{c.transfer_cost ? `−${c.transfer_cost}` : "free"}</td>
                      <td>{pct(c.start_probability)}</td>
                      <td className="small">
                        {c.selection_reason}
                        {c.optimality && c.optimality !== "proven optimal" ? <div><Badge tone="warn">time limit</Badge> plan not proven optimal</div> : null}
                        {c.follow_up.length ? <div>then: {c.follow_up.map((f) => `GW${f.gameweek} ${f.out.join(",")}→${f.in.join(",")}`).join("; ")}</div> : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {res.notes.length ? <p className="small">{res.notes.join(" · ")}</p> : null}
            </>
          ) : !busy ? <p className="muted">Pick a player to see legal replacements.</p> : null}
        </Card>
      </div>
    </>
  );
}
