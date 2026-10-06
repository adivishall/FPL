# Changelog

User-visible behaviour changes (§78). Milestones follow `docs/BUILD_STATUS.md`.

## M14 — final verification (2026-10-05)

### Fixed
- **Backtest look-ahead (fixture schedule).** Archived seasons hold only the final schedule;
  postponed fixtures were visible to decisions made before the postponement. The point-in-time
  view now shows the schedule as published at each cutoff (rule S1, ADR-0004). All three seasons
  were re-run; earlier backtest numbers are superseded (comparison in `ml/reports/backtest.md`).
- Players who change club mid-season now count against their new club in the state machine and
  the optimiser (previously the club at purchase), so legal plans are no longer rejected.
- Traceability chains stay complete when a newer snapshot has identical features, and live
  recommendations made before their decision cutoff are no longer flagged as broken.
- The optimiser no longer values free transfers, bank or squad value banked past the last
  gameweek of the season, and a time-limited solve's polished incumbent now has its hits and
  objective recomputed (it could under-count hits).
- Live squad sync reports an FPL outage (503) separately from an unexpected payload (502);
  programming errors are no longer reported as "API unavailable".
- Jobs whose worker died (OOM kill, container replaced) no longer block identical requests
  forever; the scheduler reaps them and a new attempt starts.
- A restarted worker container no longer crash-loops on its own stale Redis registration.
- `/health` reports the real outcome of the latest live capture instead of a fixed
  "unreachable" note, and answers in milliseconds (the snapshot id was re-hashed on every call).
- Manager-linked reads (squad, settings, notifications, decisions, recommendations, jobs) now
  require an API key when keys are enabled; uvicorn access logs (raw manager keys in URLs) are off.
- The web proxy returns JSON 502/504 when the API is unreachable or slow (was a bare 500).
- Docker: `/data` is writable by the non-root user on a fresh volume; worker and scheduler have
  their own health checks; services restart automatically.

- Two managers with identical squads no longer share one recommendation record (the second
  manager saw none; erasing the first removed it).
- Worker liveness probe no longer reports a working idle worker as dead (RQ heartbeats only
  every ~405 s while idle; RQ's worker set can lose a live worker after the host sleeps).
- Forecast and price caches, serving markers and snapshot manifests are published atomically:
  a process killed mid-write no longer leaves a truncated file behind.
- Logs of rejected requests no longer contain manager keys from URL paths.
- Malformed webhook URLs are rejected (422) instead of failing with a 500.
- Explanations name players, teams and chips instead of showing codes and list literals.
- Backtest Lab renders reports (tables, headings, figures in place) instead of raw Markdown, and
  shows every figure a report references (the 2023-24 and difference charts were missing).

### Added
- Retention of live snapshots and their caches (hourly `retention` job; configurable).
- Failed jobs are classified as `error` (code raised) or `interrupted` (worker killed, lost or
  timed out); `fpl_jobs_failed{kind,failure}` and a separate `JobInterruptions` alert.
- Freshness reports finished gameweeks whose match results are missing.
- Snapshot promotion: the API switches to a newer snapshot only once its forecast is ready.
- Worker metrics (recommendations, optimisation) are exported from the forked job processes.
- Resumable, checkpointed backtests pinned to a configured snapshot; three-season report with
  forecast calibration, interval coverage, start-probability reliability, chips, stability,
  accounting checks and reproducibility; optimiser benchmark on realistic workloads; serving
  performance report; alert rules replayed on real data; production-topology Playwright suite.

## M13 — production hardening (2026-10-02)
Notifications with materiality thresholds and webhooks (SSRF-guarded), privacy export/erasure,
Prometheus metrics and alert rules, recommendation traceability, RQ worker and scheduler, Docker
deployment with a server-side web proxy.

## M12 — web UI
Decision-first Next.js UI: overview, transfer lab, squad and future planners, player lab,
what-if, journal, backtest lab, data health, settings.

## M11 — API
FastAPI service, async jobs, persistence, API keys and rate limits.

## M6–M10 — optimiser, decision engine, backtesting
MILP transfer/chip planner with independent validation, replacement engine, HOLD-vs-move
decisions with paired Monte Carlo evidence, scenarios, walk-forward backtesting.

## M0–M5 — foundation, data, rules, forecasting
Versioned rulesets, point-in-time data platform, scoring engine, probabilistic minutes and
points forecasts with calibration and a model registry.
