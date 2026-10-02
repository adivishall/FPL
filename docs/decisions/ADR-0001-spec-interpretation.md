# ADR-0001: Specification interpretation, conflicts and resolutions

- Status: Accepted
- Date: 2026-10-02
- Context: `FPL Decision Engine — Complete Claude Build Specification` (54 pages) is the
  authoritative contract. Sections 1–48 are the product blueprint; sections 49–94 are the
  implementation-level "Claude Build Pack". Where they disagree, the Build Pack wins because
  §49 states it exists to remove ambiguity from the blueprint.

This ADR records every place where the specification is ambiguous, internally inconsistent,
or cannot be satisfied literally in the build environment, and the resolution chosen. The rule
applied throughout: **choose the most robust engineering interpretation, never a silently
weaker one; when a capability is blocked by the environment, build the real abstraction and
isolate/label the degraded path.**

| # | Topic | Spec text | Problem | Resolution |
|---|-------|-----------|---------|------------|
| 1 | Default planning horizon | §22 "Default horizon: 4 GWs"; §62.1 "Default horizon: 5 GWs" | Direct conflict | **5 GWs** default (Build Pack §62.1). Configurable 1–10; 8–10 = "extended/strategic"; full-season mode is research-only and labelled low-confidence. |
| 2 | Repository layout | §44 (`services/simulation`, `services/decision_engine`) vs §52 (`packages/*`, no simulation/decision dirs) | Two layouts | §52 layout, plus three documented additions that keep §44's separation of concerns: `packages/simulation` (Monte Carlo), `packages/decision` (decision engine/explanations), `packages/storage` (DB + point-in-time access, so `domain` stays infrastructure-free per §53). |
| 3 | API surface | §32 and §72 list overlapping but differently named endpoints | Naming drift | Implement the **union**. Where two names describe one capability (`/replacement` vs `/replacements`, `/optimize/transfer` vs `/optimize`, `/system/health` vs `/health`) both routes exist and share one handler. |
| 4 | "Confidence" | §72.1 numeric `0.76`; §60.3 `"medium"`; §26 "probability/interval, not a fake certainty score" | Undefined quantity | `confidence` = **P(recommended plan outscores HOLD over the horizon)** from paired (common-random-number) Monte Carlo. The label (`low/medium/high`) is a deterministic banding of that probability *and* the perturbation-stability result (ADR-0007). Both are always shown with the p10/p50/p90 gain interval. |
| 5 | Live FPL API | §6.1, §86.1 "A real manager squad can be synchronized" | `fantasy.premierleague.com` is **denied by the build environment's egress policy** (HTTP 403 at CONNECT) | The full client (retry/backoff, raw capture, schema validation, manager-state reconstruction) is implemented and contract-tested against schema fixtures. Live verification is recorded as **blocked by environment**, not as done. The running system uses §82 degraded mode: serve the last validated snapshot with an explicit freshness timestamp. Manual squad entry is provided as a first-class alternative input. |
| 6 | Historical data source | §6, §47 reference vaastav/Fantasy-Premier-League | Weekly updates stopped after 2024-25; 2025-26 complete; 2026-27 has GW1 only | Pin ingestion to an **immutable commit SHA** (reproducibility). Seasons 2022-23 → 2025-26 for training/backtests (first seasons with per-match xG/xA and `starts`); 2026-27 GW1 snapshot provides the latest real decision point (GW2). |
| 7 | Injury/news history | §7 "Use information timestamped before deadline" | No historical per-deadline news/status exists in any reachable source | Historical models are **news-free** and learn availability from observed minutes only. Live availability signals (FPL `status`, `news`, `chance_of_playing_*`) are applied as an explicit, labelled adjustment layer whose mapping is uncalibrated until enough live snapshots accrue (closed feedback loop, §38). Documented in KNOWN_LIMITATIONS. |
| 8 | Price at deadline | §7 "Use price known at simulated time" | Source rows record price at each fixture kickoff (after the deadline) | Price at cutoff = **last observation strictly before the cutoff** (as-of join). Changes between that observation and the deadline (≤ a few £0.1m) are not captured; documented. |
| 9 | Fixture schedule point-in-time | §7 "Future fixture outcome" | Source schedule is the *final* schedule; rescheduled fixtures already sit in their eventual GW | Results are point-in-time (available after kickoff). For the *schedule*, double-gameweek extra fixtures become visible only `announce_lag_days` (config, default 28) before kickoff. Residual leakage on late postponements (blanks) is quantified in the backtest report and documented. |
| 10 | "Official Auto Pick baseline" | §28 | Algorithm is not public | Implement an **FPL-style heuristic** (PPG × availability × fixture factor) and label it as an approximation, never as the official algorithm (§70.3 "where it can be reproduced"). |
| 11 | `xP` column in historical data | — | Upstream documents it is scraped *after* the GW (lookahead) | Never used as a feature or baseline. A data-contract rule flags it as `leakage_risk: high`. |
| 12 | Free transfers after Wildcard/Free Hit | §51.1 silent | Secondary sources agree banked FTs are *retained*; whether +1 accrues that week is ambiguous | Ruleset parameter `chip_ft_policy ∈ {retain, retain_and_accrue, reset}`; 2024-25+ rulesets use `retain`. Marked `verification: secondary-source` in the ruleset. |
| 13 | 2026/27 BPS changes | §51.1 "represented in feature definitions/model documentation" | Full BPS formula changes not reachable from official source | The bonus model is **learned** (event → BPS mapping fitted per season with recency weighting), so it adapts as 2026-27 data accrues; the known changes (CBI 1 BPS per 3, "being tackled" removed, GK save BPS changes) are recorded in ruleset metadata and the model card. |
| 14 | Assistant Manager chip (2024-25 only) | not in spec | Exists historically | Declared `supported: false` in the 2024-25 ruleset; no strategy plays it in backtests (fair across strategies). |
| 15 | Monte Carlo correlation | §12 "Correlation matrix" | Full joint model of all players is intractable | Correlation arises structurally: shared team-goal events (with timing) drive goals, assists, clean sheets, goals conceded and bonus competition inside a fixture. Fixtures are conditionally independent. Empirical correlation matrices are reported from samples. |
| 16 | LLM layer | §81 optional | Requires external API keys; risk of invented reasons | Not enabled by default. Explanations are deterministic renderings of stored evidence objects. A renderer interface exists so an LLM renderer could be added under §81.2 guardrails. |
| 17 | MILP vs CP-SAT | §34, §61 | Either allowed | **MILP via HiGHS** (deterministic, open source, incumbent-on-timeout). See ADR-0007. A brute-force exact solver verifies the MILP on toy leagues (§36 "flagship test"). |
| 18 | Experiment tracking | §34 "MLflow or equivalent" | MLflow is heavy and adds a server | Equivalent: `model_registry` table + content-addressed artifact store + JSON metrics + run configs (ADR-0008). |
| 19 | Milestone order | §87 puts UI (10) before backtesting (11); §50.1 says backtest before trusting recommendations and UI only after stable contracts | Ordering conflict | Backtesting is implemented **before** the API/UI, so the UI's Backtest Lab and README evidence consume real results. |
| 20 | Price Change Predictor | §66 "Ingest the official signal where legally/technically appropriate" | 2026-27 bootstrap exposes `price_change_percent`, `price_change_projections` | Ingested as a source-attributed signal and stored separately from the engine's own calibrated price model; disagreements are recorded, never merged silently. |

## Consequences

- Several acceptance items (live squad sync against the real API) can only be verified once the
  deployment environment permits `fantasy.premierleague.com`. They are tracked as
  "implemented / blocked-in-environment" in `docs/BUILD_STATUS.md` and `docs/FINAL_AUDIT.md`.
- Every resolution above is either a configuration value (versioned and stored with runs) or a
  documented limitation; none is hidden in code.
