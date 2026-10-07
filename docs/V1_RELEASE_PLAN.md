# V1 release plan

Scope-frozen plan for shipping V1 (target window: 15–30 November 2026). Each work session
takes the highest-priority open item below, finishes it (tested, committed), updates this file
and stops. Anything not listed here is V1.1 / V2.

Classes: **A** release blocker · **B** important, ships in V1 as is (documented) · **C** V1.1 / V2 ·
**D** not applicable / environmental · **E** documentation only.

## 1. The 20 non-complete audit requirements (from `docs/FINAL_AUDIT.md`, 2026-10-06)

| # | Requirement | Current state | Risk | V1 decision | Reason |
|---|---|---|---|---|---|
| 1 | Live capture of completed-gameweek results | **done (R1)**: GW1–GW5 ingested from `element-summary` histories; responses no longer degraded | was **High** — live forecasts ignored 4+ gameweeks of form and minutes | **A** | the core product's inputs were stale; also unblocks #4 and prospective evaluation |
| 2 | Ingestion-level drift gate | contracts and quality gates exist; feature/model drift monitored in `governance.py` | Low | C | no correctness impact |
| 3 | Degraded mode: baseline fallback when the forecast model fails | snapshot promotion keeps serving the last good forecast | Low | B | safe behaviour exists; documented |
| 4 | Drift monitoring against live outcomes | monitors implemented and tested; never run on 2026-27 outcomes | Medium (claim) | B | becomes possible once #1 lands; part of the prospective evaluation |
| 5 | Ownership-adjusted captain option | not implemented (expected / safe / high-variance options exist) | Low | C | |
| 6 | Injuries / news → availability calibration | FPL status/chance layer, uncalibrated | Medium, **inherent** (no historical news) | B | documented; calibrate prospectively in V1.1 |
| 7 | Ownership data used by the decision engine | captured, unused | Low | C | |
| 8 | League awareness (head-to-head) | library functions, not exposed | Low | C | |
| 9 | Differential strategy | setting stored, never read | Low | C | |
| 10 | Template exposure | library function, not exposed | Low | C | |
| 11 | Injury / suspension / doubt alerts on real data | unit-tested only | Medium | B | verify on real live status changes captured hourly (cheap) |
| 12 | Webhook delivery to a real endpoint | success path mocked; failure paths real | Low | D | needs an endpoint the operator controls; verify at deployment |
| 13 | TLS in transit | none in Compose | **High for public deployment** | **A** | release-critical for the internet |
| 14 | Short-lived tokens; encryption at rest | static hashed keys; disk encryption is the host's | Medium | D / C | single-tenant V1; host disk encryption documented; tokens come with multi-user auth (V2) |
| 15 | CD (deploy from green main) | workflow exists, never executed | Medium | D | needs a registry/host; V1 deploys with a documented manual procedure |
| 16 | Alertmanager / paging | Prometheus + 12 rules, no paging | Low for V1 | B | optional for a single-operator V1 |
| 17 | Product modes | screens/routes, not explicit modes | Low | E | map modes to screens in docs |
| 18 | Closed feedback loop (decision regret attribution) | post-GW review only | Low | C | |
| 19 | Decision playback example | traceable journal; no curated walkthrough | Low | E | |
| 20 | Demo GIF | screenshots only | Low | E | optional |

## 2. V1 release requirements (blockers and must-dos)

| ID | Item | Class | Status |
|---|---|---|---|
| R1 | Live completed-gameweek results ingested point-in-time correctly, strictly separate from historical evaluation (#1) | A | **done** 2026-10-07 (`docs/BUILD_STATUS.md` §2a) |
| R2 | Interactive performance: profile, then speed up recommendation / replacement / player analysis without changing results | A | **done** 2026-10-07: profiled (`ml/reports/performance_stages.md`); independent solves parallel (5-GW recommendation 161 s → 105 s, identical output); remaining floors documented, deeper work (targeted re-simulation, cross-snapshot feature cache) is V1.1 |
| R3 | Public deployment preparation: TLS reverse proxy, environment docs, migrations/startup, tested backup **and restore**, production smoke test (#13) | A | **done** 2026-10-07: Caddy overlay (TLS, site login), `backup.sh` / `restore.sh` with a restore into empty volumes verified, smoke test through the public origin (`docs/DEPLOYMENT.md`) |
| R4 | Optimiser status shown honestly ("best found within the time limit" vs "proven optimal"); timeouts safe | A | **done** 2026-10-07: API, report and UI label every plan |
| R5 | Evaluation claims: model/configuration freeze + prospective 2026-27 protocol; selection-bias warning kept | B | selection-bias warning kept (README, KNOWN_LIMITATIONS); the prospective 2026-27 evaluation moved to V1.1 (`docs/BACKLOG.md`) |
| R6 | Real-data checks of injury alerts (#11) and drift monitors (#4) on live captures | B | moved to V1.1: needs real status changes and several gameweeks of outcomes |
| R7 | UI walkthrough as a manager; fix confusing or unreliable core flows | B | **done** 2026-10-07: HOLD "0 %" confidence, optimality labels, time-limited candidates; settings races fixed earlier |
| R8 | Final test matrix, regenerated performance report, FINAL_AUDIT / KNOWN_LIMITATIONS / README, CI green, tag | A | **done** 2026-10-07 except the tag (with the public release) — `docs/BUILD_STATUS.md` §2b, `FINAL_AUDIT.md` → *V1 RELEASE STATUS* |
| R9 | Public deployment live at a URL (needs operator inputs: host, domain, DNS, secrets) | A | **blocked on operator inputs** (`docs/DEPLOYMENT.md` → *Operator inputs*) |

## 3. Session log

| Date | Session goal | Result |
|---|---|---|
| 2026-10-06 | Classification (this file); R1 | live results implemented; real run 3,216 rows GW1–GW5; API no longer degraded |
| 2026-10-07 | Finish R1; settings race found by the production suite | settings race reproduced on the pre-fix image and fixed (held-response regression + failed-load test); real-data re-run unchanged; production E2E 18/18, dev 7/7, fast tier 379 passed |
| 2026-10-07 | R2 performance triage | profile: MILP solves ~76 % of a recommendation, feature building 71 % of a cold forecast; parallel independent solves (identical output) 161 s → 105 s; production E2E 18/18 and fast tier 384 passed on the new image |
| 2026-10-07 | Final V1 release pass: R3, R4, R7, R8 | TLS overlay and tested restore; optimality labels; UI fixes; production E2E 19/19 through TLS, dev 7/7, fast 384, slow + network 6; scope frozen (`docs/BACKLOG.md`). Remaining: R9 (operator inputs) |
