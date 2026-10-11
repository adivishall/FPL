# Result lifecycle (M1.3)

Three stored results read as "the current answer" and each records the inputs it was computed
from. Currency is a property of those inputs, never of wall-clock age.

| Result | Table | Squad state | Data snapshot | Forecast | Other inputs |
|---|---|---|---|---|---|
| Recommendation | `recommendations` | `manager_state_id` | `data_snapshot_id` | `prediction_run_id` (and the `watch` baseline in the payload) | `ruleset_version`, `model_versions`, optimiser `config_refs`, `requested_at` (payload: the request time of its job) |
| Transfer Workbench run | `workbench_runs` | `state_id` | `data_snapshot_id` | `forecast_key` | `request_json` (plans, horizon, profile), `version` |
| Copilot Home analysis | `squad_analyses` | `state_id` | `data_snapshot_id` | `forecast_key` | `version` (the row key *is* the dependency set) |

Immutable after creation: the payload / result JSON (a re-activation only refreshes the
payload's `requested_at`), every identifier above, the trace rows (`optimization_runs`,
`prediction_runs`, feature and data snapshot rows). Mutable: the recommendation `status` and,
on both result tables, `invalidated_at` and `invalidation_json` (migration 0004) — when and why
a result stopped being current.

## The reference: the promoted snapshot

