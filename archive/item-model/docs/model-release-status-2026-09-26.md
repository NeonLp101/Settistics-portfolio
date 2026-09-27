# Settistics item model: release status (26 September 2026)

## What is live

[Settistics](https://settistics.netlify.app/) now includes the **r3 full-data item model as a research preview**. It covers 995 accepted champion/role/item-slot comparisons for the first, second, and third item and upgraded boots; 864 have enough training support to display predictions. The preview shows model-expected final win chance and five-minute gold-lead change, takedowns, deaths, champion damage, and time alive. It pools across matchups and regions, so it is **not matchup-specific advice**. It shows no uncertainty interval for these averaged predictions.

The site's build recommendation remains the observed route. A model lean is a prediction, not evidence that switching items improves wins. The r3 model has **not yet been evaluated on its newly sealed games**. Its `evaluationMatches` is zero, and the site says “new-game policy test pending.” This release does not claim a validated item advantage or a full-route recommendation.

Deployment: `main` commit `58b6a51`, [Netlify deployment](https://app.netlify.com/projects/settistics/deploys/6ab76acfc0ec0373d49fde9e). The public file is [the schema-checked aggregate preview](../data/public/recommendations.json); model weights, decisions, raw matches, and player identifiers remain private. The site's observed-data export currently reports 45,160 locally collected matches. That display count is from a separate, older aggregate export and is **not** the r3 training count.

## Training and seal

- Frozen cutoff: **2026-09-26 05:27:00 UTC** (`gameStartTimestamp >= 1790400420000` is reserved for later evaluation).
- Training cohort: **130,902 eligible general-crawl games** from EUW, KR, NA, and VN, all on patch 16.19. Targeted Kai'Sa focus games were excluded to avoid changing the all-champion sampling mix.
- The unchanged shared method fitted on **130,886 games** that yielded **2,023,088 branch-choice rows**. Sixteen otherwise eligible games supplied no usable branch row. It learned final win, item choice, five five-minute outcomes, and the base/enriched decision models.
- The frozen game-ID list has SHA-256 `4db53590b57ef377ba4d1f93978a9cb08760f2d0f0dbe370c8e6248f13ad985f`. Private local records are `data/research/prospective/r3/model/report.json`, `data/research/prospective/r3/seal.json`, and `data/research/prospective/r3/training-snapshot.json`. The locked training procedure is documented on [the r3 research branch](https://github.com/NeonLp101/Settistics/blob/codex/fulltrain-seal-20260926/docs/fulltrain-seal-r3-2026-09-26.md).
- Games that **started before** the cutoff but arrived afterward are in neither this frozen training snapshot nor the future-start test. No post-cutoff game was used for this fit or the public preview.

## Verification and collection

The public exporter reads only pre-purchase context and action columns from the frozen branch table. It validates saved model hashes, refuses future-start or focus rows, and emits only pair-level aggregates. The site build validates a closed schema and rejects a supported-improvement claim. Before deployment, `npm test` passed **121 Python and 55 Node tests**. The live site returned HTTP 401 without the password, as expected from its existing gate.

All four general regional crawlers were running and saving games after the restart and write-lock fix (`cd0b313`). Collection can continue without changing the frozen fit. Their status should be checked again before the prospective evaluation; this is a release-time observation, not a promise of ongoing uptime.

## Post-release fix: honest labeling for pairs the model can't distinguish (26 September 2026)

The preview was showing some comparisons as "No model lean" with a full table of identical predicted
numbers for both items — misleading, since most of those ties are the win model never having used the
item-choice features for that population, not a measured zero effect. A supervisor review (Opus) read
`pipeline/recommender.py` and `pipeline/recommendation_export_r3.py` and confirmed it against the saved
r3 models: the win classifier has **0 splits** on `arm_b` and only **0.047% of total gain** on the
chosen-item feature, so state features decide almost every leaf regardless of which item was picked.
Swapping the arm produced literally 0 change in the win model's raw output for 267 of 864 supported
pairs (checked per training row, not just the rounded aggregate). A second, narrower bug compounds this:
item IDs are encoded as raw ordinal numbers, so a split can only separate two items when a threshold
falls between their IDs — adjacent IDs (e.g. Shadowflame/Stormsurge, 4645/4646) can never be told apart
by this encoding no matter how much data exists.

Fix (this does not retrain or touch the frozen r3 model or its sealed evaluation cohort — export-time
only): `recommendation_export_r3.py` now scores every training row under both arms and tracks the
largest per-row swing in the win model's raw output; if that swing is exactly zero across the whole
population, `modelLean` is exported as the new value `"indistinguishable"` instead of `"none"`.
`scripts/recommendation-schema.mjs` accepts the new value, and `guide.js` shows "the model can't tell
these two choices apart" with the observed-route fallback instead of a table of identical numbers. This
is a labeling honesty fix, not a signal-detection fix — the underlying S-learner architecture (item
choice folded in as 2 of roughly 40 input features) and the ordinal item-ID encoding still need the
work below before the model can be expected to find real item effects. All 121 Python and 55 Node
tests pass with the change; local browser check confirmed the new copy renders (e.g. Sett TOP, 1st
item, Stridebreaker vs Blade of The Ruined King).

Supervisor's remaining prioritized punch list:
1. **Done, 2026-09-26** (`pipeline/dr_pairs.py`, `tests/test_dr_pairs.py`): a doubly robust per-pair
   estimate, cross-fitting fresh win and propensity models on two match-grouped halves of the same
   frozen r3 training rows (never reusing the shipped r3 weights, which were fit on these same rows and
   would bias the estimate toward zero), with match-clustered confidence intervals, joined against the
   shipped `modelLean` for comparison.
   - **First pass numbers were naive and are superseded below.** BH correction was applied within each
     shipped-lean group instead of across the whole family, and nothing checked whether the per-pair
     intervals were actually calibrated. A second supervisor review flagged this before the numbers were
     trusted; see the hardening pass.
2. **Done, 2026-09-26, same day (hardening pass 1, still `pipeline/dr_pairs.py`):** three checks added
   before any headline count: an overlap filter (drop a pair if its propensity extreme share exceeds the
   shipped policy's own gate); a within-pair arm-label permutation refit as a null calibration check
   (real z-scores had std 1.20 against an expected 1.0, so every standard error was inflated by that
   factor); and a split-half sign-agreement check, at the time selecting survivors from the pooled
   (both-halves) estimate. Benjamini-Hochberg applied once across the whole eligible family, not per
   lean group. **This pass's numbers are also superseded -- see pass 2.** A third supervisor review found
   the overlap filter alone could not catch every failure mode: Ezreal BOTTOM slot1 (46 route-B rows of
   16,039) had extreme_share 0.0 yet its out-of-fold propensity averaged 0.105 against an observed route-B
   share of 0.0029 -- 36.7x too high -- which pulled its doubly robust estimate toward the outcome model's
   own (circular) contrast and flipped its adjusted sign versus the raw gap (raw -11.9pp vs. adjusted
   +2.6pp). Root cause: `min_child_weight=20` is measured in hessian units (~p(1-p) per row), so the
   shared propensity model could never afford a leaf for a rare arm and fell back to a badly wrong pooled
   rate. The review also flagged that selecting split-half survivors from the pooled estimate makes
   agreement close to guaranteed, since the pooled estimate already contains both halves being checked.
3. **Done, 2026-09-26, same day (hardening pass 2, still `pipeline/dr_pairs.py`):** the propensity model
   now gets each pair's own empirical route-B rate (smoothed, computed from the training half only) as a
   fixed XGBoost `base_margin` offset, so its trees only need to learn the deviation from that baseline
   instead of needing enough rows to carve out a rare pair's own leaf (`recommender.py`'s `fit`/
   `Fitted.predict` gained an optional `base_margin` parameter, default `None`, to support this; no
   behavior change for any existing caller). A propensity-calibration diagnostic (mean predicted vs.
   observed route-B share, reported, not filtered on) was added to catch a future recurrence. Split-half
   replication now selects Benjamini-Hochberg survivors from half 0 alone and checks sign agreement and
   p<0.05 on half 1 alone -- independent, not close-to-guaranteed. Final, trustworthy result (785/994
   pairs pass ESS>=30 both arms and the overlap filter):
   - **Propensity calibration is now clean: 0/785 pairs off by more than 1.5x** (down from 231/993 pairs
     off by more than 2x before the offset). Null calibration also improved: z std 1.06 against an
     expected 1.0 (was 1.20), 6.0% exceed |z|>1.96 by chance (was 9.5%) -- "well calibrated," no further
     inflation needed.
   - **7 pairs survive family-wide Benjamini-Hochberg correction** (60/785, 7.6%, exclude 0 at 95% versus
     ~5% expected by chance). **6 of the 7 are boots comparisons**, consistent across every pass so far --
     boots are picked partly against the enemy team's damage-type mix, a real confounder this model's
     context does not carry (only the lane opponent is known), so some of this signal may be a proxy for
     that rather than a directly usable item effect.
   - **Independent split-half replication:** only 2 pairs survive Benjamini-Hochberg on half 0 alone
     (expected: less power on half the data), but **both of those 2 replicate on the fully independent
     half 1**, agreeing in sign and clearing p<0.05 there too.
   - Within the `indistinguishable` bucket specifically (244 eligible): **5 pairs still survive** the
     final correction -- unchanged from pass 1's count, so this particular finding was not an artifact of
     the propensity bug. The shipped model is still discarding genuine signal for a handful of pairs, not
     "roughly none," so per the supervisor's decision rule this still points at the next step below.
   - Worked example, Sett TOP 1st item (Stridebreaker vs Blade of The Ruined King, the original
     screenshot): raw win rate 50.2% vs 42.0% (-8.3pp). The doubly robust adjusted estimate is now
     **-8.08pp [-13.71, -2.45], p=0.0049** -- much closer to the raw gap than pass 1's -2.5pp, because the
     propensity offset removed a spurious correction. Individually significant, though it does not clear
     the top-7 family-wide Benjamini-Hochberg cutoff among 785 simultaneous tests.
   - Output is private research data only: `data/research/dr-pairs/report.json` (gitignored, not
     shipped). Caveat unchanged: this adjusts for measured context only, not a causal claim.
4. **Next, not started:** empirical-Bayes shrinkage on the hardened per-pair table (`pipeline/dr_shrink.py`,
   new file).
   - **Grouping key:** parse `pair_id` as `fold|champion|role|stage|patch|route_a|route_b`; group by
     `(stage, lo, hi)` where `lo, hi = sorted((route_a, route_b), key=int)`. Of 315 such groups, 181 have
     only 1 pair and 62 have 2 -- too sparse to estimate a variance per group.
   - **Orientation matters:** define the effect as P(win | hi) − P(win | lo); set `oriented = mean if
     route_b == hi else -mean`. Without this, the same two items on different champions would cancel
     inside a group, since which item is "route A" vs. "route B" per pair_id is arbitrary.
   - **Model:** two variance components instead of one per group -- pair effect = group mean +
     champion-specific deviation (tau-squared-champ); group mean = 0 + item-pair spread
     (tau-squared-group). Estimate both once across all eligible pairs using the (now well-calibrated)
     inflated standard errors, by method of moments or REML. The stage-level fallback is a prior mean of
     0 with variance tau-squared-group, since an oriented stage-wide mean has no meaning (orientation by
     item ID is arbitrary at that level). A pair alone in its group shrinks toward 0 with weight
     `(tau_g^2+tau_c^2)/(tau_g^2+tau_c^2+se^2)`; a pair in a larger group is pulled toward that group's
     precision-weighted mean; an unseen pair on patch day starts from its group's posterior mean, or 0.
   - **Checkpoint before trusting it:** fit the variances on the half-0 estimates, predict half-1, and
     compare inverse-variance-weighted MSE against zero (what the shipped model effectively says), the raw
     half-0 estimate, and the shrunk half-0 estimate, with a group-resampled bootstrap on the MSE
     difference. **Proceed only if shrinkage beats zero with an interval excluding 0**, and only then does
     the shrunk estimate become a product number. **Decide the stopping rule now:** if tau-squared-group
     comes out near 0, or zero wins the checkpoint, stop and keep "can't tell" -- with only 60 pairs
     excluding 0 against ~39 expected by chance at 785 tests, that is a realistic outcome, not a failure
     of the method.
   - The item-ID encoding fix and an R-learner/doubly-robust learner that targets the effect directly are
     deferred: they matter for effects that vary with context (e.g. by matchup), and the site currently
     averages over each pair's whole population, not by matchup.
5. **Done, 2026-09-26, same day** (`pipeline/dr_shrink.py`, `tests/test_dr_shrink.py`, 8 tests including
   Sherman-Morrison-vs-dense-inversion and closed-form-posterior-vs-dense-conditioning checks to rule out
   a subtly wrong formula): implemented exactly per the supervisor's spec above -- a hand-rolled exact
   marginal likelihood (Sherman-Morrison update per group, no dense inversion, no new dependency),
   maximized on an 80x80 log-spaced grid then refined, since the prior mean is fixed at 0 and REML and ML
   coincide. `dr_pairs.py` was extended to expose each half's per-pair table (`half_tables`) so the
   checkpoint doesn't need to refit any model.
   - **Fit on all 785 eligible pairs:** tau_g^2 (item-pair-across-champions variance) = 0.0000949,
     tau_c^2 (champion-specific variance) = 0.0000711 -- both roughly 0.8-0.95pp of standard deviation.
     Both boundary likelihood-ratio tests reject zero (group: delta=33.44, champ: delta=8.23, both well
     past the 2.71 critical value), so there is a real, non-zero shared structure to shrink toward, not
     just noise.
   - **Checkpoint (fit on half 0, predict half 1, 660 pairs / 227 groups):** the raw, unshrunk half-0
     estimate is a bad predictor of held-out half-1 outcomes -- worse than predicting zero (MSE 0.00178
     vs. 0.00113), confirming individual pair-level point estimates are too noisy to trust directly. The
     shrunk estimate is unambiguously better than the raw one (MSE 0.00104; gain vs. raw +0.00077, 95%
     bootstrap interval [+0.00051, +0.00111]). **Against zero it barely clears the pre-declared bar**: gain
     +0.00009, interval [+0.0000005, +0.00020] -- excludes zero, so the pre-registered rule says proceed,
     but the lower bound sits right at the boundary. This is a real but weak signal, not a strong one; it
     should be reported with that caveat rather than as a confident win.
   - **Correction to the split-half numbers reported earlier the same day:** `dr_pairs.py`'s own
     split-half check was, until this step, computed on each half's raw (non-inflated) standard errors
     while the main analysis used the null-calibration-inflated ones -- an inconsistency caught while
     wiring up `half_tables` for this checkpoint, not by a separate review. Recomputed consistently, 0
     pairs now survive Benjamini-Hochberg on half 0 alone (was reported as 2, both replicating, in the
     propensity-offset fix above). This does not contradict the shrinkage checkpoint above, which never
     depended on individual pairs clearing Benjamini-Hochberg on a half -- it uses every eligible pair on
     each half directly -- but the earlier "2/2 replicate" framing was over-precise and should not be
     repeated.
   - Output is private research data only: `data/research/dr-shrink/report.json` (gitignored, not
     shipped), reading `data/research/dr-pairs/report.json`.
6. Global ablation (held-out log loss with/without the action features) not yet started; lower priority
   now that the per-pair result already shows the action features are underused.
7. **Done, 2026-09-26, same day -- conclusion: hold; the result is consistent with enemy-composition
   confounding, not confirmed as one, and not promoted as a real item effect either.** A third supervisor
   review declined to promote the shrinkage checkpoint above despite it
   technically clearing the pre-declared bar, for two reasons: the two cross-fit halves were not actually
   independent (each half's out-of-fold scores came from a model trained partly on the *other* half's
   labels, since `dr_pairs.py` used one shared 2-fold split), and a confounder that is baked into the data
   (not sampling noise) would replicate identically across any split, looking exactly like a real shared
   effect. Two concrete checks were pre-declared to distinguish the two explanations, both implemented and
   run the same day:
   - **Genuinely independent halves.** `dr_pairs.py` gained `independent_halves()`: two match-grouped
     outer halves, each internally 2-fold cross-fit using *only its own rows*, so no row's score ever
     depends on a model that saw the other half's labels. Implementing this surfaced and fixed a sharper
     bug first: salting the inner split with `zlib.crc32(salt + match_id)` did not produce an independent
     split at all. CRC32 is affine over GF(2), so for same-length keys (true of nearly every match_id --
     one fixed-width id per region) a fixed-length salt is a fixed, correlated transform of the unsalted
     hash, not a fresh one; every match's inner fold collapsed onto its own outer half, so one inner fold
     silently trained on zero rows every time. Replaced with a `hashlib.sha256`-based `fold_of()` for
     every fold split in the file, with a regression test asserting a salted and unsalted split of the
     same keys land close to 50/50 rather than being correlated. On genuinely independent halves, the
     result did not weaken: 8 of 786 pairs now survive family-wide Benjamini-Hochberg (was 7), and now 2
     pairs surviving BH on half 0 alone **both** independently replicate (sign agreement and p<0.05) on
     the fully separate half 1 -- a real, if narrow, independent replication, not the pooled-vs-itself
     comparison from earlier the same day.
   - **Boots-vs-slot split and leave-one-group-out**, added to `dr_shrink.py`'s checkpoint. This is what
     settled it: **boots alone does not clear the bar** (gain vs. zero +0.00013 [-0.00003, +0.00037],
     includes 0), **non-boots alone shows no signal at all** (tau_g^2 fit to exactly 0 on half 0; shrunk
     predictions equal the zero baseline exactly), and **dropping the single largest group -- boots,
     Plated Steelcaps (3047) vs. Mercury's Treads (3111), 86 of 786 pairs -- flips the overall checkpoint
     from proceed to not-proceed** (gain vs. zero [-0.0000014, +0.00023], now including 0). The other
     large group (Sorcerer's Shoes vs. Ionian Boots of Lucidity, 61 pairs) does not have this effect.
   - **Conclusion:** the entire "shrinkage beats zero" result rests on one specific boots comparison. This
     is consistent with -- but does not on its own prove -- enemy-composition confounding: boots choices
     are made partly against the enemy team's damage-type mix, a real confounder this model's context does
     not carry (only the lane opponent is known), and Plated Steelcaps vs. Mercury's Treads is the kind of
     defensive choice that pattern would produce. The data show the signal is not robust; they do not
     establish the cause. This matches the supervisor's own pre-declared stopping condition (the gain
     existing only in boots, or disappearing without one big group) for holding the result and keeping
     "can't tell," so **no per-pair or per-group estimate from this work is promoted to a product number.**
     The site's existing "indistinguishable" copy ("not evidence the items perform the same") remains
     accurate and needs no change.
   - **What would actually turn "consistent with confounding" into an answer, whenever this is resumed
     (optional, about half a day, not started):** add an enemy-team damage-mix feature (e.g. the enemy
     team's AP/AD damage share, built from champion identities already in the match data) to
     `recommender.py`'s `Encoder.context`, and rerun `dr_pairs.py` for the 3047/3111 group specifically. If
     its adjusted effect collapses toward 0 once that feature is added, confounding is confirmed. If it
     persists, it is a real, matchup-dependent effect -- and lines up with the "matchup-specific guidance"
     priority already in `docs/current-state.md`. That feature is useful for the next model regardless.
   - **What would settle it prospectively:** a test on the sealed future-start cohort, pre-registered
     (this pair, this metric, this direction) before any post-cutoff game is read, per the general plan's
     evidence rules above -- a within-training-data split cannot rule out a confounder present in every
     game. Not scheduled; nothing is being promoted right now, so there is nothing to pre-register, and the
     sealed cohort needs more games first (see "Next decision" below).
   - All 140 Python and 55 Node tests pass. Output remains private research data only
     (`data/research/dr-pairs/report.json`, `data/research/dr-shrink/report.json`, both gitignored).
8. **Done, 2026-09-26, same day -- the optional follow-up, run: the effect did not collapse.**
   - **Feature** (`pipeline/engine.py`): `champion_ap_shares()` reads each champion's Data Dragon class
     tags from `public/ddragon/static.json` and averages a per-tag AP-share table (Mage 0.85, Support 0.5,
     Assassin 0.45, Tank 0.3, Fighter 0.25, Marksman 0.05 -- an approximate, documented proxy, not a
     measured fact). `enemy_ap_share()` averages this over the five opposing champions, `None` if any is
     unknown or the enemy team isn't exactly five. Wired into `decisions.py`'s per-decision `CONTEXT`
     (reads `info["participants"]`, already in the cached raw match JSON -- no new Riot API calls) and
     `recommender.py`'s shared `CONTEXT_SOURCES`/`CONTEXT_NAMES`/`Encoder.context`. `recommender.fit()` and
     `Fitted.predict()` also gained an unrelated but reused-here `base_margin` passthrough for consistency
     with `dr_pairs.py`'s existing offset technique (no behavior change when omitted). 5 new tests in
     `tests/test_engine.py`.
   - **Backfill, not a re-extraction** (`pipeline/backfill_enemy_ap_share.py`): `enemy_ap_share` didn't
     exist when `data/fulltrain-r3-branches.sqlite` was built. Re-running the full `decisions.py` ->
     `branches.py` extraction would re-derive every other column too and risks not reproducing exactly the
     same frozen row population, so this instead computes the one new value per (match_id, team_id)
     straight from the cached raw match JSON and updates the existing file in place -- an `ADD COLUMN`
     plus indexed `UPDATE`s, nothing else changes. (First attempt had no index on `(match_id, team_id)`,
     making each of ~260K updates a full 2M-row scan; killed and fixed before it finished, then reran in
     ~2.5 minutes.) 2 tests in `tests/test_backfill_enemy_ap_share.py`. Result: 261,601 (match_id, team_id)
     pairs updated across 130,886 matches; 3,849 left `NULL` (unknown champion or non-5v5 game mode).
   - **Rerunning `dr_pairs.py` and `dr_shrink.py` with the feature included: the Plated Steelcaps (3047) vs.
     Mercury's Treads (3111) result is essentially unchanged.** Overall checkpoint gain vs. zero: +0.00008
     [+0.00001, +0.00021] (was +0.00008 [+0.00001, +0.00021]). Non-boots alone: still exactly 0 signal
     (`tau_g^2` fits to 0). Boots alone: still doesn't clear the bar (+0.00015 [-0.00000, +0.00041]).
     Leaving out the 3047/3111 group: still flips the checkpoint to not-proceed ([-0.00000, +0.00023]).
     Per the supervisor's own framing (**"if its effect collapses toward 0, confounding is confirmed; if it
     persists, it's a real effect that depends on the matchup"**), this result did not collapse -- it
     persisted essentially unchanged after adding a feature that directly measures the thing the
     confounding theory says explains it.
   - **Read this cautiously, not as "confounding ruled out."** `champion_ap_shares()` is a coarse, six-bucket
     proxy averaged from class tags -- not a real measurement of "what the enemy team can burst you down
     with." A confound this crude a feature can't absorb is still entirely possible; persistence under this
     specific test narrows the confounding explanation without eliminating it. What it does support: the
     supervisor's alternative reading (a real, matchup/boots-choice-dependent effect) is at least as
     plausible now as the confounding one, so the hold decision (item 7) stands, but "likely confounding"
     should not be read as more than "consistent with, unresolved."
   - **Retrain, at the user's request, done:** a new experimental candidate model
     (`pipeline/retrain_with_ap_feature.py`, version `recommender-experimental-r4-enemy-ap`) refits the
     full shared model set (`recommender.fit_models()`: win, propensity, five auxiliary outcomes, one-way
     temporal cross-fit, decision base/enriched) on the exact same unsealed, non-focus training population
     as the frozen r3 (`data/fulltrain-r3-branches.sqlite`, fold `heldout`, `split_role='train'`,
     2,023,088 rows from 130,886 matches), now including `enemy_ap_share`. All 9 models fitted; the
     one-way temporal cross-fit used all 5 auxiliary targets for `decision_enriched`, same as r3. Saved to
     a private `data/research/recommender/r4-enemy-ap-1790427825/` directory (gitignored) -- **it does not
     touch, retrain or replace the frozen r3 model**, and it is not exported to the site.
     `enemy_ap_share`'s XGBoost gain share in the win model is 0.23% (rank 25 of 32 used features) and
     0.22% in `decision_enriched` (rank 22 of 28) -- the model does use it, modestly, consistent with a
     context feature that matters for some decisions without dominating final-win prediction generally.
     3 tests in `tests/test_retrain_with_ap_feature.py`. Promotion to a product number needs the same
     prospective evaluation gate as r3 does; being fit is not evidence of anything on its own -- no policy
     evaluation has been run against this candidate.
   - 150 Python and 55 Node tests pass throughout.

9. **2026-09-26, later same day: Stage 0 diagnostic run and the decision to build a pregame effect model.** A
   separate merged branch (`claude/vigilant-goldberg-9wnnw9`) brought in `docs/item-policy-structural-fix.md`,
   documenting an independent causal-inference review and a supervisor-agent verdict: **no more prospective
   rounds of the current swapped-arm ("S-learner") recommender approach**, r3 included -- about 350 changed
   decisions per round can only detect a ±16 pp effect, and a realistic 1 pp effect needs roughly 95k changed
   decisions (tens of millions of games at today's departure rate). r2's earlier +12.2 pp [4.1, 20.3] pooled
   result is relabeled "not confirmed": implausibly large, at odds with development's -16 pp, most likely
   winner's-curse selection on the ~350 most extreme predicted gaps each round, not a real per-decision effect.
   Its own smallest next step -- a read-only diagnostic (`pipeline/effects.py`) reusing the r1/r2 round files
   plus the main DB, never touching the sealed cohort -- was run on 1,898,049 branch rows (dev 589,363; r1
   650,104; r2 658,582). Full numbers: `data/research/effects/effects.json`; write-up:
   `docs/item-policy-structural-fix.md` section 12. Headline results: the timing gap between a stage's first
   purchase and the actual distinguishing purchase is a median 144 s (only 35% happen immediately) with ~1,000
   gold of average drift in between, confirming context is measured too late; a skill-confounding negative
   control came back non-zero (+0.09 pp [+0.01, +0.17]), confirming missing player-skill confounding is real;
   the average effect of B vs. A stays near zero either way (-0.16 to -0.09 pp); but pair heterogeneity
   (tau = 1.61 pp after adjusting for team composition and player history, 18% of pairs shrunk beyond 1 pp)
   survives adjustment, with signs that flip by champion (Shaco/Kalista boots negative, Kai'Sa boots positive).
   Per the pre-registered decision rule, tau clears the 1 pp bar: **build the pregame effect model (Stage 1)**,
   not another prospective round. Stage 1's quick plan is in `docs/item-policy-structural-fix.md` section 12.
   150 Python and 55 Node tests unaffected (diagnostic-only change, no production code touched).

## Next decision

**Superseded, 2026-09-26 (see item 9).** Accumulating future-start games to run one more sealed-cohort policy
evaluation of r3 is no longer the plan: the same swapped-arm architecture and the same low departure rate that
made r1/r2 underpowered would make an r3 sealed-cohort test underpowered too, per the supervisor's power
analysis. r3 stays frozen and untouched, and the sealed future-start cohort keeps accumulating for whenever it
is next needed, but the active next step is building the Stage 1 pregame effect model
(`docs/item-policy-structural-fix.md` section 12), then sizing and pre-registering one new frozen test from
Stage 1's own departure rate rather than r3's.

After Stage 1 exists and passes its own frozen test, prioritize matchup-specific recommendations with an
observed-route fallback -- Stage 1's per-pair, matchup-conditioned effects are a direct step toward this, not a
separate future task. Route-level item ordering and cross-patch transfer remain future work.
