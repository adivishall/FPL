# BUILD STATUS — FPL Decision Engine

Persistent project status (spec §50.1 item 10). Rewritten at the end of M14 from the verified
state of the repository; numbers below come from commands run on the development machine
described in §3 and from the generated reports they cite.

Legend: ☑ done **and** verified · ◐ partial (see notes) · ✗ not done (reason given)

---

## 1. Milestone summary

| # | Milestone (§87 / §91) | Status | Gate | Evidence |
|---|------------------------|--------|------|----------|
| M0 | Discovery: ADRs, source inventory, ruleset schema | ☑ | No coding ambiguity remains | `docs/decisions/`, `docs/architecture/source-inventory.md` |
| M1 | Foundation: monorepo, CI, Docker, migrations, domain models, config | ☑ | Clean build + tests pass | migration round-trip test; CI green on every pushed commit |
| M2 | Data: ingestion, canonical schema, quality gates, freshness | ☑ | Reproducible historical snapshot | `snap_b64560a8c4f434ad984e` rebuilt identically on macOS and in the Linux container |
| M3 | Rules: scoring, transfers, chips, squad validity, state machine | ☑ | Rule/property tests pass | official points reproduced on 113,870/113,870 rows (slow tier) |
| M4 | Forecasting baselines, PIT features, team strength | ☑ | Metrics reproducible | `ml/reports/baselines.md` (regenerated under rule S1) |
| M5 | ML forecasting: minutes, distributions, calibration, registry | ☑ | Out-of-sample improvement | `ml/reports/forecast_eval.md`: RMSE 1.924 vs 2.031 best baseline; gates PASSED |
| M6 | Optimiser: single-GW, replacement, captaincy | ☑ | 100 % legal squads | brute-force verification; benchmark validity in `ml/reports/optimizer_benchmark.md` |
| M7 | Multi-GW planner, chips, scenarios | ☑ | Historical scenario tests pass | three-season replays incl. 18 engine chip plays |
| M8 | Decision engine: hold/transfer/hit/chip, risk, stability | ☑ | Recommendation contract stable | decision-engine tests; API contract tests |
| M9 | Explainability: evidence, counterfactuals | ☑ | No unsupported reasons | evidence tests; E2E recommendation |
| M10 | Walk-forward backtesting, benchmarks, reports | ☑ | No-lookahead audit passes | `ml/reports/backtest.md`: 3 seasons, 0 violations, rule S1 (leak found and fixed in M14) |
| M11 | API + async jobs + cache | ☑ | Contract tests pass | `tests/integration/test_api.py`, `test_worker.py` |
| M12 | UI (Next.js) + e2e | ☑ | End-to-end flow works | dev flows 7/7 + 19/19 production-topology flows (through the TLS proxy) |
| M13 | Production hardening | ☑ | Clean production smoke test | Docker Compose verified locally (health, persistence, crash recovery, E2E) |
| M14 | Final verification, audit, limitations, portfolio docs | ☑ | A reviewer can reproduce core claims | `docs/FINAL_AUDIT.md`, `docs/KNOWN_LIMITATIONS.md`, README, this file |

The specification audit (`docs/FINAL_AUDIT.md`) is stricter than this table: it counts every
requirement and marks partial work as PARTIAL (99 requirements: 81 COMPLETE, 15 PARTIAL, 2 NOT IMPLEMENTED, 0 BLOCKED, 1 NOT VERIFIABLE).
V1 release work after M14 is planned and logged in `docs/V1_RELEASE_PLAN.md`; its verification
records are §2a and §2b (final). Public deployment waits on operator inputs (host, domain,
secrets); everything else for V1 is done. Product milestones after V1 are recorded in §2c
(M1.1a), §2d (M1.2), §2e (M1.3), §2f (M1.4) and §2g (M1.5).

## 2. M14 verification record (2026-10-04 – 06, local machine)

| Area | Result |
|---|---|
| Repository | branch `claude/modest-noether-qauiji` restored from the remote onto a local clone that only had `main` |
| Toolchain | uv, Node 22, PostgreSQL 16, Redis, Colima + Docker installed locally |
| Test suite | 379 Python tests, all passing: fast tier (unit, property, integration on real PostgreSQL + Redis) 374 passed, 0 failed, 0 skipped in 3 min 41 s; slow tier 2/2 (official points on 113,870 rows); network tier 3/3 (ruleset vs the live FPL API). TypeScript `tsc --noEmit` clean |
| Lint / types / layering | ruff, ruff format (238 files), mypy (109 source files), import-linter 4/4 contracts: clean |
| Security scans | pip-audit: no known vulnerabilities; npm audit: 0; gitleaks: no leaks in all 23 commits of the branch; every changed file also checked for the real keys and DB password (0 hits) |
| Live FPL API | reachable locally; bootstrap + fixtures captured and canonicalised; live squad sync verified against official picks (incl. Free Hit reversion); current gameweek matches `is_next` |
| Data reproducibility | canonical snapshot id identical on host and in container |
| Backtest | 2023-24, 2024-25, 2025-26 × 7 strategies × 38 GWs; checkpointed; 100 % valid; 0 look-ahead violations. Three full runs: run 1 → run 2 identical in 17/21 strategy-seasons (the 4 others diverge exactly where the club-move fix applies); run 2 → run 3 (published) identical in 11/21, the other 10 diverging only from GW34 on, where the season-end fix applies |
| Leakage audit | fixture-schedule look-ahead found (postponements visible early) → schedule rule S1; previous backtest superseded and archived |
| ML evaluations | forecast, baseline, price, rate-shrinkage, feature-importance, ensemble/conformal reports regenerated under rule S1; promotion gates still pass |
| Optimiser benchmark | current code, 12 cutoffs × 4 squad types × 3 seasons, 612 solves + 48 alternative sets: 100 % of 634 returned plans valid; 5-GW p50 3.9 s / p95 13.4 s; 8-GW p50 16.0 s / p95 43.5 s; chip-open always at the 60 s limit (48/48); forced 1 s timeouts without warm start: 26/48 no plan, 22/22 incumbents valid; re-solves 100 % deterministic (`ml/reports/optimizer_benchmark.md`). Two optimiser defects found by it and fixed: season-end terminal values; hits and objective of polished incumbents |
| Performance | `ml/reports/performance.md` (final images, 2026-10-05): deployed reads via proxy p50 22–42 ms, p95 25–45 ms (p99 ≤ 101 ms); cold forecast 71.8 s, warm 0.03 s (disk) / 0.06 ms (memory); replacement 17.7 s, what-if 8.1 s, 5-GW optimise 16.1 s (p50); full recommendation 136–178 s in-process (5 GW), 48 s via the worker (3 GW); alert job round trip 1.6 s. A first probe run crashed (its job poller tripped the API rate limit); the poller was fixed and the full probe re-run |
| Docker | images build (python, web); Compose: 7 services healthy; migrations; health checks for api/web/worker/scheduler; persistence across down/up; worker crash → restart → healthy in 32 s |
| Playwright | production topology (browser → Next proxy → API key → API → Postgres/Redis/worker) on the final images: 17/17 (adds captain/bench and Backtest Lab rendering); development flows: 7/7 |
| Alerts | every reachable §74 path replayed on real data (`ml/reports/alerts_audit.md`); availability alerts unit-tested only |
| Prometheus | config and 12 alert rules validated by promtool; live in Compose (`--profile monitoring`): api and worker targets up, rules loaded, recommendation/optimiser metrics from forked worker jobs visible |
| Retention | hourly `retention` job on the live stack: 8 snapshots + 22 cache files (326 MB) removed, 25 kept (24 newest + pinned evaluation snapshot) |
| Logs and privacy | after the final E2E run, all 10 container logs: 0 API keys, proxy key, DB password or raw manager keys; audit log holds hashes only; erased managers leave no rows |

### Defects found and fixed during M14

1. Backtest look-ahead: archived final fixture schedule (rule S1, `fpl_storage/schedule.py`).
2. Schedule solver blew up memory (6.6 GB) on a complete live season → short-circuit + node limit.
3. Club limit used clubs at purchase; mid-season moves made legal plans "illegal" (domain + MILP).
4. Traceability: content-addressed feature ids linked to the wrong data snapshot; a live-only
   invariant flagged every live recommendation.
5. Forecast key and computation could read different snapshots during a hot swap.
6. Jobs of a killed worker stayed `running` forever and blocked identical requests (leases,
   RQ status check, scheduler reaper).
7. Restarted worker crash-looped on its own stale Redis registration.
8. Worker metrics were lost in forked job processes; Prometheus never saw recommendation metrics.
9. `/health` claimed the live source was unreachable (hard-coded) and took 3–14 s (snapshot id
   re-hashed on every access) → container health checks failed.
10. Manager-linked GET routes were readable without an API key.
11. uvicorn access logs printed manager keys in URLs.
12. Docker: `/data` not writable by the non-root user on a fresh volume; worker/scheduler used
    the API's HTTP health probe; no restart policies.
13. Each live refresh opened a "forecast not ready" window → snapshot promotion.
14. Proxy returned a bare 500 when the API was unreachable → JSON 502/504.
15. Live sync reported every failure (incl. bugs and schema drift) as "API unavailable".
16. Backtests were not durable (results only at season end) and not pinned to a snapshot.
17. Feature cache writes were not atomic under parallel runs; cache entries were not validated
    against their cutoff.
18. `dev_postgres.sh` did not work on macOS / paths with spaces.
19. Worker liveness probe reported a working worker as dead: 180 s limit vs RQ's ~405 s idle
    heartbeat, and lookup through RQ's global worker set, which a host sleep emptied.
20. Forecast / price caches, serving markers and snapshot manifests were written in place: a
    process killed mid-write left a truncated file that every later request would fail to load.
21. Job failures did not distinguish environmental interruption (killed, lost, timed out) from
    code errors; reaper failures were invisible to Prometheus (scheduler serves no metrics).
22. No retention: hourly refreshes grew `/data` by ~1 GB/day (snapshots, feature caches, forecasts).
23. Rejected-request logs printed manager keys contained in URL paths.
24. Malformed webhook URLs (`https://[::1`, out-of-range ports) caused a 500 instead of a rejection.
25. Backtest Lab showed reports as raw Markdown with a hard-coded figure list that omitted the
    2023-24 and difference charts.
26. Optimiser benchmark report counted solves that returned no plan as "invalid"; the
    performance probe's job poller tripped the API rate limit.
27. Two managers with identical squads shared one recommendation (ids were content hashes of
    the computation alone): the second saw none, and erasing the first deleted it. Stored
    recommendation and optimisation-run ids are now per manager.
28. Explanations showed player codes, team codes and Python list literals ("out [502500]",
    "team(s) [8]", "['wildcard_2', …]") instead of names.
29. A development Playwright flow was flaky (one-shot visibility check raced the API under load);
    fixed and verified twice under artificial CPU load.
30. Test runs that aborted early (an early production E2E run, the crashed probe run) left two
    test managers stored; both were erased through the privacy endpoint, and both harnesses now
    clean up even when they fail midway.

## 2a. V1 release work record (2026-10-06 – 07, local machine)

