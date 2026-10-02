# BUILD STATUS — FPL Decision Engine

Persistent project status (spec §50.1 item 10). Updated at the end of every milestone after
verifying the actual repository state.

Legend: ☐ not started · ◐ in progress · ☑ done **and** tested · ⛔ implemented but blocked in the
build environment (see notes) · ✗ not done (with reason)

---

## 1. Milestone summary

| # | Milestone (§87 / §91) | Status | Gate | Evidence |
|---|------------------------|--------|------|----------|
| M0 | Discovery: repo inspection, ADRs, source inventory, ruleset schema | ☑ | No coding ambiguity remains | `docs/decisions/`, `docs/architecture/source-inventory.md` |
| M1 | Foundation: monorepo, CI, Docker, DB migrations, domain models, config | ☑ | Clean build + tests pass | 25 tests (incl. real-Postgres migration round-trip, zero drift); ruff/mypy/import-linter clean; image builds |
| M2 | Data: ingestion, canonical schema, quality gates, freshness | ☑ | Reproducible historical snapshot | 5 real seasons ingested (113,870 player-fixture rows); `snap_b64560a8c4f434ad984e` reproduced via memory/DB/Parquet; 84 tests |
| M3 | Rules: scoring, transfers, chips, squad validity, state transitions | ☑ | Rule/property tests pass | official points reproduced on 113,870/113,870 real rows; 153 tests incl. 4 Hypothesis invariant suites |
| M4 | Forecasting baselines: PIT features, team strength, baselines | ☑ | Metrics reproducible | 49 registered PIT features (leak-free property test); team model +0.055–0.12 nats/match vs league avg; 4 baselines over 114 walk-forward cutoffs (`ml/reports/`) |
| M5 | ML forecasting: minutes, point distributions, calibration, registry | ☑ | Out-of-sample results improve or justify model choice | 114-cutoff walk-forward: MC RMSE 1.927 vs 1.938 direct GBM vs 2.034 best baseline (bootstrap CIs exclude 0); CRPS −13 % vs climatology; PIT-calibrated; minutes/price gates pass; `docs/MODEL_CARD.md` |
| M6 | Optimizer: single-GW transfer/lineup, replacement, captaincy | ☐ | 100% generated squads legal on fixture suite | |
| M7 | Multi-GW planner, chips, scenarios | ☐ | Historical scenario tests pass | |
| M8 | Decision engine: hold/transfer/hit/chip, risk, stability | ☐ | Recommendation contract stable | |
| M9 | Explainability: evidence, counterfactuals | ☐ | No unsupported reasons | |
| M10 | Walk-forward backtesting, benchmarks, reports | ☐ | No-lookahead audit passes | |
| M11 | API + async jobs + cache | ☐ | Contract tests pass | |
| M12 | UI (Next.js) + e2e | ☐ | End-to-end flow works | |
| M13 | Production hardening: observability, alerts, performance, security, deployment | ☐ | Clean production smoke test | |
| M14 | Portfolio docs + FINAL_AUDIT + KNOWN_LIMITATIONS | ☐ | A reviewer can reproduce core claims | |

Order note: backtesting (M10) precedes API/UI (M11–M12); see ADR-0001 #19.

## 2. Current milestone

**M6 — Optimizer** (in progress: MILP with chips/FH/FT dynamics, exact lineup solver, independent
validator, brute-force verifier, candidate pool, HOLD + top-N alternatives, vectorised lineup
scoring on samples, captaincy engine — all tested; remaining: replacement engine, rotation
pairing, optimizer benchmark/perf report, formulation doc, CI optimizer-suite job). M0–M5 complete.

Environment: Python 3.12 (uv-managed; numpy 2.5 requires ≥3.12), Node 22, PostgreSQL 16
binaries, Redis 7, Docker 29, 4 CPU, 15 GB RAM. Network: historical dataset + PyPI + npm + Docker
Hub reachable; FPL API, premierleague.com, Debian mirrors and GHCR blobs blocked.

---

## 3. Specification checklist (spec → milestone)

