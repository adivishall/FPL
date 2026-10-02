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
| M2 | Data: ingestion, canonical schema, quality gates, freshness | ☐ | Reproducible historical snapshot | |
| M3 | Rules: scoring, transfers, chips, squad validity, state transitions | ☐ | Rule/property tests pass | |
| M4 | Forecasting baselines: PIT features, team strength, baselines | ☐ | Metrics reproducible | |
| M5 | ML forecasting: minutes, point distributions, calibration, registry | ☐ | Out-of-sample results improve or justify model choice | |
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

**M2 — Data** (next). M0 and M1 complete.

Environment: Python 3.12 (uv-managed; numpy 2.5 requires ≥3.12), Node 22, PostgreSQL 16
binaries, Redis 7, Docker 29, 4 CPU, 15 GB RAM. Network: historical dataset + PyPI + npm + Docker
Hub reachable; FPL API, premierleague.com, Debian mirrors and GHCR blobs blocked.

---

## 3. Specification checklist (spec → milestone)

### Data & state (§6–§8, §53–§56)
| Item | Spec | Milestone | Status |
|------|------|-----------|--------|
| Scheduled, cached bootstrap ingestion (no per-page-load fetch) | §6.1, §33 | M2/M11 | ☐ |
| GW-specific state capture (points, minutes, ownership, transfers, prices, availability) | §6.1 | M2 | ☐ |
| Fixture/team ingestion with canonical IDs across seasons | §6.1, §6 table | M2 | ☐ |
| Manager state ingestion (squad, bank, FT, chips, picks, transfers) | §6.1 | M2 | ⛔ live API blocked |
| Raw payloads stored unchanged (content-addressed) | §6.1, §55.1 | M2 | ☐ |
| External metrics with source/timestamp/confidence; source-priority layer | §6.2 | M2 | ☐ |
| Extract→validate→normalize→feature→serve→audit pipeline with failure handling | §6 table | M2 | ☐ |
| Idempotent ingestion; checksum/source metadata | §55 | M2 | ☐ |
| DQ gates: freshness, uniqueness, referential integrity, ranges, temporal ordering, completeness, drift, conflict | §55.2 | M2/M13 | ☐ |
| PIT correctness for season aggregates, fixtures, injuries, price, ownership, tuning | §7 | M2/M4 | ☐ |
| Full §54 database schema + migrations | §8, §54 | M1 | ☑ 37 tables, Alembic `0001`, drift test |
| Domain entities (Season…AuditEvent) + manager state invariants | §53 | M1/M3 | ☐ |
| ManagerState / DecisionState objects | §8 | M1/M8 | ☐ |
| Feature registry with declared metadata + DATA_DICTIONARY.md | §56 | M4 | ☐ |
| Player feature families (availability, minutes, role, set pieces, attacking, defensive, team, fixture, form w/ shrinkage, trend, economics, correlation) | §9, §56.1 | M4 | ☐ |
| Team/fixture features (recency, venue shrinkage, probabilistic fixtures, congestion, DGW/BGW, postponements) | §14, §56.2 | M4 | ☐ |

### Forecasting (§10–§14, §57–§59, §67, §71)
| Item | Spec | Milestone | Status |
|------|------|-----------|--------|
| P(start), expected minutes, sub risk, return probability | §10, §58 | M5 | ☐ |
| Decomposed points: appearance, goals, assists, CS, DC, cards, bonus, other | §11, §57.1 | M5 | ☐ |
| Price-change model | §57.1, §23, §66 | M5 | ☐ |
| Monte Carlo point distributions with correlation | §12, §59 | M5 | ☐ |
| Recency weighting, Bayesian updating, hierarchical priors | §13 | M4/M5 | ☐ |
| Calibration (reliability, Brier, ECE), conformal intervals, ensemble, drift | §13, §57.2, §71 | M5/M13 | ☐ |
| Baselines compared; temporal splits; MAE/RMSE/log loss/Brier/CRPS | §57.2 | M4/M5 | ☐ |
| Model registry, promotion gates, rollback, seeds | §71 | M5 | ☐ |
| Independent probabilistic fixture layer (team xG, CS prob, attack/defence indices, swings, DGW load, BGW risk, rotation pairing) | §14 | M4/M7 | ☐ |
| News/injury signals → start probability adjustments | §67 | M5 | ☐ |

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

- Docker builds inside this sandbox need the proxy CA passed as a BuildKit secret (documented in
  `infra/docker/README.md`); not needed in CI.

- `fantasy.premierleague.com` and `premierleague.com` are denied by the build environment's egress
  policy. Live sync and live-rule verification cannot be exercised here.

## 7. Files changed (by milestone)

- M0: `docs/BUILD_STATUS.md`, `docs/decisions/ADR-0001…0010`, `docs/architecture/source-inventory.md`.
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

## 9. Remaining work

Everything from M1 onward (see milestone table).