| Area | Result |
|---|---|
| Live completed-match results (R1) | `live_results` job on the Compose stack: 667 players, 3,216 rows, GW1–GW5 in 11 min 20 s (2,606 new rows; 610 GW1 revisions changing only source and lineage; 0 conflicts). All 50 finished fixtures have rows for both sides and two team rows; fixture scores equal team goals; official deadlines unchanged and equal to the API's. Second run: 11 min 10 s, 3,216 + 100 rows unchanged, nothing inserted, updated or revised. Pinned evaluation snapshot unchanged (hash-verified; 2026-27 GW1 only). API `status: ok`, not degraded, after promotion |
| Settings race | reproduced on the pre-fix web image by holding the settings GET: an edit made while it was in flight was accepted, then overwritten by the late response, and Save posted the stored value (`Europe/London`) instead of the edit. A failed load silently left editable defaults that Save would post. Both new production tests fail on that image and pass on the fixed one |
| Images under test | web `sha256:f68e999b…` (built 2026-10-07 02:34 UTC, served bundle contains the fix); engine `sha256:2a552f2f…` for the R1 runs, then `sha256:fb8c28b5…` (2026-10-07 04:13 UTC, parallel solves; API with 1 solver process, worker with 4) for the final production run |
| Playwright | production topology 18/18 on each engine image (health `ok`, not degraded, at the start); development flows 7/7 |
| Test suite | fast tier 384 passed, 0 failed, 6 deselected (slow + network) in 5 min 26 s; ingestion / live / worker set 58 passed incl. network; network tier 4/4 against the live API |
| Lint / types / layering | ruff, ruff format (240 files), mypy (109 source files), import-linter 4/4: clean; `tsc` clean; `next build` clean |
| Performance (R2) | profiled before changing anything (`ml/reports/performance_stages.md`, worker container, 4 vCPUs, live GW6 data): cold forecast 70.6 s = feature building 49.8 s + training 16.7 s + Monte Carlo 4.0 s; 5-GW recommendation 161.4 s = stability 82.3 s + chips 62.5 s (Wildcard/Free Hit week solves 26.2 s, fixture-shock re-simulations 36.3 s) + optimise 9.1 s + scenarios 4.9 s; ~76 % of it in exact MILP solves. Independent solves now run in `FPL_SOLVER_WORKERS` processes: 161.4 s → 104.9 s (stability 41.8 s, chips 48.9 s), replacement picker 13.8 s → 12.5 s, output identical. Deployed jobs: 42.2 s (3 GW), 90.4 s (5 GW). cProfile was not used for stage times: it inflated HiGHS solve time 3–4× (the solver runs in a thread with Python callbacks) |
| Source outage | 10 consecutive hourly captures failed (2026-10-06 13:00 – 2026-10-07 02:20 UTC, source unreachable while the host slept): every job ended `failed` with its reason, none stayed `running`; the API flagged the data stale and degraded, and the next capture restored `ok` without intervention |

### Defects found and fixed in the V1 work

31. Completed-match results of the live season were never ingested, so every live response was
    degraded and live forecasts used results through GW1 only.
32. Settings page: a late response for the stored settings overwrote edits made before it
    arrived, so Save posted the stored values; a failed load left editable defaults that Save
    would have written over the stored settings. Server-backed fields and Save now stay disabled
    until the stored values load; a failed load is shown with a Retry; a response for a previous
    manager key is discarded.
33. A bootstrap payload failing its schema raised out of the live capture and left its job
    `running`; it is now quarantined like any other contract failure.

## 2b. V1 release verification (2026-10-07, final images)

Images: engine `930b0f48…` (api, worker, scheduler), web `bbcaaa05…`, Caddy 2.10.2
(`caddy@sha256:4c6e91c6…`); public overlay with `FPL_PUBLIC_DOMAIN=fpl.localhost`,
`FPL_TLS=internal`.

| Area | Result |
|---|---|
| CI | green on the release commit `b925d72` (lint, test, optimizer-suite, web-e2e, security, containers), run 37590591025 |
| Python | fast tier 384 passed, 0 failed (6 deselected); slow + network tiers 6 passed (official points on all historical rows; live-API contracts); ingestion / live / worker / retention set 64 passed |
| Lint / types | ruff, ruff format, mypy (110 files), import-linter 4/4, `tsc`, `next build`: clean |
| Playwright | production topology 19/19 through Caddy (TLS + login) → web proxy → API → Postgres / Redis / worker; development flows 7/7 |
| TLS | chain valid against the CA, TLS 1.3, TLS 1.1 refused; HTTP `308` → HTTPS with path and query; `401` without / with a wrong login; HSTS, nosniff, frame, referrer and permissions headers; no `Server` / `X-Powered-By`; API, web, Postgres, Redis on loopback only; smoke test through the public origin 5/5 |
| Backup / restore | backup with writers paused: 16.0 MB dump (38 tables with data), 82.5 MB data archive, checksums; restored into a new Compose project with empty volumes (full public topology) in 78 s; restored dump: 40/40 row-count lines equal; manager squad, settings, recommendation id, decision, optimality equal; trace complete; new recommendation + alert jobs succeeded; restored scheduler captured live data; new TLS certificate served behind the login |
| Live data | GW1–GW5 3,216 rows, lineage to the succeeded bundle job, 0 conflicts, GW1 revisions provenance-only, deadlines equal to the live API, historical seasons vaastav-only, pinned evaluation snapshot unchanged (GW1 only), no job stuck `running` |
| Security | log scan of 10 containers after the final run: 0 lines with any of 6 secrets, 0 raw manager keys; gitleaks over the full history (32 commits): no leaks; pip-audit and `npm audit` (production dependencies): no known vulnerabilities; webhook SSRF tests in the fast tier; export / delete / audit exercised (E2E and manual erasure of test managers) |
| Retention | 28 successful retention jobs in two days; 26 snapshots on disk; pinned evaluation snapshot kept |
| Observability | Prometheus: api and worker targets up, 12 alert rules loaded, none firing; request, freshness, forecast, job, queue, optimisation, recommendation, model-metric and security series present |
| Performance | `ml/reports/performance_stages.md`; deployed recommendation jobs 42 s (3 GW), 90 s (5 GW); replacement picker 12.5–48.9 s |

**Re-verified on the release commit `b925d72` (2026-10-07, after CI went green)**, same images,
public overlay rebuilt from scratch (new internal CA): ruff, mypy, import-linter clean; fast tier
384 passed; slow + network tiers 6 passed; `tsc`, `next build` clean; TLS chain, TLS 1.3, TLS 1.1
refused, `308` redirect, `401` gate and headers re-checked from the host; smoke test through the
public origin 5/5; production Playwright 19/19 through Caddy; only Caddy publishes ports; a probe
request with a manager key in its path, query and header left no trace in the Caddy, web or API
logs; worker, scheduler, API and web restarted together — all healthy in 33 s, the next forecast
and retention jobs succeeded; Prometheus: both targets up, 12 rules, none firing; live data:
GW1–GW5 finalized (3,216 result rows, one capture each), GW6 upcoming with the live deadline.

### Defects found and fixed in the V1 release pass

34. Plans were never labelled with their optimality: a time-limited incumbent looked the same as
    a proven optimum, and the squad builder always said "Optimal squad". The API, report and UI
    now say "proven optimal" or "best found within the solver limit; not proven optimal".
35. The overview showed a red "0%" confidence next to every HOLD (the probability that holding
    beats holding) — read as "0% confidence". HOLD now shows why no move was chosen.
36. Replacement candidates did not show when their plan was time-limited.
37. The new TLS proxy's error log printed request URIs, which carry manager keys — found by
    provoking an upstream failure before release; URIs are now dropped from its logs.
38. The running Prometheus predated one alert rule (rule files load at start-up): restarted;
    the runbook now says so.
39. The smoke test required direct API access, which a public deployment does not expose; it
    now also runs through the public origin, behind the site login.

### Integrity pass (2026-10-10)

40. Serving accepted planning horizons up to 8 (and request schemas up to 10, silently clamped)
    while the minutes model is trained on target horizons 0–4 and the forecast evaluation covers
    h0–h4 only. The contract is now explicit (`fpl_forecasting.pipeline.SUPPORTED_HORIZON` = 5):
    settings fail at start-up beyond it, request schemas and the container refuse longer
    horizons with `422 unsupported_horizon` instead of clamping, `/gameweeks/current` reports
    `horizon_max`, and the performance scripts follow the constant. Extending to 8 GW is a
    backlog item that requires training and evaluating h5–h7 first.
41. The recommendation job received a `state_id` but evaluated the manager's *latest* state, so a
    squad saved while a job was queued was silently substituted. The job now loads exactly the
    requested state (owned by that manager) and fails clearly when it is unavailable;
    `/recommendations/generate` accepts an explicit `state_id`; recommendation responses carry
    `state_id`, `stale_state` and `latest_state_id`; re-saving an earlier squad makes it the
    latest state again. Tests: `tests/integration/test_state_selection.py`.
42. `KNOWN_LIMITATIONS.md` said models were "checked with PSI/ECE monitors"; the PSI and
    retraining-decision functions have no runtime caller and the calibration alert reads published
    report values. The limitation, the audit rows and the drift runbook now say so.
43. The CI workflow header promised a "backtest smoke" job; the bounded walk-forward run lives in
    `tests/integration/test_backtest.py` inside the test job. The header now says exactly that.

## 2c. M1.1a — Copilot Home and beta foundation (2026-10-10)

Scope (`docs/PRODUCT_GAP_ANALYSIS.md` §6, first milestone): invite-only identity with manager
ownership, FPL-ID onboarding, a real player picker, a precomputed Copilot Home, first-party
analytics — with the engine untouched (golden parity).

| Area | Result |
|---|---|
| Identity | `users`, `invites`, `sessions`, `user_managers` (migration 0002); scrypt passwords; hashed 30-day sessions; operator / web / user / anonymous principals; ownership enforced by a router dependency on every request naming a manager key and inside record routes; `fpl-api create-invite`; account erasure cascades |
| Onboarding | `GET /fpl/entry/{id}` preview (404 for an unknown ID, 503 outage, 502 contract), `/onboarding` 3-step page, `POST /squad/sync` wired from the web with the same error mapping; "Refresh from FPL" on Home and Squad; manual entry kept as a diagnostic fallback |
| Player search | `components/player-picker.tsx` over the real pool (`/players`, limit raised to 1000): name, club, position, affordability, status badges, keyboard selection, empty and error states; used on the Squad page |
| Copilot Home | `/copilot/home` serves `squad_analyses` rows computed by the `squad_analysis` job (per manager × state × snapshot × forecast × version): situation, health signals (availability, minutes, fixtures incl. blanks/doubles/tough runs, bench cover, price risk, affordability — each with evidence and source), captain profiles (shared `lineup_and_captaincy` with `POST /lineup`), squad table, FPL-difficulty fixture outlook; pending/stale states; scheduler fans the job out after every forecast precompute |
| Analytics | `product_events` (allow-listed names and properties, user-keyed, opt-out server-side, 90-day retention in the retention job, operator summary); web `track()` fire-and-forget |
| Golden parity | `tests/integration/test_golden_parity.py` pins the recommendation package for a fixed squad on the fixture snapshot, byte-exact per platform (`tests/integration/golden/recommendation_package.<platform>.json`: `darwin-arm64` from the development machine, `linux-x86_64` from the CI artifact `golden-package-linux-x86_64`); regenerate only with `FPL_UPDATE_GOLDEN=1` (or from that artifact) after independent validation |
| Latency (development machine, Docker) | `infra/scripts/home_latency.py`: first `GET /copilot/home` after a squad change 21 ms (answers `pending`), analysis ready after 2.6 s in the worker, warm p50 12 ms / p95 17 ms over 20 calls; `GET /squad` p95 11 ms; previous per-page `POST /lineup` 33 ms |
| Tests | fast tier 414 passed (new: `test_identity.py` 6, `test_onboarding_api.py` 3, `test_copilot_home.py` 4, `test_state_selection.py` 5, `test_horizon_contract.py` 7, golden parity 1); migrations 3/3; dev browser flows 11 passed (signed-in setup, onboarding, picker, Home); production browser suite (21 flows, signed-in user, new images): 21 passed, 0 failed on 2026-10-11 (closeout run, below), including the real FPL-ID import of a public entry through the proxy; the 2026-10-10 run had 20 passed with the import flow blocked by the FPL API's post-deadline HTTP 503 (the UI surfaced the designed outage message) |

