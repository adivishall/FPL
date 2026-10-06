# Alert rules on real data (§74)

Production rule functions fed with real inputs from the `engine_no_chips` walk-forward replays of 2023-24, 2024-25, 2025-26 (see the module docstring of `ml/experiments/alerts_audit.py` for every input's provenance). Thresholds: `config/notifications/default.yaml`.

| alert | fired | candidates before the materiality filter |
|---|---|---|
| role_change | 119 | 132 |
| unexpected_benching | 50 | 49 |
| fixture_change | 10 | 111 |
| price_risk | 15 | — |
| post_gameweek | 111 | — |

* Re-evaluating unchanged inputs produced identical dedupe keys: yes; every fired alert has a distinct key: yes (the store's unique constraint turns repeats into no-ops).
* Post-gameweek reviews: 111; 110 name at least one material miss.
* Candidate counts are per alert type with every threshold at zero. One alert per player and week: a role change takes precedence over benching, so with the filter off some benched players are reported as role changes instead and the benching candidate count can be lower than the number fired.
* Availability (injury / suspension / doubt) alerts need status history, which the archive does not have: exercised only on live captures and in unit tests.

## Examples (first 15 non-review alerts)

| season | GW | kind | title | materiality |
|---|---|---|---|---|
| 2023-24 | 2 | role_change | Fabianski: role down (P(start) 83%→50%) | 1.29 |
| 2023-24 | 2 | role_change | Undav: role down (P(start) 50%→19%) | 1.92 |
| 2023-24 | 3 | role_change | Mings: role down (P(start) 75%→48%) | 1.03 |
| 2023-24 | 3 | price_risk | Planned transfer at risk from price changes (67%) | 0.67 |
| 2023-24 | 4 | role_change | Martinez: role down (P(start) 83%→57%) | 1.22 |
| 2023-24 | 5 | fixture_change | Fixture changes affect your plan (≈3.7 pts) | 3.74 |
| 2023-24 | 6 | unexpected_benching | Estupiñan: unexpectedly benched | 2.48 |
| 2023-24 | 7 | unexpected_benching | Gross: unexpectedly benched | 3.21 |
| 2023-24 | 7 | role_change | Morris: role up (P(start) 70%→96%) | 1.24 |
| 2023-24 | 7 | unexpected_benching | Henry: unexpectedly benched | 1.89 |
| 2023-24 | 7 | unexpected_benching | Foster: unexpectedly benched | 2.58 |
| 2023-24 | 9 | role_change | Gross: role up (P(start) 57%→90%) | 1.24 |
| 2023-24 | 9 | role_change | Estupiñan: role down (P(start) 70%→12%) | 1.78 |
| 2023-24 | 9 | role_change | Eze: role down (P(start) 78%→50%) | 1.49 |
| 2023-24 | 9 | price_risk | Planned transfer at risk from price changes (78%) | 0.78 |

Fixture-change alerts (real schedule changes reaching the squad):

* 2023-24 GW5: Fixture changes affect your plan (≈3.7 pts)
* 2023-24 GW22: Fixture changes affect your plan (≈21.7 pts)
* 2023-24 GW25: Fixture changes affect your plan (≈4.0 pts)
* 2023-24 GW30: Fixture changes affect your plan (≈40.2 pts)
* 2023-24 GW34: Fixture changes affect your plan (≈30.5 pts)
* 2024-25 GW22: Fixture changes affect your plan (≈9.8 pts)
* 2024-25 GW23: Fixture changes affect your plan (≈6.9 pts)
* 2024-25 GW30: Fixture changes affect your plan (≈23.2 pts)
* 2025-26 GW32: Fixture changes affect your plan (≈51.5 pts)
* 2025-26 GW33: Fixture changes affect your plan (≈11.1 pts)

## Price risk on real transfers (plan spends the whole bank)

| season | GW | buys | max P(rise) of a buy | alert | P(unaffordable) |
|---|---|---|---|---|---|
| 2023-24 | 3 | Henry, Foden, Haaland | 0.25 | yes | 0.67 |
| 2023-24 | 4 | Morris | 0.01 | no | — |
| 2023-24 | 6 | Eze, Foster | 0.16 | no | — |
| 2023-24 | 9 | White, Neto | 0.50 | yes | 0.78 |
| 2023-24 | 11 | Andersen, Hee Chan | 0.25 | yes | 0.40 |
| 2023-24 | 12 | Mitoma | 0.07 | no | — |
| 2024-25 | 4 | Eze, Palmer, Wissa | 0.42 | yes | 0.80 |
| 2024-25 | 8 | Robinson, Johnson, Mbeumo, Raúl | 0.38 | yes | 0.77 |
| 2024-25 | 12 | Virgil, B.Fernandes, Isak, Evanilson | 0.51 | yes | 0.93 |
| 2024-25 | 17 | Milenković, Lacroix, M.Salah, Iwobi, Lankshear | 0.27 | yes | 0.80 |
| 2024-25 | 19 | I.Sarr, Solanke | 0.28 | yes | 0.55 |
| 2024-25 | 24 | Tarkowski, Virgil, Alexander-Arnold, O.Dango, Ndiaye | 0.31 | yes | 0.77 |
| 2025-26 | 3 | Elanga, Ekitiké | 0.38 | yes | 0.47 |
| 2025-26 | 7 | Burn, Senesi, Anthony, Haaland | 0.57 | yes | 0.96 |
| 2025-26 | 9 | Gabriel, Minteh | 0.97 | yes | 0.85 |
| 2025-26 | 13 | Guéhi, Van den Berg, B.Fernandes, Thiago | 0.31 | yes | 0.78 |
| 2025-26 | 15 | Thiaw, Szoboszlai | 0.22 | yes | 0.53 |
| 2025-26 | 17 | Verbruggen, Collins, Van Hecke, Tavernier, Foden | 0.88 | yes | 0.92 |

## Deadline reminders (official deadlines from the live FPL API)

The 20 h check runs twice on purpose: the repeat yields the same dedupe key, so the store sends the reminder once. The second deadline is the first after UK clocks go back (BST → GMT).

| GW | deadline (UTC) | time zone | hours before | alert | local time | dedupe key |
|---|---|---|---|---|---|---|
| 6 | 2026-10-10T10:00 | Europe/London | 30 | — | — | — |
| 6 | 2026-10-10T10:00 | Europe/London | 20 | GW6 deadline in under 24h | 2026-10-10T11:00:00+01:00 | deadline:2026-27:6:24h |
| 6 | 2026-10-10T10:00 | Europe/London | 20 | GW6 deadline in under 24h | 2026-10-10T11:00:00+01:00 | deadline:2026-27:6:24h |
| 6 | 2026-10-10T10:00 | Europe/London | 1.5 | GW6 deadline in under 2h | 2026-10-10T11:00:00+01:00 | deadline:2026-27:6:2h |
| 6 | 2026-10-10T10:00 | Europe/London | -0.1 | — | — | — |
| 6 | 2026-10-10T10:00 | Asia/Kolkata | 30 | — | — | — |
| 6 | 2026-10-10T10:00 | Asia/Kolkata | 20 | GW6 deadline in under 24h | 2026-10-10T15:30:00+05:30 | deadline:2026-27:6:24h |
| 6 | 2026-10-10T10:00 | Asia/Kolkata | 20 | GW6 deadline in under 24h | 2026-10-10T15:30:00+05:30 | deadline:2026-27:6:24h |
| 6 | 2026-10-10T10:00 | Asia/Kolkata | 1.5 | GW6 deadline in under 2h | 2026-10-10T15:30:00+05:30 | deadline:2026-27:6:2h |
| 6 | 2026-10-10T10:00 | Asia/Kolkata | -0.1 | — | — | — |
| 6 | 2026-10-10T10:00 | America/New_York | 30 | — | — | — |
| 6 | 2026-10-10T10:00 | America/New_York | 20 | GW6 deadline in under 24h | 2026-10-10T06:00:00-04:00 | deadline:2026-27:6:24h |
| 6 | 2026-10-10T10:00 | America/New_York | 20 | GW6 deadline in under 24h | 2026-10-10T06:00:00-04:00 | deadline:2026-27:6:24h |
| 6 | 2026-10-10T10:00 | America/New_York | 1.5 | GW6 deadline in under 2h | 2026-10-10T06:00:00-04:00 | deadline:2026-27:6:2h |
| 6 | 2026-10-10T10:00 | America/New_York | -0.1 | — | — | — |
| 9 | 2026-10-31T11:00 | Europe/London | 30 | — | — | — |
| 9 | 2026-10-31T11:00 | Europe/London | 20 | GW9 deadline in under 24h | 2026-10-31T11:00:00+00:00 | deadline:2026-27:9:24h |
| 9 | 2026-10-31T11:00 | Europe/London | 20 | GW9 deadline in under 24h | 2026-10-31T11:00:00+00:00 | deadline:2026-27:9:24h |
| 9 | 2026-10-31T11:00 | Europe/London | 1.5 | GW9 deadline in under 2h | 2026-10-31T11:00:00+00:00 | deadline:2026-27:9:2h |
| 9 | 2026-10-31T11:00 | Europe/London | -0.1 | — | — | — |
| 9 | 2026-10-31T11:00 | Asia/Kolkata | 30 | — | — | — |
| 9 | 2026-10-31T11:00 | Asia/Kolkata | 20 | GW9 deadline in under 24h | 2026-10-31T16:30:00+05:30 | deadline:2026-27:9:24h |
| 9 | 2026-10-31T11:00 | Asia/Kolkata | 20 | GW9 deadline in under 24h | 2026-10-31T16:30:00+05:30 | deadline:2026-27:9:24h |
| 9 | 2026-10-31T11:00 | Asia/Kolkata | 1.5 | GW9 deadline in under 2h | 2026-10-31T16:30:00+05:30 | deadline:2026-27:9:2h |
| 9 | 2026-10-31T11:00 | Asia/Kolkata | -0.1 | — | — | — |
| 9 | 2026-10-31T11:00 | America/New_York | 30 | — | — | — |
| 9 | 2026-10-31T11:00 | America/New_York | 20 | GW9 deadline in under 24h | 2026-10-31T07:00:00-04:00 | deadline:2026-27:9:24h |
| 9 | 2026-10-31T11:00 | America/New_York | 20 | GW9 deadline in under 24h | 2026-10-31T07:00:00-04:00 | deadline:2026-27:9:24h |
| 9 | 2026-10-31T11:00 | America/New_York | 1.5 | GW9 deadline in under 2h | 2026-10-31T07:00:00-04:00 | deadline:2026-27:9:2h |
| 9 | 2026-10-31T11:00 | America/New_York | -0.1 | — | — | — |

## Webhook failure paths (no request is sent)

* `https://hooks.invalid/x` → rejected: cannot resolve 'hooks.invalid'
* `https://localhost/x` → rejected: 'localhost' resolves to a non-public address
* `https://10.1.2.3/x` → rejected: '10.1.2.3' resolves to a non-public address
* `http://example.org/x` → rejected: only https webhooks are allowed
