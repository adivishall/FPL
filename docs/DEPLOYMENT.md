# Deployment and operations runbook

This covers the production topology, first deployment, upgrades and rollback, configuration,
security, observability and what to do when each alert fires. The stack runs anywhere Docker
Compose (or an equivalent orchestrator running the same images) is available.

## Topology

| Process | Image | Command | Notes |
|---|---|---|---|
| `api` | `infra/docker/python.Dockerfile` (`fpl-engine`) | `uvicorn fpl_api.main:app` | stateless; serves precomputed forecasts (`FPL_FORECAST_ON_DEMAND=false`) and promotes a snapshot only once its forecast exists (`FPL_SERVE_READY_SNAPSHOTS_ONLY=true`); health: HTTP `/api/v1/health` |
| `worker` | same image | `fpl-worker work` | RQ consumer: forecasts, recommendations, alert evaluations, backtests; health: its own RQ heartbeat (`fpl-worker healthcheck work`) |
| `scheduler` | same image | `fpl-worker schedule` | submits idempotent tasks from `config/schedules/default.yaml`, reaps abandoned jobs; health: tick heartbeat (`fpl-worker healthcheck schedule`) |
| `web` | `infra/docker/web.Dockerfile` | `node server.js` | Next.js; proxies `/backend/*` to the API with the key held server-side |
| `migrate` | python image | `alembic upgrade head` | one-shot before api/worker start |
| `postgres` 16, `redis` 7 | upstream | — | application state; job queue |

All Python processes share the `/data` volume: `snapshots/` (canonical Parquet snapshots, each
verified against its manifest hash on load), `raw/` (content-addressed raw captures),
`artifacts/` (forecasts and price models keyed by snapshot + model config), `feature-store/`.
The API re-checks `/data/snapshots` every `FPL_SNAPSHOT_CHECK_SECONDS` and swaps in a newer
snapshot without a restart — but only once the worker has precomputed that snapshot's serving
forecast (marker `/data/artifacts/serving/<snapshot>.ready`), so a live refresh never opens a
"forecast not ready" window. Each `live_refresh` exports a snapshot and immediately submits its
forecast; the swap follows within the check interval. On a first deployment no snapshot is ready
yet and forecast routes answer `503` with `Retry-After` until the first forecast completes.

Resources (measured, `ml/reports/performance.md`): the worker computing the 8-gameweek,
1,000-sample serving forecast peaks at ~2 GB RSS (container); give it ≥ 3 GB. The images run
as the non-root `app` user and the image pre-creates `/data` owned by `app`, so an empty named
volume is writable on first use.

## Process configuration beyond `.env`

| Variable | Process | Meaning |
|---|---|---|
| `FPL_FORECAST_ON_DEMAND=false` | api | never train/simulate on a request; serve precomputed forecasts |
| `FPL_SERVE_READY_SNAPSHOTS_ONLY=true` | api | promote a newer snapshot only once its serving forecast exists |
| `FPL_SNAPSHOT_CHECK_SECONDS` (300) | api, worker | how often to look for a newer snapshot |
| `PROMETHEUS_MULTIPROC_DIR`, `FPL_WORKER_METRICS_PORT` (9101) | worker | aggregate metrics of forked job processes and serve them |
| `FPL_SCHEDULER_HEARTBEAT` (temp dir) | scheduler | liveness marker read by `fpl-worker healthcheck schedule` |

All other settings are `fpl_api.settings.Settings` fields with the `FPL_` prefix.

## First deployment

1. `cp .env.example .env` and fill it in. Generate keys with `docker compose run --rm api
   fpl-api create-key` — put the **hash** in `FPL_API_KEYS_SHA256` (JSON list) and the key itself
   in your secret manager and in `FPL_WEB_API_KEY` for the web proxy. Keys are never stored in
   plaintext anywhere in the system.
2. `docker compose up -d postgres redis && docker compose run --rm migrate`.
3. Seed data (pinned historical source, ADR-0003):
   `docker compose run --rm scheduler fpl-ingest historical --all` then
   `docker compose run --rm scheduler fpl-ingest export-snapshot`.
