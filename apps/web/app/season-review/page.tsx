"use client";

import Link from "next/link";
import { useState } from "react";

import { Badge, Card, State, useApi } from "@/components/ui";
import { when } from "@/lib/format";
import { loadSettings } from "@/lib/settings";

// ---------------------------------------------------------------- API shapes (GET /season-review)

type Interval = [number, number] | null;

interface Comparison {
  target?: string;
  statistic: string;
  baseline: string;
  baseline_label: string;
  G: number;
  mean: number | null;
  interval_95: Interval;
  interval_label: string | null;
  p_sign_flip: number | null;
  direction: { lower: number; higher: number; equal: number };
  per_gameweek: number[];
  primary: boolean;
}

interface Calibration {
  name: string;
  band: [number, number];
  per_gameweek: number[];
  mean: number;
  interval_90: Interval;
  interval_95: Interval;
}

interface LookResult {
  gameweeks: number[];
  comparisons: Record<string, { verdict: string; p_holm: number | null; interval_95: Interval }>;
  calibration: Record<string, string>;
}

interface Window {
  G: number;
  gameweeks: number[];
  rows?: number;
  status: string;
  levels?: {
    rmse: Record<string, number>;
    mae_model: number;
    bias_model: number;
    crps: { model: number; climatology: number };
    brier_6: { model: number; climatology: number };
    brier_start: { model: number; rate: number };
    minutes_rmse: { model: number; rate: number };
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
  };
  comparisons?: Record<string, Comparison>;
  calibration?: Record<string, Calibration>;
  looks?: Record<string, LookResult>;
  per_gameweek?: { gw: number; n: number; rmse_model: number; rmse_ppg: number; crps_model: number; crps_climatology: number; brier_6_model: number; pit80: number; citl_6: number; citl_start: number; events_6: number }[];
}

interface TimelineItem {
  gameweek: number;
  deadline: string;
  cutoff: string;
  first_kickoff: string | null;
  results_final_expected: string | null;
  state: "before_record" | "upcoming" | "due" | "prospective" | "live_unverified" | "missed";
  checkpoint_id: string | null;
  captured_at: string | null;
  promoted_at: string | null;
  reasons: string[];
  rows: number;
  prediction_run_id: string | null;
  build_revision: string | null;
}

interface RefSeason {
  G: number;
  rows: number;
  rmse: Record<string, number>;
  crps: { model: number; climatology: number };
  pit80: number;
  citl_6: number;
  citl_start: number;
  ece_6: number | null;
  comparisons: Record<string, { mean: number | null; direction: { lower: number; higher: number } }>;
}

interface SeasonReview {
  season: string;
  timeline: { record_started: string | null; gameweeks: TimelineItem[]; next_checkpoint: TimelineItem | null };
  evaluation: {
    id: number;
    seq: number;
    trigger: string;
    created_at: string;
    evaluator_version: string;
    data_snapshot_id: string;
    windows: { gw: number; state: string; scored: boolean; counts?: Record<string, number | string> }[];
    exclusions: { by_reason: Record<string, number>; clubs_gained_fixture: number; status_u_rows_h0: number };
    metrics: {
      results: Record<string, Record<string, Window>>;
      ece_null: Record<string, Record<string, { null_median: number; null_p95: number }>>;
      sensitivity: { schedule_changed_rows: number; rmse_as_served_h0_all: number | null };
    };
    changes: { newly_scored?: number[]; revised?: { gw: number; points_total: [number, number] }[]; exclusion_deltas?: Record<string, number> };
  } | null;
  reference: { all: { gameweeks: number[]; seasons: Record<string, RefSeason> }; regulars: { seasons: Record<string, RefSeason> }; generated: string } | null;
  reference_window_is_default: boolean;
  drift_signals: Record<string, number | string | null> | null;
  calibration: { state: string; reason: string; final_gameweeks: number; look?: number };
  method: { looks: number[]; margins: Record<string, { name: string; band: [number, number] }> };
}

