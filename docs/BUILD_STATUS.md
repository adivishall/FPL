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
requirement and marks partial work as PARTIAL (99 requirements: 81 COMPLETE, 14 PARTIAL, 3 NOT IMPLEMENTED, 0 BLOCKED, 1 NOT VERIFIABLE).
V1 release work after M14 is planned and logged in `docs/V1_RELEASE_PLAN.md`; its verification
records are §2a and §2b (final). Public deployment waits on operator inputs (host, domain,
secrets); everything else for V1 is done.

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
| CI | green on `7eab35d` (lint, test, optimizer-suite, web-e2e, security, containers) before this work; the release commit's run is checked after the push |
| Python | fast tier 384 passed, 0 failed (6 deselected); slow + network tiers 6 passed (official points on all historical rows; live-API contracts); ingestion / live / worker / retention set 64 passed |
| Lint / types | ruff, ruff format, mypy (110 files), import-linter 4/4, `tsc`, `next build`: clean |
| Playwright | production topology 19/19 through Caddy (TLS + login) → web proxy → API → Postgres / Redis / worker; development flows 7/7 |
| TLS | chain valid against the CA, TLS 1.3, TLS 1.1 refused; HTTP `308` → HTTPS with path and query; `401` without / with a wrong login; HSTS, nosniff, frame, referrer and permissions headers; no `Server` / `X-Powered-By`; API, web, Postgres, Redis on loopback only; smoke test through the public origin 5/5 |
| Backup / restore | backup with writers paused: 16.0 MB dump (38 tables with data), 82.5 MB data archive, checksums; restored into a new Compose project with empty volumes (full public topology) in 78 s; restored dump: 40/40 row-count lines equal; manager squad, settings, recommendation id, decision, optimality equal; trace complete; new recommendation + alert jobs succeeded; restored scheduler captured live data; new TLS certificate served behind the login |
| Live data | GW1–GW5 3,216 rows, lineage to the succeeded bundle job, 0 conflicts, GW1 revisions provenance-only, deadlines equal to the live API, historical seasons vaastav-only, pinned evaluation snapshot unchanged (GW1 only), no job stuck `running` |
| Security | log scan of 10 containers after the final run: 0 lines with any of 6 secrets, 0 raw manager keys; gitleaks over the full history (29 commits): no leaks; webhook SSRF tests in the fast tier; export / delete / audit exercised (E2E and manual erasure of test managers) |
| Retention | 28 successful retention jobs in two days; 26 snapshots on disk; pinned evaluation snapshot kept |
| Observability | Prometheus: api and worker targets up, 12 alert rules loaded, none firing; request, freshness, forecast, job, queue, optimisation, recommendation, model-metric and security series present |
| Performance | `ml/reports/performance_stages.md`; deployed recommendation jobs 42 s (3 GW), 90 s (5 GW); replacement picker 12.5–48.9 s |

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
4. A public deployment target (VM + TLS reverse proxy), Alertmanager routing, and archival of
   raw payloads / live-observation tables (snapshot and cache retention is done).
5. Evaluate the live models on 2026-27 outcomes as the season progresses (first unseen season).

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
