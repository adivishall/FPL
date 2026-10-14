"use client";

import Link from "next/link";
import { useState } from "react";

import { Badge, Card, State, useApi } from "@/components/ui";
import { loadSettings } from "@/lib/settings";

// ---------------------------------------------------------------- API shapes (GET /season-review)

type Interval = [number, number] | null;

interface Comparison {
  statistic: string;
  baseline: string;
  baseline_label: string;
  G: number;
  mean: number | null;
  interval_95: Interval;
  interval_label: string | null;
  p_sign_flip: number | null;
  direction: { lower: number; higher: number; equal: number };
  primary: boolean;
}

interface Calibration {
  name: string;
  band: [number, number];
  mean: number;
  interval_90: Interval;
  interval_label: string | null;
}

interface LookResult {
  gameweeks: number[];
  comparisons: Record<string, { verdict: string; p_holm: number | null; interval_95: Interval }>;
  calibration: Record<string, string>;
}

interface Levels {
  rmse: Record<string, number>;
  bias_model: number;
  crps: { model: number; climatology: number };
  brier_6: { model: number; climatology: number };
  brier_start: { model: number; rate: number };
  pit80: number;
  pit_below_10: number;
  pit_above_90: number;
  citl_6: number;
  citl_start: number;
  events_6: number;
  events_start: number;
  ece_6: number | null;
  ece_start: number | null;
  spearman_mean: number | null;
}

interface Window {
  G: number;
  gameweeks: number[];
  rows?: number;
  status: "too_few_gameweeks" | "monitoring" | "look" | "descriptive" | "no_scored_gameweeks";
  levels?: Levels;
  comparisons?: Record<string, Comparison>;
  calibration?: Record<string, Calibration>;
  looks?: Record<string, LookResult>;
  looks_waiting?: Record<string, number>;
  per_gameweek?: { gw: number; n: number; rmse_model: number; rmse_ppg: number; bias_model: number; crps_model: number; crps_climatology: number; pit80: number; citl_6: number; citl_start: number }[];
}

interface TimelineItem {
  gameweek: number;
  cutoff: string;
  results_final_expected: string | null;
  state: "before_record" | "upcoming" | "due" | "prospective" | "prospective_unverified" | "live_unverified" | "missed";
  captured_at: string | null;
  promoted_at: string | null;
  attested_at?: string | null;
  reasons: string[];
  rows: number;
}

interface EvalWindow { gw: number; state: string; scored: boolean; counts?: { relevant?: number; played?: number; final?: number; results_final?: number } }

interface Evaluation {
  seq: number;
  trigger: string;
  created_at: string;
  evaluator_version: string;
  windows: EvalWindow[];
  exclusions: { by_reason_h0?: Record<string, number>; clubs_gained_fixture_h0?: number; status_u_rows_h0: number };
  metrics: {
    results: Record<string, Record<string, Window>>;
    ece_null: Record<string, Record<string, { null_median: number; null_p95: number; observed: number | null; excess: number | null }>>;
    sensitivity: { schedule_changed_rows_h0?: number; as_served_rows_h0?: number; rmse_as_served_h0_all: number | null };
  };
  changes: {
    newly_scored?: number[];
    no_longer_scored?: number[];
    revised?: { gw: number; points_total: [number, number] }[];
    looks_issued?: string[];
    looks_revised?: { look: string; check: string; before: string; after: string }[];
    exclusion_deltas?: Record<string, number>;
  };
}

interface RefSeason { G: number; rmse: Record<string, number>; crps: { model: number; climatology: number }; pit80: number; citl_6: number; citl_start: number }

interface SeasonReview {
  season: string;
  timeline: {
    record_started: string | null;
    gameweeks: TimelineItem[];
    next_checkpoint: TimelineItem | null;
    current_forecast?: { promoted_at: string; gameweek: number; fpl_server_time: string | null } | null;
  };
  evaluation: Evaluation | null;
  reference: { all: { gameweeks: number[]; seasons: Record<string, RefSeason> }; generated: string } | null;
  reference_window_is_default: boolean;
  calibration: { state: string; reason: string; final_gameweeks: number; look?: number };
  method: { looks: number[] };
}

interface JournalRow {
  entry_id: string;
  gameweek: number;
  action: string;
  adherence: string | null;
  realised_points: number | null;
  expected_points: number | null;
  expected_range: [number | null, number | null];
  range: string | null;
  difference_vs_hold: number | null;
  expected_difference_vs_hold: number | null;
  comparable: boolean;
  evaluation: { seq: number };
}

