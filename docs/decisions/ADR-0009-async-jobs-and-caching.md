# ADR-0009: Asynchronous jobs, scheduling and caching

- Status: Accepted
- Date: 2026-10-02

## Decision

- **Redis** for cache and queue; **RQ** workers execute expensive jobs (ingestion, feature
  builds, forecast generation, deep decisions, backtests, notifications). A lightweight
  scheduler process enqueues periodic jobs from `config/schedules.yaml`.
- The API never computes forecasts on page load (§33, §80): it reads stored predictions keyed by
  `(feature_snapshot_id, model_version)` and returns a freshness block with every response.
- Requests whose estimated cost exceeds a threshold (multi-GW deep decision, robust analysis,
  backtests) return `202 Accepted` with a job id; clients poll `GET /api/v1/jobs/{id}`.
- Cache keys: forecasts by `(player, horizon, feature_snapshot_id, model_version)`; optimisation
  results by `(manager_state_hash, objective_config_hash, data_snapshot_id, ruleset_hash)`.
  A new snapshot/model/ruleset changes the key, which is the invalidation mechanism.
- Optimisation runs are **immutable records** so users can revisit an exact decision later.
- A synchronous in-process job backend exists for tests and single-process development; it uses
  the same job functions.

## Consequences

Redis becomes a runtime dependency in production; the API degrades to synchronous execution with
lower Monte Carlo limits if Redis is unavailable (reported in `/health`).