Every verdict is judged against **one** snapshot: the promoted one —
`DataService.promoted_snapshot_id()`, the newest snapshot whose serving forecast is ready (the
ready markers the forecast job writes), or the newest export while none is ready. It is read
from disk, independently of the process's own `serve_ready_snapshots_only`, so the API and the
worker agree. They load different snapshots by design: after a live refresh the worker computes
on the new export at once (it trains that export's forecast), while the API keeps serving the
promoted snapshot until the new forecast is ready (`docs/KNOWN_LIMITATIONS.md` §2).

Snapshots are **ordered** by export time (the manifest's `created_at`, else the registered
`data_snapshots.as_of`, which is the same value). A result is snapshot-stale only when its
snapshot is *older* than the reference; a result on a *newer* snapshot — a recommendation the
worker computed on an export the API has not promoted yet — stays current and is never
invalidated. A snapshot that is neither on disk nor registered counts as older (retention
removes only snapshots older than the served one; the reference is always on disk). A
**pinned** deployment (`FPL_SNAPSHOT_DIR`, e.g. a data rollback to an older snapshot) never
promotes, so there every other snapshot — older or newer — is stale; the pin belongs in `.env`
so the API, worker and scheduler share it.

The API follows a promotion at once: when a verdict, a regeneration or an evaluation finds a
newer promoted snapshot than the one it has loaded, it re-checks on the next access instead of
waiting for `FPL_SNAPSHOT_CHECK_SECONDS` (`lifecycle.follow_promotion`), so a regeneration
right after a promotion is computed — and de-duplicated — on the promoted data.

## States and transitions

```
recommendation.status:   active ──► superseded   (a recommendation *requested later* is saved for the same manager + GW)
                         active ──► invalidated  (snapshot promotion; or its inputs moved on before it was finalised)
                         superseded | invalidated ──► active
                                                 (re-activation: the same question is asked again and the
                                                  stored answer's inputs are exactly the current ones — its
                                                  squad state is the latest again and its snapshot is not
                                                  older than the promoted one; never otherwise)
                         born superseded         (an older request that finishes after a newer one)
                         born invalidated        (a result whose inputs moved on while it was computed)

workbench_runs.status:   queued ──► running ──► succeeded | failed   (job outcome; start / finish are
                                                                     conditional updates)
                         succeeded + invalidated_at = historical     (snapshot promotion, or finalisation
                                                                     against an older snapshot)
                         failed ──► queued                           (explicit retry: the same question again)
                         queued, no job for > 2 min ──► queued       (the enqueue was lost: re-queued)
                         succeeded, computed on other inputs ──► queued
                                                                     (asking again recomputes it; such a
                                                                     result is never made current)

verdict served (lifecycle.state): current | stale | superseded | pending | failed
```

`fpl_api.lifecycle.can_transition` is the persisted rule set and `reactivation_allowed` the
guard on re-activation; the verdict is derived by `lifecycle_block` from the persisted row
**and** the promoted snapshot, so a result is never served as current merely because an event
handler did not run. Every verdict carries `served_snapshot_id` (the reference) and
`snapshot_stale`.

## Invalidation triggers

| Event | Where it happens | Effect | Idempotent because |
|---|---|---|---|
| **Snapshot promotion** — the forecast job marks a newer snapshot's serving forecast ready | `fpl_api.jobs._job_forecast` → `mark_serving_ready` → `invalidate_for_snapshot(engine, promoted, settings)`; re-run on every `forecast_precompute` scheduler tick | one transaction: every `active` recommendation and every `succeeded` run whose snapshot is **older** than the promoted one → `invalidated` / `invalidated_at` with `{kind: snapshot_promoted, old, new, detected_at}`; results on the promoted or a newer snapshot are untouched | the `UPDATE … WHERE invalidated_at IS NULL` touches nothing the second time; the scheduler's re-run completes a sweep that failed after the marker was written |
| Missed promotion (handler failed, lost, or the result was created by an older build) | `lifecycle_block` on any read, including every journal row | the verdict is `stale` regardless; the row is lazily invalidated (`detected when the result was read`) by one conditional `UPDATE` — only if it is still current-looking and on the same snapshot; a failed write changes nothing about the verdict | same guard |
| **Newer squad state** (POST /squad, /squad/sync, onboarding) | derived at read: `state_id` ≠ the manager's latest `manager_state` row | `stale` with `{kind: squad_changed, old, new}`; **not persisted** for runs or on reads, because re-saving an earlier squad makes it the latest again (content-addressed ids) and the result is then current again | — |
| **Supersession** | `RecommendationStore.save` under a per-manager-and-gameweek advisory lock | the `active` recommendation(s) for the gameweek *requested before* the new one → `superseded`; if one requested later is active, the new one is stored `superseded` instead | the lock serialises concurrent saves: never two current recommendations for a gameweek |
| **Finalisation against obsolete inputs** — a job finishes after a promotion or (recommendations only) a squad save | `RecommendationStore.save`, `WorkbenchStore.finish` (`finalisation_check` in the write transaction) | the result is stored **historical from birth** with `{kind: inputs_changed_before_finalised, old, new}` and does **not** supersede the current recommendation | content-addressed ids: a retried obsolete request returns the same historical record |

The alert rule `invalidation` (`fpl_api.alerts._invalidation`, §62.1 re-optimisation triggers)
is a different question — *is the plan still the best one economically?* — and only runs for a
`current` recommendation; it does not change a result's lifecycle.

## Jobs compute what the request names

A workbench run is named by the API with the snapshot and forecast it serves. The `workbench`
job computes on exactly that snapshot (`AppServices.at_snapshot`), even when the worker has
already loaded a newer export; if the snapshot is gone it fails the run with that reason, and
if the computed snapshot or forecast key ever differ from the ones the run names, the run is
stored historical with both sets of identifiers (`computed_on_other_inputs`; asking the same
question again recomputes it rather than re-activating it). A job delivered again never reopens
or overwrites a finished run; a job that finds its run given up on (failed by the reaper) fails
itself, and every queueing of a run gets its own job, so a retry is never de-duplicated onto an
earlier attempt; a run whose job ended without finishing it is failed by the next read. An
enqueue that fails marks its job failed at once. The recommendation job records the snapshot
it computed on (possibly a newer, unpromoted export — see the reference above) and its request
time: the job's creation, or a later moment when the same question was asked again while it
waited or ran (the request is de-duplicated onto the job, which records the re-ask); the route
re-activates a stored answer only when the request was de-duplicated, never a fresh answer that
was stored superseded.

## "What changed?"

`GET /recommendations/{id}/changes` and `GET /workbench/runs/{id}/changes` (owner only) return
the lifecycle verdict plus, computed from persisted data only:

* **state** — the two stored `manager_state` rows: players out / in, bank, free transfers,
  source, when the newer one was captured;
* **snapshot** — only when the result's snapshot is older than the promoted one, and only while
  the superseded snapshot's export is still on disk (`snapshots_root/<id>`; retention keeps every
  snapshot a recommendation or run references, caches dropped): per-player availability, chance
  of playing, news and price differences for the players the result depends on — the old
  snapshot read at the result's own decision cutoff and season (`watch.cutoff` / `watch.season`
  for a recommendation; the run's forecast cutoff and season), the promoted one at its current
  cutoff — fixture-count differences for its gameweeks (a season change is reported instead of
  compared), the tables whose content hash changed, and both export times. The promoted
  snapshot is read from disk even when this process still has an older one loaded. When the old
  export is gone, unreadable, or holds no player data for the result's season at its cutoff, the
  block says so (`available: false`, reason) and gives only the identifiers and times — nothing
  is reconstructed;
