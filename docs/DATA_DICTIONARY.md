# Data Dictionary

Canonical data model, data contracts, point-in-time availability rules and data-quality evidence.
Feature definitions (§56) are appended by the feature-store milestone (M4).

## 1. Canonical tables (analytic shape)

The same frames are produced by ingestion, read back from PostgreSQL (`fpl_storage.dataset.
load_from_db`) and stored in Parquet snapshots; equality is verified by content hash
(`snapshot_id`). Natural keys use FPL **stable codes** (player `code`, team `code`), never the
per-season ids.

| Table | Natural key | Key columns | Availability timestamp (ADR-0004) |
|-------|-------------|-------------|-----------------------------------|
| `seasons` | season | ruleset_version, started_at, ended_at | season start |
| `gameweeks` | season, gw | deadline_at (official live / derived = first kickoff − 90 min historically), first/last kickoff, finalized_at, status | schedule publication |
| `teams` | season, team_code | source_id, name, short_name, strength_json (**not PIT-safe historically**) | season start |
| `players` | season, player_code | position (authoritative season registry, §51.1), current team/price/status (**current state, never used for historical features**) | — |
| `fixtures` | season, fixture_id | gw, kickoff_at, home/away team codes, scores, FDR (**end-state, leakage risk**), status | `schedule_available_at` (schedule); `result_available_at` (scores) |
| `player_match` | season, fixture_id, player_code | minutes, starts, points, goals, assists, CS, GC, OG, pens, cards, saves, bonus, bps, dc/cbi/tackles/recoveries (2025-26+), xG/xA/xGC, ICT, price, ownership_count, GW transfers | stats: `available_at` = kickoff + 150 min; price/ownership: kickoff; transfers: GW deadline |
| `team_match` | season, fixture_id, team_code | goals for/against, xG for/against (Σ player xG), threat for/against | kickoff + 150 min |
| `player_snapshots` | season, player_code, captured_at, source | price, ownership %, form, status, news, chance of playing, set-piece orders, official price-change signal | `available_at` = capture time |
| `news_signals` | id | structured status, chance of playing, evidence strength, text | publication/capture time |

PostgreSQL stores these normalised across 37 tables (`fpl_storage.models`), with
`raw_snapshot_id` + `content_hash` lineage on canonical rows, `record_revisions` for changed rows,
`data_jobs` and `data_quality_events` for operations.

## 2. Source data contracts (`fpl_ingestion.contracts`)

Each source file has a versioned contract declaring type, nullability, range/allowed values,
requirement and **leakage risk** per column. Highlights:

| Contract | Version | Notable rules |
|----------|---------|---------------|
| `vaastav.merged_gw` | 1.1.0 | unique (fixture, element); minutes 0–130; bonus 0–3; cards bounded; `xP` declared **leakage: high** and never used |
| `vaastav.fixtures` | 1.0.0 | unique id; FDR 1–5; scores 0–20 |
| `vaastav.teams` | 1.0.0 | `strength_*` declared **leakage: high** (end-of-season values) |
| `vaastav.players_raw` | 1.0.0 | stable `code` unique; status ∈ {a,d,i,s,u,n}; optional official price-change fields |

## 3. Quality gates (`fpl_ingestion.quality`, §55.2)

| Check | Severity on failure | Behaviour |
|-------|---------------------|-----------|
| Missing column / uncoercible type | critical | batch quarantined |
| Conflicting duplicate keys | critical | batch quarantined |
| Exact duplicate keys | warning | de-duplicated |
| Referential integrity (fixture/player/team) | critical | quarantined |
| Home team = away team | critical | quarantined |
| Range / allowed values | error | loaded, flagged |
| Team column vs fixture-derived team | info | fixture-derived team used (mid-season moves) |
| Position vs season registry | error | registry authoritative |
| GW assignment vs fixture GW | error | flagged |
| Starters per team-fixture = 11 | error (≠11) / warning (0 → set NULL) | unpopulated `starts` never treated as "benched" |
| Score = Σ goals + opponent own goals | error | flagged |
| ≥ 11 players with minutes per finished team-fixture | error | flagged |
| Deadline(t+1) after last kickoff(t); row kickoff = fixture kickoff | error | flagged |
| Consecutive price jumps > £0.5m | warning | flagged |
| Lower-priority source disagrees with higher-priority value | warning (`source_conflict`) | higher-priority value kept; both preserved in the event |
| Freshness (live sources) | — | `fresh / stale / expired / unknown` per SLA (`config/sources.yaml`) |

### Evidence on the real dataset (pinned commit `9779cdb`, ingested 2026-10-02)

| Season | Player-fixture rows | Fixtures | Players | Gate | Issues found |
|--------|--------------------:|---------:|--------:|------|--------------|
| 2022-23 | 26,505 | 380 (37 GWs; GW7 postponed) | 778 | passed | 272 team-fixtures with unpopulated `starts` (set NULL) |
| 2023-24 | 29,725 | 380 | 865 | passed | none |
| 2024-25 | 27,283 | 380 | 784 | passed | 322 Assistant-Manager rows filtered |
| 2025-26 | 29,747 | 380 | 841 | passed | 10 exact duplicate rows (one player, GW1–9) de-duplicated |
| 2026-27 | 610 (GW1) | 380 (schedule) | 616 | passed | — ; 616 live snapshot rows, 123 news signals |

Snapshot of all five seasons: `snap_b64560a8c4f434ad984e` (re-ingestion and re-export reproduce
the same id).

## 4. Point-in-time policies (ADR-0004 addendum)

| Policy | Rule | Approximation / residual risk |
|--------|------|-------------------------------|
| Decision cutoff | deadline − 90 min | — |
| Deadline (historical) | first kickoff of GW − 90 min (FPL rule) | exact under the current FPL rule |
| Price at cutoff | last observation with kickoff < cutoff | misses price moves between that kickoff and the deadline |
| **A1** registration/team | player with a row in GW t (or the 3 previous GWs) is registered at cutoff(t), on the team of their latest row ≤ t | assumes players are added before the deadline of their first GW with data |
| **A2** first-appearance price | a player's first observed price is known at the cutoff of that GW | initial prices are fixed before a player's first deadline |
| Ownership at cutoff | last observation before cutoff (unknown at GW1) | — |
| GW transfer counts | available at that GW's deadline | — |
| DGW fixture visibility | `kickoff − 28 days` | late postponements creating blanks are not modelled (documented limitation) |
| Labels | `provisional_ok` (kickoff + 150 min) or `finalized_only` (GW finalisation) | configurable per experiment |

Enforced by `fpl_storage.pit.PointInTimeView` and verified by a Hypothesis property test that
randomly corrupts every value whose availability is after the cutoff and asserts all PIT outputs
are unchanged (`tests/unit/storage/test_raw_store_and_pit.py`).
