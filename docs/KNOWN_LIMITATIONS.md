# Known limitations

What this system does **not** do, what has only been checked against snapshots or mocks, and
where its numbers come with caveats (§92). Every item names the evidence; numbers live in the
generated reports so they cannot drift from this page.

## At a glance (V1)

* **Uncertain inputs.** Forecasts are probabilistic and lineups, minutes and injuries are often
  wrong; news-driven availability is uncalibrated (§4).
* **Selection bias.** The design was iterated on the evaluated seasons; 2026-27 is the first
  unseen season (§3).
* **Optimiser limits.** Chip-open solves run to the 60 s limit and return validated incumbents,
  labelled "best found within the solver limit; not proven optimal" (§5). With a club already
  over the limit (an inherited excess) the MILP can let it grow back, the rules replay rejects
  that plan, and the plan recommended instead is labelled "best valid plan among those explored;
  not proven optimal" (defect 53, §5).
* **Planning horizon.** Forecasts are trained and evaluated for five gameweeks (target horizons
  0–4); the API refuses longer horizons rather than serve an extrapolation (§3).
* **Latency.** Recommendations take 42–90 s as worker jobs; the Transfer Workbench runs its
  plan comparison as a job too (HOLD + 3 replacements + best plan: tens of seconds, §5); the
  older `POST /replacements` endpoint still holds a request open for 13–49 s (§5).
* **Result lifecycle.** Snapshot promotion and finalisation races are handled (`docs/architecture/
  result-lifecycle.md`); the "what changed" snapshot diff needs the superseded snapshot's export
  on disk (retention keeps referenced snapshots, caches dropped) and otherwise reports only the
  identifiers; model-configuration changes are recorded (`model_versions`, `config_refs`) but
  do not by themselves invalidate a result; nothing is regenerated automatically. Between a live
  export and its promotion the worker computes on the newer export: a recommendation made then
  is current on that snapshot, and a missing Copilot Home analysis shows as *computing* until
  the promotion.
* **Decision journal.** One gameweek's outcome is a single draw: the journal reports what
  happened against what was expected and never grades a decision from it. Only decisions the
  application can reconstruct are scored ("something else" is recorded, not evaluated);
  HOLD's XI is rebuilt for recommendation sources; evaluation is final only once FPL finalises
  the gameweek; verification against the official picks needs an FPL ID (§4a).
* **Reproducibility across machines.** The engine reproduces itself byte-exactly on one machine;
  on another CPU class the forecasts, objectives and tie-breaks differ, and shared x86_64 CI
  runners differ from each other (`docs/BUILD_STATUS.md` §2d defect 52). Golden parity is
  therefore enforced on the development platform, not on shared CI (§5).
* **Invite-only beta accounts.** Users own their manager data (checked server-side on every
  request); there is no password reset, no lockout and no e-mail verification yet (§6).
* **External source.** The live FPL API is unofficial and can change or go away (§2).
* **Laptop sleep.** Long jobs on a sleeping development machine are interrupted and re-run (§7).
* **Webhooks.** A DNS-rebinding window remains between validation and connection (§6).
* **One machine.** Every timing comes from one development machine and its Docker VM (§5).
* **Not yet public.** Deployment is verified locally, TLS included; no public URL exists yet
  (`docs/BUILD_STATUS.md` §2i lists the host script and the owner inputs still missing).

## 1. What has been verified against real FPL data — and what has not

