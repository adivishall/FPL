# Changelog

User-visible behaviour changes (§78). Milestones follow `docs/BUILD_STATUS.md`.

## M1.5 — Season Review: the forecast record and live-season evaluation (2026-10-14)

### Added
- **Forecast record.** Before a forecast can be served, the forecast job records a commitment
  to it (the file's hash, its prediction run, the data export time, the database clock). After
  each gameweek's decision cutoff the forecast the API served then is frozen — every player and
  target gameweek, the decision-time baselines and the exact simulated distributions — with
  checks against the database record made before the cutoff and against the data itself (no FPL
  news stamped after the cutoff, no result of the gameweek). It outlives the forecast cache, the snapshot and
  restarts; nothing is ever recomputed for a gameweek that was not recorded. The record started
  on 2026-10-14; 2026-27 GW1–6 are shown as "before the record".
- **FPL server time for the forecast record.** The laptop's clock no longer decides whether a
  forecast was frozen before kickoff: FPL's own server time (the HTTP `Date` of a request made
  after the forecast file was written and hashed) must be before the cutoff. Without it a
  checkpoint is "timing not proven" and never scored; FPL news stamps are a leakage check only.
  Migration 0008 (`forecast_attestations`, state `prospective_unverified`); scheduler job
  `forecast_attest`.
- **Season Review** page (`/season-review`): the live record (forecast frozen before kickoff)
  with its checkpoint timeline; once results are final, per-gameweek tables, the difference from
  each predeclared baseline with intervals over gameweeks, and calibration checks against
  tolerances fixed in advance — "too few gameweeks to say" until 8 live gameweeks are final; the
  past seasons 2023–26 over the same weeks by the same method; your own recorded decisions
  (private); the calibration state (no adjustment has been made); and how every number shown
  in Copilot is or is not evaluated.
- Hourly evaluation of the live record, written only when its inputs change (insert-only, with
  what changed and why); rows whose fixtures moved, whose player changed club or whose result
  is missing are excluded and counted, never scored as zero.
- API: `GET /season-review`, `GET /season-review/evaluations`, `POST /season-review/evaluate`
  (operator), `GET /journal/summary` (owner only). Migrations 0006 (`forecast_promotions`,
  `forecast_checkpoints`, `season_evaluations`; `predictions` keyed by checkpoint) and 0007
  (a promotion per snapshot and forecast key). Scheduler tasks `forecast_ledger` (15 min) and
  `season_eval` (hourly).

### Changed
- Data Health labels the promotion gates "passed on past seasons 2023–26" and points to the
  Season Review for live evidence.
- Forecasts record the digest of the code that computed them and the image's declared build
  revision (`FPL_BUILD_REVISION`; "unrecorded" when the build declares none).

## M1.4 — Decision journal and prospective outcome evaluation (2026-10-12)

### Added
- **Record your decision** under the recommendation on Home and under a plan in the Transfer
  Workbench: follow the recommendation, hold, choose another evaluated plan, change the
  captain / vice-captain, "I'll decide later", or "I did something else", with an optional
  note. Recording against a historical result is allowed and says so.
- Each decision keeps the **decision-time evidence** it was made on — the recommended plan,
  HOLD and your chosen plan with lineups, armbands, hits and their expected points (mean and
  80 % range) and expected difference to HOLD, the squad, the clubs' fixtures, and every
  identifier (snapshot, forecast, model versions, ruleset) — and never changes afterwards. A
  change of mind before the deadline supersedes the earlier record (both kept); after the
  deadline a pre-deadline decision cannot be replaced.
- **What happened**: an hourly job (and "Evaluate now" on the entry page) scores your recorded
  action, holding and the recommended plan on the official results with the FPL scoring rules
  (automatic substitutions, vice-captain, chips, hits): *pending* before the relevant matches,
  *partial* while some are missing (provisional totals once all are in), *final* once FPL
  finalises the gameweek and the results have been reloaded after the finalisation lag (about
  three to four days after the last match). Each result is set against what was expected when you decided, with
  the caveat that one gameweek is a single draw — no decision is graded good or bad from it.
  Official corrections add a revised evaluation; earlier ones are kept.
- **Verification** for squads imported with an FPL ID: after the deadline the official picks
  of the gameweek are compared with the recorded action (matches / differs), without changing
  the record; "I made this move" lets you confirm it yourself.
