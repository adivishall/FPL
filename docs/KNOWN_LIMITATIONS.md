# Known limitations

What this system does **not** do, what has only been checked against snapshots or mocks, and
where its numbers come with caveats (§92). Every item names the evidence; numbers live in the
generated reports so they cannot drift from this page.

## 1. What has been verified against real FPL data — and what has not

| Verified against real data | Evidence |
|---|---|
| Live FPL API (`fantasy.premierleague.com`) reachable from the development machine; `bootstrap-static` and `fixtures` parse against the typed contracts and load canonically (667 players, 380 fixtures, news/availability, official deadlines) | `fpl-ingest live --season 2026-27`; the compose scheduler's hourly `live_refresh` |
| Live squad sync of a public manager entry: squad, bank, free transfers and both chip sets reconstructed; squad equal to the official pre-Free-Hit picks (correct Free Hit reversion) | manual run against the deployed API (data deleted afterwards; identifiers not recorded) |
| Current gameweek and decision cutoff equal the official API's `is_next` event | `GET /api/v1/gameweeks/current` vs `bootstrap-static` |
| Historical seasons 2022-23 – 2026-27 (GW1) from the pinned vaastav commit; the canonical snapshot reproduces byte-identically on macOS and in the Linux container | `snap_b64560a8c4f434ad984e` (`docs/BACKTEST_PROTOCOL.md`) |
| Official points reproduced from components on all 113,870 historical player-match rows | `tests/integration/test_full_scoring_reproduction.py` (slow tier) |
| Walk-forward backtests of three seasons on that data | `ml/reports/backtest.md` |
| Alert rules on real squads, forecasts, schedules, price probabilities and live deadlines | `ml/reports/alerts_audit.md` |

| Only tested on synthetic payloads, fixtures or mocks | Why |
|---|---|
| Injury / suspension / doubt alerts (availability drops) | the archive has no status history; real live changes did not occur during the test window |
| Webhook *delivery* success | only failure paths were exercised against real DNS; no external endpoint was posted to |
| Manager sync for edge cases (mid-season joiners, AFCON top-ups, 2022-23 GW17 rule) | synthetic payloads in `tests/unit/ingestion/test_manager_sync.py` |
| Double-gameweek splitting of live per-match data | not implemented (see 2) |

## 2. Live data pipeline

* **Completed-gameweek results are not ingested from the live API.** The live capture stores
  bootstrap (prices, status, news, ownership) and fixtures, but not `event/{gw}/live` per-match
  statistics: the per-row price/transfer fields of the canonical match table cannot be filled
  point-in-time correctly from that endpoint. Until the pinned historical archive is advanced,
  live forecasts for 2026-27 use match results through GW1 only. This is **not hidden**: the
  freshness block of every response lists "match results missing for finished GW2–GW5" and marks
  the response degraded, and the UI banner shows it.
* Every live refresh exports a full snapshot (~3 MB, hourly) plus, once served, a ~30 MB feature
  cache and a ~7 MB forecast. The hourly `retention` job bounds this (newest 24 kept by default,
  plus the serving, pinned and recommendation-referenced snapshots; `docs/DEPLOYMENT.md`), but
  `/data/raw` (~0.3 MB per capture, the provenance evidence) and the live-observation tables in
  PostgreSQL are not pruned: they need an archival policy before multi-year operation.
* After a live refresh the worker computes on the new snapshot while the API keeps serving the
  previous one until its forecast is precomputed (minutes). A recommendation made in that window
  records — and shows — its own, newer snapshot, which differs from the banner's.
* Prices observed between captures are not modelled; the official price-change predictor is
  shown only when the captured bootstrap carries it and is never blended with the engine's model.
* Source reliability: the FPL API is unofficial and undocumented; schema drift fails the typed
  contracts (the capture is recorded as failed and the last validated snapshot keeps serving).

## 3. Historical data and evaluation

* **Selection bias.** Model structure, hyper-parameters and optimiser defaults were developed
  while looking at 2023-24 – 2025-26. The backtest is walk-forward in *information* (no future
  data reaches any decision) but not free of researcher degrees of freedom. 2026-27 is the first
  genuinely unseen season.
* **Corrected look-ahead.** The previously published backtest (commit `03c14a7`) read the
  archive's final fixture schedule, which leaked postponements. Schedule rule S1 fixes this;
  some earlier verdicts changed (the report lists old vs new). Rule S1 is itself conservative
  (cup-driven blanks are treated as unknown until the original deadline).
* No historical injury news; labels are final (corrected) values; prices between kickoffs are
  unobserved; 2023-24 trains on a single prior season.
* Statistical power: one season is 38 correlated gameweeks. Per-season differences of ±150
  points are routinely inside the 95 % CI; bootstrap CIs treat gameweeks as exchangeable and are
  probably somewhat narrow; 24 comparisons are shown without multiplicity correction.
* Overall rank cannot be evaluated (no score distribution of other managers).

## 4. Forecasting and decisions

