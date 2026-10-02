"use client";

import { useState } from "react";

import { Card } from "@/components/ui";
import { post } from "@/lib/api";
import { type UiSettings, loadSettings, saveSettings } from "@/lib/settings";

export default function Settings() {
  const [s, setS] = useState<UiSettings>(loadSettings);
  const [msg, setMsg] = useState<string | null>(null);
  async function save() {
    saveSettings(s);
    try {
      await post(`/settings?manager_key=${encodeURIComponent(s.managerKey)}`, { horizon: s.horizon, profile: s.profile });
      setMsg("Saved.");
    } catch (e) {
      setMsg(`Saved locally; server: ${String(e)}`);
    }
  }
  return (
    <>
      <h1>Settings</h1>
      <Card testId="settings">
        <div className="kv">
          <label htmlFor="mk">Manager key</label>
          <input id="mk" value={s.managerKey} onChange={(e) => setS({ ...s, managerKey: e.target.value })} />
          <label htmlFor="pf">Objective profile</label>
          <select id="pf" value={s.profile} onChange={(e) => setS({ ...s, profile: e.target.value as UiSettings["profile"] })}>
            <option value="default">default — maximise expected points</option>
            <option value="conservative">conservative — risk-aware, demands larger, surer gains</option>
            <option value="aggressive">aggressive — upside-seeking</option>
          </select>
          <label htmlFor="hz">Planning horizon (GWs)</label>
          <input id="hz" type="number" min={1} max={10} value={s.horizon} onChange={(e) => setS({ ...s, horizon: Number(e.target.value) })} />
        </div>
        <p className="small">Profiles change objective weights and decision thresholds only — never the rules (config/optimizer/*.yaml).</p>
        <button onClick={save} data-testid="save-settings">Save</button> {msg ? <span role="status">{msg}</span> : null}
      </Card>
    </>
  );
}
