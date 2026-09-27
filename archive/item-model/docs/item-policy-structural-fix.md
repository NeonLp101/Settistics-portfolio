# Why the item model rarely changes a build, and the structural fix

Status 2026-09-26. Development note: nothing here changes the site or the frozen r1/r2 results. Numbers for the
funnel come from `pipeline/departures.py` once it has run on the real data; the causes below are read from the
code and must be confirmed by that output.

**An independent review (section 10) changed this plan.** Read section 10 first. The main corrections:
- the decision unit and the missing confounders come before a better estimator;
- the 10-minute WPA change is not a valid surrogate as first proposed;
- r1/r2 were far too small to detect a realistic effect;
- the "50× weights" claim in section 3 was wrong.

## 1. Where we stand

| Round | Test games | Branch decisions | Model changed the build | Gain per changed decision | Verdict |
|---|---:|---:|---:|---:|---|
| r1 | 43,735 | 650,104 | 365 (0.056%) | +8.1 pp [−2.9, +19.0] | Fail (interval includes zero) |
| r2 | 44,295 | 658,582 | 346 (0.053%) | +17.3 pp [+5.1, +29.4] | Pass (selected repeat after r1 failed) |

Both rounds point the same way, but the policy acts in about one of every 1,900 decisions. A recommender that
agrees with the most common build 99.95% of the time adds almost nothing, even if its rare changes are good.

## 2. How the current policy decides (`pipeline/recommender.py`)

- **The choice.** For each champion × role × stage (slot 1–3, boots) × patch, `branches.py` picks the two most
  completed routes in training games: A (most common) and B (second). A player's decision is dated at their first
  purchase that distinguishes A from B. Only these binary A-vs-B decisions exist; there are no other routes.
- **The decision model.** An XGBoost classifier predicts *final win* from ~40 pre-purchase context features
  (game state, stage, role, gold, inventory, coded champion/opponent/region/patch/pair) plus two action
  features: `arm_b` and the route's item id. It is regularized hard: depth 3, min child weight 30, λ = 10,
  200 trees at learning rate 0.05. The predicted gain is `P(win | B, x) − P(win | A, x)`: the same row scored
  twice with the arm swapped.
- **The enriched variant** adds out-of-time predictions of five short-window outcomes (5-minute gold-lead change,
  takedowns, deaths, champion damage, time alive), each also scored with the arm swapped.
- **The gate.** The policy recommends B only if the pair has ≥ 30 training rows in each arm, the propensity
  `P(B | x)` lies in [0.1, 0.9], and the predicted gain exceeds **3 percentage points**. Otherwise it keeps A.
- **The evaluation.** Doubly robust policy value against always-A, match-clustered intervals, propensities
  clipped to [0.02, 0.98].

## 3. Why it changes so few decisions

In order of expected weight. `departures.py` measures each one.

1. **The model barely separates A and B (the main suspect).** Final win depends mostly on game state. In a
   heavily regularized tree ensemble the arm is one weak feature among ~40, so most trees never split on it and
   the swapped-arm difference is close to zero for most rows. The model is built to predict *who wins*, not
   *what the choice changes*. Measured by: the share of split gain from the route features, and the
   distribution of predicted B − A.
2. **The 3-point bar is high for one decision.** Realistic average item effects on final win are around 1–3 pp.
   The *variation* of that effect across game states is smaller still. A per-decision point estimate above
   3 pp is therefore rare, and when it happens it is often an extrapolation. Measured by: departures at 2, 1,
   0.5 and 0 pp.
3. **Overlap excludes rare routes.** Where B is bought in under 10% of similar situations, the propensity falls
   outside [0.1, 0.9] and the row is excluded. Measured by: the `outside_overlap` count and the share of
   decisions whose pair's rarer route has under 10% of buyers.
4. **Support is not the bottleneck.** In r2, 94.7% of decisions had a supported pair.
5. **In the enriched model the arm effect moves into the auxiliary predictions.** In a synthetic test with a
   planted effect, the route features carried 46% of the base model's split gain but 0.6% of the enriched
   model's. The auxiliary predictions (which also change with the arm) carried the effect instead. This is not
   wrong, but it makes the enriched delta depend on how well each auxiliary model separates the arms.

The large gain per changed decision (+8 and +17 pp, wide intervals) fits this picture: the few departures are
extreme, selected cases. *(Corrected after review: their noise does not come from extreme weights. A departure
needs e ∈ [0.1, 0.9], so its weights are at most 10×. The noise is a binary outcome averaged over only ~350
rows; see section 10.)*

## 4. Why lowering the margin is not the fix

Lowering the 3-point bar is a knob, not a method. If predicted differences are mostly near zero, a smaller
margin lets through decisions whose predicted gain is noise. That dilutes the gain and can turn it negative.
The development result already hints at this: a post-hoc margin sweep found no supported improvement. The
diagnostic's margin sweep shows how steeply the gain per decision falls as the bar drops.

## 5. The structural fix: model the effect of the choice, not the outcome of the game

### 5a. Target the difference directly (heterogeneous treatment effect)

Estimate `τ(x) = E[win | B, x] − E[win | A, x]` as the model's own target, instead of reading it off a win
model. Main-effect variance (who is ahead, which champion) then no longer drowns the choice.

- **DR-learner** (Kennedy): cross-fit the outcome models μ_A, μ_B and the propensity e(x) on other time blocks,
  build the doubly robust pseudo-outcome
  `φ = μ_B(x) − μ_A(x) + A·(Y − μ_B(x))/e(x) − (1 − A)·(Y − μ_A(x))/(1 − e(x))`,
  then regress φ on x with a regularized model. It reuses what we already have: the win model, the
  propensity model and the DR scores in the evaluator.
- **R-learner** (Nie & Wager): residualize the outcome and the choice on x, then fit τ by minimizing
  `((Y − m(x)) − (A − e(x))·τ(x))²`. More stable when e(x) is extreme.
- **X-learner** (Künzel et al.): suited to imbalanced arms, which we have (B is the rarer route).
- **Causal forests** (Athey, Tibshirani, Wager; `grf`, EconML `CausalForestDML`): honest splitting and
  per-decision confidence intervals out of the box.

