# GPU benchmark: 2026-09-24

Private source rows remain in the local database. This summary contains only aggregate metrics.
Every learner gets the same rows, features and three forward windows (later 40–60%, 60–80%,
80–100% of match start times), each calibrated on the preceding 10% and fitted only on earlier
matches. Parameters were fixed before evaluation. Single runs on an RTX 4090 with 20 CPU threads
(learners limited to 8); seconds include fit, calibration and prediction.

## Production scale: every WPA training moment

1,807,372 two-minute snapshots from 12,421 matches with an item catalog. This is the workload
`pipeline/wpa.py` actually trains on.

| Model | Forward AUC, windows 1 / 2 / 3 | Brier | Seconds |
|---|---|---|---|
| logistic | 0.7735 / 0.7818 / 0.7711 | 0.1930 / 0.1897 / 0.1938 | 1.0 / 1.4 / 1.7 |
| Extra Trees | 0.7773 / 0.7828 / 0.7738 | 0.1914 / 0.1893 / 0.1926 | 5.4 / 9.1 / 17.4 |
| HistGB (`cpu`) | 0.7783 / 0.7835 / 0.7742 | 0.1903 / 0.1885 / 0.1920 | 7.4 / 10.1 / 13.1 |
| XGBoost CPU | 0.7781 / 0.7837 / 0.7743 | 0.1905 / 0.1884 / 0.1920 | 2.6 / 4.1 / 5.7 |
| XGBoost CUDA | 0.7785 / 0.7836 / 0.7745 | 0.1903 / 0.1885 / 0.1919 | 2.6 / 2.6 / 3.0 |

Boosted trees tie for held-out quality; the GPU is fastest and its time stays flat as the training
window grows (CPU time roughly doubles). Cold-player AUCs track the main AUCs within 0.002.

## Small scale: one state before minute 10

123,460 participants in 12,346 games lasting over 10 minutes.

| Model | Forward AUC, windows 1 / 2 / 3 | Seconds |
|---|---|---|
| logistic | 0.7608 / 0.7668 / 0.7578 | 0.11 / 0.12 / 0.13 |
| Extra Trees | 0.7594 / 0.7641 / 0.7572 | 0.33 / 0.41 / 0.50 |
| HistGB (`cpu`) | 0.7416 / 0.7530 / 0.7488 | 1.2 / 1.3 / 1.4 |
| XGBoost CPU | 0.7509 / 0.7596 / 0.7527 | 0.28 / 0.36 / 0.43 |
| XGBoost CUDA | 0.7462 / 0.7575 / 0.7517 | 0.86 / 0.70 / 0.70 |

At this size GPU launch and transfer overhead exceed the fit itself, and a linear model is best.

## Where each stage runs

| Stage | Hardware | Why |
|---|---|---|
| JSON parse, game-state reconstruction, export records | all CPU cores but two, cached per match | Branchy Python/JSON work a GPU cannot run. Vectorized and cached: 55 s cold for 12,374 matches (was ~10 min single-threaded), then only new matches |
| WPA outcome model (5 cross-fitted folds on ~1.8M rows) | GPU (`--backend auto`) | Largest fit; GPU fastest with equal held-out quality |
| Platt calibration, metrics | CPU | One-feature logistic fits; microseconds of work |
| Lane model, pairwise item research | CPU | Thousands to ~100k rows: the small-scale result applies |
| Export reliability gate (split-half, placebo) | CPU, numpy | Was 486 of 566 s of export: `statistics.pstdev` computes in exact fractions. Now vectorized group sums; decisions identical to the old code (tested against it). Placebo shuffles use numpy's generator, so the displayed placebo share varies within its random noise; it never decides what is shown |

`auto` performs a real one-round CUDA fit and falls back to the same XGBoost model on CPU if the
GPU cannot train. A CUDA run that silently trained on CPU still fails the device check.

The earlier 94,210-row minute-10 run from the same morning (CUDA slower than CPU XGBoost) was
correct for its size and misleading for the production workload; this table replaces it.

No causal or complete-route validation passed as a result of this experiment. Outcome AUC is not
item impact. The most played observed route remains the default. Item-score reliability gates are
unchanged.

See [protocol and commands](live-data-and-gpu.md) and [the optimized build plan](optimized-build-plan.md).
