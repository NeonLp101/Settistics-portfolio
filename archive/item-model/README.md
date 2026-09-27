# The item-recommendation model (archived 2026-09-27)

**Verdict:** a model that recommends items because it knows which one wins more cannot be built from
observational match data at this scale. Real item-versus-item effects on winning are about 1 percentage
point or less, one pair's estimate carries 1–2 points of uncertainty, and the biases (who buys an item,
and against which team) are as large as the effects themselves. Collecting more games shrinks the noise
but not the bias, so it would have made the wrong answers more confident, not right.

The work was not wasted: it stopped three confident, wrong claims from reaching the site, and it left
tooling and a validated win-probability model that the rest of Settistics still uses.

## Thesis

One shared model, trained on every champion's Riot match timelines, could learn the **causal** effect of
an item choice on winning: "in this champion, role and matchup, buying B instead of the usual A raises
your win chance by X points". Pooling across champions would let rare matchups borrow strength, and the
site could then recommend items that beat the most common build, not just repeat it.

## Approach, in order

1. **Win Probability Added (WPA).** Predict the winner from game state, then score each item by
   (won − predicted) averaged over its buyers. The per-item scores did not reproduce: split-half agreement
   was about 0 in every slot. The site labels them "not reproducible yet".
2. **One Kai'Sa contrast** (Nashor path vs. Hearthbound Axe, 3rd item), with games from 2026-09-24 14:00
   UTC sealed for a one-time test. Withdrawn on 2026-09-25 before evaluation, in favour of a general model.
3. **The general recommender** (`recommender.py`). `decisions.py` extracts every dated purchase and the
   game state before it; `branches.py` pairs the two most common routes per champion, role and stage.
   XGBoost models predict final win (with the chosen route as a feature), the choice itself, and five
   5-minute outcomes (gold, takedowns, deaths, damage, time alive). The policy recommends B only if its
   predicted gain exceeds 3 points with overlap. Development: AUC 0.778, but it changed 135 of 115,564
   decisions (0.12%), with an estimated gain of −0.019 points [−0.038, 0.000].
4. **Two pre-registered prospective rounds** on games it had never seen:

   | Round | Test games | Changed decisions | Gain per changed decision | Verdict |
   |---|---:|---:|---|---|
   | r1 | 43,735 | 365 (0.056%) | +8.1 pp [−2.9, +19.0] | fail |
   | r2 | 44,295 | 346 (0.053%) | +17.3 pp [+5.1, +29.4] | pass, then relabelled "not confirmed" |

   Development had shown −16 pp for the same statistic. Across all decisions, the gain was about
   +0.007 pp.
5. **r3, the full-data model** (2,023,088 decisions from 130,886 games), shown on the site as a labelled
   research preview for 995 pairs. For 267 of 864 supported pairs, the win model never split on the
   choice at all, so its "prediction" was identical for both items. Those were relabelled
   "indistinguishable".
6. **Per-pair doubly robust estimates** (`dr_pairs.py`): match-clustered intervals, a permutation null to
   calibrate them, independent halves for replication, and Benjamini-Hochberg across all pairs. 7 of 864
   pairs survived.
7. **Empirical-Bayes shrinkage** (`dr_shrink.py`), pooling the same item pair across champions. Outside
   boots the signal was exactly zero. The overall result rested on one boots pair, Plated Steelcaps vs.
   Mercury's Treads; without it, the result disappeared.
8. **An independent review and a supervisor verdict** (`docs/item-policy-structural-fix.md`). With about
   350 changed decisions per round, a round can only detect effects of ±16 points; a realistic 1-point
   effect needs about 95,000 changed decisions, or roughly 11 million games at this departure rate. No
   further rounds of this architecture were run.
9. **A diagnostic on 1.9M decisions** (`effects.py`) and a proposed rebuild as a pregame heterogeneous-
   effect model ("Stage 1"). It was cut before any code was written, once properly calibrated numbers
   showed no variation within an item pair left for such a model to find.