### Data & state (§6–§8, §53–§56)
| Item | Spec | Milestone | Status |
|------|------|-----------|--------|
| Scheduled, cached bootstrap ingestion (no per-page-load fetch) | §6.1, §33 | M2/M11 | ◐ live client + `ingest_live` done; scheduling in M11 |
| GW-specific state capture (points, minutes, ownership, transfers, prices, availability) | §6.1 | M2 | ☑ historical; ⛔ live capture blocked in env (contract-tested) |
| Fixture/team ingestion with canonical IDs across seasons | §6.1, §6 table | M2 | ☑ stable `code` keys |
| Manager state ingestion (squad, bank, FT, chips, picks, transfers) | §6.1 | M2/M3 | ☑ reconstruction (FT replay cross-checked vs recorded hit costs, FH reversion, purchase prices, chips) tested on synthetic payloads; ⛔ live API blocked in env |
| Raw payloads stored unchanged (content-addressed) | §6.1, §55.1 | M2 | ☑ |
| External metrics with source/timestamp/confidence; source-priority layer | §6.2 | M2 | ☑ `underlying_stats` (source, priority, confidence); priority arbitration + conflict events; shots/box touches unavailable from any reachable source (interface only) |
| Extract→validate→normalize→feature→serve→audit pipeline with failure handling | §6 table | M2 | ☑ extract→audit; feature/serve in M4/M11 |
| Idempotent ingestion; checksum/source metadata | §55 | M2 | ☑ |
| DQ gates: freshness, uniqueness, referential integrity, ranges, temporal ordering, completeness, drift, conflict | §55.2 | M2/M13 | ◐ all but drift (M13) |
| PIT correctness for season aggregates, fixtures, injuries, price, ownership, tuning | §7 | M2/M4 | ◐ PIT view + property test done; feature/tuning side in M4 |
| Full §54 database schema + migrations | §8, §54 | M1 | ☑ 37 tables, Alembic `0001`, drift test |
| Domain entities (Season…AuditEvent) + manager state invariants | §53 | M1/M3 | ☑ entities; ManagerState/ChipState/FreeHitRevert with §53.2 invariants + provenance |
| ManagerState / DecisionState objects | §8 | M1/M8 | ☐ |
| Feature registry with declared metadata + DATA_DICTIONARY.md | §56 | M4 | ☑ registry → generated dictionary (sync test); AST-fingerprint version lock |
| Player feature families (availability, minutes, role, set pieces, attacking, defensive, team, fixture, form w/ shrinkage, trend, economics, correlation) | §9, §56.1 | M4 | ◐ availability/minutes/role/attacking/defensive/team/fixture/form/economics/live done; shrinkage applied in M5 rate models; correlation via joint simulation (M5) |
| Team/fixture features (recency, venue shrinkage, probabilistic fixtures, congestion, DGW/BGW, postponements) | §14, §56.2 | M4 | ☑ recency-weighted team model with home advantage (shrunk via prior), rest days, DGW counts, lag-gated DGW visibility |

### Forecasting (§10–§14, §57–§59, §67, §71)
| Item | Spec | Milestone | Status |
|------|------|-----------|--------|
| P(start), expected minutes, sub risk, return probability | §10, §58 | M5 | ☑ calibrated multi-horizon LightGBM (return probability via horizon feature), P(sub), minutes buckets; Brier 0.081 vs 0.100 baseline, ECE 0.010 |
| Decomposed points: appearance, goals, assists, CS, DC, cards, bonus, other | §11, §57.1 | M5 | ☑ per-component xP in every forecast row; joint fixture simulation |
| Price-change model | §57.1, §23, §66 | M5 | ☑ calibrated rise/fall classifiers vs naive trend; official predictor signal kept separate (`official_signal`); `ml/reports/price_change.md` |
| Monte Carlo point distributions with correlation | §12, §59 | M5 | ☑ CRN joint simulation; CRPS 0.630 vs 0.726 climatology; PIT 80 % coverage 0.803 |
| Recency weighting, Bayesian updating, hierarchical priors | §13 | M4/M5 | ☑ recency weights; team MAP/Laplace priors; Bühlmann–Straub player rates with position/price priors (κ validated out of sample) |
| Calibration (reliability, Brier, ECE), conformal intervals, ensemble, drift | §13, §57.2, §71 | M5/M13 | ☑ reliability diagrams, Brier/ECE/PIT; P1 ensemble + temporal conformal evaluated and documented (not adopted, reasons in MODEL_CARD); PSI/residual/ECE drift + retrain triggers (dashboard in M13) |
| Baselines compared; temporal splits; MAE/RMSE/log loss/Brier/CRPS | §57.2 | M4/M5 | ☑ 4 baselines + direct GBM + climatology + rate minutes baseline; cutoff-block bootstrap CIs |
| Model registry, promotion gates, rollback, seeds | §71 | M5 | ☑ `fpl_storage.registry` (content-addressed, status machine, rollback) + pre-registered gates in `config/models/*.yaml`; gate history kept (points@1.0.0 FAILED → 1.1.0) |
| Independent probabilistic fixture layer (team xG, CS prob, attack/defence indices, swings, DGW load, BGW risk, rotation pairing) | §14 | M4/M7 | ◐ team μ, CS prob, ratings with Laplace SD done; swings/rotation pairing in M7 |
| News/injury signals → start probability adjustments | §67 | M5 | ◐ live availability layer (status/chance, recovery, departed); uncalibrated by necessity (no historical news) |

