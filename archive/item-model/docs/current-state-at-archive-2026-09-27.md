# Settistics: current state and plan

**Latest release status (2026-09-26):** [model-release-status-2026-09-26.md](model-release-status-2026-09-26.md). The r3 full-data model is live as a training-only research preview. The counts and “no seal yet” statements below describe the earlier 2026-09-25 development state. **That linked doc also covers 2026-09-26 same-day research** (not yet site-facing): a labeling fix for pairs the win model cannot distinguish, doubly robust per-pair estimates, an empirical-Bayes shrinkage model held as a private research artifact, and -- later the same day -- a supervisor-agent decision (from a merged branch) that the current recommender architecture is too underpowered for further prospective rounds, r3's pending sealed-cohort evaluation included. The active next step is now building a pregame effect model (Stage 1, `docs/item-policy-structural-fix.md` section 12), not accumulating games for another round; see step-board item 4f below.

**Historical development snapshot. Last updated 2026-09-25.** Read the linked release status for current model, seal and 2026-09-26 research facts. **The numbers below (460,896 decisions / 30,904 games, 640 pairs, 120/53 tests) are stale**: the deployed r3 model fit on 2,023,088 decisions from 130,886 games, and the public preview now covers 995 pairs (864 supported) with 121 Python / 55 Node tests passing as of 2026-09-26. Left as originally written below since it is a snapshot of the 2026-09-25 development state, not corrected in place.

**Authoritative model plan:** [general-item-model-plan.md](general-item-model-plan.md). Target: shared,
patch-aware item recommendations that improve on most-common choices, using predicted combat,
survival and short-term progress alongside direct final-win training and evaluation. This replaces
the Kai'Sa-first roadmap. The first implementation is a research preview; it has not shown a supported
improvement over the observed most-common route.

## ML progress at a glance (2026-09-25)

**Built and deployed:** the first shared item model covers champion/role comparisons for first, second
and third items and upgraded boots. It uses pre-purchase game state, including opponent identity, and
learns final-win and five-minute gold, takedown, death, champion-damage and survival predictions. The
saved experimental model fitted on 460,896 decisions from 30,904 games; its later development check
used 115,564 decisions from 7,796 games. The public site shows a research preview for 640 pairs,
including 527 with enough training support for predictions. It averages across matchups and shows no
uncertainty interval for those preview predictions. The full build, 120 Python tests, 53 Node tests,
public-data validation and local browser check passed before deployment.

**What the result means:** final-win prediction reaches AUC 0.778 on the later development block, but
prediction accuracy is not recommendation quality. The enriched policy changed 135 of 115,564
decisions (0.12%) under its three-percentage-point margin. Its estimated gain against the common-choice
baseline was −0.019 percentage points with a 95% interval from −0.038 to 0.000 points. That block has
been inspected during development, so the result is exploratory; it does not establish an advantage.
The website therefore keeps observed routes as its suggestions and labels model leans as predictions.

**Next:** make matchup-specific item guidance the development priority. The model already receives the
opponent, but the published preview averages over opponents and useful matchup interactions have not
been shown. Compare a matchup-conditioned policy with both champion-wide and matchup-specific popular
choices on chronological development games, reporting support, uncertainty, coverage and value where
it differs. Use a shared model so sparse matchups borrow strength, and keep the popular route as the
fallback. Only after choosing the model and evaluation rules should a new future-game cohort be sealed
for one prospective policy check. That new seal is **not yet in place**. Clean-fight profiles, route-level
validation and cross-patch transfer also remain unfinished. The four general crawlers were actively
adding games in EUW, KR, NA and VN when this snapshot was written.

## 1. Goal

A pregame build guide that recommends items that are **actually better**, for every champion and item,
backed by evidence that reproduces. The current model uses short-window player outcomes as well as
final wins; clean-fight windows are a planned secondary view, not yet built.

**Deliverable (user decision, 2026-09-25): the actual general ML recommender on the website.** Remove
the separate observed-route Riot demo milestone. Show model-generated item preferences, situational
alternatives and combat/survival explanations, with uncertainty. A popularity-only page is not this
deliverable. The older Riot-demo handoff is historical, not the active roadmap.

## 2. Crawlers (what is running right now)