Start with the DR-learner (least new machinery), compare with the R-learner and a causal forest on development
folds, and choose one before the next frozen round.

### 5b. Share strength across champions

Most pairs have hundreds, not tens of thousands, of decisions. Fit one pooled effect model across all
champions and pairs, with features describing the choice rather than only its id: item stats and tags
(damage type, health, resistances, active), champion class and damage profile, the opponent's damage type,
and the stage. Then a rare pair borrows the pattern from similar choices, e.g. "an armor route against an AD
lane when behind". Shrink each pair's effect toward the pooled prediction (hierarchical / empirical Bayes) so
thin pairs stay close to zero unless their own data disagree.

### 5c. WPA as a signal (revised in section 10: context yes, surrogate no)

WPA is the win-chance model in `pipeline/wpa.py`. It enters in two ways:

- **Context.** The win chance at the moment of purchase is a compact summary of the game state. It is already
  implicit in the state features, but as an explicit feature it helps the effect model find "ahead vs behind"
  patterns.
- **Short-term outcome.** The change in win chance over the 10 minutes after the purchase is a less noisy
  signal of what the purchase did than the final result, which is decided by everything that happens later.
  Add it as a sixth auxiliary outcome, and consider it as a surrogate outcome that increases the effective
  sample for τ. Final win stays the judge: the surrogate is only trusted where its effect agrees with the
  final-win effect on held-out data.
- **Data prerequisite.** `decisions.py` writes `win_chance_pre` and `win_chance_change_10` only when built with
  `--win-model` (a frozen `wpa.py` model file). The r1/r2 datasets were built without it, so the next dataset
  build must pass it. The win-chance model must be trained on games that do not include the rows it scores
  (out-of-fold), or the surrogate leaks.

### 5d. Recommend with uncertainty, not with a point estimate

Replace "predicted gain > 3 pp" with "the lower bound of τ(x) exceeds a small practical margin", e.g. the 80%
lower bound above 0.5 pp. The bounds come from the causal forest or from bootstrap/cross-fit variation.
Confident small effects in common situations then count, while large noisy guesses on rare rows do not.

### 5e. Learn the policy, not just the effect

Given DR scores, choose the recommendation rule that maximizes estimated policy value directly (Athey & Wager
policy learning, `policytree`). A shallow policy tree is also readable: "buy Mercury's Treads instead of
Plated Steelcaps when the enemy lane is AP and the team is behind by over 1,000 gold".

### 5f. Check that the ranking means something

The key development diagnostic is effect calibration: sort held-out decisions by predicted τ into deciles
and check that the realized DR gain rises with it. Also use the rank-weighted average treatment effect
(RATE, Yadlowsky et al.) and its TOC curve. A model whose top decile does not beat its bottom decile has no
usable targeting, whatever its AUC.

### 5g. Evaluation stays as it is

Design on development folds only (forward in time, match-grouped). Then freeze and run one new round on games
collected after the freeze. Report departures, overall gain, gain per changed decision, coverage and slices,
exactly as in r1/r2.

## 6. Beyond two routes (later)

- **More than two routes** per decision: multi-arm effects against the common route, with the same overlap
  rules.
- **Sequences**: item 1 changes what item 2 should be. That is a dynamic treatment regime problem
  (Q-learning / fitted Q-evaluation over the build path). Only after the one-step method works.
- **New patches**: carry effects across patches through item-attribute features, and mark changed items
  provisional, as the general plan already requires.

## 7. The Coachless-style WPA view on the site (separate, quick)

The site already computes a per-item WPA ("adjusted association": won minus predicted win chance at purchase,
with a 95% interval). It is hidden in two ways: the site export still stops at the 24 September seal (about
43k games), and a value is hidden unless a split-half reproducibility check passes. The quick change is to show
the value with its interval always and turn the reproducibility check into a label. Moving the site's date limit
forward needs the user's approval, because the rules keep the old filter until a replacement is approved.
Focus-crawl games stay excluded.

## 8. Order of work (superseded by the staged plan in section 10)

1. Run `node scripts/python.mjs pipeline/departures.py --reports-only`, then the full run. This confirms or
   refutes section 3.
2. Coachless-style WPA view on the site (after the date-limit decision).
3. Rebuild the development dataset with `--win-model`, so WPA columns exist.
4. DR-learner effect model with pooled choice features and WPA signals, compared with the R-learner and a
   causal forest; effect calibration and RATE on development folds.
5. Uncertainty-based decision rule; freeze; new round on later-collected games.

## 9. End goal

A build recommender that, for a given champion, role, matchup and game state, ranks the common legal routes by
their estimated effect on winning. It shows how confident it is, explains the choice in game terms, falls
back honestly to the observed build where it cannot tell, works on patch day from shared item and champion
patterns, and is proven on games it has never seen.

## 10. Independent second opinion (2026-09-26) and revised plan

A separate agent, briefed as a causal-inference and ML expert, reviewed this document against the code and the
literature. It had read-only access. Its power and pooling numbers were rechecked and hold.

### Verdict

- **The diagnosis is partly right.** The swapped-arm win model (an "S-learner") does shrink effects toward
  zero. But two structural causes were missed:
  - **The action is coded badly.** `arm_b` means "the second route" in every pair, so one pooled feature
    averages opposite effects. `pair_id`, champion and item ids are integer codes, so depth-3 trees split
    arbitrary groups of them.
  - **The context is measured too late.** It is taken at the distinguishing purchase, so purchase time, gold,
    `stage_step` and inventory are partly *consequences* of the route plan. That pushes propensities toward
    0 or 1 by design. Affordability is also never checked: `budget_exact` is always 0.
- **Power is the untold story.** Each changed decision's DR difference has an SD of about 1.1. With about 350
  changes per round, the smallest effect a round can detect is about ±16 pp. A realistic 1 pp gain needs
  roughly 90k changed decisions for 80% power. r1 and r2 could only pass through bias or luck.
- **The per-change gains swing too much to trust.** Development gave −16 pp [−32, 0], r1 +8 pp, r2 +17 pp.
  Pooling r1 and r2 gives +12.2 pp [4.1, 20.3], which is implausibly large for one component choice.
  Unmeasured skill or intent is the likely explanation. Report r2 as "not confirmed". The overall gain,
  about +0.007 pp, is practically nil.
