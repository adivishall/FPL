# ADR-0010: Explanations are built from structured evidence

- Status: Accepted
- Date: 2026-10-02

## Decision

Every recommendation carries a `DecisionExplanation` (§26, §68) assembled only from stored,
typed evidence objects:

```
Evidence { id, kind, feature, player_id, value, baseline, direction, unit,
           source_snapshot, model_version, importance, as_of }
```

- **Drivers**: the expected gain of a move is decomposed exactly into scoring components
  (appearance, goals, assists, clean sheets, saves, defensive contributions, bonus, cards,
  goals conceded) per gameweek, because the forecast is decomposed. Input-level attribution
  (minutes vs team strength vs player rates vs fixtures) uses exact Shapley values over a small
  set of factor groups evaluated with the analytic expectation model.
- **Downside**: conditional analysis of the lower tail of the paired gain distribution (which
  events dominate the worst 10% of simulations) + the stress-scenario table.
- **Binding constraints**: read from the solved model (budget slack, club counts at the limit,
  free transfers used, chip windows).
- **Counterfactual**: the explicitly solved HOLD plan.
- **Reproducibility**: data snapshot id, feature snapshot id, model versions, ruleset version,
  optimiser run id, seeds.

Prose is produced by deterministic templates that reference evidence ids; no free-text reason is
emitted without at least one evidence id (§50.2 "Every displayed reason must point to actual
stored evidence"). An optional LLM renderer may later rephrase, but numbers are always injected
from evidence (§81).