interface JournalSummary {
  counts: Record<string, number>;
  actions: Record<string, number>;
  adherence: Record<string, number>;
  outcomes: {
    rows: JournalRow[];
    range_counts: { below: number; inside: number; above: number; n: number; typical_inside: [number, number] | null; past_seasons: { below: number; inside: number; above: number; weeks: number } | null; armband_changed_excluded: number };
    realised_minus_expected: { n: number; sum: number | null; label: string };
    vs_hold: { n: number; sum: number | null; direction: { positive: number; zero: number; negative: number }; interval_95_mean?: Interval; interval_note?: string; label: string };
  };
  limits: string[];
}

// ---------------------------------------------------------------- wording (contract §1, §6, §11)

const FIRST_LOOK = 8;
const STATE_LABEL: Record<TimelineItem["state"], string> = {
  before_record: "before the record",
  upcoming: "upcoming",
  due: "capture pending (may be recorded as missed)",
  prospective: "frozen before kickoff",
  prospective_unverified: "captured, timing not proven",
  live_unverified: "captured, not verified",
  missed: "missed",
};
const STATE_TONE: Record<TimelineItem["state"], "good" | "warn" | "bad" | "neutral"> = {
  before_record: "neutral",
  upcoming: "neutral",
  due: "neutral",
  prospective: "good",
  prospective_unverified: "warn",
  live_unverified: "warn",
  missed: "bad",
};
const WINDOW_LABEL: Record<string, string> = {
  not_started: "not started",
  in_progress: "in progress",
  provisional: "played, not yet final",
  results_not_final: "final at FPL, results not yet reloaded",
  final: "final",
};
const VERDICT: Record<string, string> = {
  lower_error: "lower error than the baseline",
  higher_error: "higher error than the baseline",
  no_clear_difference: "no clear difference",
  within_tolerance: "within tolerance",
  outside_tolerance: "outside tolerance",
  inconclusive: "inconclusive",
};
const CALIBRATION_COPY: Record<string, string> = {
  deferred:
    "No calibration adjustment has been made. The forecasting method and its settings are unchanged; it re-fits on new results at every forecast, as it always has.",
  not_needed: "Every calibration check was within its tolerance at the latest formal check. Nothing has been changed.",
  review_suggested:
    "A calibration check was outside its tolerance at the latest formal check. Nothing changes automatically: any adjustment must first be tested on later live gameweeks without being shown to anyone.",
};
const COMPARISON_LABEL: Record<string, string> = {
  C1: "Expected points (squared error)",
  C2: "Points distribution (CRPS)",
  C3: "P(6+ points) (Brier)",
  C4: "P(start) (Brier)",
};
const UI_NUMBERS: [string, string][] = [
  ["Expected points", "evaluated: squared-error comparison with ppg × availability; RMSE and bias"],
  ["Points range (p10–p90)", "evaluated: PIT central-80 coverage and its tails (the inclusive range over-covers whole-number points)"],
  ["P(6+ points)", "evaluated: Brier against climatology, calibration-in-the-large, ECE"],
  ["P(start)", "evaluated: Brier against the start-rate baseline, calibration-in-the-large, ECE"],
  ["P(2+), P(10+)", "descriptive Brier only (10+ points is rare)"],
  ["P(play)", "not evaluated: no realised appearance target is stored separately from minutes"],
  ["Expected minutes", "secondary: RMSE against the rate baseline"],
  ["Component expected points", "not evaluated this season (needs a realised-component breakdown); stored with every record"],
  ["Price changes, plan gains, chips", "not evaluated here"],
];

