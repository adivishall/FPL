# Season evaluation, live-season calibration and decision quality (M1.5)

This note is the evaluation contract. It was committed on 2026-10-14. That is before the first
2026-27 decision cutoff the forecast record can capture (GW7: 2026-10-17 08:30 UTC), and before
any live outcome was scored.

It fixes:

* the questions;
* the evidence classes;
* the data and leakage rules;
* the populations, metrics and primary comparisons;
* the statistical method and the wording of verdicts;
* the calibration policy;
* what the Season Review page may show.

The code follows it:

* `fpl_api.ledger`: the forecast record;
* `fpl_forecasting.season_eval`: the statistics;
* `fpl_api.season`: the evaluations, the API and the jobs;
* the `/season-review` page.

A change to any rule here is a new evaluator version, recorded with every result it produces.
§15 records how each finding of the independent review was resolved.

## 0. Questions (fixed now)

| # | Question | Answered from | Primary measure |
|---|---|---|---|
| Q1 | Are the live forecasts more accurate than simple baselines? | Live record | per-gameweek paired differences, §5–§6 |
| Q2 | Are the probabilities and ranges shown honest? | Live record | calibration-in-the-large and PIT-80 coverage, §6 |
| Q3 | Is the live record in line with past seasons over the same weeks? | Live record beside past seasons (descriptive) | same-window reference, §7 |
| Q4 | Does the engine beat simple strategies? | Past seasons only (published backtest) | §10; nothing new is claimed this season |
| Q5 | How did my recorded decisions turn out against what was expected when I made them? | Your recorded decisions (private) | per-decision table, §9 |
| Q6 | Has anything been recalibrated? | Calibration state | §11; in M1.5 the answer is no |

## 1. Evidence classes

Every number carries exactly one label, and labels are never pooled. The page uses the
user-facing names below. Class letters and the word "prospective" never appear in UI copy.

### Past seasons 2023–26 — the seasons the model was built on

What it is:

* Walk-forward forecasts and backtests on the 2023-24 to 2025-26 seasons.
* Computed only on the pinned snapshot `snap_b64560…`, which holds no 2026-27 data.
* The reports were last regenerated on 2026-10-06. The promotion thresholds were committed on
  2026-10-02.

Where it comes from:

* `ml/reports/forecast_eval.md` and `backtest.md`;
* `season_reference.json`: the same rows re-summarised with this note's method (§7).

What it can show: how the method did on the seasons its design was iterated on. It has
selection bias (`docs/BACKTEST_PROTOCOL.md`). The thresholds were set after 2026-27 GW1–5 had
been played publicly, but before this system scored any of them.

### Live record — forecast frozen before kickoff

What it is: the forecast the product served at a gameweek's decision cutoff. The forecast record
captures it with checks that prove it was promoted before the cutoff (§2).

Where it comes from: `forecast_checkpoints` rows with state `prospective`, and their
`predictions` rows.

What it can show: genuine out-of-sample evidence. It is the only class that can support "the
live forecasts are calibrated". It starts at GW7 of 2026-27.

### Your recorded decisions — private to you

What it is: M1.4 journal decisions recorded before the deadline, whose evaluation is final.

Where it comes from: `decision_journal` and `decision_evaluations`, computed when read.

What it can show: what a manager's own decisions achieved against their decision-time
expectations. It is observational (§9).

### Not used

* **Replay of 2026-27 GW1–6.** These would be forecasts recomputed now for gameweeks already
  played. Deferred (§15: L9, S10, SR-04, P-06, P-15).
* **2026-27 GW1–6 as live record.** These gameweeks have no live record and are never relabelled
  as one:
  * the record did not exist before 2026-10-14;
  * retention deleted the serving forecasts of those weeks (the GW6 file is gone);
  * the `predictions` table had never been written.

Outcomes (official FPL results) are the same for every class. A forecast is only ever scored
against final results (§3).

## 2. The forecast record (live record)

Before M1.5, the serving forecast was a joblib file of about 4.5 MB. Retention deleted it once 24
newer snapshots had been exported and it was more than 24 hours old. No per-player forecast
outlived its gameweek.

### 2.1 Promotion record

When the forecast job marks a snapshot's serving forecast ready (the API may now serve it), it
first writes a `forecast_promotions` row with:

* snapshot id, season and decision gameweek;
* forecast key and prediction run id;
* the snapshot's export time (manifest `created_at`);
* the SHA-256 of the forecast file;
* the code digest stamped into the forecast's provenance when it was computed;
* `promoted_at`, taken from the database clock.

The row is insert-if-absent, so the first promotion time is kept. If the row cannot be written,
the job fails and nothing is promoted. So a served forecast always has its commitment in the
database before the API can serve it.

### 2.2 Checkpoint rule

For each (season, gameweek), the live checkpoint is the forecast the API would have served at the
decision cutoff (deadline − 90 minutes). That is the forecast of the newest snapshot, by export
time, among those whose promotion was recorded at or before the cutoff. This matches
`DataService._newest(ready_only=True)`, which serves the newest export that has a ready marker.

