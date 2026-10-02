# ADR-0006: Decomposed probabilistic forecasting with a joint fixture simulator

- Status: Accepted
- Date: 2026-10-02

## Decision

Forecasting is split into **parameter estimation** (`fpl_forecasting`) and **generative
simulation + scoring** (`fpl_simulation`), joined by typed parameter contracts in `fpl_domain`.

### Parameter models (each benchmarked against baselines, §57)

| Component | Model family | Baselines it must beat or justify against |
|-----------|-------------|---------------------------------------------|
| Team scoring environment | Poisson attack/defence ratings with home advantage, exponential time decay, MAP with Gaussian prior (promoted-team prior), fitted on a goals/xG blend; Laplace posterior for uncertainty | league-average rates; FDR-derived rates |
| P(start) | Gradient-boosted classifier + isotonic calibration on a later time slice | recency-weighted start rate; logistic regression |
| Minutes given start / given sub appearance | Categorical over minute buckets (classifier) with empirical within-bucket sampling | player's shrunk empirical distribution |
| Goal and assist involvement | Player share of team xG/xA per 90 on pitch, empirical-Bayes shrinkage to position-and-price priors, recency weighted | season-average rate; direct Poisson GLM |
| Saves, defensive-contribution actions, cards, own goals, penalty events | Shrunk rates (Poisson/negative-binomial), opponent-adjusted where meaningful | position averages |
| Bonus | Learned event→BPS linear map + residual noise per position, ranked within fixture | position bonus base rate |
| Price change | Calibrated multinomial classifier on transfer momentum | naive net-transfer threshold trend |
| Total points | Emerges from the simulator; benchmarked against a direct gradient-boosted points regressor and form/PPG baselines | form, PPG, position mean |

### Simulator (per fixture, vectorised over N samples)

1. Sample each player's minutes (start/sub/bucket); goalkeepers mutually exclusive per team.
2. Sample team goals from the team model; assign each goal a time `U(0,90)`.
3. Assign scorer among players **on the pitch at that time** ∝ goal share; assister ∝ assist
   share (excluding scorer) with the empirically estimated assisted-goal rate; residual mass →
   unmodelled players / own goals.
4. Goals conceded and clean sheets are evaluated **per player on-pitch interval**.
5. Saves, defensive actions (NB), cards, penalty events sampled from rates scaled by minutes.
6. BPS = learned linear map of sampled events + noise; bonus awarded by ruleset tie rules.
7. Versioned ruleset converts events to points; DGWs sum fixtures; BGWs give zero.

Outputs per player-GW: mean, median, p10/p25/p50/p75/p90, P(≥2/6/10/15), expected minutes,
P(start), component expectations, and the raw sample matrix (for squad-level, paired
comparisons). All runs record `seed`, `n_simulations`, `model_version`, `ruleset_version` and
`feature_snapshot_id` (§59.2).

## Rationale

Decomposition makes forecasts explainable ("expected minutes +10, team xG +8%") and gives full
distributions with structural correlation, which a single regressor cannot provide.

## Consequences

- More moving parts than a direct regressor; every part has its own baseline comparison and
  calibration report, and the direct regressor is kept as a benchmark (and ensemble candidate).
- Uncertainty is separated into **aleatoric** (simulation samples) and **epistemic**
  (parameter posterior draws used for stability analysis, ADR-0007).
