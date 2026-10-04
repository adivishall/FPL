# ADR-0004: Point-in-time (PIT) data model and leakage controls

- Status: Accepted
- Date: 2026-10-02

## Context

§7, §55.2 (temporal ordering), §70.1 and §77.2 require that a decision at time `T` uses only
information whose *availability* timestamp is ≤ `T`. This must hold for live recommendations,
model training and backtests alike, using one code path (§27 "executes the same rules the live
engine uses").

## Decision

Every canonical observation carries an **`available_at`** timestamp distinct from its event time.

| Observation | Event time | `available_at` |
|-------------|-----------|----------------|
| Fixture schedule entry | kickoff | archive: the schedule *as published* at `T` (rule S1 below); live: capture time |
| Match result / player match stats (provisional) | kickoff | `kickoff + provisional_lag` (default 2.5 h) |
| Match stats (final) | kickoff | gameweek `finalized_at` (ruleset-configured lag) |
| Price / ownership observed on a match row | kickoff | `kickoff` (it is the value at that time) |
| GW transfer counts (`transfers_in/out` for GW t) | — | deadline of GW t |
| Live bootstrap snapshot fields | — | `retrieved_at` |
| News item | `news_added` | `news_added` (or `retrieved_at` if missing) |
| Positions, season membership | — | season start |

**Decision cutoff**: `cutoff(GW t) = deadline(t) − decision_buffer` (config, default 90 min).
Therefore no GW-t transfer counts or GW-t results can be visible to a GW-t decision. One documented
exception (policies A1/A2): a player whose *first* archive row is in GW t is treated as
registered at GW t's cutoff with that first observed price — players are added to the game before
the deadline of their first gameweek, and their launch price is the price at that deadline.

**Rule S1 — schedule as published (added 2026-10-04, after an audit found a leak).** Archived
seasons hold only the final schedule; stamping it with the season publication time made late
postponements visible months early. `fpl_storage.schedule` recovers each moved fixture's
original round (exact, uniqueness-checked assignment: every team plays once per round, fewest
moves) and `PointInTimeView.fixtures()` shows the fixture in its original round until
`known_at = min(original deadline, new-date announcement)`, hides it until the new date is
announced (`kickoff − announce_lag_days`), then shows it in its final gameweek. Seasons whose
reconstruction is not proven unique keep the archive timestamps and the backtest refuses to run
on them. The rule only under-states information (cup blanks were known weeks ahead).

**Enforcement (defence in depth):**
1. `PointInTimeView(as_of=T)` is the *only* accessor used by feature building, training and
   backtests. Every table read is filtered `available_at <= T`.
2. Each feature row records `max_source_available_at`; the builder raises `LeakageError` if it is
   `> T` (§6 "Block features that reference future matches").
3. Property test: for random `T`, arbitrarily perturbing every record with `available_at > T` must
   leave features, forecasts and decisions bit-identical.
4. Backtests store, per decision, the max `available_at` actually touched; the no-lookahead audit
   asserts it is `≤ cutoff` for every decision.
5. Training labels obey the same rule: a label is usable for a model trained at `T` only if its
   `available_at ≤ T` (`label_policy: provisional_ok | finalized_only`).
6. Train/validation/test splits are strictly chronological (§7 "Model tuning").

## Consequences

- Historical approximations (price at deadline, schedule visibility, A1/A2) are explicit
  policies, recorded with every run; the PIT rules are part of the feature-version fingerprint
  (`fpl_features.version_lock`), so changing them invalidates cached features.
- Results published before rule S1 (commit `03c14a7`) contained the schedule look-ahead and were
  superseded.
- The same PIT view powers the live system (with `as_of = now`), so live and backtest code paths
  cannot diverge.
