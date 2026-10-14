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
  non-GET routes and for every read of manager-linked data — any GET carrying `manager_key`, and
  `/squad`, `/settings`, `/notifications`, `/decisions`, `/journal*`, `/recommendations/*`, `/jobs/*`,
  `/managers/*` (401 without a key, 403 with a wrong one); reference data (players, fixtures,
  gameweeks, reports, models, health, metrics) stays public. Token-bucket
  rate limits per client (stricter for optimisation/simulation routes), applied before
  authentication; body-size limit; CORS allow-list; request ids (`x-request-id`). Rejections are
  logged as `security_event` (no key material) and counted in `fpl_security_events_total`. No
  FPL account credentials are ever accepted — sync uses the public entry id. The web UI calls
  the API through its server-side proxy (`/backend/*`), so the key never reaches a browser; the
  proxy answers `502` (`API unreachable`) or `504` (no response in 120 s) as JSON. Behind the
  proxy all browser users share the proxy's key and therefore one rate-limit bucket.
  The manager key is an identifier, not a credential: any holder of an API key (including every
  user of the web UI) can read or erase any manager's data by key — a single-tenant design.

## Endpoints

| Method & path | Purpose | Notes |
|---|---|---|
| `GET /health`, `GET /system/health` | Service, database, job backend, dataset; `live_source` = outcome and time of the latest recorded live capture (not an assumption); freshness incl. finished gameweeks whose results are missing | `status` = ok / degraded |
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
| `GET /workbench/context?manager_key=` | Transfer Workbench (M1.2): the squad with purchase and selling prices, next-GW and horizon expectations, availability, bank, transfers, chips and current weak spots | |
| `POST /workbench/evaluate` | Evaluate concrete plans (HOLD always; your transfer, engine replacements for a player, the optimiser's best plan) for one stored squad state as a job; the same question on the same inputs returns the same run | `202` queued, `200` when already computed |
| `GET /workbench/runs?manager_key=`, `GET /workbench/runs/{id}` | A manager's recent runs; one run with its result, `stale_state` / `stale_snapshot` flags and the names it needs | owner only |
| `GET /recommendations/{id}/changes`, `GET /workbench/runs/{id}/changes` | Result lifecycle (M1.3): why a result is no longer current and what differs between the inputs it used and the inputs served now — stored-state diff, snapshot-to-snapshot diff when the superseded snapshot is still on disk, forecast versions; honest `available: false` otherwise | owner only |
| `POST /scenarios`, `POST /what-if`, `POST /simulate` | Scenario re-simulation of the squad; optional transfer compared with holding under each scenario | |
| `POST /recommendations/generate` | Full decision package (stability, scenarios, chips, evidence) as a job | `202` |
| `GET /recommendations/current?manager_key=` | Active recommendation for the current gameweek | |
| `GET /recommendations/{id}` | Immutable stored package incl. Markdown explanation | |
| `GET /decisions?manager_key=` | The manager's recommendations with their lifecycle verdict and the decisions recorded against each (`feedback`, newest first: `entry_id`, `state` active / superseded, adherence, rationale, self-reported points) | |
| `POST /decisions/{id}/feedback` | Pre-M1.4 feedback, kept for compatibility: `followed` records the recommended plan as a journal decision; `ignored` / `partial` record "something else" (adherence rejected / modified); the realised points typed by the user are kept as self-reported (also on a repeat of the same feedback), never used by the evaluation | owner only; `409` when a different verdict would replace a decision recorded before the deadline after it |
| `GET /journal?manager_key=` | Decision journal (M1.4): recorded decisions, newest first; filters `gameweek`, `action`, `evaluation`, `include_superseded` | |
| `POST /journal` | Record a decision against an owned recommendation or workbench run, with the decision-time evidence copied into the record | `201` new, `200` the identical submission again; `404` foreign or unknown source; `409` after the deadline when a pre-deadline decision exists |
| `GET /journal/{id}` | One decision: evidence, recorded action, verification, the source's verdict now, evaluation history | owner only |
| `POST /journal/{id}/confirm` | The manager confirms they made the recorded action in FPL | owner only; not for DEFER / UNKNOWN |
| `POST /journal/{id}/evaluate` | Evaluate (and, after the deadline, verify) the decision now as a job | `202`; owner only; `409` for a replaced decision (only the one that stands is evaluated) |
| `GET /journal/summary?manager_key=` | The manager's own recorded decisions summarised (M1.5): counts, per-decision outcomes from official results (final decisions recorded before the deadline only), range counts beside a binomial typical range, paired sums; optional `season` | owner only; registered before `/journal/{id}` |
| `GET /season-review` | The live record (M1.5): checkpoint timeline, the newest evaluation (windows, exclusions, metrics, what changed), the past-season reference over the same gameweeks, calibration state, method; optional `season` | public, no manager data; reads stored rows only (never computes a forecast or submits a job) |
| `GET /season-review/evaluations` | Every stored evaluation of the live record, newest first, with its trigger and changes; `limit` | public |
| `POST /season-review/evaluate` | Evaluate the live record now (job); writes nothing when its inputs are unchanged | operator only; `202` |
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

## Result lifecycle (M1.3)

Every recommendation, workbench run, Copilot Home response and `/decisions` row carries a
`lifecycle` block:

```json
{"state": "stale", "current": false,
 "reasons": [{"kind": "snapshot_promoted", "detail": "a newer data snapshot was promoted to serving",
              "old": "snap_a…", "new": "snap_b…", "detected_at": "2026-10-11T09:10:00+00:00"}],
 "state_id": "mstate_…", "latest_state_id": "mstate_…",
 "snapshot_id": "snap_a…", "served_snapshot_id": "snap_b…", "snapshot_stale": true,
 "next_action": "regenerate", "evaluated_at": "…"}
```

`served_snapshot_id` is the promoted snapshot (the newest with a ready serving forecast) every
verdict is judged against; `snapshot_stale` is true only when the result's snapshot is *older*
than it — a result computed on a newer, not yet promoted export is current. `state` is one of
`current` (actionable now), `stale` (its inputs are no longer the current ones), `superseded`
(a recommendation requested later replaced it), `pending` (queued or running), `failed` (retry
by submitting the same question again). Reason kinds: `snapshot_promoted` (persisted:
`recommendations.status = invalidated`, `invalidated_at` / `invalidation_json` on both tables),
`squad_changed` (derived: the state is not the manager's latest), `superseded`,
`inputs_changed_before_finalised` (the job finished after its inputs moved on; stored as
historical from birth). Asking the same question again when a historical answer's inputs are
exactly the current ones again (the squad saved back, its snapshot still promoted) makes that
stored answer current again. `GET /recommendations/current` serves the current recommendation,
or — when none is current — the most recent one marked historical, never as current; `404`
only when there is none. Requests that name a manager key in more than one place (path, query,
body) must own every one of them (`403` otherwise). `docs/architecture/result-lifecycle.md` has
the state machine, triggers, worker races and the "what changed" rules.

## Decision journal (M1.4)

`POST /journal` body:

```json
{"manager_key": "fpl-123", "source": "recommendation", "source_id": "rec_…",
 "choice": "recommended", "plan": null, "captain": null, "vice_captain": null,
 "rationale": "optional, ≤ 1000 characters"}
```

`source` is `recommendation` or `workbench`; `choice` is `recommended` (the source's
recommended plan), `hold`, `plan` (a workbench plan, `plan` = `"<index>:<label>"` from the run's
`plans`; only plans that passed the rules replay, `422` otherwise), `defer` ("I'll decide
later") or `other` ("I did something else" — recorded, never reconstructed or scored). `captain` / `vice_captain` optionally change the chosen plan's armband
(two different starters of that plan; `422` otherwise). Every manager key in the request must be
owned, and the source must belong to that manager (`404` otherwise, without revealing whether it
exists). Recording against a historical source is allowed and returns a `warning`.

An entry:

```json
{"entry_id": "jd_…", "manager_key": "fpl-123", "season": "2026-27", "gameweek": 3,
 "deadline_at": "…", "recorded_at": "…", "prospective": true, "state": "active",
 "superseded_by": null,
 "source": {"kind": "recommendation", "id": "rec_…", "plan": "recommended"},
 "state_id": "mstate_…", "data_snapshot_id": "snap_…", "prediction_run_id": "pred_…",
 "recommended_action": "TRANSFER", "action": "HOLD", "adherence": "rejected",
 "sells": [], "buys": [], "hit_points": 0, "captain": 123, "vice_captain": 456, "chip": null,
 "rationale": "…", "confirmed_at": null,
 "verification": "matches", "verification_detail": {"…": "…"}, "verified_at": "…",
 "evaluation_status": "final", "evaluated_at": "…", "self_reported_points": null,
 "latest_evaluation": {"id": 7, "seq": 3, "status": "final", "evaluator_version": "journal-eval-1",
                       "data_snapshot_id": "snap_…", "realized_points": 61,
                       "realized_hold_points": 61, "realized_recommended_points": 57,
                       "detail": {"statements": ["…"], "caveat": "…"}, "revises_id": null,
                       "evaluated_at": "…"},
 "source_now": {"state": "stale", "current": false, "reasons": ["…"]},
 "names": {"123": "…"}, "links": {"trace": "/recommendations/rec_…/trace", "workbench_run": null}}
```

`GET /journal/{id}` returns `evidence` (the immutable decision-time evidence: identifiers,
squad, recommended / HOLD / chosen plans with lineups and expectations, the system's rationale)
and `evaluations` (every stored evaluation, newest first; a revision names the one it revises)
instead of `latest_evaluation`.
`action`: `HOLD`, `TRANSFER`, `HIT`, `CHIP`, `DEFER`, `UNKNOWN`. `adherence` (derived from the
recommended and chosen plans): `followed`, `modified`, `rejected`, `deferred`.
`evaluation_status`: `pending` (no relevant match played), `partial` (some relevant fixtures
unplayed or unrecorded, or every one recorded but not yet final — FPL has not finalised the
gameweek or the rows were loaded before the finalisation lag — then with provisional totals),
`final`, `unavailable` (nothing reconstructible to score, with the reason; a "something else"
decision ends here once the reference totals for holding and the recommendation are final).
`evidence` is `null` for a decision migrated from pre-M1.4 feedback.
`verification`: `matches`, `differs`, `observed`, `unavailable` (FPL-ID squads only, after the
deadline). The evaluator never grades a decision from one outcome; the methodology and its
limits are in `docs/architecture/decision-journal.md`.

## Season Review (M1.5)

Contract: `docs/architecture/season-evaluation.md`. `GET /season-review` returns `season`;
`timeline` (`record_started`, per gameweek `state` = `before_record` / `upcoming` / `due` /
`prospective` / `prospective_unverified` / `live_unverified` / `missed` with cutoff, expected
finality, reasons, row count and `attested_at` — the FPL server time by which the frozen
forecast existed; `next_checkpoint`; `current_forecast` — the served forecast's promotion time
and the FPL server time on record for it, or `null` while there is none); `evaluation` (`seq`, `previous_id`, `trigger`, `evaluator_version`,
`data_snapshot_id`, `windows` with each target gameweek's finality state and counts,
`exclusions` (`by_reason` on final target gameweeks, `by_reason_h0`, `clubs_gained_fixture_h0`,
`status_u_rows_h0`), `metrics.results[population][h0…h4]` from
`fpl_forecasting.season_eval.summarize` (`status` too_few_gameweeks / monitoring / look for all
players at horizon 0, `descriptive` elsewhere; `looks`, `looks_waiting`), `metrics.ece_null`
(with `observed` and `excess`), `metrics.sensitivity` (horizon 0, "as served"), `changes`
(newly scored, no longer scored, revised, looks issued and revised)) or `null`; `reference` (the past seasons
over the same gameweeks, per season); `drift_signals` (descriptive); `calibration` (`deferred` /
`not_needed` / `review_suggested`); `method` (the metric configuration: looks, margins,
populations). No verdict is present before a formal look (G = 8, 16, season end).

## Transfer Workbench contract (M1.2)

`POST /workbench/evaluate` takes `manager_key`, optional `state_id` (default: the latest stored
state), `horizon` (≤ the served maximum), `profile` and 1–8 `plans`:

| `kind` | fields | evaluated as |
|---|---|---|
| `hold` | — | no transfer this gameweek, optimal afterwards (always included, the baseline) |
| `transfer` | `sells`, `buys` | exactly these moves now (`first_gw_exact`), optimal afterwards; an illegal request is listed under `excluded` with the rule it breaks (squad size, position quota, club limit, budget with *selling* prices, unknown player) and never solved around |
| `replace` | `out_player`, `candidates` (≤ 6) | the replacement engine's shortlist (every affordable, club-legal candidate screened; top gain, high upside, cheap enablers), each as an exact single transfer |
| `best` | `max_transfers`, `allow_hits`, `alternatives` (≤ 4) | the optimiser's best plan, optionally capped in transfers now or forbidden to take hits anywhere; alternatives differ in their first-gameweek move |

The run (`GET /workbench/runs/{id}`) records `state_id`, `snapshot_id`, `forecast_key`,
`prediction_run_id`, the forecast's cutoff, simulation count and seed, model versions, the
optimiser config reference and the ruleset version. `result.result.plans` lists HOLD first, then
valid plans by the optimiser's objective; each plan carries `action`, `sells` / `buys` with
`moves_out` / `moves_in` (price, selling price, xP, start probability), `valid` + `issues`
(independent rules replay of the whole plan), `optimality` / `proven_optimal`, `transfers_now`,
`hit_now`, `hits_total`, `hit_cost_vs_hold`, `gain_1gw` / `gain_horizon` (paired against HOLD on
the same simulation samples, net of hits: mean, p10/p50/p90, `probability_positive`),
`gross_gain_horizon` (= net gain + hit cost), `objective_gain_vs_hold`, `passes_thresholds`,
`bank_after`, `free_transfers_after`, `squad_after`, `lineup`, the per-gameweek `timeline` and
`follow_up` moves. `excluded` lists what could not be scored and why; `notes` reports how many
candidates were screened. Status flow: `queued` → `running` → `succeeded` | `failed`
(`error`, retry by evaluating again); `stale_state` when the manager saved a newer squad,
`stale_snapshot` when the data snapshot moved on — the run is never silently replaced.

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