### Optimisation & decisions (§15–§26, §29, §60–§65, §68–§69)
| Item | Spec | Milestone | Status |
|------|------|-----------|--------|
| Initial squad optimiser (horizon, bench value, budget flexibility, club concentration, captaincy structure) | §15 | M6 | ☐ |
| Transfer optimiser with all constraints (budget/selling price, 2-5-5-3, club ≤3, FT/rollover, hits, chips, deadline) | §16, §61 | M6/M7 | ☐ |
| Configurable objective profiles (max EV, risk-aware) | §16, §61.3, §83 | M6 | ☐ |
| Replacement picker (detect → candidates → forecast → rebuild → compare → explain → alternatives) | §17, §60 | M6/M8 | ☐ |
| Hold-vs-transfer decision class | §18 | M8 | ☐ |
| XI/formation/bench/captain/vice optimiser incl. rotation pairing | §19, §63 | M6 | ☐ |
| Captaincy engine (distributions, ceiling, expected/safe/high-variance, ownership-adjusted option) | §20 | M6 | ☐ |
| Chip planner (WC/FH/BB/TC, two sets, windows, waiting value, what-if) | §21, §64 | M7 | ☐ |
| Multi-GW planner (default 5, 2–10, locks, multiple sequences, plan stability) | §22, §62 | M7 | ☐ |
| Price/selling-value economics, affordability risk, transfer-now-vs-later | §23, §66 | M5/M7 | ☐ |
| Mini-league & ownership awareness (optional mode) | §24, §65 | M8 | ☐ |
| Scenario/counterfactual engine (9 scenario types + stress set) | §25, §69 | M7 | ☐ |
| Decision explanation & audit trail object | §26, §68 | M9 | ☐ |
| Robustness/stability (perturbation, near-optimal set, stable vs fragile) | §29 | M8 | ☐ |

### Evaluation (§27–§28, §36, §38, §70)
| Item | Spec | Milestone | Status |
|------|------|-----------|--------|
| Walk-forward backtest with information cutoff | §27, §70 | M10 | ☐ |
| Benchmarks: naive hold, recent-points, FPL-style heuristic, simple xP optimiser, single-GW optimiser, full engine | §28, §70.3 | M10 | ☐ |
| Metrics: MAE/RMSE, log loss/Brier, CRPS, calibration, regret, transfer efficiency, plan stability, validity rate, runtime, freshness | §28, §70.4 | M10 | ☐ |
| Synthetic toy leagues with known ground truth | §36 | M6 | ☐ |
| Closed feedback loop (forecast vs actual, decision vs forecast error) | §38 | M10/M13 | ☐ |

### Product (§30–§33, §72–§74, §84)
| Item | Spec | Milestone | Status |
|------|------|-----------|--------|
| API endpoints (§32 ∪ §72) + recommendation response contract | §32, §72 | M11 | ☐ |
| Async jobs, caching by snapshot+model, immutable runs | §33, §80 | M11 | ☐ |
| Screens: Overview, Transfer Lab, Squad Planner, Future Planner, Player Lab, Fixture Planner, Captain & Bench, Chip Planner, What-If/Scenario Lab, Backtest Lab, Decision Journal, Data/System Health, Settings | §30, §73 | M12 | ☐ |
| Dashboard information hierarchy | §31, §73.2 | M12 | ☐ |
| Product modes: Quick Pick, Deep Decision, Planner, Research, What-If | §84 | M11/M12 | ☐ |
| Notifications with materiality thresholds | §74 | M13 | ☐ |