| Region | Routing cluster | Mode | Log |
|---|---|---|---|
| EUW (`euw1`) | europe | All champions | `data/collect.log` |
| KR (`kr`) | asia | All champions | `data/collect-kr.log` |
| NA (`na1`) | americas | All champions | `data/collect-na1.log` |
| VN (`vn2`) | sea | All champions | `data/collect-vn2.log` |

- The four Kai'Sa-focused crawlers were stopped on 2026-09-25 at about 04:34 UTC at the user's
  request. `pipeline/console.py` `CRAWL_ARGS` was restored to general `--champion all --snowball`
  collection on patch 16.19, and all four regions were restarted. Process arguments were verified:
  four regional crawler process trees, none with `--focus`.
- **One crawler per routing cluster.** Riot limits each cluster separately; a second crawler in the same
  cluster only splits the budget.
- Historical focus games remain tagged `matches.source = 'focus kaisa:BOTTOM'`; about 3,233 were complete
  at the switch. Keep that tag available for export and sampling checks. The general crawlers first clear
  each region's queued matches, so startup logs may lag while they process the backlog.
- **The dev key expires every 24 h** (current one around 19:30 CEST on 2026-09-25). All crawlers stop
  with HTTP 401; the user pastes a new key into `.env` and restarts them from the console.

## 3. Data

- The first full general-model extraction used **50,077 finished matches** on patch 16.19 and produced
  6,718,272 dated purchases in private `data/decisions.sqlite`. The branch table contains 1,941,862
  rows across development folds, covering 172 champions and 639 candidate item pairs.
- The later general-source evaluation block contains 7,796 matches and 115,564 branch rows. Another
  30,904 matches and 460,896 branch rows fit the model. Focus-source games remain training-only.
- **Route timing cohorts (2026-09-25):** each exported build (3-item core) now carries `routes`, keyed
  `bootsId:bootsPosition:firstComponentId` (`-` = none, position capped at 3). All times of one route are summed over
  the same games; route games add up to the build's games (schema invariant). The site shows timings only from one
  route cohort (15+ games), falls back to the whole boots route without the component, and otherwise shows the item
  order without times. Older exports without `routes` show no route times. Needs a fresh export to populate.
- The local public build contains 34,835 collected matches across 173 champion shards. It uses the
  established public-data filter; the general model's research preview is a separate, bounded aggregate.

## 4. Win-chance model (`pipeline/wpa.py`)

- XGBoost on the GPU, cross-fitted in three folds, then Platt-recalibrated. Inputs (`STATE`): minute,
  team gold, team XP, lane gold, lane level, kills, towers, inhibitors, dragons, barons, heralds,
  voidgrubs, plus side. **Items are not inputs**, on purpose. Snapshots every 2 minutes.
- On 14,416 games: AUC 0.78, calibration error 0.19 pp. Training takes ~30 s; the full export ~100 s.
- **Per-item scores (WPA = won minus predicted) do not reproduce.** Split-half agreement is about 0 in
  every slot (the gate needs 0.40), so the site shows "Not reproducible yet". Short windows (win-chance
  change 5 min after completion) cut the noise but shrink the effects as much: no net gain.

## 5. Historical Kai'Sa study: withdrawn without evaluation

- Research worktree `D:/Downloads/settistics-item-impact`, branch `item-impact`, commits `0c58748` and
  `0aefa31`. **These commits are not pushed.**
- One frozen contrast: Kai'Sa BOTTOM, 3rd item, Nashor-path components vs Hearthbound Axe. Exploratory
  pre-seal effect on final win: −3.72 pp [−7.20, −0.24].
- The historical protocol reserved games **starting at or after 2026-09-24 14:00 UTC**
  (`SEALED_FROM_MS = 1790258400000`) and set a 40,000-match/4,600-action-row gate for a narrow
  Kai'Sa contrast. This gate is historical, not an active threshold for the general model.
- **Withdrawn on 2026-09-25 without evaluation.** The research worktree's evaluation ledger now records
  `withdrawn_without_evaluation`; no claim file exists, and the old evaluator refuses with
  `duplicate_evaluation`. The frozen protocol and model remain unchanged. No v2 will run. The general
  method uses these games for chronological development in `pipeline/decisions.py`; the site's
  `USABLE_SQL` still excludes them. A new future evaluation set will test the general method.

## 6. Vision (agreed 2026-09-25, revised after the supervisor's call)

Judge every item, for every champion and slot, against the other choices in that slot at the same game
state. **What follows is the working plan, not a proven answer for every item.**