* Lineups, minutes and injuries are inherently uncertain; the minutes model is calibrated in
  aggregate (start-probability ECE 0.007–0.023 at horizon 0 in the backtest forecasts) but individual rotation
  calls are frequently wrong, and news-driven availability is configuration, not a calibrated
  model (no historical news to fit it on).
* Point intervals are discrete-valued; the inclusive p10–p90 interval over-covers integer
  outcomes and the strict one under-covers (both reported). Squad-level p10–p90 covers 83 % of
  non-chip weeks (nominal 80 %; 5 % below p10, 11 % above p90): slightly wide, slight upside skew.
* Monte Carlo: 1,000 joint samples per forecast; tail probabilities below ~1 % are noisy.
* Model drift: models are retrained on a schedule and checked with PSI/ECE monitors, but the
  2026-27 live models have not yet been evaluated on 2026-27 outcomes beyond GW1.
* The decision engine's thresholds (`min_gain`, `min_prob_positive`) trade activity for
  stability; plans beyond the first week are provisional (most next-week plans change).
* The "FPL-style heuristic" baseline is an approximation; the official algorithm is not public.

## 5. Optimisation and computation

* MILP solves are exact within a 60 s limit (`mip_rel_gap` 0); a time-limited solve returns a
  polished, independently validated incumbent without an optimality proof. Measured timings,
  timeout frequency and the cost of candidate pooling: `ml/reports/optimizer_benchmark.md`.
* Measured on 48 realistic squad states (`ml/reports/optimizer_benchmark.md`): every chip-open
  solve (all usable chips, 5 GW) ran to the 60 s limit (48 of 48; p50 63.8 s wall) and returns a
  warm-started, validated incumbent rather than a proven optimum; one of 36 8-GW transfer solves
  also hit the limit. Without a warm start and with a 1 s limit, 26 of 48 8-GW chip problems
  found no feasible plan (the engine then falls back to HOLD).
* With every forecast perturbed by 5 % lognormal noise the first-week action stayed the same in
  only 38 % of solves: competing moves are often within forecast noise of each other.
* Candidate pooling (top players by EV / value / cheapest per position) is an approximation; its
  objective cost is measured, not assumed zero (0.000 points lost in 24 of 24 full-universe
  comparisons).
* Timings in `ml/reports/performance.md` and `optimizer_benchmark.md` come from one development
  machine (Apple M4, 10 logical CPUs, 16 GB, macOS); they are not production guarantees. The
  benchmark ran partly on battery power (recorded per run, with CPU time ≈ wall time showing no
  sleep); an earlier attempt that the machine slept through was discarded, not reported.
* Heavy requests are slow: on the development machine a full 5-GW recommendation takes 2–3
  minutes (stability analysis dominates), the replacement picker ~18 s and a 5-GW what-if ~8 s
  (`ml/reports/performance.md`). Recommendations run as worker jobs; the synchronous replacement
  and what-if routes hold an HTTP request open for that long.
* The serving forecast needs ~2 GB of memory while it is computed (worker); it is computed
  asynchronously and the API never trains on a request path in production.
* In-process rate limiting is per API replica; behind the web proxy all browser users share one
  key and therefore one bucket (a busy user can throttle others).

## 6. Security and privacy model

* **Single-tenant.** The manager key identifies stored data; it is not a credential. Anyone
  holding an API key — including every user of the web UI, whose proxy holds the key — can read
  or erase any manager's data by key. Suitable for a personal or trusted-group deployment, not a
  public multi-user service (that needs real user authentication and per-user authorisation).
* No TLS termination in the compose file (documented: put a reverse proxy in front).
* Webhook SSRF defence: HTTPS-only, no credentials, exact host allow-list, port 443, every
  resolved address global (IPv4-mapped and NAT64 forms unwrapped), no redirects, 5 s timeout.
  **Residual risk:** DNS is resolved again by the HTTP client at connect time, so a rebinding
  window exists between validation and connection; the host allow-list is the primary control
  (an attacker would have to control DNS of an allow-listed host).
* Audit entries store hashes of manager and API keys. Logs pseudonymise manager keys in
  rejected-request paths, and uvicorn access logs are disabled (both printed raw manager keys
  until M14). Manager keys still appear in job request rows in PostgreSQL until the manager's
  data is erased (they are application data, not logs).

## 7. Engineering scope not covered

* No automated backups or multi-replica rate limiting (runbook only).
* **Host sleep (development laptops).** When the Mac sleeps, the Colima VM pauses; on wake its
  clock jumps and RQ judges running jobs past their timeout and kills them. Observed overnight
  on the local stack: four jobs were closed as `interrupted` and the next scheduler buckets
  re-ran them. Nothing partial is published, but long jobs on a sleeping laptop can repeat; a
  real deployment (server/VM that does not sleep) is not affected. Long local runs used
  `caffeinate`.
* The release workflow publishes images from tags; it has not been executed against a real
  registry or cloud environment in this project — deployment is verified locally with Docker
  Compose only.
* §43 research extensions (RL policies, rival-response simulation, contextual bandits) are not
  implemented.