### Engineering (§34–§37, §75–§83)
| Item | Spec | Milestone | Status |
|------|------|-----------|--------|
| Architecture: Next.js, FastAPI, Postgres, Redis/RQ, sklearn/LightGBM, HiGHS, Docker | §34 | M1+ | ☐ |
| Security: no plaintext credentials, secrets via env, rate limiting, input validation, SSRF prevention, audit log, delete/export | §35, §75 | M11/M13 | ☐ |
| Test layers: unit, property, integration, data quality, model, optimizer, backtest, e2e, load, failure recovery | §36, §77 | all | ☐ |
| Observability metrics + traceability chain | §37, §76 | M13 | ☐ |
| CI/CD contract (lint, types, tests, optimizer suite, backtest smoke, builds, dep+secret scan, migration validation, deploy from green main, changelog) | §78 | M1/M13 | ☐ |
| Performance targets measured | §79 | M13 | ☐ |
| Degraded modes (news, price predictor, model failure, optimizer timeout, source outage, conflicts, missing stats) | §82 | M2–M13 | ☐ |
| Versioned config tree (rules/models/optimizer/backtest) | §83 | M1 | ◐ loader + rules + sources done; model/optimizer/backtest configs land with their milestones |
| LLM layer optional, never owns truth | §81 | M9 | ☐ (decision: deterministic renderer, ADR-0010) |

### Documentation & portfolio (§41, §45, §89, §92)
| Item | Spec | Milestone | Status |
|------|------|-----------|--------|
| README with architecture/data-flow diagrams, methodology, limitations | §45 | M14 | ☐ |
| DATA_DICTIONARY.md, MODEL_CARD.md, BACKTEST_PROTOCOL.md, API.md, optimizer formulation | §52, §89 | M4–M14 | ☐ |
| Calibration plots, backtest tables, performance report, decision playback example | §45, §89 | M10/M13 | ☐ |
| FINAL_AUDIT.md (requirement → location → tests → status → limitation → evidence) | §92 | M14 | ☐ |
| KNOWN_LIMITATIONS.md | §92 | M14 | ☐ |

### P1 / P2 scope (§5)
| Item | Priority | Status |
|------|----------|--------|
| League-aware modes, differential analysis, scenario manager, chip planning, price-risk planning | P1 | ☐ |
| Notifications, scheduled refresh, alert rules, drift monitoring | P1 | ☐ |
| Ensembles, Bayesian updating, conformal intervals, contextual bandit / policy experiments | P1 | ☐ |
| RL policies, crowd-sourced injury signals, advanced rival-response simulation | P2 | ☐ (research extensions after core is robust) |

---

## 4. Technical decisions

See `docs/decisions/` — ADR-0001 (spec interpretation & 20 resolutions), ADR-0002 (monorepo &
layering), ADR-0003 (sources & degraded operation), ADR-0004 (point-in-time model),
ADR-0005 (versioned rulesets), ADR-0006 (forecasting architecture), ADR-0007 (optimizer &
decision policy), ADR-0008 (model registry), ADR-0009 (async jobs & caching),
ADR-0010 (evidence-based explanations).

## 5. Assumptions

- Decision cutoff = deadline − 90 minutes (configurable).
- Historical price at a deadline = last observed price strictly before the cutoff.
- DGW extra fixtures become visible 28 days before kickoff in backtests (configurable).
- Banked FTs are retained (no +1 accrual) when WC/FH is played (2024-25+), configurable.

## 6. Known issues / blockers

- Team-strength hyper-parameter tuning does not beat the hand-set default out of sample (within
  ~0.004 nats/match); the protocol-selected per-season configs are still used (documented).
- MAE favours median-like predictions under heavy-tailed points; RMSE (mean-optimal), Spearman
  and CRPS are the selection metrics (documented in `ml/reports/baselines.md`).

- Historical `fixtures.csv` is the *final* schedule; blanks caused by late postponements are
  visible earlier than they were in reality (DGW extra fixtures are lag-gated). Quantified in M10.