| Verified against real data | Evidence |
|---|---|
| Live FPL API (`fantasy.premierleague.com`) reachable from the development machine; `bootstrap-static` and `fixtures` parse against the typed contracts and load canonically (667 players, 380 fixtures, news/availability, official deadlines) | `fpl-ingest live --season 2026-27`; the compose scheduler's hourly `live_refresh` |
| Completed-match results of the live season from the API (`element-summary/{id}` histories, 2026-10-06): 3,216 rows for 667 players, GW1–GW5, in 11 min 20 s. All 50 finished fixtures have rows for both sides and two team rows; fixture scores equal team goals; the 610 GW1 rows equal the archive's values exactly (only source and lineage changed: 610 revisions, 0 conflicts); deadlines and the schedule untouched. A second run (2026-10-07) inserted, updated and revised nothing (3,216 + 100 unchanged). The live snapshot was promoted and the API stopped reporting degraded | `fpl-ingest live-results`; scheduler `live_results`; `tests/integration/test_live_results.py`; `test_live_rules.py::test_live_player_histories_pass_the_archive_contract` (network) |
| Live squad sync of a public manager entry: squad, bank, free transfers and both chip sets reconstructed; squad equal to the official pre-Free-Hit picks (correct Free Hit reversion) | manual run against the deployed API (data deleted afterwards; identifiers not recorded) |
| Current gameweek and decision cutoff equal the official API's `is_next` event | `GET /api/v1/gameweeks/current` vs `bootstrap-static` |
| Historical seasons 2022-23 – 2026-27 (GW1) from the pinned vaastav commit; the canonical snapshot reproduces byte-identically on macOS and in the Linux container | `snap_b64560a8c4f434ad984e` (`docs/BACKTEST_PROTOCOL.md`) |
| Official points reproduced from components on all 113,870 historical player-match rows | `tests/integration/test_full_scoring_reproduction.py` (slow tier) |
| Walk-forward backtests of three seasons on that data | `ml/reports/backtest.md` |
| Alert rules on real squads, forecasts, schedules, price probabilities and live deadlines | `ml/reports/alerts_audit.md` |

