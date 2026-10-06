# ADR-0003: Data sources, licensing and degraded operation

- Status: Accepted
- Date: 2026-10-02

## Context

§6, §55 and §75 require reproducible, licensed, provenance-tracked ingestion that never
silently overwrites official values, and §82 requires useful degraded behaviour.

Observed in the build environment (2026-10-02):

| Source | Host | Reachable | Notes |
|--------|------|-----------|-------|
| FPL official API (`/api/bootstrap-static/`, `/api/fixtures/`, `/api/event/{gw}/live/`, `/api/entry/{id}/…`, `/api/leagues-classic/{id}/standings/`) | fantasy.premierleague.com | **No** (egress policy 403) | Primary live source in production. |
| FPL historical dataset (vaastav/Fantasy-Premier-League, MIT licence; data © FPL / Understat) | raw.githubusercontent.com | Yes | Seasons 2016-17 → 2025-26 complete, 2026-27 GW1. Pinned by commit SHA. |
| premierleague.com rule pages | www.premierleague.com | No | Rules verified via secondary sources + empirical scoring reproduction (ADR-0005). |

## Decision

1. **Source adapters behind interfaces** (`fpl_ingestion.sources`): `FplApiSource` (live) and
   `HistoricalRepoSource` (vaastav). Each produces immutable raw payloads with source URL,
   retrieval timestamp, schema version and SHA-256 content hash.
2. **Pinning**: historical ingestion reads `https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/<sha>/…`.
   The SHA is configuration (`config/sources.yaml`) and is stored with every raw snapshot.
3. **Source priority + confidence layer**: canonical values record `source`, `source_priority`
   and `retrieved_at`. When two sources disagree (e.g. price in a live snapshot vs historical
   row) a `data_quality_events` row of type `conflict` is written and the higher-priority
   *official* value is served; the other is preserved, never discarded.
4. **Degraded mode** (§82): if the live source is unavailable, the API serves the last
   validated snapshot with `freshness.age_seconds`, `freshness.status ∈ {fresh, stale, expired}`
   and lowers recommendation confidence labels; price-sensitive claims are suppressed when the
   price signal is unavailable.
5. **Manual squad entry** is a first-class input (useful when the live API is unreachable or
   the manager does not want to share an entry id). It is validated by the same domain rules.
6. **Licensing**: The historical dataset is MIT-licensed code with data that is property of
   FPL/Understat. The project stores raw copies only as a local development cache (`data/raw/`,
   git-ignored) and commits only small test excerpts with attribution (`data/fixtures/README.md`).
   The live FPL API is used read-only, at low frequency (scheduled, cached), with no credential
   storage: manager state is reconstructed from public endpoints (ADR-0004).

## Consequences

- Live sync is implemented and contract-tested but cannot be exercised end-to-end here; this is
  reported explicitly in BUILD_STATUS and FINAL_AUDIT.
- Anyone deploying with network access to `fantasy.premierleague.com` gets live operation with no
  code change.


## Addendum (2026-10-05, local development machine)

The egress block described above applied to the earlier cloud build environment only. On the
local machine `fantasy.premierleague.com` and `premierleague.com` are reachable: live bootstrap
and fixture captures, live squad sync (verified against official picks) and the hourly scheduled
refresh run against the real API, and `tests/integration/test_live_rules.py` (marker `network`)
checks the 2026-27 ruleset against the official `game_settings`, chip windows and scoring table
(all pass). Degraded mode remains the behaviour whenever the source is unreachable.
