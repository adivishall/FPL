"use client";

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