It is not "the forecast every recommendation used":

* worker jobs compute on the newest export, even one not yet promoted;
* recommendations made between the cutoff and the deadline may use a later forecast.

Recorded decisions are scored against their own stored decision-time values (§9).

### 2.3 Prospective test

A checkpoint has state `prospective` only when all four checks hold:

1. `promoted_at` ≤ cutoff.
2. The snapshot's export time ≤ cutoff. This bounds every channel by which information from after
   the cutoff reaches a point-in-time view:
   * result rows reloaded with corrected values that keep their original `available_at`;
   * schedule backdating under rule S1;
   * the players table, which is read without a time filter.
3. The SHA-256 of the forecast file at capture equals the one in the promotion record. The
   prediction run id, data snapshot and forecast key in the file also match the record.
4. The cutoff in the forecast's provenance equals the cutoff computed from the gameweek's
   deadline at capture. If FPL moved the deadline, the checkpoint is flagged `deadline_moved`, and
   checks 1–2 apply against the earlier of the two cutoffs.

Capture time is recorded but is not a check. The forecast file is never rewritten:

* `ForecastService.get` returns an existing file;
* retention only deletes;
* retention keeps the candidate until it is captured (§2.6).

A capture after kickoff therefore adds no risk, and a host that sleeps through the cutoff does not
lose the gameweek.

### 2.4 States

* **`prospective`**: every check holds. These checkpoints form the live record.
* **`live_unverified`**: a forecast was captured, but a check failed. The failed checks are
  stored. It is shown separately with its reasons, and never scored with the live record.
* **`missed`**: no promotion was recorded before the cutoff, or the file was gone at capture. The
  reason is stored. Nothing is ever recomputed in its place.
* **Before the record**: a gameweek whose cutoff precedes the first recorded promotion. No row is
  written. The page says when the record began.

### 2.5 What is stored

Everything is insert-only.

**`forecast_checkpoints`**, unique on (season, gameweek, kind). Each row holds:

* season, gameweek and kind (`live`);
* state, with reasons and checks;
* prediction run, forecast key, and data and feature snapshots;
* cutoff, deadline and first kickoff;
* export, promotion, generation and capture times;
* model, feature and ruleset versions, and the simulation seed and size;
* the compute-time and capture-time code digests;
* the image build revision as given at build time: a git SHA, with `-dirty` when the tree had
  uncommitted changes, or `unrecorded`;
* the full forecast provenance;
* the row count, the population counts, and the content hashes of the rows and blobs.

The id is a content hash of (kind, season, gameweek, prediction run, forecast file hash), so
repeating a capture finds the same row.

**`predictions`** (reused), per player × target gameweek, horizons 0–4. It holds the forecast's
own numbers:

* mean, p10–p90 and std;
* P(points ≥ 2/6/10/15) and P(start) as served;
* expected minutes and its quantiles;
* fixture count;
* the 13 expected-point components (JSON).

Migration 0006 changes the table:

* adds `checkpoint_id`, a foreign key with RESTRICT, unique with (player, gameweek);
* makes the run foreign key RESTRICT (it no longer cascades);
* drops the run-scoped unique constraint. A prediction run can be recomputed by different code
  under the same id, because the id hashes provenance, not code or numbers. So rows are keyed by
  checkpoint.

**`artifact_blobs`** (content-addressed) holds two blobs per checkpoint:

* **Context and baselines (parquet).** Per row:
  * fixture ids, club, position, price, status and chance of playing at the cutoff;
  * `start_rate_long`, P(play), and P(start) before the availability layer;
  * the decision-time baseline predictions (§5).

  The baselines are computed at capture through the point-in-time view on the forecast's own
  snapshot. They use the `forecast_eval` code (`train_benchmarks`, `prediction_rows`), so past
  seasons and the live record share one implementation. This needs the snapshot to still exist
  (§2.6). If it is gone, the checkpoint is stored with `baselines_unavailable`: its rows are kept
  but not scored (they could not be paired), and they are counted under that reason.
* **Distributions (npz).** The Monte Carlo and climatology points distributions of every row, as
  exact per-value counts (`fpl_forecasting.distributions`). CRPS and PIT computed from counts
  equal those computed from samples.

Size per gameweek is about 3,335 prediction rows, a ~0.2 MB context blob and a ~0.06 MB
distribution blob. A season is about 127k rows and about 10 MB of blobs.

### 2.6 Retention

Until a checkpoint row exists, retention keeps three things for every gameweek whose cutoff has
passed since the record began:

* the candidate snapshot, with its caches;
* its forecast file;
* its ready marker.

Once captured, the checkpoint is self-contained (rows, blobs, hashes), and the snapshot follows
the normal rule.

### 2.7 Code identity

`code_digest` is a SHA-256 over every installed first-party Python source. Two processes report
the same digest exactly when they run the same code. The process that computed a forecast stamps
it into the forecast's provenance. Forecast keys and prediction run ids do not include code, so
the digest is the record of which code produced the numbers.

