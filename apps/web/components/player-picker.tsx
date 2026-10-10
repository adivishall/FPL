"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { ApiError, api, type PlayerRow } from "@/lib/api";
import { money } from "@/lib/format";

// A searchable player picker over the real player pool of the current snapshot (/players): name
// search, team and position filters, an affordability cap, availability status and keyboard
// selection. Identity is the player code; the list shows name, club, position and price so two
// players with the same name are never ambiguous.

const STATUS_LABEL: Record<string, string> = {
  a: "available",
  d: "doubtful",
  i: "injured",
  s: "suspended",
  u: "unavailable",
  n: "not in squad",
};

export interface PickerProps {
  onPick: (p: PlayerRow) => void;
  exclude?: number[]; // codes already in the squad
  maxPrice?: number | null; // tenths of £m; null = no cap
  position?: string | null; // restrict to one position
  label?: string;
  testId?: string;
}

let poolPromise: Promise<PlayerRow[]> | null = null;
/** The whole selectable pool once per page load (≤ ~700 rows); filtering is then instant. */
function loadPool(): Promise<PlayerRow[]> {
  if (!poolPromise) {
    poolPromise = api<{ players: PlayerRow[] }>("/players?limit=1000&sort=xp")
      .then((r) => r.players)
      .catch((e) => {
        poolPromise = null;
        throw e;
      });
  }
  return poolPromise;
}

export function PlayerPicker(props: PickerProps) {
  const [pool, setPool] = useState<PlayerRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [team, setTeam] = useState<string>("");
  const [pos, setPos] = useState<string>(props.position ?? "");
  const [affordable, setAffordable] = useState(true);
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);

  useEffect(() => {
    loadPool().then(setPool).catch((e) => setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e)));
  }, []);
  useEffect(() => setPos(props.position ?? ""), [props.position]);

  const teams = useMemo(() => Array.from(new Set((pool ?? []).map((p) => p.team))).sort(), [pool]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const excluded = new Set(props.exclude ?? []);
    return (pool ?? [])
      .filter((p) => !excluded.has(p.player_code))
      .filter((p) => (pos ? p.position === pos : true))
      .filter((p) => (team ? p.team === team : true))
      .filter((p) => (affordable && props.maxPrice != null ? p.price <= props.maxPrice : true))
      .filter((p) => (needle ? p.name.toLowerCase().includes(needle) : true))
      .slice(0, 40);
  }, [pool, q, pos, team, affordable, props.exclude, props.maxPrice]);
  useEffect(() => setActive(0), [q, pos, team, affordable]);

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(rows.length - 1, a + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(0, a - 1));
    } else if (e.key === "Enter") {
      const r = rows[active];
      if (r) {
        e.preventDefault();
        props.onPick(r);
      }
    }
  }
  useEffect(() => {
    listRef.current?.children[active]?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const label = props.label ?? "Find a player";
  return (
    <div className="picker" data-testid={props.testId ?? "player-picker"} onKeyDown={onKey}>
      <div className="row">
        <label>
          {label}{" "}
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="name…"
            aria-label={label}
            autoComplete="off"
            data-testid="picker-search"
          />
        </label>
        <label>
          Club{" "}
          <select value={team} onChange={(e) => setTeam(e.target.value)} aria-label="club">
            <option value="">any</option>
            {teams.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </label>
        <label>
          Position{" "}
          <select value={pos} onChange={(e) => setPos(e.target.value)} aria-label="position" disabled={!!props.position}>
            <option value="">any</option>
            {["GK", "DEF", "MID", "FWD"].map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
        </label>
        {props.maxPrice != null ? (
          <label>
            <input type="checkbox" checked={affordable} onChange={(e) => setAffordable(e.target.checked)} /> affordable (≤ {money(props.maxPrice)})
          </label>
        ) : null}
      </div>
      {error ? <p className="error" role="alert">Players could not be loaded ({error}).</p> : null}
      {!pool && !error ? <p className="muted" role="status">Loading players…</p> : null}
      {pool && rows.length === 0 ? <p className="muted" data-testid="picker-empty">No player matches these filters.</p> : null}
      {rows.length > 0 ? (
        <ul className="clean picker-list" role="listbox" aria-label={`${label} results`} ref={listRef}>
          {rows.map((p, i) => (
            <li
              key={p.player_code}
              role="option"
              aria-selected={i === active}
              className={i === active ? "active" : ""}
              onMouseEnter={() => setActive(i)}
              onClick={() => props.onPick(p)}
              data-testid={`pick-${p.player_code}`}
            >
              <strong>{p.name}</strong> <span className="small">{p.team} · {p.position} · {money(p.price)}</span>{" "}
              {p.status && p.status !== "a" ? <span className="badge warn">{STATUS_LABEL[p.status] ?? p.status}</span> : null}{" "}
              <span className="small muted">xP {p.xp_next.toFixed(1)} next · code {p.player_code}</span>
            </li>
          ))}
        </ul>
      ) : null}
      <p className="small muted">Arrow keys move, Enter picks. Source: the current data snapshot (/players).</p>
    </div>
  );
}
