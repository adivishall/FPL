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

1. `cp .env.example .env` and fill it in. Generate **two** keys with `docker compose run --rm api
   fpl-api create-key`: an operator key (its hash in `FPL_API_KEYS_SHA256`, the key in your secret
   manager — full access, for operations and smoke tests) and the web proxy's key (its hash in
   `FPL_WEB_API_KEYS_SHA256`, the key in `FPL_WEB_API_KEY`). The web key alone reaches only public
   reference data; manager data needs a signed-in user (*Accounts and beta access* below). Keys
   are never stored in plaintext anywhere in the system.
2. `docker compose up -d postgres redis && docker compose run --rm migrate`.
3. Seed data (pinned historical source, ADR-0003):
   `docker compose run --rm scheduler fpl-ingest historical --all` then
   `docker compose run --rm scheduler fpl-ingest export-snapshot`.
4. Precompute the serving forecast: `docker compose run --rm scheduler fpl-worker run-once
   forecast_precompute` (submits the job; the worker computes it — about 1–3 minutes, see
   `ml/reports/performance.md`).
5. `docker compose up -d` (api, worker, scheduler, web).
6. `uv run python infra/scripts/smoke.py --api http://localhost:8000 --web http://localhost:3000`.
7. Full production-topology browser suite (22 flows: a signed-in beta user registered through
   the web origin, proxy-only traffic, key and session enforcement, FPL-ID onboarding when
   `E2E_FPL_ENTRY_ID` names a public entry, the player picker, squad, captain/bench, worker jobs,
   the Transfer Workbench, the result lifecycle after a squad change, alerts, settings incl. the
   load race, traceability, export/delete, Backtest Lab, error states, rate limiting):

   ```bash
   cd apps/web && E2E_BASE_URL=http://127.0.0.1:3000 E2E_API_URL=http://127.0.0.1:8000 \
     E2E_OPS_KEY=<operator key> E2E_WEB_KEY=<FPL_WEB_API_KEY> [E2E_FPL_ENTRY_ID=<public entry id>] \
     npx playwright test -c playwright.prod.config.ts
   ```
8. Invite the first users: `docker compose run --rm api fpl-api create-invite --label <who>
   --days 14` prints a code once; they register at `/register`.

Every service listens on loopback or the Compose network only. To serve the internet, add the
TLS proxy — next section.

## Accounts and beta access (M1.1a)

The product is an **invite-only beta**. Accounts, sessions and ownership live in the API
(`fpl_api.accounts`); the browser never holds a credential other than its own session cookie.

* **Principals.** An *operator* key (`FPL_API_KEYS_SHA256`) has full access. The *web* key
  (`FPL_WEB_API_KEYS_SHA256`, the one the Next server holds) reaches public reference data only;
  a *user* is a session token (`Authorization: Bearer`, issued at login) and may touch only the
  manager keys it owns. Every route that names a manager key (path, query or body) is checked
  server-side; routes addressed by record id (recommendation, trace, feedback, job) check the
  record's manager. Operators keep access to everything for operations and tests.
* **Invitations.** `fpl-api create-invite --label <who> --days 14` (or `POST /api/v1/auth/invites`
  with an operator key) prints a code once; only its SHA-256 is stored; a code admits one account
  and expires.
* **Registration and login** (`/register`, `/login`): e-mail + password (10+ characters, scrypt
  hash with a per-user salt, standard library); a 256-bit session token stored hashed, 30 days
  (`FPL_SESSION_TTL_DAYS`), kept by the Next server in an HttpOnly, SameSite=Lax (Secure over
  HTTPS) cookie and forwarded to the API as a bearer. Sign-out revokes the session. Failed logins
  are security events (`login_failed`) and are rate-limited per client; there is no lockout and no
  password reset yet (an operator can issue a new invitation).
* **Manager keys** are created by the API for the signed-in user (`POST /api/v1/auth/managers`;
  onboarding does this) — a user can never name a key the account does not own. Erasing a
  manager releases its key; `DELETE /api/v1/auth/me` erases the account and every manager it owns.
* **The site login at the TLS proxy** (`auth.caddy`) is still supported as an outer gate for a
  closed beta, but is no longer the authentication: user accounts are.

### Product analytics (first-party, opt-out)