const f3 = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v.toFixed(3));
const f4s = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(4)}`);
const pct1 = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
const iv = (i: Interval, f: (v: number) => string = (v) => v.toFixed(3)) => (i ? `[${f(i[0])}, ${f(i[1])}]` : "—");
/** Times in UTC, labelled: deadlines and cutoffs are defined in UTC. */
const utc = (iso: string | null | undefined) =>
  iso
    ? `${new Date(iso).toLocaleString("en-GB", { timeZone: "UTC", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} UTC`
    : "—";

/** The verdict a check carries now: only at a formal look; between looks "monitoring". */
function verdictNow(live: Window, pick: (l: LookResult) => string | undefined): string {
  if (live.status === "look") {
    const at = live.looks?.[String(live.G)];
    const v = at ? pick(at) : undefined;
    return v ? VERDICT[v] ?? v : "—";
  }
  if (live.status === "monitoring") return "monitoring (between formal checks)";
  return "too few gameweeks to say";
}

// ---------------------------------------------------------------- chart: difference vs baseline

/** Each comparison's mean per-gameweek difference and 95 % interval, as a percentage of the
 * baseline's own error over the same gameweeks (a common, disclosed unit; left of zero = lower
 * error). Only comparisons with an interval are drawn. Inline SVG with the theme tokens; the
 * same numbers are in the table below. */
function DifferenceChart({ rows }: { rows: { key: string; mean: number; lo: number; hi: number }[] }) {
  const W = 460;
  const rowH = 30;
  const pad = 160;
  const right = 20;
  const raw = Math.max(0.01, ...rows.flatMap((r) => [Math.abs(r.lo), Math.abs(r.hi), Math.abs(r.mean)]));
  const nice = [0.05, 0.1, 0.2, 0.3, 0.5, 1, 2].find((v) => v >= raw) ?? Math.ceil(raw);
  const x = (v: number) => pad + ((v + nice) / (2 * nice)) * (W - pad - right);
  const ticks = [-nice, -nice / 2, 0, nice / 2, nice];
  const h = rows.length * rowH + 34;
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${h}`} role="img" aria-labelledby="diff-caption" style={{ maxWidth: 600 }}>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={x(t)} x2={x(t)} y1={6} y2={rows.length * rowH + 8} stroke={t === 0 ? "var(--muted)" : "var(--line)"} strokeDasharray={t === 0 ? "3 3" : undefined} />
          <text x={x(t)} y={rows.length * rowH + 22} fill="var(--muted)" fontSize={10} textAnchor="middle">{`${t > 0 ? "+" : ""}${Math.round(t * 100)}%`}</text>
        </g>
      ))}
      {rows.map((r, i) => {
        const y = i * rowH + 20;
        return (
          <g key={r.key}>
            <text x={0} y={y + 4} fill="var(--text)" fontSize={11}>{COMPARISON_LABEL[r.key] ?? r.key}</text>
            <line x1={x(r.lo)} x2={x(r.hi)} y1={y} y2={y} stroke="var(--text)" strokeWidth={2} />
            <circle cx={x(r.mean)} cy={y} r={4.5} fill="var(--text)" />
          </g>
        );
      })}
      <text x={pad} y={h - 2} fill="var(--muted)" fontSize={10}>← lower error than the baseline</text>
      <text x={W - right} y={h - 2} fill="var(--muted)" fontSize={10} textAnchor="end">higher error →</text>
    </svg>
  );
}

// ---------------------------------------------------------------- page

export default function SeasonReview() {
  const [settings] = useState(loadSettings);
  const r = useApi<SeasonReview>("/season-review");
  const d = r.data;
  const live = d?.evaluation?.metrics.results.all?.h0;
  const scored = live?.G ?? 0;
  const next = d?.timeline.next_checkpoint;
  return (
    <>
      <h1>Season Review</h1>
      <p className="small">
        How this season&apos;s live forecasts are doing, judged only on official results that are final, against forecasts frozen before
        kickoff. Each part says what evidence it is; they are never combined into one score.
      </p>
      <State loading={r.loading} error={r.error} />
      {d ? (
        <>
          {/* 1. status strip */}
          <div className="banner fresh" role="status" data-testid="season-status">
            <strong>
              {scored < FIRST_LOOK
                ? `Live record: ${scored} of ${FIRST_LOOK} gameweeks final`
                : `Live record: ${scored} gameweeks final`}
            </strong>{" "}
            {scored < FIRST_LOOK
              ? `(the first formal check needs ${FIRST_LOOK}).`
              : `(formal checks after ${d.method.looks.join(" and ")} gameweeks and at season end).`}
            {next ? (
              <>
                {" "}GW{next.gameweek}: {next.state === "due" ? "capture pending" : `forecast freezes at ${utc(next.cutoff)}`}
                {next.results_final_expected ? `; results expected to be final around ${utc(next.results_final_expected)}` : ""}.
              </>
            ) : null}
            {next && next.state === "upcoming" && d.timeline.current_forecast ? (
              <div className="small" data-testid="current-forecast-time">
                {d.timeline.current_forecast.fpl_server_time
                  ? `The current forecast existed by ${utc(d.timeline.current_forecast.fpl_server_time)} by FPL's server clock.`
                  : "FPL's server clock has not yet confirmed when the current forecast existed; until it does, it cannot count as frozen before kickoff."}
              </div>
            ) : null}
            {d.evaluation ? (
              <div className="small">
                Evaluated {utc(d.evaluation.created_at)} · evaluation #{d.evaluation.seq} ({d.evaluation.trigger.replaceAll("_", " ")}) ·{" "}
                <code>{d.evaluation.evaluator_version}</code>
                <WhatChanged changes={d.evaluation.changes} />
              </div>
            ) : (
              <div className="small">No evaluation yet: nothing has been frozen to evaluate.</div>
            )}
          </div>

          {/* 2. live record */}
          <Card title="Live record — forecast frozen before kickoff" testId="live-record" wide>
            <p className="small">
              The forecast Copilot served at each gameweek&apos;s decision cutoff (90 minutes before the deadline). Its file hash was
              recorded when it was published, and FPL&apos;s own server clock (not this computer&apos;s) must show the file existed before the
              cutoff; the frozen forecast is checked against that hash, and its data must contain no FPL news stamped after the cutoff and no
              result of the gameweek. Without FPL&apos;s time it is shown as &ldquo;timing not proven&rdquo; and never scored. Gameweeks before{" "}
              {d.timeline.record_started ? utc(d.timeline.record_started) : "the record began"} were not recorded and are never reconstructed.
            </p>
            <Timeline items={d.timeline.gameweeks} />
            {d.evaluation?.windows.length ? <Windows windows={d.evaluation.windows} /> : null}
            {!live || live.G === 0 ? (
              <p className="muted" data-testid="live-empty">
                No live gameweek is final yet. Results become final when FPL has checked them and the finalisation period has passed, usually
                three to four days after a gameweek&apos;s last match.
              </p>
            ) : (
              <LiveResults live={live} evaluation={d.evaluation!} />
            )}
          </Card>

          {/* 3. past seasons */}
          <Card title="Past seasons 2023–26 — the seasons the model was built on" testId="past-seasons" wide>
            <PastSeasons review={d} />
          </Card>

          {/* 4. your recorded decisions */}
          <Card title="Your recorded decisions — private to you" testId="your-decisions" wide>
            <YourDecisions managerKey={settings.managerKey} season={d.season} />
          </Card>

          {/* 5. calibration */}
          <Card title="Calibration" testId="calibration" wide>
            <p data-testid="calibration-state" data-state={d.calibration.state}>
              <Badge tone="neutral">{d.calibration.state.replace("_", " ")}</Badge> {CALIBRATION_COPY[d.calibration.state] ?? ""}
            </p>
            <p className="small">
              {d.calibration.reason}. <EarliestCheck review={d} />
            </p>
          </Card>

          {/* 6. how these are computed */}
          <Card title="How these are computed" testId="method" wide>
            <Method review={d} />
          </Card>
        </>
      ) : null}
    </>
  );
}

