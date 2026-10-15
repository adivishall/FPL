"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import { signOut, useUser } from "@/components/auth";
import { ApiError, api, type Freshness } from "@/lib/api";
import { pct, when } from "@/lib/format";

// Navigation in task groups (M1.6): what to do this gameweek, what to explore, what was
// recorded, and the system. Link labels are stable (the browser tests and the docs use them).
const NAV: [string, [string, string][]][] = [
  [
    "Plan",
    [
      ["/", "Overview"],
      ["/transfers", "Transfer Workbench"],
      ["/squad", "Squad Planner"],
      ["/planner", "Future Planner"],
    ],
  ],
  [
    "Explore",
    [
      ["/players", "Player Lab"],
      ["/what-if", "What-If"],
    ],
  ],
  [
    "Record",
    [
      ["/journal", "Journal"],
      ["/season-review", "Season Review"],
      ["/alerts", "Alerts"],
    ],
  ],
  [
    "System",
    [
      ["/backtests", "Backtest Lab"],
      ["/health", "Data Health"],
      ["/settings", "Settings"],
    ],
  ],
];

export function Nav() {
  const path = usePathname();
  const { user } = useUser();
  // the strip scrolls sideways on narrow screens: keep the current page's link in view
  useEffect(() => {
    const strip = document.querySelector<HTMLElement>(".nav .inner");
    if (!strip || strip.scrollWidth <= strip.clientWidth) return;
    strip.querySelector<HTMLElement>("a.active")?.scrollIntoView({ inline: "center", block: "nearest" });
  }, [path, user]);
  return (
    <header>
      <div className="topbar">
        <div className="inner">
          <Link href="/" className="brand" aria-label="FPL Copilot home">
            <span className="mark" aria-hidden="true" />
            FPL Copilot <span className="beta">beta</span>
          </Link>
          {user ? (
            <span className="account" data-testid="account">
              <span className="who">{user.email}</span>
              <button type="button" className="secondary" onClick={() => void signOut()} data-testid="sign-out">
                Sign out
              </button>
            </span>
          ) : (
            <Link href="/login" className={`signin${path === "/login" ? " active" : ""}`}>Sign in</Link>
          )}
        </div>
      </div>
      {user ? (
        <nav className="nav" aria-label="Main">
          <div className="inner">
            {NAV.map(([group, links]) => (
              <span key={group} style={{ display: "contents" }}>
                <span className="group" aria-hidden="true">{group}</span>
                {links.map(([href, label]) => (
                  <Link key={href} href={href} className={path === href ? "active" : ""} aria-current={path === href ? "page" : undefined}>
                    {label}
                  </Link>
                ))}
              </span>
            ))}
          </div>
        </nav>
      ) : null}
    </header>
  );
}

/** Page title, an optional one-line explanation, and actions aligned to the right. */
export function PageHeader(props: { title: ReactNode; lede?: ReactNode; actions?: ReactNode; testId?: string }) {
  return (
    <div className="page-head" data-testid={props.testId}>
      <h1>{props.title}</h1>
      {props.actions ? <div className="actions">{props.actions}</div> : null}
      {props.lede ? <p className="lede small">{props.lede}</p> : null}
    </div>
  );
}

export function Card(props: { title?: string; children: ReactNode; testId?: string; wide?: boolean; tone?: "accent" | "brand" }) {
  return (
    <section className={`card${props.wide ? " wide" : ""}${props.tone ? ` ${props.tone}` : ""}`} data-testid={props.testId}>
      {props.title ? <h2>{props.title}</h2> : null}
      {props.children}
    </section>
  );
}

/** A row of headline numbers (label, value, hint); tone colours the value, the label says why. */
export function Stats({ children }: { children: ReactNode }) {
  return <div className="stats">{children}</div>;
}

export function Stat(props: { label: string; value: ReactNode; hint?: ReactNode; tone?: "good" | "warn" | "bad"; testId?: string }) {
  return (
    <div className={`stat${props.tone ? ` ${props.tone}` : ""}`} data-testid={props.testId}>
      <span className="label">{props.label}</span>
      <span className="value">{props.value}</span>
      {props.hint ? <span className="hint">{props.hint}</span> : null}
    </div>
  );
}

export function Callout(props: { tone?: "info" | "good" | "warn" | "bad" | "pending"; children: ReactNode; testId?: string; role?: "status" | "alert" }) {
  return (
    <div className={`callout${props.tone ? ` ${props.tone}` : ""}`} data-testid={props.testId} role={props.role}>
      {props.children}
    </div>
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
      <line x1={x(props.p10)} x2={x(props.p90)} y1={7} y2={7} stroke="var(--line-strong)" strokeWidth={2} />
      {props.p25 !== undefined && props.p75 !== undefined ? (
        <rect x={x(props.p25)} y={3} width={Math.max(1, x(props.p75) - x(props.p25))} height={8} fill="var(--accent-soft)" />
      ) : null}
      <circle cx={x(props.mean)} cy={7} r={3.5} fill="var(--brand)" />
    </svg>
  );
}

export function Prob({ p }: { p: number | null | undefined }) {
  return <Badge tone={confidenceTone(p)}>{pct(p)}</Badge>;
}

export function useApi<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  // loading from the first render when there is something to load: otherwise the page shows
  // its "nothing here yet" state (e.g. "No recommendation" + Generate) for a frame before the
  // request starts
  const [loading, setLoading] = useState(path !== null);
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
