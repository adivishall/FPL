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
| `caddy` 2.10 (public overlay only) | upstream, pinned by digest | `caddy run` | the only public listener: TLS (automatic Let's Encrypt certificates), HTTP→HTTPS, site login, security headers; proxies to `web` (see *Public deployment*) |

All Python processes share the `/data` volume: `snapshots/` (canonical Parquet snapshots, each
verified against its manifest hash on load), `raw/` (content-addressed raw captures),
`artifacts/` (forecasts and price models keyed by snapshot + model config), `feature-store/`.
The API re-checks `/data/snapshots` every `FPL_SNAPSHOT_CHECK_SECONDS` and swaps in a newer
snapshot without a restart — but only once the worker has precomputed that snapshot's serving
forecast (marker `/data/artifacts/serving/<snapshot>.ready`), so a live refresh never opens a
"forecast not ready" window. Each `live_refresh` exports a snapshot and immediately submits its
forecast; the swap follows within the check interval. On a first deployment no snapshot is ready
yet and forecast routes answer `503` with `Retry-After` until the first forecast completes.

Completed-match results come from the scheduler's `live_results` task: when a finished (or
provisionally finished) gameweek has no results, and daily during the 4 days after a gameweek's
last kickoff, it reads every player's `element-summary` history (one paced request per player,
~11 minutes), loads the results tables only, then exports a snapshot and submits its forecast.
It is otherwise skipped ("results up to date"). The scheduler runs it inline (its heartbeat is
kept alive), so its other tasks wait for it once per gameweek. Run it by hand with
`docker compose exec scheduler fpl-worker run-once live_results` (same checks and steps);
`fpl-ingest live-results --season <season>` forces a capture, which the next hourly refresh
exports.

