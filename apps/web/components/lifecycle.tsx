"use client";

import { useEffect, useRef, useState } from "react";

import { Badge } from "@/components/ui";
import { ApiError, api, type Changes, type Lifecycle, type LifecycleReason } from "@/lib/api";
import { money, when } from "@/lib/format";

// Result lifecycle (M1.3): one badge that says whether a stored result is still the current
// answer, the reasons when it is not, and a "What changed?" panel built from the API's
// comparison of the stored inputs with the inputs served now. The page never guesses.

export function lifecycleTone(lc: Lifecycle): "good" | "warn" | "bad" | "neutral" {
  if (lc.state === "current") return "good";
  if (lc.state === "pending") return "neutral";
  if (lc.state === "failed") return "bad";
  return "warn";
}

export function lifecycleLabel(lc: Lifecycle): string {
  switch (lc.state) {
    case "current":
      return "current";
    case "pending":
      return "computing";
    case "failed":
      return "failed";
    case "superseded":
      return "historical — superseded";
    default:
      return "historical — not current";
  }
}

function describe(r: LifecycleReason): string {
  const id = (v: unknown) => (typeof v === "string" ? v : v && typeof v === "object" ? Object.values(v as Record<string, unknown>).join("/") : String(v ?? "?"));
  const at = r.detected_at ? ` (detected ${when(r.detected_at)})` : "";
  switch (r.kind) {
    case "snapshot_promoted":
      return `data snapshot ${id(r.old)} was superseded by ${id(r.new)}${at}`;
    case "squad_changed":
      return `your squad was saved again (state ${id(r.old)} → ${id(r.new)})${at}`;
    case "superseded":
      return "a newer recommendation for this gameweek replaced it";
    case "inputs_changed_before_finalised":
      return `${r.detail} (${id(r.old)} → ${id(r.new)})${at}`;
    default:
      return r.detail;
  }
}

export function LifecycleBadge({ lc, testId }: { lc: Lifecycle; testId?: string }) {
  return (
    <span data-testid={testId ?? "lifecycle"} data-state={lc.state}>
      <Badge tone={lifecycleTone(lc)}>{lifecycleLabel(lc)}</Badge>
      {lc.reasons.length ? (
        <ul className="clean small" data-testid="lifecycle-reasons">
          {lc.reasons.map((r, i) => (
            <li key={i}>{describe(r)}</li>
          ))}
        </ul>
      ) : null}
      {!lc.current && lc.state !== "pending" ? (
        <p className="small">This result is kept for reference only; it is not a recommendation for your current situation.</p>
      ) : null}
    </span>
  );
}

/** Loads and renders the detailed comparison for a result (recommendation or workbench run). */
export function WhatChanged({ path, label }: { path: string; label?: string }) {
  const [data, setData] = useState<Changes | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // the comparison belongs to one result: another result (a regenerated plan, another run)
  // starts closed, and a response for the previous one is dropped
  const current = useRef(path);
  useEffect(() => {
    current.current = path;
    setData(null);
    setErr(null);
  }, [path]);
  async function load() {
    const asked = path;
    setBusy(true);
    setErr(null);
    try {
      const got = await api<Changes>(asked);
      if (current.current === asked) setData(got);
    } catch (e) {
      if (current.current === asked) setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  }
  if (!data) {
    return (
      <p>
        <button className="secondary" onClick={() => void load()} disabled={busy} data-testid="what-changed-button">
          {busy ? "Comparing…" : (label ?? "What changed?")}
        </button>
        {err ? <span className="error"> {err}</span> : null}
      </p>
    );
  }
  const st = data.state;
  const sn = data.snapshot;
  const fc = data.forecast;
  return (
    <div data-testid="what-changed">
      <strong>What changed</strong>
      {st ? (
        <div data-testid="changed-state">
          <p className="small">
            Squad state {st.old_state_id} → {st.new_state_id}
            {st.captured_at ? ` (saved ${when(st.captured_at)})` : ""}.
            {st.available ? (
              <>
                {" "}
                {st.players_out?.length ? `Out: ${st.players_out.map((p) => p.name).join(", ")}. ` : ""}
                {st.players_in?.length ? `In: ${st.players_in.map((p) => p.name).join(", ")}. ` : ""}
                {st.bank && st.bank.old !== st.bank.new ? `Bank ${money(st.bank.old)} → ${money(st.bank.new)}. ` : ""}
                {st.free_transfers && st.free_transfers.old !== st.free_transfers.new ? `Free transfers ${st.free_transfers.old} → ${st.free_transfers.new}. ` : ""}
              </>
            ) : (
              ` ${st.reason ?? ""}`
            )}
          </p>
        </div>
      ) : null}
      {sn ? (
        <div data-testid="changed-snapshot">
          <p className="small">
            Data snapshot {sn.old_snapshot_id}
            {sn.old_as_of ? ` (${when(sn.old_as_of)})` : ""} → {sn.new_snapshot_id}
            {sn.new_as_of ? ` (${when(sn.new_as_of)})` : ""}.
          </p>
          {sn.available ? (
            <>
              {sn.season_changed ? <p className="small">Season {sn.season_changed.old} → {sn.season_changed.new}.</p> : null}
              {sn.tables_changed === null || sn.tables_changed === undefined ? (
                <p className="small">Table-level differences unknown (one manifest unavailable).</p>
              ) : sn.tables_changed.length ? (
                <p className="small">Changed tables: {sn.tables_changed.join(", ")}.</p>
              ) : (
                <p className="small">No table content differs for the data this result used.</p>
              )}
              {sn.players && sn.players.length ? (
                <ul className="clean small">
                  {sn.players.map((p) => (
                    <li key={p.player_code}>
                      <strong>{p.name as string}</strong>:{" "}
                      {Object.entries(p)
                        .filter(([k]) => !["player_code", "name"].includes(k))
                        .map(([k, v]) => {
                          const d = v as { old: unknown; new: unknown };
                          return `${k.replace("chance_of_playing_next_round", "chance of playing")} ${String(d.old ?? "—")} → ${String(d.new ?? "—")}`;
                        })
                        .join("; ")}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="small">No availability, news or price change for the players this result depends on.</p>
              )}
              {sn.fixtures && sn.fixtures.length ? (
                <p className="small">Fixtures: {sn.fixtures.map((f) => `GW${f.gameweek} ${f.old_fixtures} → ${f.new_fixtures} fixture(s)`).join("; ")}.</p>
              ) : null}
            </>
          ) : (
            <p className="small" data-testid="changed-snapshot-unavailable">{sn.reason}</p>
          )}
        </div>
      ) : null}
      {fc ? (
        <p className="small" data-testid="changed-forecast">
          Forecast:{" "}
          {fc.available
            ? fc.changed
              ? `recomputed (${fc.old_prediction_run_id ?? "?"} → ${fc.new_prediction_run_id})`
              : "the same prediction run is still served"
            : fc.reason}
        </p>
      ) : null}
      {!st && !sn && !fc ? (
        data.lifecycle.current ? (
          <p className="small">No input difference is recorded for this result.</p>
        ) : (
          <p className="small" data-testid="changed-none-comparable">
            No stored input of this result can be compared with the data served now; it is historical for the reasons listed above.
          </p>
        )
      ) : null}
    </div>
  );
}