function EarliestCheck({ review }: { review: SeasonReview }) {
  const g = review.evaluation?.metrics.results.all?.h0?.G ?? 0;
  if (g >= FIRST_LOOK) {
    return <>Calibration changes are considered only at the formal checks, and only after a test on later live gameweeks without being shown to anyone.</>;
  }
  const first = review.timeline.gameweeks.find((i) => i.state !== "before_record" && i.state !== "missed");
  if (!first) return <>The first formal check needs {FIRST_LOOK} final live gameweeks.</>;
  const target = review.timeline.gameweeks.find((i) => i.gameweek === first.gameweek + FIRST_LOOK - 1);
  return (
    <>
      A change can only be considered after {FIRST_LOOK} live gameweeks are final — at the earliest after GW{first.gameweek + FIRST_LOOK - 1}
      {target?.results_final_expected ? ` (around ${utc(target.results_final_expected)})` : ""} if no gameweek is missed — and then only after a test on
      later live gameweeks without being shown to anyone.
    </>
  );
}

function WhatChanged({ changes }: { changes: Evaluation["changes"] }) {
  const parts: string[] = [];
  if (changes.newly_scored?.length) parts.push(`GW${changes.newly_scored.join(", GW")} added`);
  if (changes.no_longer_scored?.length) parts.push(`GW${changes.no_longer_scored.join(", GW")} no longer scored`);
  for (const rv of changes.revised ?? []) parts.push(`GW${rv.gw} official results corrected (points total ${rv.points_total[0]} → ${rv.points_total[1]})`);
  for (const l of changes.looks_issued ?? []) parts.push(`formal check after ${l} gameweeks issued`);
  for (const l of changes.looks_revised ?? []) parts.push(`check after ${l.look} gameweeks, ${l.check}: ${VERDICT[l.before] ?? l.before} → ${VERDICT[l.after] ?? l.after}`);
  for (const [k, v] of Object.entries(changes.exclusion_deltas ?? {})) parts.push(`${k.replaceAll("_", " ")} ${v > 0 ? "+" : ""}${v}`);
  return parts.length ? <div data-testid="what-changed">Since the previous evaluation: {parts.join("; ")}.</div> : null;
}

