# FPL Decision Engine — V1 release notes

A decision-support system for Fantasy Premier League that makes transfer, captaincy and chip
decisions under uncertainty: probabilistic player forecasts, a multi-gameweek MILP planner with
independent validation, every plan compared with doing nothing, stress-tested and explained from
stored evidence, and every recommendation traceable to its data snapshot, models and source
revision.

## Architecture

```
Browser ─▶ Caddy (TLS, site login) ─▶ Next.js web (server-side /backend proxy holds the API key)
          ─▶ FastAPI ─▶ PostgreSQL · Redis ◀─ RQ worker (forecasts, recommendations, alerts)
                                            ◀─ scheduler (hourly live capture, results, retention)
```

Python 3.12 workspace (domain, storage, ingestion, features, forecasting, simulation, optimizer,
decision, backtesting, notifications) with enforced layering; one engine image for API, worker
and scheduler; Docker Compose with health checks; Prometheus with 12 alert rules. Live data from
the official FPL API (hourly bootstrap, completed-match results), historical data from a pinned
archive commit; evaluation is pinned to its own snapshot and never sees live data.

## Evidence (2026-10-07)

| Check | Result |
|---|---|
| Python fast tier | 384 passed, 0 failed |
| Slow + network tiers (official points on 113,870 rows; live FPL API contracts) | 6 passed |
| Playwright, production topology through TLS and the site login | 19 passed |
| Playwright, development flows | 7 passed |
| ruff, mypy, import-linter, `tsc`, `next build` | clean |
| Backup → restore into empty volumes | 40/40 row-count lines equal; data, traces, worker, scheduler, TLS verified |
| Secret scans | gitleaks over 29 commits: none; container logs: no secrets or raw manager keys |
| Live data | 2026-27 GW1–GW5 results ingested, idempotent, GW1 equal to the archive |

**Backtest** (three seasons, 114 gameweeks, walk-forward, 0 look-ahead violations,
`ml/reports/backtest.md`): the engine beats `hold` (+1905, 95 % CI [+1454, +2381]), `form`
(+742 [+391, +1076]), `fpl_style_heuristic` (+732 [+369, +1094]) and `simple_xp`
(+626 [+311, +956]); chip timing is better overall (+306 [+28, +593]) but not in any single
season; against a one-week optimiser on the same forecast the result is **inconclusive**
(+223 [−126, +584]). The design was iterated on these seasons (selection bias); 2026-27 is the
first unseen season.

**Optimiser** (`ml/reports/optimizer_benchmark.md`, 612 solves): 100 % of 634 returned plans valid;
5-GW transfer solves p50 3.9 s / p95 13.4 s, 8-GW p50 16.0 s / p95 43.5 s; chip-open solves reach
the 60 s limit and return validated incumbents, labelled "not proven optimal".

**Performance** (one machine, `ml/reports/performance_stages.md`): recommendation jobs 42 s
(3 GW) / 90 s (5 GW); 5-GW recommendation 161 s → 105 s with parallel solves, identical output;
replacement picker 12.5–48.9 s; cold forecast 70.6 s, precomputed before a snapshot goes live.

## Deployment status

Verified end to end with Docker Compose on the development machine, including the public TLS
topology (Caddy's internal CA). **Not deployed publicly**: a host, a domain with DNS access, an
ACME e-mail address, a site password and production secrets are needed
(`docs/DEPLOYMENT.md` → *Public deployment*).

## Limitations

Single-tenant (one shared API key behind the web proxy, one site login); heavy requests take
tens of seconds; chip planning is time-limited; availability from news is uncalibrated; the live
FPL API is unofficial; timings come from one machine. Full list: `docs/KNOWN_LIMITATIONS.md`;
open audit items: `docs/FINAL_AUDIT.md` → *V1 RELEASE STATUS*; future work: `docs/BACKLOG.md`.
