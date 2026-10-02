# ADR-0002: Monorepo layout, Python workspace and enforced layering

- Status: Accepted
- Date: 2026-10-02

## Decision

A single repository (§52) with a **uv workspace** for Python and an npm project for the web app.
Each Python component is its own distribution with a `src/` layout and explicit dependencies,
so architectural boundaries are declared in `pyproject.toml` and enforced by `import-linter`
in CI rather than by convention.

| Path | Distribution / import | Responsibility | May depend on |
|------|----------------------|----------------|---------------|
| `packages/domain` | `fpl_domain` | Entities, versioned rulesets, scoring, squad/lineup validity, transfer & chip state machine, pricing, hashing. Pure, no I/O except loading ruleset YAML. | — |
| `packages/storage` | `fpl_storage` | SQLAlchemy models (Postgres), repositories, content-addressed raw store, canonical dataset snapshots, point-in-time (PIT) views. | domain |
| `services/ingestion` | `fpl_ingestion` | Source clients (FPL API, historical dataset), raw capture, validation, canonicalisation, data-quality gates, data jobs. | domain, storage |
| `services/feature-store` | `fpl_features` | Feature registry with declared metadata, PIT-safe feature builder, leakage guard, feature snapshots. | domain, storage |
| `packages/forecasting` | `fpl_forecasting` | Team strength, minutes, event-rate, bonus and price models; baselines; calibration; evaluation; model registry. | domain, storage, features |
| `packages/simulation` | `fpl_simulation` | Joint fixture-level Monte Carlo, scoring of samples, squad/lineup evaluation over samples. | domain |
| `packages/optimizer` | `fpl_optimizer` | MILP formulation (HiGHS), exact lineup solver, brute-force verifier, solution extraction. | domain |
| `packages/decision` | `fpl_decision` | Replacement engine, hold-vs-transfer, captaincy, chips, planner, scenarios, stability, explanations, recommendation objects. | domain, optimizer, simulation, forecasting |
| `packages/backtesting` | `fpl_backtest` | Walk-forward engine, strategies/baselines, metrics, leakage audit, reports. | all of the above |
| `services/notifications` | `fpl_notifications` | Alert rules, materiality thresholds, channels. | domain, storage |
| `apps/api` | `fpl_api` | FastAPI HTTP API, auth, rate limits, caching, job submission. | everything except backtesting internals |
| `apps/worker` | `fpl_worker` | RQ worker + scheduler for ingestion, forecasts, optimisation, backtests, notifications. | everything |
| `apps/web` | Next.js + TypeScript | Operational cockpit UI. Talks only to the API via generated OpenAPI types. | API contract |

Rules enforced by `import-linter`:
1. `fpl_domain` imports no other first-party package and no infrastructure library
   (sqlalchemy, fastapi, redis, httpx).
2. `fpl_optimizer` and `fpl_simulation` depend only on `fpl_domain` (they are pure computation).
3. Nothing below `apps/*` imports `fpl_api` or `fpl_worker`.

## Rationale

- Separate distributions make dependency direction reviewable and testable.
- Pure computational cores (domain, simulation, optimizer) are unit-testable without a database.
- uv gives a single lockfile (`uv.lock`) → reproducible environments in CI and Docker.

## Consequences

- Slightly more packaging boilerplate (one `pyproject.toml` per component).
- Root `tests/` (per §52) exercises all packages; package-local tests are not used to keep one
  test tree as the spec requires.