Resources (measured, `ml/reports/performance.md`): the worker computing the 8-gameweek,
1,000-sample serving forecast peaks at ~2 GB RSS (container); give it ≥ 3 GB. Recommendation
jobs solve independent MILPs (stability perturbations, chip weeks) in `FPL_SOLVER_WORKERS`
spawned processes (default 2; set it to the worker's vCPUs — 4 here; up to ~250 MB each); the
output is identical for any value (`ml/reports/performance_stages.md`). The API keeps 1, so
synchronous requests never spawn processes. The images run
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
7. Full production-topology browser suite (18 flows: proxy-only traffic, key enforcement, squad,
   captain/bench, worker jobs, alerts, settings incl. the load race, traceability, export/delete,
   Backtest Lab, error states, rate limiting):

   ```bash
   cd apps/web && E2E_BASE_URL=http://127.0.0.1:3000 E2E_API_URL=http://127.0.0.1:8000 \
     E2E_OPS_KEY=<operator key> E2E_WEB_KEY=<FPL_WEB_API_KEY> \
     npx playwright test -c playwright.prod.config.ts
   ```

Every service listens on loopback or the Compose network only. To serve the internet, add the
TLS proxy — next section.

## Public deployment (TLS)

```
Internet ──80/443──▶ caddy  TLS (Let's Encrypt) · HTTP→HTTPS · site login · security headers
                       └──▶ web:3000  Next.js; /backend/* → API with the server-held key
                              └──▶ api:8000 ──▶ postgres · redis ◀── worker · scheduler
```

`docker-compose.public.yml` adds one service, `caddy` (`infra/deployment/caddy/Caddyfile`), the
only one publishing ports (80, 443 TCP/UDP). The API, database, Redis and metrics stay off the
internet; operators reach the API through SSH (`ssh -L 8000:127.0.0.1:8000 <host>`).

**Why a site login.** V1 is single-tenant: the web server calls the API with one key on behalf of
whoever reaches it, and a manager key is an identifier, not a credential. Exposed without a gate,
anyone could start expensive jobs or read and erase data under a guessed manager key. Caddy
therefore requires one login (HTTP Basic over TLS, bcrypt-hashed) for the whole site and refuses
to start without `infra/deployment/caddy/auth.caddy`. Per-user accounts are V2.

**Logs.** Caddy keeps no access log, and its error log drops request URIs (manager keys appear in
paths and queries) and authorisation headers.

**Host.** One small VM: 2 vCPUs minimum, 4 recommended (`FPL_SOLVER_WORKERS` = vCPUs); 4 GB RAM
minimum, 8 GB recommended (serving forecast ~2 GB peak in the worker); 30 GB disk; Docker Engine
with Compose v2. Inbound 80/443 (and SSH); outbound `fantasy.premierleague.com`,
`raw.githubusercontent.com`, the image registries and Let's Encrypt.

**Steps.**

1. Point an A (and AAAA) record for the site name at the VM; open 80/tcp, 443/tcp, 443/udp.
2. On the VM: check out the release, `cp .env.example .env` and fill it in (*First deployment*
   step 1), plus `FPL_PUBLIC_DOMAIN=<site name>`, `FPL_TLS=<ACME e-mail address>` and
   `FPL_SOLVER_WORKERS=<vCPUs>`.
3. Site login: `docker run --rm caddy:2.10-alpine caddy hash-password --plaintext '<password>'`,
   then create `infra/deployment/caddy/auth.caddy` from `auth.caddy.example` with that hash
   (`chmod 600`; git-ignored).
4. *First deployment* steps 2–4, then
   `docker compose -f docker-compose.yml -f docker-compose.public.yml up -d`.
5. Verify from anywhere:
   `FPL_SMOKE_WEB_AUTH=<user>:<password> uv run python infra/scripts/smoke.py --web https://<site>`
   (TLS, HSTS, HTTP→HTTPS redirect, then health, gameweek, players and models through the proxy),
   and the browser suite with `E2E_BASE_URL=https://<site> E2E_HTTP_USER=… E2E_HTTP_PASSWORD=…`
   (`E2E_API_URL` through the SSH tunnel).
6. Schedule `infra/scripts/backup.sh` daily and copy the backups off the host (*Backup and
   restore*).

**Verified on the development machine (2026-10-07)** with `FPL_PUBLIC_DOMAIN=fpl.localhost` and
`FPL_TLS=internal` (Caddy's own CA): certificate chain valid against Caddy's root, TLS 1.3, TLS 1.1
refused; HTTP answered `308` to the same path and query over HTTPS; `401` without or with a wrong
login; HSTS, `nosniff`, `X-Frame-Options: DENY`, referrer and permissions policies, no `Server` or
`X-Powered-By`; API, web, Postgres and Redis bound to loopback only; the production browser
suite 19/19 through Caddy with TLS and the login; the smoke test 5/5 through the
public origin; a failed request with a manager key in its path and query left no trace of it in
Caddy's logs (it did before the log filter). **Not verified:** issuance of a publicly trusted
certificate and a reachable public URL — they need the operator inputs below.

**Operator inputs still needed for a public URL.**

| Input | Used for |
|---|---|
| A VM or container host (size above) with SSH access | running the stack |
| A domain name and access to its DNS | `FPL_PUBLIC_DOMAIN`, the certificate |
| An e-mail address for Let's Encrypt | `FPL_TLS` (expiry notices) |
| A site login password, chosen by you | `auth.caddy` (only its bcrypt hash is stored) |
| Production secrets, generated on the host | `POSTGRES_PASSWORD`, the API key and its hash (never committed) |
| Optional: an HTTPS endpoint you control | verifying webhook delivery (`FPL_WEBHOOK_ALLOWED_HOSTS`) |
| Optional: off-host backup storage | copies of `infra/scripts/backup.sh` output |

## Network policy

Live data comes from `fantasy.premierleague.com` (the only host in
`config/sources.yaml → fpl_api.allowed_hosts`). Historical data comes from the pinned commit of
the vaastav repository on `raw.githubusercontent.com`. Where egress to the live host is not
permitted, set `FPL_LIVE_SYNC_ENABLED=false`: the system runs in **degraded mode** — it serves
the last validated snapshot, every response's `freshness` block says so, recommendations list it
under assumptions, and `/squad/sync` answers `503` with guidance to enter the squad manually.

## Upgrades and rollback

* **Release**: push a `v*` tag → `.github/workflows/deploy.yml` builds and publishes both images to
  GHCR (with SBOM and provenance) and smoke-tests the environment whose `API_URL` or `WEB_URL` is
  configured (`SMOKE_WEB_AUTH` secret for the site login). Roll out with `docker compose pull &&
  docker compose run --rm migrate && docker compose up -d` (add `-f docker-compose.yml -f
  docker-compose.public.yml` on a public host). Take a backup first.
* **Database**: migrations are validated upgrade → downgrade → upgrade in CI. To roll back the
  schema: `docker compose run --rm migrate alembic downgrade -1` with the previous image.
* **Images**: pin the previous tag in `docker-compose.override.yml` and `up -d`.
* **Models**: forecasts are keyed by model-config hash; reverting `config/models/*.yaml`
  restores the previous model on the next forecast. Registered artifacts roll back with
  `ModelRegistry.rollback` (`fpl_storage.registry`), which re-promotes the previous version.
* **Data**: snapshots are immutable and content-addressed; pin one with `FPL_SNAPSHOT_DIR`.

## Retention

Every live refresh exports a ~3 MB snapshot and, once a forecast is computed for it, a ~30 MB
feature cache and a ~7 MB forecast — about 1 GB a day unpruned (measured on the local stack).
The scheduler submits a `retention` job hourly (`fpl_api/retention.py`):

* kept with caches: the `FPL_SNAPSHOT_RETENTION_KEEP` newest snapshots (default 24), the one the
  API serves, and every evaluation snapshot pinned in `config/backtest/*.yaml`;
* kept without caches: any other snapshot a stored recommendation, backtest run or registered
  model references (its decisions stay traceable and re-derivable; caches are recomputable);
* deleted: all other snapshots with their feature caches and serving markers, forecast/price
  caches unused by a kept serving forecast and older than `FPL_ARTIFACT_RETENTION_HOURS`
  (default 24), exports interrupted before their manifest and temporary files of killed writers.

Database rows are never deleted. A path that cannot be deleted fails the job after the rest is
done (`failure=error`, `JobFailures` alert); `0` disables pruning. Not pruned: `/data/raw` (the
captured source payloads that canonical rows cite as provenance, ~0.3 MB per hourly capture) and
the PostgreSQL tables of live observations — both grow slowly and need an archival policy before
multi-year operation (`docs/KNOWN_LIMITATIONS.md`).

## Backup and restore

**What is backed up** (`infra/scripts/backup.sh`): the whole PostgreSQL database (`pg_dump`
custom format, a consistent snapshot while the stack runs) — manager states, settings,
recommendations and their optimisation runs, journal, notifications, jobs, audit log, lineage
(raw-capture records, data jobs, revisions), canonical data and the model registry — and, from
`/data`, `snapshots/` (incl. the pinned evaluation snapshot and every snapshot a stored
recommendation cites) and `raw/` (the source captures canonical rows cite).

**Not backed up, by design:** forecasts, price models and feature caches (recomputed from the
snapshots; the serving forecast takes ~1–2 minutes); the Redis job queue (jobs queued or running
at backup time are closed as abandoned after a restore, and the scheduler resubmits periodic
work); secrets (`.env`, `auth.caddy` — keep them in a secret manager); TLS certificates
(re-issued automatically); Prometheus history.

```bash
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.public.yml" \
  infra/scripts/backup.sh /srv/fpl-backups              # → /srv/fpl-backups/<UTC timestamp>/
# on a new host (or fresh volumes), with .env and auth.caddy in place:
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.public.yml" \
  infra/scripts/restore.sh /srv/fpl-backups/<UTC timestamp>
```

`backup.sh` writes `db.dump`, its table of contents read back through `pg_restore` (it fails if the
dump has no table data), `data.tar.gz`, `SHA256SUMS` and a manifest (schema version, sizes).
`restore.sh` verifies the checksums, restores the database while nothing writes, applies
migrations (a no-op at the same version), unpacks `/data`, starts the stack and submits the
serving forecast.

**Recovery test (2026-10-07, development machine).** Realistic state — live 2026-27 data GW1–GW5,
a manager with a squad, settings and a worker-generated recommendation — was backed up with the
writers paused (16.0 MB dump with 38 tables of data; 82.5 MB data archive) and restored into a
new Compose project with empty volumes, the full public topology including Caddy, in 78 s:

* restored into a scratch database, every table's exact row count, the schema version and the
  latest job timestamp equal the source at backup time (40 of 40 lines);
* the manager's squad, settings, recommendation id, decision and optimality are identical, and
  the recommendation's trace chain is complete;
* the worker computed the serving forecast and the API reported `ok`, not degraded; a new
  recommendation job and an alert evaluation succeeded, the new recommendation is traceable, and
  the restored scheduler captured live data on its own;
* the site answered over TLS with a newly issued certificate, behind the login; smoke test 5/5.

Not tested: a restore onto a different machine or across an image upgrade, a database large
enough for dump time to matter, and off-host copies (the operator's tooling). Recovery point: the
last backup (daily → up to a day of manager data; live data is re-captured automatically).
Recovery time measured here: ~1.5 minutes to a running stack, plus the serving forecast.

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
  `queued`), `fpl_jobs_failed{kind,failure}` (from the database, so failures closed by the
  scheduler's reaper count too) and the per-process `fpl_job_failures_total{kind,failure}`;
  `failure` is `error` (the job's code raised) or `interrupted` (its worker was killed, lost or
  timed out).
* Freshness and models: `fpl_data_freshness_hours{source}`, `fpl_model_metric{model,metric}`
  (published gate metrics: PIT coverage, ECE, CRPS, log loss …).
* Security: `fpl_security_events_total{kind}`.

Logs are structured JSON (structlog) with request ids. Security events are logged without key
material. Prometheus loads rule files at start-up: restart it after changing `alerts.yml`
(`docker compose --profile monitoring restart prometheus`).

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
"match results missing for finished GW…" means the `live_results` capture has not completed:
check the `data_jobs` rows of type `live_results` and the scheduler log.

### Abandoned jobs

`JobInterruptions`. A job whose worker died (OOM kill, container replaced, host asleep long
enough for RQ's timeout to fire on wake) is closed by the scheduler's reaper as
`failed: abandoned …` — immediately when RQ reports the job failed or unknown, otherwise when its
lease expires (forecast/recommendation 30 min, alerts/retention 15 min, backtest 6 h) — and an
identical request then starts a new attempt. RQ's own job timeout is recorded as
`interrupted: …`. All of these have `failure=interrupted` in `GET /jobs/{id}` and in
`fpl_jobs_failed`; every job publishes its result only at the end (one database transaction,
atomic file renames), so an interrupted job leaves no partial state and re-running it is safe.
The reaper is idempotent (it only closes `queued`/`running` rows) and logs `job_interrupted` per
job. Failed jobs are never retried automatically except by the scheduler's next bucket.

### Queue backlog

`JobQueueBacklog`. Workers are down or saturated: `docker compose ps worker`, scale with
`docker compose up -d --scale worker=3`. Jobs are de-duplicated by request hash, so retries and
duplicate submissions do not multiply work.

### Job failures

`JobFailures` (`failure=error`) / `RecommendationFailures`. The job's own code raised:
`GET /api/v1/jobs/{id}` shows the error and a short traceback. Typical causes: a manager squad that is no longer legal under the season's ruleset
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
