"use client";

// First-party product analytics (M1.1a): a few named events with small, allow-listed properties,
// sent fire-and-forget to the API through the proxy, where they are stored against the signed-in
// user (never a manager key, never credentials) and pruned after the documented retention. A user
// who opted out in Settings sends nothing; the server drops events for opted-out users too.

import { apiUrl } from "@/lib/api";

export type EventName =
  | "onboarding_started"
  | "onboarding_completed"
  | "manager_import_succeeded"
  | "manager_import_failed"
  | "squad_analysis_viewed"
  | "recommendation_opened"
  | "transfer_comparison_completed"
  | "recommendation_followed"
  | "recommendation_dismissed"
  | "return_visit";

const OPT_OUT_KEY = "fpl.analytics.optout";
const LAST_VISIT_KEY = "fpl.analytics.lastvisit";

export function setAnalyticsOptOut(optOut: boolean): void {
  try {
    window.localStorage.setItem(OPT_OUT_KEY, optOut ? "1" : "0");
  } catch {
    /* storage unavailable: nothing to remember */
  }
}

function optedOut(): boolean {
  try {
    return window.localStorage.getItem(OPT_OUT_KEY) === "1";
  } catch {
    return false;
  }
}

export function track(event: EventName, props: Record<string, string | number | boolean> = {}): void {
  if (typeof window === "undefined" || optedOut()) return;
  const body = JSON.stringify({ event, props });
  try {
    if (navigator.sendBeacon) {
      navigator.sendBeacon(apiUrl("/events"), new Blob([body], { type: "application/json" }));
      return;
    }
  } catch {
    /* fall through to fetch */
  }
  void fetch(apiUrl("/events"), { method: "POST", headers: { "content-type": "application/json" }, body, keepalive: true }).catch(() => undefined);
}

/** Once per calendar day per browser: a returning visit. */
export function trackReturnVisit(): void {
  try {
    const today = new Date().toISOString().slice(0, 10);
    const last = window.localStorage.getItem(LAST_VISIT_KEY);
    if (last === today) return;
    window.localStorage.setItem(LAST_VISIT_KEY, today);
    if (last) track("return_visit", { days_since: Math.round((Date.parse(today) - Date.parse(last)) / 86_400_000) });
  } catch {
    /* storage unavailable */
  }
}
