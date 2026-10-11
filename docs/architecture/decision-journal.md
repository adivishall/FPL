# Decision journal and prospective evaluation (M1.4)

The journal answers four questions for each gameweek decision — what Copilot recommended and
why, what the manager decided, what happened, and how that compares with what was expected when
the decision was made — without rewriting history when newer data, forecasts or models arrive.

Code: `apps/api/src/fpl_api/journal.py` (evidence, store, evaluator, verification, job body),
routes in `apps/api/src/fpl_api/app.py` (`/journal*`), web in `apps/web/components/journal.tsx`,
`apps/web/app/journal/` and the record panels on Home and in the Transfer Workbench. Storage:
`decision_journal` (one row per recorded decision) and `decision_evaluations` (insert-only
evaluation history), migration `0005`.

## Four facts kept apart

| Fact | Where | Can it change? |
|---|---|---|
| What Copilot recommended | `recommended_action` + `evidence_json.recommended` (the source's recommended plan: the recommendation's chosen plan, or the workbench's best-ranked valid plan that makes a move this gameweek and clears the decision thresholds — the decision engine's rule — else HOLD) | never |
| What the manager said they would do | `action`, `sells`, `buys`, `hit_points`, `captain`, `vice_captain`, `chip` (typed columns) + `evidence_json.chosen`; `followed` = adherence (followed / modified / rejected / deferred, **derived** from the two plans); `user_note` = the manager's rationale | never — a change of mind writes a new row |
| What the manager confirms they did | `confirmed_at` (an explicit "I made this move in FPL") | set once |
| What first-party data shows | `verification` (matches / differs / observed / unavailable) + `verification_json` — the official FPL picks of the gameweek, for squads imported with an FPL ID, read after the deadline | reported beside the record; never changes it |

Adherence is never inferred from later events: a manager who follows the recommendation in FPL
but does not record it has no journal entry, and a recorded HOLD that the FPL picks contradict
stays a recorded HOLD with `verification = differs`.

## Lifecycle

```
recorded (state=active) ──► superseded       (a different decision for the same gameweek, recorded
                                               before the deadline — or the first decision was itself
                                               recorded after it); the identical submission again is
                                               the same row (content-addressed entry_id, jd_<hash>)
action:            HOLD | TRANSFER | HIT | CHIP  (a stored plan: lineup, captain, hits known)
                   DEFER                         ("I'll decide later": nothing to evaluate)
                   UNKNOWN                       ("I did something else": not reconstructible, never guessed)
evaluation_status: pending ──► partial ──► final        (revised official data adds a final revision)
                   pending ──► unavailable               (DEFER past the deadline; no lineup stored)
                   pending ──► partial ──► unavailable   (UNKNOWN: the reference totals for HOLD and
                                                          the recommendation, once final)
```

Only the decision that stands (`state = active`) is evaluated; a replaced one keeps whatever
evaluations it had and `POST /journal/{id}/evaluate` refuses it (409).

* **Deadline rule.** A decision recorded before the gameweek deadline (`prospective = true`)
  cannot be replaced after it (409): the journal never lets hindsight overwrite a pre-deadline
  decision. A decision recorded after the deadline is kept and labelled so; it is not
  prospective evidence. A decision migrated from pre-M1.4 feedback has no stored flag; it
  counts as prospective when it was recorded before the deadline.
* **Choices.** Only plans whose content is stored can be chosen: the recommendation's plan and
  HOLD, or any workbench plan that passed the independent rules replay (an illegal plan is
  refused with 422). An optional captain / vice-captain (two different starters of the chosen
  plan) changes the armband; the plan's own armband is kept as `offered_captain` /
  `offered_vice_captain` so the armband choice can be evaluated. The web form offers the
  armband for the plan it shows; "I'm holding" records HOLD with its own armband. Other
  transfers belong in the Transfer Workbench, where they are evaluated as whole-squad plans.
* **Obsolete sources.** Recording against a result that M1.3 marks historical is allowed (the
  manager may have acted on it) but the response carries a warning and the record keeps the
  source's verdict at the time (`evidence.source_state`); the entry page also shows the source's
  verdict now. Invalidation, supersession, re-queueing or erasure of the source never changes
  the journal record.

## Decision-time evidence (immutable)

`DecisionEvidence` (pydantic, `extra = forbid`) is copied into `evidence_json` when the decision
is recorded, because nothing it depends on is guaranteed to last: recommendations and runs are
invalidated or erased, workbench results can be cleared and re-queued, and forecast artifacts are
pruned within a day (`docs/architecture/result-lifecycle.md`, retention). It holds:

* identifiers: source kind / id / plan, manager state, data snapshot, prediction run, forecast
  key (for a workbench run, the ones its plans were computed on), ruleset version, model
  versions, decision cutoff, source data time, gameweek deadline;
* the squad before the decision, every involved player's position and **decision-time** club
  (the club the source was computed with — the saved squad may predate a club move), and each
  club's fixture ids in the gameweek as scheduled when the decision was recorded;
* three plans — recommended, HOLD and chosen — each with its moves, hits, chip, XI and ordered
  bench, captain and vice, and the decision-time expectation for the gameweek (mean, p10, p90;
  p50 where stored) and paired gain against HOLD on the same simulation samples (mean, p10, p50,
  p90, P(beats HOLD));
* the system's rationale (decision text, drivers, downside scenarios, optimality, stability)
  and per-player decision-time expectations (xP next, P(start)) where the source stored them.