`POST /api/v1/events` stores a small allow-listed set of named events (`fpl_api.analytics`:
onboarding started/completed, import succeeded/failed, squad analysis viewed, recommendation
opened, transfer comparison completed, recommendation followed/dismissed, return visit) with a
few allow-listed scalar properties, keyed by the user — never a manager key, never free text or
credentials. Users opt out in Settings (`analytics_opt_out`; the API then drops their events too).
Retention is 90 days: the hourly retention job deletes older rows (`deleted_events` in its
report). Operators read counts with `GET /api/v1/events/summary?days=28`. Account deletion removes
the user's events.

### Copilot Home analysis

`GET /api/v1/copilot/home` serves a precomputed, versioned squad analysis (`squad_analyses`,
keyed by manager, squad state, data snapshot, forecast and analysis version). The worker computes
it (`squad_analysis` job, ~2–3 s) after every forecast precompute for every stored squad and on
each squad save or import; the page never waits for it — while a newer one is pending, the last
one is served marked `stale`/`pending`. It reads the serving forecast only (no solver, no
simulation), so it cannot disagree with a recommendation from the same inputs.

## Public deployment (TLS)

```
Internet ──80/443──▶ caddy  TLS (Let's Encrypt) · HTTP→HTTPS · site login · security headers
                       └──▶ web:3000  Next.js; /backend/* → API with the server-held key
                              └──▶ api:8000 ──▶ postgres · redis ◀── worker · scheduler
```

`docker-compose.public.yml` adds one service, `caddy` (`infra/deployment/caddy/Caddyfile`), the
only one publishing ports (80, 443 TCP/UDP). The API, database, Redis and metrics stay off the
internet; operators reach the API through SSH (`ssh -L 8000:127.0.0.1:8000 <host>`).

**The outer site login is optional.** Before M1.1a the web server acted for whoever reached
it, so Caddy gated the whole site with one HTTP Basic login. Since M1.1a the invite-only
accounts are the authentication (every manager-scoped route checks the signed-in owner; sign-up
needs an invitation; failed logins are rate-limited), so the outer gate is a choice: keep it for
a closed test, or turn it off for beta users, who then see only the product's own sign-in.
`infra/deployment/caddy/auth.caddy` must exist either way (Caddy refuses to start without it):
with a `basic_auth` block the gate is on; **an empty file turns it off** (validated with
`caddy validate`, 2026-10-16). The smoke test and the browser suite take the login when
`FPL_SMOKE_WEB_AUTH` / `E2E_HTTP_USER` are set and run without it otherwise.

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
3. Site login (optional, above): `docker run --rm caddy:2.10-alpine caddy hash-password
   --plaintext '<password>'`, then create `infra/deployment/caddy/auth.caddy` from
   `auth.caddy.example` with that hash — or create it empty to run without the outer gate
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

**All of the above as one script.** `infra/scripts/host_setup.sh` executes steps 2–6 and the
*First deployment* steps on a fresh Ubuntu 24.04 host, idempotently (a re-run skips what is
done): Docker Engine and Compose from Docker's apt repository, the repository at a given commit
under `/srv/fpl`, `.env` from the example with a generated database password, the images built
with the revision recorded (`FPL_BUILD_REVISION`, `FPL_IMAGE_TAG`), the two API keys (`fpl-api
create-key`; the operator key is written once to `/srv/fpl-secrets/operator_key`, root-only,
and printed nowhere), migrations, the pinned historical data, a snapshot, the serving forecast,
the stack behind Caddy, a daily backup in `/etc/cron.d/fpl-backup` (14 days kept under
`/srv/fpl-backups`) and `ufw` (22, 80, 443). Inputs are environment variables:
`FPL_PUBLIC_DOMAIN`, `FPL_TLS`, `FPL_GIT_REF`, `FPL_PRODUCT_NAME` (below) and, to keep the
outer gate, `FPL_SITE_LOGIN_PASSWORD`. It never prints a secret. Operators then reach the API
through `ssh -L 8000:127.0.0.1:8000 <host>` with the key in `/srv/fpl-secrets/operator_key`.

**Product name.** The web UI shows `FPL_PRODUCT_NAME` (brand, page titles, sign-in; empty
means "FPL Copilot") — read by the web server on every request, so renaming the product is a
change of `.env` and `docker compose up -d web`, no rebuild. The browser extension is built
with the same name (`apps/extension/build.sh --name`). The word *Copilot* elsewhere in the UI
names the advisor inside the product (what it recommended), not the product.

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

## Browser extension