- Journal page: "Your decisions" with filters (gameweek, action, evaluation, superseded), the
  recommendations table shows your decision for each, and an entry page shows the four
  questions, the evidence, provenance and the evaluation history.
- API: `GET/POST /journal`, `GET /journal/{id}`, `POST /journal/{id}/confirm`,
  `POST /journal/{id}/evaluate`; migration 0005 (journal columns, `decision_evaluations`);
  scheduler task `journal` (hourly). Export and erasure include the journal.

### Changed
- `POST /decisions/{id}/feedback` records a journal decision: "followed" is the recommended
  plan; "ignored" / "partial" are recorded as "something else" (not evaluated). Realised points
  typed by hand are kept as self-reported and are never used as the outcome.
- Existing feedback rows are migrated as legacy decisions whose evaluation and verification
  are *unavailable*: no decision-time evidence was stored for them, so none is invented; their
  entry pages say so.
- `GET /decisions` feedback rows carry `entry_id` and `state` (active / superseded).

## M1.3 — Snapshot-aware invalidation and "What changed?" (2026-10-11)

### Added
- **Result lifecycle**: every recommendation, Transfer Workbench run and Copilot Home analysis
  says whether it is *current*, *historical* (stale or superseded), *computing* or *failed*, and
  why: a newer data snapshot was promoted (recorded with both snapshot ids and when it was
  detected), the squad was saved again (old and new state), or a job finished after its inputs
  had moved on. A promoted snapshot invalidates every result on an older one; a worker that
  started against old inputs stores its result as historical and never replaces the current one.
- **What changed?** on Home and the Transfer Workbench: the stored-state diff (players, bank,
  free transfers), the snapshot-to-snapshot diff for the players and gameweeks the result used
  (availability, chance of playing, news, price, fixture counts, changed tables) when the older
  snapshot is still stored, and the forecast versions; otherwise an explicit "not available"
  with the identifiers. Regenerate / re-evaluate actions reuse the existing jobs.
- Journal entries and the Future Planner show the same verdict (current / superseded /
  historical, with the reasons); a historical plan in the Future Planner is labelled so and
  gives no "play now" chip advice.
- Asking the same question again on inputs that are current again (a squad saved back) makes
  the stored answer current again instead of leaving the manager with only historical results.
- API: `lifecycle` block on affected responses (with `served_snapshot_id`, the promoted
  snapshot the verdict is judged against, and `snapshot_stale`), on every `/decisions` row,
  `GET /recommendations/{id}/changes`, `GET /workbench/runs/{id}/changes`; migration 0004 adds
  `invalidated_at` and `invalidation_json` to `recommendations` and `workbench_runs`.

### Changed
- `GET /recommendations/current` serves the most recent recommendation marked historical when
  none is current (previously a 404), so the page can explain and offer to regenerate.
- A recommendation requested earlier but finished later no longer replaces the answer to a
  newer request; concurrent saves for one gameweek are serialised.
- A Transfer Workbench run is computed on exactly the snapshot and forecast it names, even when
  the worker has already loaded a newer export; Retry asks the failed run's own question again.
- The API swaps to a newly promoted snapshot on the next verdict, regeneration or evaluation
  rather than at its next periodic check.
- Optimiser honesty (defect 53): plans found after the optimiser's preferred plan failed the
  rules replay are labelled "best valid plan among those explored; not proven optimal (…)"
  instead of "proven optimal" — the cut that removed the illegal plan also removed the legal
  plans sharing its first move. Plans and numbers are unchanged.