The build revision is informational. A dirty or unrecorded build is recorded as such, and never
presented as a clean git SHA.

## 3. Outcomes: one finality rule

`fpl_storage.finality` holds the single definition, shared with the M1.4 journal. A target
gameweek is final when all three hold:

* FPL has finalised it (`gameweeks.status = finalized`: `finished` and `data_checked`);
* every fixture in it is `final` and `finalized`;
* every result row of those fixtures is `finalized`, meaning it was loaded after the ruleset's
  finalisation lag. For 2026-27 the lag is 72 hours, an assumption of the ruleset.

Gameweeks that are not final are not scored. They are listed as windows with their state
(`not_started`, `in_progress`, `provisional`, `results_not_final`), never as zero.

### 3.1 Row eligibility

A checkpoint row (player, target gameweek) is scored only when all of these hold:

* the target gameweek is final;
* the checkpoint is `prospective`;
* the forecast target is `points`, under the same ruleset version as the outcome season;
* the set of fixture ids in the player's result rows for that gameweek equals the forecast's set.

The outcome is the sum of those rows, so double gameweeks are summed.

Otherwise the row is excluded and counted under one of these reasons:

* **`schedule_changed`**: the club's final fixture set differs from the forecast's, because a
  fixture was postponed or moved in or out.
* **`club_changed`**: the player's result rows belong to another club.
* **`outcome_missing`**: the player has no result row for that gameweek. FPL publishes a row for
  every registered player, including those who did not play, so a missing row is never read
  as 0.
* **`outcome_incomplete`**: the player has result rows for only some of the forecast's fixtures.
  The rest are never filled with zeros.
* **`ruleset_changed`**.
* **`baselines_unavailable`** (§2.5).

Exclusion depends on the schedule, not on the player's outcome. It applies identically to the
model and to every baseline, so the rows stay paired. Clubs that gained a fixture the forecast did
not have are counted. One descriptive sensitivity number, RMSE "as served", scores
schedule-changed rows on their realised values.

Past seasons applied no such exclusion. There, missing outcomes were filled with 0, which rarely
applied.

### 3.2 Corrections and revisions

An evaluation's **input digest** covers:

* the evaluator version and metric configuration;
* the set of scored (checkpoint, player, target) rows;
* their outcome values;
* the finality state of every window.

The code digest is provenance only, so a deploy that does not change the evaluator changes
nothing.

A new evaluation is written only when the digest changes. It names:

