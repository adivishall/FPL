// Typed client for the FPL Decision Engine API (docs/API.md). All numbers shown in the UI come
// from these responses — the UI never computes or invents a recommendation.

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}/api/v1${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, (body as { detail?: unknown }).detail ?? body);
  return body as T;
}

export const post = <T,>(path: string, data: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(data) });

export interface Freshness {
  data_snapshot_id: string;
  season: string;
  gameweek: number;
  decision_cutoff: string;
  latest_source_at: string | null;
  status: string;
  message: string;
  degraded: boolean;
  degraded_reasons: string[];
}

export interface Gameweek {
  season: string;
  gameweek: number;
  deadline: string;
  decision_cutoff: string;
  freshness: Freshness;
}

export interface PlayerRow {
  player_code: number;
  name: string;
  team: string;
  team_code: number;
  position: string;
  price: number;
  status?: string | null;
  xp: number;
  xp_next: number;
  p10_next: number;
  p90_next: number;
  start_probability: number;
}

export interface ForecastRow {
  player_code: number;
  gw: number;
  horizon: number;
  mean: number;
  std: number;
  p10: number;
  p25: number;
  p50: number;
  p75: number;
  p90: number;
  prob_2_plus: number;
  prob_6_plus: number;
  prob_10_plus: number;
  prob_15_plus: number;
  prob_start: number;
  prob_play: number;
  expected_minutes: number;
  [component: `xp_${string}`]: number;
}

export interface Gain {
  mean: number;
  p10: number;
  p50: number;
  p90: number;
  probability_positive: number;
}

export interface PlanStep {
  gameweek: number;
  action: string;
  transfers_out: number[];
  transfers_in: number[];
  chip: string | null;
  hit_points: number;
  free_transfers: number;
  bank_after: number;
  expected_points: number;
  p10: number;
  p90: number;
  expected_delta_vs_hold: number;
  probability_delta_positive: number;
  captain: number;
  vice_captain: number;
  squad_blanks: number[];
  squad_doubles: number[];
}

export interface OptionSummary {
  label: string;
  action: string;
  sells: number[];
  buys: number[];
  chip: string | null;
  objective: number;
  objective_gain_vs_hold: number;
  gain_1gw: Gain;
  gain_horizon: Gain;
  passes_thresholds: boolean;
  valid: boolean;
  timeline: PlanStep[];
}

export interface Evidence {
  evidence_id: string;
  feature: string;
  label: string;
  player_id: number | null;
  value: number;
  baseline: number | null;
  direction: string;
  unit: string;
  gameweeks: number[];
  source_snapshot: string;
  model_version: string;
  importance: number;
}

export interface ChipWeek {
  gameweek: number;
  uplift_mean: number;
  uplift_p10: number;
  uplift_p90: number;
  probability_positive: number;
  method: string;
}

export interface ChipPlan {
  chip_id: string;
  chip_type: string;
  window: [number, number];
  by_gameweek: ChipWeek[];
  best_gameweek: number | null;
  value_now: number | null;
  value_of_waiting: number | null;
  recommendation: string;
  reason: string;
}

export interface Recommendation {
  id: string;
  status: string;
  decision_id: string;
  gameweek: number;
  season: string;
  decision: {
    action: string;
    transfers_out: number[];
    transfers_in: number[];
    chip: string | null;
    expected_points: number;
    expected_gain_vs_hold: number;
    p10: number;
    p50: number;
    p90: number;
    confidence: number | null;
    stability: string | null;
  };
  chosen: OptionSummary;
  hold: OptionSummary;
  alternatives: OptionSummary[];
  lineup: { starters: number[]; bench: number[]; captain: number; vice_captain: number };
  captaincy: {
    expected: number;
    safe: number;
    high_variance: number;
    options: { player: number; mean_total: number; p_captain_haul: number; p_beats_expected_choice: number }[];
  } | null;
  evidence: Evidence[];
  explanation: {
    decision: string;
    primary_drivers: { text: string; evidence_ids: string[] }[];
    constraints_binding: string[];
    downside_scenarios: { scenario: string; description: string; gain_vs_hold: number | null; feasible: boolean }[];
    refusal_reasons: string[];
    confidence: { probability_beats_hold: number; gain_p10: number; gain_p90: number };
    assumptions: string[];
  };
  stability: { label: string; share_near_optimal: number; perturbations: number } | null;
  chips: ChipPlan[];
  markdown: string;
  optimizer_run_id: string;
  snapshot_id: string;
  model_versions: Record<string, string>;
  freshness: Freshness;
}

export interface SquadState {
  state_id: string;
  state: {
    season: string;
    gameweek: number;
    bank: number;
    free_transfers: number;
    squad: { player_code: number; position: string; team_code: number; purchase_price: number }[];
    chips: { chip_id: string; chip_type: string; status: string; first_gameweek: number; last_gameweek: number }[];
  };
  names: Record<string, string | null>;
  freshness?: Freshness;
}

export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "succeeded" | "failed";
  result_ref: string | null;
  error: string | null;
}

export async function waitForJob(id: string, onTick?: (j: Job) => void): Promise<Job> {
  for (let i = 0; i < 600; i += 1) {
    const j = await api<Job>(`/jobs/${id}`);
    onTick?.(j);
    if (j.status === "succeeded" || j.status === "failed") return j;
    await new Promise((r) => setTimeout(r, 1500));
  }
  throw new Error("job did not finish in time");
}
