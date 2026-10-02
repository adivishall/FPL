# Team strength model — rolling-origin tuning

Snapshot `snap_b64560a8c4f434ad984e`; grid of 27 configs (half-life days × goals weight α × prior SD); walk-forward over every GW; selection by match log-likelihood on all seasons *before* the target.

| Target | Tuned on | Selected (hl, α, sd) | log-lik selected | default | goals-only | league avg | CS Brier selected | league avg |
|---|---|---|---:|---:|---:|---:|---:|---:|
| 2024-25 | 2023-24 | (60, 0.35, 0.6) | -2.9606 | -2.9567 | -3.0069 | -3.0813 | 0.1707 | 0.1812 |
| 2025-26 | 2023-24, 2024-25 | (120, 0.35, 0.6) | -2.8954 | -2.8919 | -2.9249 | -2.9495 | 0.1815 | 0.1902 |

Production (2026-27) configuration, selected on 2023-24, 2024-25, 2025-26: half-life 120 d, α 0, prior SD 0.6.
