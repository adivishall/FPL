# Backtest protocol (§27, §70)

Implementation: `packages/backtesting/src/fpl_backtest/runner.py`; experiment and report:
`ml/experiments/backtest.py` → `ml/reports/backtest.{md,json}`; tests:
`tests/integration/test_backtest.py`.

## 1. Information cutoff rule (§70.1)

A decision for gameweek *t* is made at `cutoff_t = deadline_t − 90 min` (ruleset
`timing.decision_buffer_minutes`). Every input is read through `PointInTimeView(dataset, cutoff_t)`,
which only returns rows whose availability timestamp is ≤ the cutoff:

| Data | Availability rule (ADR-0004) |
|---|---|
| Match stats / labels | kickoff + provisional lag (results are not known at kickoff) |
| Fixture schedule | `schedule_available_at`; double-gameweek additions only 28 days before kickoff |
| Prices / ownership | observed at an earlier kickoff (price at the cutoff = last observation before it) |
| Transfer totals | published at the gameweek's deadline |
| Live status / news | capture time (none exists historically — models never saw it) |

Enforcement: each forecast records `max_source_available_at`; the runner aborts if it exceeds the
cutoff, and the report counts look-ahead violations (must be 0). An automated test corrupts every
value that becomes available after GW3's cutoff and asserts that all decisions up to GW3 are
unchanged (`test_no_lookahead_future_perturbation_does_not_change_decisions`). Model training uses
`training_table`, whose labels obey the same rule; models are retrained every 4 gameweeks and the
team-strength configuration for a season is the one selected on earlier seasons only.

## 2. Walk-forward process (§70.2)

1. Select the season; build the GW1 squad with the initial-squad optimiser on the GW1 forecast
   (budget £100.0m). **All strategies start from this squad** so that differences come from weekly
   decisions, not from the starting point.
2. For each gameweek: freeze information at the cutoff → forecast (decomposed Monte Carlo model and
   the four point baselines from the same feature rows) → each strategy decides for **its own**
   manager state (squad, bank, purchase prices, free transfers, chips).
3. The decision is applied through the domain state machine (`apply_deadline`) at the prices known
   at the cutoff; an illegal decision is recorded (validity rate) and replaced by HOLD.
4. Score with the **actual** outcomes: official points of the playing squad, automatic
   substitutions from actual minutes, captain/vice rules, chips, minus hit points.
5. `advance` the state (free-transfer banking, chip status, Free Hit reversion) and continue.

## 3. Strategies (§28, §70.3)

| Strategy | Forecast | Decision policy | Purpose |
|---|---|---|---|
| `engine` | decomposed MC | 5-GW MILP + HOLD/alternatives, paired-gain thresholds, chip planner | the full system |
| `engine_no_chips` | decomposed MC | as above, chips never played | separates chip timing from weekly decisions |
| `single_gw_mc` | decomposed MC | 1-GW MILP, no thresholds | value of multi-GW planning and thresholds |
| `simple_xp` | ppg × availability | 1-GW MILP | "simple xP optimiser": prediction vs optimisation sophistication |
| `form` | recent form | 1-GW MILP | naive form ranking |
| `fpl_style_heuristic` | official-style heuristic *approximation* | 1-GW MILP | known heuristic baseline (not the official algorithm, which is not public) |
| `hold` | decomposed MC (XI/captain only) | never transfers | naive hold |

Every strategy uses the same rules engine, prices, state machine and lineup rules — identical
information constraints.

## 4. Metrics (§70.4)

| Metric | Where |
|---|---|
| Point forecast MAE/RMSE, log loss/Brier, CRPS, calibration | `ml/reports/forecast_eval.md` (114 cutoffs, same protocol) |
| Season points after hits, per-GW paired differences with bootstrap CIs | backtest report |
| Decision regret (diagnostic): hindsight-best lineup of the same squad − chosen lineup | `lineup_regret` |
| Transfer efficiency: realised gain of every transfer over 1 and 4 GWs (− hit) | full transfer log (successes **and** failures) |
| Plan stability: share of planned next-week actions that changed | `plan_churn_rate` |
| Squad validity rate | `valid` |
| Runtime per decision | `runtime_s` |
| Data freshness: cutoff − newest source datum | `data_age_hours` |

## 5. Known limitations

* The fixture archive contains the *final* schedule; postponements announced late are visible
  earlier than in reality (double-gameweek additions are lag-gated, blanks are not). Quantifying
  this requires historical schedule snapshots, which the source does not provide.
* No historical availability news: the engine's live availability layer is inactive in backtests,
  so injured players are only detected through minutes data (a disadvantage shared by every
  strategy).
* Prices between kickoffs are not observed; the price at a cutoff is the last observation before
  it, which can miss intra-week changes of ±£0.1–0.2m.
* Hindsight diagnostics (regret, realised transfer gain) are not objectives and must not be
  read as proof of skill on single decisions; the season totals with confidence intervals are the
  evidence.
* Overall rank is not reported: the archive has no distribution of other managers' scores.
