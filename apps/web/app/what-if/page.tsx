"use client";

import { useState } from "react";

import { Card, useApi } from "@/components/ui";
import { ApiError, post, type SquadState } from "@/lib/api";
import { pct, pts, signed } from "@/lib/format";
import { loadSettings } from "@/lib/settings";

const KINDS = ["injury_shock", "minutes_downside", "minutes_upside", "team_attack_downside", "fixture_shock", "price_shock", "conservative"] as const;

interface ScenarioResult {
  kind: string;
  hold_points_base: number;
  hold_points_scenario: number;
  move_gain_vs_hold?: { mean: number; p10: number; p90: number; probability_positive: number };
  reoptimized_first_action?: { out: number[]; in: number[] };
  infeasible?: string;
}

export default function WhatIf() {
  const [settings] = useState(loadSettings);
  const squad = useApi<SquadState>(`/squad?manager_key=${settings.managerKey}`);
  const [kind, setKind] = useState<(typeof KINDS)[number]>("injury_shock");
  const [player, setPlayer] = useState<string>("");
  const [magnitude, setMagnitude] = useState(0.5);
  const [sell, setSell] = useState("");
  const [buy, setBuy] = useState("");
  const [res, setRes] = useState<ScenarioResult[] | null>(null);
  const [chip, setChip] = useState({ chip_id: "", gameweek: 0 });
  const [chipRes, setChipRes] = useState<Record<string, unknown> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    setErr(null);
    try {
      const body = {
        manager_key: settings.managerKey,
        horizon: settings.horizon,
        scenarios: [{ kind, players: player ? [Number(player)] : [], magnitude }],
        sells: sell ? [Number(sell)] : [],
        buys: buy ? [Number(buy)] : [],
      };
      setRes((await post<{ scenarios: ScenarioResult[] }>("/what-if", body)).scenarios);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function runChip() {
    setBusy(true);
    setErr(null);
    try {
      setChipRes(await post("/chips/simulate", { manager_key: settings.managerKey, horizon: settings.horizon, ...chip }));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const names = squad.data?.names ?? {};
  const wi = chipRes?.what_if as { gain_mean: number; gain_p10: number; gain_p90: number; probability_positive: number } | undefined;
  return (
    <>
      <h1>What-If Simulator</h1>
      <div className="grid">
        <Card title="Scenario (re-simulated, paired with the base forecast)" testId="scenario">
          <div className="row">
            <select aria-label="scenario kind" value={kind} onChange={(e) => setKind(e.target.value as (typeof KINDS)[number])}>
              {KINDS.map((k) => <option key={k}>{k}</option>)}
            </select>
            <select aria-label="player" value={player} onChange={(e) => setPlayer(e.target.value)}>
              <option value="">(no player)</option>
              {squad.data?.state.squad.map((p) => <option key={p.player_code} value={p.player_code}>{names[String(p.player_code)] ?? p.player_code}</option>)}
            </select>
            <label>magnitude <input type="number" step="0.1" value={magnitude} onChange={(e) => setMagnitude(Number(e.target.value))} style={{ width: 70 }} /></label>
          </div>
          <p className="small">Optional transfer to compare against holding under the scenario:</p>
          <div className="row">
            <input aria-label="sell" placeholder="sell code" value={sell} onChange={(e) => setSell(e.target.value)} />
            <input aria-label="buy" placeholder="buy code" value={buy} onChange={(e) => setBuy(e.target.value)} />
            <button onClick={run} disabled={busy} data-testid="run-scenario">Run</button>
          </div>
          {err ? <p className="error" role="alert">{err}</p> : null}
          {res?.map((r) => (
            <div key={r.kind} data-testid="scenario-result" style={{ marginTop: 8 }}>
              <strong>{r.kind}</strong>: holding scores {pts(r.hold_points_scenario)} vs {pts(r.hold_points_base)} in the base forecast.
              {r.move_gain_vs_hold ? (
                <div>Transfer vs hold under this scenario: {signed(r.move_gain_vs_hold.mean)} (p10 {signed(r.move_gain_vs_hold.p10, 1)}, p90 {signed(r.move_gain_vs_hold.p90, 1)}), P&gt;0 {pct(r.move_gain_vs_hold.probability_positive)}</div>
              ) : null}
              {r.reoptimized_first_action ? <div className="small">Re-optimised first move: {r.reoptimized_first_action.out.join(",") || "hold"} → {r.reoptimized_first_action.in.join(",")}</div> : null}
              {r.infeasible ? <div className="error">{r.infeasible}</div> : null}
            </div>
          ))}
        </Card>
        <Card title="Chip what-if (“what if I use it here?”)">
          <div className="row">
            <input aria-label="chip id" placeholder="e.g. wildcard_1" value={chip.chip_id} onChange={(e) => setChip({ ...chip, chip_id: e.target.value })} />
            <input aria-label="chip gameweek" type="number" placeholder="GW" value={chip.gameweek || ""} onChange={(e) => setChip({ ...chip, gameweek: Number(e.target.value) })} style={{ width: 70 }} />
            <button onClick={runChip} disabled={busy || !chip.chip_id || !chip.gameweek}>Simulate</button>
          </div>
          {wi ? <p>Gain vs the base plan: {signed(wi.gain_mean)} (p10 {signed(wi.gain_p10, 1)}, p90 {signed(wi.gain_p90, 1)}), P&gt;0 {pct(wi.probability_positive)}</p> : null}
        </Card>
      </div>
    </>
  );
}
