# Backlog after V1 (scope frozen at the V1 release)

Nothing here is required for V1. Each item says what it would change and the evidence that
motivates it; V1.1 items build on what exists, V2 items need a new capability. Measurements are
in `ml/reports/performance_stages.md`, open audit items in `docs/FINAL_AUDIT.md` → *V1 RELEASE
STATUS*. The product gap analysis, competitor comparison and the roadmap that re-orders
these items around user value (and adds identity, beta access and analytics as launch-critical)
is `docs/PRODUCT_GAP_ANALYSIS.md`.

## V1.1

| Item | Why | Evidence |
|---|---|---|
| Re-simulate only the players a fixture shock affects | the ten Triple Captain / Bench Boost postponement checks re-run the full Monte Carlo (~35 s of a 5-GW recommendation) | stage report: chips 48.9 s, of which re-simulations 34.5 s |
| Reuse historical-season features across hourly snapshots | feature building is 71 % of a cold forecast and rebuilds pinned seasons every hour | stage report: 49.8 of 70.6 s |
| Faster stability analysis | the slowest single perturbation (two exact solves, ~20 s) bounds the parallel stage | stage report: stability 41.8 s with 4 processes |
| Replacement picker latency | 13–49 s on the deployed API, solved serially in the request | `docs/KNOWN_LIMITATIONS.md` §5 |
| Better chip planning | chip-open solves always reach the 60 s limit and return validated incumbents | `ml/reports/optimizer_benchmark.md` (48 of 48) |
| Longer planning horizon (8 GW) | serving is capped at the validated 5-GW horizon; extending needs training horizons 0–7 and a re-run of the forecast evaluation for h5–h7 before anything longer is served | `docs/MODEL_CARD.md` → *Served horizon*; `docs/BUILD_STATUS.md` defect 40 |
| Prospective 2026-27 evaluation | the first unseen season; frozen configuration evaluated gameweek by gameweek, drift monitors on live outcomes | selection-bias warning in `ml/reports/backtest.md` |
| Calibrate injury / news availability | the status layer is configuration, not a fitted model | no historical news; capture live status changes first |
| Observe availability alerts on real status changes | alert logic is unit-tested only | `docs/KNOWN_LIMITATIONS.md` §1 |
| League awareness and template exposure in the API/UI | library functions exist, not exposed | `fpl_decision/league.py` |
| Per-decision regret attribution | close the loop between forecast error and decision quality | audit: closed feedback loop |
| Ingestion-level drift gate; baseline-forecast fallback | defence in depth for data and model failures | audit rows (PARTIAL) |
| Alertmanager routing | alerts are visible in Prometheus but not pushed | 12 rules loaded |
| Archival of raw captures and live-observation tables | `/data/raw` and live tables are not pruned | `docs/DEPLOYMENT.md` → *Retention* |
| Live data completeness | players removed from the API are not captured; corrections after 4 days are not fetched | `docs/KNOWN_LIMITATIONS.md` §2 |
| Decision playback walkthrough, demo GIF | portfolio material | audit rows (PARTIAL) |

## V2

| Item | Why |
|---|---|
| Multi-user authentication and per-user authorisation (short-lived tokens) | V1 is single-tenant: one shared key behind the web proxy and one site login |
| Ownership, rank and differential strategy; ownership-adjusted captaincy | needs an ownership / rank objective the engine does not have |
| Browser-extension integration | out of V1 scope |
| Richer UX (explicit product modes, onboarding) | V1 maps modes to screens |
| Further model experiments | after the prospective 2026-27 evaluation, not before |
