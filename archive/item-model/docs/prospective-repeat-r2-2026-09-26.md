# Unchanged item policy: independent repeat on new games (r2)

> **Status after review (2026-09-26): not confirmed.** The unchanged policy failed its pre-registered test (r1)
> and passed a repeat run after that failure (r2). Pooled gain per changed decision: +12.2 pp [4.1, 20.3],
> implausibly large and at odds with development (−16 pp). Gain across all decisions: about +0.007 pp. Not
> confirmed, and not a basis for recommendations. See `docs/item-policy-structural-fix.md` sections 10–11.

**Run completed 2026-09-26 04:53 UTC.** The r2 evaluator was locked in commit `4266b4d` before the new outcomes were opened. It reused the exact r1 training-game IDs, model code, feature definitions, candidate rules, backend, seed, and pass criteria. The r1 and r2 reports have identical `fit`, `gate`, `xgb`, and `decision_xgb` sections. R2 excluded every r1 training and test ID; overlap with each was zero. The new games were *collected* after r1 finished, but some may have been played earlier. Both tests use patch 16.19.

| Evaluation | Games evaluated | Branch decisions | Policy changes | Estimated gain per changed decision | Estimated gain across all decisions | Original verdict |
|---|---:|---:|---:|---:|---:|---|
| r1, 2026-09-25 | 43,735 | 650,104 | 365 (0.056%) | +8.06 pp [−2.91, +19.02] | +0.0045 pp [−0.0016, +0.0107] | Fail: interval includes zero |
| r2, 2026-09-26 | 44,295 | 658,582 | 346 (0.053%) | +17.28 pp [+5.13, +29.43] | +0.0091 pp [+0.0026, +0.0155] | Pass |

Intervals are 95% match-clustered intervals from the observational doubly robust evaluator. R2 listed 44,303 games; eight produced no eligible branch row. The 589,363 fit rows (38,974 games) were unchanged. Of r2's 658,582 decisions, 94.7% had a supported pair, but the three-point preference margin, overlap and support gates left only 346 actual departures from the common route. Every r2 region had a positive point estimate among departures, but each region's interval still crossed zero.

**Interpretation:** This is a valid new out-of-training performance test and a positive independent repeat for the unchanged policy. R1 failed and R2 passed; report both. Running r2 after seeing r1's failure means a favorable r2 alone is not a one-shot confirmation. More importantly, the policy acts in roughly one of every 1,900 eligible branch decisions. Even the positive r2 result does not yet yield broadly useful recommendations, and observational adjustment cannot eliminate unmeasured buyer selection. The website has not been changed on the basis of this result.

Private artifacts: `data/research/prospective/r1/report.json`, `data/research/prospective/r2/report.json`, the corresponding `ledger.json` files, and the disjoint game-ID lists. The r2 code and lock are `pipeline/prospective_r2.py` and `pipeline/prospective-r2.lock.json` on the `claude/vigilant-goldberg-9wnnw9` branch; raw matches and per-game decisions remain private.
