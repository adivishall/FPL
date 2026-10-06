# Baseline forecasts — walk-forward evaluation

Snapshot `snap_b64560a8c4f434ad984e`; decision cutoffs: every GW of 2023-24, 2024-25, 2025-26 (114 cutoffs); targets: next 5 GWs; unit: player × target GW (double GWs summed); populations: all pool players with a fixture and *regulars* (recency-weighted start rate ≥ 0.5 at the cutoff).

| Population | Horizon | Forecaster | n | RMSE | MAE | Bias | Spearman |
|---|---:|---|---:|---:|---:|---:|---:|
| all | 0 | ppg_availability | 86021.0 | 2.031 | 1.035 | +0.021 | 0.671 |
| all | 0 | recent_form | 86021.0 | 2.035 | 1.028 | +0.018 | 0.672 |
| all | 0 | fpl_style | 86021.0 | 2.052 | 0.991 | -0.080 | 0.681 |
| all | 0 | position_mean | 86021.0 | 2.350 | 1.487 | +0.043 | 0.093 |
| all | 1 | ppg_availability | 83572.0 | 2.068 | 1.066 | +0.028 | 0.648 |
| all | 1 | recent_form | 83572.0 | 2.080 | 1.061 | +0.024 | 0.650 |
| all | 1 | fpl_style | 83572.0 | 2.103 | 1.028 | -0.073 | 0.658 |
| all | 1 | position_mean | 83572.0 | 2.356 | 1.491 | +0.047 | 0.091 |
| all | 2 | ppg_availability | 81092.0 | 2.100 | 1.092 | +0.026 | 0.629 |
| all | 2 | recent_form | 81092.0 | 2.114 | 1.089 | +0.022 | 0.630 |
| all | 2 | fpl_style | 81092.0 | 2.140 | 1.059 | -0.074 | 0.637 |
| all | 2 | position_mean | 81092.0 | 2.357 | 1.492 | +0.043 | 0.090 |
| all | 3 | ppg_availability | 78864.0 | 2.127 | 1.111 | +0.028 | 0.613 |
| all | 3 | recent_form | 78864.0 | 2.139 | 1.108 | +0.024 | 0.615 |
| all | 3 | fpl_style | 78864.0 | 2.169 | 1.081 | -0.072 | 0.621 |
| all | 3 | position_mean | 78864.0 | 2.359 | 1.492 | +0.042 | 0.090 |
| all | 4 | ppg_availability | 76393.0 | 2.151 | 1.127 | +0.019 | 0.600 |
| all | 4 | recent_form | 76393.0 | 2.158 | 1.121 | +0.014 | 0.603 |
| all | 4 | fpl_style | 76393.0 | 2.191 | 1.096 | -0.079 | 0.608 |
| all | 4 | position_mean | 76393.0 | 2.364 | 1.489 | +0.029 | 0.087 |
| regulars | 0 | ppg_availability | 24758.0 | 3.125 | 2.290 | +0.212 | 0.315 |
| regulars | 0 | recent_form | 24758.0 | 3.138 | 2.300 | +0.238 | 0.328 |
| regulars | 0 | fpl_style | 24758.0 | 3.153 | 2.274 | +0.194 | 0.361 |
| regulars | 0 | position_mean | 24758.0 | 3.520 | 2.146 | -1.508 | 0.111 |
| regulars | 1 | ppg_availability | 24125.0 | 3.147 | 2.322 | +0.271 | 0.287 |
| regulars | 1 | recent_form | 24125.0 | 3.170 | 2.336 | +0.296 | 0.295 |
| regulars | 1 | fpl_style | 24125.0 | 3.202 | 2.323 | +0.256 | 0.321 |
| regulars | 1 | position_mean | 24125.0 | 3.496 | 2.127 | -1.452 | 0.107 |
| regulars | 2 | ppg_availability | 23474.0 | 3.165 | 2.348 | +0.308 | 0.262 |
| regulars | 2 | recent_form | 23474.0 | 3.191 | 2.366 | +0.332 | 0.270 |
| regulars | 2 | fpl_style | 23474.0 | 3.225 | 2.357 | +0.291 | 0.292 |
| regulars | 2 | position_mean | 23474.0 | 3.469 | 2.110 | -1.410 | 0.103 |
| regulars | 3 | ppg_availability | 22873.0 | 3.190 | 2.372 | +0.334 | 0.244 |
| regulars | 3 | recent_form | 22873.0 | 3.211 | 2.387 | +0.357 | 0.251 |
| regulars | 3 | fpl_style | 22873.0 | 3.253 | 2.387 | +0.320 | 0.269 |
| regulars | 3 | position_mean | 22873.0 | 3.458 | 2.104 | -1.380 | 0.101 |
| regulars | 4 | ppg_availability | 22218.0 | 3.223 | 2.392 | +0.326 | 0.226 |
| regulars | 4 | recent_form | 22218.0 | 3.234 | 2.399 | +0.346 | 0.237 |
| regulars | 4 | fpl_style | 22218.0 | 3.282 | 2.403 | +0.314 | 0.252 |
| regulars | 4 | position_mean | 22218.0 | 3.467 | 2.103 | -1.368 | 0.091 |
