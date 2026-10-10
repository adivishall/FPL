"use client";

import { api } from "@/lib/api";

// Per-browser preferences. The manager key identifies the stored squad (no FPL credentials).
export interface UiSettings {
  managerKey: string;
  profile: "default" | "conservative" | "aggressive";
  horizon: number;
}

const KEY = "fpl.settings.v1";
export const DEFAULTS: UiSettings = { managerKey: "demo", profile: "default", horizon: 5 };

export function loadSettings(): UiSettings {
  if (typeof window === "undefined") return DEFAULTS;
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? { ...DEFAULTS, ...(JSON.parse(raw) as Partial<UiSettings>) } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

export function saveSettings(s: UiSettings): void {
  window.localStorage.setItem(KEY, JSON.stringify(s));
}

// The longest planning horizon the deployment serves (its validated forecast range). Requests
// beyond it are refused by the API rather than shortened, so every page asks for at most this.
let horizonMaxPromise: Promise<number | undefined> | null = null;
export function horizonMax(): Promise<number | undefined> {
  if (!horizonMaxPromise) {
    horizonMaxPromise = api<{ horizon_max?: number }>("/gameweeks/current")
      .then((g) => g.horizon_max)
      .catch(() => {
        horizonMaxPromise = null; // retry on the next call
        return undefined;
      });
  }
  return horizonMaxPromise;
}

export async function effectiveHorizon(s: UiSettings): Promise<number> {
  const max = await horizonMax();
  return max ? Math.max(1, Math.min(s.horizon, max)) : s.horizon;
}