* **forecast** — the old and the promoted snapshot's serving prediction run ids and model
  versions (or that the promoted snapshot's forecast is not computed yet).

The page never says "no input difference" for a result that is not current: when nothing can be
compared it says so and points at the verdict's reasons.

## Worker races

| Race | Outcome |
|---|---|
| promotion while a workbench job is queued or running | the job computes on the snapshot the run names; `finish` sees that snapshot is now older than the promoted one and stores the result historical; the page shows it as such, never as current |
| the worker computes on an export the API has not promoted yet | the result records that newer snapshot and is current (it is not older than the reference); reads persist nothing; the API keeps serving the promoted snapshot |
| an older recommendation job finishes after a newer one | squad state no longer the latest: stored `invalidated`; same state and snapshot (e.g. another profile): stored `superseded` — either way the newer one stays `active` and current |
| two recommendation jobs for the same gameweek finish together | the per-gameweek advisory lock serialises the saves; the later request wins |
| a question asked again while its job still waits, after another question was answered | the re-ask is recorded on the job; when it runs, its answer counts as requested at the re-ask and is current |
| a job runs for a run the reaper had already failed | the job fails (it did not compute the run); the retry gets a new job and computes it |
| a workbench job delivered twice, or after the reaper failed it and a retry re-queued it | `start` / `finish` are conditional: the run is computed once; a finished run is never reopened or overwritten |
| a run created but never handed a job (enqueue error, process death) | an enqueue error fails the run (and its job) at once — retryable; a run still queued without a job after 2 minutes is re-queued by the next identical request |
| the same promotion processed twice, or a sweep that failed | the second pass updates zero rows; the scheduler re-runs the sweep each `forecast_precompute` tick |
| a squad re-saved after a result became historical for a squad change | runs: current again by themselves (no persisted squad verdict); recommendations: asking again re-activates the stored answer |
| retried obsolete work | returns the same historical record (content-addressed); nothing current is replaced |

## Known limitations

* The Copilot Home analysis is looked up by the API's loaded snapshot; while the worker is
  ahead of the API (between an export and its promotion) a missing analysis is computed on the
  worker's export, so Home shows the previous analysis as *computing* until the promotion.
* A recommendation computed during that window records the newer export's snapshot, which can
  differ from the freshness banner (`docs/KNOWN_LIMITATIONS.md` §2).
* Re-activation history is logged (`recommendation_reactivated`, `workbench_run_reactivated`),
  not stored on the row: after a re-activation the row shows no earlier invalidation.
* A deployment whose API serves the newest export (not ready snapshots only) while a forecast
  job writes ready markers judges results against the newest *ready* snapshot; the compose
  deployment serves ready snapshots only, so the two are the same there.

Tests: `tests/integration/test_lifecycle.py` (a real promotion on a snapshots directory with
ready markers; a worker computing on an unpromoted export; an API that has not swapped yet;
finalisation races; request ordering; re-delivered and lost jobs; re-activation; the scheduler's
sweep; honest explanations; ownership; `test_identity.py` covers a query key that never vouches
for a body key), browser flows in `apps/web/e2e/flows.spec.ts` (squad change; real promotion of a staged
snapshot through the development stack, with the Future Planner and the journal) and
`apps/web/e2e-prod/production.spec.ts` (squad change through the proxy).
