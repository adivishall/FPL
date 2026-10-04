# Data Dictionary

Canonical data model, data contracts, point-in-time availability rules and data-quality evidence.
Feature definitions (§56) are in §5 (generated from the feature registry).

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

## 5. Feature dictionary (§56)

All features are computed by `fpl_features.builder.build_features` from a `PointInTimeView` at the
decision cutoff; each frame records the latest source availability timestamp it used and the
builder fails if that is after the cutoff. Recency weights decay by half-life in *team fixtures*
(non-appearances count as zeros while the player is registered).

<!-- BEGIN GENERATED FEATURE TABLE -->
Feature version `1.2.0` — generated from `fpl_features.registry`.

| Feature | Family | Definition | Source | Window | Timestamp policy | Missing | Leakage | Type/range |
|---|---|---|---|---|---|---|---|---|
| `n_prior_matches` | meta | Team fixtures observed for the player before cutoff (any season) | player_match (PIT: available_at ≤ cutoff) | all history | as of the latest row available at cutoff | 0 | none | int [0, None] |
| `mins_last1` | minutes | Minutes in the most recent team fixture | player_match (PIT: available_at ≤ cutoff) | 1 match | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 130] |
| `mins_ewm_short` | minutes | Recency-weighted mean minutes | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 3 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 130] |
| `mins_ewm_long` | minutes | Recency-weighted mean minutes | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 130] |
| `start_rate_short` | minutes | Recency-weighted share of team fixtures started | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 3 | as of the latest row available at cutoff | NaN where starts unpopulated | none | float [0, 1] |
| `start_rate_long` | minutes | Recency-weighted share of team fixtures started | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 1] |
| `app_rate_short` | availability | Recency-weighted share of team fixtures with minutes>0 | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 3 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 1] |
| `full90_rate_long` | minutes | Recency-weighted share of fixtures with ≥89 minutes | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 1] |
| `sub_app_rate_long` | minutes | Share of team fixtures entered as a substitute | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 1] |
| `mins_if_start_long` | minutes | Recency-weighted mean minutes when starting | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 130] |
| `zero_min_streak` | availability | Consecutive most recent team fixtures with 0 minutes (injury/drop proxy) | player_match (PIT: available_at ≤ cutoff) | up to 40 matches | as of the latest row available at cutoff | 0 | none | int [0, 40] |
| `days_since_last_app` | availability | Days from last appearance (minutes>0) to cutoff | player_match (PIT: available_at ≤ cutoff) | all history | as of the latest row available at cutoff | NaN if never appeared | none | float [0, None] |
| `xg_p90` | attacking | Recency-weighted xG per 90 minutes | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN if <90 weighted minutes | none | float [0, 3] |
| `xa_p90` | attacking | Recency-weighted xA per 90 minutes | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 3] |
| `goals_p90` | attacking | Recency-weighted goals per 90 | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 5] |
| `assists_p90` | attacking | Recency-weighted assists per 90 | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 5] |
| `threat_p90` | attacking | Recency-weighted ICT threat per 90 (shot/box proxy) | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, None] |
| `creativity_p90` | attacking | Recency-weighted ICT creativity per 90 (chance creation) | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, None] |
| `xg_share` | role | Player xG / team xG over fixtures played, minutes-adjusted | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 1.5] |
| `xa_share` | role | Player xA / team xG over fixtures played, minutes-adjusted | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 1.5] |
| `saves_p90` | defensive | Recency-weighted saves per 90 (GK) | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, None] |
| `dc_actions_p90` | defensive | Recency-weighted defensive actions per 90 (CBI+tackles(+recoveries for MID/FWD)) | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN before 2025-26 (not recorded) | none | float [0, None] |
| `dc_hit_rate` | defensive | Share of appearances reaching the position DC threshold | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN before 2025-26 | none | float [0, 1] |
| `bps_p90` | defensive | Recency-weighted BPS per 90 | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float |
| `bonus_p90` | form | Recency-weighted bonus per 90 | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, 3] |
| `yellow_p90` | defensive | Recency-weighted yellow cards per 90 | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float [0, None] |
| `pts_last1` | form | FPL points in the most recent team fixture | player_match (PIT: available_at ≤ cutoff) | 1 match | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float |
| `pts_ewm_short` | form | Recency-weighted points per team fixture | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 3 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float |
| `pts_ewm_long` | form | Recency-weighted points per team fixture | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float |
| `pts_p90` | form | Recency-weighted points per 90 | player_match (PIT: available_at ≤ cutoff) | EWM over the player's last ≤40 team fixtures, half-life 10 | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float |
| `ppg_season` | form | Season points per appearance up to cutoff | player_match (PIT: available_at ≤ cutoff) | season | as of the latest row available at cutoff | NaN before first appearance | none | float |
| `price` | economics | Price (tenths) known at cutoff (ADR-0004 price policy) | price_observations / live snapshot |  | last observation before cutoff (A2) | never missing for pool players | none | int [35, 200] |
| `price_change_season` | economics | Price at cutoff minus first price this season | price_observations |  | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | int |
| `ownership_pctile` | economics | Percentile rank of ownership within the player pool at cutoff (scale-free: identical for historical counts and live percentages) | price_observations / live snapshot |  | as of the latest row available at cutoff | NaN at GW1 historically | none | float [0, 1] |
| `net_transfers_last_gw` | economics | (transfers in − out) / (in + out + 1000) for the last completed GW (bounded momentum) | gw_transfers |  | available at that GW's deadline | NaN (model handles); new players flagged | none | float [-1, 1] |
| `pos_GK` | role | Position indicator | players (season registry) |  | season start | NaN (model handles); new players flagged | none | bool |
| `pos_DEF` | role | Position indicator | players |  | season start | NaN (model handles); new players flagged | none | bool |
| `pos_MID` | role | Position indicator | players |  | season start | NaN (model handles); new players flagged | none | bool |
| `pos_FWD` | role | Position indicator | players |  | season start | NaN (model handles); new players flagged | none | bool |
| `team_xg_for_ewm` | team | Team xG for per match (recency-weighted) | team_match (PIT) | EWM half-life 8 team matches | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float |
| `team_xg_against_ewm` | team | Team xG against per match (recency-weighted) | team_match (PIT) | EWM half-life 8 team matches | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | float |
| `opp_xg_for_ewm` | fixture | Opponent xG for per match (recency-weighted) | team_match (PIT) | EWM half-life 8 team matches | as of the latest row available at cutoff | NaN for opponents without PL history (promoted) | none | float |
| `opp_xg_against_ewm` | fixture | Opponent xG against per match (recency-weighted) | team_match (PIT) | EWM half-life 8 team matches | as of the latest row available at cutoff | NaN for opponents without PL history (promoted) | none | float |
| `is_home` | fixture | Target fixture at home | fixtures schedule (PIT) |  | schedule_available_at ≤ cutoff | NaN (model handles); new players flagged | none | bool |
| `horizon` | schedule | Gameweeks ahead of the decision GW (0 = this GW) | derived |  | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | int [0, 10] |
| `fixtures_in_gw` | schedule | Team fixtures in the target GW (2 = double) | fixtures schedule (PIT) |  | as of the latest row available at cutoff | NaN (model handles); new players flagged | none | int [1, 3] |
| `days_rest` | schedule | Days between the team's previous scheduled fixture and target | fixtures schedule (PIT) |  | as of the latest row available at cutoff | NaN for season opener | none | float [0, None] |
| `status_flag` | live | FPL status mapped: a=0, d=1, i/s/u/n=2 | player_snapshots |  | captured_at ≤ cutoff | NaN when no live snapshot (all historical seasons) | none | float [0, 2] |
| `chance_of_playing` | live | FPL chance of playing next round (0–100) | player_snapshots |  | as of the latest row available at cutoff | NaN = no flag | none | float [0, 100] |
| `penalty_taker` | live | FPL penalties_order == 1 | player_snapshots |  | as of the latest row available at cutoff | NaN historically | none | bool |
<!-- END GENERATED FEATURE TABLE -->
