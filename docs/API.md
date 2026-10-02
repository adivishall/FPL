# API reference

Service: `apps/api` (FastAPI; `uvicorn fpl_api.main:app`). OpenAPI schema at `/openapi.json`,
interactive docs at `/docs`. All routes are under `/api/v1`. The surface is the **union** of the
§32 and §72 lists; where two names describe one capability both routes exist and share one
handler (ADR-0001 #3).

## Conventions

* **Freshness block** on every data-serving response (§6, §82): data snapshot id, season,
  gameweek, decision cutoff, newest source timestamp, `status` (fresh/stale/expired/unknown),
  `degraded` + reasons. When the live FPL source is unreachable the API keeps serving the last
  validated snapshot and says so — it never fabricates fresher data.
* **Units**: prices in tenths of £m (`55` = £5.5m); points are FPL points.
* **Errors**: `422` rule violations (`{"detail", "code"}`), `404` unknown resources, `429` rate
  limit (`Retry-After`), `503` missing forecast (computation queued, `job_id`) or unavailable
  dependency (database, live source).
* **Jobs** (ADR-0009): expensive requests return `202 {"job_id"}`; poll `GET /jobs/{id}` until
  `succeeded`/`failed`; `result_ref` points at the stored result (recommendation id, backtest id,
  forecast key). Identical queued/finished requests are de-duplicated.
* **Security** (§35, §75): optional API keys (`X-API-Key`, stored as SHA-256 only) for all
  non-GET routes and for reads of manager-linked personal data (`/managers/*`); token-bucket
  rate limits per client (stricter for optimisation/simulation routes), applied before
  authentication; body-size limit; CORS allow-list; request ids (`x-request-id`). Rejections are
  logged as `security_event` (no key material) and counted in `fpl_security_events_total`. No
  FPL account credentials are ever accepted — sync uses the public entry id. The web UI calls
  the API through its server-side proxy (`/backend/*`), so the key never reaches a browser.

## Endpoints

| Method & path | Purpose | Notes |
|---|---|---|
| `GET /health`, `GET /system/health` | Service, database, job backend, dataset and live-source status + freshness | `status` = ok / degraded |
| `GET /data-quality` | Recent data-quality incidents + freshness | |
| `GET /gameweeks/current` | Season, gameweek, deadline, decision cutoff, freshness | |
| `GET /players` | Player pool with forecast summary | filters `position`, `team`, `max_price`, `q`; `sort` xp/xp_next/price/start; `horizon`; `limit` |
| `GET /players/{code}` | Profile: registry, recent matches (PIT), price history, live status | |
| `GET /players/{code}/forecast` | Per-GW distribution (quantiles, P(≥k)), minutes, start probability, component xP, provenance | |
| `GET /squad?manager_key=` | Latest stored manager state | |
| `POST /squad` | Manual squad entry, validated by the rules engine | 15 picks, bank, free transfers, chips used |
| `POST /squad/sync` | Reconstruct state from the public FPL API | `503` + guidance in degraded mode |
| `POST /optimize`, `POST /optimize/transfer` | Best plan, HOLD and alternatives with paired gains and timelines | horizon > `sync_horizon_limit` or chips → `202` job |
| `POST /optimize/squad` | Initial squad (budget, horizon, profile) | |
| `POST /lineup` | Exact XI/bench/armband + captaincy profiles (expected/safe/high-variance, paired win matrix) | optional chip TC/BB |
| `POST /replacement`, `POST /replacements` | Replacement engine (§60.3 contract per candidate) | |
| `POST /chips/simulate` | Chip planner (all chips × weeks, value of waiting, reasons) + optional forced-chip what-if | |
| `POST /scenarios`, `POST /what-if`, `POST /simulate` | Scenario re-simulation of the squad; optional transfer compared with holding under each scenario | |
| `POST /recommendations/generate` | Full decision package (stability, scenarios, chips, evidence) as a job | `202` |
| `GET /recommendations/current?manager_key=` | Active recommendation for the current gameweek | |
| `GET /recommendations/{id}` | Immutable stored package incl. Markdown explanation | |
| `GET /decisions?manager_key=` | Decision journal with feedback | |
| `POST /decisions/{id}/feedback` | Followed / ignored / partial, note, realised points | |
| `POST /backtests` | Start a walk-forward backtest (job) | |
| `GET /backtests`, `GET /backtests/{id}` | Published report summary + stored runs; run detail | |
| `GET /reports`, `GET /reports/{name}`, `GET /reports/figures/{file}` | Published experiment reports (allow-listed) | |
| `GET /models` | Promotion-gate status and the serving forecast's provenance | |
| `GET /settings`, `POST /settings` | Manager preferences (horizon, profile, alert threshold, time zone, webhook) | webhook URL must pass the SSRF policy (`422` otherwise) |
| `GET /recommendations/{id}/trace` | Traceability chain (§76.2): recommendation → optimisation run → prediction set → feature snapshot → data snapshot → source revision, with integrity checks | `complete` = every check passed |
| `POST /notifications/evaluate` | Run the §74 alert rules for a manager (job) | `202`; one job per manager per minute |
| `GET /notifications?manager_key=` | Alerts (newest first; `unread_only`) with evidence and config reference | |
| `POST /notifications/read?manager_key=` | Mark alerts read | |
| `GET /managers/{key}/export` | Every manager-linked row (§75) | API key required when keys are on |
| `DELETE /managers/{key}` | Erase manager-linked rows; audit entry stores only a key hash | |
| `GET /jobs/{id}` | Job status | |
| `GET /metrics` (no prefix) | Prometheus metrics (§76.1; see docs/DEPLOYMENT.md) | |

## Recommendation contract (§72.1)

`GET /recommendations/current` returns, besides the identifiers:

```json
{
  "gameweek": 2,
  "snapshot_id": "snap_…",
  "ruleset_version": "2026-27.1",
  "decision": {"action": "HOLD | TRANSFER | HIT | CHIP", "transfers_out": [], "transfers_in": [],
               "chip": null, "expected_points": 52.1, "expected_gain_vs_hold": 0.0,
               "p10": 33, "p50": 51, "p90": 72, "confidence": 0.81, "stability": "stable"},
  "chosen": {"timeline": ["… per-GW plan steps …"]}, "hold": {}, "alternatives": [],
  "lineup": {}, "captaincy": {}, "evidence": [], "explanation": {}, "scenarios": [],
  "stability": {}, "chips": [], "assumptions": [], "generated_at": "…",
  "model_versions": {}, "optimizer_run_id": "opt_…", "markdown": "# GW2 recommendation — …",
  "freshness": {}
}
```

`confidence` is the paired probability that the chosen plan beats holding (for HOLD: the
probability that no considered move beats holding) — never a fabricated "certainty score".

## Alerts (§74)

Rules live in `fpl_notifications.rules`, thresholds in `config/notifications/default.yaml`
(versioned; the reference is stored on every alert). A recommendation stores a *watch* block —
the inputs it was built on — and evaluation recomputes them at the current cutoff:

| Kind | Fires when | Materiality |
|---|---|---|
| `deadline` | inside a reminder window (24 h, 2 h) before the official deadline, shown in the manager's time zone | always (one per window) |
| `squad_change` | owned player's availability drops ≥ 25 pts, P(start) moves ≥ 0.25, or a regular starter is benched while fit | expected points at stake ≥ 1.0 |
| `price_risk` | exact P(planned transfers unaffordable after price moves) from the calibrated price model | probability ≥ 0.30 |
| `fixture_change` | fixtures added/removed for the planned squad's teams inside the horizon | Δfixtures × xP per match ≥ 1.5 |
| `invalidation` | re-solved on fresh forecasts, a different plan beats the saved one by ≥ the manager's threshold, or the saved move's P(beats hold) < 0.5 | regret in points |
| `post_gameweek` | the gameweek after a recommendation finished: forecast vs actual, misses explained (minutes, hauls, blanks) | one report per GW |

De-duplication: each alert's key names the underlying *state* (e.g. `avail:<season>:<gw>:<player>:<status>:<chance>`),
and the store's unique constraint makes re-evaluation of unchanged inputs a no-op.
