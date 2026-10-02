# Baseline forecasts — walk-forward evaluation

Snapshot `snap_b64560a8c4f434ad984e`; decision cutoffs: every GW of 2023-24, 2024-25, 2025-26 (114 cutoffs); targets: next 5 GWs; unit: player × target GW (double GWs summed); populations: all pool players with a fixture and *regulars* (recency-weighted start rate ≥ 0.5 at the cutoff).

| Population | Horizon | Forecaster | n | RMSE | MAE | Bias | Spearman |
|---|---:|---|---:|---:|---:|---:|---:|
| all | 0 | ppg_availability | 84999.0 | 2.034 | 1.035 | +0.009 | 0.677 |
| all | 0 | recent_form | 84999.0 | 2.037 | 1.027 | +0.005 | 0.678 |
| all | 0 | fpl_style | 84999.0 | 2.053 | 0.990 | -0.094 | 0.688 |
| all | 0 | position_mean | 84999.0 | 2.361 | 1.491 | +0.029 | 0.092 |
| all | 1 | ppg_availability | 82509.0 | 2.070 | 1.065 | +0.012 | 0.654 |
| all | 1 | recent_form | 82509.0 | 2.080 | 1.060 | +0.008 | 0.656 |
| all | 1 | fpl_style | 82509.0 | 2.103 | 1.028 | -0.089 | 0.663 |
| all | 1 | position_mean | 82509.0 | 2.366 | 1.493 | +0.029 | 0.091 |
| all | 2 | ppg_availability | 79619.0 | 2.096 | 1.088 | +0.014 | 0.635 |
| all | 2 | recent_form | 79619.0 | 2.108 | 1.085 | +0.010 | 0.636 |
| all | 2 | fpl_style | 79619.0 | 2.133 | 1.056 | -0.087 | 0.643 |
| all | 2 | position_mean | 79619.0 | 2.360 | 1.491 | +0.029 | 0.090 |
| all | 3 | ppg_availability | 76771.0 | 2.119 | 1.107 | +0.015 | 0.620 |
| all | 3 | recent_form | 76771.0 | 2.129 | 1.103 | +0.010 | 0.622 |
| all | 3 | fpl_style | 76771.0 | 2.160 | 1.077 | -0.085 | 0.628 |
| all | 3 | position_mean | 76771.0 | 2.358 | 1.490 | +0.027 | 0.089 |
| all | 4 | ppg_availability | 72798.0 | 2.127 | 1.118 | +0.016 | 0.606 |
| all | 4 | recent_form | 72798.0 | 2.131 | 1.112 | +0.010 | 0.610 |
| all | 4 | fpl_style | 72798.0 | 2.165 | 1.087 | -0.083 | 0.615 |
| all | 4 | position_mean | 72798.0 | 2.340 | 1.477 | +0.023 | 0.086 |
| regulars | 0 | ppg_availability | 24475.0 | 3.125 | 2.284 | +0.182 | 0.321 |
| regulars | 0 | recent_form | 24475.0 | 3.135 | 2.292 | +0.207 | 0.335 |
| regulars | 0 | fpl_style | 24475.0 | 3.148 | 2.265 | +0.161 | 0.367 |
| regulars | 0 | position_mean | 24475.0 | 3.538 | 2.157 | -1.539 | 0.110 |
| regulars | 1 | ppg_availability | 23848.0 | 3.143 | 2.312 | +0.234 | 0.293 |
| regulars | 1 | recent_form | 23848.0 | 3.163 | 2.325 | +0.258 | 0.302 |
| regulars | 1 | fpl_style | 23848.0 | 3.193 | 2.312 | +0.217 | 0.327 |
| regulars | 1 | position_mean | 23848.0 | 3.515 | 2.138 | -1.485 | 0.106 |
| regulars | 2 | ppg_availability | 23071.0 | 3.152 | 2.333 | +0.279 | 0.269 |
| regulars | 2 | recent_form | 23071.0 | 3.173 | 2.350 | +0.302 | 0.277 |
| regulars | 2 | fpl_style | 23071.0 | 3.205 | 2.340 | +0.261 | 0.297 |
| regulars | 2 | position_mean | 23071.0 | 3.474 | 2.113 | -1.433 | 0.103 |
| regulars | 3 | ppg_availability | 22301.0 | 3.171 | 2.352 | +0.304 | 0.253 |
| regulars | 3 | recent_form | 22301.0 | 3.187 | 2.366 | +0.326 | 0.260 |
| regulars | 3 | fpl_style | 22301.0 | 3.229 | 2.366 | +0.288 | 0.276 |
| regulars | 3 | position_mean | 22301.0 | 3.458 | 2.104 | -1.405 | 0.100 |
| regulars | 4 | ppg_availability | 21231.0 | 3.181 | 2.361 | +0.318 | 0.233 |
| regulars | 4 | recent_form | 21231.0 | 3.188 | 2.368 | +0.335 | 0.245 |
| regulars | 4 | fpl_style | 21231.0 | 3.237 | 2.372 | +0.302 | 0.258 |
| regulars | 4 | position_mean | 21231.0 | 3.430 | 2.082 | -1.371 | 0.090 |