Decisions and defects: 44. The `chance_of_playing` evidence item crashed on a nullable NA (not NaN) —
exposed by the golden test's cheapest-players squad; guarded, no output change for non-NA cases.
45. The alerts idempotence browser test assumed no alert would ever fire; within a deadline window
the same-minute de-duplicated job shows the same result twice — the assertion now accepts either
an identical result or zero new alerts. 46. Changing the forecast horizon on a running stack leaves
the API answering `503 forecast not ready` until the next precompute (documented under upgrades).

### M1.1a closeout (2026-10-11)

| Check | Result |
|---|---|
| CI | `f33584c` success; `648d6d4` cancelled (superseded within a minute by `5eaa252`); `5eaa252` failed in the test job on `test_golden_parity` only (407 passed, 6 skipped): the golden generated on macOS/arm64 is not byte-exact on Linux — HiGHS objectives differ in the last bits (summation order) and exact-value ties (bench order of two equal-EV players, one perturbed-table re-solve) break differently; a Linux/aarch64 container run reproduced 17 such differences with the same transfers, gains and probabilities. Fix: the golden is keyed by platform, the CI job uploads the computed package as an artifact, and the Linux golden is committed from it (defect 47) |
| Live FPL-ID import | the watcher's retry at 10:33Z (entry endpoint back) still failed: the entry *history* endpoint was still 503 and the app showed the outage message (correct). Re-run on 2026-10-11 after full recovery: preview → confirm → import → Squad page → refresh → ownership assertions pass; the only defect was in the test harness (defect 48). Full production suite afterwards: 21 passed, 0 failed (1.6 min) |
| Auxiliary containers | `fpl-web-badkey` (127.0.0.1:3001, wrong proxy key) and `fpl-web-noapi` (127.0.0.1:3002, unreachable API host) confirmed as `docker run` test containers outside the Compose stack; removed after the suite; `docker compose -p fpl ps` all healthy, `/health` ok, images, volumes and data untouched |

47. The golden parity test compared one byte-exact file across platforms. Platform-keyed goldens
keep the comparison exact (no tolerance) where it runs; a platform without a golden skips with
an explicit message unless `FPL_GOLDEN_REQUIRED=1` (set in CI once its golden is committed).
48. The production suite's `asManager` init script re-seeded the active manager on every
navigation, so after onboarding switched to the imported manager the Squad page showed the
seed manager again; the seed is now applied only when no settings exist.

## 2d. M1.2 — Whole-squad Transfer Workbench (2026-10-11)

Scope (`docs/PRODUCT_GAP_ANALYSIS.md` §6, second milestone): evaluate concrete transfer plans for
the manager's entire squad against HOLD, with the existing forecasts, simulation, optimiser and
validator, as background jobs bound to the exact squad state.

