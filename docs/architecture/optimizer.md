# Optimiser — formulation, verification and use

Implementation: `packages/optimizer/src/fpl_optimizer/` (pure computation, depends only on
`fpl_domain`; enforced by import-linter). Decision record: ADR-0007. Objective profiles:
`config/optimizer/{default,conservative,aggressive}.yaml` (versioned; the reference
`optimizer/<profile>@<version>#<hash>` is stored with every run).

## 1. Inputs

| Input | Source |
|---|---|
| `ManagerState` (squad with purchase prices, bank, free transfers, transfers already made, chips) | `fpl_domain.state` (synced or entered) |
| Ruleset (quotas, club limit, formations, FT banking, hit cost, chip windows, top-ups) | `config/rules/<season>.yaml` |
| `ev[p,t]`, `q10[p,t]` per player × horizon gameweek | forecast summary (`fpl_decision.inputs.player_table`) |
| Current purchase prices | `PointInTimeView.player_pool` at the cutoff |
| Preferences (locks, bans, forced sells/buys, max transfers, HOLD) and chip options / forced chips | user / planner |

Prices are tenths of £m. Selling prices of owned players follow the ruleset rule
(half-profit floor); prices are held at current values within the horizon (price movement is
handled by the probabilistic price model as a separate risk analysis).

## 2. MILP (HiGHS)

Index sets: players `p ∈ P` (candidate pool), horizon gameweeks `t = 0 … T−1`, chip instances
`k` (e.g. `wildcard_2`). `o_p = 1` if owned now; `sale_p` = selling price if owned, else price;
`buy_p` = price.

**Variables (§61.1)**

| Symbol | Domain | Meaning |
|---|---|---|
| `x[p,t]` | {0,1} | in the persistent squad after GW t's transfers |
| `y[p,t]`, `z[p,t]` | {0,1} | bought / sold in GW t |
| `s[p,t]`, `c[p,t]`, `v[p,t]` | {0,1} | starter, captain, vice-captain |
| `b[p,t,k]` | {0,1} | outfield bench slot k ∈ {1,2,3} |
| `ch[k,t]` | {0,1} | chip instance k played in GW t (only for allowed (k, t)) |
| `q[p,t]`, `w[p,t]` | {0,1}, [0,1] | Free Hit squad and playing squad (only where a Free Hit is allowed) |
| `h[t]` | ℤ≥0 | paid transfers |
| `f[t]`, `u[t]` | ℤ≥0 | free transfers available at GW t; unused free transfers |
| `bank[t]` | ℝ≥0 | bank after GW t |

**Constraints (§61.2)**

* Quotas: `Σ_{p∈pos} x[p,t] = n_pos` (2/5/5/3); club: `Σ_{p∈club} x[p,t] ≤ 3`.
* Flow: `x[p,t] = x[p,t−1] + y[p,t] − z[p,t]` with `x[p,−1] = o_p`; `y + z ≤ 1`; owned players
  are not re-bought and a bought player is sold at most once inside the horizon (exact selling
  prices, no churn).
* Budget: `bank[t] = bank[t−1] + Σ sale_p z[p,t] − Σ buy_p y[p,t] ≥ 0`.
* Paid transfers: `h[t] ≥ n[t] + made[t] − f[t] − M·(WC_t + FH_t)` with `n[t] = Σ_p y[p,t]`; no
  row at all in unlimited weeks (GW1, post-World-Cup GW17 2022-23, initial squad).
* Free transfers: `f[0]` = state; `u[t] ≤ max(f[t] − n[t] − made[t], 0)` (two rows + binary);
  `f[t+1] ≤ u[t] + per` (normal week), `f[t+1] ≤ f[t] + {0 | per}` or `≤ per` in WC/FH weeks
  depending on `chip_ft_policy`, `f[t+1] ≤ cap`, and for a scheduled top-up to K (AFCON 2025-26)
  `f[t+1] ≤ max(…, K)` via a binary branch. Only upper bounds are needed because more free
  transfers never worsen the objective, so they bind at the optimum; the reported path is
  recomputed with the domain rule.
* Free Hit: in a FH week the persistent squad is frozen (`Σ y = Σ z = 0`), `q` satisfies quotas,
  club limit and the budget `Σ sale_p q[p,t] ≤ bank[t−1] + Σ sale_p x[p,t−1]`, and the playing
  squad `w = q` (else `w = x`) via four linear rows; reversion is implicit (`x` unchanged).
