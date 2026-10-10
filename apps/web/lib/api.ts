// Typed client for the FPL Decision Engine API (docs/API.md). All numbers shown in the UI come
// from these responses — the UI never computes or invents a recommendation.

// Direct mode (NEXT_PUBLIC_API_BASE set, development/e2e) or same-origin proxy mode (production:
// /backend/* is forwarded server-side with the API key, which never reaches the browser).
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";
const ROOT = API_BASE ? `${API_BASE}/api/v1` : "/backend";
export const apiUrl = (path: string) => `${ROOT}${path}`;

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${ROOT}${path}`, {
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
  horizon_max?: number; // longest planning horizon this deployment serves (validated forecast range)
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
  optimality?: string;
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
  state_id?: string | null; // the stored squad state this plan evaluated
  stale_state?: boolean; // the squad was saved again since: the plan describes a previous squad
  latest_state_id?: string | null;
  id: string;
  /** names of every player an alternative sells or buys (not all are in the squad) */
  names?: Record<string, string | null>;
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
    optimality?: string; // "proven optimal", or best found within the solver limit
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

export interface EntryPreview {
  entry_id: number;
  team_name: string;
  started_event: number;
  current_event: number | null;
  overall_points: number | null;
  overall_rank: number | null;
  source: string;
}

export interface SyncResult {
  state_id: string;
  state: SquadState["state"];
  warnings: string[];
  freshness: Freshness;
}

export interface HomeFixture {
  gw: number;
  opponent: string;
  opponent_code: number;
  home: boolean;
  difficulty: number | null;
}

export interface HomePlayer {
  player_code: number;
  name: string;
  team: string;
  team_code: number;
  position: string;
  purchase_price: number;
  price: number;
  xp_next: number;
  p10_next: number;
  p90_next: number;
  p_start_next: number;
  xp_horizon: number;
  per_gw: { gw: number; xp: number; p10: number; p90: number; p_start: number }[];
  status: string;
  chance_of_playing: number | null;
  news: string;
  starter: boolean;
  bench_slot: number | null;
  p_rise: number | null;
  p_fall: number | null;
  fixtures: HomeFixture[];
  blank_next: boolean;
  double_next: boolean;
}

export interface HealthSignal {
  kind: string;
  severity: "bad" | "warn" | "info";
  players: number[];
  message: string;
  evidence: Record<string, unknown>;
  source: string;
}

export interface SquadAnalysis {
  version: string;
  computed_at: string;
  manager_key: string;
  state_id: string;
  snapshot_id: string;
  forecast_key: string;
  season: string;
  gameweek: number;
  horizon: number;
  state: {
    bank: number;
    free_transfers: number;
    manager_id: number | null;
    source: string;
    chips: { chip_id: string; status: string }[];
    squad_value: number;
  };
  players: HomePlayer[];
  lineup: { starters: number[]; bench: number[]; captain: number; vice_captain: number };
  lineup_expected_points: number;
  captaincy: {
    expected: number;
    safe: number;
    high_variance: number;
    options: { player_code: number; mean_total: number; p_captain_haul: number; p_beats_expected_choice: number }[];
  };
  totals: { xi_xp_next: number; bench_xp_next: number };
  health: HealthSignal[];
  sources: Record<string, string>;
}

export interface HomeData {
  season: string;
  gameweek: number;
  deadline: string;
  horizon_max: number;
  freshness: Freshness;
  state_id: string | null;
  snapshot_id?: string;
  analysis: SquadAnalysis | null;
  pending: boolean;
  stale: boolean;
  job_id?: string | null;
  recommendation: { id: string; decision: { action: string }; state_id?: string | null; stale_state?: boolean } | null;
}

export interface SquadState {
  state_id: string;
  state: {
    manager_id?: number | null; // the FPL entry this squad was imported from (null: manual)
    source?: string;
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
  /** why a failed job failed: its own code raised, or its worker was killed / lost / timed out */
  failure?: "error" | "interrupted" | null;
}

/** A failed job's message, telling an environmental interruption apart from a code error. */
export function jobFailureMessage(j: Job): string {
  if (j.failure === "interrupted")
    return `Interrupted before it finished (worker restarted, killed or timed out); nothing was saved — safe to run again. ${j.error ?? ""}`.trim();
  return `Failed: ${j.error ?? "job failed"}`;
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

export const del = <T,>(path: string) => api<T>(path, { method: "DELETE" });

export interface Notification {
  id: string;
  kind: "deadline" | "squad_change" | "price_risk" | "fixture_change" | "invalidation" | "post_gameweek";
  severity: "info" | "warning" | "critical";
  title: string;
  body: string;
  materiality: number;
  evidence: Record<string, unknown>;
  created_at: string;
  delivered_at: string | null;
  read_at: string | null;
}

export interface TraceCheck {
  check: string;
  ok: boolean;
  detail: string;
}

export interface Trace {
  recommendation_id: string;
  complete: boolean;
  checks: TraceCheck[];
  chain: Record<string, Record<string, unknown>>;
}
