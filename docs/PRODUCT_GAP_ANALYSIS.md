# FPL Copilot — product gap analysis, competitor comparison and roadmap

Written 2026-10-10 against branch `claude/modest-noether-qauiji` at `ead2d3d` (V1 release
candidate, verified locally, not yet deployed publicly). Evidence comes from reading the
repository (file references below are relative to the repository root), the V1 reports in
`ml/reports/`, and the competitor sources listed at the end. Competitor capabilities are marked
**verified** (seen on the vendor's own page or documentation, with a source) or **assumed**
(inferred from secondary coverage or marketing). Effort figures are rough estimates in
engineer-weeks for one engineer who knows the codebase; they are planning inputs, not promises.

**Summary.** The engine is the strongest part of the product and the parts around it are the
weakest. Forecasting, joint simulation, a validated multi-gameweek optimiser, paired comparison
against HOLD, stability analysis, evidence-linked explanations and full traceability all exist
and are tested; no competitor surveyed shows uncertainty, a HOLD baseline and a reproducible
trace together. What a manager meets first, however, is a squad entered as fifteen numeric
codes, a decision card that arrives after 42–90 s, a "Transfer Lab" that handles one player at
a time, alerts that never change a plan's status, a journal that records "followed" but not
what happened next, no accounts, and no assistant. The recommended first milestone is a
**Copilot Home with FPL-ID onboarding, a precomputed squad analysis and a restricted-beta
access layer** (M1.1a below): it removes the biggest onboarding and latency barriers, touches no
forecasting or optimiser code, and creates the identity layer every later milestone needs.

**Status (2026-10-10):** M1.1a is implemented and verified locally (`docs/BUILD_STATUS.md` §2c);
the next milestone is the Transfer Workbench (§6, M1.1d scope first because it is the core weekly
decision), then invalidation and the journal.

One naming risk first: **"FPL Copilot" is an existing product** (fplcopilot.com, "AI-Powered
Fantasy Premier League Optimizer", with a solver, chip strategies and mini-league pages). The
product should ship under a different name; this document keeps "Copilot" as a working label
only.

---

## 1. What exists today (one page)

| Vision capability | Status | Evidence |
|---|---|---|
| Probabilistic forecasting and constrained optimiser | **Exists**, tested | minutes/rates/team-strength models (`packages/forecasting`), joint Monte Carlo (`packages/simulation`), HiGHS MILP with independent validation (`packages/optimizer`); 384 fast tests, optimiser benchmark 100 % valid plans |
| Fast, understandable squad analysis | **Partial** | `/squad` shows XI, bench, captain profiles and a per-GW xP grid (`apps/web/app/squad/page.tsx`); no weakness or per-position analysis, no fixture-difficulty view (difficulty is ingested, `packages/storage/.../models.py:251`, never used); squad entry is fifteen numeric codes |
| Whole-squad transfer and replacement planning | **Partial** | replacement picker takes exactly one player out (`packages/decision/.../replacement.py:137-160`); the recommendation compares N plans and HOLD (`engine.py:328-609`); locks, bans, forced moves and hit caps exist in `Preferences` (`fpl_optimizer/problem.py:152-161`) but the web never exposes them |
| Captaincy and bench | **Exists** | paired re-scoring per captain with expected/safe/high-variance profiles (`captaincy.py`); bench and vice from the lineup solver; shown on `/squad` and the overview |
| Chip and transfer-hit analysis | **Exists**, limited | TC/BB paired re-scoring with postponement checks, WC/FH re-solves per week (`chips.py:230-282`); hit cost in the objective (`milp.py:268,609`); chip-open solves always hit the 60 s limit and are labelled "not proven optimal" |
| Personalised, evidence-grounded assistant | **Missing** | no LLM, chat or assistant code; evidence items and primary drivers exist (`evidence.py:90-245`) and the markdown explanation is stored (`render.py`) |
| Live intelligence and recommendation invalidation | **Partial** | freshness block on every response, degraded banner on 5 of 13 pages; `invalidation` alert exists (`apps/api/src/fpl_api/alerts.py:230-306`) but the `invalidated` status is never set, `/recommendations/current` never compares snapshot ids, and nothing re-plans automatically |
| Mini-league / rival strategy | **Library only** | `league.py` (`head_to_head`, `template_gaps`, `plan_choice_vs_rival`) is imported only by a unit test; `league_rivals` and `differential_tolerance` settings exist in the schema and are never read; the classic-league standings client has no caller |
| Decision journal with prospective evaluation | **Partial** | `/decisions` with followed/ignored/partial, note and user-typed `realized_points`; `realized_hold_points`, `forecast_error` and `review_json` columns are never written; the post-gameweek alert computes realised vs expected but does not persist it; no prospective evaluation of the live season |
| Chrome extension | **Missing** | nothing in the repository; the backend has every read endpoint an overlay would need, but no per-user credential an extension could hold |
| Accounts, beta access, product analytics | **Missing** | one operator API key, free-form `manager_key` (web default `"demo"`), anyone behind the site login can read or erase any manager; no usage analytics of any kind (Prometheus only) |

Two documentation inaccuracies found on the way, to fix in the first milestone: `KNOWN_LIMITATIONS.md` says PSI/ECE drift monitors run, but `governance.py` has no runtime caller (gates run only in `ml/experiments`); the CI workflow header promises a "backtest smoke" job that does not exist. Two engineering notes: models are retrained for every new snapshot (hourly) rather than on the 4-GW cadence used in evaluation (`services.py:331-335`), and the serving horizon is 8 while training horizons are 0–4 (`pipeline.py:47`, `settings.py:41`), so horizons 5–7 extrapolate the horizon feature. Neither blocks the roadmap; both belong in the model card.

---

## 2. Capability-by-capability gap analysis

Each capability answers the ten questions from the mandate. "Exists / Partial / Missing" cite
code; "Differentiation" is honest about where competitors already are.

### C1. A single, coherent Copilot experience (priority 1)

| | |
|---|---|
| **Exists** | Decision-first overview with action, confidence = P(plan beats HOLD), Why (drivers tied to evidence ids), downside scenarios, HOLD comparison, plan timeline and assumptions (`apps/web/app/page.tsx:77-178`); planner, transfers, what-if, players, alerts, journal, backtests, health, settings pages; freshness banner |
| **Partial** | The experience is a set of pages around a stored recommendation, not one flow. Onboarding is "Get started → type 15 player codes" (`squad/page.tsx:97-108`); the FPL-ID sync endpoint `/squad/sync` exists (`app.py:470`, `manager_sync.py:49`) but the squad page hard-codes "Live sync … unavailable" (`squad/page.tsx:98`). Profiles (default / conservative / aggressive, `config/optimizer/*.yaml`) are a settings dropdown with no explanation of what changes |
| **Missing** | A home that answers "what is my situation, what should I do, what changed" in one screen; a player picker/search; any per-user identity (see C12); product analytics |
| **Target user / problem** | Every manager, every gameweek: "I have ten minutes before the deadline; tell me where I stand and what to do, and let me check why." New managers: get from an FPL ID to a first recommendation without reading docs |
| **Differentiation** | Official FPL shows the squad and (new in 2026/27) live ranks, and the Microsoft-built Companion answers questions but "will not predict FPL points" (verified). Fix, Hub, Scout and FPL Review each have a planner plus projections (verified) but present point estimates; none surveyed shows P(beats hold), intervals and the evidence chain on the same card. The difference is credible only if the card is reached in seconds, which is the gap |
| **Data** | Official FPL entry endpoints (`entry`, `entry/history`, `entry/transfers`, `event/{gw}/picks`, client in `fpl_ingestion/fpl_api.py:94-120`): public, unofficial, one request per second, picks visible only after the deadline; private "my-team" (pre-deadline picks) needs the user's own login and is not used |
| **Dependencies** | C12 (identity) for anything per-user; precomputation (C2) for latency; the existing `/squad/sync` path (returns 503 in degraded mode) |
| **Effort** | 3–4 weeks for Copilot Home + FPL-ID onboarding + picker (inside M1.1a) |
| **Acceptance / tests** | A new user with an FPL ID reaches a stored recommendation in under 2 minutes wall-clock (Playwright, production topology); Home renders from precomputed data in < 1.5 s p95 warm; every page shows the degraded banner when freshness is stale; engine outputs unchanged (golden package parity test) |
| **Success metric** | Onboarding completion (FPL ID → first recommendation) ≥ 70 % of new beta users; Home p95 latency; weekly returning users |

### C2. Fast, context-aware squad analysis (priority 2)

| | |
|---|---|
| **Exists** | Per-player xP, p10/p90, P(start) per GW for the squad (`/squad` grid); player detail with xP components, recent matches, prices, status and news text, engine vs official price-change probabilities (`players/[code]/page.tsx:83-99`); evidence items including opponents' xG conceded; set-piece orders and chance-of-playing in `player_snapshots` (`models.py:264-287`) |
| **Partial** | Squad-level context is scattered: no "weakest slots", no per-position comparison against the replacement level or the template, no fixture run view (FPL difficulty ingested, unused), no bench-strength or bank-usage read-out; status is in the players payload but not rendered on `/players`; price-change risk is only on the player page and in alerts |
| **Missing** | A squad analysis read model computed once per snapshot per manager; template/ownership context (ownership is a model feature, never shown as context); a players list with search, filters by position/club/price and sortable xP (the API supports filter and sort, `app.py:276`) |
| **Target user / problem** | "Is my squad in good shape, where is it weak, and what is about to go wrong (injury, price, fixtures)?" in one look |
| **Differentiation** | Rate My Team (Scout) and AI Team Rating (Fix, Hub) give a score and projected totals (verified); FPL Plus and Touchline overlays show EO and ownership tiers on the FPL site (verified). Ours can show per-slot expected points with intervals, P(start), the availability adjustment actually applied, and the fixture run, each linked to evidence — a diagnosis rather than a score |
| **Data** | Bootstrap snapshot (hourly) with status/news/chance/set pieces; fixtures with difficulty; ownership % (`selected_by_percent`) per snapshot. Limits: `transfers_in_event/out_event` are parsed but not stored (`fpl_api_schemas.py:64,68`); the availability multiplier is uncalibrated (`minutes.py:243-280`); no odds, lineups or press-conference feeds |
| **Dependencies** | Serving forecast cache (already precomputed per snapshot, `services.py:271-343`); a worker job to materialise the read model after each forecast; identity to key it per user |
| **Effort** | 2–3 weeks (read model + Home panels + players list), inside M1.1a |
| **Acceptance / tests** | Read model is a pure function of (snapshot, forecast, manager state) with unit tests on fixtures; contents match the recommendation package's own numbers (parity test); Home shows squad analysis within 1.5 s p95; a regression test proves the forecast cache is reused (no extra model training) |
| **Success metric** | Home p95 latency; share of sessions that open at least one evidence detail; qualitative beta feedback on "I understand my squad" |

### C3. Whole-squad transfer and replacement comparisons against HOLD (priority 3)

| | |
|---|---|
| **Exists** | Recommendation = MILP plan + alternatives + explicit HOLD, paired Monte-Carlo gains, thresholds per profile (`engine.py:372-389`); replacement picker for one player out with gain 1 GW/horizon, p10/p90, P(>0), hit cost and follow-up moves; what-if single sell/buy vs HOLD (`/scenarios`); hit cost from the ruleset inside the objective |
| **Partial** | One player out only; no "0 vs 1 vs 2 transfers (with hit)" ladder; no multi-out replacement (two injuries); `Preferences` (locks, bans, forced in/out, max transfers, hit cap) exist but the web never calls `/optimize` with them; the replacement picker runs serially inside the request (12.5–48.9 s measured, `ml/reports/performance_stages.md`) |
| **Missing** | A whole-squad view: for each of the weakest slots, the best replacement and its paired gain vs HOLD; a transfer-count ladder; precomputed shortlists so the comparison is instant for the common cases |
| **Target user / problem** | "Should I move at all this week, and if so one move or two, which, and is a hit worth it?" — the central weekly decision |
| **Differentiation** | FPL Review's Transfer Solver and Linear Optimiser produce multi-week plans with chip timing and sensitivity analysis (verified); Fix's Assistant Manager proposes one move per gameweek (verified); Hub's AI transfers have conservative/optimised/aggressive styles with seven parameters and exclusions (verified). Our edge is the paired comparison with intervals and P(beats hold) for every option, validation of every plan, and the explicit "best found within the limit" label. The ladder and multi-out are table stakes we lack |
| **Data** | Same forecast; prices are held constant within the horizon (`engine.py:507`), so hit-vs-price-rise trade-offs are not modelled |
| **Dependencies** | Parallel solves in the API path bounded by `FPL_SOLVER_WORKERS` (currently 1 in the API, `docker-compose.yml:45`); precomputation job per manager (weakest 3 slots × shortlist); job dedupe already caches succeeded jobs per snapshot (`jobs.py:193-203`) |
| **Effort** | 4–5 weeks (M1.1d) |
| **Acceptance / tests** | Ladder and multi-out plans pass the independent validator; paired gains reproduce the recommendation's numbers for the chosen plan; precomputed shortlist equals the live picker's top-5 for the same snapshot (parity test); p95 < 2 s for a precomputed slot, < 20 s for an ad-hoc out |
| **Success metric** | Share of recommendations where the user opens an alternative; "followed" rate by option type; realised regret vs HOLD once C10 exists |

### C4. Captaincy and bench recommendations

| | |
|---|---|
| **Exists** | Captain profiles expected / safe (best p25) / high-variance (P(≥ 13)), vice from the lineup solver, bench order from the lineup optimiser, shown on the pitch; tested end to end (production Playwright test 5) |
| **Partial** | No ownership-aware ("differential captain") option; the decision card shows one captain without the profile comparison |
| **Missing** | Rank-objective captaincy (needs ownership modelling, V2) |
| **Target user / problem** | Deadline-day armband choice; bench order when rotation is likely |
| **Differentiation** | Scout's captaincy matrix and Touchline's captain suggestions with xP are verified; the distribution-based profiles (safe vs ceiling) are the differentiator and already exist |
| **Data / dependencies / effort** | None new; surface the profile comparison on Home (≤ 1 week, inside M1.1a) |
| **Acceptance / metric** | Captain profiles on Home match `/lineup`; click-through to the comparison |

### C5. Chip and transfer-hit analysis

| | |
|---|---|
| **Exists** | Chip plan with "play now / wait / keep" and P(best week), postponement sensitivity, forced-chip what-if (`/chips/simulate`); hit valuation inside the objective; backtest evidence that chip timing adds +306 points over three seasons (CI [+28, +593]) but not in every season |
| **Partial** | No value estimated beyond the horizon (`chips.py:1-18`), so "keep" is conservative; chip-open solves always reach the 60 s limit (48/48 in the benchmark); the Assistant Manager chip is excluded (`chips.py:113`) |
| **Missing** | A plain "hit calculator" view (gain vs −4 with the interval) separate from the full recommendation; chip plan versus rivals' chips (C9) |
| **Target user / problem** | "Is this the week for Bench Boost / Triple Captain; is a −4 worth it?" |
| **Differentiation** | FPL Review includes chip timing in its solver and FPL Copilot "tests every valid chip plan" (both verified claims, methods unverified). Our postponement sensitivity and the honest optimality label are unusual; the horizon limit is a weakness against a 14-GW competitor horizon |
| **Effort** | Hit view ≤ 1 week (M1.1d); better chip planning is V1.1 backlog (re-simulate only affected players, warm starts) |
| **Acceptance / metric** | Hit view numbers equal the recommendation's paired gains; chip-plan solve p95 after optimisation < 30 s |

### C6. Personal strategy profiles and scenario analysis (priority 4)

| | |
|---|---|
| **Exists** | Three optimiser profiles (discount, risk aversion, transfer penalty, thresholds); scenario kinds expected / minutes up-down / injury shock / team-attack downside / fixture shock / price shock / conservative (`scenarios.py:26-43`); default stress set on every recommendation; what-if page for one scenario and one move |
| **Partial** | Profiles are global knobs, not a manager's stated strategy (e.g. "protect rank", "chase", "no hits before GW20", "avoid rotation risk"); `differential_tolerance` is stored but unused; scenarios are not editable beyond one kind and magnitude; no comparison of two full plans under a chosen scenario |
| **Missing** | A strategy profile that maps stated intent to `Preferences` plus profile parameters and is shown back in plain language on every recommendation ("because you asked for no hits, …") |
| **Target user / problem** | Managers who distrust generic advice because it ignores their league position, risk appetite or rules of thumb |
| **Differentiation** | Hub's three styles with seven parameters and FPL Review's free-transfer value are verified; mapping stated intent to constraints and explaining the effect back is not seen elsewhere (assumed) |
| **Dependencies** | Settings schema already has the fields; engine already accepts `Preferences`; needs identity to persist per user |
| **Effort** | 2 weeks (M1.1d) |
| **Acceptance / tests** | Each profile option changes the plan in the documented direction on fixture cases (unit tests); the recommendation text names the active constraints; regression: the default profile output is unchanged |
| **Success metric** | Share of beta users who set a profile; "followed" rate for profiled vs default recommendations |

### C7. Personalised, evidence-grounded assistant

| | |
|---|---|
| **Exists** | Everything an assistant should ground on: the recommendation package (decision, options, drivers with evidence ids, scenarios, chips), the markdown explanation, player detail with status and news text, trace endpoints, journal |
| **Missing** | Any LLM component; a question interface; guardrails |
| **Target user / problem** | "Why not X instead of Y?", "what if Z is injured?", "explain this in my terms" — follow-up questions the fixed card cannot answer |
| **Differentiation** | ChatFPL (Fix, since 2024) and the official Companion (Microsoft, 2026) are verified conversational assistants; a 2024 review of ChatFPL recorded it recommending a striker who had already been sold. The Companion explicitly does not predict points. Our assistant can be grounded on a stored, traceable package: every numeric claim it makes must come from an evidence id or a tool call, and it must say "not in my data" otherwise. That grounding, not the chat, is the differentiator |
| **Data** | Only the snapshot and package: the assistant must never introduce news or lineups it has not been given; news text is the FPL news string only |
| **Dependencies** | Identity and per-user rate limits (cost control); read-only tool endpoints; an evaluation set of questions with expected grounded answers; provider choice and cost caps |
| **Effort** | 4–6 weeks (M1.1e), including the evaluation harness |
| **Acceptance / tests** | Offline eval: ≥ 95 % of numeric statements traceable to an evidence id or tool result; 0 fabricated player status in a red-team set; refuses out-of-snapshot questions; latency p95 < 8 s; every answer stored with its tool calls for audit |
| **Success metric** | Assistant sessions per active user; thumbs-up rate; fabrication rate from audits (target 0) |

### C8. Reliable live intelligence and stale-recommendation detection (priority 5)

| | |
|---|---|
| **Exists** | Hourly bootstrap capture, completed-match results from element-summary, freshness status fresh/stale/expired on every response, degraded mode with reasons; alert rules for deadline, squad change (status, chance, P(start), benching), price risk, fixture change, invalidation (plan loses ≥ 2 pts or P(beats hold) < 0.5), post-gameweek (`services/notifications/.../rules.py`); webhook and in-app channels |
| **Partial** | Invalidation only creates a notification: the recommendation keeps status `active` (`invalidated` never set, `services.py:488-496`); `/recommendations/current` does not compare its snapshot with the current one; the GW only advances when a snapshot arrives; the degraded banner is missing on transfers, planner, what-if, journal and alerts; the invalidation check costs 3 MILP solves per manager every 30 minutes (`alerts.py:266-269`); no scheduled re-sync of the manager's entry after a deadline |
| **Missing** | A "what changed since your plan" diff; automatic re-plan when invalidated or when a new GW starts; live points during matches (`event/{gw}/live` is not captured); push/e-mail channels |
| **Target user / problem** | Trust: a plan must say when it is out of date and offer the new one; a manager must never act on a Friday plan after Saturday's team news |
| **Differentiation** | Live rank is now official and LiveFPL/OneFPL add context (verified); nobody surveyed ties freshness to the recommendation itself. Plan-level invalidation with a reason and a re-plan is the differentiator (assumed) |
| **Data** | Hourly bootstrap; FPL news string with `news_added`; live endpoint not ingested; deadline from the live API |
| **Dependencies** | Worker capacity (re-plans are 42–90 s each); identity for per-user channels |
| **Effort** | 3–4 weeks (M1.1b) |
| **Acceptance / tests** | Status machine active → invalidated → superseded with unit tests; a Playwright test: change a squad player's status in a fixture snapshot, see the banner, the reason and the new plan; degraded banner on all pages; no plan older than the current snapshot shown without a warning |
| **Success metric** | Median time from snapshot to a refreshed plan; share of invalidations acknowledged before the deadline; false-invalidation rate from audits |

### C9. Mini-league and rival-aware strategy (priority 8)

| | |
|---|---|
| **Exists** | Library functions `head_to_head`, `template_gaps`, `plan_choice_vs_rival`, `lineup_points` (`league.py`, test-only); classic-league standings client (`fpl_api.py:122`, no caller); ownership % per snapshot; entry picks fetchable after the deadline |
| **Missing** | Any ingestion of rivals, any UI, any objective that knows about rank or ownership (plans maximise points) |
| **Target user / problem** | The majority of managers care about one or two mini-leagues: "what do my rivals have that I don't, what do they have left (chips, transfers), what swings the league?" |
| **Differentiation** | Fix (league upload, differential transfers, predicted rival transfers), LiveFPL (Live Battle), OneFPL (captain differences, players remaining, provisional bonus), FPL Copilot (standings with chips, FTs and win probability) and Touchline (EO shirt tints) are all verified. This is a crowded area; our only credible edge is combining rival context with forecast uncertainty (P(I finish above X) under the current plans), which needs a rank objective that does not exist |
| **Data** | Classic-league standings (public), rival picks after each deadline (public), chips used from entry history; rivals' pre-deadline moves are unknowable |
| **Dependencies** | Identity; scheduled rival ingestion (1 req/s policy); a league read model; later, an ownership/rank objective (V2) |
| **Effort** | Read-only rival view 3 weeks (V1.2); rank-aware optimisation 6+ weeks (V2) |
| **Acceptance / tests** | Rival view reproduces official standings and picks for a fixture league; P(finish above rival) computed from paired simulations with tests; rate-limit compliance test |
| **Success metric** | Share of users who connect a league; return rate on match days |

### C10. Decision journal with prospective evaluation (priority 6)

| | |
|---|---|
| **Exists** | Journal rows with followed / ignored / partial, note, user-typed realised points; lineage trace with integrity checks per decision (`journal/[id]`); post-gameweek alert computing realised vs expected (`alerts.py:314-390`); offline calibration metrics (Brier, log loss, ECE, CRPS, PIT) in `metrics.py` and reports |
| **Partial** | Outcomes are typed by hand and not shown; `realized_hold_points`, `forecast_error`, `review_json` never written; calibration metrics are static report values exported to Prometheus, not computed on the live season; drift functions have no runtime caller |
| **Missing** | Automatic scoring of every recommendation after the GW is finalised: realised points of the plan, of HOLD (counterfactual from the stored squad), regret, and whether the user followed it; season-to-date calibration of P(start), minutes and points on the live season; a season review page |
| **Target user / problem** | Trust over a season: "was the advice good, and is the model still calibrated?" Also the project's own evidence problem: 2026-27 is the first unseen season and the backtest has selection bias |
| **Differentiation** | Premier Fantasy Tools tracks captain points, transfer cost and bench points and has a Transfer Analyzer (verified listing, method unverified); no surveyed tool replays "held vs transferred" with pre-deadline information. Prospective, pre-registered evaluation is unique here (assumed) and is also the strongest honest marketing the product can have |
| **Data** | Results already ingested per GW; stored squads and plans; needs the manager's actual picks after each deadline to measure "followed" automatically (entry picks endpoint) |
| **Dependencies** | Results ingestion (exists); scheduled entry re-sync; identity |
| **Effort** | 3–4 weeks (M1.1c) |
| **Acceptance / tests** | For a fixture season, the scorer reproduces official points for plan and HOLD exactly (reuse the official-points reproduction test); regret is 0 for a followed HOLD; calibration metrics on live GWs match the offline implementation on the same inputs; the journal shows outcome columns |
| **Success metric** | Cumulative realised gain vs HOLD for followed recommendations; live-season ECE and PIT coverage within tolerance; share of recommendations with an outcome within 24 h of GW finalisation (target 100 %, automatic) |

### C11. Chrome extension integrated into the FPL workflow (priority 7)

| | |
|---|---|
| **Exists** | Backend read endpoints for everything an overlay would show; the web proxy pattern that keeps the API key off the browser |
| **Missing** | The extension; a credential an extension can hold (today the only key is the operator key behind the web proxy) |
| **Target user / problem** | Managers who make their decisions on fantasy.premierleague.com and will not switch sites: show xP with intervals, P(start), the active plan and "this breaks your plan" next to the official pitch |
| **Differentiation** | FPL Plus (Fix) overlays projections, price predictions, FDR and ownership tiers; Touchline overlays EO, captain xP and price alerts; FPL Comrade adds mini-league columns (all verified). Overlaying uncertainty and plan status is new; overlaying projections is not |
| **Data** | Reads the entry id from the page; everything else from our backend |
| **Dependencies** | **C12 identity with revocable per-user tokens**, CORS and rate limits per token, a stable versioned read API, Manifest V3 review, and a privacy policy; the FPL site's DOM is unversioned and changes each season |
| **Effort** | 4–5 weeks for a read-only overlay (V1.2), after identity |
| **Acceptance / tests** | Overlay shows the same numbers as the web for the same snapshot (contract tests against a recorded DOM); token revocation works; no FPL credentials ever touched; extension builds and lints in CI |
| **Success metric** | Installs among beta users; daily active overlay sessions; uninstall rate |

### C12. Platform prerequisites: restricted beta, identity, analytics

| | |
|---|---|
| **Exists** | Site login (one shared Basic-auth user), operator API key, SSRF-guarded webhooks, export and erasure with an audit log, in-process rate limits |
| **Missing** | Users, sessions, invite codes, binding of a manager key to a user, per-user rate limits, first-party product analytics, a privacy notice for beta users, worker capacity planning for N users (one recommendation = 42–90 s of CPU) |
| **Why launch-critical** | The mandate forbids exposing the single-user setup as an unrestricted multi-user service; every priority-1 to priority-8 feature is per-user; the success metrics cannot be measured without events |
| **Design (smallest safe)** | Invite-code sign-up (e-mail + magic link, no passwords stored), server session cookie, `users` and `user_managers` tables, every manager-scoped route checks ownership, per-user token buckets, an append-only `product_events` table written by the web server (no third-party analytics), erasure extended to users. Short-lived API tokens for the extension come in V1.2 |
| **Effort** | 3 weeks (inside M1.1a) |
| **Acceptance / tests** | Cross-user access tests (user A cannot read, plan for or erase user B's manager); invite limits; session expiry; events written for the funnel; existing production Playwright suite passes with a logged-in user; load test: 20 concurrent users requesting recommendations do not starve the scheduler |

---

## 3. Competitor comparison

Sources are listed at the end; "V" = verified on the vendor's own page or documentation, "A" = assumed from secondary coverage or marketing. Prices change often and were read in October 2026.

| Product | Projections | Planner / solver | Chips & hits | Uncertainty shown | AI assistant | Live / rivals | Extension / apps | Decision tracking | Evidence trail | Price |
|---|---|---|---|---|---|---|---|---|---|---|
| **Official FPL + Companion** (Microsoft, 2026/27) | none ("will not predict FPL points") V | none | none | none | conversational Q&A, suggests replacement options with data and editorial links V | live overall/league ranks in the official game (2026/27) V | official apps V | none | none | free V |
| **Fantasy Football Scout** (+ Premier Fantasy Tools) | RMT points projections, season and 6-GW; "averages" V | Transfer Planner (app / PlanFPL), Rob's planner V | planner shows before/after totals for a hit V | none (averages) V | none named V | live rank tracking V; predicted line-ups V | FFScout app V | Team Analyzer (captain pts, transfer cost, bench) V; Transfer Analyzer (detail unverified) A | none | Chief Scout £3.25/mo billed annually V |
| **Fantasy Football Fix** | Points Projections V | Future Planner V; Assistant Manager: one recommended move per GW V; bench and captain advice A | planner A | none V | ChatFPL (since Jul 2024) V; 2024 review saw it recommend an already-sold player V | league upload: differential transfers, live mini-league rank, league ownership, predicted rival transfers V | FPL Plus Chrome extension (projections, price predictions, FDR, ownership tiers on the FPL site), iOS/Android V | none | none | free tier + Premium (price not shown) V |
| **FPL Review** | Massive Data Model: ML + betting odds, 14-GW horizon, hourly refresh V; free model 3 GW V | Transfer Solver (search, multi-period, chip timing, sensitivity analysis) and Linear Optimiser (MILP) V | yes V | sensitivity analysis of plans V | none V | none V | none V | none | none | Patreon tiers V |
| **Fantasy Football Hub** | Opta-based regression projections V | AI transfers with conservative/optimised/aggressive styles, 7 parameters, exclusions, chip plan factored V | yes V | none V | none named V | live mini-leagues V | iOS/Android, screenshot import V | none | none | Starter £4.98 / Pro £7.98 / Ultra £29.98 per month billed yearly V (tiers vary by page) |
| **LiveFPL / OneFPL** | none | LiveFPL transfer planner A | none | none | none | live rank, Live Battle, EO (LiveFPL) V; captain differences, players remaining, provisional bonus (OneFPL) V | LiveFPL iOS V | none | none | free / in-app purchases V |
| **FPL Copilot** (fplcopilot.com) | expected points V | solver by Team ID V | "tests every valid chip plan" V | none V | "Copilot" page (content unverified) A | standings with chips, FTs, win probability; differentials V | none seen | none | none | leagues free, rest not stated V |
| **Overlay extensions** (Touchline/FPL Elite HUD Pro, FPL Comrade, myfpl) | captain xP (Touchline) V | none | none | none | none | EO tints, elite ownership, mini-league columns V | Chrome / Firefox V | none | none | free / paid (myfpl) V |
| **This product (V1)** | decomposed probabilistic forecast, 1,000-sample joint simulation, 8-GW horizon | MILP, validated plans, alternatives, HOLD baseline, stability analysis | chips with postponement checks; hits in objective | p10/p90, P(beats HOLD), stability label, optimality label | none | none | none | journal (followed/ignored), trace | evidence ids → snapshot → models → source revision | self-hosted, no price |

**What is genuinely different** (and should be the product's spine):

1. **Uncertainty as the interface.** Every number carries an interval and every move a P(beats HOLD). Closest competitor: FPL Review's sensitivity analysis (verified), which tests plan robustness but does not present per-option probabilities.
2. **HOLD is a first-class option with thresholds.** OneFPL argues for a no-change baseline (verified blog), FPL Review has a free-transfer value knob (verified); none shows the paired distribution of "move minus hold".
3. **Validated, honestly labelled plans.** Independent validation of every returned plan and "best found within the limit" labels; FPL Review claims guaranteed optimality within its framework (verified claim).
4. **Traceability.** Snapshot, models, source revision and evidence ids behind every decision, with integrity checks. Nothing comparable found.
5. **Prospective evaluation** (once C10 exists): pre-registered scoring of every live recommendation against HOLD with calibration tracking. No competitor publishes this; the official Companion deliberately avoids predictions.

**Where the market is ahead** (table stakes to close before "preferred over alternatives" is plausible): FPL-ID onboarding in the UI, a player search, fixture-difficulty views, price-change lists, 14-GW horizons, odds-informed projections, predicted line-ups, live points and ranks, mini-league context, mobile apps, and content/community. Most are V1.1–V1.2 UI work on existing data; odds, line-ups and community are not.

---

## 4. Prioritised roadmap

| Tier | Item | Capability | Effort (weeks) | Depends on |
|---|---|---|---|---|
| **Launch-critical (restricted beta)** | Identity, invite codes, sessions, manager ownership, per-user limits, product events, privacy notice | C12 | 3 | — |
| | FPL-ID onboarding in the web (reuse `/squad/sync`), player picker | C1 | 1.5 | C12 |
| | Copilot Home: situation → decision → what changed, precomputed squad analysis, captain profiles, FDR | C1, C2, C4 | 3 | forecast cache |
| | Degraded banner on all pages; model-card fixes (drift claim, horizons) | C8 | 0.5 | — |
| | Capacity: worker count and precompute scheduling for N beta users | C12 | 0.5 | — |
| **V1.1** | Invalidation status machine, snapshot comparison, "what changed" diff, automatic re-plan | C8 | 3–4 | M1.1a |
| | Automatic post-GW scoring, regret, live-season calibration, season review | C10 | 3–4 | M1.1a, results ingestion |
| | Whole-squad transfers: ladder 0/1/2 moves, multi-out, preferences in UI, hit view, precomputed shortlists | C3, C5, C6 | 4–5 | M1.1a |
| | Strategy profiles mapped to constraints and explained back | C6 | 2 | C3 work |
| | Grounded assistant with evaluation harness | C7 | 4–6 | M1.1a, cost caps |
| **V1.2** | Read-only Chrome overlay with per-user tokens | C11 | 4–5 | C12 tokens, versioned API |
| | Rival view: league standings, rival picks, chips left, P(finish above) | C9 | 3 | C12, league ingestion |
| **V2** | Rank/ownership objective, differential captaincy, rival-aware optimisation | C4, C9 | 6+ | ownership model |
| | Open multi-user access, billing, mobile apps, odds data partnership, better chip planning beyond the horizon | — | — | beta results |

Everything in the existing `docs/BACKLOG.md` remains valid; this table re-orders it around user value and adds C12.

---

## 5. Architecture and integration plan

```
                     ┌──────────────── web (Next.js) ───────────────┐
 browser ──TLS/Caddy─▶ Home · onboarding · assistant UI · overlay API │
                     │ session cookie · /backend proxy · events      │
                     └───────────────┬──────────────────────────────┘
                                     ▼
            ┌──────────────── FastAPI (existing) ───────────────────┐
            │ + users/sessions/ownership  + /copilot/home (read model)│
            │ + /recommendations status machine + /assistant (tools)  │
            └──┬───────────────┬──────────────────────┬──────────────┘
               ▼               ▼                      ▼
        PostgreSQL        Redis / RQ worker        LLM provider (C7 only,
   + users, user_managers  + copilot_refresh       read-only tools, cost caps)
   + product_events        + invalidation/re-plan
   + decision outcomes     + post_gw_scoring
                           + rival_sync (V1.2)
```

Principles: the forecasting, simulation and optimiser packages are **not modified** by V1.1 work; new capabilities are read models and jobs that consume their outputs. Each milestone adds a golden-package parity test proving the recommendation produced for a fixture snapshot and squad is byte-identical before and after.

1. **Identity layer (C12).** Tables `users`, `user_managers` (one user → N manager keys), `invites`, `sessions`; e-mail magic links; the web server validates the session and passes `X-User-Id` to the API over the internal network; every manager-scoped route checks ownership; erasure cascades. Rate limits move from "per API key hash" to "per user". The operator key remains for operations only.
2. **Copilot read model (C1/C2).** Job `copilot_refresh(manager)` runs after each forecast precompute and after a squad change: per-slot xP with intervals, P(start), availability adjustment applied, fixture run with difficulty, price-risk, bench strength, weakest slots, captain profiles, plus the "situation" (deadline, freshness, plan status). Stored as one JSON row per (user, manager, snapshot); `/copilot/home` returns it with `ETag` so the page is instant. Cost: no solves; one pass over the cached forecast.
3. **Invalidation (C8).** Status machine on `recommendations`: `active → invalidated(reason) → superseded`; the invalidation check already run by the alerts job sets the status and enqueues a re-plan with the dedupe hash; `/recommendations/current` compares snapshot ids and returns `stale_reason`. The diff ("what changed") is computed from the two packages' evidence items.
4. **Prospective evaluation (C10).** Job `post_gw_scoring` after `live_results` finalises a GW: for every recommendation of that GW, realised points of the plan and of HOLD from the stored squads (reusing the official-points reproduction code), regret, followed-ness from the re-synced entry picks; writes `decision_journal.realized_*`, `forecast_error`, `review_json`; a rolling calibration table per GW feeds the existing `fpl_model_metric` gauge with live values and a season review page.
5. **Whole-squad comparisons (C3).** Reuse `find_replacements` per weakest slot inside `copilot_refresh` with `FPL_SOLVER_WORKERS` in the worker (the parallel path already exists); the transfer ladder is three recommendation solves (max_transfers 0/1/2) with paired gains, run as one job; `Preferences` exposed in the UI.
6. **Assistant (C7).** A server-side tool-calling loop with read-only tools (`get_home`, `get_recommendation`, `get_player`, `get_trace`, `get_journal`); system prompt forbids claims not returned by tools and requires evidence ids; every turn logged with its tool results; per-user daily budget; offline evaluation set in `tests/assistant/` run in CI against recorded tool outputs (no network).
7. **Extension (C11).** Per-user short-lived tokens (scoped read-only, revocable), `/v1` read API frozen, Manifest V3 content script on `fantasy.premierleague.com` only, overlay fed by `/copilot/home` and `/recommendations/current`.
8. **Caching and precomputation rules.** Precompute what is a function of (snapshot, manager) in the worker; compute per request only what depends on user input; never cache across snapshots; keep the existing dedupe-by-request-hash and fix the recommendation job to honour `state_id`. Correctness over latency: no heuristic replaces a solve or a simulation.
9. **Capacity.** One beta user costs roughly one forecast share plus 1–3 solves per hour of alerts and one 42–90 s recommendation per GW; a 4-vCPU host handles about 20–30 active beta users with the alerts cadence relaxed to hourly; beyond that, a second worker host.

---

## 6. V1.1 milestone sequence

| Milestone | Scope | Exit criteria | Weeks |
|---|---|---|---|
| **M1.1a Copilot Home and beta foundation** (recommended first) | C12 identity and events; FPL-ID onboarding and player picker; Copilot Home with precomputed squad analysis, captain profiles, FDR; degraded banner everywhere; doc fixes | new user FPL ID → recommendation < 2 min; Home p95 < 1.5 s; cross-user access tests; golden parity test; production Playwright extended and green | 6–8 |
| **M1.1b Live intelligence** | invalidation status machine, snapshot comparison, "what changed", automatic re-plan, alerts cadence tuning | invalidated plans never shown as current; re-plan within 15 min of a snapshot; Playwright invalidation scenario | 3–4 |
| **M1.1c Journal and prospective evaluation** | automatic post-GW scoring, regret, followed-ness from entry re-sync, live calibration, season review page | 100 % of recommendations scored within 24 h of finalisation; scorer reproduces official points; calibration page live | 3–4 |
| **M1.1d Whole-squad decisions** | transfer ladder, multi-out replacement, hit view, preferences and strategy profiles in UI, precomputed shortlists | all plans validated; precomputed slot < 2 s; profile tests; parity on default profile | 5–6 |
| **M1.1e Grounded assistant** | tool-calling assistant with evaluation harness, budgets, audit log | ≥ 95 % grounded statements, 0 fabricated statuses on the red-team set; p95 < 8 s | 4–6 |

Total V1.1: roughly 21–28 engineer-weeks. M1.1a is the gate for inviting the first beta users; M1.1b and M1.1c should land before the first "season review" the beta users are promised.

---

## 7. Acceptance criteria and measurable success metrics

**Engineering acceptance for every milestone.** Fast tier, slow/network tiers, optimiser suite and production Playwright stay green; a golden recommendation-package parity test proves engine outputs unchanged; new features ship with unit tests, an integration test on the real database, and a Playwright flow; no new secret in logs (existing probe); latency budgets measured in the production topology and recorded in `docs/BUILD_STATUS.md`.

**Product metrics** (all from the first-party `product_events` table and the journal; no third-party analytics; retained 90 days; users can erase them):

| Metric | Definition | Beta target |
|---|---|---|
| Interactive latency | p95 of `/copilot/home`, `/players`, player detail, precomputed replacement slot; p95 of recommendation job wall-clock | 1.5 s / 1.5 s / 1.5 s / 2 s; job ≤ 90 s at 5 GW |
| Onboarding completion | share of new users who go from FPL ID to a stored recommendation in one session | ≥ 70 % |
| Recommendation engagement | share of recommendations viewed before the deadline; share with journal feedback; share where an alternative or evidence detail is opened | ≥ 80 % / ≥ 50 % / ≥ 40 % |
| Returning users | share of beta users active (Home viewed) in ≥ 3 of the last 4 gameweeks | ≥ 60 % |
| User feedback | thumbs on each recommendation and assistant answer with optional text; monthly 5-question survey | ≥ 70 % positive; themes reviewed per milestone |
| Forecast calibration (live season) | per-GW Brier for P(start), PIT coverage of 80 % intervals for points, ECE for price changes, computed by the post-GW job | PIT coverage within 75–85 %; no drift alert for 4 consecutive GWs |
| Decision quality | cumulative realised gain vs HOLD for followed recommendations; regret distribution; invalidation precision from audits | positive by season end with a reported interval; audited invalidations ≥ 80 % justified |
| Trust | share of recommendations whose trace integrity check passes; fabrication rate in assistant audits | 100 %; 0 |

---

## 8. Recommended first milestone: M1.1a

**Why this and not a feature.** It is the smallest change that materially improves user value for every manager (onboarding from fifteen codes to an FPL ID, an instant Home instead of a 42–90 s wait for context, the squad diagnosis that does not exist today) while not touching forecasting, simulation or optimisation. It also creates the identity and analytics layer that the mandate's measurement requirements and every later milestone depend on, and it is the gate for a restricted beta. The assistant, the extension and mini-leagues are the attractive items, but each is either indefensible without identity (extension, assistant cost control) or crowded (mini-leagues); building them first would pile features on an onboarding funnel that leaks at step one.

**Out of scope for M1.1a** (explicitly): any change to models, solver, chip logic or thresholds; the assistant; the extension; mini-leagues; public sign-up; mobile.

**Risks to manage.** FPL's public entry endpoints are unofficial and rate-limited (one request per second policy already enforced); the product name must change; worker capacity must be measured with 20 simulated users before inviting more; the beta privacy notice must say what is stored (FPL ID, squad, e-mail) and how to erase it.

---

## Sources

Official and Microsoft: [Fantasy Premier League Companion gives managers a new tool for success (Microsoft, 27 Jul 2026)](https://news.microsoft.com/source/emea/features/fantasy-premier-league-companion-gives-managers-a-new-tool-for-success/); [FPL to launch AI assistant manager in 2025/26 (Fantasy Football Scout, 2 Jul 2025)](https://www.fantasyfootballscout.co.uk/2025/07/02/fpl-to-launch-ai-assistant-manager-in-2025-26); [FPL live ranks are now official: what OneFPL still adds (OneFPL, 20 Jul 2026)](https://onefpl.com/blog/fpl-live-ranks-onefpl).

Fantasy Football Scout and network: [Benefits page](https://www.fantasyfootballscout.co.uk/benefits); [Get your FPL team rated ahead of 2025/26 (Rate My Team)](https://www.fantasyfootballscout.co.uk/2025/07/22/get-your-fpl-team-rated-ahead-of-2025-26); [Premier Fantasy Tools: Was that FPL move actually good? (updated 9 Sep 2026)](https://www.premierfantasytools.com/?p=10935); [Premier Fantasy Tools joins the Scout network (2021)](https://www.fantasyfootballscout.co.uk/2021/07/30/premier-fantasy-tools-to-join-the-scout-network-in-2021-22/).

Fantasy Football Fix: [Home page](https://fantasyfootballfix.com); [Competitive / mini-league tools](https://www.fantasyfootballfix.com/competitive/); [FPL Plus Chrome extension (Web Store)](https://chromewebstore.google.com/detail/hbjklngnnpgaenkibbjmnhhgkhbajilm); [FPL Plus blog](https://fantasyfootballfix.com/blog-index/fpl-plus); [ChatFPL launch (24 Jul 2024)](https://www.fantasyfootballfix.com/blog-index/fpl-chat-ai-revolution/); [AI is killing fantasy football (The Next Web, 13 Sep 2024, ChatFPL hands-on)](https://thenextweb.com/news/ai-is-killing-fantasy-football-fpl).

FPL Review: [About](https://docs.fplreview.com/getting-started/about-fplreview/); [Massive Data Model](https://docs.fplreview.com/the-model/projections/massive-data-model/); [Free model](https://docs.fplreview.com/the-model/projections/free-model/); [Introduction to solvers](https://docs.fplreview.com/the-model/solvers/into-to-solvers/); [Solver comparison](https://docs.fplreview.com/the-model/solvers/solver-comparison/); [FAQ](https://docs.fplreview.com/getting-started/faq/).

Fantasy Football Hub: [Member upgrade (prices)](https://www.fantasyfootballhub.co.uk/member-upgrade); [Join page](https://fantasyfootballhub.co.uk/join?via=ross); [Complete review (All About FPL, Aug 2026)](https://allaboutfpl.com/2026/08/complete-detailed-review-of-fantasy-football-hub/); [Assistant Manager predictions page](https://www.fantasyfootballhub.co.uk/fantasy-football-hub-assistant-manager-predictions).

Live and rivals: [LiveFPL (App Store)](https://apps.apple.com/app/id6753036580); [OneFPL: how to win your mini-league](https://onefpl.com/blog/how-to-win-fpl-mini-league); [OneFPL transfer planner](https://onefpl.com/blog/how-to-use-onefpl-transfer-planner); [FPL Copilot home](https://fplcopilot.com/); [FPL Copilot: win your mini-league](https://fplcopilot.com/blog/win-your-minileague); [FPL Intelligence MCP server](https://glama.ai/mcp/servers/pyum4cmk9j).

Extensions: [FPL Elite HUD Pro / Touchline (Web Store)](https://chromewebstore.google.com/detail/dhkalghhadhaaabkeopmmhmkagojjghi); [FPL Comrade (Chromeboard listing)](https://www.chromeboard.com/extension/fpl-comrade-gkjihbkkbmmeafnacffoojeekoafofhg); [myfpl (Firefox add-on)](https://addons.mozilla.org/nl/firefox/addon/myfpl); [FPL Mini-League Team View (Web Store)](https://chrome.google.com/webstore/detail/fggnjilkhkbambfagpdnlbhpnmjljmch).

Other: [The Dugout (Product Hunt)](https://www.producthunt.com/posts/1233185); [Points gained over the lifetime of a transfer (FFScout, 2021)](https://www.fantasyfootballscout.co.uk/2021/10/08/points-gained-over-the-lifetime-of-a-transfer/).
