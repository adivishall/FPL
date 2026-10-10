"use client";

import { useEffect, useState } from "react";

import { Card, State } from "@/components/ui";
import { ApiError, api, del, post } from "@/lib/api";
import { type UiSettings, loadSettings, saveSettings } from "@/lib/settings";

interface ServerSettings {
  notify_min_gain: number;
  notify_injuries: boolean;
  timezone: string;
  webhook_url: string | null;
}

const SERVER_DEFAULTS: ServerSettings = { notify_min_gain: 1, notify_injuries: true, timezone: "Europe/London", webhook_url: null };

export default function Settings() {
  const [s, setS] = useState<UiSettings>(loadSettings);
  const [srv, setSrv] = useState<ServerSettings>(SERVER_DEFAULTS);
  const [msg, setMsg] = useState<string | null>(null);
  // Server-backed fields (and Save, which posts all of them) stay disabled until the stored values
  // have arrived: an edit made earlier was overwritten by the late response, and a save after a
  // failed load would have replaced the stored values with these defaults. A response for a
  // previous manager key is discarded.
  const [loaded, setLoaded] = useState(false);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  // The longest horizon the deployment serves (the validated forecast range); never offer more.
  const [horizonMax, setHorizonMax] = useState(5);
  useEffect(() => {
    api<{ horizon_max?: number }>("/gameweeks/current").then((g) => g.horizon_max && setHorizonMax(g.horizon_max)).catch(() => undefined);
  }, []);
  const key = encodeURIComponent(s.managerKey);
  useEffect(() => {
    let current = true;
    setLoaded(false);
    setLoadErr(null);
    api<{ settings: Partial<ServerSettings> }>(`/settings?manager_key=${key}`)
      .then((r) => {
        if (!current) return;
        setSrv({ ...SERVER_DEFAULTS, ...r.settings });
        setLoaded(true);
      })
      .catch((e) => {
        if (current) setLoadErr(`Stored settings could not be loaded (${e instanceof ApiError ? e.message : String(e)}); nothing can be saved until they are.`);
      });
    return () => {
      current = false;
    };
  }, [key, attempt]);
  async function save() {
    saveSettings(s);
    try {
      await post(`/settings?manager_key=${key}`, {
        horizon: Math.min(s.horizon, horizonMax),
        profile: s.profile,
        notify_min_gain: srv.notify_min_gain,
        notify_injuries: srv.notify_injuries,
        timezone: srv.timezone,
        webhook_url: srv.webhook_url || null,
      });
      setMsg("Saved.");
    } catch (e) {
      setMsg(`Saved locally; server rejected: ${String(e)}`);
    }
  }
  async function exportData() {
    const data = await api<unknown>(`/managers/${key}/export`);
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `fpl-engine-${s.managerKey}-export.json`;
    a.click();
    URL.revokeObjectURL(url);
    setMsg("Export downloaded.");
  }
  async function deleteData() {
    if (!window.confirm(`Permanently delete every record stored for "${s.managerKey}"?`)) return;
    const r = await del<{ deleted: Record<string, number> }>(`/managers/${key}`);
    const total = Object.values(r.deleted).reduce((a, b) => a + b, 0);
    setMsg(`Deleted ${total} records.`);
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
          <input id="hz" type="number" min={1} max={horizonMax} value={Math.min(s.horizon, horizonMax)} onChange={(e) => setS({ ...s, horizon: Math.max(1, Math.min(horizonMax, Number(e.target.value))) })} />
          <span className="small">1–{horizonMax}: forecasts are validated this far ahead; longer horizons are not served.</span>
        </div>
        <p className="small">Profiles change objective weights and decision thresholds only — never the rules (config/optimizer/*.yaml).</p>
      </Card>
      <Card title="Notifications" testId="notification-settings">
        <State loading={!loaded && !loadErr} error={loadErr} />
        {loadErr ? <button className="secondary" onClick={() => setAttempt((n) => n + 1)} data-testid="retry-settings">Retry</button> : null}
        <div className="kv">
          <label htmlFor="tz">Time zone (deadline reminders)</label>
          <input id="tz" disabled={!loaded} value={srv.timezone} onChange={(e) => setSrv({ ...srv, timezone: e.target.value })} />
          <label htmlFor="mg">Re-plan alert threshold (points)</label>
          <input id="mg" disabled={!loaded} type="number" min={0} max={20} step={0.5} value={srv.notify_min_gain} onChange={(e) => setSrv({ ...srv, notify_min_gain: Number(e.target.value) })} />
          <label htmlFor="inj">Injury / role alerts</label>
          <input id="inj" disabled={!loaded} type="checkbox" checked={srv.notify_injuries} onChange={(e) => setSrv({ ...srv, notify_injuries: e.target.checked })} />
          <label htmlFor="wh">Webhook (HTTPS, allow-listed host)</label>
          <input id="wh" disabled={!loaded} value={srv.webhook_url ?? ""} placeholder="optional" onChange={(e) => setSrv({ ...srv, webhook_url: e.target.value })} />
        </div>
        <button onClick={save} disabled={!loaded} data-testid="save-settings">Save</button> {msg ? <span role="status" data-testid="settings-msg">{msg}</span> : null}
      </Card>
      <Card title="Your data" testId="privacy">
        <p className="small">
          Stored per manager key: squads, recommendations, journal feedback, settings and alerts. No FPL credentials are
          ever stored. Deletion is permanent; an audit entry records only a hash of the key.
        </p>
        <button className="secondary" onClick={exportData} data-testid="export-data">Export (JSON)</button>{" "}
        <button className="danger" onClick={deleteData} data-testid="delete-data">Delete all my data</button>
      </Card>
    </>
  );
}