| Only tested on synthetic payloads, fixtures or mocks | Why |
|---|---|
| Injury / suspension / doubt alerts (availability drops) | the archive has no status history; real live changes did not occur during the test window |
| Webhook *delivery* success | only failure paths were exercised against real DNS; no external endpoint was posted to |
| Manager sync for edge cases (mid-season joiners, AFCON top-ups, 2022-23 GW17 rule) | synthetic payloads in `tests/unit/ingestion/test_manager_sync.py` |
| Live results in a double gameweek | the API reports one history row per fixture (the archive's schema, whose double gameweeks the historical tests cover), but no double gameweek has occurred in 2026-27 yet |

## 2. Live data pipeline

* **Completed-match results take one request per player.** The `live_results` task reads every
  player's `element-summary/{id}` history — the archive's per-fixture rows, with the price and
  ownership at that fixture — so a capture takes ~11 minutes at the paced request rate. It runs
  when a finished (or provisionally finished) gameweek has no results, then daily for 4 days after
  a gameweek's last kickoff to pick up bonus and late corrections (stored as revisions); later
  corrections are not fetched. The scheduler runs it inline, so its other tasks (alerts,
  forecast precompute) are delayed by up to ~11 minutes when it runs. Players the API no longer lists are not captured. Until a
  capture completes, the freshness block lists "match results missing for finished GW…" and marks
  responses degraded. Live results never reach the evaluation: the backtests read only the pinned
  evaluation snapshot and seasons (`config/backtest/default.yaml`).
* Every live refresh exports a full snapshot (~3 MB, hourly) plus, once served, a ~30 MB feature
  cache and a ~7 MB forecast. The hourly `retention` job bounds this (newest 24 kept by default,
  plus the serving, pinned and recommendation-referenced snapshots; `docs/DEPLOYMENT.md`), but
  `/data/raw` (~0.3 MB per capture, the provenance evidence) and the live-observation tables in
  PostgreSQL are not pruned: they need an archival policy before multi-year operation.
* After a live refresh the worker computes on the new snapshot while the API keeps serving the
  previous one until its forecast is precomputed (1–2 minutes) and the API's next snapshot check
  (`FPL_SNAPSHOT_CHECK_SECONDS`, 300 s by default). A recommendation made in that window
  records — and shows — its own, newer snapshot, which differs from the banner's.
* Prices observed between captures are not modelled; the official price-change predictor is
  shown only when the captured bootstrap carries it and is never blended with the engine's model.
* Source reliability: the FPL API is unofficial and undocumented; schema drift fails the typed
  contracts (the capture is quarantined; an unreachable source is recorded as failed) and the last
  validated snapshot keeps serving, flagged stale after 12 hours. Observed on 2026-10-06/07: ten
  consecutive hourly captures failed while the source was unreachable (host asleep / network);
  every job ended `failed` with the reason, responses were flagged degraded, and the next
  successful capture restored fresh data without intervention.

## 3. Historical data and evaluation

* **Selection bias.** Model structure, hyper-parameters and optimiser defaults were developed
  while looking at 2023-24 – 2025-26. The backtest is walk-forward in *information* (no future
  data reaches any decision) but not free of researcher degrees of freedom. 2026-27 is the first
  genuinely unseen season.
* **Corrected look-ahead.** The previously published backtest (commit `03c14a7`) read the
  archive's final fixture schedule, which leaked postponements. Schedule rule S1 fixes this;
  some earlier verdicts changed (the report lists old vs new). Rule S1 is itself conservative
  (cup-driven blanks are treated as unknown until the original deadline).
* No historical injury news; labels are final (corrected) values; prices between kickoffs are
  unobserved; 2023-24 trains on a single prior season.
* Statistical power: one season is 38 correlated gameweeks. Per-season differences of ±150
  points are routinely inside the 95 % CI; bootstrap CIs treat gameweeks as exchangeable and are
  probably somewhat narrow; 24 comparisons are shown without multiplicity correction.
* Overall rank cannot be evaluated (no score distribution of other managers).

## 4. Forecasting and decisions

* Lineups, minutes and injuries are inherently uncertain; the minutes model is calibrated in
  aggregate (start-probability ECE 0.007–0.023 at horizon 0 in the backtest forecasts) but individual rotation
  calls are frequently wrong, and news-driven availability is configuration, not a calibrated
  model (no historical news to fit it on).
* Point intervals are discrete-valued; the inclusive p10–p90 interval over-covers integer
  outcomes and the strict one under-covers (both reported). Squad-level p10–p90 covers 83 % of
  non-chip weeks (nominal 80 %; 5 % below p10, 11 % above p90): slightly wide, slight upside skew.
* Monte Carlo: 1,000 joint samples per forecast; tail probabilities below ~1 % are noisy.
* Model drift: serving retrains every model for each new snapshot (the 4-gameweek cadence in
  `config/models/points.yaml` applies to evaluation only). The PSI feature-drift and
  retraining-decision functions in `fpl_forecasting/governance.py` are unit-tested but have no
  runtime caller, and the figures behind the `ForecastCalibrationDrift` alert are the published
  offline report values, not live-season measurements. Live-season calibration is V1.1
  (prospective evaluation); the 2026-27 models have not been evaluated on 2026-27 outcomes
  beyond GW1.
* The decision engine's thresholds (`min_gain`, `min_prob_positive`) trade activity for
  stability; plans beyond the first week are provisional (most next-week plans change).
* The "FPL-style heuristic" baseline is an approximation; the official algorithm is not public.

## 4a. Decision journal and outcome evaluation (M1.4)

Design and methodology: `docs/architecture/decision-journal.md`.

* **No grading from one outcome.** A gameweek's points are one draw from the forecast
  distribution; the evaluation places the result against the decision-time 80 % range and
  expected difference to HOLD and says so, but it does not call a decision good or bad, and one
  manager's journal is not a calibration study. Live-season calibration of the models is still
  separate work (§4).
* **What can be evaluated.** Only plans whose content is stored: the recommendation's plan and
  HOLD, any evaluated Transfer Workbench plan, each optionally with a different captain /
  vice-captain. "I did something else" is recorded as `UNKNOWN` and never reconstructed or
  scored (the counterfactual HOLD and recommended totals are still shown). Chips are evaluated
  only as part of a stored plan; recommendations and workbench runs do not plan chips today.
* **HOLD for a recommendation.** A recommendation stores HOLD's armband but not its XI; the
  journal rebuilds the XI from the decision-time expected points of the unchanged squad (the
  backtest's convention) and labels it *reconstructed*. When those expectations are incomplete,
  HOLD is reported as not scorable rather than invented. Workbench runs store HOLD's lineup.
* **Armband changes.** The decision-time expectation and paired gain belong to the plan as the
  engine built it (with its own captain); when the manager changes the armband, the comparison
  with that expectation is labelled, the expected-difference band is not reported, and the
  plan's own armband is scored separately to show the armband's effect.
* **Bands, not percentiles.** Only the mean, p10 and p90 (and p50 where stored) are kept, not the
  simulation samples, so a result is placed below / inside / above the 80 % range, not at an
  exact percentile.
* **Final means finalised.** Totals stay hidden until every relevant fixture (the decision-time
  clubs of every player in the scored plans) is recorded, and are *provisional* until FPL marks
  the gameweek `finished` and `data_checked` **and** the result rows were loaded after the
  ruleset's finalisation lag (72 hours after the last match for 2026-27 — an assumption, not an
  FPL signal), so a final evaluation arrives about three to four days after the gameweek's last
  match; an official correction after that writes a revised evaluation (the previous one is
  kept). Results are loaded by the `live_results` task (one paced pass of about 11 minutes, then
  a daily re-check for four days after the last match) and evaluated by the hourly journal job;
  FPL corrections after that window are not fetched (§2). If no load lands between the
  finalisation lag and the end of that window (the first load very late, or every retry
  failing), the rows become final only with the next gameweek's load — and after GW38 there is
  none, so such a decision would stay *partial* with provisional totals.
* **Postponements and double gameweeks.** A fixture that moved out of the gameweek after the
  decision is listed and not scored there (FPL scores it where it is played); double gameweeks
  sum both matches. A player who changes club after the decision is judged complete on their
  decision-time club's fixtures.
* **Automatic substitutions** follow the domain rules (bench order, formation, vice-captain
  only when the captain did not play) on per-player gameweek minutes; they reproduce official
  points on the historical rows (§1), but the journal's own bench order is the plan's stored or
  reconstructed order, which may differ from the order the manager set in FPL.
* **Verification** compares squad, captain, vice-captain and chip with the official picks of the
  gameweek, for squads imported with an FPL ID, after the deadline; a manual squad cannot be
  verified, bench order is not compared, and a failed or disabled live source leaves it
  *unavailable* — retried hourly for seven days after the deadline, then only on request. The
  recorded decision is never changed by it.
* **Pre-M1.4 feedback** is kept as decisions without decision-time evidence: shown, never
  evaluated or verified, and the realised points typed into the old form stay self-reported.
* **Deadline rule.** A decision recorded before the deadline cannot be replaced after it; one
  first recorded after the deadline is accepted and labelled non-prospective.
* **Defects 52 and 53** still apply to the sources being journalled: recommendations are
  byte-reproducible on one machine only, and a plan after an inherited club excess may be the
  best valid plan explored rather than a proven optimum (§5).

## 4b. Season Review and the live record (M1.5)

Contract: `docs/architecture/season-evaluation.md`.

* **Live evidence starts at GW7 of 2026-27.** The forecast record began on 2026-10-14. GW1–6
  had no record (their serving forecasts were deleted by retention, and the `predictions` table
  had never been written); they are shown as "before the record" and are never reconstructed
  from later data. A replay of them with current code would be retrospective evidence and is
  deferred.
* **No live verdict before 8 final live gameweeks** (GW7–14, about 10 December 2026). Until
  then every live number is "monitoring"; intervals from three gameweeks on are wide. Three
  formal looks (8, 16, season end) each control a 5 % family-wise error; across the looks the
  chance of at least one false verdict is at most about 15 %.
* **The early window overstates a full-season margin** (past seasons: GW7–14 margins 1.5–1.8×
  those of GW15–38).
* **Calibration is measured, not adjusted.** No recalibration is fitted; a change needs 8 final
  live gameweeks and then a shadow test on at least 6 later ones.
* **The capture depends on the stack running.** A checkpoint is prospective by its recorded
  times, not by when it is captured, and retention keeps the candidate until then — but if no
  forecast is promoted before a cutoff (the stack down for the hours before it), that gameweek
  is recorded as missed.
* **Code identity.** The code digest identifies the installed sources; the build revision is
  whatever the image build declared (`unrecorded` without `FPL_BUILD_REVISION`). Recomputing a
  frozen forecast elsewhere is not bit-identical (defect 52), which is why its numbers are stored.
* **Not evaluated this season:** component expected points (needs a realised-component
  breakdown), P(play), the availability layer on its own (only an availability-flagged segment),
  an external forecast (FPL's own expected points are not ingested; `fpl_style` is a simple rule),
  and the engine's live decisions (no live shadow benchmark; strategy comparisons are past seasons
  only).
* **Finality.** A gameweek waits for result rows reloaded after the finalisation lag; if FPL drops a
  player whose row is still provisional, that gameweek stays `results_not_final` (shown with the
  count), because the shared finality rule is unchanged.
* **Recorded decisions** in the Season Review are one manager's own, few, correlated and
  self-selected; nothing there estimates the effect of following Copilot.
* **Time comes from FPL's server, not a third party.** Promotion and export times come from the
  laptop's clocks (Colima VM, database), which can lag after sleep; they never decide. A forecast
  counts as frozen before kickoff only when FPL's HTTP `Date` (read after the file was written
  and hashed) is before the cutoff; otherwise it is "timing not proven" and never scored. This
  defeats a wrong clock, not an operator who fabricates rows (an RFC 3161 timestamp would). FPL
  news stamps are a leakage check only.
