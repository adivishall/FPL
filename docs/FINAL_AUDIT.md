# Final specification audit (§92)

Every major requirement of *FPL Decision Engine — Flagship Project Specification* mapped to its
implementation, the test or measured artifact that supports it, and an honest status. Audited on
the repository state of this commit, on 2026-10-06, after the M14 verification work; updated on
2026-10-07 for the V1 release work (`docs/V1_RELEASE_PLAN.md`).

**Status rules.** COMPLETE = implemented, integrated, and backed by an automated test or a
reproducible measured artifact. PARTIAL = implemented in part, or implemented but not reachable /
not verified end to end. NOT IMPLEMENTED = absent. BLOCKED = cannot be done in this environment.
NOT VERIFIABLE = implemented, but the evidence would need an environment or data this project
does not have. Code existing is never enough for COMPLETE.

Abbreviations: `T:` test file(s); `R:` generated report; `E2E` = production-topology Playwright
suite `apps/web/e2e-prod/production.spec.ts` (18/18 against Docker Compose).

## Data

| Requirement | Spec | Implementation | Files | Test/Evidence | Status | Notes |
|---|---|---|---|---|---|---|
| Historical ingestion from a pinned source | §6.1, §55 | extract → validate → canonicalise → load, idempotent | `services/ingestion/.../pipeline.py`, `sources/historical.py` | T: `test_ingestion_pipeline.py` (idempotent re-run, revisions); real run of 5 seasons (113,870 rows) | COMPLETE | |
| Live capture: bootstrap, fixtures, prices, status, news | §6.1 | `ingest_live`, `live.py` | `fpl_ingestion/live.py`, `sources/fpl_api.py` | real captures on this machine and hourly in Compose; T: `test_sources.py` contracts | COMPLETE | verified against the real API |
| Live capture of completed-gameweek match results | §6.1 | `ingest_live_results`: per-player `element-summary` histories → archive contracts → quality gates → canonical results only (source `fpl_api`); scheduler task `live_results` when results are missing, then daily during the 4-day correction window | `fpl_ingestion/pipeline.py`, `live.py`, `fpl_worker/cli.py` | T: `test_live_results.py` (rows equal the archive path's, results-only load, idempotent, drift quarantined, outage fails cleanly), `test_schedule.py`; network contract test; real run: 3,216 rows GW1–GW5, GW1 equal to the archive, re-run unchanged | COMPLETE | ~11 min per capture (one request per player); KNOWN_LIMITATIONS §2 |
| Raw payloads stored unchanged, content-addressed | §55.1 | `RawStore` | `fpl_storage/raw_store.py` | T: `test_raw_store_and_pit.py` | COMPLETE | |
| Canonical schema, stable ids across seasons | §8, §54 | 37 tables, Alembic | `fpl_storage/models.py`, `db/migrations/` | T: `test_migrations.py` (upgrade/downgrade/no drift) | COMPLETE | |
| Reproducible snapshots | §55, §86.2 | content-hashed Parquet + manifest | `fpl_storage/dataset.py` | same `snap_b64560a8c4f434ad984e` rebuilt on macOS and in Linux container | COMPLETE | |
| Provenance (row → raw payload → source revision) | §6, §76.2 | `raw_snapshot_id` per row, `data_jobs`, trace endpoint | `load.py`, `apps/api/.../lineage.py` | T: `test_api.py::test_traceability_chain_is_complete`, `test_lineage_snapshots.py`; E2E trace | COMPLETE | |
| Freshness SLA and stale-data messaging | §55.2, §82 | freshness block on every response; health `live_source` from recorded captures | `fpl_api/services.py`, `app.py` | T: `tests/unit/api/test_freshness_context.py`, `test_api.py` health tests; E2E freshness | COMPLETE | |
| Data-quality gates | §55.2 | contracts, uniqueness, referential integrity, ranges, temporal ordering, conflicts | `contracts.py`, `quality.py` | T: `test_contracts_and_quality.py` (fault injection) | PARTIAL | no ingestion-level *drift* gate (feature/model drift is monitored in `governance.py`) |
| Versioned rulesets verified against the official source | §51, §91 | YAML rulesets per season; live comparison with `game_settings`, chips and scoring | `config/rules/*.yaml`, `fpl_domain/rules/` | T: `test_live_rules.py` (network, 3/3 against the live API); `test_rulesets.py`; points reproduced on 113,870 rows | COMPLETE | hit cost / auto-sub / bonus ties not exposed by the API: verified empirically |
| Source pinning | §51, ADR-0003 | `config/sources.yaml` commit; backtest snapshot pinned in `config/backtest/default.yaml` | | backtest refuses other snapshots; trace checks pinned commit | COMPLETE | |
| Degraded mode | §82 | live unreachable → last validated snapshot + 503 sync; price model absent → continue; optimiser timeout → validated incumbent; conflicts recorded | `services.py`, `app.py`, `milp.py` | T: `test_api.py::test_sync_degrades…`; optimiser forced-timeout benchmark | PARTIAL | forecast-model failure has no automatic baseline fallback; snapshot promotion keeps serving the last good forecast instead |

## Forecasting

| Requirement | Spec | Implementation | Files | Test/Evidence | Status | Notes |
|---|---|---|---|---|---|---|
| Point-in-time feature engineering (registry + data dictionary) | §9, §56 | 49 registered features | `services/feature-store/` | T: `test_builder.py` (future-perturbation property, version lock); `docs/DATA_DICTIONARY.md` | COMPLETE | |
| Expected minutes / P(start) | §10, §58 | calibrated LightGBM + availability layer | `fpl_forecasting/minutes.py` | R: `forecast_eval.md` (Brier 0.083 vs 0.101, ECE 0.011); backtest start ECE 0.007–0.023 | COMPLETE | availability layer is configuration (no historical news) |
| Decomposed point prediction | §11, §57 | appearance/goals/assists/CS/DC/cards/bonus | `pipeline.py`, `params.py` | R: `forecast_eval.md` RMSE 1.924 vs 2.031 best baseline (CI excludes 0) | COMPLETE | |
| Probabilistic distributions + joint Monte Carlo | §12, §59 | common-random-number joint simulation | `packages/simulation/` | T: `test_engine.py`; R: CRPS 0.628 vs 0.722 climatology | COMPLETE | |
| Calibration (reliability, PIT, coverage) | §13, §57.2 | | `forecasting/metrics.py` | R: PIT 80 % coverage 0.804; backtest report reliability table | COMPLETE | |
| Model versioning, registry, promotion gates, rollback | §71 | registry with status machine; pre-registered gates | `fpl_storage/registry.py`, `config/models/*.yaml` | T: `test_model_registry.py`, `test_governance.py`; R: gates PASSED | COMPLETE | one gate redefined after seeing results (documented) |
| Drift monitoring and retraining triggers | §71 | PSI / residual / ECE checks | `governance.py` | T: `test_governance.py` | PARTIAL | not yet run against live 2026-27 outcomes |
| Leakage prevention | §7, §70.1 | PIT view, schedule rule S1, cutoff assertions | `fpl_storage/pit.py`, `schedule.py` | T: future-perturbation tests incl. full engine; `test_schedule.py`; R: 0 look-ahead violations | COMPLETE | selection bias remains (design iterated on evaluated seasons) |
| Price-change model | §23, §66 | calibrated rise/fall classifiers | `price_change.py` | R: `price_change.md` | COMPLETE | |
| Fixture/team context (strength, DGW/BGW) | §14 | recency-weighted team model | `team_strength.py` | T: `test_team_strength.py`; R: `team_strength_tuning.md` | COMPLETE | |

## Decision engine

| Requirement | Spec | Implementation | Files | Test/Evidence | Status | Notes |
|---|---|---|---|---|---|---|
| HOLD always valid; hold vs transfer vs hit vs chip | §18, §86.1 | paired-gain thresholds vs HOLD | `fpl_decision/engine.py` | T: `test_decision_engine.py`; R: backtest action table | COMPLETE | |
| Replacement picker (whole-squad rebuild, alternatives) | §17, §60 | universe screen → shortlist → MILP re-optimisation | `replacement.py` | T: `test_api.py::test_lineup_optimize_replacement`; E2E transfer workflow | COMPLETE | |
| Transfer optimiser with all constraints | §16, §61 | MILP + independent validator | `fpl_optimizer/milp.py`, `validate.py` | T: `test_milp_vs_bruteforce.py`, `test_club_moves_milp.py`; R: `optimizer_benchmark.md` | COMPLETE | |
| Multi-GW planning | §22, §62 | horizons 1–8 served (`horizon_max`, configurable) | `milp.py` | R: optimiser benchmark (1/3/5/8 GW); backtest plan stability | COMPLETE | 8-GW solves: p95 43.5 s, 1 of 36 hit the 60 s limit |
| Captain / vice / bench / formation | §19, §20, §63 | exact lineup solver, captaincy analysis | `lineup.py`, `captaincy.py` | T: `test_lineup_and_initial.py`, `test_captaincy.py`, property auto-sub tests; E2E pitch matches `/lineup` (captain, vice, bench order) | COMPLETE | |
| Ownership-adjusted captain option | §20 | — | | | NOT IMPLEMENTED | expected / safe / high-variance options exist |
| Chip planner (two sets, windows, waiting value) | §21, §64 | | `chips.py` | R: backtest chip table (18 engine chip plays) | COMPLETE | |
| Transfer hits, free-transfer rollover | §16 | state machine | `fpl_domain/state.py` | T: `test_state_machine.py`, property tests | COMPLETE | |
| Price / selling-value economics; price-risk planning | §23, §66 | selling-price rule; P(plan unaffordable) | `squad.py`, `notifications/rules.py` | T: brute-force check of price-risk convolution; R: `alerts_audit.md` | COMPLETE | |
| Blank / double gameweeks | §14, §41 | schedule-aware forecasts and plans | `pipeline.py`, `schedule.py` | R: backtest across 45 real moved fixtures | COMPLETE | |
| Squad structure / club limit incl. mid-season club moves | §16 | domain validator; club allowance | `squad.py`, `state.py` | T: `test_club_moves.py` | COMPLETE | rule interpretation recorded in ADR-0005 |
| Initial squad optimiser | §15 | | `milp.py` | T: `test_lineup_and_initial.py`; E2E build squad | COMPLETE | |
| Objective profiles | §61.3 | default / conservative / aggressive | `config/optimizer/*.yaml` | T: `test_decision_engine.py::test_profiles…` | COMPLETE | |
| Robustness / stability | §29 | epistemic perturbation, near-optimal set | `stability.py` | T: decision tests; R: optimiser perturbation stability | COMPLETE | |
| Scenario / counterfactual engine | §25, §69 | 7 scenario kinds, paired re-simulation | `scenarios.py` | T: `test_api.py` what-if; E2E what-if without state mutation | COMPLETE | |

## Intelligence

| Requirement | Spec | Implementation | Files | Test/Evidence | Status | Notes |
|---|---|---|---|---|---|---|
| Injuries / news → availability | §67 | status/chance layer | `minutes.py` (availability) | unit tests only | PARTIAL | uncalibrated; no historical news |
| Role changes | §67, §74 | P(start) deltas | `notifications/rules.py` | R: `alerts_audit.md` (119 on real replays) | COMPLETE | |
| Ownership data | §24, §65 | captured in snapshots and match rows | | | PARTIAL | not used by the decision engine |
| League awareness (head-to-head vs rivals) | §24, §65 | library functions | `fpl_decision/league.py` | T: `test_triggers_league.py` | PARTIAL | not reachable through the API or UI (§88) |
| Differential strategy | §24 | setting stored, never read | `schemas.py` | — | NOT IMPLEMENTED | |
| Template exposure | §24 | `template_gaps` library function | `league.py` | T: unit | PARTIAL | not exposed |

## Explanation

| Requirement | Spec | Implementation | Files | Test/Evidence | Status | Notes |
|---|---|---|---|---|---|---|
| Reasons only from structured evidence | §26, §68, §81 | evidence objects + deterministic renderer (no LLM) | `evidence.py`, `render.py` | T: `test_decision_engine.py` (no unsupported reasons) | COMPLETE | LLM layer deliberately absent (ADR-0010) |
| Counterfactuals ("if you do nothing") | §68 | HOLD counterfactual, alternatives | `engine.py` | E2E recommendation | COMPLETE | |
| Uncertainty, upside / downside | §68 | p10/p50/p90, P(beats hold), downside scenarios | `engine.py` | E2E; R: squad-level calibration 83 % in p10–p90 | COMPLETE | |
| Traceability recommendation → source revision | §76.2 | lineage tables + consistency checks | `lineage.py` | T: `test_lineage_snapshots.py`; E2E trace on Docker | COMPLETE | two chain bugs found and fixed in M14 |

## Alerts (§74)

| Requirement | Spec | Implementation | Files | Test/Evidence | Status | Notes |
|---|---|---|---|---|---|---|
| Deadline reminder in the manager's time zone | §74 | | `rules.py` | R: `alerts_audit.md` (official deadlines, 3 zones, BST→GMT) | COMPLETE | |
| Injury / suspension / doubt | §74 | | `rules.py` | T: `test_rules.py` | PARTIAL | no real status changes available to replay |
| Role change, unexpected benching | §74 | | `rules.py` | R: alerts audit (119 / 50 on real data) | COMPLETE | |
| Price movement / planned-transfer affordability | §74 | exact P(unaffordable) | `rules.py` | T: brute force; R: alerts audit (15 alerts) | COMPLETE | |
| Fixture change affecting a plan | §74 | | `rules.py` | R: alerts audit (10 on real postponements) | COMPLETE | |
| Recommendation invalidation | §74 | re-solve on new forecasts | `alerts.py`, `rules.py` | T: `test_api.py::test_notifications_dedupe_and_materiality` | COMPLETE | not part of the replay audit |
| Post-gameweek review | §74, §38 | | `rules.py` | T: `test_api.py::test_post_gameweek_review_on_real_results`; R: 111 reviews | COMPLETE | |
| Materiality thresholds; dedupe; unchanged re-evaluation | §74 | state-based keys, unique constraint | `rules.py`, `store.py` | R: alerts audit; E2E re-evaluation adds 0 | COMPLETE | |
| Webhook delivery | §74 | | `store.py` | T: `test_webhook.py` (mocked transport) | PARTIAL | success path never sent to a real endpoint |

## Security and privacy

| Requirement | Spec | Implementation | Files | Test/Evidence | Status | Notes |
|---|---|---|---|---|---|---|
| API keys (hashed, constant time) incl. manager-linked reads | §35, §75 | | `security.py` | T: `test_api.py` key tests; E2E missing / invalid / valid | COMPLETE | single-tenant model (KNOWN_LIMITATIONS §6) |
| Rate limiting | §75 | token buckets before auth | `security.py` | T: `test_api.py`; E2E 429 + Retry-After through proxy | COMPLETE | per replica; shared bucket behind the proxy |
| Webhook SSRF protection | §75 | https only, no credentials, allow-list, port 443, every resolved address global (incl. IPv4-mapped / NAT64), no redirects, 5 s timeout | `store.py` | T: `test_webhook.py` (30 cases: malformed URLs, ports, private / loopback / link-local / CGNAT / multicast, DNS failure, redirect, timeouts, 4xx/5xx); E2E unsafe URLs rejected | COMPLETE | M14: malformed URLs (`https://[::1`, port 99999) raised a 500 → now rejected (422). DNS-rebinding window documented |
| Security event logs without secrets | §75 | manager keys in logged paths pseudonymised (`loggable_path`) | `app.py`, `security.py` | log scan of every container after the full E2E run: 0 API keys, proxy key or DB password; T: `test_api.py` (captured logs of rejected requests) | COMPLETE | M14 found and fixed two leaks: uvicorn access log, and rejected-request paths (`/managers/{key}/export`) |
| Audit log with hashed identifiers | §75 | | `privacy.py` | `pg_dump`: raw manager key absent after deletion | COMPLETE | |
| Export / deletion | §75 | | `privacy.py` | T: `test_api.py` (incl. two managers with identical squads keep separate, separately erasable records); E2E download + delete | COMPLETE | M14: identical squads shared one recommendation record across managers → per-manager ids |
| Secrets not committed | §86.2 | `.env` git-ignored, `.env.example` placeholders | | gitleaks: 23 commits, no leaks | COMPLETE | |
| Dependency scanning | §78 | pip-audit, npm audit | CI `security` job | pip-audit: no known vulnerabilities; npm audit: 0 | COMPLETE | |
| TLS in transit | §75 | — | | | NOT IMPLEMENTED | documented: terminate TLS in front of api/web |
| Short-lived tokens; encryption at rest | §75 | — | | | NOT IMPLEMENTED | static API keys; spec says "where possible / required" |

## Platform

| Requirement | Spec | Implementation | Files | Test/Evidence | Status | Notes |
|---|---|---|---|---|---|---|
| REST API with the §72 capabilities | §32, §72 | FastAPI | `apps/api/` | T: `test_api.py` (18 tests); OpenAPI + `docs/API.md` | COMPLETE | |
| Frontend screens (§73.1) | §30, §73 | Next.js | `apps/web/app/` | E2E 17/17 (incl. Backtest Lab: rendered tables, every figure a report references loads); dev-topology flows | COMPLETE | M14: reports were shown as raw Markdown with a hard-coded, stale figure list |
| Server-side proxy (key never in browser) | §75 | `/backend/*` route | `apps/web/app/backend/` | E2E topology test | COMPLETE | |
| Worker, queue, job status | §33 | RQ + durable job rows + reaper | `jobs.py`, `fpl_worker/cli.py` | T: `test_worker.py` (real Redis, SIGKILLed horse); Docker crash recovery 32 s | COMPLETE | |
| Interrupted vs failed jobs; no partial results | §33, §82 | `failure` = `error` / `interrupted`; results published at the end (one transaction, atomic renames) | `jobs.py`, `services.py`, `dataset.py` | T: `test_worker.py` (timeout, SIGKILL, callback), `test_atomic_publish.py`; jobs killed by host sleep on the local stack were closed as interrupted and retried | COMPLETE | |
| Worker liveness probe | §34 | own heartbeat key within RQ's expiry | `fpl_worker/cli.py` | T: `test_worker.py` (idle gap, lost registry entry); Compose: healthy after removal from RQ's worker set + 330 s idle | COMPLETE | M14: probe flagged a working idle worker (180 s limit vs ~405 s idle beat; RQ's global set emptied after host sleep) |
| Snapshot and cache retention | §80 | newest N + serving + pinned + referenced kept | `fpl_api/retention.py`, scheduler `retention` job | T: `test_retention.py` (policy, idempotence, failures raised, DB references); Compose: 8 snapshots + 22 cache files (326 MB) removed | COMPLETE | raw payloads and DB observations are not pruned (KNOWN_LIMITATIONS) |
| Scheduler (idempotent buckets) | §33 | | `fpl_worker/cli.py` | T: `test_schedule.py`; Compose live refresh | COMPLETE | |
| Caching by snapshot + model | §80 | memory/disk forecast cache, promotion | `services.py` | R: `performance.md`; T: promotion test | COMPLETE | |
| Database migrations | §86.2 | Alembic | `db/migrations/` | T: `test_migrations.py`; Compose `migrate` | COMPLETE | |
| Docker images and Compose deployment | §34, §86.2 | | `infra/docker/`, `docker-compose.yml` | built and run locally; health checks; persistence; restart; E2E | COMPLETE | verified locally only |
| CI | §78 | lint, types, layering, tests, optimiser suite, e2e, security, containers | `.github/workflows/ci.yml` | GitHub Actions green on every pushed commit | COMPLETE | |
| CD (deploy from green main, release) | §78 | tag-triggered image publish + smoke test | `.github/workflows/deploy.yml` | never executed | NOT VERIFIABLE | needs a registry / target environment |
| Changelog | §78 | | `CHANGELOG.md` | | COMPLETE | |
| Metrics (§76.1) | §76 | API + worker (multiprocess) exporters | `observability.py`, worker | T: `test_ops_config.py`, forked-metrics test; Docker worker `/metrics` | COMPLETE | |
| Monitoring and alert rules | §37, §76 | Prometheus rules, runbook | `infra/deployment/prometheus/` | promtool: config valid, 12 rules; Prometheus in Compose scraped api + worker (both up), rules loaded | PARTIAL | no Alertmanager / paging; rules never fired in anger |
| Product modes (Quick Pick, Deep, Planner, Research, What-If) | §84 | as screens/routes, not as explicit modes | | | PARTIAL | |

## Evaluation

| Requirement | Spec | Implementation | Files | Test/Evidence | Status | Notes |
|---|---|---|---|---|---|---|
| Walk-forward backtest with strict cutoffs | §27, §70 | resumable runner | `fpl_backtest/runner.py` | T: `test_backtest.py`; R: `backtest.md` (0 violations) | COMPLETE | |
| Multiple seasons | §86.3 | 2023-24, 2024-25, 2025-26 (114 GWs) | | R: `backtest.md` | COMPLETE | 2023-24 trains on one prior season |
| ≥ 3 baselines with identical information | §28, §86.3 | 6 benchmarks | | R: `backtest.md` | COMPLETE | |
| Calibration measured | §19 (checklist) | | | R: `backtest.md`, `forecast_eval.md` | COMPLETE | |
| Confidence intervals | §70.4 | paired bootstrap per season and pooled | `backtest_report.py` | R | COMPLETE | exchangeability caveat |
| Reproducibility | §86.2 | pinned snapshot, seeds, checkpoints | | three full runs; every divergence between runs starts exactly where a rule fix applied (club moves; season end); optimiser re-solves 100 % deterministic | COMPLETE | |
| Optimiser benchmark vs simpler heuristic | §86.3 | | `ml/experiments/optimizer_benchmark.py` | R: `optimizer_benchmark.md` | COMPLETE | development-machine timings |
| Performance benchmark | §79 | | `infra/scripts/perf_probe.py` | R: `performance.md` | COMPLETE | development-machine timings |
| Synthetic leagues with known ground truth | §36 | tiny leagues + brute force | `tests/opt_util.py` | T: `test_milp_vs_bruteforce.py` | COMPLETE | |
| Closed feedback loop | §38 | post-GW review, forecast evaluation | | R: alerts audit, backtest report | PARTIAL | decision regret vs forecast error not attributed per decision |

## Portfolio evidence (§89)

| Requirement | Spec | Implementation | Files | Test/Evidence | Status | Notes |
|---|---|---|---|---|---|---|
| Architecture, data-lineage diagrams | §89 | Mermaid in README | `README.md` | | COMPLETE | hand-written, not generated from deployment |
| Database schema diagram | §89 | Mermaid ER generated from the SQLAlchemy models (37 tables, 42 foreign keys) | `infra/scripts/schema_diagram.py`, `docs/architecture/database-schema.md` | T: `test_ops_config.py::test_schema_diagram_is_generated_from_the_models` | COMPLETE | |
| Feature dictionary, model card, optimiser formulation, backtest tables, calibration plots, performance report | §89 | | `docs/`, `ml/reports/` | | COMPLETE | |
| Decision playback example | §89 | traceable recommendations, journal | | | PARTIAL | no curated "recommendation changed after new information" walkthrough |
| CI badge, health screenshot, demo GIF | §89 | badge; screenshots captured from the deployed stack | `docs/images/`, `e2e-prod/screenshots.spec.ts` | 6 screenshots | PARTIAL | no demo video/GIF |
| Known limitations and failed experiments | §89, §92 | | `docs/KNOWN_LIMITATIONS.md`, gate history, superseded backtest | | COMPLETE | |

## Summary

Counts over the rows above (99 requirements):

| Status | Count |
|---|---|
| COMPLETE | 80 |
| PARTIAL | 14 |
| NOT IMPLEMENTED | 4 |
| BLOCKED | 0 |
| NOT VERIFIABLE | 1 |

Nothing is BLOCKED any more: the live FPL API, Docker and Playwright — blocked in the earlier
cloud environment — all ran on the local machine.