| Area | Result |
|---|---|
| Engine extension | `Preferences.first_gw_exact` (the first gameweek's moves are exactly the forced ones) and `Preferences.first_gw_max_transfers` (a cap on transfers now) in the MILP; both default off and are added to `describe()` only when set, so every existing problem, plan and identifier is unchanged; `solve_with_alternatives` accepts a precomputed HOLD; the replacement shortlist (`shortlist_replacements`) and the per-plan summary (`summarise_option`) are shared, not duplicated. Tests: `tests/unit/optimizer/test_first_gw_preferences.py` (6) |
| Evaluation | `fpl_decision.workbench.evaluate_plans`: plan kinds `hold` (always), `transfer` (exact moves), `replace` (engine shortlist for one outgoing player, each as an exact single transfer), `best` (optimiser's best plan + distinct alternatives, optional transfer cap, optional no-hits); every plan solved on the same candidate pool, validated by the independent rules replay, scored on the same joint simulation samples as HOLD (paired gains net of hits), with `hit_cost_vs_hold` and `gross_gain_horizon` separated; illegal requests are listed under `excluded` with the rule they break (squad size, position quota, club limit, budget with *selling* prices, unknown player) and never solved around. Tests: `tests/integration/test_workbench_engine.py` (5, incl. "the workbench's HOLD is the engine's HOLD") |
| API and storage | `workbench_runs` (migration 0003, validated upgrade → downgrade → upgrade); `GET /workbench/context`, `POST /workbench/evaluate` (`202` queued / `200` served; one run per manager × state × snapshot × forecast × version × request, a failed run is re-queued), `GET /workbench/runs`, `GET /workbench/runs/{id}`; `workbench` job (lease 20 min) evaluates exactly the state the run names; `stale_state` / `stale_snapshot` flags; ownership by the router dependency plus `require_owner` on the run route and the job route; runs are exported and erased with the manager. Tests: `tests/integration/test_workbench_api.py` (5, incl. cross-user 403s on run, job, context, list and evaluate) |
| Web | `/transfers` is the Transfer Workbench: squad with bought / now / **selling** prices, xP next and over the horizon, start probability, status and weak spots from the Copilot Home analysis; choose a player to sell, engine shortlist or a specific replacement through the player picker (position and affordability pre-set), horizon up to the served maximum, the optimiser's best plan with "allow hits" and "max transfers now"; evaluation runs as a job with queued / running / succeeded / failed status, retry, and polling; plans table (action, moves, net vs hold, gross, hit cost, p10/p90, P(>hold), next-GW xP, bank/FT after, proven optimal vs best within limit, details); illegal plans marked, never recommended; details view with moves, squad after, captain, per-gameweek timeline vs hold and later moves; trace line (snapshot, forecast key, squad state, run id, optimiser config, ruleset). Events `workbench_evaluated`, `workbench_plan_selected` |
| Golden parity | intact where it is reproducible: `recommendation_package.darwin-arm64.json` is byte-exact on the development machine after every engine-side change of this milestone (fast tier 430 passed includes it). A Linux golden was tried and withdrawn: the CI artifact of `949ec7b` differed from the macOS file in 71 values (one simulation sample in 200; objectives ≤ 0.005) with an identical decision, but the next run (`b06d561`, same engine semantics, same locked wheels, same fixture) differed from *that* in 407 places including the forecasts themselves (chosen objective 65.26 → 64.80, captains, validity) — defect 52. CI therefore skips the comparison with an explicit message, prints the runner's CPU and numpy dispatch, and uploads the package it computed |
| Latency (development machine, Docker stack, `infra/scripts/workbench_latency.py`) | `GET /workbench/context` p50 / p95 20 / 21 ms; `POST /workbench/evaluate` answers in 20–24 ms (`202`); the default question (HOLD + 3 replacements + best + 1 alternative, 6 plans) is served **10.4 s** after submission at 3 GW (solver 5.5 s, of which optimise 2.6 s) and **64 s** at 5 GW on a worker that had just restarted (solver 14.7 s; the rest was the worker's cold start and the squad-analysis job queued ahead of it on the single worker); the same question again 17–19 ms; `GET /workbench/runs/{id}` p50 / p95 12 / 12 ms |
| Tests | fast tier 430 passed, 6 deselected (new: 19 above); migrations 3/3; dev browser flows 11 passed (workbench flow: engine shortlist → details → specific replacement as an exact transfer); production browser suite against the rebuilt images (migration 0003 applied by the `migrate` service): 21 passed, 0 failed (1.4 min), including the workbench evaluated as a worker job through the proxy, the stored run served again to its owner, and the live FPL-ID import; the two auxiliary web containers were recreated for the run and removed afterwards |

CI: `949ec7b` (closeout) success; `b06d561` (M1.2 commits `ae1c635`, `ad85c20`, `9fcfcb9`,
`9f1f7b5`, docs) failed in the test job on `test_golden_parity` only (every other job green);
`eacb083` success on all jobs (lint, security, test with the golden skipped on linux-x86_64 and
its package uploaded, web-e2e, optimizer-suite, containers); the runner that computed it was an
AMD EPYC 9V45 with numpy dispatching AVX512_ICL.

Decisions and defects: 49. The first version of the workbench evaluated a named transfer with
`forced_in` / `forced_out` alone, which lets the optimiser add further moves in the same
gameweek; `first_gw_exact` makes "your transfer" mean exactly that. 50. Chip alternatives and
the §69 stress scenarios are not in the comparison yet (backlog); the full recommendation
package still has them. 51. A single worker runs jobs serially, so an evaluation submitted
right after a squad change waits behind that squad's Copilot Home analysis. 52. Shared x86_64
GitHub runners are not numerically reproducible with each other for this engine: two CI runs
of the golden fixture on identical code, wheels and data produced forecasts, objectives,
captain choices and plan validity that differ far beyond rounding (see the golden-parity row);
the macOS development machine reproduces itself byte-exactly. A reproducible CI check needs a
fixed CPU class (self-hosted or pinned kernels: `OPENBLAS_CORETYPE`, numpy dispatch,
LightGBM build) — backlog. 53. Pre-existing optimiser edge case, exposed by the golden fixture
(a cheapest-players squad holding 5 players of one club): the MILP caps a club that already
exceeds the limit at the *initial* owned count for every horizon gameweek, while the rules
replay (ADR-0005) lets an inherited excess be kept or reduced but never increased again, so a
plan that sells club players now and buys one back later is rejected by the independent
validator (`GW3: [club_limit] 5 players from team 88 (max 4)`) and the engine recommends the
next-best valid plan — correct by design, but the optimiser's best plan is lost in that case.
Only squads with an inherited excess (mid-season club moves) can trigger it; the fix belongs to
a separate engine change with its own validation and golden regeneration — backlog.

## 2e. M1.3 — Snapshot-aware invalidation and "What changed?" (2026-10-11)

Scope: lifecycle correctness for the three results users read as current (recommendation,
Transfer Workbench run, Copilot Home analysis) — `docs/architecture/result-lifecycle.md`.

| Area | Result |
|---|---|
| Lifecycle semantics | each result's authoritative inputs (`manager_state.id`, `data_snapshot_id`, `prediction_run_id` / `forecast_key`) were already persisted and immutable; migration 0004 adds `invalidated_at` and `invalidation_json` to `recommendations` and `workbench_runs` (validated upgrade → downgrade → upgrade); recommendation statuses `active → superseded \| invalidated`, and back to `active` only by re-activation (the same question asked again when the stored answer's inputs are exactly the current ones); `fpl_api.lifecycle` is the single implementation (events, verdicts, explanations) |
| Reference and direction | every verdict is judged against the **promoted** snapshot (newest with a ready serving forecast, read from disk the same way in the API and the worker — `DataService.promoted_snapshot_id`); snapshots are ordered by export time and only a result on an *older* snapshot is stale, so a recommendation the worker computes on a newer, not yet promoted export stays current; the API follows a promotion on its next verdict, regeneration or evaluation |
| Invalidation | snapshot promotion (`_job_forecast` → `mark_serving_ready` → `invalidate_for_snapshot`, re-run on every `forecast_precompute` tick) invalidates every current result on an older snapshot in one idempotent transaction, recording old/new snapshot ids and the detection time; every read (including each journal row) derives the same verdict and records a missed one with a conditional UPDATE (a failed write never changes the verdict); a newer squad state is a derived reason (`squad_changed`, with the stored-state diff), never persisted for runs, because a re-saved earlier squad is legitimately latest again; finalisation checks inside `RecommendationStore.save` and `WorkbenchStore.finish` store a result computed against obsolete inputs as historical from birth and never supersede the current one |
| Ordering and jobs | competing recommendation jobs are ordered by request time under a per-manager-and-gameweek advisory lock (an older request finishing last is stored `superseded`; never two current ones); a workbench job computes on exactly the snapshot and forecast its run names (`AppServices.at_snapshot`), never reopens or overwrites a finished run (conditional `start` / `finish`), and a failed enqueue fails the run and its job at once (a run left queued without a job is re-queued after 2 minutes) |
| What changed | `GET /recommendations/{id}/changes`, `GET /workbench/runs/{id}/changes` (owner only): stored-state diff; snapshot diff against the promoted snapshot (read from disk even when the process still has an older one loaded) with the old snapshot read at the result's own cutoff and season (availability, chance of playing, news, price for the players the result used, fixture counts, changed tables, both export times — retention keeps snapshots referenced by workbench runs); forecast run ids and model versions; `available: false` with the reason when the older snapshot is gone, unreadable or holds no data for the result's season |
| UI | Home: current / historical badge with reasons, "What changed?" panel, regenerate; Transfers: lifecycle badge (current, historical, computing, failed), what-changed panel, the latest run shown after a reload with its verdict, a pending-evaluation guard so neither the initial load nor a late poll can put another run on the page, Retry re-submits the failed run's own question; Future Planner: the verdict, a historical plan labelled so and without "play now" advice; Journal: badges from the same derived verdict; the "What changed?" panel resets per result and never reads "no input difference" for a historical result |
| Adversarial audit of the M1.3 diff | a five-way review against the ten lifecycle invariants, each finding checked by an independent skeptic: 23 findings confirmed, 0 refuted, all fixed in this milestone with regression tests — lazy invalidation persisting in the wrong direction (a recommendation on a newer, unpromoted snapshot was permanently invalidated when read); the worker judging results against the newest *export* (fresh workbench runs born historical); workbench runs computed on the worker's snapshot while naming the API's; re-delivered jobs reopening finished runs; non-atomic lazy writes; no ordering between competing recommendation jobs; squad-only finalisation staleness persisted and unrecoverable after a squad was saved back; a promotion sweep that could not be completed after a failure; "what changed" against the API's lagging snapshot and the served cutoff; Future Planner and Journal presenting historical recommendations as current; a run left queued without a job locking the page; Retry submitting the form instead of the failed question; a stale comparison panel; and **ownership**: `authorize` checked only the query `manager_key`, so a signed-in user could act on another manager through the JSON body (pre-existing since M1.1a; fixed — every key named in path, query and body must be owned; `test_identity.py::test_a_key_in_the_query_never_vouches_for_another_key_in_the_body` fails against the previous check and passes now) |
| Second review (of the fixes) | four reviewers over the fixed code, each finding checked by a skeptic: 9 confirmed (6 distinct defects), 1 refuted, all fixed with regression tests — a **pinned** deployment (the documented `FPL_SNAPSHOT_DIR` data rollback) kept results on the newer, rolled-back snapshot current (now every other snapshot is stale there); a re-ask de-duplicated onto a waiting job did not count as a later request; in inline mode the route re-activated a fresh answer stored superseded (now only de-duplicated answers); a job running for a run the reaper had failed was recorded as succeeded and a retry was de-duplicated onto it, leaving the run queued forever (the job now fails, every queueing gets its own job, and a run whose job ended unfinished is failed on read); a run computed on other inputs than it names was shown as a snapshot change and cleared by asking again (now its own reason, recomputed instead); Home labelled the new defect-53 optimality "time limit". The refuted one (the lost-enqueue window measured from the original creation time) needs a millisecond window and a duplicate submit that de-duplicates harmlessly |
| Tests | all on the final source of this milestone (re-run in full after the `DataService.refresh` fix found by CI, see below), development machine: Python fast tier **453 passed**, 0 failed, 6 deselected (the slow and network tiers); slow + network tiers **6 passed** (official points on all historical rows; rules and per-player histories vs the live FPL API); lifecycle module `test_lifecycle.py` **18 passed** (a real promotion on a snapshots directory with ready markers, a worker computing on an unpromoted export, an API that has not swapped yet, a pinned deployment, finalisation races, request ordering incl. a re-ask while the job waits, re-delivered, given-up and lost jobs, runs computed on other inputs, re-activation, the scheduler's sweep, honest explanations, ownership); identity / workbench / onboarding suites 15 passed, including `test_a_key_in_the_query_never_vouches_for_another_key_in_the_body` (query vs body, two query keys, path vs query; a mutation run with the previous single-key check makes it fail); `test_inherited_club_excess.py` 3 and the brute-force optimiser suite 29 passed; golden parity passes against the regenerated macOS golden; `test_engine_determinism.py` 1 passed; migrations validated upgrade → downgrade → upgrade; ruff, ruff format, mypy (116 source files), import-linter 4/4, `tsc`, `next build`: clean. Browser flows: development stack **13 passed** (new in M1.3: squad change → historical run after a reload with the state diff → current again; a *real* snapshot promotion — the staged snapshot is copied into the snapshots root with a ready marker, the recommendation turns historical with the snapshot reason, the Future Planner labels it historical, "What changed?" shows the changed table and player, regeneration gives a current plan, the journal shows exactly one current recommendation); one development run before the last web fix failed intermittently in that flow — the page briefly showed "No recommendation" + Generate before its request started (`useApi` began in a not-loading state); a rerun before the fix passed 13/13 (the race is timing-dependent); the fix makes it start loading, and the suite then passed 13/13 on the final web build. Production topology on the rebuilt images through the web proxy: **22 passed**, 0 failed, 0 skipped (with `E2E_FPL_ENTRY_ID=1`: the live FPL-ID import ran against the real API; the lifecycle flow — a saved squad makes the last workbench run and the recommendation historical with what changed, and both are current again once the squad is restored — now runs before the export/delete flow, with an unconditional recommendation check). An earlier production run (09:5x UTC) had stopped at the live import (the external FPL API timed out, 503 after 17.9 s) and, in serial mode, skipped the six flows after it; that was an environmental failure, not counted as a pass |
| Defect 52 (reproducibility) | still **open**, characterised. On one machine the engine reproduces itself byte-exactly (two independent trainings and recommendations: `test_engine_determinism.py`, enforced in CI). Across machines it does not: macOS/arm64 vs CI x86_64 differed in 71 values with an identical decision, and two CI x86_64 runs on identical code and data differed in 407 values including the forecasts (chosen objective 65.26 vs 64.80) and plan validity — so the difference is environmental (CPU microarchitecture: numpy/OpenBLAS kernel dispatch and LightGBM histogram arithmetic; the `eacb083` runner was an AMD EPYC 9V45 with AVX512_ICL dispatch, now printed in every run) and numerical, not an engine inconsistency: solver threads are 1, `mip_rel_gap` 0, LightGBM is `deterministic` / `force_row_wise` with fixed `n_jobs`, the Monte Carlo uses seeded uniform draws. The byte-exact golden is enforced on the development platform only; CI skips that comparison with an explicit message and uploads its package and a `platform_fingerprint.json` for correlation. CI therefore gives **no cross-platform regression coverage of engine outputs**; that needs a fixed CPU class (self-hosted runner or pinned kernels: `OPENBLAS_CORETYPE`, `NPY_DISABLE_CPU_FEATURES`, a LightGBM build) — not available here and not claimed |
| Defect 53 (inherited club excess) | still **open** as an optimiser formulation limitation; its user-facing consequence is fixed. The regression (`tests/unit/optimizer/test_inherited_club_excess.py`, 3 tests) builds a squad with four club-1 players where the profitable two-step plan sells one now and buys another club-1 player next gameweek; the MILP returns it, the independent rules replay rejects it (`club_limit`), and the engine's gate can never pass it however large its simulated gain. The audit found that the alternatives found afterwards were labelled "proven optimal" although the no-good cut that removed the illegal plan also removed the legal plans sharing its first move (in the test, "A → B now, keep B" was never explored). They are now labelled "best valid plan among those explored; not proven optimal (…)" and `proven_optimal` is false. The golden fixture hits this case (5 players of club 88; `best` and `alt-1` fail `club_limit` in GW3, the chosen `alt-2` was served as "proven optimal"): the macOS golden was regenerated and differs **only** in those four optimality strings — plans, objectives, decision and identifiers are unchanged. The formulation fix (a per-gameweek allowance that can only fall) stays isolated backlog work with its own validation |
| Rollout | `docker compose build api web` from the final source, then the `migrate` service (`alembic upgrade head`: no-op, `0004 (head)` already applied — `alembic current` reports `0004 (head)`), `up -d --no-deps api worker scheduler web`, rebuilt and rolled again after the last two fixes (web loading state; `DataService.refresh` below): api / worker / scheduler run engine image `cd4309e63316`, web runs `d19021851972`; all seven services healthy (postgres, redis, api, worker, scheduler, web, prometheus); database volume and data preserved (no volume or container of the stateful services recreated); no error, traceback or schema mismatch in the api / worker / scheduler / web logs after the rollout; the two auxiliary web containers used by the production suite (wrong key, unreachable API) were removed afterwards |
| Repository | renamed on GitHub from `adivishall/FPL` to `adivishall/fpl-copilot` (GitHub does not allow spaces); `origin`, the README badge and the FPL API user-agent contact URL point to the new name, the old URLs redirect |

CI: `2c593df` (the six M1.3 commits) — [run 38153704355](https://github.com/adivishall/fpl-copilot/actions/runs/38153704355)
**failed** in the test job (lint, security, optimiser suite and web e2e passed): the lagging-API
lifecycle test exposed that `DataService.refresh()` reset its last-check time to 0, which on a
runner booted less than `FPL_SNAPSHOT_CHECK_SECONDS` ago still looks recent, so the API did not
follow a promotion; two later tests failed in cascade. Fixed in `d415e89` (the test now simulates
a clock 120 s after boot and fails against the old code); the full local verification above was
re-run on the fixed source. `5081f10` (the fix, refreshed screenshots, README, collaboration
files) — [run 38157203712](https://github.com/adivishall/fpl-copilot/actions/runs/38157203712)
**success** on every job: lint, security, test (446 passed, 7 skipped — the byte-exact golden on
linux-x86_64 by design, defect 52, and six worker tests that need a local `redis-server` binary
the runner does not have; they pass on the development machine), optimiser suite (54 passed),
web e2e (13 passed) and container builds. The documentation commit recording this is checked
by its own run (reported with the milestone, not written into itself).

## 2f. M1.4 — Decision journal and prospective outcome evaluation (2026-10-12)

Scope (`docs/PRODUCT_GAP_ANALYSIS.md` C10, core): record what the manager decided against what
Copilot recommended, keep the evidence it was decided on, and evaluate it against the official
results without hindsight — `docs/architecture/decision-journal.md`.

| Area | Result |
|---|---|
| Workflow | a decision is recorded from Home (against the recommendation) or the Transfer Workbench (against any plan that passed the rules replay): follow, hold, another plan, a different captain / vice-captain, "I'll decide later", "I did something else", with an optional reason; recording against a historical result is allowed and warned about. Actions `HOLD`, `TRANSFER`, `HIT`, `CHIP`, `DEFER`, `UNKNOWN`; adherence (followed / modified / rejected / deferred) is **derived** from the recommended and chosen plans, never inferred from later events; the manager's own confirmation and the first-party verification are separate fields. A change of mind before the deadline supersedes (both kept); after the deadline a pre-deadline decision cannot be replaced (409); the identical submission is the same content-addressed entry |
| Immutable evidence | copied at record time (sources are invalidated, erased or pruned later; forecasts within a day): identifiers (source, state, snapshot, prediction run, forecast key, ruleset, model versions, cutoff, deadline), the squad, decision-time clubs and fixtures, the recommended / HOLD / chosen plans with lineups, armbands, hits, expected points (mean, p10, p90) and paired gain vs HOLD, the system's rationale and per-player expectations. HOLD's XI for a recommendation is rebuilt from decision-time expectations and labelled (with whether the stored armband was kept); the evidence is never rewritten (tested across promotions, a correction and a revision) |
| Evaluation | pure evaluator in an idempotent `journal` job (hourly sweep + "evaluate now"): pending → partial (no totals while a relevant fixture is missing; provisional totals when all are recorded but not final) → final only when FPL finalised the gameweek, every relevant fixture is final and the result rows were loaded after the finalisation lag; `unavailable` with the reason for a deferred or unreconstructible action. Scoring reuses `fpl_domain.squad.gameweek_points` (auto-substitution, vice-captain, chips) minus hits, for the recorded action, HOLD and the recommended plan; statements compare the result with the decision-time range, expected difference and P(beats HOLD) and carry a caveat that one gameweek is a single draw — no decision is graded |
| History | `decision_evaluations` is insert-only and numbered per decision; unchanged inputs write nothing; revised official data (also a withdrawn correction) adds a revision pointing at the previous final evaluation; a process on older data never replaces an evaluation made on newer data |
| Verification | after the deadline, for FPL-ID squads, the official picks are mapped to player codes and compared (squad, captain, vice-captain, chip): matches / differs / observed / unavailable (retryable failures retried for seven days); the record is never changed by it |
| Ownership, privacy, retention | every journal route checks the owner of every manager key in path, query and body and of the entry; recording requires the source to belong to the same manager (404 otherwise); `/journal*` is a privileged GET prefix; evaluation jobs carry the manager key; export and erasure (manager and account) include both tables; retention keeps the snapshots journal decisions reference |
| Migration 0005 | adds the journal columns, backfills pre-M1.4 feedback from its recommendation (`jd_legacy_…`, adherence vocabulary mapped, evaluation and verification `unavailable`), keeps one active decision per manager and gameweek, creates `decision_evaluations`; validated upgrade → downgrade → upgrade on a database with legacy rows (`test_0005_…`); rollback drops the new table and columns and deletes decisions recorded from workbench runs (documented in `docs/DEPLOYMENT.md`) |
| Adversarial review of the M1.4 diff | three reviewers (evaluation, records, surface), each finding checked by a skeptic: 23 confirmed (20 distinct defects), 1 refuted, all fixed in this milestone with regression tests — **final declared on result rows loaded before they were final** (bonus not yet confirmed; now the rows' own finality is required); "something else" left the hourly sweep before its reference totals existed; a rebuilt HOLD silently replaced the stored armband; "zero by construction" stated after an armband change; the outcome digest covered players of unchosen plans (spurious revisions); the compatibility feedback route dropped self-reported points on a repeat; a withdrawn official correction was silently not recorded; `source_plan` overflowed on long plan labels (500); migrated decisions could be replaced with hindsight after the deadline; evidence used a squad's stale clubs and the run's named instead of used snapshot / forecast; a plan that holds this week was taken as the workbench's recommended move (adherence inverted); an illegal workbench plan could be recorded through the API; **the entry page crashed on every migrated pre-M1.4 decision**; the record form kept a previous plan's armband and message; the armband selectors applied to "I'm holding"; replaced decisions showed "awaiting results" forever; the recommendations table fell back to a replaced decision; "changed before the deadline" was shown for changes after it; the scheduler re-submitted settled managers every hour. The refuted finding (retention does not keep the snapshots evaluations were computed on) is a documented choice, not a defect: an evaluation stores the per-player minutes, points and fixture states it used, the entry page renders from them, and the snapshot id is provenance only |
| Second review (of the fixes) | an independent reviewer re-checked every fix against the code: 17 closed outright; 3 problems confirmed and fixed with regression tests — a replaced decision that had been checked once still showed "awaiting results" (now shown as replaced, with its last check only when it had settled); the guard that keeps older data from replacing newer evaluations used the lifecycle's pinned-deployment rule and froze the journal during a data rollback (now export time only); a workbench plan that holds this week with its own lineup was described as "zero by construction" (now only HOLD's own XI and armband). It also found a narrow timing gap — no results load between the finalisation lag and the end of the correction window leaves a GW38 decision partial — documented in `docs/KNOWN_LIMITATIONS.md` §4a, not changed |
| Tests | all on the final source of this milestone, development machine: Python fast tier **488 passed**, 0 failed, 6 deselected (the slow and network tiers), golden parity included (the engine is untouched: no change under `packages/` except the storage models); slow + network tiers **6 passed**; journal evaluator unit tests **22 passed** (official scoring with auto-substitution, vice-captain and Triple Captain; pending / partial / provisional / final incl. rows captured before they were final; moved fixtures; deferred and "something else"; digests; wording without grading; verification; adherence; the review regressions); journal integration **12 passed** on a real database (decision-time evidence across promotions, change of mind and the deadline rule incl. migrated decisions, workbench plans, first-party picks matching then differing, partial → final → revision → withdrawn correction, the older-data guard, confirmation, ownership on path / query / body, export and erasure, long plan labels, the compatibility route); migrations **4 passed** (0005 on a database with pre-M1.4 feedback, upgrade → downgrade → upgrade); ruff, ruff format, mypy (117 source files), import-linter 4/4, `tsc`, `next build`: clean. Browser flows: development stack **14 passed** (new: record on Home → entry pending → listed before and after a reload → a migrated pre-M1.4 decision opens without evidence and without an evaluate button → a results snapshot is promoted → evaluate → final with scores, statements and the caveat, the source now historical, the evidence unchanged); production topology on the rebuilt images through the web proxy **23 passed**, 0 failed, 0 skipped (new: record on Home → entry → listed → the proxy key alone gets 401 → journal row; with `E2E_FPL_ENTRY_ID=1` the live FPL-ID import ran against the real API) |
| Rollout | `docker compose build api web` from the final source; a `pg_dump` of the stack's database first (17.7 MB, 45 tables with data); the `migrate` service took the schema from `0004` to `0005 (head)` (`alembic current`); this stack had no pre-M1.4 feedback rows, so the backfill was a no-op here (it is exercised by the migration test); `up -d --no-deps api worker scheduler web`: api / worker / scheduler run engine image `3d9fd3ec6c81`, web runs `50ece8d02b80`; all seven services healthy; database volume and data preserved; no error, traceback or schema mismatch in the api / worker / scheduler / web logs; the scheduler's hourly `journal` task ran against the migrated database (no due managers: the production suite's erasure flow removes its test manager's decisions); the two auxiliary web containers used by the production suite were removed afterwards |
| Time-attestation review | before arming the GW7 capture (2026-10-14): the host's clocks (Colima VM, database) and the absence of late FPL news no longer decide when a forecast existed. FPL's API returns a usable `Date` (Fastly sets it at delivery; equal to the NTP-synced host clock within a second while `Age` was 276 s), so the forecast job reads it before computing and after hashing the file (`not_before = Date`, `not_after = Date + Age + 1 s`; migration 0008 `forecast_attestations`); without such a time before the cutoff a checkpoint is `prospective_unverified`, never scored; `news_added` is a leakage check only. An adversarial reviewer found no path from a post-cutoff forecast to `prospective` while `Date` behaves as observed; its findings were fixed (contradictory or stale readings stored nowhere, the forecast's own cutoff on the promotion, retention of the next gameweek's proven forecast, a repeated `Age`, raw headers, the attestation job's lease) or deferred with a limit (contract §15) — `8a93e04`, `750c9e2` |
| Attestation rollout | `pg_dump` 18.9 MB, 49 tables with data; `0007 → 0008` with every row kept (7 promotions, 0 checkpoints); api / worker / scheduler run engine image `fda95d0afdb6` with `FPL_BUILD_REVISION=750c9e2…` (clean tree), web `545c7d2abc97`, all `restart: unless-stopped` and healthy. Within two minutes the record had FPL server time for the served GW7 forecast: `snap_eba141af451a53f7e0c7` / `fc_4b6d603e21492f9b` — a `generation` attestation, computed after 08:10:28 and existing by 08:14:32 UTC on 2026-10-14 by FPL's clock (`Date` 08:11:52, `Age` 159 s, host clock 0.9 s ahead), 4,335 minutes before the GW7 cutoff — and an `existence` attestation of the previous promotion by the scheduler's `forecast_attest` job. The six promotions made before 0008 have none and cannot be GW7's checkpoint unless nothing newer is proven |
| GW7 rehearsal with FPL time | on a copy of the stack's database (dropped afterwards; the live record untouched: 0 checkpoints, 0 evaluations): capture at cutoff + 5 min → `prospective`, 3,335 rows in 4.6 s, all six deciding checks passing, `attested_at` 2026-10-14 08:14:32 UTC, re-capture idempotent, the evaluation and the timeline reading it; the same capture with the copy's attestations removed → `prospective_unverified` with only `fpl_server_time_before_cutoff` failing (the host clock's "before" and the absence of late news were not enough) |
| Defects 52 and 53 | unchanged and still **open**: the engine reproduces itself byte-exactly on one machine only (CI skips the byte-exact golden on Linux), and the MILP's inherited club excess is labelled, not fixed. The journal records what each recommendation and plan said, including its optimality label |

CI: `ac3a9e6` (the three M1.4 commits `7e433ce`, `88931c0`, `ac3a9e6`) —
[run 38170482994](https://github.com/adivishall/fpl-copilot/actions/runs/38170482994)
**success** on every job: lint, security, test (481 passed, 7 skipped — the byte-exact golden on
linux-x86_64 by design, defect 52, and six worker tests that need a local `redis-server` binary
the runner does not have; they pass on the development machine), optimiser suite (54 passed),
web e2e (14 passed, including the journal flow and the migrated pre-M1.4 decision) and container
builds. The documentation commit recording this is checked by its own run (reported with the
milestone, not written into itself).

## 2g. M1.5 — Season Review: the forecast record and live-season evaluation (2026-10-14)

Scope: evaluate the live season honestly — forecasts frozen before kickoff, scored only on final
results, by a method fixed in advance — and show it on a Season Review page beside the past
seasons and, privately, the manager's own recorded decisions
(`docs/architecture/season-evaluation.md`).

| Area | Result |
|---|---|
| Evaluation contract | committed `f4df364` on 2026-10-14, before the first live checkpoint (GW7 cutoff 2026-10-17 08:30 UTC) and before any live outcome; amended the same day by the implementation review (`dde3853`), still before the cutoff and before any outcome. Four critics reviewed the draft (statistics, leakage, feasibility, product honesty and privacy), each finding checked by a separate verifier (three verifiers did not complete; those findings were assessed against the code): every finding is recorded in §15 as accepted, rejected with a reason, or deferred — e.g. percentile bootstrap replaced by t-intervals and an exact sign-flip test with verdicts only at looks G = 8, 16 and season end; a database promotion record instead of marker times; failed live checkpoints kept out of every class; `ep_next` rejected as a baseline (not ingested point-in-time); the GW1–6 replay and a live shadow benchmark deferred |
| Forecast record | the forecast job commits to a serving forecast before marking it ready (`forecast_promotions`: file SHA-256, prediction run, export time, database clock; keyed by snapshot and forecast key); after each decision cutoff `forecast_capture` freezes the forecast the API served into `forecast_checkpoints`, `predictions` (now keyed by checkpoint, RESTRICT) and two content-addressed blobs (decision-time context and baselines fitted on the forecast's own snapshot with the `forecast_eval` code; exact Monte Carlo and climatology distributions as per-value counts). FPL's server time (`forecast_attestations`: the HTTP `Date` + `Age` of a request to the official API sent after the file was written and hashed, and one before computing) decides when a forecast existed — never the host's clocks. Prospective only when that time precedes the cutoff, the file is byte-identical to the promoted one, the identity and cutoff match, and the leakage checks pass (no FPL news stamped after the cutoff, no result of the gameweek); `prospective_unverified` when only the server time is missing or late; otherwise `live_unverified` or `missed`, never recomputed. The scheduler's `forecast_attest` job attests the newest promotion when the forecast job could not reach FPL and fails visibly while FPL is unreachable. Retention keeps every uncaptured candidate and the newest promotion. Migrations 0006–0008 |
| The record on the local stack | started 2026-10-14 03:26:13 UTC (first promotion: GW7 forecast `fc_9fd3f1b718ff5e7e`, file hash in the database equal to the file's); GW1–6 of 2026-27 predate it and are shown as "before the record". GW7 deadline verified from the latest FPL bootstrap payload (retrieved 2026-10-14 02:31 UTC, `deadline_source` official): 2026-10-17 10:00 UTC, cutoff 08:30 UTC, first kickoff 11:30 UTC. A rehearsal on a copy of the stack's database (dropped afterwards; the live record untouched) captured GW7 at cutoff + 5 min as `prospective` with every check passing: 3,335 player × gameweek rows (667 players at horizon 0, 224 regulars, 105 with status `u`), no missing baseline, 4.9 s, ~0.9 GB peak, a repeat returning the same checkpoint |
| Evaluation | `fpl_forecasting.season_eval` (fair CRPS and expected PIT coverage from distribution counts, per-gameweek sums, t-intervals, exact sign-flip with Holm, TOST equivalence against margins fixed from 2023-26 per-gameweek variability, ECE with its null band) and `fpl_api.season` (eligibility under the shared finality rule; exclusions counted, never read as 0; insert-only revisions with trigger and changes; advisory lock; no writes from older data). The same per-gameweek method applied to the past seasons' GW7–14 gives, in each of 2023-24, 2024-25 and 2025-26, "lower error than the baseline" on all four primary comparisons (Holm-adjusted sign-flip p = 0.031, the smallest attainable at G = 8) and "within tolerance" on all three calibration checks — the first live look can detect a margin of the past seasons' size, and nothing is claimed before it |
| Season Review | `GET /season-review` (public, no manager data, stored rows only), `GET /season-review/evaluations`, `POST /season-review/evaluate` (operator), `GET /journal/summary` (owner only); the `/season-review` page with the status strip, the live record and its timeline, the past seasons over the same weeks, the private decisions section, the calibration state ("No calibration adjustment has been made") and how every number shown is evaluated; Data Health labels its gates "passed on past seasons 2023–26" |
| Implementation review | three reviewers (forecast record; evaluation statistics; product honesty and privacy), each re-checked against the code and, where practical, by reproduction: every confirmed finding fixed with a regression test (`7457989`, `fe6fcf3`) — among them a re-keyed snapshot dropping its newer promotion, settled looks (a late-final gameweek could re-form a look), the "as served" sensitivity pooling horizons and missing postponement blanks, exclusions pooled across horizons, segment verdicts, unused minimum sizes, a Monte Carlo p of 0, a stale guard that failed open, an over-broad digest, armband-changed decisions compared with the plan's expectation, an unpublished past-season split, and page issues (stale verdicts between looks, a chart whose marks always touched its edges, local times, an unknown season returning 500). Recorded in the contract's §15 |
| Tests | with the time attestation (`750c9e2`), development machine: Python fast tier **549 passed**, 0 failed, 6 deselected (new: FPL server time unit 13, prospective checks 6, forecast record integration 15 including an unattested capture in a second database, server-time selection, contradictory and stale readings, the attestation job failing while FPL is unreachable and succeeding after, and retention of the next gameweek's proven forecast; migration 0008 round trip). Earlier, on `24277c4`: Python fast tier **529 passed**, 0 failed, 6 deselected (slow and network); slow + network **6 passed**; new: season evaluation unit 13, forecast-record checks 5, finality 3, forecast record integration 10 (commitment before the marker, due only after the cutoff, before-the-record, retention keeps the candidate whatever its age, the frozen rows equal the forecast's numbers and samples, concurrent and repeated capture, survival after deleting the cache and the feature store and in a fresh process, missed gameweeks never recomputed, RESTRICT, re-keyed promotions), Season Review integration 8 (nothing scored before final results, in-progress and provisional windows, exclusions by reason incl. a postponement and a missing result, hand-computed C1 and CITL, horizon-0 "as served", a correction revision and no revision for an uncovered player, the stale guard, the public endpoint doing no work, operator-only evaluation, the scheduler's due-check), journal summary (route order, ownership, self-reported points never used, the published split), migrations 0006 (guard, RESTRICT, downgrade); ruff, ruff format, mypy (124 source files), import-linter 4/4, `tsc`, `next build`: clean. Browser flows: development stack **15 passed** (new: the Season Review keeps the classes apart and claims nothing early); production topology on the final images **24 passed**, 0 failed, 0 skipped (new: the public endpoint carries no manager data, the proxy key and a user session cannot evaluate, the private summary needs the owner) |
| Rollout | backend first (`9e1cab1`: a `pg_dump` of 18.7 MB, 46 tables with data; `0005 → 0006`), then the final images (`24277c4`: dump 18.8 MB, 49 tables; `0006 → 0007`): api / worker / scheduler run engine image `50fed46eb04c` with `FPL_BUILD_REVISION=24277c4…` (clean tree), web runs `738d96a80c57`; all services healthy; data preserved; the scheduler records a promotion with each hourly forecast, its ledger task finds nothing due before the cutoff, and `season_eval` waits for the first checkpoint |
| Defects 52 and 53 | unchanged and still **open**: the engine reproduces itself byte-exactly on one machine only (cross-machine engine-output regression coverage is missing; the frozen forecasts are stored rather than recomputed for this reason), and the MILP's inherited club excess is labelled, not fixed (illegal plans are rejected independently; fallback plans are labelled "best valid plan among those explored; not proven optimal"). M1.5 touches neither the engine nor the optimiser |

CI: `24277c4` (the M1.5 source: `c9068cf`, `f4df364`, `07260e0`, `9e1cab1`, `950982f`, `c35622e`,
`7457989`, `fe6fcf3`, `dde3853`, `24277c4`) —
[run 38510488940](https://github.com/adivishall/fpl-copilot/actions/runs/38510488940)
**success** on every job: lint, security, test (522 passed, 7 skipped — the byte-exact golden on
linux-x86_64 by design, defect 52, and six worker tests that need a local `redis-server` binary the
runner does not have; they pass on the development machine — 6 deselected), optimiser suite
(54 + 7 passed), web e2e (15 passed, including the Season Review flow) and container builds. The
backend commit `9e1cab1` had its own green run
([38506021089](https://github.com/adivishall/fpl-copilot/actions/runs/38506021089)); the run of
`dde3853` was cancelled when `24277c4` superseded it. The documentation commit recording this is
checked by its own run (reported with the milestone, not written into itself). The time
attestation `8a93e04` passed CI on every job
([run 38527383123](https://github.com/adivishall/fpl-copilot/actions/runs/38527383123)); its review
fixes `750c9e2` and this record are checked by their own runs. The GW7 checkpoint is captured
unattended by the stack's scheduler after the cutoff on 2026-10-17 and recorded here once
verified.

### M1.5 stage status (live GW7 capture verified against the running stack, 2026-10-17 12:37 UTC)

One stage succeeding says nothing about the others; each is recorded on its own.

| Stage | Status | Evidence |
|---|---|---|
| Rehearsal | done — **not live evidence** | a copy of the stack's database, 2026-10-14 (above): `prospective` with the attestations, `prospective_unverified` without them |
| Forecast generation (live) | **ran** | the forecast frozen for GW7, `fc_ca59948a7a8b1486` on `snap_a4e41581660ffe2024a1`, was exported 2026-10-17 07:00:56 UTC and promoted 07:02:06 UTC (host clocks, `build_revision` `750c9e2`, 3,335 rows, 1,000 simulations); 29 promotions preceded the cutoff by the host clock |
| Time attestation (live) | **ran** | the frozen forecast's `generation` attestation: FPL answered `Date: Sat, 17 Oct 2026 07:02:06 GMT`, `Age: 114`, so the file existed between 07:01:04 and 07:04:01 UTC by FPL's clock (host skew 0.7 s), hash equal to the checkpoint's; `forecast_attest` jobs since 0008: 2 succeeded, 1 failed (12:29 UTC, FPL unreachable — recorded as a failed job and re-submitted by the next tick, which succeeded at 12:32 UTC) |
| Prospective checkpoint (GW7) | **done — `prospective`** | `ckp_757799b78ea4e3cf`, `reasons []`, `attested_at` 07:04:01 UTC (86 min before the 08:30 UTC cutoff); every check holds: FPL server time before the cutoff, file unchanged since promotion, identity matches the promotion, cutoff equals the gameweek's, no FPL news stamped after the cutoff (latest 2026-10-16 17:30 UTC), no results of the gameweek; informational: the capture-time FPL read failed ("FPL not reached"), which is not a criterion. Captured at 09:43:53 UTC by `job_c1c32f6548a4425d`, submitted by the first `forecast_ledger` tick after the cutoff (09:32 UTC: the host slept from 07:33 to 09:32 UTC, as the readiness note anticipated — the capture time is not a criterion, and the frozen forecast is the newest one promoted before the cutoff). 3,335 `predictions` rows, 2 blobs, row digest `62267b9b…` identical before and after a restart of api, worker, scheduler and web, after a retention run and after `forecast_ledger` run-once ("skipped", nothing due); re-capture returns the same checkpoint id every time (counts 1 / 3,335 / 2 unchanged). Season Review: GW7 `prospective` with its attestation time; GW1–6 `before_record`; GW8+ `upcoming` |
| Scoring | **not run** | `season_evaluations` seq 1 (trigger `first`, 2026-10-17 09:46 UTC) with every window `not_started`; GW7 results expected final around 2026-10-22 19:00 UTC; calibration `deferred` until 8 live gameweeks are final |
| Public deployment | **not done** | no public URL exists (§2i: the host, domain and name are still owner inputs) |

Readiness check for the GW7 capture (read-only, 2026-10-15 14:11 UTC): the official deadline
re-read from FPL (`is_next`, 2026-10-17 10:00 UTC) agrees with the `gameweeks` row
(`deadline_source` official); the ruleset's 90-minute buffer gives the cutoff 08:30 UTC; every
service is healthy; `checkpoint_candidate` for GW7 at the cutoff selects the newest attested
forecast, whose file (4.5 MB, hash equal to the promotion's) and snapshot are stored and which
`pending_candidates` keeps; no promotion lacks FPL's time; FPL's `Date` equalled the host clock
(`Age` 215 s). Operational limit observed: the host sleeps often — 37 scheduler ticks in 30 hours
instead of about 120, with gaps of 2–5 hours (macOS power log: repeated clamshell and idle
sleeps) — and the Colima VM pauses with it, so the scheduler runs only while the host is awake. A
host asleep at the cutoff captures at its next wake (capture time is not a criterion) and freezes
the last forecast promoted before 08:30 UTC, which can be hours old; for the freshest forecast the
host must be awake, on power, with Colima and the stack healthy from about 07:30 UTC (13:00 IST)
on 2026-10-17. Colima is not a login service (no decision recorded; `brew services start colima`
is the operator's option). The post-capture verification (`verify_gw7.sh`: counts, attestation
bounds, provenance, idempotent recapture, retention and ledger run-once, restart) is armed as a
background waiter in the development session only: it depends on that session and the host
staying available, and is otherwise run by hand after the capture.

Outcome (2026-10-17): the session waiter did not survive the session, as documented; the
capture itself needed nothing from it — the scheduler's `forecast_ledger` tick submitted the
capture at the first wake after the cutoff and the verification was run by hand at 12:37 UTC
(`verify_gw7.sh`: the table above). One other job failed in the window: a `forecast` job
abandoned at 11:21 UTC ("queue reports failed", the host asleep while it ran), which concerns a
later snapshot and not the frozen forecast; the next forecast was promoted and attested
(`existence`, 12:32 UTC) as designed.

## 2h. M1.6 — FPL Copilot design system and redesigned pages (2026-10-15)

Scope: the visual language of the official FPL site — deep purple, vivid green, light surfaces,
player and statistics tables, a clear gameweek dashboard — on the real Next.js application, with
the product identity FPL Copilot (no official affiliation, no protected assets), and without
changing forecasting, optimisation, scoring, journal semantics, snapshot invalidation, ownership
or the API. Started only after the GW7 capture readiness was re-verified and recorded (§2g); the
GW7 capture itself is still in the future at this writing.

| Area | Result |
|---|---|
| Audit | the live app (11 pages, 7 components, plain CSS with tokens, dark theme), both browser suites and the committed screenshots were inspected first; a throw-away beta account with an optimiser-built squad, a worker recommendation and a workbench run was used on a `next dev` server (port 3100) against the live API, and erased afterwards |
| Design system | `apps/web/app/globals.css`: tokens (`--brand #37003c`, `--green #00ff87`, surfaces `#f3f2f7` / `#fff` / `#faf8fc`, status pairs that pass AA), typography, spacing, radii, shadows, visible focus, reduced motion; status badges always carry a word; a 720 px breakpoint; wide tables in `.table-wrap` scroll sideways on a phone |
| Shell | `components/ui.tsx`: purple brand bar with the account, a white navigation strip in four groups (Plan, Explore, Record, System) with a green active underline that scrolls sideways on narrow screens; shared `PageHeader`, `Stats`/`Stat`, `Callout`, `Card` tones; `layout.tsx` titled FPL Copilot with the brand theme colour. Link labels and every `data-testid` unchanged |
| Pages, in the required order | Copilot Home as a gameweek dashboard (deadline countdown, headline strip: XI expected, bench, bank, free transfers, flagged players; the recommended action first with its lifecycle state; situation, squad health, captain profiles, the squad with FPL difficulty chips, the plan); Transfer Workbench (HOLD baseline row, illegal plans marked, hit cost / uncertainty / solver columns); Decision Journal, entry and traceability; Season Review (headline strip, status callout, every table scrollable); then auth, onboarding, Squad Planner (FPL-style pitch), Player Lab, Future Planner, What-If, Alerts, Backtest Lab, Data Health and Settings |
| Honesty preserved | stale / historical results, "timing not proven", insufficient samples, unproven optimality and degraded data are shown with the same words as before, now in one consistent style; nothing was hidden to make a page tidier |
| Defect found and fixed | the Backtest Lab tabs briefly took `role="tab"`, which removed them from the button role the browser test (and assistive technology) addresses them by: reverted to buttons with `aria-pressed` (`6af332a`) |
| Verification (source `6af332a`) | `tsc --noEmit` clean; `next build` clean; dev Playwright **15 passed** (0 failed; the full suite re-run after the fix); production Playwright against the local stack on the final web image `0f846fb0c7a1` with both auxiliary containers **24 passed, 0 skipped**; rendered pages inspected at 1440 and 390 px — zero horizontal overflow on every page after two fixes (a select wider than a phone viewport on Settings; the navigation strip scrolling at desktop width); committed screenshots regenerated from the production stack, now including the Season Review and phone views with a no-sideways-scroll assertion |
| Backend | untouched: no Python source changed in M1.6, so the M1.5 regression results stand (549 passed locally; CI green on `47f81ac`) |
| Defects 52 and 53 | unchanged and still **open** |

CI: `6af332a` (the M1.6 source: `71592f6`, `c297330`, `64dc080`, `6af332a`) —
[run 38758418989](https://github.com/adivishall/fpl-copilot/actions/runs/38758418989) **success**
on every job: lint, security, test, optimiser suite, web e2e and container builds. The run of
`64dc080` was cancelled when `6af332a` superseded it (its web-e2e job would have failed on the
tab-role defect fixed there). This documentation commit, with the regenerated screenshots in
`docs/images/`, is checked by its own run. The GW7 checkpoint (§2g) is still in the future.

## 2i. Release sprint — public deployment path, browser extension, configurable name (2026-10-16)

Scope (owner directive of 2026-10-16, hard deadline Sunday 18 October 2026, 23:59 IST): ship the
existing stack on a real public HTTPS URL with the invite-only beta flow intact (P0), and the
smallest useful Chrome Manifest V3 extension (P1); resolve the "FPL Copilot" naming collision
(fplcopilot.com is another FPL product) without derailing the release; GW7 capture (§2g) stays a
parallel task. No forecasting, optimisation, scoring, journal, invalidation, ownership or API
change. **There is still no public URL at this writing**: this section records what was built and
verified on the development machine, and exactly what the owner must provide.

| Area | Status | Result |
|---|---|---|
| Blockers found in the first 10 minutes | reported | checkout clean at `c95ea4e` (= `develop`, `main` `b4c8fde`), CI green, GitHub auth working, Colima and the local stack healthy; **no hosting account, cloud CLI, SSH target, tunnel login or DNS zone on this machine**, so nothing could be deployed publicly; no extension code existed (the docs were right); the naming collision is recorded in `docs/PRODUCT_GAP_ANALYSIS.md` |
| Owner inputs requested (one message) | BLOCKED on the owner | a Linux host (Ubuntu 24.04, 4 vCPU, 8 GB, ≥ 40 GB, ports 22/80/443; SSH public key `fpl-copilot-deploy` created on the development machine); a domain and its DNS; the Let's Encrypt e-mail; the product name (candidates: *Holdline*, *Fanchart*, *Deadline Ledger* — "Squad Ledger" dropped, an existing sports-team finance product); whether a Chrome Web Store developer account exists; keep or drop the outer site login (recommended: drop). Costs stated: VM ≈ US$12–48 per month, domain ≈ $10–40 per year, Web Store $5 once |
| Deployment preparation | PASS (development machine) | `infra/scripts/host_setup.sh` executes the runbook on a fresh Ubuntu host, idempotently and without printing a secret (Docker from Docker's apt repository, the repository at `FPL_GIT_REF`, `.env` with a generated database password, both API keys from `fpl-api create-key` with only hashes in `.env` and the operator key in `/srv/fpl-secrets/operator_key`, images built with the revision recorded, migrations, historical data, snapshot, serving forecast, the stack behind Caddy, a daily backup in cron with 14 days kept, `ufw`); `bash -n` and shellcheck clean; the key-generation and `.env`-editing snippets dry-run against a scratch copy of `.env.example` with keys from the local image (3 values set, key file mode 600, keys discarded). **Not run on a real host** (none exists) |
| Outer site login | PASS | optional since M1.1a made accounts the authentication: an empty `auth.caddy` turns the gate off, a `basic_auth` block keeps it; both validated with `caddy validate` (caddy 2.10 image); Caddyfile comment and runbook updated |
| Product name | PASS | `FPL_PRODUCT_NAME` is read by the web server on every request (`lib/product.ts`; root layout `force-dynamic`; `ProductProvider` → brand, `aria-label`, page title, sign-in text); a throw-away `fpl-web` container started with `FPL_PRODUCT_NAME="Example Name"` served `<title>Example Name</title>` and `aria-label="Example Name home"`; the local stack keeps the default "FPL Copilot" until the owner chooses. "Copilot" elsewhere in the UI names the advisor, not the product (unchanged; the browser tests assert "followed Copilot") |
| Onboarding hand-off | PASS | `/onboarding?entry=<id>`: a valid id (1–10 digits, no leading zero) is filled in and looked up once as if typed, with a note that it came from the link; the confirmation step and sign-in still stand; an invalid id in the link is refused with a message; the sign-in redirect now keeps the query string and the sign-up link carries `next`. Dev Playwright: the new flow and the existing onboarding flow **2 passed** (built UI + throw-away API). Production Playwright against the local stack with the rebuilt web image and `E2E_FPL_ENTRY_ID=1`: the onboarding test, now hand-off path then typed path with the real import, **1 passed** (throw-away account erased by the suite) |
| Browser extension | PASS (built, tested) | `apps/extension/`: Manifest V3, permission `activeTab` only (no host permissions, content scripts, background worker, storage or credentials); the popup reads the clicked tab's address and offers *Open team <ID> in <product>* → `<site>/onboarding?entry=<ID>` on a `fantasy.premierleague.com/entry/<ID>/…` page, otherwise *Open <product>* with a hint; `build.sh --url --name` writes `dist/unpacked/` and a reproducible ZIP (sorted entries, fixed timestamps) with its SHA-256; unit tests **7 passed** (address patterns incl. unsupported pages, missing, zero, non-numeric, over-long and look-alike-host ids; decisions; manifest permissions; two builds byte-identical; refusal of a plain-http or path-bearing production address); browser tests **5 passed** (the built extension loaded in Chromium against a stand-in web app: manifest, both popup states, the hand-off sends one GET with no cookie, authorisation or custom header, nine unsupported/invalid addresses, and an unreachable web app leaving the user on the browser's own error page). Store listing draft (`STORE_LISTING.md`), privacy notice (`docs/EXTENSION_PRIVACY.md`), 1280×800 store screenshots renderer (`tools/store_shots.sh`). Release ZIP, checksum and store submission: **NOT RUN** — they need the public URL and the product name |
| Manual "Load unpacked" in Google Chrome | NOT RUN | Google Chrome 155 on this machine ignores `--load-extension` (branded builds dropped it in 137; `chrome-extension://…` answered `ERR_BLOCKED_BY_CLIENT`), so the flow cannot be driven from this session, and the Claude in Chrome connection was not available. Playwright's Chromium loads the same unpacked directory through the same path (the id derived from the directory, as Chrome does). The owner's step: `chrome://extensions` → Developer mode → Load unpacked → `apps/extension/dist/unpacked` (`apps/extension/README.md`) |
| Web verification (source `22a8406`) | PASS | `tsc --noEmit` clean; `next build` clean (every route dynamic by design, for the runtime name); the local `fpl-web` image rebuilt from `22a8406` and running on the local stack |
| Backend | untouched | no Python source in the API, worker or packages changed; the extension's two tools and the host script pass ruff. CI's first run of the sprint commits failed one consistency test, `test_env_example_names_map_to_settings` (every `FPL_*` name in `.env.example` must be a `Settings` field or a known web-server variable): `FPL_PRODUCT_NAME` is a web-server variable, so the test's allow-list gained it, with a new assertion that the compose `web` environment carries it (`6dea528`); the other 541 tests passed in that run |
| GW7 capture | PASS (`prospective`) | captured unattended by the scheduler at its first tick after the cutoff (09:32 UTC; the host slept through the cutoff) and verified by hand 2026-10-17 12:37 UTC — §2g stage table |
| Public deployment | NOT RUN / BLOCKED | no host, domain or DNS access; a local Docker deployment, green CI or an image build is not a public deployment and is not claimed as one |
| Three read-only reviews (deployment readiness, extension release, launch prerequisites; 2026-10-16) | PASS (findings fixed) | **Deployment:** behind the proxy every browser shared one rate-limit bucket (the web key's), so one visitor could throttle everyone's sign-in and password guessing was bounded only site-wide — fixed: with `FPL_TRUST_FORWARDED_FOR` (set by the host script) the web key's requests count per `X-Forwarded-For` address, which Caddy sets from the connection and the Next server forwards; operator keys count per key; sign-in attempts are limited per e-mail address (`FPL_LOGIN_ATTEMPTS_PER_MINUTE`, default 10); sign-in and sign-up are no longer proxied through `/backend/*` (the raw session token never reaches a script). `host_setup.sh` would have failed writing the root-only operator key as an unprivileged user — it now re-executes under `sudo -E`, passes secrets on stdin (never on a command line), bounds container logs and refuses to start Caddy until the site name resolves to the host's public address. **Extension:** the register page's sign-in link dropped the hand-off and `?next=//host` was an open redirect after sign-in — fixed (`lib/nav.ts`: same-site paths only), the popup prefers a pending navigation's address, the build refuses names that would splice into JSON or HTML, and the store's required 440×280 promo tile is rendered. **Prerequisites:** Holdline is crowded (a Windows dictation product owns getholdline.com and holdline.app); Fanchart and Deadline Ledger have no product, app or extension conflicts, with fanchart.app/.io and deadlineledger.com/.app/.io unregistered; Hetzner's 4-vCPU cost-optimised plans are out of stock and new accounts face identity checks, so DigitalOcean (8 GB, $48/month, BLR1 or LON1) or Vultr ($48, Mumbai/London) is the frictionless host for this weekend; Chrome Web Store needs 2-Step Verification, the one-time fee, a privacy policy URL, 1–5 screenshots at 1280×800 and the promo tile, offers unlisted visibility, and may reject a popup whose only function is opening a site (the popup shows its detected-team work; the risk is noted) |
| Verification of the review fixes (source `56e08c7`) | PASS | backend: ruff, mypy clean; the rate-limit, forwarded-visitor, sign-in-limit, login/session and ops-config tests **9 passed**; dev Playwright hand-off flow (sign-in through `next`, register link, `//host` refused) **2 passed** on a rebuilt UI; production suite against the local stack rebuilt from `56e08c7`: key enforcement incl. the proxy denial **1 passed**, onboarding hand-off + real import **1 passed**; extension unit **8 passed**, browser **5 passed** |
| Zero-spend override (owner directive, 2026-10-17) | in progress | budget ₹0: no paid VM, no domain purchase, no Chrome Web Store fee. Target: an Oracle Cloud **Always Free** Arm instance (documented allowance verified on Oracle's Always Free page the same day: `VM.Standard.A1.Flex` 2 OCPUs / 12 GB, 200 GB block storage, 10 TB/month outbound, home region only, idle instances reclaimable after 7 days under 20 % CPU/network/memory), a free `*.duckdns.org` name (on the public suffix list, checked) with Let's Encrypt through Caddy, display name *Fanchart* by setting, the extension as a GitHub Release ZIP. Free alternatives checked and rejected for the full stack: Hugging Face Docker Spaces now need a paid plan (only static Spaces are free), Neon's free Postgres suspends after 5 minutes idle and no free tier keeps a worker and scheduler alive. Owner steps (account, instance, security-list rules, DuckDNS name) sent; nothing provisioned yet |
| Rehearsal of the host script on a real Ubuntu 24.04 arm64 VM (Lima, 2 CPUs, 6 GB; `8441eeb`; 2026-10-17 13:48–13:58 UTC) | PASS | `host_setup.sh` ran unattended with `FPL_TLS=internal` and `FPL_SKIP_DNS_CHECK=1` (rehearsal only): Docker 29.9.1 + Compose 5.6.0 installed, log rotation, 2 GB swap, repository cloned at `8441eeb`, `.env` with a generated password, images built on arm64 in 2 min 42 s (`fpl-engine:8441eeb1c9de` `96c8f914c08e`, `fpl-web:8441eeb1c9de` `147993435388`), both keys generated (operator key root-only, never printed), migrations to 0008, historical ingest 1.5 min, snapshot, serving forecast ~2 min on 2 cores, all seven services healthy behind Caddy, only 80/443 listening publicly, cron backup installed, `ufw` active — 8 min 15 s end to end. Then, with the name switched to `127-0-0-1.sslip.io`: `<title>Fanchart</title>`, HTTP → HTTPS `308`, HSTS / nosniff / `X-Frame-Options: DENY` / referrer policy present, no `Server` header, TLS 1.3 (internal CA), smoke test **5/5** through the proxy, invite → register `200` → `/backend/auth/me` `200` → another manager's key `403` → proxied sign-in `404` → invites without a key `403` → erase `200`, `backup.sh` wrote a 14.2 MB dump of 50 tables + 10.8 MB data archive with checksums, users persisted across an API restart, scheduler ticking. Production Playwright suite from the development machine through an SSH tunnel (`E2E_IGNORE_HTTPS_ERRORS` for the internal CA, `E2E_FPL_ENTRY_ID=1`): **22 passed, 0 failed, 2 skipped** (the two auxiliary-container flows). Oracle's pre-installed reject-all firewall rules were then reproduced on the VM and the script's firewall block inserted and persisted the 80/443 rules idempotently, the site answering `200` on the VM's own address. Not rehearsed: a publicly trusted certificate and external reachability (no public host yet) |
| Defects 52 and 53 | unchanged | still **open** |

Definition of done (owner directive §10), as of this writing: **Website** — public HTTPS URL
BLOCKED (owner inputs); deployed SHA, image identifiers, public health and smoke test, production
browser totals, account/import flow on the public site, worker and scheduler on the host,
database persistence, external access, security posture and backup on the host: NOT RUN.
**Extension** — source and version PASS (`apps/extension/`, 0.1.0); build and test commands
with results PASS; ZIP, checksum, manual installation, store submission: NOT RUN (public URL and
name needed). **Repository** — clean tree, commits pushed, CI on the exact commit: below;
`develop` updated only after the required checks; `main` untouched; deployment documentation
updated to describe the script and the optional gate, and this section records the real state.

CI: the sprint source (`96c27a1` web, `8f33260` extension, `22a8406` ops, `6dea528` test
allow-list) — [run 38811378979](https://github.com/adivishall/fpl-copilot/actions/runs/38811378979)
on `6dea528` **success** on every job: lint, security, test, optimiser suite, web e2e and
container builds. The earlier run of `22a8406`
([38809210431](https://github.com/adivishall/fpl-copilot/actions/runs/38809210431)) failed only
the consistency test described above (541 passed; container builds skipped behind it). The review
fixes (`696472f` API, `6ed0d66` web, `ae71f8c` extension, `56e08c7` ops) —
[run 38861918750](https://github.com/adivishall/fpl-copilot/actions/runs/38861918750) on
`56e08c7` **success** on every job. This documentation commit is checked by its own run;
`develop` is fast-forwarded only after that run is green. The public deployment and the extension
release remain NOT RUN; the GW7 capture is recorded in §2g.

## 3. Environment

Apple M4 (10 logical CPUs), 16 GB RAM, macOS 27.0.1; Python 3.12.15 (uv), Node 22.23, PostgreSQL
16.15, Redis 8.10 (tests) / 7 (containers), Docker 29.5 via Colima (4 vCPU, 8 GB VM). Network: the
FPL API, GitHub raw, PyPI, npm and Docker Hub reachable.

## 4. Technical decisions

`docs/decisions/` — ADR-0001 (spec interpretation), ADR-0002 (monorepo & layering), ADR-0003
(sources & degraded operation), ADR-0004 (point-in-time model; rule S1 and A1/A2 added in M14),
ADR-0005 (versioned rulesets; club-move addendum), ADR-0006 (forecasting), ADR-0007 (optimiser &
decision policy), ADR-0008 (model registry), ADR-0009 (async jobs & caching), ADR-0010
(evidence-based explanations).

## 5. Assumptions

- Decision cutoff = deadline − 90 minutes.
- Historical price at a deadline = last observed price before the cutoff (A2 for new players).
- Schedule: moved fixtures stay in their original round until the original deadline (or an
  earlier announcement of the new date); rescheduled fixtures become visible 28 days before kickoff.
- Banked free transfers are kept (no +1) in Wildcard / Free Hit weeks (2024-25+).
- A club excess caused by a mid-season move may be kept or reduced, never increased.

## 6. Known limitations

`docs/KNOWN_LIMITATIONS.md` (single-tenant security model; ~11-minute live results capture;
selection bias; development-machine timings; availability model uncalibrated; …).

## 7. Remaining work

Genuinely open items, in priority order (details in the audit and limitations):

1. ~~Ingest completed-gameweek results from the live API~~ — done in the V1 work (§2a).
2. Real per-user authentication/authorisation if the system is ever exposed beyond a trusted group.
3. Expose league analytics (head-to-head, template, differential) through the API/UI.
4. A public deployment target (VM + TLS reverse proxy; the host script and every owner input
   are listed in §2i — the host, domain and name are still missing), the browser extension's
   release ZIP and Chrome Web Store submission (§2i), Alertmanager routing, and archival of
   raw payloads / live-observation tables (snapshot and cache retention is done).
5. Evaluate the live models on 2026-27 outcomes as the season progresses (first unseen season):
   the forecast record and the evaluation run since M1.5 (§2g); the first formal look needs
   8 final live gameweeks (GW7–14, about 10 December 2026).

## 8. Historical test record (M1–M5, from the original build log)

The counts below are those recorded when each milestone was completed; the current suite is in §2.

- M1: 25 tests — ruleset contracts ×5 seasons, schema drift, migration upgrade/downgrade/no-drift
  on real PostgreSQL; ruff/mypy/import-linter clean; image build.
- M2: 84 tests — contracts, fault-injection gates, canonical timestamps, retry/backoff + SSRF
  allow-list, PIT view incl. Hypothesis future perturbation, idempotent re-ingestion, snapshot
  equality across memory/DB/Parquet, conflict arbitration.
- M3: 153 tests — every scoring rule, official-points reproduction, selling prices, formations,
  auto-subs, free transfers, chips, Free Hit reversion; Hypothesis invariant suites.
- M4: 183 tests — feature builder, team strength recovery, metric closed forms, harness causality.
- M5: 217 tests — rate shrinkage, BPS, minutes calibration, price features, forecast pipeline
  provenance, governance, model registry lifecycle.
- M6–M13: grew to 315 (fast tier) by the start of M14.