- Live event data for players with two fixtures in a GW is not split per fixture from the
  aggregated live payload (DQ warning); the end-of-season historical import provides the split.

- Points gate `interval_80_coverage` (points@1.0.0) was mis-specified for integer outcomes and
  failed (0.938); replaced in points@1.1.0 by PIT-based central coverage (0.803) after seeing the
  result — recorded in the config header, the report's gate history and the model card.
- The availability layer (FPL status → start probability) is configuration, not calibrated: no
  historical news exists. Price model information set is GW-granular (see MODEL_CARD §7).

- Docker builds inside this sandbox need the proxy CA passed as a BuildKit secret (documented in
  `infra/docker/README.md`); not needed in CI.

- `fantasy.premierleague.com` and `premierleague.com` are denied by the build environment's egress
  policy. Live sync and live-rule verification cannot be exercised here.

## 7. Files changed (by milestone)

- M0: `docs/BUILD_STATUS.md`, `docs/decisions/ADR-0001…0010`, `docs/architecture/source-inventory.md`.
- M5: `fpl_forecasting/{minutes,rates,params,pipeline,direct,forecast_eval,price_change,
  governance,model_config}.py`, `fpl_storage/registry.py`, `fpl_simulation/engine.py` (starts),
  `config/models/{points,minutes,price_change}.yaml`, `ml/experiments/{forecast_eval,
  price_change_eval,rate_shrinkage_check,feature_importance,ensemble_conformal}.py`,
  `ml/reports/{forecast_eval,price_change,rate_shrinkage,feature_importance,ensemble_conformal}.*`
  + figures, `docs/MODEL_CARD.md`, tests `tests/unit/forecasting/{test_rates,test_params,
  test_minutes,test_price_change,test_governance}.py`, `tests/integration/{test_forecast_pipeline,
  test_forecast_eval,test_model_registry}.py`.
- M4: `fpl_features/{registry,builder,labels,version_lock}.py`,
  `fpl_forecasting/{team_strength,team_eval,baselines,metrics,walkforward}.py`,
  `config/models/team_strength.yaml`, `ml/experiments/{team_strength_tuning,baselines_eval}.py`,
  `ml/reports/{team_strength_tuning,baselines}.{md,json}`, DATA_DICTIONARY §5 (generated),
  tests `tests/unit/{features,forecasting}/*`.
- M3: `fpl_domain/{scoring,squad,state,validation}.py`, `fpl_ingestion/manager_sync.py`,
  rulesets' scoring provenance → empirical, `docs/architecture/rules-verification.md`,
  tests `tests/unit/domain/{test_scoring,test_squad_and_lineup,test_state_machine}.py`,
  `tests/property/test_state_invariants.py`, `tests/unit/ingestion/test_manager_sync.py`,
  `tests/integration/test_full_scoring_reproduction.py` (slow), helpers `tests/domain_util.py`.
- M2: `fpl_storage/{raw_store,repositories,dataset,pit}.py`, `fpl_domain/freshness.py`,
  `fpl_ingestion/{http,contracts,quality,canonical,load,pipeline,live,cli,logs}.py`,
  `fpl_ingestion/sources/{historical,fpl_api,fpl_api_schemas}.py`, `config/sources.yaml`
  (PIT policies, source priority, SLAs), `data/fixtures/**` (real-data excerpts + API-shaped
  contract fixtures, with provenance README), `infra/scripts/{make_test_fixtures,make_api_fixtures,
  dev_postgres.sh}`, `docs/DATA_DICTIONARY.md`, tests under `tests/unit/{ingestion,storage}` and
  `tests/integration/test_ingestion_pipeline.py`.
- M1: root `pyproject.toml` (uv workspace, ruff/mypy/pytest/import-linter config), `uv.lock`,
  12 package skeletons (`packages/*`, `services/*`, `apps/api`, `apps/worker`),
  `fpl_domain` (enums, entities, hashing, versioned config, rules model/loader/schema export),
  `config/rules/{2022-23..2026-27,schema}.yaml`, `config/sources.yaml`, `fpl_storage`
  (models, db, ephemeral Postgres helper), `alembic.ini`, `db/migrations/*`,
  `.github/workflows/ci.yml`, `infra/docker/python.Dockerfile`, `docker-compose.yml`, `Makefile`,
  `.env.example`, `LICENSE`, tests under `tests/unit/domain`, `tests/integration`.