* the evaluation it follows (`previous_id`);
* its trigger: `first`, `new_gameweek_final`, `outcome_correction`, `evaluator_change`, or
  `record_change` (a checkpoint was captured, or a window's finality state moved on);
* a typed changes block: gameweeks newly scored; gameweeks revised, with their result-row count
  and total points before and after; exclusion-count deltas; headline metrics before and after.

For a window that is not final, only its state is part of the digest. Fixture-level status
changes within a live gameweek therefore do not each write an evaluation.

Earlier evaluations are kept.

## 4. Leakage rules

1. **Final results only.** Forecasts are scored only against final results, and only for target
   gameweeks after their cutoff. Rows of non-final gameweeks are dropped before outcomes are
   joined, never filled with 0.
2. **No replacement.** A checkpoint is never replaced by a newer forecast. Every evaluation reads
   the same stored rows.
3. **Point-in-time baselines.** Decision-time baselines are fitted at capture, through the
   point-in-time view at the checkpoint's cutoff, on the forecast's own snapshot (export time ≤
   cutoff). They are fitted on the training rows the walk-forward `training_table` exposes at that
   cutoff, and never on any other snapshot.
4. **No shuffling across time.** Uncertainty treats whole target gameweeks as the unit (§6).
5. **Temporal split.** Calibration fitted on outcomes may use only scored rows whose target
   gameweek was final (under §3) in the data available at the cutoff where it is applied, whatever
   the horizon. M1.5 fits none (§11).
6. **No randomised PIT.** The expected (non-randomised) coverage is deterministic, so adding a
   gameweek never changes earlier rows' values.

## 5. Populations, horizons, metrics and the primary comparisons

### 5.1 Horizons and populations

**Horizon 0 is primary**: the next gameweek from each checkpoint. Horizons 1–4 get separate
per-horizon tables, with the target gameweek as the cluster. They are never pooled, because
pooling scores one outcome up to five times.

**Populations**, as defined in `forecast_eval`, at the cutoff:

* **All**: every registered player with a fixture in the target gameweek. This matches the
  past-season reports and the promotion gates. The number of rows with status `u` is reported.
* **Regulars**: `start_rate_long` ≥ 0.5. Shown beside "all" in every primary table, because it is
  the population managers pick from.
* **Availability-flagged**: status i/d/s/n, or chance of playing < 100. A descriptive segment for
  the uncalibrated availability layer.

### 5.2 Primary family

Verdicts use these four comparisons, at horizon 0 on population "all". Each is a per-gameweek
paired difference, model − baseline, so lower is better.

| # | Target | Statistic d_g | Baseline (named now) |
|---|---|---|---|
| C1 | points mean | MSE difference (same sign as the RMSE difference on identical rows) | `ppg_availability`: the best simple baseline on 2023-26 for "all" and "regulars" |
| C2 | points distribution | fair-CRPS difference | climatology: position × start-rate pools, fitted at the cutoff |
| C3 | P(points ≥ 6) | Brier difference | climatology P(≥ 6) |
| C4 | P(start) | Brier difference | the start-rate baseline |

### 5.3 Calibration checks

Verdicts use these checks, at horizon 0 on population "all":

* **K1**: calibration-in-the-large of P(≥ 6), i.e. mean p − observed rate.
* **K2**: calibration-in-the-large of P(start).
* **K3**: PIT central-80 coverage, using the expected, non-randomised coverage
  (Czado–Gneiting–Held). P(PIT < 0.1) and P(PIT > 0.9) are reported beside it, each against
  0.10.

### 5.4 Secondary measures

These get intervals or values only, never verdict words:

* RMSE and MAE levels, and bias (mean ŷ − y).
* RMSE differences vs `recent_form`, `position_mean` and `fpl_style`. `fpl_style` is "a simple
  rule resembling FPL's expected points", never "FPL's forecast". FPL's own `ep_next` is not
  ingested and is not a baseline (§15, SR-15).
* Rank correlation within regulars: the mean of per-gameweek Spearman values.
* Brier for P(≥ 2) and P(≥ 10). P(≥ 10) is a rare event.
* Expected-minutes RMSE vs the rate baseline. Minutes MAE is descriptive.
* ECE for P(≥ 6) and P(start), with the null consistency band (§6).
* Every regulars, horizon and segment figure.
* The drift signals of the existing governance policy: RMSE ratio to the past-season same-window
  value > 1.15, |bias| > 0.4, ECE > 0.05. These are descriptive monitoring signals. Serving already
  refits on every forecast, so no action follows (§11).

### 5.5 Every forecast number the UI shows

| Number | Status |
|---|---|
| Expected points (mean) | evaluated: C1, RMSE, bias |
| Points range p10–p90 | evaluated: K3 (PIT-80) and the tails. The inclusive [p10, p90] coverage is descriptive only, because it over-covers integer outcomes |
| P(≥ 6) | evaluated: C3, K1, ECE |
| P(start) | evaluated: C4, K2, ECE |
| P(≥ 2), P(≥ 10) | descriptive Brier only (P(≥ 10) is rare) |
| P(play) | not evaluated: no realised "appearance" target is stored separately from minutes |
| Expected minutes | secondary: RMSE vs the rate baseline |
| Component expected points (goals, assists, …) | not evaluated in M1.5: it needs a realised-component decomposition of the official results; backlog. The expected components are stored with every checkpoint |
| Price-change probabilities, plan gains, chip values | not evaluated here (price model: `forecast_eval`; plans: §10) |

## 6. Uncertainty and verdict rules

### 6.1 Intervals and tests

* **Unit.** The inferential unit is the target gameweek. Rows within a gameweek share fixtures
  and shocks, so 3,000 rows are not 3,000 independent observations. For each gameweek g, d_g is
  the gameweek's mean of the per-row statistic.
* **Interval.** mean(d) ± t_{0.975, G−1} · sd(d) / √G over the G scored gameweeks, weighted
  equally. With equal cluster sizes this matches the CR2 (Bell–McCaffrey) correction.
* **Test.** The exact sign-flip randomisation test on d_g: all 2^G sign patterns, or 20,000
  seeded draws when G > 15. The smallest attainable two-sided p is 2/2^G: 0.0625 at G = 5, 0.031
  at G = 6, 0.0078 at G = 8.
* **When intervals appear.** With G < 3, values only. From G = 3, intervals are shown, labelled
  "wide: G gameweeks".
* **No percentile bootstrap.** It is not used for live intervals. With few clusters it is too
  narrow, and it excludes 0 whenever all gameweeks share a sign.

### 6.2 Comparison verdicts

**Formal looks.** Verdicts are issued only at G = 8, G = 16 and the season's last evaluation.
Between looks the page shows "monitoring", with the interval.

**At a look**, the page says "lower error than ⟨baseline⟩" or "higher error than ⟨baseline⟩" only
when both hold:

* the Holm-adjusted sign-flip p is below 0.05 within the C1–C4 family;
* the 95 % t-interval excludes 0.

Otherwise it says "no clear difference".

**Before G = 8**, the page says "too few gameweeks to say", with the direction count (e.g. "lower
error in 4 of 5 gameweeks").

**Error control.** Each look controls the family-wise error at 5 %. Across three looks, the chance
of at least one false verdict when there is no difference is at most about 15 %. Every verdict is
shown with its look. A reversal at a later look is shown as such.

**Early-window caveat.** The early live window overstates a full-season margin: in past seasons,
the margin over GW7–14 was 1.5–1.8× the margin over GW15–38. The page says so.

### 6.3 Calibration verdicts

Calibration uses equivalence testing (TOST at 5 %) at the same looks:

* **"within tolerance"**: the 90 % t-interval of the per-gameweek statistic lies inside the margin;
* **"outside tolerance"**: the 95 % interval lies entirely outside it;
* **"inconclusive"**: otherwise.

The margins were fixed now, before any live outcome. They come from the promotion gates and from
the past seasons' per-gameweek variability:

| Check | Margin | Past seasons (per gameweek) |
|---|---|---|
| K1, P(≥ 6) | ±0.015 | mean +0.0014, SD 0.0095 |
| K2, P(start) | ±0.03 | mean +0.004, SD 0.0176 |
| K3, PIT-80 | [0.77, 0.83] (the promotion gate) | mean 0.803, SD 0.0175 |

At G = 8, the 90 % half-widths would be about 0.006, 0.012 and 0.012, so equivalence is
attainable.

**ECE carries no verdict.** It is biased upward at small samples, and its percentile interval
never contains 0 for a perfect forecaster. It is shown with:

* the observed value;
* the null consistency band: the median and 95th percentile of the ECE of y ~ Bernoulli(p),
  resampled 500 times on the same rows and bins;
* the excess ECE: observed − null median.

**The logistic calibration slope is not used.** On past seasons its per-gameweek SD is 0.23, which
makes it uninformative at these sizes.

### 6.4 Segments and sample sizes

Segments never carry verdicts. The minimum sizes for an interval are:

* **point and CRPS statistics**: ≥ 300 rows and ≥ 3 gameweeks;
* **Brier**: ≥ 50 events and ≥ 50 non-events.

Below these, values only. Event counts are shown next to every probability metric.

The per-gameweek table is descriptive and exempt from these minimums, with no intervals and no
verdicts.

Sample sizes are shown next to every metric: rows, gameweeks, players, checkpoints, and exclusions
by reason.

## 7. Past-season reference (same method, same weeks)

`ml/experiments/season_reference.py` re-summarises the stored 2023-26 walk-forward rows
(`data/eval/forecast_eval_rows.parquet`, horizon 0, 114 cutoffs) with `season_eval`'s
per-gameweek statistics. It needs no refit. It writes `ml/reports/season_reference.json` with, per
season and target gameweek, for "all" and "regulars":

* the sums for C1–C4, K1–K3 and the secondary measures;
* the bin sums for ECE.

The API reads it to show, beside the live record over GW7..k, the past seasons' values over the
same gameweeks.

This is context, not a test: the seasons, players and data regime all differ. Live and past
seasons are never put in one interval, and nothing is called better or worse across them.

Past seasons used the randomised PIT and the CRPS estimator with self-pairs, as published. The
live record uses the expected coverage and the fair CRPS. The difference between the two CRPS
estimators is under 1 % of the past-season CRPS margin.

## 8. Live-season jobs

### 8.1 Forecast record

The scheduler task **`forecast_ledger`** runs every 15 minutes and only queries the database. For
each gameweek of the current season that has no checkpoint and whose cutoff has passed since the
record began, it submits `forecast_capture {season, gameweek}`.

The **`forecast_capture`** job has a 15-minute lease. It:

1. selects the candidate (§2.2);
2. runs the checks (§2.3);
3. fits the baselines on the forecast's snapshot;
4. writes the header, rows and blobs in one transaction.

It is idempotent. The unique constraint makes a concurrent second capture a no-op, and a failed
job writes nothing.

### 8.2 Evaluation

The scheduler task **`season_eval`** runs hourly. It computes the input digest from the stored
checkpoints and from the snapshot's finality states and outcome values. It submits the
`season_eval` job only when the digest differs from that of the newest stored evaluation.

The **`season_eval`** job:

1. takes an advisory lock;
2. recomputes the digest;
3. refuses to write if its data snapshot is older than the newest evaluation's;
4. writes one `season_evaluations` row.

That row records:

* sequence number, previous id, trigger and input digest;
* evaluator and metric-config versions;
* data snapshot and code digest;
* checkpoint ids, windows and exclusions;
* metrics and changes.

Operators can also run it on demand (`POST /api/v1/season-review/evaluate`, operator only).

Scored rows are not stored per revision; the metrics and per-gameweek sums are. Rows can be
recomputed from the checkpoints and the outcome digest.

### 8.3 Endpoints and side effects

The Season Review endpoints read stored rows only. They never compute a forecast, never submit a
job and never auto-refresh.

Evaluation never writes manager states, recommendations, journal decisions or the optimizer's
inputs.

## 9. Your recorded decisions (journal summary)

### 9.1 Endpoint and access

`GET /api/v1/journal/summary?manager_key=&season=`:

* is registered before `/journal/{entry_id}`;
* uses the journal's ownership checks: another user's manager returns 403, no session returns
  401;
* is computed on read from the newest final evaluation of each active decision. There is no new
  table, and no change to export or erasure.

### 9.2 What it shows

**Counts**: recorded, active, final, pending, partial, unavailable, recorded after the deadline,
and replaced. Also the action mix, and adherence (followed / modified / rejected / deferred),
described as behaviour.

**Outcome rows** cover only active decisions recorded before the deadline that have a final
evaluation. Decisions recorded after the deadline are counted but never scored. Outcomes come from
official results, never from a value the manager typed.

**Per-decision table** (the primary view), labelled "first gameweek only; later weeks of a
transfer are not counted". Each row shows:

* gameweek and action;
* how the recommendation was used;
* realised vs decision-time expected points;
* realised and expected difference vs HOLD;
* the evaluation revision.

**Range counts**: realised points below / inside / above the decision-time 80 % range.

* They are shown as counts with the binomial typical range for n decisions ("with 5 decisions,
  3–5 inside the 80 % range is unsurprising"), never as a percentage score.
* The past-season split for the same check is shown beside them: in the 2023-26 backtests
  (engine plans, 114 gameweeks), 5.3 % of outcomes fell below the plan's 80 % range, 81.6 %
  inside and 13.2 % above.

**Totals**:

* the sum of realised − expected, and the sum of action − HOLD, each with n, labelled "sum of
  decision-gameweek differences";
* no action − recommended total;
* an interval only from ≥ 6 scored non-HOLD decisions, using the §6 t-interval and sign-flip test.

### 9.3 Limits and what is not shown

These limits are shown with the numbers:

* managers choose what to record and what to follow;
* a deviation may use information the system did not have;
* only stored alternatives are scored;
* the sample is a handful of correlated observations;
* nothing here estimates the effect of following Copilot.

There are no ratings, grades or percentages of "good decisions". There is no population-level
journal view: with a few beta users, it would identify people.

## 10. Strategy benchmarks

**Past seasons.** The published three-season backtest (the engine and six predeclared strategies,
`docs/BACKTEST_PROTOCOL.md`), reported as published. It predates later engine changes: the
season-end valuation fix and the defect-53 labels.

**This season**, nothing new is claimed:

* **Live shadow benchmark: deferred.** It would freeze the seven default strategies' decisions for
  one reference squad at every checkpoint, and score them. The page says: "The engine's live
  decisions are not benchmarked this season; strategy comparisons come from past seasons only."
* **Strategy replay of 2026-27 GW1–6: deferred.** It is post-hoc, it would carry the data-regime
  caveats in §15 (L9), and six gameweeks cannot support a verdict.

## 11. Calibration policy

No calibration layer is fitted or applied in M1.5. The forecasting method and its settings are
unchanged: it refits on new results at every forecast, as it always has.

### 11.1 Calibration state

The state is computed at each formal look, from the live record only:

* **`deferred`**: fewer than 8 final live gameweeks, or any of K1–K3 inconclusive. Page copy: "n
  of 8 live gameweeks final; the earliest look is after GW14 (about 10 December 2026)".
* **`not_needed`**: K1–K3 all "within tolerance" at a look.
* **`review_suggested`**: any of K1–K3 "outside tolerance" at a look. Nothing changes
  automatically. A proposal must pass the adoption test below.

### 11.2 Adopting a recalibration

A recalibration may be adopted only if all of these hold:

* it is fitted only on rows allowed by §4 rule 5;
* it first runs in shadow mode: never served, with its output versioned separately from the
  original forecasts, which stay unchanged;
* it is evaluated prequentially: at each later live gameweek, it is refitted on all earlier final
  gameweeks and scored on the next;
* it improves the targeted check on ≥ 6 test gameweeks under the §6 rule: the interval of the
  improvement excludes 0, and the sign-flip p < 0.05;
* segment behaviour and interval widths are checked.

### 11.3 The rolling eight-cutoff conformal interval

This is a candidate, not a rule. In the 2023-26 research, residual conformal calibrated on the
previous 8 cutoffs reached coverage 0.805 with intervals wider than the Monte Carlo ones, and was
not adopted. For live use:

* it would first have 8 earlier final gameweeks at the GW16 cutoff;
* it respects time order by construction;
* it applies only to the displayed points range, not to P(≥ 6) or P(start);
* it would be evaluated in shadow mode as in §11.2.

It is not justified now: the past-season PIT-80 coverage of the Monte Carlo distribution is 0.803,
inside the gate.

The existing governance drift thresholds (§5.4) are reported as descriptive monitoring signals.
They are not a second trigger.

## 12. The Season Review page

The page is public. It shows no manager data, except the signed-in user's own private section.

### 12.1 Sections, in order

1. **Status strip**:
   * live gameweeks scored, out of the 8 needed for the first formal look;
   * the next checkpoint ("GW7 forecast freezes 17 Oct 08:30 UTC") and when its results are
     expected to be final;
   * the newest evaluation's time and version, and what changed since the previous one.
2. **Live record — forecast frozen before kickoff**:
   * the checkpoint timeline: per gameweek, captured / missed / before the record / unverified,
     with reasons;
   * the per-gameweek table;
   * one dot-and-interval "difference vs baseline" per comparison C1–C4, with direction counts;
   * K1–K3, with their margins and the past-season same-window value;
   * ECE numbers with the null consistency band;
   * an empty state while nothing is scored: "No live gameweek is final yet."
3. **Past seasons 2023–26 — the seasons the model was built on**: the reference numbers for the
   same weeks, and the published summary, with the selection-bias note.
4. **Your recorded decisions — private to you**: signed-in users only (§9).
5. **Calibration**: the state and its copy (§11).
6. **How these are computed**: the §5.5 table, the populations, the verdict rules and the limits.

### 12.2 Chart policy

* **Shown now**: the checkpoint timeline, the per-gameweek table, the dot-and-interval
  differences, and coverage and ECE numbers with the null consistency band.
* **Gated**:
  * reliability diagrams (P(start) first, regulars) only when every drawn bin has ≥ 3
    contributing gameweeks;
  * horizon charts only when every horizon has ≥ 3 target gameweeks.
* **Never shown**:
  * a per-player scatter;
  * cumulative-points lines;
  * a strategy league table;
  * component-bias charts;
  * classes mixed in one chart or table.

### 12.3 Accessibility

* Charts are inline SVG using the theme tokens, in a `<figure>` whose `<figcaption>` states the
  takeaway and n.
* The same numbers appear in a table next to each chart.
* Intervals are drawn at ≥ 3:1 contrast.
* Class and verdict are never shown by colour alone: text labels and filled / hollow markers carry
  them.
* There is no animation, and nothing needs hover.

### 12.4 Data Health

Data Health labels its gates "passed on past seasons 2023–26", and links here.

## 13. Reproducibility

* Every checkpoint and evaluation records:
  * the data snapshot;
  * the model, feature and ruleset versions;
  * the evaluator and metric-config versions;
  * the code digest.
* An evaluation can be recomputed from its checkpoints and outcome digest, with the evaluator
  version it names.
* Cross-machine numerical differences (defect 52) mean recomputing a checkpoint's forecast
  elsewhere is not bit-identical. That is why the forecast's numbers are stored rather than
  recomputed.
* Defect 53 (the club-excess optimizer formulation) does not touch forecasts or their evaluation.
  Both defects stay open.

## 14. Limits

* **Live evidence starts at GW7 of 2026-27**, if the GW7 checkpoint is captured. GW1–6 have no
  live record.
* **The first formal look needs 8 final live gameweeks** (GW7–14, about 10 December 2026). Until
  then, every live number is "monitoring".
* **The finalisation lag is an assumption of the ruleset.** "Final" arrives three to four days
  after a gameweek's last match.
* **Results are re-checked only within 4 days of the newest finished gameweek.** A gameweek that
  misses that window waits for the next one. It is listed as `results_not_final`, with its
  non-final rows counted.
* **The availability layer (news multiplier) has no past-season evidence.** Its effect is visible
  only in the availability-flagged segment.
* **No external forecast benchmark.** FPL's own expected points are not ingested. `fpl_style` is a
  simple rule, not FPL's forecast.

## 15. Review findings and their resolution

Four independent critics reviewed the draft on 2026-10-12:

* statistics (S);
* leakage and data integrity (L);
* engineering feasibility (P);
* product honesty and privacy (SR).

A separate verifier then checked each finding against the code and the live stack. Three verifiers
(P-06, P-07, P-09) did not complete; those findings were assessed directly against the code.

| Finding | Resolution |
|---|---|
| S1, L7, P-09, SR-01: a percentile cluster bootstrap with 3–8 gameweeks | **Accepted.** A t-interval on per-gameweek differences plus an exact sign-flip test. Verdicts only at looks, from G = 8 (§6). |
| S2, L6: horizon and population not fixed; pooling horizons | **Accepted** (§5.1). |
| S3: baseline strength varies over the season | **Accepted.** A same-window past-season reference (§7) and an early-window warning (§6). The replay part is moot, as there is no replay. |
| S4: ECE bias; tolerances are not interval margins | **Accepted.** CITL and PIT-80 with TOST margins. ECE is descriptive, with a null band. The logistic slope is **rejected** as a verdict statistic: its per-gameweek SD (0.23) makes it uninformative at these sizes. |
| S5: multiplicity and repeated looks | **Accepted.** A four-comparison family with Holm, three formal looks, and the across-looks bound stated (§6). |
| S6, P-11, SR-06: segment minimums | **Accepted** (§6). |
| S7: the availability layer cannot be separated | **Accepted in part.** Status, chance and the pre-layer P(start) are stored, and a flagged segment is added. A paired layer-on/off comparison is **deferred**: it needs a second simulation per checkpoint. |
| S8, SR-05: population definition | **Accepted, except one part.** Excluding status `u` from "all" is **rejected**: the past seasons and the gates include those rows, the rows affect model and baseline alike in paired comparisons, and their count is reported. |
| S9, SR-07, L8: journal summary | **Accepted** (§9). |
| S10, SR-04, L9, P-06, P-15: the GW1–6 replay | **Deferred**, as a backlog item with these constraints. It adds no live evidence. Its data regime differs by gameweek: GW1 had no live player data at its cutoff, and GW2–5 used a stale capture from 28 Aug. S1 backdates schedule changes. Six gameweeks cannot support a verdict. |
| S11: minutes MAE | **Accepted.** RMSE is the secondary minutes measure; MAE is descriptive. |
| S12, P-19, L11: randomised PIT | **Accepted.** Expected coverage (§4 rule 6). |
| S13: CRPS self-pairs | **Accepted.** Fair CRPS for the live record; past seasons as published (§7). |
| S14, L4, P-12: the fixture-count rule misses swaps | **Accepted.** Fixture-id set equality, paired, with an "as served" sensitivity (§3.1). |
| S15, L13, P-13: `temporal_split` undefined; rule 3 unattainable | **Accepted.** Defined in §4 rule 5, with a prequential test on ≥ 6 gameweeks (§11). |
| S16, L3, P-08: failed live checkpoints counted as replay | **Accepted.** A `live_unverified` state, never pooled (§2.4). |
| L1, P-22: selection by marker mtime; "every recommendation used it" | **Accepted.** A database promotion record, and selection that mirrors `_newest(ready_only)`. The claim is removed (§2.1–2.2). |
| L2, P-01: the prospective test checks the wrong things; the capture-before-kickoff window | **Accepted.** The checks are now: export time ≤ cutoff, promotion ≤ cutoff, the file hash committed at promotion, and cutoff equality. Capture time is not a criterion (§2.3). |
| L5: snapshot pruned before a late capture | **Accepted.** A retention keep-rule, with no fallback to another snapshot (§2.5–2.6). |
| P-02: retention deletes the forecast after a refresh outage | **Rejected as stated.** The verifier showed that retention keeps any snapshot among the 24 newest, and outages export nothing. The keep-rule closes the narrower real exposures: a prolonged capture failure, and demotion of a stale candidate (§2.6). |
| L10, P-14, SR-16: dating of "predeclared" | **Accepted.** Dated wording (§1). |
| L11, P-17: revision races; extend vs revise | **Accepted.** An advisory lock, no backward writes, triggers, and a changes block (§3.2, §8). Per-window outcome digests are kept in each evaluation. |
| L12: a dropped player's provisional row blocks finality | **Accepted in part.** The window shows `results_not_final` with the count of non-final rows. Redefining the shared finality rule is **deferred**: it would change M1.4 journal behaviour, and the case has not occurred. |
| L14: baseline code drift; refit cadence | **Accepted.** One implementation (`train_benchmarks`, `prediction_rows`). The cadence difference is documented: live refits at every cutoff, past seasons every 4 gameweeks. |
| P-03, P-23: predictions keyed by run; NOT NULL; CASCADE | **Accepted.** Migration 0006 (§2.5). |
| P-04: code digest of the capturing process | **Accepted.** Stamped at compute time. The capture digest is kept separately and is not in the input digest. |
| P-05: per-revision scored tables grow without bound | **Accepted.** Each revision stores metrics and per-gameweek sums; rows are recomputable. |
| P-07: `score_rows` fills missing outcomes with 0; direct LightGBM and `start_rate_long` required | **Accepted.** Non-final rows are dropped before the join, and a missing outcome is excluded (§3.1). `start_rate_long` is stored. The direct LightGBM comparator is not fitted at capture: it is not a baseline here, and it costs 6 s and 1.5 GB per capture. |
| P-10: store samples as counts; drop starts samples | **Accepted.** |
| P-16: wasteful polling | **Accepted.** Due-checks in the scheduler, and jobs only when due (§8). |
| P-18: journal analytics in a shared table | **Accepted.** Computed on read (§9). |
| P-20: GW1–6 marked missed | **Accepted.** They are shown as "before the record" (§2.4). |
| P-21: a second drift rule | **Accepted.** The governance thresholds are descriptive signals (§11). |
| SR-02: questions and page undefined | **Accepted** (§0, §12). |
| SR-03: class letters and "prospective" in the UI | **Accepted** (§1). |
| SR-08: route order and privacy mechanics | **Accepted** (§9). |
| SR-09: calibration states | **Accepted** (§11). |
| SR-10: "what changed" undefined | **Accepted** (§3.2). |
| SR-11: no live benchmark of engine decisions | **Deferred**, with page copy (§10). |
| SR-12: accessibility | **Accepted** (§12). |
| SR-13: Data Health gate labels | **Accepted** (§12). |
| SR-14: UI numbers without an evaluation status | **Accepted** (§5.5). |
| SR-15: `ep_next` as a baseline | **Rejected for M1.5.** It is not ingested through a documented point-in-time path. Its comparability is not established: FPL's expected points include FPL's own availability rules and are published per player, not per fixture. It needs its own ingestion milestone. `fpl_style` is relabelled. |
| SR-17: the page must not trigger work | **Accepted** (§8.3). |
