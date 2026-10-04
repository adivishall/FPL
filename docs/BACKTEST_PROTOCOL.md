# Backtest protocol (§27, §70)

Implementation: `packages/backtesting/src/fpl_backtest/runner.py`; experiment and report:
`ml/experiments/backtest.py` + `ml/experiments/backtest_report.py` → `ml/reports/backtest.{md,json}`;
parameters: `config/backtest/default.yaml`; tests: `tests/integration/test_backtest.py`,
`tests/unit/storage/test_schedule.py`.

## 0. Reproduce

```bash
uv sync --frozen
infra/scripts/dev_postgres.sh start                     # or any PostgreSQL 16 (FPL_DATABASE_URL)
uv run alembic upgrade head
uv run fpl-ingest historical --all                      # pinned vaastav commit (config/sources.yaml)
uv run fpl-ingest export-snapshot                       # → data/snapshots/snap_b64560a8c4f434ad984e
uv run python ml/experiments/backtest.py --run-only 2023-24   # one process per season (resumable)
uv run python ml/experiments/backtest.py --report-only
```

The snapshot id is a content hash of every canonical table; rebuilding from the pinned source on
macOS (host) and in the Linux container produced the same id. The backtest refuses to run on any
other snapshot (`config/backtest/default.yaml → snapshot_id`), so live captures never leak into
historical evaluation. Every gameweek is checkpointed atomically in
`data/eval/checkpoints/<season>/` (records, manager states, trained models, the forecast used); an
interrupted run resumes after the last finished gameweek and refuses a checkpoint written with
different parameters. A resumed replay is record-for-record identical to an uninterrupted one
(`test_interrupted_replay_resumes_to_identical_records`).

## 1. Information cutoff rule (§70.1)

A decision for gameweek *t* is made at `cutoff_t = deadline_t − 90 min` (ruleset
`timing.decision_buffer_minutes`). Every input is read through `PointInTimeView(dataset, cutoff_t)`,
which only returns rows whose availability timestamp is ≤ the cutoff:

| Data | Availability rule (ADR-0004) |
|---|---|
| Match stats / labels | kickoff + provisional lag (150 min); results are not known at kickoff |
| Fixture schedule | **as published at the cutoff — rule S1** (below) |
| Prices / ownership | observed at an earlier kickoff (price at the cutoff = last observation before it) |
| Transfer totals | published at the gameweek's deadline |
| Live status / news | capture time (none exists historically — the models never see it) |
| New players (A1/A2) | a player whose first archive row is in GW *t* is treated as registered at GW *t*'s cutoff, priced at that first observation (players are added before the deadline of their first gameweek) |

**Schedule rule S1** (`fpl_storage/schedule.py`). Archived seasons contain only the *final*
schedule. Reading it naively leaks the future: a fixture postponed on match day (2024-25 GW15
Everton v Liverpool) or a cup-driven blank (2023-24 GW29) would be "known" months early. The
original round of every moved fixture is recovered from the final schedule by an exact,
uniqueness-checked assignment (every team plays once per round; fewest moves). Until the move
could have been known — the original round's deadline, or the announcement of the new date if
earlier — the fixture is shown in its original round; then it is hidden until its new date is
announced (28 days before kickoff); then it is shown in its final gameweek. This can only
under-state what managers knew (e.g. FA Cup blanks were public a few weeks ahead). The 45 moves
of 2022-23 – 2025-26 are listed in the report appendix; a season whose reconstruction is not
proven unique makes the backtest refuse to run.

Enforcement: each forecast records `max_source_available_at`; the runner aborts if it exceeds the
cutoff, and the report counts look-ahead violations (must be 0). An automated test corrupts every
value that becomes available after GW3's cutoff and asserts that every decision up to GW3 —
transfers, starters, bench, captain, vice, chip and the engine's expected points — is unchanged,
for the full engine as well as the baselines
(`test_no_lookahead_future_perturbation_does_not_change_decisions`). Model training uses
`training_table`, whose labels obey the same rule; models are retrained every 4 gameweeks and the
team-strength configuration for a season is the one selected on earlier seasons only.

## 2. Walk-forward process (§70.2)

1. Select the season; build the GW1 squad with the initial-squad optimiser on the GW1 forecast
   (budget £100.0m). **All strategies start from this squad** so that differences come from weekly
   decisions, not from the starting point.
2. For each gameweek: freeze information at the cutoff → forecast (decomposed Monte Carlo model and
   the four point baselines from the same feature rows) → each strategy decides for **its own**
   manager state (squad, bank, purchase prices, free transfers, chips). Owned players count for
   their club *at the cutoff* (a mid-season move counts against the new club; an excess created
   by such a move may be kept or reduced but never increased).
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
| Forecast MAE/RMSE/bias, interval coverage and width, start-probability Brier/ECE/reliability — of the forecasts the replays used | backtest report, *Forecasts that drove the decisions* |
| Forecast metrics vs baselines (114 cutoffs, same protocol) | `ml/reports/forecast_eval.md` |
| Season and pooled points after hits; paired per-GW differences with bootstrap CIs (per season and season-stratified over 114 GWs) | backtest report |
| Squad-level calibration (actual inside the plan's simulated p10–p90) | backtest report |
| Decision regret (diagnostic): hindsight-best lineup of the same squad − chosen lineup | `lineup_regret` |
| Transfer efficiency: realised gain of every transfer over 1 and 4 GWs (− hit) | full transfer log (successes **and** failures) |
| Plan stability; round trips | backtest report |
| Squad validity rate; accounting invariants | backtest report |
| Runtime per decision; data freshness (cutoff − newest datum) | `runtime_s`, `data_age_hours` |

## 5. Known limitations

* **Hyper-parameters and model design were chosen while looking at these seasons.** The
  forecast-evaluation studies (`ml/reports/*.md`) and the optimiser defaults
  (`config/optimizer/default.yaml`) were iterated on 2023-24 – 2025-26 cutoffs; there is no
  untouched hold-out season, so the backtest is walk-forward in *information* but not free of
  *selection* bias. 2026-27 (in progress) is the first genuinely unseen season.
* Rule S1 is conservative: a move is assumed unknown until the original deadline unless the new
  date was announced earlier. Cup-driven blanks were usually public 2–4 weeks ahead, so the
  replays (all strategies alike) sometimes field players whose match has been moved.
* No historical availability news: the live availability layer is inactive in backtests, so
  injured players are only detected through minutes data (shared by every strategy).
* Labels are the archive's final values (corrected bonus points / stats), not the provisional
  values a manager would have seen right after the match.
* Prices between kickoffs are not observed; the price at a cutoff is the last observation before
  it, which can miss intra-week changes of ±£0.1–0.2m.
* 2023-24 has a single prior season (2022-23) for training, team strength and the BPS map.
* Hindsight diagnostics (regret, realised transfer gain) are not objectives and must not be read
  as proof of skill on single decisions; the season totals with confidence intervals are the
  evidence. Per-gameweek bootstrap CIs treat gameweeks as exchangeable and ignore autocorrelation.
* Overall rank is not reported: the archive has no distribution of other managers' scores.
