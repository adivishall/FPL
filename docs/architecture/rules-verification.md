# Rules verification evidence (ADR-0005)

Official rule pages were unreachable from the build environment, so rules are verified two ways:
secondary sources (recorded in each ruleset's `provenance.sources`) and **empirical reproduction**
on real data. This page records the empirical evidence.

## Scoring: recomputed points vs official `total_points`

Method: for every player-fixture row of the canonical snapshot, recompute points from raw event
counts (minutes, goals, assists, clean-sheet flag, goals conceded, saves, penalties, cards, own
goals, bonus, defensive actions) with `fpl_domain.scoring` under that season's ruleset, and compare
with the official value. Test: `tests/integration/test_full_scoring_reproduction.py` (slow, full
data) and `tests/unit/domain/test_scoring.py::test_reproduces_official_points_exactly`
(committed excerpts, runs in CI).

| Season | Rows | Exact matches | Rule regime |
|--------|-----:|--------------:|-------------|
| 2022-23 | 26,505 | 100% | GK goal 6, no DC |
| 2023-24 | 29,725 | 100% | GK goal 6, no DC |
| 2024-25 | 27,283 | 100% | GK goal 6, no DC |
| 2025-26 | 29,747 | 100% | GK goal 10, DC (DEF ≥10 CBIT, MID/FWD ≥12 CBIRT → 2 pts) |
| 2026-27 (GW1) | 610 | 100% | as 2025-26 |
| **Total** | **113,870** | **100%** | |

### Rule coverage (rows exercising each rule)

| Rule | 2022-23 | 2023-24 | 2024-25 | 2025-26 | 2026-27 GW1 |
|------|--------:|--------:|--------:|--------:|------------:|
| Penalty saved | 17 | 8 | 13 | 11 | 0 |
| Penalty missed | 25 | 11 | 13 | 15 | 1 |
| Own goal | 45 | 50 | 33 | 39 | 1 |
| Red card | 30 | 58 | 52 | 44 | 1 |
| Defensive contribution awarded | — | — | — | 1,875 | 36 |
| **Goalkeeper goal** | **0** | **0** | **0** | **0** | **0** |

Goalkeeper-goal points (6 → 10 from 2025-26) are therefore verified by secondary sources only.

### Finding discovered by the reproduction

The first implementation zeroed every component for players with 0 minutes; 9 rows disagreed.
All were **cards received without playing** (bookings on the bench), which FPL deducts. The rule
was corrected in `score_components` and is covered by
`test_cards_count_without_minutes_but_nothing_else_does`.

## Transfers, chips, captaincy

Not observable in the public historical dataset (no manager-level data). Verified by:
* secondary sources (ruleset `provenance`);
* unit tests per rule (`tests/unit/domain/test_state_machine.py`,
  `tests/unit/domain/test_squad_and_lineup.py`);
* property-based invariants (`tests/property/test_state_invariants.py`);
* in live operation, the manager-sync replay cross-checks every gameweek's replayed hit cost
  against FPL's recorded `event_transfers_cost` and reports disagreements
  (`fpl_ingestion.manager_sync`).