## 8. Tests performed (by milestone)

- M0: environment probes (runtimes, DB/Redis binaries, Docker daemon, network reachability of
  each data source) — results recorded in §2 and the source inventory.
- M1: `pytest` 25 passed (ruleset contract tests ×5 seasons, schema drift, invalid rulesets
  rejected, hashing property test, config versioning, migration upgrade/downgrade/no-drift on a
  real PostgreSQL 16 cluster); `ruff check`, `ruff format --check`, `mypy` (strict-ish, 24 files),
  `lint-imports` (4 contracts kept); `docker build` of the API/worker image + import smoke test.

- M2: `pytest` 84 passed — contracts (range/allowed/missing/duplicate/type), fault-injection
  gates (unknown fixture → quarantine, score mismatch, extra starter, position conflict,
  unpopulated starts), canonical availability timestamps, retry/backoff + SSRF allowlist (respx),
  FPL API contract parsing, raw-store integrity, PIT view incl. Hypothesis future-perturbation
  property, freshness thresholds; PostgreSQL integration: idempotent rerun (0 inserts/updates,
  0 revisions), memory = DB = Parquet snapshot id, quarantine leaves canonical data untouched,
  outage → failed job, upstream correction → 1 update + revision, DB CHECK blocks future feature
  snapshots, source-priority conflict arbitration. Real-data run: all 5 seasons via
  `fpl-ingest historical --all` (pinned commit) + `export-snapshot`.

- M3: `pytest` 153 passed — every scoring rule (appearance, goals by position & season, CS,
  GC, saves, pens, cards incl. bench bookings, OG, DC thresholds by position, bonus tie rules +
  Hypothesis properties, vectorised = scalar on 500 random events), official-points reproduction
  on committed excerpts (CI) and on all 113,870 real rows (slow), selling price table, squad
  violations, 9 formation cases, auto-subs (GK-only, bench order, formation-preserving), captain/
  vice/TC/BB, FT banking/rollover/hits, GW1 unlimited, WC/FH retention, pre-2024 reset, AFCON
  top-up, 2022-23 unlimited GW17, chip windows/expiry/re-use, FH exact reversion, plan validator;
  Hypothesis: random decision sequences preserve invariants, validator replay = direct
  application, auto-subs always legal, scoring monotone; manager-sync FT replay/FH/chips/hit-cost
  disagreement warning.

- M4: `pytest` 183 passed (+ simulation tests written for M5) — feature builder: every
  registered feature built and in range, hand-computed values, live signals only with snapshots,
  Hypothesis future-perturbation (features identical), version-lock fingerprint, registry↔docs
  sync; team strength: recovery of known ratings on a synthetic league (corr > 0.9, home
  advantage ±0.1, identified SDs < 0.12), cutoff isolation, promoted prior, regime-change
  tracking; metrics vs closed forms (CRPS of N(0,1), calibrated ECE ≈ 0, PIT uniformity);
  harness causality (training rows strictly before cutoff). Experiments: rolling-origin team
  tuning (27 configs × 3 seasons), baseline walk-forward (114 cutoffs × horizon 5).

- M5: `pytest` 217 passed — rate shrinkage recovers Poisson noise φ≈1 and CV² on synthetic
  leagues, posterior beats raw/prior vs truth; BPS map/elasticity/globals recover known
  parameters; minutes model calibrated out of sample, deterministic, chronological calibration
  slice, availability layer incl. departed players; price features invariant to future
  perturbation (Hypothesis), labels visible at cutoff, hand-computed features; end-to-end forecast
  on real excerpt: provenance complete, deterministic, exact binomial consistency of simulated
  starts; forecast-eval harness retrain schedule causal, bootstrap = direct metric; gates, PSI,
  retrain triggers; registry lifecycle on PostgreSQL (promotion, rollback, tamper detection).
  Experiments: forecast walk-forward (114 cutoffs, 397k rows, 30 min), price (76k predictions),
  rate shrinkage OOS, feature-importance stability, ensemble/conformal.

## 9. Remaining work

Everything from M1 onward (see milestone table).