4. Precompute the serving forecast: `docker compose run --rm scheduler fpl-worker run-once
   forecast_precompute` (submits the job; the worker computes it — about 1–3 minutes, see
   `ml/reports/performance.md`).
5. `docker compose up -d` (api, worker, scheduler, web).
6. `uv run python infra/scripts/smoke.py --api http://localhost:8000 --web http://localhost:3000`.
7. Full production-topology browser suite (15 flows: proxy-only traffic, key enforcement, worker
   jobs, alerts, settings, traceability, export/delete, error states, rate limiting):

   ```bash
   cd apps/web && E2E_BASE_URL=http://127.0.0.1:3000 E2E_API_URL=http://127.0.0.1:8000 \
     E2E_OPS_KEY=<operator key> E2E_WEB_KEY=<FPL_WEB_API_KEY> \
     npx playwright test -c playwright.prod.config.ts
   ```

Terminate TLS in front of `api` and `web` (reverse proxy or load balancer); both listen on
loopback in the compose file. Set `FPL_CORS_ORIGINS` to the web origin when the browser calls
the API directly; in the default proxy mode the browser only talks to the web origin.

## Network policy

Live data comes from `fantasy.premierleague.com` (the only host in
`config/sources.yaml → fpl_api.allowed_hosts`). Historical data comes from the pinned commit of
the vaastav repository on `raw.githubusercontent.com`. Where egress to the live host is not
permitted, set `FPL_LIVE_SYNC_ENABLED=false`: the system runs in **degraded mode** — it serves
the last validated snapshot, every response's `freshness` block says so, recommendations list it
under assumptions, and `/squad/sync` answers `503` with guidance to enter the squad manually.

## Upgrades and rollback

* **Release**: push a `v*` tag → `.github/workflows/deploy.yml` builds and publishes both images to
  GHCR (with SBOM and provenance) and smoke-tests the environment whose `API_URL` is configured.
  Roll out with `docker compose pull && docker compose run --rm migrate && docker compose up -d`.
* **Database**: migrations are validated upgrade → downgrade → upgrade in CI. To roll back the
  schema: `docker compose run --rm migrate alembic downgrade -1` with the previous image.
* **Images**: pin the previous tag in `docker-compose.override.yml` and `up -d`.
* **Models**: forecasts are keyed by model-config hash; reverting `config/models/*.yaml`
  restores the previous model on the next forecast. Registered artifacts roll back with
  `ModelRegistry.rollback` (`fpl_storage.registry`), which re-promotes the previous version.
* **Data**: snapshots are immutable and content-addressed; pin one with `FPL_SNAPSHOT_DIR`.

## Retention

Every live refresh exports a ~3 MB snapshot (hourly by default: ~70 MB/day). There is no automatic
retention yet: prune old `snap_*` directories (keep the pinned evaluation snapshot and the one
being served) and their `/data/artifacts/forecasts` entries periodically.

## Backups

`pg_dump` the `fpl` database daily (application state: manager states, recommendations,
journal, lineage, notifications, audit log). Snapshot the `/data` volume after each
`export-snapshot`; raw captures and snapshots are reproducible from the pinned source, forecasts
from snapshots + config, so `/data` loss costs recomputation time, not information.

## Privacy operations (§75)

* Export: `GET /api/v1/managers/{key}/export` (API key required) returns every manager-linked
  row. Erase: `DELETE /api/v1/managers/{key}` deletes them in one transaction.
* Both are recorded in `audit_log` with a SHA-256 of the key only.
* No FPL credentials are accepted or stored anywhere; sync uses the public entry id.

## Observability

Prometheus scrapes `api:8000/metrics` (`infra/deployment/prometheus/prometheus.yml`); rules are
in `infra/deployment/prometheus/alerts.yml`. Exposed series:

* Requests: `fpl_http_requests_total{method,route,status}`, `fpl_http_request_seconds` (route
  templates only).