- **Final win** is an important check on every result.
- **Change in win chance over a fixed 10 minutes after finishing the item** is a candidate result. The
  model has no item or damage inputs, so it cannot award points just for seeing an item. That does
  **not** make it an unbiased measure of item strength: buyer selection and events elsewhere on the map
  remain.
- **Player impact** in fixed minutes after purchase, measured for every player: takedowns, deaths, time
  alive, damage share, team takedowns minus deaths while present, surviving large damage. **Offensive
  and defensive measures stay visible separately** until a combination is shown to work.
- **Clean 2v2 and teamfight results are a separate view**, never the only ranking measure. Looking only
  at fights with kills misses escapes and fights where nobody dies.
- **Ranking design is now chosen:** globally pooled outcome models and an item-choice model, followed
  by a regularized decision model targeting adjusted final-win value. Out-of-fold predictions of combat,
  survival and progress are additional candidate features. Their contribution is learned and tested
  against a version without them; no hand-written damage/kill points. See the authoritative plan.
- **No offense/defense item classes.** Keep outcome profiles distinct; neither damage nor a win-chance
  proxy is automatically an unbiased or equally informative measure for every item.
- **Evidence rules:** compare the recommendation policy with most-common choices on later games,
  including overlap, coverage, clustered uncertainty and subgroup checks. Calibrate any weighted
  reproducibility gate against false positives; correlation alone is insufficient.
- **Also shown:** "bought when ahead or behind" (the raw win chance at purchase), and opponent types as
  a fallback when a matchup is thin.
- **The Kai'Sa seal was withdrawn without evaluation.** Future confirmation must test the general
  recommendation policy on newly collected games under a protocol fixed before those outcomes are read.

### Patch-day and route-scale constraint

The product must work when a new patch has little or no data. **Do not train a separate model for every
champion × complete build path × patch.** The number of paths is too large, and day-one samples cannot
support a precise effect for each one.

- Learn shared patterns across champions, roles, item attributes, game states and recent patches. Reuse
  older evidence only for choices whose relevant items and context have not materially changed; test
  this transfer on later patches before relying on it.
- Recommend among a small set of common, legal routes, including item order and boots type/timing.
  Account for how an earlier purchase changes later choices; do not simply concatenate four independent
  slot winners. The general route method and its held-out validation are future work, not built yet.
- On patch day one, use a clearly labeled observed or carried-over route as the fallback. Mark changed
  items provisional, withhold unsupported item advantages, and update estimates as fresh games arrive.
  A major item or system change may invalidate older evidence entirely.
- Coverage is conditional: popular choices may earn a model-backed comparison, while rare items,
  matchups and new patches may remain `Measuring`. Never imply equal confidence across all champions,
  items and patches.

## 6a. Step board: build the general recommender

- [x] 1a. Record the old Kai'Sa study as withdrawn without evaluation; preserve its artifacts and
  disable its confirmation entry point. Do not run v2.
- [x] 1b. The loader uses released games with match-grouped chronological folds, time embargo and
  region/source provenance. The old site's public-data filter remains unchanged.
- [x] 2a. `pipeline/decisions.py` extracts dated purchases, inventory and pre-purchase context, final
  win, 5/10-minute gold, takedowns, deaths, damage and time alive for all roles. `pipeline/branches.py`
  defines common slot 1–3 alternatives at the first distinguishing component, retaining non-completers;
  upgraded-boots alternatives start at the upgrade purchase and are conditional on reaching it.
- [ ] 2b. Add clean-fight and clean-bot-2v2 profiles with extraction-quality checks as secondary views.
  The current model uses fixed-window player outcomes and does not require a kill-producing fight.
- [x] 3. Train shared XGBoost models for final win, item choice and five short-window outcomes, then a
  direct final-win model with out-of-fold auxiliary predictions. The private full-development report is
  `data/research/recommender/full-dev-v0/report.json`; its later block remains exploratory because these
  games were inspected during development. Base and enriched policy verdicts are both
  `insufficient_evidence`: the enriched policy switched 135/115,564 decisions under a 3-point margin,
  estimated gain −0.019 percentage points, 95% CI [−0.038, 0.000] points. The site must not call this a
  better policy. A post-hoc margin sweep and pairwise study also found no supported improvement.
