# Settistics: current state and plan

**Living document. Last updated 2026-09-25 (ML progress refreshed).** Update this file whenever the crawlers, the
model or the plan change, so no session has to reconstruct the state from chat history.

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
- [ ] 6. Test transfer across historical patches, then reuse compatible evidence with change-aware
  confidence. New or materially changed choices stay provisional. No per-path, per-patch model explosion.

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

- None blocking for implementation. Four all-champion crawlers continue; the next model task is study
  retirement and the general decision dataset, followed by the multi-outcome recommender.
