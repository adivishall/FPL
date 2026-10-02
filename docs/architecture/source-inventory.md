# Data source inventory

Recorded during Phase 0 discovery (2026-10-02). See ADR-0003 for the policy and ADR-0004 for
point-in-time semantics.

## 1. FPL official API (live, primary in production)

Base: `https://fantasy.premierleague.com/api/` — read-only, unauthenticated public endpoints.
**Status in build environment: blocked by egress policy (HTTP 403 at CONNECT).**

| Endpoint | Used for | Cadence | Notes |
|----------|----------|---------|-------|
| `bootstrap-static/` | players, teams, gameweeks (deadlines), prices, ownership, status/news, chance of playing, set-piece orders, official price-change predictor fields (`price_change_percent`, `price_change_projections`, …) | scheduled (default every 6 h; hourly near deadline) | Largest payload; cached, never fetched per page load (§6.1). |
| `fixtures/` | fixture schedule, results, per-fixture stats | scheduled | Normalised to canonical fixtures. |
| `event/{gw}/live/` | per-player GW stats incl. provisional bonus | after matches | Provisional vs final distinguished (§51.1). |
| `element-summary/{id}/` | per-player history | on demand / backfill | Rate-limited client. |
| `entry/{id}/` | manager summary | on sync | Public. |
| `entry/{id}/history/` | per-GW bank, value, transfers, hits, chips used | on sync | Used to reconstruct bank/FT/chip state. |
| `entry/{id}/event/{gw}/picks/` | squad, captain, bench order for a finished/locked GW | on sync | Current-GW picks become public after the deadline. |
| `entry/{id}/transfers/` | transfer history with prices | on sync | Purchase prices → selling-price reconstruction. |
| `leagues-classic/{id}/standings/` | mini-league standings | on demand | League-aware mode (§24, §65). |

Terms: FPL data is the property of the Premier League. The engine performs low-frequency,
cached, read-only access and stores no FPL credentials (§35, §75).

## 2. Historical dataset (vaastav/Fantasy-Premier-League)

`https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/<commit-sha>/data/<season>/…`
**Status: reachable.** Pinned commit at discovery: `9779cdbc0c07f6c900c2d0c181ddf6bb9c800f88`
(2026-08-28T09:47:53Z, "Add 26/27 gw1 data"). Licence: MIT for the repository; README states the
data is the property of fantasy.premierleague.com and understat.com.

| File | Content | Seasons used |
|------|---------|--------------|
| `gws/merged_gw.csv` | one row per player × fixture: minutes, starts, goals, assists, CS, GC, saves, bonus, bps, xG, xA, xGI, xGC, ICT, cards, OG, pens, price (`value`), ownership (`selected`), GW transfers, DC/CBI/tackles/recoveries (2025-26+) | 2022-23 → 2026-27 |
| `fixtures.csv` | fixture id, GW, kickoff, teams, scores, FDR, per-fixture stats JSON | same |
| `teams.csv` | team id (per season), stable `code`, names, FPL strength indices | same (strength indices **not** used historically: end-of-season values = leakage) |
| `players_raw.csv` | end-of-season (or snapshot-time) player bootstrap fields incl. stable `code`, position, status, news, set-piece orders, price-change predictor | 2026-27 snapshot only as a point-in-time snapshot (published 2026-08-28T09:47Z, before the GW2 deadline); historical seasons only for identity (`code`, position) |

### Known data caveats
- `xP` is scraped *after* each gameweek (upstream caveat, commit `81980c1`): **excluded**.
- 2024-25 rows include Assistant Manager entries (`position == "AM"`): filtered out.
- 2025-26 introduced `defensive_contribution`, `clearances_blocks_interceptions`, `tackles`,
  `recoveries` per match; earlier seasons lack them (and their rulesets have no DC scoring).
- Team ids are per-season; the stable team `code` and player `code` are the canonical keys.
- Weekly upstream updates stopped after 2024-25 (2025-26 is complete via the end-of-season
  update; 2026-27 currently has GW1 only).

## 3. Rules references

Official pages (premierleague.com, fantasy.premierleague.com/help) are blocked from the build
environment. Rule facts were cross-checked via search results summarising the official 2026/27
announcements (accessed 2026-10-02):
- Two sets of chips (WC, FH, TC, BB), one per half; first set must be used by the GW19 deadline.
- Up to five free transfers can be banked; no AFCON top-up in 2026/27.
- Hits cost 4 points.
- Defensive contributions unchanged from 2025/26.
- BPS: 1 BPS per 3 clearances/blocks/interceptions (was per 2); "being tackled" deduction removed;
  goalkeeper save BPS restructured.
- Banked free transfers are retained when a Wildcard or Free Hit is played.

Scoring rules are additionally verified empirically by reproducing official points (ADR-0005).

## 4. External football data (§6.2)

Underlying metrics available in the pinned dataset per match: xG, xA, xGI, xGC (from FPL/Opta),
ICT components (threat ≈ shot/box involvement, creativity ≈ chance creation, influence),
defensive actions (2025-26+). Shots, box touches, key passes, big chances are **not** available
from any reachable source; the ingestion layer exposes an `UnderlyingStatsSource` interface so
such a provider can be added with source priority and conflict tracking. Documented in
KNOWN_LIMITATIONS.