### Fixed
- Ownership: a request naming a manager key in the query and another in the JSON body is
  checked for both (previously only the query key was checked, so a signed-in user could act
  on another manager's data through the body).
- A job whose enqueue fails is marked failed at once, so the same request can be retried.
- With a snapshot pinned (`FPL_SNAPSHOT_DIR`, the documented data rollback), results on any
  other snapshot — including the newer one rolled back — are historical, not current.
- A Transfer Workbench run whose job was given up on can be retried (it no longer stays queued
  forever), and a run computed on other inputs than it names is recomputed rather than shown
  as current. Home labels the "best valid plan among those explored" optimality correctly.
- Pages no longer show their "nothing here yet" state (e.g. "No recommendation" with a Generate
  button) for a moment before their data starts loading.

## M1.2 — Whole-squad Transfer Workbench (2026-10-11)

### Added
- **Transfer Workbench** (replaces the one-player Transfer Lab): choose a player to sell, ask for
  the engine's replacements or name a specific one, include the optimiser's best plan (with or
  without hits, with a cap on transfers now), pick a horizon up to the served maximum, and compare
  every plan against *holding* on the whole squad: net gain, gross gain, hit cost, p10/p90,
  probability of beating hold, bank and free transfers after, solver status (proven optimal or
  best within the limit), and a details view with the squad after the move, the gameweek
  timeline and later moves. Illegal requests are listed with the rule they break, never shown as
  plans. Evaluations run as background jobs bound to the exact squad state; a changed squad or
  snapshot marks the run stale.
- API: `GET /workbench/context`, `POST /workbench/evaluate`, `GET /workbench/runs`,
  `GET /workbench/runs/{id}`; runs are exported and erased with the manager.

### Changed
- The optimiser accepts two first-gameweek preferences (exact moves now; a cap on transfers now);
  the defaults leave every existing plan and identifier unchanged (golden parity kept).

## M1.1a — Copilot Home and beta foundation (2026-10-10)

### Added
- **Accounts (invite-only beta)**: register with an invitation code, sign in / out, sessions in an
  HttpOnly cookie; every manager key belongs to one account and is checked server-side on every
  request; account deletion erases everything the account owns.
- **FPL-ID onboarding**: validate an FPL ID, preview whose team it is, confirm, import the real
  squad (bank, free transfers, chips) from the public FPL API; refresh any time; clear errors for
  unknown IDs and outages. Manual entry remains as a fallback.
- **Player picker**: search by name with club, position, affordability and status, keyboard
  selection, on the Squad page.
- **Copilot Home**: situation, squad health signals with sources, the recommended action, captain
  profiles, the squad with forecasts, status and prices, and an FPL-difficulty fixture outlook —
  served from an analysis the worker precomputes (warm p95 17 ms on the development machine).
- **First-party analytics** (opt-out in Settings, 90-day retention) and an operator summary.

### Changed
- Planning horizons beyond the validated 5 gameweeks are refused instead of silently shortened;
  a recommendation evaluates exactly the squad state it was asked for and says when the squad
  changed since.
- The web proxy's API key is a separate class (`FPL_WEB_API_KEYS_SHA256`) that cannot act on
  manager data without a signed-in user.

## V1 release work (2026-10-07)

### Added
- **Public deployment overlay** (`docker-compose.public.yml`): a Caddy reverse proxy is the only
  public listener — automatic TLS certificates, HTTP→HTTPS, HSTS and security headers, and one
  site login (V1 is single-tenant); its logs carry no request URIs (they contain manager keys).
- **Backup and restore**: `infra/scripts/backup.sh` (database dump verified readable, canonical
  snapshots and raw captures, checksums) and `restore.sh` (fresh volumes, migrations, forecast);
  a restore into empty volumes was verified table by table.
- Plans state their optimality: "proven optimal" or "best found within the solver limit; not
  proven optimal" (API `optimality`, recommendation report, overview, squad builder, replacement
  candidates).
- `smoke.py` checks a public deployment through its web origin alone (TLS, HSTS, redirect, site
  login); the release workflow's smoke step supports it.
- **Live completed-match results.** `fpl-ingest live-results` and the scheduler task
  `live_results` load the current season's per-fixture results from the FPL API's player
  histories (`element-summary/{id}`) through the archive's contracts and quality gates — with
  each fixture's price and ownership, results tables only, never deadlines or the schedule.
  It runs when a finished gameweek has no results and daily during a 4-day correction window,
  then exports a snapshot and precomputes its forecast. Live responses are no longer degraded
  for missing results.

### Changed
- Recommendation jobs solve their independent MILPs (stability perturbations, Wildcard / Free Hit
  weeks) in `FPL_SOLVER_WORKERS` worker processes; output is identical for any value. A 5-GW
  recommendation went from 161 s to 105 s in the 4-vCPU worker container
  (`ml/reports/performance_stages.md`, generated by `infra/scripts/perf_stages.py`).

### Fixed
- The overview showed "0 %" confidence next to every HOLD; it now says no move cleared the
  thresholds.
- Settings: a late response for a previous manager key can no longer overwrite the current one
  (regression test added).
- Settings: edits made while the stored settings were still loading were overwritten by the late
  response, so Save posted the stored values; a failed load left editable defaults that Save
  would have written over the stored settings. Server-backed fields and Save are now disabled
  until the stored values arrive; a failed load is shown with a Retry button.