`apps/extension/` is a Chrome (Manifest V3) extension with one job: on a public team page of
fantasy.premierleague.com (an address containing `/entry/<ID>/`) its button opens
`https://<site>/onboarding?entry=<ID>`; elsewhere it opens the site. Its only permission is
`activeTab` (the clicked tab's address, per click); it has no host permissions, content
scripts, background worker, storage or credentials, and sign-in and the team confirmation
happen in the web app as for any visitor. Build the release with
`apps/extension/build.sh --url https://<site> --name "<product name>"`: `dist/unpacked/` for
Chrome's *Load unpacked* and a reproducible ZIP with its SHA-256 for the Chrome Web Store.
Tests: `npm test` (unit, incl. build reproducibility) and `npm run test:browser` (the built
extension in Chromium against a stand-in web app). Listing text, permission justification and
the privacy disclosure: `apps/extension/STORE_LISTING.md`, `docs/EXTENSION_PRIVACY.md`.

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
* **Database**: migrations are validated upgrade → downgrade → upgrade in CI (M1.2 adds
  `0003` `workbench_runs`; M1.3 adds `0004`, two nullable lifecycle columns on
  `recommendations` and `workbench_runs`; the `migrate` service applies them before the API and
  worker start). Rolling `0004` back drops the two columns (when and why a result became
  historical) but no result: recommendations keep their `status`, so those already
  `invalidated` stay non-current under the previous image, while workbench runs lose their
  historical marker and fall back to the M1.2 `stale_state` / `stale_snapshot` flags; the
  previous image no longer invalidates on promotion. Roll the images and the schema back
  together (an M1.3 image needs `0004`). M1.4 adds `0005` (decision journal): it adds the
  journal's columns to `decision_journal` (typed action, adherence, decision-time evidence,
  verification, evaluation status), backfills every existing feedback row from its
  recommendation (labelled `jd_legacy_…`, evaluation `unavailable`: no decision-time evidence
  was stored for it), keeps only the newest row per manager and gameweek active, and creates
  the insert-only `decision_evaluations` table. Rolling `0005` back drops `decision_evaluations`
  and the new columns, maps adherence back to the old vocabulary (`deferred` becomes empty) and
  **deletes the decisions recorded against Transfer Workbench runs** (the `0004` schema needs a
  recommendation) — export the affected managers first if they matter. Roll the images and the
  schema back together (an M1.4 image needs `0005`). M1.5 adds `0006` (forecast record and
  season evaluations): new tables `forecast_promotions`, `forecast_checkpoints` and
  `season_evaluations`, and `predictions` re-keyed by checkpoint (it refuses to run if
  `predictions` holds rows, which nothing wrote before M1.5). Rolling `0006` back **deletes the
  forecast record and every season evaluation** — the frozen live forecasts cannot be recreated
  (`pg_dump` first; restore it to keep them). Roll the images and the schema back together (an
  M1.5 image needs `0006`). `0007` re-keys `forecast_promotions` by (snapshot, forecast key); its
  downgrade keeps each snapshot's first promotion. To roll back the
  schema: `docker compose run --rm migrate alembic downgrade -1` with the previous image.
* **Images**: pin the previous tag in `docker-compose.override.yml` and `up -d`.
* **Forecast horizon or model configuration changes** invalidate the serving forecast's key:
  until the next `forecast_precompute` (every 15 minutes, or `fpl-worker run-once
  forecast_precompute`) the API answers `503 forecast not ready` for the current snapshot.
* **Models**: forecasts are keyed by model-config hash; reverting `config/models/*.yaml`
  restores the previous model on the next forecast. Registered artifacts roll back with
  `ModelRegistry.rollback` (`fpl_storage.registry`), which re-promotes the previous version.
* **Data**: snapshots are immutable and content-addressed; pin one with `FPL_SNAPSHOT_DIR`,
  set in `.env` so the api, worker and scheduler all pin the same one. A pinned deployment
  never promotes: results on any other snapshot (including the newer one being rolled back)
  are shown as historical, and the scheduler's sweep records them so (M1.3).

## Forecast record and Season Review (M1.5)

* The forecast job writes a `forecast_promotions` row (forecast file hash, export time,
  database clock) **before** the ready marker; if it cannot, the job fails and nothing is
  promoted. It then records FPL's server time (`forecast_attestations`, migration 0008): an
  HTTPS request to `fantasy.premierleague.com/api/event-status/` before computing and another
  after hashing the file. The worker therefore needs outbound HTTPS to FPL, which ingestion
  already uses; `FPL_TIME_ATTESTATION=false` turns it off (the test suite does), and every
  checkpoint is then at best `prospective_unverified`. A failure never blocks serving.