10. **The last two candidates, tested against their confounders** (`boots_confound_check.py`,
    `defensive_boots_deepcheck.py`):
    - **Gluttonous vs. Berserker's Greaves** (+2.26 pp for Gluttonous across all players) shrank to
      +1.20 pp among players who bought both, and to +0.02 pp [−1.08, +1.11] comparing each of those
      players with themselves. The effect belonged to *who* buys Gluttonous, not to the item.
    - **Plated Steelcaps vs. Mercury's Treads** (−1.14 pp, Steelcaps favoured) survived the
      within-player check and independent halves. It then fell to −0.09 pp [−0.48, +0.30] once each
      enemy team's real damage mix was added. Split by how magic-heavy the enemy was, it runs −1.99 /
      −0.22 / +1.42 pp: there is no universal winner, only "magic resist against magic damage", which
      players already know. Whether even that split is causal was left open.

## Hardships

- **The effects are small.** About 1 point or less per item choice, against 1–2 points of uncertainty
  per pair. Most pairs cannot be told apart, and outside boots nothing was found at all.
- **The model almost never disagreed with players.** A departure rate of about 0.05% meant each test
  round had a few hundred useful decisions, far too few for the effect sizes involved.
- **Every apparent effect was mostly bias.** Player skill and champion mastery (greaves), enemy team
  composition (defensive boots), and selection on the most extreme predictions (r2's +17 pp). Adjusting
  for what we could measure never fully removed what we could not.
- **The architecture hid the choice.** With the item as one feature among ~40, the trees rarely split on
  it (the "S-learner" problem). The game state was also read at the moment of the distinguishing
  purchase, a median 144 s and ~1,000 gold after the shopping trip began, so part of it was a
  consequence of the choice, not its cause.
- **Quiet bugs, each able to fake or hide a result:**
  - a salted CRC32 fold split collapsed onto the unsalted one (CRC is affine), so "independent" inner
    folds trained on zero rows;
  - rare-arm propensities were off by up to 36× because XGBoost's minimum leaf weight is in hessian
    units;
  - the evaluator's outcome model shared rows with the decision model it was judging;
  - multiple-testing correction was first applied per group rather than across all pairs;
  - replication was first selected on the pooled estimate that already contained the replication half.
- **Our own checks needed checking.** Pair orientation (which item is "A" differs by champion) was
  misread twice; a negative control counted repeat purchases of the same item as "skill"; and a
  within-player estimate was first read backwards.

## Why it ended

- **The ceiling is known.** The best outcome was "no clear difference" on nearly every pair, plus
  perhaps one composition-conditioned boots label after a sealed test of about 100,000 games. That label
  would tell players what they already know.
- **More data does not fix bias.** Removing it needs randomized choices or far more changed decisions
  per pair than League data can supply.
- **The rigor had already paid for itself.** Without it, the site would be showing r2's +17 pp,
  "Gluttonous is better" and "Steelcaps is better" as findings. All three were wrong.
- **Anything broader would be a new bet**, not a continuation: larger decisions (whether to buy any
  defensive item, build archetypes, runes, timing) may have effects big enough to measure, but that is
  unproven.

## What survives

- **WPA as prediction**, not as item credit: the win-probability model is accurate and calibrated, and
  prediction is what observational data supports. It still trains on every unsealed game.
- **Empirical-Bayes shrinkage** (`dr_shrink.py`) is the right tool for making small-sample descriptive
  win rates honest.
- **The methods**: independent halves, permutation-calibrated intervals, within-player comparisons and
  negative controls apply to any future claim that "X causes wins".

## This folder

| Folder | Contents |
|---|---|
| `pipeline/` | the recommender, its exports, the prospective rounds and their lock files, and every diagnostic above |
| `tests/` | their tests (`test_candidates.py` was split out of `tests/test_pipeline.py`) |
| `docs/` | the model plan, release status, structural-fix review (sections 10–12 hold the final verdicts), the r2 report, the Kai'Sa study, the optimized-path plan, and `current-state.md` as it stood when archived |
| `scripts/` | the site's preview schema |
| `data/recommendations.json` | the last exported research preview (r3) |

**This code does not run from here.** It imports modules from `pipeline/` and helpers that were removed
from `engine.py` when it was archived (`enemy_ap_share`, `champion_ap_shares`, `SEALED_FROM_MS`). Commit
`39be385` is the last runnable state. Private outputs (`data/research/`, `data/*branches*.sqlite`,
`data/*decisions*.sqlite`) are gitignored and remain on the collection machine.

The seal on games starting at or after 2026-09-26 05:27 UTC, set for r3's evaluation, is still held
back from training and from the site (`engine.FUTURE_SEAL_MS`).
