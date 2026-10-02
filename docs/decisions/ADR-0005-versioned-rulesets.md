# ADR-0005: FPL rules as versioned configuration

- Status: Accepted
- Date: 2026-10-02

## Decision

All game rules live in `config/rules/<season>.yaml`, validated against `config/rules/schema.yaml`
(JSON-Schema expressed in YAML) and loaded into an immutable `Ruleset` domain object. No rule
constant appears in application logic (§41 #1, §42, §51).

A ruleset contains: squad composition and budget, lineup/formation bounds, transfer economics
(free transfers per GW, max banked, hit cost, chip FT policy, scheduled top-ups), selling-price
rule, chip catalogue with validity windows per season half, scoring tables (including defensive
contribution thresholds by position), bonus tie rules, captaincy multipliers, gameweek
finalisation lag, and provenance (`sources`, `verification` status per section).

Every optimisation run, prediction and recommendation stores `ruleset_version` and the ruleset
content hash.

## Verification strategy

Official rule pages were not reachable from the build environment. Rules are verified by:
1. Secondary sources (recorded in the ruleset `sources` block with access date).
2. **Empirical reproduction**: the scoring engine recomputes `total_points` for every
   player-fixture row of each historical season from raw event counts and compares with the
   official value. The pass rate per season is a CI-checked threshold and reported in
   `docs/DATA_DICTIONARY.md`. Mismatches are investigated and documented, not ignored.

## Consequences

- A new season is introduced by adding a YAML file and its verification evidence, not by
  editing algorithms.
- Rulesets exist for every season used in training/backtesting (2022-23 → 2026-27), because a
  backtest must apply *that season's* rules.
