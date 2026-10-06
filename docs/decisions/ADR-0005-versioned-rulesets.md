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

## Addendum (2026-10-04): club limit after a mid-season club move

An owned player who moves to another Premier League club counts against his **new** club
(`fpl_domain.state.with_current_clubs`, applied before every decision by the backtest runner and
the API). If that leaves a squad above the per-club limit, the excess may be kept or reduced, but
no transfer may increase that club's count above the limit (`squad.club_allowance`; mirrored in
the MILP). This is our reading of the official behaviour — managers are not forced to sell — and
is recorded as an assumption. Found by a 2025-26 replay in which the optimiser (current clubs)
and the state machine (clubs at purchase) disagreed (Semenyo, Bournemouth → Manchester City).

## Addendum (2026-10-05): official verification

Squad size, starting XI, per-club limit, budget, selling-price rule (sell-on fee 0.5), the banked
free-transfer cap (1 + `max_extra_free_transfers` = 5), all eight chip windows and the scoring
table by position were compared with the official `bootstrap-static` payload — all equal
(`tests/integration/test_live_rules.py`, run against the live API). Hit cost, auto-substitution
rules and bonus tie-breaks are not exposed by that payload and remain verified empirically.
