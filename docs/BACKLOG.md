# Backlog after V1 (scope frozen at the V1 release)

Nothing here is required for V1. Each item says what it would change and the evidence that
motivates it; V1.1 items build on what exists, V2 items need a new capability. Measurements are
in `ml/reports/performance_stages.md`, open audit items in `docs/FINAL_AUDIT.md` → *V1 RELEASE
STATUS*. The product gap analysis, competitor comparison and the roadmap that re-orders
these items around user value (and adds identity, beta access and analytics as launch-critical)
is `docs/PRODUCT_GAP_ANALYSIS.md`.

**Done since the gap analysis:** M1.1a (Copilot Home and beta foundation — identity, onboarding,
player search, precomputed Home, analytics; `docs/BUILD_STATUS.md` §2c), M1.2 (whole-squad
Transfer Workbench, §2d), M1.3 (result lifecycle and "What changed?", §2e) and M1.4 (decision
journal with prospective outcome evaluation, §2f) and M1.5 (forecast record and Season Review,
§2g). **Next:** the first formal live look (8 final live gameweeks) and the items below, then the
assistant, mini-league intelligence and the extension (`docs/PRODUCT_GAP_ANALYSIS.md` §6).

## V1.1

| Item | Why | Evidence |
|---|---|---|
| Re-simulate only the players a fixture shock affects | the ten Triple Captain / Bench Boost postponement checks re-run the full Monte Carlo (~35 s of a 5-GW recommendation) | stage report: chips 48.9 s, of which re-simulations 34.5 s |
| Reuse historical-season features across hourly snapshots | feature building is 71 % of a cold forecast and rebuilds pinned seasons every hour | stage report: 49.8 of 70.6 s |
| Faster stability analysis | the slowest single perturbation (two exact solves, ~20 s) bounds the parallel stage | stage report: stability 41.8 s with 4 processes |
| Transfer Workbench: chip alternatives and scenario stress in the comparison | the workbench compares HOLD, exact transfers, engine replacements and the optimiser's plans; chip plans and the §69 stress scenarios are still only in the full recommendation package | `docs/KNOWN_LIMITATIONS.md` §5 |
| Transfer Workbench: precompute the default question | runs are keyed by snapshot and forecast and are invalidated on promotion (M1.3), so a new snapshot means a new job on the next visit; the scheduler could precompute HOLD + best per manager after each forecast precompute (as Copilot Home does) | `docs/BUILD_STATUS.md` §2d latency |
| Automatic regeneration after promotion (opt-in) | results are invalidated and explained; the manager regenerates explicitly | `docs/architecture/result-lifecycle.md` |
| Reproducible golden parity in CI | shared x86_64 runners train measurably different forecasts from run to run; the strict golden runs on the development platform only | `docs/BUILD_STATUS.md` §2d defect 52 |
| MILP club cap after an inherited excess | the optimiser may re-increase a club's count up to the initial excess in a later gameweek; the validator rejects such plans, the next-best valid plan is recommended and (since M1.3) labelled "best valid plan among those explored; not proven optimal"; the formulation fix (a per-gameweek allowance that can only fall) with its own validation and golden regeneration is open | `docs/BUILD_STATUS.md` §2d, §2e defect 53 |
| Copilot Home analysis while the worker is ahead of the API | between an export and its promotion a missing analysis is computed on the worker's export and Home shows *computing*; the analysis job could compute on the snapshot the API names (as workbench jobs do) | `docs/architecture/result-lifecycle.md` |
| Replacement picker latency (`POST /replacements`) | 13–49 s on the deployed API, solved serially in the request; the workbench runs the same shortlist as a job instead | `docs/KNOWN_LIMITATIONS.md` §5 |
| Better chip planning | chip-open solves always reach the 60 s limit and return validated incumbents | `ml/reports/optimizer_benchmark.md` (48 of 48) |
| Longer planning horizon (8 GW) | serving is capped at the validated 5-GW horizon; extending needs training horizons 0–7 and a re-run of the forecast evaluation for h5–h7 before anything longer is served | `docs/MODEL_CARD.md` → *Served horizon*; `docs/BUILD_STATUS.md` defect 40 |
| Calibration shadow test (after the first live look) | M1.5 measures calibration only; once 8 live gameweeks are final, a candidate (e.g. the rolling 8-cutoff conformal interval for the displayed range) can be run in shadow mode and adopted only after ≥ 6 later live gameweeks pass the §6 test | `docs/architecture/season-evaluation.md` §11 |
| Replay of 2026-27 GW1–6 (retrospective) | the weeks before the forecast record; a fixed window run once, labelled not prospective, with a per-gameweek data-regime table (GW1 had no live player data at its cutoff; GW2–5 used a stale capture) | `docs/architecture/season-evaluation.md` §15 (L9, S10, SR-04) |
| Live shadow benchmark of the strategies | freeze the seven default strategies' decisions for one reference squad at every checkpoint and score them like the forecasts | §10 of the contract (SR-11) |
| Realised-component breakdown | evaluate the expected-point components (goals, assists, clean sheets, bonus …) against official results | contract §5.5 |
| Availability layer on / off on flagged players | the one forecast layer with no past-season evidence; needs a second simulation per checkpoint | contract §15 (S7) |
| Ingest FPL's own expected points point-in-time | a recognised external baseline needs its own documented, point-in-time ingestion and a comparability study | contract §15 (SR-15) |
| Finality for players FPL drops mid-season | a provisional row of a removed player keeps its gameweek `results_not_final`; a rule change would touch the journal too | contract §15 (L12) |
| Calibrate injury / news availability | the status layer is configuration, not a fitted model | no historical news; capture live status changes first |
| Observe availability alerts on real status changes | alert logic is unit-tested only | `docs/KNOWN_LIMITATIONS.md` §1 |
| League awareness and template exposure in the API/UI | library functions exist, not exposed | `fpl_decision/league.py` |
| Journal: store per-plan outcome samples | evaluations place a result against the stored p10 / p90; keeping each plan's simulated totals would give an exact percentile | `docs/KNOWN_LIMITATIONS.md` §4a |
| Journal: store HOLD's XI in the recommendation package | HOLD's lineup is rebuilt from decision-time expectations for recommendation sources (labelled); the engine already computes it | `docs/KNOWN_LIMITATIONS.md` §4a |
| Journal: record a decision the workbench never evaluated | "something else" is recorded but not scored; evaluating it means submitting it to the workbench first (or a free-form plan builder with the same rules replay) | `docs/KNOWN_LIMITATIONS.md` §4a |
| Per-decision regret attribution | close the loop between forecast error and decision quality (the journal now stores realised vs expected per decision) | audit: closed feedback loop |
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