- A live bootstrap payload that fails its schema is quarantined instead of leaving its job
  `running`.

## M14 — final verification (2026-10-05)

### Fixed
- **Backtest look-ahead (fixture schedule).** Archived seasons hold only the final schedule;
  postponed fixtures were visible to decisions made before the postponement. The point-in-time
  view now shows the schedule as published at each cutoff (rule S1, ADR-0004). All three seasons
  were re-run; earlier backtest numbers are superseded (comparison in `ml/reports/backtest.md`).
- Players who change club mid-season now count against their new club in the state machine and
  the optimiser (previously the club at purchase), so legal plans are no longer rejected.
- Traceability chains stay complete when a newer snapshot has identical features, and live
  recommendations made before their decision cutoff are no longer flagged as broken.
- The optimiser no longer values free transfers, bank or squad value banked past the last
  gameweek of the season, and a time-limited solve's polished incumbent now has its hits and
  objective recomputed (it could under-count hits).
- Live squad sync reports an FPL outage (503) separately from an unexpected payload (502);
  programming errors are no longer reported as "API unavailable".
- Jobs whose worker died (OOM kill, container replaced) no longer block identical requests
  forever; the scheduler reaps them and a new attempt starts.
- A restarted worker container no longer crash-loops on its own stale Redis registration.
- `/health` reports the real outcome of the latest live capture instead of a fixed
  "unreachable" note, and answers in milliseconds (the snapshot id was re-hashed on every call).
- Manager-linked reads (squad, settings, notifications, decisions, recommendations, jobs) now
  require an API key when keys are enabled; uvicorn access logs (raw manager keys in URLs) are off.
- The web proxy returns JSON 502/504 when the API is unreachable or slow (was a bare 500).
- Docker: `/data` is writable by the non-root user on a fresh volume; worker and scheduler have
  their own health checks; services restart automatically.

- Two managers with identical squads no longer share one recommendation record (the second
  manager saw none; erasing the first removed it).
- Worker liveness probe no longer reports a working idle worker as dead (RQ heartbeats only
  every ~405 s while idle; RQ's worker set can lose a live worker after the host sleeps).
- Forecast and price caches, serving markers and snapshot manifests are published atomically:
  a process killed mid-write no longer leaves a truncated file behind.
- Logs of rejected requests no longer contain manager keys from URL paths.
- Malformed webhook URLs are rejected (422) instead of failing with a 500.
- Explanations name players, teams and chips instead of showing codes and list literals.
- Backtest Lab renders reports (tables, headings, figures in place) instead of raw Markdown, and
  shows every figure a report references (the 2023-24 and difference charts were missing).

### Added
- Retention of live snapshots and their caches (hourly `retention` job; configurable).
- Failed jobs are classified as `error` (code raised) or `interrupted` (worker killed, lost or
  timed out); `fpl_jobs_failed{kind,failure}` and a separate `JobInterruptions` alert.
- Freshness reports finished gameweeks whose match results are missing.
- Snapshot promotion: the API switches to a newer snapshot only once its forecast is ready.
- Worker metrics (recommendations, optimisation) are exported from the forked job processes.
- Resumable, checkpointed backtests pinned to a configured snapshot; three-season report with
  forecast calibration, interval coverage, start-probability reliability, chips, stability,
  accounting checks and reproducibility; optimiser benchmark on realistic workloads; serving
  performance report; alert rules replayed on real data; production-topology Playwright suite.

## M13 — production hardening (2026-10-02)
Notifications with materiality thresholds and webhooks (SSRF-guarded), privacy export/erasure,
Prometheus metrics and alert rules, recommendation traceability, RQ worker and scheduler, Docker
deployment with a server-side web proxy.

## M12 — web UI
Decision-first Next.js UI: overview, transfer lab, squad and future planners, player lab,
what-if, journal, backtest lab, data health, settings.

## M11 — API
FastAPI service, async jobs, persistence, API keys and rate limits.

## M6–M10 — optimiser, decision engine, backtesting
MILP transfer/chip planner with independent validation, replacement engine, HOLD-vs-move
decisions with paired Monte Carlo evidence, scenarios, walk-forward backtesting.

## M0–M5 — foundation, data, rules, forecasting
Versioned rulesets, point-in-time data platform, scoring engine, probabilistic minutes and
points forecasts with calibration and a model registry.