- [x] 4a. Export a schema-checked, versioned research artifact with 640 champion/role/stage pairs, 527
  with enough arm support for predictions. The site shows model leans and predicted final win plus
  5-minute gold, takedowns, deaths, champion damage and time alive for slots 1–3 and boots. It labels
  these as predictions without uncertainty intervals, leaves the observed route in place, and states
  that no policy improvement is confirmed. The build copies only this aggregate, never raw records or
  private model files. `npm test` passes the full Python and Node suite; local browser QA found and fixed
  a narrow-screen table overflow.
- [ ] 4b. Do not promote model leans to recommended routes until a prospective policy evaluation supports
  a gain. Add calibrated uncertainty and affordability/state-specific selection before that promotion.
- [ ] 4c. **Next development priority: matchup-specific guidance.** Find supported situational item
  alternatives for a champion against a particular opponent, retaining the popular build when evidence
  is weak. Opponent identity already enters the shared model, but the public preview averages across
  opponents and no matchup-specific advantage is validated. Test whether conditioning actually helps,
  using shared evidence rather than a separate model for every matchup. Compare with both champion-wide
  and matchup-specific popular choices on chronological development games. Report support for both arms,
  uncertainty, coverage, overall policy value and value where choices change; do not increase switching
  merely to make the model appear more active. Prematch advice must average over historically plausible
  states for that matchup rather than assume future game state. Select the method before sealing a new
  future-game cohort; a prospective observational test still cannot remove unmeasured purchase bias.