* Lineup: `Σ s = 11`, `s ≤ w`, formation bounds per position, `Σ c = Σ v = 1`, `c, v ≤ s`,
  `c + v ≤ 1`; bench: `Σ_k b[p,t,k] = w − s` (outfield), each slot filled once.
* Chips: each instance at most once (exactly once at a forced GW), at most one chip per GW
  (ruleset), only inside its window and only if still available.
* Preferences: locked `x = 1`, banned `y = q = 0`, forced first-GW sells/buys, max transfers per
  GW, HOLD (`y[·,0] = z[·,0] = 0`), max total hits.

**Objective (§61.3, configurable)** with `a = ev − λ_risk·(ev − q10)` and discount δ:

```
max Σ_t δ^t [ Σ a·s + (m−1)·Σ a·c + w_v·Σ a·v + Σ_k w_k·bench_k + w_gk·bench_gk
              + Bench-Boost uplift + Triple-Captain uplift − hit_cost·h[t] − λ_tr·n[t] ]
    + v_ft·f[T] + v_bank·bank[T−1] + α·Σ ev[·,T−1]·x[·,T−1] − Σ_k v_chip(k)·Σ_t ch[k,t]
```

`bench_k` is the EV of the player in bench slot k (an auxiliary variable bounded by
`Σ_p ev·b[p,t,k]` and switched off in a Bench Boost week); BB adds `a` for every bench player and TC
adds `(m_TC − m)·a` for the captain. The hit cost is always the ruleset's; all λ/w/v are profile
configuration.

## 3. Verification (never trust the solver alone)

| Check | Where | Evidence |
|---|---|---|
| Independent replay of every solution through the domain state machine (legality, budget with selling prices, FT accounting, hits, chips, FH reversion, lineup) and recomputation of the objective with the exact lineup solver | `validate.py` | every test and benchmark solution |
| Exact lineup sub-problem (enumeration of all legal XIs) — MILP lineups must be optimal for their squad | `lineup.py` | `tests/unit/optimizer/test_lineup_and_initial.py` |
| Brute-force reference on tiny leagues (all action sequences via `apply_deadline`/`advance`) | `bruteforce.py` | `tests/unit/optimizer/test_milp_vs_bruteforce.py`: 2-GW transfers, risk/churn weights, hits, WC/BB/TC choice, Free Hit (optional + forced), AFCON top-up, unlimited week, locks/bans/forced moves, HOLD, initial squad |
| Infeasibility is reported, never papered over | `milp.py` | `test_infeasible_budget_is_reported_not_papered_over` |
| Candidate-pool pruning cost | `pool.py` | measured objective gap (`ml/reports/optimizer_benchmark.md`, `test_pool_pruning_costs_nothing_here`) |

## 4. Products built on it

* **HOLD and top-N alternatives** (`alternatives.py`): HOLD is solved explicitly; alternatives are
  generated with no-good cuts on the first-GW (sells, buys) set.
* **Replacement engine** (`fpl_decision.replacement`): full affordable universe screened exactly,
  shortlist (best gains + high-upside + cheap enablers), MILP re-optimisation with forced A → B,
  paired Monte Carlo gains vs HOLD (§60.3 contract).
* **Captaincy** (`fpl_decision.captaincy`): armband choices re-scored on the joint samples with the
  exact auto-sub / vice rules (`fpl_simulation.lineup_eval`).
* **Rotation pairing** (`fpl_decision.rotation`).

## 5. Chip choice inside the MILP

Opening every chip in every gameweek makes the MILP much harder (Free Hit squads, Bench Boost /
Triple Captain uplift variables): in the benchmark the chip-open model hits the time limit from
horizon 3 on. Two measures, both measured in `ml/reports/optimizer_benchmark.md`:

* `solve_with_chips` warm-starts the chip-open model from the no-chip optimum (MIP start mapped
  by variable name), so a time-limited incumbent is never worse than playing no chip;
* the decision engine does not rely on free chip timing: the chip planner solves one *forced*
  chip-week at a time (fast, optimal) and compares weeks explicitly, which is also what the
  explanation needs ("why not now").

The Free Hit budget row needs no big-M (the Free Hit squad is empty when the chip is not played),
which tightens the LP relaxation.

## 6. Known linearisations (corrected by simulation downstream)

Bench value uses fixed slot weights (exact auto-subs are evaluated on samples); the downside
penalty is per player (portfolio risk is evaluated on samples); prices are constant inside the
horizon; vice value uses a fixed weight. Final recommendations are compared by paired simulation,
not by the MILP objective alone (ADR-0007).