- **The DR-learner is a reasonable estimator but not the first step.** First fix the decision unit, the
  missing confounders (enemy and ally team composition, player history) and the categorical coding. A better
  estimator on the same features is still biased.
- **WPA:**
  - *As context:* fine, but it adds little. It is a function of `state_pre`, which the model already has. It
    must be cross-fitted by match.
  - *The 10-minute change as a surrogate outcome:* not valid as proposed. The win model has no item or
    inventory inputs, so it favors early-power items over scaling ones. And final win is observed for every
    row, so a surrogate adds no efficiency without assuming surrogacy.
  - *The principled route:* retrain the win model *with inventory* so it becomes a real value function of
    the game state (stage 4 below). Before that, use short-term effects only as a shrinkage prior learned
    across many pairs, never as the target.
- **The uncertainty rule and the ranking checks point the right way, but won't raise departures much.** The
  honest output for most decisions is **"no clear preference"**. The site should say so, not "keep A", which
  implicitly tells every B buyer they chose wrong.

### What the review adds that this document missed

1. **The estimand doesn't match the product.** The site is a pregame guide, but the decision is dated after
   purchases within the stage. Covariates after the moment the route is chosen are post-treatment.
2. **Missing confounders:** enemy and ally composition, and player skill or history. Player history can be
   built from strictly earlier games via `player_ref`.
3. **Departure concentration.** Check whether the ~350 changes come from a few pairs or a few players.
   Intervals are clustered by match, not by player.
4. **Negative controls.** Does the player's past win rate predict the route? Is there a "placebo effect" on
   the gold change *before* the decision?
5. **`departures.py` gaps:**
   - its funnel is sequential (overlap is tested before the margin), so marginal counts are needed too;
   - its route gain share ignores the auxiliary columns;
   - `rare_route_b` is the pair's overall share, not the conditional propensity;
   - it has no per-pair propensity-calibration check.
6. **The enriched model's auxiliary inputs shift between training and scoring.** It trains on out-of-fold
   auxiliary predictions, but at scoring time gets predictions from the full-data models. When it is fitted,
   both decision models also drop the first time block.

### Revised staged plan

**Stage 0: diagnostics (next).** Run `departures.py`. Then extend it with:
- marginal gate counts;
- per-pair propensity calibration;
- propensity refits with and without the timing, gold, `stage_step` and inventory features;
- departure concentration by pair and by player;
- the negative controls above;
- a power table.

**Stage 1: fixed decision unit and a pooled DR effect model.**
- **Decision unit:** the stage's first purchase event of any item, with covariates frozen there. The
  treatment is which route's distinguishing part is bought first in the stage.
- **Features:**
  - enemy and ally team damage profiles, from Data Dragon tags or champion class;
  - player history from strictly earlier games;
  - a cross-fitted `win_chance_pre`;
  - one-hot or native categoricals instead of integer codes;
  - item-stat and cost *differences* between the two routes.
- **Nuisance models:** 5-fold cross-fitting grouped by match, a calibrated propensity, and training trimmed
  to e ∈ [0.05, 0.95] (Crump et al.).
- **Final stage:** EconML `DRLearner`, compared with `NonParamDML` (R-loss), using a strongly regularized
  regressor on features describing the pair. Add a pair-level AIPW mean and standard error, and shrink each
  pair toward the pooled prediction with robust empirical-Bayes intervals.
- **Output:** three states per decision: B, A, or no clear preference. The threshold is chosen by
  cross-fitted policy value on development folds.
- **Development checks:**
  - BLP calibration slope near 1 and GATES (Chernozhukov et al.);
  - RATE/AUTOC;
  - between-pair and within-pair targeting reported separately;
  - value against both always-A and the observed behavior.

**Stage 2: readable policy and robustness.**
- **Readable policy:** EconML `DRPolicyTree`, depth ≤ 3, on the pooled DR scores.
- **Robustness to hidden confounding:** a DoubleML omitted-variable-bias sensitivity analysis and a
  Kallus–Zhou Γ-robust improvement check. Ship only where the gain survives Γ ≈ 1.2–1.5.
- **Evaluation:**
  - a frozen round sized for about 50k changed decisions, run forward in time (a new patch where possible);
  - a pre-registered pooled analysis across rounds;
  - negative controls.
- **WPA:** its short-term effects are used only as a learned prior across pairs (Bibaut et al.).

**Stage 3: more routes and patches.** Multi-arm DR over the top-k legal routes with shared item embeddings,
and transfer across patches from item-attribute differences, with provisional flags.

**Stage 4: sequences.** Retrain the win model with inventory so it becomes a value function V(s); this is where
WPA properly becomes part of the ML. Then fitted-Q or double RL over build paths, and treat boots and item
timing as a when-to-treat problem.

### Pitfalls to keep in view

- Hidden confounding by skill or intent survives every prospective round. Unseen games do not make an
  estimate causal.
- Covariates measured after the route is chosen create positivity violations and bias.
- Winner's curse: judging a few changes selected on extreme predictions (Andrews, Kitagawa & McCloskey).
- A win-chance model that isn't cross-fitted by match leaks into the rows it scores.
- An item-blind surrogate favors early-power items.
- Forking paths across repeated rounds and development sweeps.
- Regularized, uncalibrated propensities make overlap look better than it is.
- Team-level outcomes make real effects well under 1 pp, which needs very large samples.

### Resources

- Kennedy (2023), *Towards optimal doubly robust estimation of heterogeneous causal effects*,
  https://arxiv.org/abs/2004.14497 — the DR-learner.
- Nie & Wager (2021), *Quasi-oracle estimation of heterogeneous treatment effects*,
  https://academic.oup.com/biomet/article-abstract/108/2/299/5911092 — the R-learner; downweights poor overlap.
- Künzel, Sekhon, Bickel & Yu (2019), *Metalearners for estimating heterogeneous treatment effects*,
  https://arxiv.org/abs/1706.03461 — the X-learner, and why S-learners shrink effects.
- Curth & van der Schaar (2021), https://proceedings.mlr.press/v130/curth21a.html — why S-learners
  under-separate arms.
- Wager & Athey (2018), https://arxiv.org/abs/1510.04342; Athey, Tibshirani & Wager (2019),
  https://arxiv.org/abs/1610.01271 — honest causal forests and pointwise intervals.
