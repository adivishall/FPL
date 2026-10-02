# ADR-0007: MILP optimisation (HiGHS), independent validation, and decision policy

- Status: Accepted
- Date: 2026-10-02

## Decision

### Solver
- One multi-gameweek **MILP** (horizon H ≥ 1) solved with **HiGHS** (`highspy`), built from a
  sparse matrix by an in-house thin modelling layer (stable across highspy versions, fast to
  build). Variables follow §61.1 (`x, y, z, s, c, v, bench order, chip, hit`, free-transfer
  state, Free Hit squad). Deterministic settings: fixed `random_seed`, `threads=1`, configured
  `time_limit` and `mip_rel_gap`.
- On timeout HiGHS returns the best incumbent; it is used only if the independent validator
  accepts it (§82 "Optimizer timeout").
- **Independent validator**: every optimiser output is replayed through the domain state
  machine (`fpl_domain`) — squad legality each GW, budget with selling prices, free-transfer
  accounting, hits, chip windows, Free Hit reversion, lineup formation, captaincy. The MILP is
  never trusted on its own (§91 "validated by a separate rules validator").
- **Exact lineup solver**: XI/captain/bench via enumeration of the 8 legal formations (exact for
  additive objectives). Used for fast candidate screening and to cross-check MILP lineups.
- **Brute-force verifier**: exhaustive search over tiny synthetic leagues with known optima;
  the MILP must match it (§36 flagship test).

### Objective (configurable, versioned, stored with every run — §16, §61.3)
`maximize Σ_t δ^t [EV_lineup(t) − hit_cost·hits(t) − λ_transfer·transfers(t) − λ_risk·downside(t)]
+ α·terminal_squad_value + v_ft·FT_end + v_bank·bank_end + Σ_chips v_chip·unused_valid_chip − λ_fragility·fragility`

Weights live in `config/optimizer/{default,conservative,aggressive}.yaml`. The hit cost is the
real ruleset penalty; λ terms are explicit preferences, never baked-in constants.

### Known linearisations (documented, then corrected by simulation)
- Bench auto-substitution value uses per-slot bench weights in the MILP; the exact auto-sub rules
  are evaluated afterwards by Monte Carlo.
- Downside risk is linearised per player as `EV − q10`; portfolio covariance is evaluated by
  simulation, not in the MILP.
- Prices within the horizon are held at current values in the MILP; price movement risk is a
  separate probabilistic analysis (affordability risk) and a stress scenario.
- No re-purchase of a player sold within the horizon (prevents churn; keeps selling-price
  accounting exact).

### Alternatives and stability
- **HOLD** is always solved explicitly (no GW-1 transfers, later GWs free).
- Top-N distinct first-GW actions via no-good cuts (§29 "Preserve top-N feasible solutions").
- Stability: re-optimise under K epistemic perturbations of the forecasts; a recommendation is
  `stable` if its first-GW action stays optimal (or within tolerance) in ≥ `stability_threshold`
  of perturbations, otherwise `fragile` (§29).

### Decision policy (hold vs transfer vs hit vs chip, §18)
Candidate plans are compared with **paired common-random-number simulation** against HOLD.
A move is recommended only if `E[gain] ≥ min_gain` **and** `P(gain > 0) ≥ min_prob_positive`
(thresholds per objective profile). Otherwise the engine recommends HOLD and says why.

## Consequences

- Correctness rests on the validator + brute-force tests, not on solver trust.
- Large instances (deep decision with stress tests and stability) run asynchronously.