interface JournalSummary {
  counts: Record<string, number>;
  actions: Record<string, number>;
  adherence: Record<string, number>;
  outcomes: {
    rows: { entry_id: string; gameweek: number; action: string; adherence: string | null; realised_points: number | null; expected_points: number | null; expected_range: [number | null, number | null]; range: string | null; difference_vs_hold: number | null; expected_difference_vs_hold: number | null; evaluation: { seq: number } }[];
    range_counts: { below: number; inside: number; above: number; n: number; typical_inside: [number, number] | null; past_seasons: { below: number; inside: number; above: number } };
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
  due: "being frozen",
  prospective: "frozen before kickoff",
  live_unverified: "captured, not verified",
  missed: "missed",
};
const STATE_TONE: Record<TimelineItem["state"], "good" | "warn" | "bad" | "neutral"> = {
  before_record: "neutral",
  upcoming: "neutral",
  due: "neutral",
  prospective: "good",
  live_unverified: "warn",
  missed: "bad",
};
const VERDICT: Record<string, string> = {
  lower_error: "lower error than the baseline",
  higher_error: "higher error than the baseline",
  no_clear_difference: "no clear difference",
  within_tolerance: "within tolerance",
  outside_tolerance: "outside tolerance",
  inconclusive: "inconclusive",
  too_few_gameweeks: "too few gameweeks to say",
};
const CALIBRATION_COPY: Record<string, string> = {
  deferred:
    "No calibration adjustment has been made. The forecasting method and its settings are unchanged; it re-fits on new results at every forecast, as it always has.",
  not_needed: "Every calibration check was within its tolerance at the latest formal look. Nothing has been changed.",
  review_suggested:
    "A calibration check was outside its tolerance at the latest formal look. Nothing changes automatically: any adjustment must first be tested in shadow mode on later live gameweeks.",
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

// ---------------------------------------------------------------- chart: difference vs baseline

/** One row per comparison: the mean per-gameweek difference (model − baseline; left of zero =
 * lower error) and its 95 % interval. Hollow marker when there is no interval yet. Inline SVG
 * with the theme tokens; the same numbers are in the table beside it. */
function DifferenceChart({ rows }: { rows: [string, Comparison][] }) {
  const W = 420;
  const rowH = 28;
  const pad = 150;
  const span = Math.max(1e-6, ...rows.flatMap(([, c]) => [Math.abs(c.mean ?? 0), ...(c.interval_95 ? c.interval_95.map(Math.abs) : [])]));
  // each comparison has its own unit; scale each row to its own span so all are readable
  const x = (v: number, s: number) => pad + ((v / s) * (W - pad - 20)) / 2 + (W - pad - 20) / 2;
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${rows.length * rowH + 24}`} role="img" aria-labelledby="diff-caption" style={{ maxWidth: 560 }}>
      {rows.map(([k, c], i) => {
        const s = Math.max(1e-6, Math.abs(c.mean ?? 0), ...(c.interval_95 ? c.interval_95.map(Math.abs) : []), span * 0);
        const y = i * rowH + 16;
        return (
          <g key={k}>
            <text x={0} y={y + 4} fill="var(--text)" fontSize={11}>{COMPARISON_LABEL[k] ?? k}</text>
            <line x1={pad} x2={W - 20} y1={y} y2={y} stroke="var(--line)" />
            <line x1={x(0, s)} x2={x(0, s)} y1={y - 9} y2={y + 9} stroke="var(--muted)" strokeDasharray="2 2" />
            {c.interval_95 ? <line x1={x(c.interval_95[0], s)} x2={x(c.interval_95[1], s)} y1={y} y2={y} stroke="var(--text)" strokeWidth={2} /> : null}
            {c.mean !== null ? (
              <circle cx={x(c.mean, s)} cy={y} r={4.5} fill={c.interval_95 ? "var(--text)" : "var(--bg)"} stroke="var(--text)" strokeWidth={1.5} />
            ) : null}
          </g>
        );
      })}
      <text x={pad} y={rows.length * rowH + 20} fill="var(--muted)" fontSize={10}>← lower error than baseline</text>
      <text x={W - 20} y={rows.length * rowH + 20} fill="var(--muted)" fontSize={10} textAnchor="end">higher error →</text>
    </svg>
  );
}

// ---------------------------------------------------------------- page

export default function SeasonReview() {
  const [settings] = useState(loadSettings);
  const r = useApi<SeasonReview>("/season-review");
  const mine = useApi<JournalSummary>(settings.managerKey ? `/journal/summary?manager_key=${encodeURIComponent(settings.managerKey)}` : null, [settings.managerKey]);
  const d = r.data;
  const live = d?.evaluation?.metrics.results.all?.h0;
  const liveRegulars = d?.evaluation?.metrics.results.regulars?.h0;
  const scored = live?.G ?? 0;
  const next = d?.timeline.next_checkpoint;
  return (
    <>
      <h1>Season Review</h1>
      <p className="small">
        How the live forecasts are doing this season, judged only on official results that are final, against forecasts frozen before
        kickoff. Each part says what evidence it is; they are never combined into one score.
      </p>
      <State loading={r.loading} error={r.error} />
      {d ? (
        <>
          {/* 1. status strip */}
          <div className="banner fresh" role="status" data-testid="season-status">
            <strong>
              Live record: {scored} of {FIRST_LOOK} gameweeks final
            </strong>{" "}
            (the first formal check needs {FIRST_LOOK}).
            {next ? (
              <>
                {" "}GW{next.gameweek} forecast {next.state === "due" ? "is being frozen" : `freezes ${when(next.cutoff)}`}
                {next.results_final_expected ? `; its results are expected to be final around ${when(next.results_final_expected)}` : ""}.
              </>
            ) : null}
            {d.evaluation ? (
              <div className="small">
                Evaluated {when(d.evaluation.created_at)} · evaluation #{d.evaluation.seq} ({d.evaluation.trigger.replaceAll("_", " ")}) ·{" "}
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
              The forecast Copilot served at each gameweek&apos;s decision cutoff (90 minutes before the deadline), frozen with proof it existed
              before the cutoff. Gameweeks before {d.timeline.record_started ? when(d.timeline.record_started) : "the record began"} were
              not recorded and are never reconstructed.
            </p>
            <Timeline items={d.timeline.gameweeks} />
            {!live || live.G === 0 ? (
              <p className="muted" data-testid="live-empty">
                No live gameweek is final yet. Results become final when FPL has checked them and the finalisation period has passed, usually
                three to four days after a gameweek&apos;s last match.
              </p>
            ) : (
              <LiveResults live={live} regulars={liveRegulars} evaluation={d.evaluation!} />
            )}
          </Card>

          {/* 3. past seasons */}
          <Card title="Past seasons 2023–26 — the seasons the model was built on" testId="past-seasons" wide>
            <PastSeasons review={d} />
          </Card>

          {/* 4. your recorded decisions */}
          <Card title="Your recorded decisions — private to you" testId="your-decisions" wide>
            <State loading={mine.loading} error={mine.error ? "Your decision summary is not available for this manager." : null} />
            {mine.data ? <Decisions s={mine.data} /> : null}
          </Card>

          {/* 5. calibration */}
          <Card title="Calibration" testId="calibration" wide>
            <p data-testid="calibration-state" data-state={d.calibration.state}>
              <Badge tone="neutral">{d.calibration.state.replace("_", " ")}</Badge> {CALIBRATION_COPY[d.calibration.state] ?? ""}
            </p>
            <p className="small">
              {d.calibration.reason}. A change can only be considered after {FIRST_LOOK} live gameweeks are final (the earliest is after GW14,
              around 10 December 2026), and then only after it has been tested on later live gameweeks without being served.
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

function WhatChanged({ changes }: { changes: NonNullable<SeasonReview["evaluation"]>["changes"] }) {
  const parts: string[] = [];
  if (changes.newly_scored?.length) parts.push(`GW${changes.newly_scored.join(", GW")} added`);
  for (const rv of changes.revised ?? []) parts.push(`GW${rv.gw} official results corrected (points total ${rv.points_total[0]} → ${rv.points_total[1]})`);
  for (const [k, v] of Object.entries(changes.exclusion_deltas ?? {})) parts.push(`${k.replaceAll("_", " ")} ${v > 0 ? "+" : ""}${v}`);
  return parts.length ? <div data-testid="what-changed">Since the previous evaluation: {parts.join("; ")}.</div> : null;
}

function Timeline({ items }: { items: TimelineItem[] }) {
  const shown = items.filter((i) => i.state !== "upcoming").concat(items.filter((i) => i.state === "upcoming").slice(0, 2));
  return (
    <table data-testid="timeline">
      <thead><tr><th>GW</th><th>Decision cutoff</th><th>Forecast record</th><th>Detail</th></tr></thead>
      <tbody>
        {shown.map((i) => (
          <tr key={i.gameweek} data-testid={`timeline-gw${i.gameweek}`} data-state={i.state}>
            <td>GW{i.gameweek}</td>
            <td className="small">{when(i.cutoff)}</td>
            <td><Badge tone={STATE_TONE[i.state]}>{STATE_LABEL[i.state]}</Badge></td>
            <td className="small">
              {i.state === "prospective" ? `${i.rows.toLocaleString()} player-gameweek forecasts · promoted ${when(i.promoted_at)} · frozen ${when(i.captured_at)}` : null}
              {i.reasons.length ? i.reasons.join("; ") : null}
              {i.state === "before_record" ? "the record had not started" : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LiveResults({ live, regulars, evaluation }: { live: Window; regulars?: Window; evaluation: NonNullable<SeasonReview["evaluation"]> }) {
  const primary = Object.entries(live.comparisons ?? {}).filter(([, c]) => c.primary) as [string, Comparison][];
  const lastLook = Object.keys(live.looks ?? {}).sort((a, b) => Number(b) - Number(a))[0];
  const look = lastLook ? live.looks![lastLook] : null;
  const lv = live.levels!;
  const ex = evaluation.exclusions;
  const nulls = evaluation.metrics.ece_null.all;
  return (
    <>
      <p className="small" data-testid="live-sample">
        {live.rows?.toLocaleString()} player-gameweeks in {live.G} gameweek{live.G === 1 ? "" : "s"} (GW{live.gameweeks.join(", GW")}), horizon 0
        (the next gameweek). Players in the same gameweek share fixtures and luck, so the unit of evidence is the gameweek, not the player.
        Excluded: {Object.entries(ex.by_reason).map(([k, v]) => `${v} ${k.replaceAll("_", " ")}`).join(", ") || "none"}.
      </p>
      <figure style={{ margin: "8px 0" }}>
        <DifferenceChart rows={primary} />
        <figcaption id="diff-caption" className="small">
          Difference from each predeclared baseline per gameweek (model − baseline; left of the dashed line = lower error). Filled marker
          with line: mean and 95 % interval over {live.G} gameweeks; hollow marker: too few gameweeks for an interval. Each row has its own
          scale.
        </figcaption>
      </figure>
      <table data-testid="comparisons">
        <thead><tr><th>Comparison</th><th>Baseline</th><th>Mean difference</th><th>95 % interval</th><th>Gameweeks with lower error</th><th>Verdict</th></tr></thead>
        <tbody>
          {primary.map(([k, c]) => (
            <tr key={k} data-testid={`comparison-${k}`}>
              <td>{COMPARISON_LABEL[k]}</td>
              <td className="small">{c.baseline_label}</td>
              <td>{c.mean === null ? "—" : c.mean.toFixed(4)}</td>
              <td className="small">{iv(c.interval_95, (v) => v.toFixed(4))}{c.interval_label ? ` (${c.interval_label})` : ""}</td>
              <td>{c.direction.lower} of {c.G}</td>
              <td>
                {look ? VERDICT[look.comparisons[k]?.verdict ?? ""] : live.G < FIRST_LOOK ? VERDICT.too_few_gameweeks : "monitoring"}
                {look ? <div className="small">at the check after {lastLook} gameweeks</div> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="small">
        Early-season windows overstate a full-season margin: in past seasons the margin over GW7–14 was 1.5–1.8× that over GW15–38.
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
              <td className="small">{iv(c.interval_90, (v) => (k === "K3" ? pct1(v) : v.toFixed(4)))}</td>
              <td>{look ? VERDICT[look.calibration[k] ?? ""] : live.G < FIRST_LOOK ? VERDICT.too_few_gameweeks : "monitoring"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="small">
        Outcomes below the 10th percentile: {pct1(lv.pit_below_10)}; above the 90th: {pct1(lv.pit_above_90)} (10 % each when the range is
        honest). ECE (descriptive, no verdict): P(6+) {f3(lv.ece_6)} against {f3(nulls?.p6?.null_median)} a perfectly calibrated forecast
        would typically show on these players (95th percentile {f3(nulls?.p6?.null_p95)}); P(start) {f3(lv.ece_start)} against{" "}
        {f3(nulls?.start?.null_median)} ({f3(nulls?.start?.null_p95)}). Events: {lv.events_6} hauls of 6+, {lv.events_start} starts.
      </p>
      <h3>Per gameweek (descriptive)</h3>
      <table data-testid="per-gameweek">
        <thead><tr><th>GW</th><th>Players</th><th>RMSE model / ppg × avail.</th><th>CRPS model / climatology</th><th>PIT-80</th><th>CITL P(6+)</th><th>CITL P(start)</th></tr></thead>
        <tbody>
          {(live.per_gameweek ?? []).map((g) => (
            <tr key={g.gw}>
              <td>GW{g.gw}</td><td>{g.n}</td><td>{f3(g.rmse_model)} / {f3(g.rmse_ppg)}</td><td>{f3(g.crps_model)} / {f3(g.crps_climatology)}</td>
              <td>{pct1(g.pit80)}</td><td>{f4s(g.citl_6)}</td><td>{f4s(g.citl_start)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {regulars?.levels ? (
        <p className="small">
          Regular starters only (start rate ≥ 50 %): {regulars.rows} player-gameweeks, RMSE {f3(regulars.levels.rmse.model)} (ppg × availability{" "}
          {f3(regulars.levels.rmse.ppg)}), rank correlation {f3(regulars.levels.spearman_mean)}, PIT-80 {pct1(regulars.levels.pit80)}.
        </p>
      ) : null}
      {evaluation.metrics.sensitivity.schedule_changed_rows ? (
        <p className="small">
          Including the {evaluation.metrics.sensitivity.schedule_changed_rows} rows whose fixtures moved after the forecast, scored on what
          actually happened: RMSE {f3(evaluation.metrics.sensitivity.rmse_as_served_h0_all)}.
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
        The same method applied to the walk-forward forecasts of 2023-24 to 2025-26{" "}
        {review.reference_window_is_default ? `over GW${gws[0]}–${gws[gws.length - 1]} (the weeks of the first live check)` : `over the same gameweeks as the live record (GW${gws.join(", GW")})`}. This is
        context, not a test: different seasons and players, and the design was iterated on these seasons (selection bias). Computed only on
        2023-26 data; reports last regenerated 2026-10-06; thresholds committed 2026-10-02.
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
        Strategy comparisons come from past seasons only (<Link href="/backtests">Backtest Lab</Link>); the engine&apos;s live decisions are not
        benchmarked this season.
      </p>
    </>
  );
}

function Decisions({ s }: { s: JournalSummary }) {
  const o = s.outcomes;
  const rc = o.range_counts;
  return (
    <>
      <p className="small" data-testid="decision-counts">
        {s.counts.recorded ?? 0} decisions recorded, {s.counts.active ?? 0} standing ({s.counts.replaced ?? 0} replaced by a later one);{" "}
        {s.counts.recorded_after_deadline ?? 0} recorded after the deadline (counted, never scored); {o.rows.length} with final results.
        How you used recommendations:{" "}
        {Object.entries(s.adherence).map(([k, v]) => `${k} ${v}`).join(", ") || "—"} (behaviour, not effectiveness).
      </p>
      {o.rows.length === 0 ? (
        <p className="muted" data-testid="decisions-empty">No recorded decision has final results yet.</p>
      ) : (
        <>
          <table data-testid="decision-rows">
            <thead><tr><th>GW</th><th>Action</th><th>Relation</th><th>Points</th><th>Expected (80 % range)</th><th>vs holding (expected)</th></tr></thead>
            <tbody>
              {o.rows.map((x) => (
                <tr key={x.entry_id}>
                  <td><Link href={`/journal/entry/${x.entry_id}`}>GW{x.gameweek}</Link></td>
                  <td>{x.action}</td>
                  <td className="small">{x.adherence ?? "—"}</td>
                  <td>{x.realised_points ?? "—"}</td>
                  <td className="small">{x.expected_points?.toFixed(1) ?? "—"} ({x.expected_range[0] ?? "—"}–{x.expected_range[1] ?? "—"}){x.range ? ` · ${x.range === "within" ? "inside" : x.range}` : ""}</td>
                  <td className="small">{x.difference_vs_hold ?? "—"} ({x.expected_difference_vs_hold?.toFixed(1) ?? "—"})</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="small">
            First gameweek only; later weeks of a transfer are not counted. Inside the 80 % range: {rc.inside} of {rc.n}
            {rc.typical_inside ? ` (with ${rc.n} decisions, ${rc.typical_inside[0]}–${rc.typical_inside[1]} inside is unsurprising)` : ""}; below{" "}
            {rc.below}, above {rc.above}. In past-season backtests the split was {pct1(rc.past_seasons.below)} below, {pct1(rc.past_seasons.inside)} inside,{" "}
            {pct1(rc.past_seasons.above)} above. {o.realised_minus_expected.label}: {o.realised_minus_expected.sum?.toFixed(1) ?? "—"} over{" "}
            {o.realised_minus_expected.n}. {o.vs_hold.label}: {o.vs_hold.sum ?? "—"} over {o.vs_hold.n} ({o.vs_hold.direction.positive} above,{" "}
            {o.vs_hold.direction.zero} equal, {o.vs_hold.direction.negative} below holding).{" "}
            {o.vs_hold.interval_95_mean ? `95 % interval of the mean: ${iv(o.vs_hold.interval_95_mean, (v) => v.toFixed(1))}.` : `${o.vs_hold.interval_note ?? ""}.`}
          </p>
        </>
      )}
      <ul className="clean small">{s.limits.map((l) => <li key={l}>{l}</li>)}</ul>
    </>
  );
}

function Method({ review }: { review: SeasonReview }) {
  return (
    <>
      <p className="small">
        Primary measures, named before any live result: squared error against ppg × availability, CRPS and P(6+) Brier against climatology,
        P(start) Brier against the start-rate baseline — at the next gameweek, for every registered player with a fixture. Intervals are
        t-intervals over gameweeks; a difference is only called at the formal checks after {review.method.looks.join(" and ")} final live
        gameweeks and at season end, when the interval excludes zero and an exact sign-flip test agrees (Holm-adjusted across the four).
        Calibration is judged by equivalence against tolerances fixed in advance. Rows are excluded, and counted, when a fixture moved, a player
        changed club or a result is missing — never scored as zero. &ldquo;A simple rule resembling FPL&apos;s expected points&rdquo; is a
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
      {review.drift_signals ? (
        <p className="small">
          Monitoring signals (descriptive, no action): RMSE ratio to past seasons over the same weeks{" "}
          {typeof review.drift_signals.rmse_ratio_to_past_same_weeks === "number" ? review.drift_signals.rmse_ratio_to_past_same_weeks.toFixed(3) : "—"} (signal above 1.15).
        </p>
      ) : null}
    </>
  );
}