- Athey & Wager (2021), *Policy learning with observational data*,
  https://onlinelibrary.wiley.com/doi/abs/10.3982/ECTA15732; policytree, https://github.com/grf-labs/policytree
  — DR welfare maximization and readable trees.
- Yadlowsky et al. (2024), RATE, https://arxiv.org/abs/2111.07966 — targeting evaluation and the TOC curve.
- Chernozhukov, Demirer, Duflo & Fernández-Val, https://arxiv.org/abs/1712.04802 — BLP and GATES
  calibration tests.
- grf `test_calibration`, `best_linear_projection`, `rank_average_treatment_effect`,
  https://grf-labs.github.io/grf/reference/best_linear_projection.html — ready-made checks.
- EconML (`DRLearner`, `CausalForestDML`, `DRPolicyTree`), https://www.pywhy.org/EconML/ — Python
  implementations with `groups` and `effect_interval`.
- DoubleML, https://docs.doubleml.org/ — IRM and sensitivity analysis. CausalML,
  https://github.com/uber/causalml — meta-learner baselines.
- Athey, Chetty, Imbens & Kang, *Surrogate index*, https://www.nber.org/papers/w26463 — the surrogacy
  assumption WPA would need.
- Kallus & Mao (2025), https://academic.oup.com/jrsssb/article/87/2/480/7829031 — surrogates add efficiency
  only when outcomes are missing.
- Bibaut et al. (2023), https://arxiv.org/abs/2311.04657 — learning the short-to-long mapping from many weak
  experiments.
- Armstrong, Kolesár & Plagborg-Møller (2022), *Robust empirical Bayes confidence intervals*,
  https://onlinelibrary.wiley.com/doi/full/10.3982/ECTA18597 — shrinking many thin pairs.
- Crump, Hotz, Imbens & Mitnik (2009), https://academic.oup.com/biomet/article/96/1/187/235329 — principled
  overlap trimming.
- Andrews, Kitagawa & McCloskey (2024), *Inference on winners*,
  https://academic.oup.com/qje/article/139/1/305/7276491 — selection bias in the chosen departures.
- Kallus & Zhou (2018), *Confounding-robust policy improvement*,
  https://proceedings.neurips.cc/paper/2018/hash/3a09a524440d44d7f19870070a5ad42f-Abstract.html
- Chernozhukov et al., *Long story short: omitted variable bias in causal ML*, https://arxiv.org/abs/2112.13398
- Nie, Brunskill & Wager (2021), *Learning when-to-treat policies*, https://github.com/xnie/adr — boots and
  item timing.
- Kallus & Uehara (2020), *Double reinforcement learning*,
  https://jmlr.org/papers/volume21/19-827/19-827.pdf — off-policy evaluation for sequential build paths.

Links were gathered by the reviewing agent and have not all been opened; check a citation before relying on it.

## 11. Supervisor-agent decision (2026-09-26)

A second independent agent was briefed as the project's research supervisor and asked whether further testing
of the current model makes sense. It had read-only access. This is an agent's memo, not the human supervisor's
decision; the owner takes it to that conversation. Its new numbers were rechecked:
- heterogeneity across development, r1 and r2: Q = 10.7;
- random-effects pooled gain per changed decision: +3.9 pp [−13, +21];
- `models["win"]` and `models["decision_base"]` are fitted on the same features and rows
  (`recommender.py:284`, `:296`).

### Decision

- **No more prospective rounds of the current model (no r3).** A round cannot answer the product question,
  whatever it shows. About 350 changes per round detect only ±16 pp; a 1 pp effect needs about 95k changes,
  which is about 11M games at today's change rate. The SD per change (~1.1) is near the floor for a binary
  doubly robust score, so a better estimator will not buy power. Only more changed decisions will.
- **r2 is "not confirmed".** Suggested wording: *"The unchanged policy failed its pre-registered test (r1) and
  passed a repeat run after that failure (r2). Pooled gain per changed decision: +12.2 pp [4.1, 20.3],
  implausibly large and at odds with development (−16 pp). Gain across all decisions: about +0.007 pp. Not
  confirmed, and not a basis for recommendations."*
- **A new flaw in the evaluator.** Its outcome model is fitted on the same features and rows as the decision
  model that chose the changes. So part of each changed decision's score is that model's own predicted B − A
  gap, not a test outcome. Future evaluations fit the outcome model separately from the decision model, on
  other folds, and report the two parts separately.
- **Rebuild, but staged and smaller than section 10.** Order:
  1. a cross-fitted average effect per pair (AIPW), shrunk toward the pooled estimate (empirical Bayes);
  2. one DR-learner on pregame features, with the R-learner as a single cross-check;
  3. one sensitivity analysis.

  The policy tree is deferred: the site shows labels, not a policy.
- **Separate the two feature sets.** The supporting models (outcome, propensity) use the full game state. The
  model of *where the effect differs* uses only pregame-knowable features (champion, role, matchup, team
  compositions, at most a coarse ahead/even/behind split), because the site is a pregame guide.
- **The earlier decision unit needs care.** Fixing covariates at the start of the stage while the treatment is
  a later purchase lets deaths and gold between the two drive both the choice and the outcome. Measure that
  gap first. If most distinguishing purchases fall in the first shop visit, use the state at the start of that
  visit. Use the pre-decision gold placebo to choose between the options.
- **Clustering:** by player as well as match. Estimate the design effect before sizing any test.
- **Honest end state:** if true effects barely differ between pairs, "no clear difference" almost everywhere is
  a valid result, not a failure.

### Site labels (four states, not three)

| State | When | Label |
|---|---|---|
| Measuring | too few comparable games or no overlap | "Not enough comparable games" |
| No clear difference | the model cannot separate the routes | common build shown as "most common build", the other as an equal alternative |
| Model also prefers this | confident the common build is better (same bar as below) | "Model also prefers this" |
| Model suggests X | confident X is better | "Model suggests X", labeled an experimental lean until a frozen test backs it |

Until the rebuild passes a frozen test, the site shows no "recommended" wording for any model output, no win-rate
gain, and no claim based on r1 or r2.

### The next frozen test

