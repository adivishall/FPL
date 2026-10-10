"use client";

import { useEffect, useState } from "react";

import { Pitch } from "@/components/pitch";
import { Card, FreshnessBanner, State, useApi } from "@/components/ui";
import { ApiError, api, post, type ForecastRow, type SquadState } from "@/lib/api";
import { money, pct, pts } from "@/lib/format";
import { effectiveHorizon, loadSettings } from "@/lib/settings";

interface LineupResp {
  lineup: { starters: number[]; bench: number[]; captain: number; vice_captain: number };
  expected_points: number;
  captaincy: { expected: number; safe: number; high_variance: number; options: { player_code: number; mean_total: number; p_captain_haul: number; p_beats_expected_choice: number }[] };
}

export default function SquadPlanner() {
  const [settings] = useState(loadSettings);
  const squad = useApi<SquadState>(`/squad?manager_key=${settings.managerKey}`);
  const [lineup, setLineup] = useState<LineupResp | null>(null);
  const [fixtures, setFixtures] = useState<Record<number, ForecastRow[]>>({});
  const [codes, setCodes] = useState("");
  const [bank, setBank] = useState(0);
  const [ft, setFt] = useState(1);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const st = squad.data?.state;
    if (!st) return;
    setCodes(st.squad.map((p) => p.player_code).join(" "));
    setBank(st.bank);
    setFt(st.free_transfers);
    void post<LineupResp>("/lineup", { manager_key: settings.managerKey }).then(setLineup).catch(() => setLineup(null));
    void effectiveHorizon(settings)
      .then((h) =>
        Promise.all(
          st.squad.map((p) => api<{ gameweeks: ForecastRow[] }>(`/players/${p.player_code}/forecast?horizon=${h}`).then((r) => [p.player_code, r.gameweeks] as const)),
        ),
      )
      .then((rows) => setFixtures(Object.fromEntries(rows)));
  }, [squad.data, settings.managerKey, settings.horizon]);

  async function save() {
    setMsg(null);
    setBusy(true);
    try {
      const picks = codes.split(/[\s,]+/).filter(Boolean).map((c) => ({ player_code: Number(c) }));
      await post("/squad", { manager_key: settings.managerKey, picks, bank, free_transfers: ft });
      setMsg("Squad saved.");
      await squad.reload();
    } catch (e) {
      setMsg(e instanceof ApiError ? `Rejected: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function build() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await post<{ squad: { player_code: number }[]; bank_after: number; optimality: string }>("/optimize/squad", { budget: 1000, horizon: await effectiveHorizon(settings), profile: settings.profile });
      setCodes(r.squad.map((p) => p.player_code).join(" "));
      setBank(r.bank_after);
      setMsg(`Squad proposed by the initial-squad optimiser (${r.optimality}) — review, then save.`);
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
    }
  }

  const st = squad.data?.state;
  const pos = Object.fromEntries((st?.squad ?? []).map((p) => [p.player_code, p.position]));
  const xpNext = (c: number) => fixtures[c]?.[0]?.mean;
  const gws = Array.from(new Set(Object.values(fixtures).flat().map((r) => r.gw))).sort((a, b) => a - b);
  return (
    <>
      <FreshnessBanner f={squad.data?.freshness} />
      <h1>Squad Planner</h1>
      <div className="grid">
        <Card title="Starting XI, bench and armband" testId="lineup">
          <State loading={squad.loading} error={squad.error && !squad.error.startsWith("404") ? squad.error : null} />
          {lineup && st ? (
            <>
              <Pitch
                starters={lineup.lineup.starters.map((c) => ({ code: c, position: pos[c] ?? "", xp: xpNext(c) }))}
                bench={lineup.lineup.bench.map((c) => ({ code: c, position: pos[c] ?? "" }))}
                captain={lineup.lineup.captain}
                vice={lineup.lineup.vice_captain}
                names={squad.data?.names}
              />
              <p className="small">
                Lineup EV {pts(lineup.expected_points)} · captain profiles — expected {squad.data?.names[String(lineup.captaincy.expected)]}, safe{" "}
                {squad.data?.names[String(lineup.captaincy.safe)]}, high-variance {squad.data?.names[String(lineup.captaincy.high_variance)]}
              </p>
            </>
          ) : null}
        </Card>
        <Card title="Enter squad (manual — no FPL credentials needed)" testId="entry">
          <p className="small">15 player codes separated by spaces. Live sync from the FPL API is unavailable in this deployment (see Data Health).</p>
          <textarea aria-label="player codes" value={codes} onChange={(e) => setCodes(e.target.value)} rows={4} style={{ width: "100%" }} />
          <div className="row">
            <label>Bank (tenths) <input type="number" value={bank} onChange={(e) => setBank(Number(e.target.value))} style={{ width: 80 }} /></label>
            <label>Free transfers <input type="number" value={ft} min={0} max={5} onChange={(e) => setFt(Number(e.target.value))} style={{ width: 60 }} /></label>
            <button onClick={save} disabled={busy} data-testid="save-squad">Save squad</button>
            <button className="secondary" onClick={build} disabled={busy} data-testid="build-squad">Build optimal squad</button>
          </div>
          {msg ? <p role="status" data-testid="squad-msg">{msg}</p> : null}
          {st ? <p className="small">Stored: GW{st.gameweek}, bank {money(st.bank)}, {st.free_transfers} free transfer(s), chips available: {st.chips.filter((c) => c.status === "available").map((c) => c.chip_id).join(", ")}</p> : null}
        </Card>
        {st ? (
          <Card title="Fixture & expectation timeline" wide>
            <table>
              <thead><tr><th>Player</th><th>Pos</th>{gws.map((g) => <th key={g}>GW{g}</th>)}</tr></thead>
              <tbody>
                {st.squad.map((p) => (
                  <tr key={p.player_code}>
                    <td>{squad.data?.names[String(p.player_code)] ?? p.player_code}</td>
                    <td>{p.position}</td>
                    {gws.map((g) => {
                      const row = fixtures[p.player_code]?.find((r) => r.gw === g);
                      return <td key={g}>{row ? `${pts(row.mean)} (${pct(row.prob_start)})` : <span className="muted">blank</span>}</td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="small">Cell: expected points (start probability). Blank = no fixture.</p>
          </Card>
        ) : null}
      </div>
    </>
  );
}