- [ ] 5. Lock the general method and evaluate on new general-crawl games. Report estimated policy value,
  overlap, coverage and subgroup performance; do not validate it solely by prediction AUC or correlation.
  **Round r1 is frozen (2026-09-25), not yet run.** `pipeline/prospective.py` holds the method, the game
  lists and the rules; `pipeline/prospective-r1.lock.json` holds the frozen code hashes. Training is exactly
  the development dataset (50,077 games in `data/decisions.sqlite`, last start 2026-09-25 05:32:18 UTC).
  The test is every finished general-source game outside it that was collected after its last game, whenever
  it was played. None was used or seen during development. Because many test games were played on the same
  days as training games, r1 tests unseen games, not forward-in-time transfer; later rounds should test
  forward. (A first r1 draft used a start-time cutoff, which left only ~4,900 test games; it was replaced
  before any test game was read.) `node scripts/python.mjs pipeline/prospective.py check` snapshots the
  development game list and shows the test count; `... run --backend xgb-cuda` builds the data from the two
  lists, fits, opens the ledger (`data/research/prospective/r1/`) and reads the test outcomes once. It
  refuses changed code, a changed development list or a second run. Primary policy: enriched. **Pass** =
  recommender gate `supported_improvement` (overall gain's 95% lower bound above zero), gain per departed
  decision at least 1 point, at least 5% of test decisions supported, and no region, patch or gold-state
  slice (behind < −1,000 ≤ even ≤ 1,000 < ahead team gold) with at least 50 departures whose 95% upper
  bound is below −1 point. Gate shortfalls (under 1,000 games, under 200 departures, poor overlap) are
  **inconclusive**; anything else is **fail**. After the run, every game may train the next round, which
  needs new later-collected test games, new rules and a new lock. The same games are never tested twice.
  Expect few departures (development: 135 of 115,564).
- [ ] 6. Test transfer across historical patches, then reuse compatible evidence with change-aware
  confidence. New or materially changed choices stay provisional. No per-path, per-patch model explosion.
- [x] 4d. **2026-09-26, toward 4b's calibrated uncertainty:** found the win model has 0 splits on the
  item-choice action feature for a meaningful share of pairs (267/864), so `modelLean` now distinguishes
  "indistinguishable" (the model cannot see the choice at all) from a genuine near-zero tie. Built a
  doubly robust per-pair estimator (`pipeline/dr_pairs.py`) with match-clustered CIs, a within-pair-
  permutation null calibration check, and a propensity `base_margin` offset fix for rare arms (one pair's
  propensity was off by 36.7x before the fix). 7 of 864 pairs show a real, hardened, family-wide
  Benjamini-Hochberg-surviving adjusted effect the shipped model cannot represent. Full details, numbers
  and the outstanding independence caveat: model-release-status-2026-09-26.md.
- [x] 4e. **Concluded, 2026-09-26: hold; consistent with enemy-composition confounding (not confirmed),
  not promoted.** Empirical-Bayes shrinkage (`pipeline/dr_shrink.py`) pools per-pair estimates within
  (stage, item pair) groups across champions. Rerun with genuinely independent within-half cross-fitting
  (fixed a sharper bug along the way: a crc32-based salted fold split was not actually independent of the
  unsalted one, because CRC32 is affine and same-length match_ids made the two splits collapse onto each
  other -- fixed with a sha256-based fold hash). With independent halves the checkpoint still barely clears
  its pre-declared bar overall, but boots-vs-slot and leave-one-group-out checks show the entire result
  rests on one boots comparison (Plated Steelcaps vs. Mercury's Treads, 86 of 786 pairs): non-boots alone
  shows no signal at all, and dropping that one group flips the checkpoint to not-proceed. The data show
  the signal is not robust; they do not prove the cause, though a defensive-boots-vs-enemy-composition
  pattern is the obvious candidate. Per the pre-declared stopping rule, no estimate from this work is
  promoted to a product number; the site's "indistinguishable" copy is unaffected. Optional half-day
  follow-up, not started: add an enemy-team damage-mix feature to `recommender.py`'s `Encoder.context` and
  rerun `dr_pairs.py` for this one group -- if its effect collapses toward 0, confounding is confirmed; if
  it persists, it's a real matchup-dependent effect (ties into 4c). Full numbers: model-release-status-2026-09-26.md, item 7. What
  would still settle it: a pre-registered prospective test on the sealed future-start cohort once it has
  enough games -- not scheduled yet.
- [x] 4f. **2026-09-26, later same day: Stage 0 diagnostic run; decision made to build a pregame effect model,
  not run another prospective round.** A merged branch brought in an independent causal-inference review and a
  supervisor verdict (`docs/item-policy-structural-fix.md`): the current swapped-arm recommender architecture
  is structurally underpowered for prospective testing (about 350 changed decisions per round detects only
  ±16 pp; a realistic 1 pp effect needs ~95k changed decisions), so **no more prospective rounds of it,
  r3 included** -- r2's earlier +12.2 pp pooled result is now labeled "not confirmed." Its own smallest next
  step, a read-only diagnostic (`pipeline/effects.py`, reusing r1/r2 files, never touching the sealed cohort),
  ran on 1,898,049 branch rows. Confirmed two real problems (context effectively measured after the choice is
  made: median 144 s / ~1,000 gold drift; a non-zero skill-confounding negative control), but also found real
  per-pair heterogeneity (tau = 1.61 pp after adjustment) that survives team-composition and player-history
  adjustment, with signs that flip by champion. Decision rule: **build the pregame effect model (Stage 1)**.
  Quick plan in `docs/item-policy-structural-fix.md` section 12; result numbers in
  `data/research/effects/effects.json`. This does not touch the frozen r3 model or the site.

Keep the four general crawlers collecting throughout. There is no separate demo task or Kai'Sa-first
milestone. The full design and first-release acceptance criteria are in
[general-item-model-plan.md](general-item-model-plan.md).

## 6b. Earlier reviews

Earlier proposals to run Kai'Sa v2, delay general collection, or use one short-window proxy as an
item-strength certificate are superseded. Their history remains in prior revisions. Follow the
[general model plan](general-item-model-plan.md), including its observational limits and validation
requirements, rather than reviving an old checklist.

## 7. Rules that must hold

- The Kai'Sa study is withdrawn; its data may be used for general-model development with match-grouped
  temporal splits. The current site/export filter stays unchanged until its replacement is validated.
- Keep focus-source provenance, including focus-discovered queued games completed later. Exclude these
  from general public aggregates unless their sampling is explicitly handled; no invented weights.
- Model recommendations require the general plan's support and evaluation gates. Prediction accuracy
  or split-half correlation alone does not validate an item or a complete route.
- Never average item timings across build orders.
- Commit, push and deploy without asking once tests and browser checks pass. Ask before touching
  secrets, the password gate or Riot keys.

## 8. Open questions for the user

- None blocking. Four all-champion crawlers continue. Next model task: build the Stage 1 pregame effect
  model (`docs/item-policy-structural-fix.md` section 12), not another prospective round -- rounds r1 and r2
  already ran and are superseded by the 2026-09-26 supervisor decision (item 4f); the current recommender
  architecture is too underpowered for a prospective round to answer the product question.