- **Collection is fast** (r1's games took ~10 h to collect, r2's ~13 h). What limits the test is how often the
  model changes a decision.
- **Games needed for a 1 pp effect** at about 15 decisions per game:

  | Share of decisions changed | Games needed |
  |---:|---:|
  | 1% | about 640k |
  | 5% | about 128k |
  | 10% | about 64k |

- **Primary statistic:** calibration across all label groups (B does better where the model says B, A where it
  says A), not only the changed decisions.
- **Pre-register:**
  - the decision unit and the effect features;
  - the label rule and its thresholds;
  - one primary statistic, the sample size or a group-sequential stopping rule, and the smallest effect the
    test is meant to detect;
  - the clustering;
  - the slices and their harm limits;
  - the negative controls;
  - what the site does on a pass and on a fail.
- **Test forward in time,** on a new patch where possible.

### Smallest next step

One development diagnostic on the ~138k games already collected (development, r1 and r2). All may now be used
for development. It produces:

1. **Pair effects:** a cross-fitted average effect for every pair, with and without team composition and
   player history, and an empirical-Bayes estimate of how much the true effects vary between pairs.
2. **The r1/r2 changed decisions rescored with player history added.** Does the +12 pp collapse?
3. **The placebo:** the estimated "effect" on gold change before the decision should be zero.
4. **The split** of the per-change gain into the model's predicted gap and the part from test outcomes.
5. **The timing:** the gap between the start of the stage and the distinguishing purchase.
6. **Player-history coverage:** how many players have enough earlier games.

**Decision rule:**
- If true pair effects vary by clearly more than about 1 pp and survive the skill adjustment, build the pregame
  effect model and size the frozen test from these numbers.
- If not, ship the four labels with "no clear difference" as the honest default, and put the effort into
  coverage instead.

## 12. Stage 0 result (2026-09-26): diagnostic run, decision made

`pipeline/effects.py` ran on 1,898,049 branch rows (dev 589,363; r1 650,104; r2 658,582), read-only, never
touching the sealed cohort. Full numbers: `data/research/effects/effects.json`.

- **Timing confirms the post-treatment concern.** The distinguishing purchase lands a median 144 s after the
  stage's first purchase (35% at the first purchase, 45% within 120 s). Team gold lead moves by ~1,000 gold on
  average in between. Context measured at the distinguishing purchase is materially post-treatment for most
  decisions -- Stage 1 must freeze covariates at the stage's first purchase instead, as section 11 specifies.
- **Player-history coverage is thin.** 73% of rows have any earlier game by that player, but only 29% have ≥5
  and 3% have ≥20. Skill adjustment reaches most rows shallowly, a fraction of rows well.
- **The skill-confounding negative control is not clean.** With base features only, the placebo "effect" on
  the player's own earlier win rate is +0.09 pp [+0.01, +0.17] -- small, but excludes zero. Players choosing
  route B differ slightly in historical skill from those choosing A, confirming the missing-confounder concern
  is real, not hypothetical.
- **Average effect stays near zero either way:** base -0.16 pp [-0.35, +0.02], adjusted -0.09 pp [-0.29, +0.11].
  Consistent with "no clear difference" as the honest default for most decisions.
- **Pair heterogeneity survives adjustment, but "signs flip by champion" below was wrong -- corrected
  2026-09-26 by the standing supervisor review.** tau = 1.61 pp after adding team composition and player
  history (was 1.75 pp raw), with 18% of pairs still shrunk beyond 1 pp. But `effects.py`'s pair table is not
  oriented by item identity -- "A" and "B" mean "most common" and "second" *within each champion*, which
  differs by champion for the same item pair. Re-reading the top boots pairs oriented by item id (3008 =
  Gluttonous Greaves, 3006 = Berserker's Greaves): Kalista A=3008/B=3006 -8.0pp, Zeri A=3008/B=3006 -5.0pp,
  Yunara A=3008/B=3006 -3.5pp, Kai'Sa A=3006/B=3008 +4.2pp -- **all four say the same thing: Gluttonous beats
  Berserker's by 3.5-8pp.** This is one consistent item-level effect showing up as a sign flip purely because
  of which item happened to be "A" for that champion, not genuine matchup-dependent heterogeneity. Also worth
  flagging: an effect that large and that consistent, on a still-new meta item, is itself a prime suspect for
  skill/meta-awareness confounding -- which fits the non-zero skill-confounding negative control above. Part
  of the tau=1.61pp is this one item-level effect, not variation by context.
- **r1/r2 departures rescored, does not collapse.** r1: cross-fitted base +8.56 pp [-3.51, +20.64], adjusted
  +13.29 pp [-0.29, +26.87] (crosses zero either way). r2: cross-fitted base +20.68 pp [+5.65, +35.71], adjusted
  +16.74 pp [+1.25, +32.23] -- **still excludes zero after adjustment.** Composition and player history do not
  fully explain r2's departures; either an unmeasured confounder remains, or -- more likely given both rounds'
  departures were selected on extreme predicted gaps -- this is winner's-curse regression-to-the-mean on a
  small, selected set, not a real +17 pp per-decision effect. Does not change the "not confirmed" verdict on r2.
- **Decision rule fires: build the pregame effect model.** tau (1.61 pp) is clearly above the 1 pp bar.

### Stage 1 quick plan (started 2026-09-26)

1. **Fix the decision unit.** Freeze covariates at the stage's first shop visit, not the later distinguishing
   purchase, using the state already captured for the timing check. Treatment stays "which route's
   distinguishing part is bought first in the stage."
2. **Split the feature sets.** Nuisance models (outcome, propensity) keep the full game state at the freeze
   point. The effect model (where tau(x) differs) uses only pregame-knowable features: champion, role,
   opponent, team damage-profile composition (already built in `effects.py`), a coarse gold bucket
   (behind/even/ahead), patch -- no live inventory or purchase timing.
2b. **Native categoricals**, not integer codes, for champion/opponent/pair -- avoids depth-3 trees splitting
   arbitrary groups of ids (the coding flaw section 10 flagged).
3. **Reuse the cross-fitting already built.** 5-fold, match-grouped, propensity trimmed to [0.05, 0.95]
   (`effects.py`'s `crossfit_propensity`/`crossfit_outcome`), on rows keyed to the new frozen decision unit.
4. **Effect estimation, staged small (per the supervisor, smaller than section 10):**
   a. cross-fitted AIPW effect per pair, shrunk toward the pooled estimate (empirical Bayes) -- already built,
      adapt to the new decision unit;
   b. one DR-learner regressing the AIPW pseudo-outcome on the pregame-only effect features, so thin pairs
      borrow strength from similar matchups;
   c. one R-learner as a single cross-check, not a full bake-off.
5. **One sensitivity analysis** -- how much an unmeasured confounder would need to shift results to null the
   top pairs' effects (Kallus-Zhou or an omitted-variable-bias bound), not a full robustness suite.
6. **Re-run the negative controls on the new decision unit.** The skill-placebo leak (+0.09 pp) should shrink
   once covariates are frozen earlier and player history is included as a feature rather than left out.
7. **Development checks:** calibration of predicted tau by decile against realized DR gain; report between-pair
   and within-pair targeting separately.
8. **Labels, not a policy yet:** ship the four-state labels from section 11 (Measuring / No clear difference /
   Model also prefers this / Model suggests X). No "recommended" wording, no win-rate claim, until a frozen
   test passes.
9. **Size and pre-register the next frozen test only after Stage 1 exists** -- from section 11's table (about
   64k-128k games at a 5-10% departure rate), forward in time, on a new patch where possible, with the decision
   unit, effect features, label rule, primary statistic, clustering (by match and player) and negative controls
   fixed before any test outcome is read.

### Standing-supervisor review of the Stage 1 plan (2026-09-26), before any Stage 1 code was written

Verdict: **ADJUST. Do three pre-checks before writing the DR-learner.** Confirmed real, right direction (pregame
effect model, labels not a policy, no more S-learner rounds). But the plan repeated two already-diagnosed
mistakes and its headline number is less solid than first written:

- **Arm orientation bug (the big one, corrected above):** neither `effects.py` nor the proposed pooled model
  oriented phi by item identity. Fix: orient every pseudo-outcome as P(hi item) - P(lo item)
  (`dr_shrink.group_key`/`oriented_estimates` already do this); give the DR-learner (stage, item_lo, item_hi)
  as features, since champion/opponent/composition don't transfer across item pairs but the item pair itself
  does.
- **Decision-unit freeze point is still wrong.** Freezing at the *stage's* first purchase (minutes before boots,
  for example) reintroduces time-varying confounding and immortal time -- deaths/gold between the freeze and
  the real choice drive both. Measured gaps: boots median 206s (29% within 30s), slot1 148s (36%), slot2/slot3
  ~0s (56-68%). Fix: freeze at the start of the *shop visit* that contains the distinguishing purchase (a visit
  = purchases chained ≤30s apart), not the stage.