function Timeline({ items }: { items: TimelineItem[] }) {
  const shown = items.filter((i) => i.state !== "upcoming").concat(items.filter((i) => i.state === "upcoming").slice(0, 2));
  return (
    <table data-testid="timeline">
      <thead><tr><th>GW</th><th>Decision cutoff</th><th>Forecast record</th><th>Detail</th></tr></thead>
      <tbody>
        {shown.map((i) => {
          const unscored = i.reasons.some((r) => r.startsWith("baselines_unavailable"));
          return (
            <tr key={i.gameweek} data-testid={`timeline-gw${i.gameweek}`} data-state={i.state}>
              <td>GW{i.gameweek}</td>
              <td className="small">{utc(i.cutoff)}</td>
              <td>
                <Badge tone={unscored ? "warn" : STATE_TONE[i.state]}>{STATE_LABEL[i.state]}{unscored ? " — not scored (no baselines)" : ""}</Badge>
              </td>
              <td className="small">
                {i.state === "prospective"
                  ? `${i.rows.toLocaleString()} player-gameweek forecasts · existed by ${utc(i.attested_at)} (FPL server time) · captured ${utc(i.captured_at)}`
                  : null}
                {i.reasons.length ? `${i.state === "prospective" ? " · " : ""}${i.reasons.join("; ")}` : null}
                {i.state === "before_record" ? "the record had not started" : null}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Windows({ windows }: { windows: EvalWindow[] }) {
  return (
    <p className="small" data-testid="windows">
      Results of the forecast gameweeks:{" "}
      {windows
        .map((w) => {
          const c = w.counts ?? {};
          const detail = w.state === "final" ? "" : ` (${c.played ?? 0} of ${c.relevant ?? 0} fixtures played, ${c.results_final ?? 0} with final results)`;
          return `GW${w.gw} ${WINDOW_LABEL[w.state] ?? w.state}${detail}`;
        })
        .join("; ")}
      . A gameweek is scored only once all of its results are final.
    </p>
  );
}

function LiveResults({ live, evaluation }: { live: Window; evaluation: Evaluation }) {
  const comps = live.comparisons ?? {};
  const primary = (["C1", "C2", "C3", "C4"] as const).filter((k) => comps[k]);
  const lv = live.levels!;
  const ex = evaluation.exclusions;
  const nulls = evaluation.metrics.ece_null.all;
  const regulars = evaluation.metrics.results.regulars?.h0;
  const baseLevel: Record<string, number> = {
    C1: (lv.rmse.ppg ?? 0) ** 2,
    C2: lv.crps.climatology,
    C3: lv.brier_6.climatology,
    C4: lv.brier_start.rate,
  };
  const charted = primary
    .filter((k) => comps[k]!.interval_95 && comps[k]!.mean !== null && baseLevel[k]! > 0)
    .map((k) => ({ key: k, mean: comps[k]!.mean! / baseLevel[k]!, lo: comps[k]!.interval_95![0] / baseLevel[k]!, hi: comps[k]!.interval_95![1] / baseLevel[k]! }));
  const looks = Object.entries(live.looks ?? {}).sort(([a], [b]) => Number(a) - Number(b));
  const waiting = Object.entries(live.looks_waiting ?? {});
  return (
    <>
      <p className="small" data-testid="live-sample">
        Next-gameweek forecasts: {live.rows?.toLocaleString()} player-gameweeks in {live.G} gameweek{live.G === 1 ? "" : "s"} (GW{live.gameweeks.join(", GW")}),
        of which {ex.status_u_rows_h0} were players listed as unavailable (status u). Players in the same gameweek share fixtures and luck, so the
        unit of evidence is the gameweek, not the player. Excluded and counted:{" "}
        {Object.entries(ex.by_reason_h0 ?? {}).map(([k, v]) => `${v} ${k.replaceAll("_", " ")}`).join(", ") || "none"}
        {ex.clubs_gained_fixture_h0 ? `; ${ex.clubs_gained_fixture_h0} clubs gained a fixture after the forecast` : ""}.
      </p>
      <p className="small" data-testid="live-status">
        Status: <strong>{live.status === "look" ? `formal check after ${live.G} gameweeks` : live.status === "monitoring" ? "monitoring (between formal checks)" : "too few gameweeks to say"}</strong>
        {waiting.length ? ` · the check after ${waiting[0]![0]} gameweeks waits for GW${waiting[0]![1]}'s final results` : ""}
      </p>
      {charted.length ? (
        <figure style={{ margin: "8px 0" }}>
          <DifferenceChart rows={charted} />
          <figcaption id="diff-caption" className="small">
            Difference from each predeclared baseline over {live.G} gameweeks, as a percentage of the baseline&apos;s own error: dot = mean of the
            per-gameweek differences, line = 95 % interval. An interval that lies left of zero means lower error than the baseline in these
            weeks; a verdict is only given at the formal checks. Comparisons without an interval yet are not drawn.
          </figcaption>
        </figure>
      ) : (
        <p className="small muted">No interval yet: intervals need at least three final gameweeks (and enough events for the probabilities).</p>
      )}
      <table data-testid="comparisons">
        <thead><tr><th>Comparison</th><th>Baseline</th><th>Mean difference</th><th>95 % interval</th><th>Gameweeks with lower error</th><th>Regular starters (mean)</th><th>Verdict</th></tr></thead>
        <tbody>
          {primary.map((k) => {
            const c = comps[k]!;
            return (
              <tr key={k} data-testid={`comparison-${k}`}>
                <td>{COMPARISON_LABEL[k]}</td>
                <td className="small">{c.baseline_label}</td>
                <td>{c.mean === null ? "—" : c.mean.toFixed(4)}</td>
                <td className="small">{iv(c.interval_95, (v) => v.toFixed(4))}{c.interval_label ? ` (${c.interval_label})` : ""}</td>
                <td>{c.direction.lower} of {c.G}</td>
                <td className="small">{regulars?.comparisons?.[k]?.mean?.toFixed(4) ?? "—"}</td>
                <td>{verdictNow(live, (l) => l.comparisons[k]?.verdict)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {looks.length ? (
        <p className="small" data-testid="looks">
          Formal checks so far:{" "}
          {looks.map(([at, l]) => `after ${at} gameweeks (GW${l.gameweeks[0]}–GW${l.gameweeks[l.gameweeks.length - 1]}): ${primary.map((k) => `${k} ${VERDICT[l.comparisons[k]?.verdict ?? ""] ?? "—"}`).join(", ")}`).join("; ")}.
        </p>
      ) : null}
      <p className="small">
        Expected points: RMSE {f3(lv.rmse.model)} against {f3(lv.rmse.ppg)} for ppg × availability; average error (forecast − points){" "}
        {f4s(lv.bias_model)}. In past seasons the squared-error margin over GW7–14 was 1.5–1.8× that over the rest of the season, so an early
        window overstates the full-season margin for expected points (not for the other three).
      </p>
      <h3>Are the probabilities and ranges honest?</h3>
      <table data-testid="calibration-checks">
        <thead><tr><th>Check</th><th>Live value</th><th>Tolerance</th><th>90 % interval</th><th>Verdict</th></tr></thead>
        <tbody>
          {Object.entries(live.calibration ?? {}).map(([k, c]) => (
            <tr key={k}>
              <td>{c.name}</td>
              <td>{k === "K3" ? pct1(c.mean) : f4s(c.mean)}</td>
              <td className="small">{k === "K3" ? `${pct1(c.band[0])} – ${pct1(c.band[1])}` : `±${c.band[1]}`}</td>
              <td className="small">{c.interval_90 ? iv(c.interval_90, (v) => (k === "K3" ? pct1(v) : v.toFixed(4))) : c.interval_label ?? "—"}</td>
              <td>{verdictNow(live, (l) => l.calibration[k])}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="small">
        Outcomes below the 10th percentile: {pct1(lv.pit_below_10)}; above the 90th: {pct1(lv.pit_above_90)} (10 % each when the range is
        honest). ECE (descriptive, no verdict): P(6+) {f3(lv.ece_6)}; a perfectly calibrated forecast would typically show{" "}
        {f3(nulls?.p6?.null_median)} on these players (95th percentile {f3(nulls?.p6?.null_p95)}); P(start) {f3(lv.ece_start)} against{" "}
        {f3(nulls?.start?.null_median)} ({f3(nulls?.start?.null_p95)}). These bands assume independent outcomes and are a lower bound. Events: {lv.events_6} hauls
        of 6+, {lv.events_start} starts. About half the players rarely play, which makes the all-player checks easier to pass; the regular
        starters&apos; values are in the per-gameweek detail.
      </p>
      <h3>Per gameweek (descriptive)</h3>
      <table data-testid="per-gameweek">
        <thead><tr><th>GW</th><th>Players</th><th>RMSE model / ppg × avail.</th><th>Bias</th><th>CRPS model / climatology</th><th>PIT-80</th><th>CITL P(6+)</th><th>CITL P(start)</th></tr></thead>
        <tbody>
          {(live.per_gameweek ?? []).map((g) => (
            <tr key={g.gw}>
              <td>GW{g.gw}</td><td>{g.n}</td><td>{f3(g.rmse_model)} / {f3(g.rmse_ppg)}</td><td>{f4s(g.bias_model)}</td><td>{f3(g.crps_model)} / {f3(g.crps_climatology)}</td>
              <td>{pct1(g.pit80)}</td><td>{f4s(g.citl_6)}</td><td>{f4s(g.citl_start)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {regulars?.levels ? (
        <p className="small">
          Regular starters (start rate ≥ 50 %): {regulars.rows} player-gameweeks, RMSE {f3(regulars.levels.rmse.model)} (ppg × availability{" "}
          {f3(regulars.levels.rmse.ppg)}), rank correlation {f3(regulars.levels.spearman_mean)}, PIT-80 {pct1(regulars.levels.pit80)} — descriptive.
        </p>
      ) : null}
      {evaluation.metrics.sensitivity.schedule_changed_rows_h0 ? (
        <p className="small">
          {evaluation.metrics.sensitivity.schedule_changed_rows_h0} next-gameweek rows were excluded because fixtures moved after the forecast.
          Scoring them on what those players actually got ({evaluation.metrics.sensitivity.as_served_rows_h0} of them known, a postponement counting as
          0 points) gives RMSE {f3(evaluation.metrics.sensitivity.rmse_as_served_h0_all)}.
        </p>
      ) : null}
    </>
  );
}

function PastSeasons({ review }: { review: SeasonReview }) {
  const ref = review.reference;
  if (!ref) return <p className="muted">The past-season reference is not available.</p>;
  const gws = ref.all.gameweeks;
  return (
    <>
      <p className="small">
        The walk-forward forecasts of 2023-24 to 2025-26{" "}
        {review.reference_window_is_default ? `over GW${gws[0]}–${gws[gws.length - 1]} (the weeks of the first live check)` : `over the same gameweeks as the live record (GW${gws.join(", GW")})`},
        summarised per gameweek in the same way as the live record. Two estimators differ: past seasons use the published randomised PIT and
        the CRPS with self-pairs. This is context, not a test: different seasons and players, and the design was iterated on these seasons
        (selection bias). Computed only on 2023-26 data (summarised {utc(ref.generated)}; forecasts published 2026-10-06; thresholds committed
        2026-10-02).
      </p>
      <table data-testid="reference">
        <thead><tr><th>Season</th><th>Gameweeks</th><th>RMSE model / ppg × avail.</th><th>CRPS model / climatology</th><th>PIT-80</th><th>CITL P(6+)</th><th>CITL P(start)</th></tr></thead>
        <tbody>
          {Object.entries(ref.all.seasons).map(([s, v]) => (
            <tr key={s}>
              <td>{s}</td><td>{v.G}</td><td>{f3(v.rmse.model)} / {f3(v.rmse.ppg)}</td><td>{f3(v.crps.model)} / {f3(v.crps.climatology)}</td>
              <td>{pct1(v.pit80)}</td><td>{f4s(v.citl_6)}</td><td>{f4s(v.citl_start)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="small">
        Full past-season reports: <Link href="/backtests">Backtest Lab</Link> (forecast evaluation and the strategy backtest). The engine&apos;s live
        decisions are not benchmarked this season; strategy comparisons come from past seasons only.
      </p>
    </>
  );
}

function YourDecisions({ managerKey, season }: { managerKey: string; season: string }) {
  const s = useApi<JournalSummary>(managerKey ? `/journal/summary?manager_key=${encodeURIComponent(managerKey)}&season=${encodeURIComponent(season)}` : null, [managerKey, season]);
  if (!managerKey) return <p className="muted">Choose your manager in Settings to see your recorded decisions.</p>;
  if (s.error) {
    const msg = s.error.startsWith("401")
      ? "Sign in to see your recorded decisions."
      : s.error.startsWith("403")
        ? "The manager selected in Settings does not belong to your account."
        : `Your recorded decisions could not be loaded (${s.error}).`;
    return <p className="muted" role="alert">{msg}</p>;
  }
  if (s.loading || !s.data) return <State loading />;
  const sum = s.data;
  const o = sum.outcomes;
  const rc = o.range_counts;
  const c = sum.counts;
  const r1 = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v.toFixed(1));
  return (
    <>
      <p className="small" data-testid="decision-counts">
        {c.recorded ?? 0} decisions recorded this season, {c.active ?? 0} standing ({c.replaced ?? 0} replaced by a later one); results: {c.evaluation_final ?? 0} final,{" "}
        {c.evaluation_pending ?? 0} pending, {c.evaluation_partial ?? 0} partial, {c.evaluation_unavailable ?? 0} unavailable;{" "}
        {c.recorded_after_deadline ?? 0} recorded after the deadline (counted, never scored). Actions:{" "}
        {Object.entries(sum.actions).map(([k, v]) => `${k} ${v}`).join(", ") || "—"}. How you used recommendations:{" "}
        {Object.entries(sum.adherence).map(([k, v]) => `${k} ${v}`).join(", ") || "—"} (behaviour, not effectiveness).
      </p>
      {o.rows.length === 0 ? (
        <p className="muted" data-testid="decisions-empty">No decision recorded before its deadline has final results yet.</p>
      ) : (
        <>
          <table data-testid="decision-rows">
            <thead><tr><th>GW</th><th>Action</th><th>Relation</th><th>Points</th><th>Expected when you decided (80 % range)</th><th>vs holding: actual (expected)</th><th>Evaluation</th></tr></thead>
            <tbody>
              {o.rows.map((x) => (
                <tr key={x.entry_id}>
                  <td><Link href={`/journal/entry/${x.entry_id}`}>GW{x.gameweek}</Link></td>
                  <td>{x.action}</td>
                  <td className="small">{x.adherence ?? "—"}</td>
                  <td>{x.realised_points ?? "—"}</td>
                  <td className="small">
                    {x.comparable
                      ? `${r1(x.expected_points)} (${r1(x.expected_range[0])}–${r1(x.expected_range[1])})${x.range ? ` · ${x.range === "within" ? "inside" : x.range}` : ""}`
                      : "not compared: you changed the armband from the plan's"}
                  </td>
                  <td className="small">{x.difference_vs_hold ?? "—"} ({x.comparable ? r1(x.expected_difference_vs_hold) : "—"})</td>
                  <td className="small">#{x.evaluation.seq}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="small">
            First gameweek only; later weeks of a transfer are not counted. Inside the 80 % range: {rc.inside} of {rc.n}
            {rc.typical_inside ? ` (with ${rc.n} decisions, ${rc.typical_inside[0]}–${rc.typical_inside[1]} inside is unsurprising)` : ""}; below {rc.below},
            above {rc.above}
            {rc.armband_changed_excluded ? `; ${rc.armband_changed_excluded} with a changed armband not compared` : ""}.
            {rc.past_seasons
              ? ` In past-season backtests (${rc.past_seasons.weeks} weeks) the split was ${pct1(rc.past_seasons.below)} below, ${pct1(rc.past_seasons.inside)} inside, ${pct1(rc.past_seasons.above)} above.`
              : ""}{" "}
            {o.realised_minus_expected.label}: {r1(o.realised_minus_expected.sum)} over {o.realised_minus_expected.n}. {o.vs_hold.label}: {r1(o.vs_hold.sum)} over{" "}
            {o.vs_hold.n} ({o.vs_hold.direction.positive} above, {o.vs_hold.direction.zero} equal, {o.vs_hold.direction.negative} below holding).{" "}
            {o.vs_hold.interval_95_mean ? `95 % interval of the mean: ${iv(o.vs_hold.interval_95_mean, (v) => v.toFixed(1))}.` : `${o.vs_hold.interval_note ?? ""}.`}
          </p>
        </>
      )}
      <ul className="clean small">{sum.limits.map((l) => <li key={l}>{l}</li>)}</ul>
    </>
  );
}

function Method({ review }: { review: SeasonReview }) {
  return (
    <>
      <p className="small">
        Primary measures, named before any live result: squared error against ppg × availability, CRPS and P(6+) Brier against climatology,
        P(start) Brier against the start-rate baseline — at the next gameweek, for every registered player with a fixture. Intervals are
        t-intervals over gameweeks (from three gameweeks, with minimum rows and events); a difference is only called at the formal checks after{" "}
        {review.method.looks.join(" and ")} final live gameweeks and at season end — each check waits until every earlier gameweek is final — when
        the interval excludes zero and an exact sign-flip test agrees (Holm-adjusted across the four). These assume independent gameweeks; form
        and injuries carry over between weeks, so the intervals may be somewhat narrow. Calibration is judged by equivalence against tolerances
        fixed in advance. Rows are excluded, and counted, when a fixture moved, a player changed club or a result is missing — never scored as
        zero; those exclusions are not random (postponements follow cup runs). &ldquo;A simple rule resembling FPL&apos;s expected points&rdquo; is a
        baseline Copilot computes; FPL&apos;s own expected points are not used.
      </p>
      <table data-testid="ui-numbers">
        <thead><tr><th>Number shown in Copilot</th><th>How it is evaluated here</th></tr></thead>
        <tbody>{UI_NUMBERS.map(([n, how]) => <tr key={n}><td>{n}</td><td className="small">{how}</td></tr>)}</tbody>
      </table>
      <p className="small">
        Known open findings, unaffected by this page: cross-machine reproducibility of the engine (defect 52) and the inherited club-limit
        formulation in the optimiser (defect 53; illegal plans are rejected independently). Full method:{" "}
        <code>docs/architecture/season-evaluation.md</code>.
      </p>
    </>
  );
}