HOLD's XI is stored for workbench runs. A recommendation stores HOLD's captain and vice-captain
but not its XI; the journal then rebuilds it from the decision-time expected points of the
unchanged squad (`best_lineup`, the backtest's HOLD convention) and labels it
`lineup_source = reconstructed`, with the stored armband when both players start in the rebuilt
XI (`armband_as_stored`; otherwise the rebuilt XI's own armband, said so in the note and the
evaluation, which then reports no expected-difference band); if those expectations are
incomplete it is `unavailable` and the HOLD comparison is reported as unavailable — never
invented.

## Evaluation

`evaluate_decision` is pure (dataset in, result out) and runs in the `journal` job — submitted
hourly by the scheduler for managers with a due decision, and on demand from the entry page
(`POST /journal/{id}/evaluate`). It reads the newest results the worker has loaded and records
the snapshot it used. One rule (`due_clause`) decides what is due, for the scheduler and the
job alike: pending or partial; final and evaluated within the last seven days (official
revisions); not yet verified; or verification failed for a retryable reason, within seven days
of the deadline. Everything else has settled and leaves the sweep.

1. **Completeness** for the clubs involved (decision-time clubs of every player in the lineups
   of the chosen plan, HOLD and the recommended plan): *pending* until a relevant match has been
   played; *partial* while some relevant fixture is unplayed or unrecorded (no totals — a player
   whose match is still to come would count as not playing and be auto-substituted); *partial
   with provisional totals* when every relevant fixture is recorded but FPL has not finalised
   the gameweek, or the loaded result rows were captured before they were final (bonus can
   still change); *final* when the gameweek is `finalized` (FPL `finished` and `data_checked`),
   every relevant fixture is `final`, and every relevant result row is `finalized` — captured
   after the ruleset's finalisation lag (72 hours after the gameweek's last match for 2026-27,
   an assumption of the ruleset); the daily results re-check reloads them, so a final
   evaluation arrives about three to four days after the last match. Fixtures that moved out of the gameweek since the decision
   (postponements) are listed and not scored here — FPL scores them in the gameweek they are
   played in; fixtures moved in are listed too.
2. **Scoring** reuses the domain rules: `fpl_domain.squad.gameweek_points` (automatic
   substitution in bench order with formation rules, vice-captain armband only when the captain
   did not play, Triple Captain, Bench Boost) on the official per-fixture points summed per
   player (double gameweeks included), minus the plan's hit points — for the recorded action, for
   HOLD and for the recommended plan, and for the plan's own armband when the manager changed it.
3. **Comparison**, stated as facts: realised points against the decision-time 80 % range;
   realised difference to HOLD against the decision-time expected difference, its 80 % range and
   P(beats HOLD) (on the same samples the engine used); difference to the recommended plan; who
   wore the armband and why; automatic substitutions. A followed HOLD has a zero difference by
   construction. Every evaluation carries the caveat that one gameweek is a single draw from the
   forecast distribution — a sound decision can lose points and a poor one can gain them — and
   no statement grades a decision or attributes the outcome to it.
4. **History.** `decision_evaluations` is insert-only and numbered per decision (`seq`). The
   outcome digest covers the relevant fixtures' states and the scored players' per-fixture
   minutes, points and finality; inputs whose digest equals the latest evaluation's write
   nothing (retries and repeated jobs are idempotent; the journal row lock serialises writers
   and the unique `(entry_id, seq)` backs it). A changed final outcome — revised official data,
   including a correction that is later withdrawn — is a new row whose `revises_id` points at
   the previous final evaluation, which is kept. A process still on a snapshot exported before
   the one the latest evaluation used writes nothing (export time only: in a pinned deployment
   the pinned snapshot still replaces evaluations made on earlier exports).

## First-party verification

After the deadline, for a squad imported with an FPL ID (`manager_state.manager_id`), the job
reads the gameweek's official picks (`/entry/{id}/event/{gw}/picks`), maps FPL element ids to
player codes through the season's `players.source_id`, and compares the squad, captain,
vice-captain and chip with the recorded action. A manual squad is `unavailable` (nothing to
check against), and so is a decision whose gameweek deadline is unknown or one migrated from
pre-M1.4 feedback; a disabled live source, an API failure or unknown players are `unavailable`
and retried hourly for seven days after the deadline (and whenever "evaluate now" is used).

## Ownership, privacy, retention

Every journal route is behind the router's ownership check (every manager key in path, query and
body must be owned); record-id routes check the entry's manager; recording requires the source
recommendation or run to belong to the same manager (404 otherwise); `GET /journal*` is a
privileged prefix (a key is required, and manager data needs a signed-in user). Evaluation jobs
carry the manager key, so `/jobs/{id}` stays with its owner. `GET /managers/{key}/export` returns
both tables; `DELETE /managers/{key}` and account deletion erase them first (before the
recommendations, runs and states they reference). Retention keeps the data snapshots journal
decisions reference; journal rows themselves are never pruned.

## Limits

* Only decisions the application can reconstruct are evaluated: chips are evaluated when they
  are part of a stored plan (recommendations and workbench runs do not plan chips today), and
  "something else" is recorded but never scored.
* HOLD's XI for a recommendation is rebuilt, not stored (labelled).
* Distribution comparisons use the stored mean / p10 / p90 (no per-sample outcomes are kept), so
  the result is placed in a band, not at an exact percentile.
* Verification needs an FPL ID and the live API; it compares squad, captain, vice-captain and
  chip, not bench order.
* "Final" waits for result rows loaded after the ruleset's finalisation lag (an assumption of
  72 hours for 2026-27), not for an FPL signal on the rows themselves.
* One manager's journal is not a calibration study: season-level calibration of the live models
  is separate backlog work.