- **Tau isn't calibrated yet.** Adjusted Q/df ~1.47; `dr_pairs`' own permutation null previously found per-pair
  SEs understated 1.06-1.20x. At 1.06x tau drops to ~1.4pp; at 1.20x it drops to ~0.4pp, below the 1pp bar.
  `effects.py` never ran `null_calibration` -- must run before trusting the go decision.
- **Boots vs non-boots:** prior art (`dr_shrink` checkpoint) already found non-boots tau fits to exactly 0 and
  all signal rests on one boots group. Report every check split boots/non-boots; pre-declare non-boots gets no
  "suggests" label if it's null again.
- **The win-rate negative control is circular** once `hist_win_rate` is a covariate (zero by construction) --
  use the player's win rate in *later* games instead. Drop `hist_pair_b_share`/`hist_pair_games` from the
  propensity/outcome models -- they predict the treatment, not the outcome (instrument-like; raised trimming
  17.5%->25.1%).
- **CRC32 recurrence risk:** Stage 1 needs a second independent split for calibration halves -- use
  `dr_pairs.fold_of` (sha256) everywhere, never `effects.fold_of` (zlib.crc32, the already-documented affine
  bug).
- **Calibration must use `dr_pairs.independent_halves`**, not naive out-of-fold tau, or nuisance-fold leakage
  (release-status item 7's bug) recurs. No time-forward split exists in dev+r1+r2 (r2 starts before dev) --
  don't describe any split of that pool as forward in time.
- **Categoricals on the effect model** (not the nuisance models) need depth <=3 / high min_child_weight, or
  ~170-level champion/opponent ids will fit noise on a phi with SD~1. Freeze the vocabulary with the model
  (`recommender.Encoder`'s pattern), not rebuilt per dataset (`effects.base_features`'s current bug).
- **Rare-arm propensity:** reuse `dr_pairs.pair_offsets`'s base_margin fix and its no-pair-off-by->1.5x gate --
  `effects.py`'s average calibration gap (0.005) hides per-pair errors as large as the documented 36x one.
- **Scope for "finish and ship":** confirmed labels-only-defer-frozen-test is the right boundary, with three
  changes: (1) size and pre-register the eventual test **now**, against the sealed cohort (start >=
  1790400420000) -- all of dev/r1/r2 end before it (r2 max 1790395987479), so it is untouched and usable;
  (2) gate the DR-learner (step 4b) on whether a held-out within-pair BLP shows real signal -- otherwise ship
  step 4a alone (oriented, EB-shrunk per-pair labels); (3) drop `patch` (constant at 16.19, useless) and the
  gold bucket (defer unless BLP shows it matters) from the effect features; remove r3's per-arm "model-expected
  win chance" from the site when labels ship (contradicts "no win-rate claim").
- **Codebase risk:** build on `data/fulltrain-r3-branches.sqlite`/`fulltrain-r3-decisions.sqlite` instead of
  the r1/r2 files -- one consistent pair definition, already has `enemy_ap_share`, and its cutoff matches the
  seal. Add a hard `started_at < CUTOFF_MS` + not-focus guard to every Stage 1 loader (`effects.load_rows` has
  none today).

**Revised order of work, before writing the DR-learner (about half a day, all on existing data):**
- **A.** Oriented `dr_shrink` fit on `effects.py`'s pair table, permutation-calibrated SEs, split boots vs
  non-boots, leave-the-3006/3008-group-out. Does between-pair variation still clear 1pp?
- **B.** Choose the decision unit: visit-start freeze, the stage-start-to-visit-start placebo, agreement check
  on the first-visit subset (where the two freeze points coincide).
- **C.** Future-win-rate negative control, with the instrument-like history features removed.

Then Stage 1 as adjusted above. The supervisor agent (`a25f1594f9e48752d`) is being kept open across every
major step (data-unit fix, feature split, effect estimation, calibration, site integration) rather than
re-spawned, to keep review context and cost down.

### Pre-check A result and a second orientation correction (2026-09-26)

Re-read `data/research/dr-shrink/report.json` (already built, already calibrated: item-identity orientation,
permutation-inflated SEs, boots/non-boots split, leave-largest-group-out -- no new code needed). Calibrated
numbers: tau_g=0.97pp, tau_c=0.84pp overall (both variances reject zero); non-boots alone is exactly null
(tau_c^2 fits to 0, neither LR test rejects zero); the checkpoint (fit half 0, predict half 1) barely proceeds
overall (gain-vs-zero 95% CI touches 0), fails boots-only on its own, and flips to fail when the largest single
group (boots, Plated Steelcaps 3047, Mercury's Treads 3111, 86 pairs) is dropped.

**The first pass at this result (above, "signs that flip by champion... Gluttonous beats Berserker's by
3.5-8pp consistently") was itself still wrong, caught by the standing supervisor on review.** `dr_shrink`'s
`shrunk_mean` is already flipped back to each pair's *own* B-vs-A orientation, which is champion-specific
(within a pair, "A"/"B" mean most-/second-common for that champion) -- reading it directly, without
re-orienting by item id again, reproduces the exact mistake being corrected. Properly oriented as
P(Gluttonous, 3008) - P(Berserker's, 3006), **all 22 pairs in that group have the same sign, +1.5 to +3.2pp,
strongest for Kalista/Yunara/Zeri where Gluttonous is already the most common build.** There is no
within-pair (champion/matchup-dependent) heterogeneity in this group at all -- it is one item-group effect.
Lesson: never read a bare B-A number from this pipeline again; always re-derive the item-oriented value, and
print item names alongside any number shown to a human.

**Decision: do not build the Stage 1 DR-learner. Cut it from this build.** Section 11's tau>1pp rule does not
clearly fire on the calibrated numbers (tau_g=0.97pp, not clearly above the bar), non-boots is exactly null,
and there is no evidence the effect varies *within* an item pair by context -- so there is nothing for a
pregame heterogeneous-effect model to target. What exists instead is two boots item-group effects. The
R-learner, EconML, native categoricals for an effect model, and the gold bucket are all deferred, not built.
The visit-start decision-unit fix stays documented as a requirement for any *future* item-slot work, but does
not apply to boots (see pre-check B below).

### Revised, smaller ship: per-item-group labels, not a per-pair model

Four states, using the existing `dr_shrink` output directly (no new estimator):
- **Measuring:** pair ineligible (ESS<30 either arm, overlap filter, or propensity miscalibration).
- **Model suggests X / Model also prefers this:** only if the pair is boots, its item group is one of the two
  qualifying groups below, its 95% posterior interval excludes 0, AND pre-check C (below) is clean for that
  group.
- **No clear difference:** everything else, including every non-boots pair.

Claims are made at the **item-group level** ("Gluttonous Greaves over Berserker's Greaves"), never
champion-specific -- champion deviations are weak (tau_c^2 near 0 on the half-0 fit) and do not support singling
out any champion. Label copy stays "experimental lean", no win-rate number. **r3's per-arm "model-expected win
chance" must come off the site when these labels ship** -- it is a win-rate figure from the arm-blind S-learner
and directly contradicts "no win-rate claim".

### Pre-check B: shrinks to a note, not a fix

For boots, the upgrade purchase *is* the treatment and nothing route-specific happens earlier in the stage --
covariates taken at the upgrade purchase (today's decision unit) are already right. Freezing at the *stage*
start (the basic-boots purchase, ~206s earlier) would be wrong: exactly the 206s of confounding pre-check A's
timing diagnostic warned about, reintroduced on purpose. No B fix needed for boots; revisit B only if/when the
item slots (non-boots) are picked back up.

### Pre-check C, round 1: two design flaws caught on review, corrected before trusting the result

The first C1/C2 implementation (`pipeline/boots_confound_check.py`) had two flaws the standing supervisor
caught on review, one of them its own:
- **C1 (future win rate) was contaminated by habit.** "Later games can't be caused by this choice" is only
  true of *this one purchase* -- a player who buys Gluttonous tends to keep buying Gluttonous in later games,
  so if the item has a real ~2-3pp effect, that effect shows up in the "later games" control too, through the
  repeat purchases, not through skill. The observed +0.33/+0.46pp for greaves is roughly the size a real
  2.5pp effect would produce this way -- it does not establish confounding on its own.
- **C2 (within-player) was misread -- backwards.** A within-player comparison removes *stable* skill by
  construction (same player, both sides). A non-zero C2 (+3.34pp [+2.04,+4.65] for greaves) is evidence
  *against* stable-skill confounding, not for it, as the first pass claimed. "Players who buy Gluttonous win
  more in general" is what C1 measures; C2's remaining threat is in-game state ("I buy the upgrade when I'm
  already ahead"), not who the player is.
- **Even taken at face value, C1 only bounds stable-skill bias at ~0.3-0.5pp** -- nowhere near enough to
  explain a 2-3pp raw effect by itself.
- **Correction: greaves is "not confirmed", not "sunk by confounding."** The claim in the first pass overstated
  what a flawed control showed.

**Fixed versions, before trusting any verdict:**
- **C1'**: only count a later game in the control if it contains no 3006/3008 purchase by that player at all
  (purges the habitual-repurchase channel), and report hi-vs-lo as one player-clustered difference, not two
  separately-eyeballed CIs.
- **C2'**: within-player mean of (win - out-of-fold arm-blind outcome-model prediction) by arm, not raw win --
  isolates in-game state confounding, which C2 alone cannot see.

### Pre-check C, on the two boots groups: full plan

Both **(3006 Berserker's Greaves -> 3008 Gluttonous Greaves)** and **(3047 Plated Steelcaps <-> 3111 Mercury's
Treads)**. A consistent ~2-3pp final-win effect for a boots swap, strongest where the newer item is already
the norm, is exactly the pattern skill/meta-awareness confounding produces -- and the skill placebo already
leaked once (+0.09pp, item 12's pre-check-A predecessor). Two controls, on `fulltrain-r3-decisions.sqlite`:
- **C1, future win rate:** does the player's win rate in *other, later* games (ended after this one) differ by
  choice, given X? Built from `player_ref` the way `effects.history()` does, but on later games, not earlier
  ones (a control on earlier win rate is circular once skill is a covariate -- see pre-check-A's own note above).
- **C2, within-player comparison:** among players seen making both choices within a group, compare their own
  outcomes across their own games -- removes stable skill directly. Coverage is thin but these two groups are
  large.
- **Outcome:** either control clearly non-zero, or the within-player estimate collapsing toward 0, means every
  pair in that group ships "No clear difference" instead. Both clean means the group may carry the lean label.

### Pre-register the frozen test in this session

Feasible: the hypothesis is narrow (two oriented group effects, direction and metric fixed, tested once on the
sealed cohort, start >= 1790400420000, never read). Rough power: per-row DR SD ~1.1, so a 2pp effect at 80%
power needs ~24k group rows (before match-clustering/trimming losses) -- for the Gluttonous group, on the
order of 20-30k sealed games, which is days of collection, not a scale blocker. Compute the exact number from
each group's rows-per-game before freezing. Freeze before shipping and before any sealed game is read: the
`dr_shrink` report hash, the label rule, one primary statistic per group with a Holm correction across the two,
match+player clustering, the C1/C2 controls, and what the site does on a pass vs. a fail.

**Coherent unit of work for this session:** C1/C2 on the two boots groups -> four labels from the existing
`dr_shrink` output (item-group wording, r3 win-chance display removed) -> pre-registered sealed-cohort test for
just those two groups. No new estimator, no Stage 1 DR-learner.

### Pre-check C, round 3: the deciding diagnostic (2026-09-26)

C1/C2 (round 1) had two flaws; C1'/C2' (round 2, `pipeline/boots_confound_check.py`) fixed them, but C2' turned
out not to be a clean negative control either -- it is a second *estimator* of the effect (within-player,
removing stable per-player confounding), not a placebo. The deciding check the standing supervisor specified:
compare the between-player doubly robust estimate on the full population against the same estimate restricted
to just the "switcher" players C2' uses, and against C2' itself.

- **Greaves: confirmed player-level confounding, not a real item effect.** full-population DR +2.26pp
  [+1.57,+2.96] -> switchers-only DR +1.20pp [+0.08,+2.33] (about half, barely excludes 0) -> within-player
  (C2') +0.02pp [-1.08,+1.11] (nothing left). This step-down is the textbook signature of player-level
  confounding (plausibly champion mastery or meta-awareness on specific ADCs, not general skill -- C1' had
  already ruled general skill out). **Ships "No clear difference."** A future sealed-cohort pass on this pair
  (kept as H2 in the fixed-sequence pre-registration below, since it costs nothing) would not by itself make it
  eligible for a label -- a prospective test replicates confounding, it does not remove it.
- **Defensive boots: survives the deciding check, no attenuation.** full-population DR -1.14pp [-1.51,-0.77],
  switchers-only DR -1.22pp [-1.73,-0.72], within-player (C2') -1.28pp [-1.87,-0.69] -- all three agree closely.
  Player-level confounding does not explain this result. Independent-halves confirms both halves agree in sign
  (half 0 -1.51pp [-2.03,-0.99], half 1 -0.79pp [-1.31,-0.26]).

### Defensive boots: the pooled number was hiding a real, composition-dependent effect, not "confounding"

`pipeline/defensive_boots_deepcheck.py` added real per-champion damage-mix features (mean magic-damage-dealt
share and mitigation share, from actual participant stats over a broad 20,000-match sample, not the coarse
Data-Dragon class-tag `enemy_ap_share` already in context) for the enemy team and the player's own team.
**The pooled effect collapses to -0.09pp [-0.48,+0.30] once this is added** -- at first read, this looks like
the confounding story finally catching this group too. But the descriptive enemy-magic-damage-share tercile
split (independent-halves, each tercile ~81,200 rows) shows exactly the *opposite* of a confounding red flag:

| Enemy AP tercile | Oriented effect (Mercury's - Steelcaps) |
|---|---:|
| Low (enemy mostly physical) | -1.99pp [-2.89,-1.10] (Steelcaps favored) |
| Mid | -0.22pp [-0.88,+0.45] (no clear difference) |
| High (enemy mostly magic) | +1.42pp [+0.74,+2.10] (Mercury's favored) |

This is the *sane* pattern (magic resist helps more against magic damage, armor/tenacity helps more against
physical/mixed), not the backwards pattern that would flag leftover confounding. **Reading: there is no
universal winner between these two boots -- the correct choice is genuinely composition-dependent, and the
population-average -1.1pp was an artifact of this population facing low/mid-AP compositions more often than
high-AP ones, not evidence Plated Steelcaps is fundamentally better.** Once composition is properly accounted
for, the "pick against composition" story explains the data at least as well as "one item is better," and the
tercile split actually supports it being a real, sensible, situational effect rather than debunking it as pure
noise/confounding.

**Sizing (docs/item-policy-structural-fix.md's own numbers, `defensive_boots_deepcheck.py` step 4):** ~46,885
sealed games for the raw 1.1pp pooled effect, ~100,855 for the more conservative 0.75pp -- both feasible (days
to ~2 weeks of collection at current rates), before trimming losses.

**Open decision, needs the standing supervisor's read before shipping anything:** does this composition-
dependent structure change what gets shipped for this one pair -- a genuinely useful, situational label
("Mercury's Treads against heavy-magic teams, Plated Steelcaps against heavy-physical teams, no clear
difference otherwise") backed by real data, vs. the earlier plan of one flat group-level lean label (which the
tercile split shows would be actively misleading roughly a third of the time) vs. simply "No clear difference"
to keep scope small for this session. This reopens a narrow, single-pair version of the composition-
conditioned idea that Stage 1 was cut for being too broad -- worth checking whether that changes the "no
Stage 1" call, or whether this one pair is narrow enough to ship as a one-off without reopening that.