* **One laptop.** If FPL cannot be reached while the laptop is awake before a cutoff, that
  gameweek is "timing not proven"; if the laptop sleeps through the hours before a cutoff, an
  older (still pre-deadline) forecast is frozen. The Colima VM does not start at login. A capture
  or attestation that keeps failing is a failed job every 15 minutes, with no dedicated alert
  (the page shows "capture pending").
* **Statistical assumptions** (contract §6.1, §14): gameweeks are treated as independent although
  form and injuries carry over, so intervals may be somewhat narrow; all-player calibration checks
  are diluted by players who rarely play; exclusions (postponements, departed players) are not
  random.

## 5. Optimisation and computation

* MILP solves are exact within a 60 s limit (`mip_rel_gap` 0); a time-limited solve returns a
  polished, independently validated incumbent without an optimality proof, and the plan says so
  (API `optimality`, report and UI: "proven optimal" or "best found within the solver limit (…);
  not proven optimal"). Measured timings,
  timeout frequency and the cost of candidate pooling: `ml/reports/optimizer_benchmark.md`.
* Inherited club excess (defect 53, open): when the squad already holds more players of a club
  than the limit (a mid-season club move), the MILP caps the club at that initial count for
  every planned gameweek, while the rules allow an excess to be kept or reduced but never
  increased again. A plan that sells one such player now and buys another later is rejected by
  the independent rules replay and never recommended. Its no-good cut also removes the *legal*
  plans that share its first move, so the plans found afterwards are only the best valid plans
  explored and are labelled "best valid plan among those explored; not proven optimal (…)"
  rather than "proven optimal" (pinned by `tests/unit/optimizer/test_inherited_club_excess.py`;
  the golden fixture's cheapest-players squad holds five players of one club and hits this).
  The formulation fix — a per-gameweek allowance that can only fall — is backlog.
* Measured on 48 realistic squad states (`ml/reports/optimizer_benchmark.md`): every chip-open
  solve (all usable chips, 5 GW) ran to the 60 s limit (48 of 48; p50 63.8 s wall) and returns a
  warm-started, validated incumbent rather than a proven optimum; one of 36 8-GW transfer solves
  also hit the limit. Without a warm start and with a 1 s limit, 26 of 48 8-GW chip problems
  found no feasible plan (the engine then falls back to HOLD).
* With every forecast perturbed by 5 % lognormal noise the first-week action stayed the same in
  only 38 % of solves: competing moves are often within forecast noise of each other.
* Candidate pooling (top players by EV / value / cheapest per position) is an approximation; its
  objective cost is measured, not assumed zero (0.000 points lost in 24 of 24 full-universe
  comparisons).
* Timings in `ml/reports/performance.md` and `optimizer_benchmark.md` come from one development
  machine (Apple M4, 10 logical CPUs, 16 GB, macOS); they are not production guarantees. The
  benchmark ran partly on battery power (recorded per run, with CPU time ≈ wall time showing no
  sleep); an earlier attempt that the machine slept through was discarded, not reported.
* Heavy requests are slow. Deployed recommendation jobs (worker with 4 solver processes, live
  GW6 data) take 42 s at 3 GW and 90 s at 5 GW. The Transfer Workbench (M1.2) runs the same
  kind of solves — HOLD, each exact transfer, the optimiser's plan — as one worker job per
  question and serves the stored run afterwards (`docs/BUILD_STATUS.md` §2d has the measured
  timings); it compares HOLD, exact transfers, engine replacements and the optimiser's plans,
  but chip alternatives and the §69 stress scenarios are still only in the full
  recommendation package. The replacement picker endpoint holds an HTTP request open
  while it solves serially: 12.5–13.8 s for one outgoing player in the stage report, 48.9 s for
  another (232 candidates screened) on the deployed API; a 5-GW what-if takes ~8 s.
  `ml/reports/performance_stages.md` shows where the time goes: after parallelising independent
  solves (161 s → 105 s for a 5-GW recommendation, identical output), the floor is the slowest
  single stability perturbation (two exact MILP solves, ~20 s on the development machine) and
  ten full Monte Carlo re-simulations used to value Triple Captain / Bench Boost under a
  postponement (~35 s).
  Re-simulating only the affected players, and reusing historical-season features across hourly
  snapshots (71 % of a cold forecast), are V1.1 work.
* The serving forecast needs ~2 GB of memory while it is computed (worker); it is computed
  asynchronously and the API never trains on a request path in production.
* In-process rate limiting is per API replica; behind the web proxy all browser users share one
  key and therefore one bucket (a busy user can throttle others).

## 6. Security and privacy model

* **Accounts (M1.1a).** Invite-only registration, e-mail + password (scrypt), 30-day sessions
  in an HttpOnly cookie, server-side ownership of every manager key (`docs/DEPLOYMENT.md` →
  *Accounts and beta access*; cross-user access is tested for every manager-scoped route).
  Not yet: password reset, e-mail verification, lockout after failed logins (they are rate-limited
  and logged), two-factor authentication, per-user API tokens for an extension. Operator keys
  retain full access for operations. The web proxy's key must be configured in
  `FPL_WEB_API_KEYS_SHA256`, not the operator list — otherwise the proxy would act as an operator
  for anonymous browsers (the API warns about nothing here; it is an operator responsibility).
* TLS: `docker-compose.public.yml` (Caddy) terminates TLS with automatic certificates; verified
  locally with Caddy's internal CA. Publicly trusted issuance is only exercised on a real domain.
* Webhook SSRF defence: HTTPS-only, no credentials, exact host allow-list, port 443, every
  resolved address global (IPv4-mapped and NAT64 forms unwrapped), no redirects, 5 s timeout.
  **Residual risk:** DNS is resolved again by the HTTP client at connect time, so a rebinding
  window exists between validation and connection; the host allow-list is the primary control
  (an attacker would have to control DNS of an allow-listed host).
* Audit entries store hashes of manager and API keys. Logs pseudonymise manager keys in
  rejected-request paths, and uvicorn access logs are disabled (both printed raw manager keys
  until M14). Manager keys still appear in job request rows in PostgreSQL until the manager's
  data is erased (they are application data, not logs).

## 7. Engineering scope not covered

* Backups are a tested script, not a managed service: scheduling, off-host copies and their
  retention are the operator's. The restore was tested on the same machine into fresh volumes,
  not onto another host or across an image upgrade; the Redis job queue is not backed up (jobs in
  flight at backup time are closed as abandoned after a restore). Rate limiting is per API
  replica.
* **Host sleep (development laptops).** When the Mac sleeps, the Colima VM pauses; on wake its
  clock jumps and RQ judges running jobs past their timeout and kills them. Observed overnight
  on the local stack: four jobs were closed as `interrupted` and the next scheduler buckets
  re-ran them. Nothing partial is published, but long jobs on a sleeping laptop can repeat; a
  real deployment (server/VM that does not sleep) is not affected. Long local runs used
  `caffeinate`.
* The release workflow publishes images from tags; it has not been executed against a real
  registry or cloud environment in this project. Deployment — the public TLS topology included —
  is verified locally with Docker Compose only; there is no public URL until a host, domain and
  secrets are provided (`docs/DEPLOYMENT.md` → *Operator inputs*).
* §43 research extensions (RL policies, rival-response simulation, contextual bandits) are not
  implemented.