* The scheduler's `forecast_ledger` task (every 15 minutes) checks, in the database only, for
  gameweeks whose decision cutoff has passed since the record began without a checkpoint, and
  submits `forecast_capture` (lease 15 min); and, when the newest promotion has no FPL server
  time on record, `forecast_attest` (lease 5 min), which fails while FPL is unreachable.
  `season_eval` (hourly) submits an evaluation only when the input digest changed. All are
  idempotent; a failed capture or attestation is submitted again on the next tick, and
  retention keeps the candidates meanwhile.
* Persistence: the stack's services run with `restart: unless-stopped`, independent of any
  terminal or editor session, but only while the Docker VM runs. On macOS with Colima, start
  the VM at login (`brew services start colima`) or start it after every reboot; a laptop
  asleep before a cutoff freezes an older forecast (contract §2.3).
* Build with `FPL_BUILD_REVISION=$(git rev-parse HEAD)` (append `-dirty` when the tree has
  uncommitted changes); it is recorded with every forecast and checkpoint and is `unrecorded`
  otherwise. The code digest recorded beside it is computed from the installed sources.
* Operator: `POST /api/v1/season-review/evaluate` evaluates now; `fpl-worker run-once
  forecast_ledger` / `run-once season_eval` run the tasks once. Checkpoints and evaluations are
  never deleted by the application (only by a `0006` downgrade).

## Retention

Every live refresh exports a ~3 MB snapshot and, once a forecast is computed for it, a ~30 MB
feature cache and a ~7 MB forecast — about 1 GB a day unpruned (measured on the local stack).
The scheduler submits a `retention` job hourly (`fpl_api/retention.py`):

* kept with caches: the `FPL_SNAPSHOT_RETENTION_KEEP` newest snapshots (default 24), the one the
  API serves, every evaluation snapshot pinned in `config/backtest/*.yaml`, and — until the
  forecast record has frozen it — the snapshot whose forecast was served at each passed decision
  cutoff (with its forecast file, whatever its age);
* kept without caches: any other snapshot a stored recommendation, workbench run, journal
  decision, backtest run or registered model references (its decisions stay traceable and re-derivable; caches are recomputable);
* deleted: all other snapshots with their feature caches and serving markers, forecast/price
  caches unused by a kept serving forecast and older than `FPL_ARTIFACT_RETENTION_HOURS`
  (default 24), exports interrupted before their manifest and temporary files of killed writers.

Database rows are never deleted (journal decisions and their evaluations included; the
hourly `journal` task evaluates and verifies decisions whose outcome can still change —
pending or partial, final within the last seven days, not yet verified, or whose check failed
for a retryable reason within seven days of the deadline). A path that cannot be deleted fails the job after the rest is
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

* Export: `GET /api/v1/managers/{key}/export` (owner or operator) returns every manager-linked
  row, journal decisions and their evaluations included. Erase: `DELETE /api/v1/managers/{key}`
  deletes them in one transaction (including the precomputed analyses and the journal) and
  releases the key from its owner; `DELETE /api/v1/auth/me` erases an account with every
  manager it owns, its sessions and its analytics events.
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
lease expires (forecast/recommendation 30 min, workbench 20 min, alerts/retention/journal
15 min, squad analysis 10 min, backtest 6 h) — and an
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

`ForecastCalibrationDrift`. The *published* calibration is outside the promotion band. The
`fpl_model_metric` gauge exports the figures from `ml/reports/forecast_eval.json`, not live
measurements, so this fires only after a new report is published. Re-run
`ml/experiments/forecast_eval.py` on the newest data and apply the retraining policy
(`config/models/points.yaml → retraining`; the PSI checks in `fpl_forecasting/governance.py`
run from that experiment, not at runtime) to decide whether to retrain or roll back (see
*Upgrades and rollback → Models*).

### Security events

`SecurityEventsSpike`. Inspect `security_event` log lines (kind, path, client id prefix — never
keys). Repeated `auth_invalid` from one client: block at the edge. `webhook_rejected`: a manager
configured a webhook outside `FPL_WEBHOOK_ALLOWED_HOSTS` (SSRF guard working as intended).
