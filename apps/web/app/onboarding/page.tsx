"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useUser } from "@/components/auth";
import { Card, FreshnessBanner } from "@/components/ui";
import { ApiError, api, post, type EntryPreview, type SyncResult } from "@/lib/api";
import { money } from "@/lib/format";
import { track } from "@/lib/analytics";
import { loadSettings, saveSettings } from "@/lib/settings";

// FPL-ID onboarding: validate the ID, show whose team it is, confirm, import the real squad from
// the public FPL API (no credentials), and land on Copilot Home. Manual entry stays available
// as a fallback on the Squad page.

type Step = "id" | "preview" | "done";

function explain(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 404) return "No FPL manager has this ID. The ID is the number in the URL of your Points page on fantasy.premierleague.com (…/entry/<ID>/…).";
    if (e.status === 503) return `The FPL API cannot be reached from this deployment right now (${e.message}). Try again later, or enter your squad manually on the Squad page.`;
    if (e.status === 502) return `The FPL API answered in an unexpected way (${e.message}). Try again later, or enter your squad manually.`;
    if (e.status === 422) return `That is not a valid FPL ID (${e.message}).`;
    return `${e.status}: ${e.message}`;
  }
  return String(e);
}

export default function Onboarding() {
  const router = useRouter();
  const { user, reload } = useUser();
  const [step, setStep] = useState<Step>("id");
  const [entryId, setEntryId] = useState("");
  const [preview, setPreview] = useState<EntryPreview | null>(null);
  const [result, setResult] = useState<SyncResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function lookUp(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    track("onboarding_started");
    try {
      const id = Number(entryId.trim());
      if (!Number.isInteger(id) || id <= 0) throw new ApiError(422, "an FPL ID is a positive whole number");
      setPreview(await api<EntryPreview>(`/fpl/entry/${id}`));
      setStep("preview");
    } catch (e) {
      setErr(explain(e));
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!preview || !user) return;
    setErr(null);
    setBusy(true);
    try {
      // a manager key owned by this account, created for this import unless one already exists
      // for the same FPL team
      const existing = user.managers.find((m) => m.label === `fpl:${preview.entry_id}`);
      const key =
        existing?.manager_key ??
        (await post<{ manager_key: string }>("/auth/managers", { label: `fpl:${preview.entry_id}` })).manager_key;
      const r = await post<SyncResult>("/squad/sync", { manager_key: key, manager_id: preview.entry_id });
      saveSettings({ ...loadSettings(), managerKey: key });
      await reload();
      setResult(r);
      setStep("done");
      track("manager_import_succeeded", { warnings: r.warnings.length });
      track("onboarding_completed");
    } catch (e) {
      track("manager_import_failed", { status: e instanceof ApiError ? e.status : 0 });
      setErr(explain(e));
    } finally {
      setBusy(false);
    }
  }

  const st = result?.state;
  return (
    <>
      <h1>Set up your team</h1>
      <ol className="steps" aria-label="steps">
        <li className={step === "id" ? "active" : "done"}>1 · Your FPL ID</li>
        <li className={step === "preview" ? "active" : step === "done" ? "done" : ""}>2 · Confirm it is your team</li>
        <li className={step === "done" ? "active" : ""}>3 · Squad imported</li>
      </ol>
      {step === "id" ? (
        <Card title="Enter your FPL ID" testId="onboarding-id">
          <form onSubmit={lookUp} className="row">
            <label htmlFor="entry">FPL ID</label>
            <input id="entry" inputMode="numeric" value={entryId} onChange={(e) => setEntryId(e.target.value)} placeholder="e.g. 1234567" required />
            <button type="submit" disabled={busy} data-testid="lookup-entry">{busy ? "Checking…" : "Look up"}</button>
          </form>
          <p className="small">
            Find it on fantasy.premierleague.com → Points: the number in the address bar after <code>/entry/</code>. Only public data is read; no FPL password is ever asked for.
            Prefer to type the squad yourself? <Link href="/squad">Enter it manually</Link>.
          </p>
          {err ? <p className="error" role="alert" data-testid="onboarding-error">{err}</p> : null}
        </Card>
      ) : null}
      {step === "preview" && preview ? (
        <Card title="Is this your team?" testId="onboarding-preview">
          <dl className="kv">
            <dt>Team</dt><dd data-testid="preview-team">{preview.team_name}</dd>
            <dt>FPL ID</dt><dd>{preview.entry_id}</dd>
            <dt>Overall points</dt><dd>{preview.overall_points ?? "—"}</dd>
            <dt>Overall rank</dt><dd>{preview.overall_rank?.toLocaleString() ?? "—"}</dd>
            <dt>Playing since</dt><dd>GW{preview.started_event}</dd>
          </dl>
          <p className="small">Source: {preview.source}. Nothing is stored until you confirm.</p>
          <div className="row">
            <button onClick={confirm} disabled={busy} data-testid="confirm-import">{busy ? "Importing…" : "Yes, import this squad"}</button>
            <button className="secondary" onClick={() => { setStep("id"); setPreview(null); }} disabled={busy}>No, different ID</button>
          </div>
          {err ? <p className="error" role="alert">{err}</p> : null}
        </Card>
      ) : null}
      {step === "done" && result && st ? (
        <Card title="Squad imported" testId="onboarding-done">
          <FreshnessBanner f={result.freshness} />
          <p>
            <strong>{st.squad.length} players</strong> · bank {money(st.bank)} · {st.free_transfers} free transfer(s) · chips available:{" "}
            {st.chips.filter((c) => c.status === "available").map((c) => c.chip_id).join(", ") || "none"}
          </p>
          <p className="small">
            Imported from the official FPL API (entry {preview?.entry_id}) for GW{st.gameweek}. Free transfers are reconstructed from your transfer history through the rules; purchase prices from your transfer history.
            Model-derived values (expected points, start probabilities) are computed separately and shown on Home.
          </p>
          {result.warnings.length ? (
            <details>
              <summary>{result.warnings.length} import note(s)</summary>
              <ul className="clean">{result.warnings.map((w, i) => <li key={i} className="small">{w}</li>)}</ul>
            </details>
          ) : null}
          <div className="row">
            <button onClick={() => router.push("/")} data-testid="go-home">Go to Copilot Home</button>
            <Link href="/squad" className="small">Review the squad</Link>
          </div>
        </Card>
      ) : null}
    </>
  );
}
