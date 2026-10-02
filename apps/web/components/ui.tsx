"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import { ApiError, api, type Freshness } from "@/lib/api";
import { pct, when } from "@/lib/format";

const NAV: [string, string][] = [
  ["/", "Overview"],
  ["/transfers", "Transfer Lab"],
  ["/squad", "Squad Planner"],
  ["/planner", "Future Planner"],
  ["/players", "Player Lab"],
  ["/what-if", "What-If"],
  ["/journal", "Journal"],
  ["/alerts", "Alerts"],
  ["/backtests", "Backtest Lab"],
  ["/health", "Data Health"],
  ["/settings", "Settings"],
];

export function Nav() {
  const path = usePathname();
  return (
    <nav className="nav" aria-label="Main">
      <span className="brand">FPL Decision Engine</span>
      {NAV.map(([href, label]) => (
        <Link key={href} href={href} className={path === href ? "active" : ""}>
          {label}
        </Link>
      ))}
    </nav>
  );
}

export function Card(props: { title?: string; children: ReactNode; testId?: string; wide?: boolean }) {
  return (
    <section className={`card${props.wide ? " wide" : ""}`} data-testid={props.testId}>
      {props.title ? <h2>{props.title}</h2> : null}
      {props.children}
    </section>
  );
}

export function Badge(props: { tone: "good" | "warn" | "bad" | "neutral"; children: ReactNode }) {
  return <span className={`badge ${props.tone}`}>{props.children}</span>;
}

export function confidenceTone(p: number | null | undefined): "good" | "warn" | "bad" | "neutral" {
  if (p === null || p === undefined) return "neutral";
  return p >= 0.75 ? "good" : p >= 0.6 ? "warn" : "bad";
}

export function FreshnessBanner({ f }: { f: Freshness | undefined }) {
  if (!f) return null;
  return (
    <div className={`banner ${f.degraded ? "degraded" : "fresh"}`} role="status" data-testid="freshness">
      <strong>{f.degraded ? "Degraded mode" : "Data fresh"}</strong> · {f.season} GW{f.gameweek} ·
      decision cutoff {when(f.decision_cutoff)} · latest source {when(f.latest_source_at)} · snapshot{" "}
      <code>{f.data_snapshot_id}</code>
      {f.degraded ? <div className="small">{f.degraded_reasons.join(" · ")}</div> : null}
    </div>
  );
}

/** p10–p90 range, interquartile box and mean marker on a shared scale (SVG, no chart library). */
export function DistBar(props: { p10: number; p25?: number; p75?: number; p90: number; mean: number; max: number }) {
  const w = 180;
  const x = (v: number) => Math.max(0, Math.min(w, (v / Math.max(props.max, 1)) * w));
  return (
    <svg width={w} height={14} role="img" aria-label={`p10 ${props.p10}, mean ${props.mean.toFixed(1)}, p90 ${props.p90}`}>
      <line x1={x(props.p10)} x2={x(props.p90)} y1={7} y2={7} stroke="var(--muted)" strokeWidth={2} />
      {props.p25 !== undefined && props.p75 !== undefined ? (
        <rect x={x(props.p25)} y={3} width={Math.max(1, x(props.p75) - x(props.p25))} height={8} fill="var(--accent-soft)" />
      ) : null}
      <circle cx={x(props.mean)} cy={7} r={3.5} fill="var(--accent)" />
    </svg>
  );
}

export function Prob({ p }: { p: number | null | undefined }) {
  return <Badge tone={confidenceTone(p)}>{pct(p)}</Badge>;
}

export function useApi<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const load = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    setError(null);
    try {
      setData(await api<T>(path));
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
      setData(null);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(() => {
    void load();
  }, [load]);
  return { data, error, loading, reload: load };
}

export function State(props: { loading?: boolean; error?: string | null; empty?: string }) {
  if (props.loading) return <p className="muted" role="status">Loading…</p>;
  if (props.error) return <p className="error" role="alert">{props.error}</p>;
  if (props.empty) return <p className="muted">{props.empty}</p>;
  return null;
}

export function PlayerName({ code, names }: { code: number; names?: Record<string, string | null> }) {
  const n = names?.[String(code)];
  return (
    <Link href={`/players/${code}`} className="player">
      {n ?? code}
    </Link>
  );
}