* Forecasts: `fpl_forecast_cache_total{result=memory|disk|miss}`, `fpl_forecast_seconds`,
  `fpl_forecast_failures_total`.
* Optimisation: `fpl_optimization_seconds{stage}` and `fpl_optimization_status_total{status}`
  (validator verdict on the chosen plan).
* Outcomes and queue: `fpl_recommendations_total{outcome}`, `fpl_jobs{status}` (queue depth =
  `queued`), `fpl_job_failures_total{kind}`.
* Freshness and models: `fpl_data_freshness_hours{source}`, `fpl_model_metric{model,metric}`
  (published gate metrics: PIT coverage, ECE, CRPS, log loss …).
* Security: `fpl_security_events_total{kind}`.

Logs are structured JSON (structlog) with request ids. Security events are logged without key
material.

## Alert runbook

### API errors

`ApiHighErrorRate`. Check `GET /api/v1/health` (database, dataset, jobs). 503 with
`forecast not ready` means the serving forecast is missing — see *Forecast failures*. 503
`database not configured` means `FPL_DATABASE_URL` is unset or Postgres is down. Inspect logs by
`request_id`.

### Latency

`ApiLatencyP95High`. Interactive routes are served from cached forecasts; a slow p95 usually
means the forecast cache is cold (`fpl_forecast_cache_total{result="miss"}` rising) or CPU is
saturated by a co-located worker. Move workers to separate hosts or reduce `FPL_N_SIMS`.

### Stale data

`DataStale` / `DataExpired`. The live capture is failing (network policy, upstream outage) or the
scheduler is down. Check `fpl-worker run-once live_refresh` output. The product keeps serving
the last snapshot in degraded mode; no action can make data fresher than the source.

### Abandoned jobs

A job whose worker died (OOM kill, container replaced) is closed by the scheduler's reaper as
`failed: abandoned …` — immediately when RQ reports the job failed or unknown, otherwise when its
lease expires (forecast/recommendation 30 min, alerts 15 min, backtest 6 h) — and an identical
request then starts a new attempt. Failed jobs are never retried automatically except by the
scheduler's next bucket. `fpl_job_failures_total{kind}` counts them.

### Queue backlog

`JobQueueBacklog`. Workers are down or saturated: `docker compose ps worker`, scale with
`docker compose up -d --scale worker=3`. Jobs are de-duplicated by request hash, so retries and
duplicate submissions do not multiply work.

### Job failures

`JobFailures` / `RecommendationFailures`. `GET /api/v1/jobs/{id}` shows the error and a short
traceback. Typical causes: a manager squad that is no longer legal under the season's ruleset
(re-sync or re-enter), or a missing forecast for a new snapshot.

### Forecast failures

`ForecastFailures`. Run `fpl-worker run-once forecast_precompute` and read the worker log. A
failing forecast usually means a snapshot violated a data contract upstream — check
`GET /api/v1/data-quality`; pin the last good snapshot with `FPL_SNAPSHOT_DIR` meanwhile.

### Invalid plans

`InvalidOptimiserPlans`. The independent validator (replay through the domain state machine)
rejected a chosen plan. This must never happen; capture the recommendation id, its trace
(`/recommendations/{id}/trace`) and the optimisation run, and open an incident — the plan was
stored but flagged `valid=false`.

### Model drift

`ForecastCalibrationDrift`. Published calibration fell outside the promotion band. Re-run
`ml/experiments/forecast_eval.py` on the newest data; the retraining policy
(`config/models/points.yaml → retraining`) and PSI drift checks decide whether to retrain or
roll back (see *Upgrades and rollback → Models*).

### Security events

`SecurityEventsSpike`. Inspect `security_event` log lines (kind, path, client id prefix — never
keys). Repeated `auth_invalid` from one client: block at the edge. `webhook_rejected`: a manager
configured a webhook outside `FPL_WEBHOOK_ALLOWED_HOSTS` (SSRF guard working as intended).
