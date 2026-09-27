# General item recommender: decided model plan

Decision: 2026-09-25. This is the target architecture, not a report of implemented capabilities.
It supersedes the competing model proposals and Kai'Sa-first roadmap in earlier notes.

## Product and success criterion

Build a shared, patch-aware contextual item recommender. Its objective is to recommend purchases that
perform better than the most-common-choice baseline in comparable situations. Final wins measure the
ultimate objective; fighting, survival and short-term progress provide additional training signals and
explanations. Improved win prediction alone does not demonstrate improved recommendations.

Cover all champions and roles with shared models, starting with first item, second item, boots and
third item. Coverage depends on evidence. Kai'Sa is a worked example, not a separate product model.
The site remains a pregame guide: show the model's supported item preferences, a legal route template,
situational alternatives and combat/survival explanations. Do not assume access to live game state.
The user has removed the separate observed-route Riot demo milestone: the first working release of
this actual recommender is what will be shown. Fallbacks remain for unsupported choices, but a
popularity-only page does not fulfill this deliverable.

## 1. Data and decision unit

- Continue EUW, KR, NA and VN general collection, one collector per Match-V5 routing host.
- The old Kai'Sa confirmation was recorded as withdrawn without evaluation on 2026-09-25. Its frozen
  artifacts remain intact, and its ledger blocks the old evaluator. Do not create or run Kai'Sa v2.
- After that administrative step, all eligible collected games can enter general-model development.
  This does not mean every row is used both for training and evaluation: use forward chronological
  development folds, with every player and event from one match in the same fold.
- Keep region, patch and collection-source provenance. Focus-discovered games are a selected sample,
  including previously queued games later completed by general collection. Use general-collection
  games as the main evaluation population; report whether adding focused games changes the result.
  Do not invent sampling weights when the selection probabilities are unknown.
- Date a decision at an observable purchase or recipe branch. Use inventory, recipes and prices as
  they existed then. Do not infer intent from the item eventually completed. Keep non-completers in
  comparisons of observed component actions. Ambiguous shared components remain ambiguous.
- Compare supported, legal alternatives with comparable spending opportunities. Exact affordability
  is not always recoverable from minute frames; label unknown availability and exclude uncertain
  comparisons from claims that a player could have bought either alternative.
- Completion-based windows can describe completed-item performance, but do not convert them into a
  claim about choosing that build path before completion.

## 2. Inputs available before the decision

Champion and role, opponent and team composition, patch and region, game minute, inventory and purchase
history, recipe progress, gold/XP/level gaps, recent combat and gold trends, objectives, observable
death/buff state, and player history from games that finished before the current game started.
Use item identity plus patch-specific cost, stats and effect descriptors so the model can share
information without treating every item as interchangeable.

Future events are outcome labels only. No final inventory, later purchases, realized post-purchase
damage or other future state enters the decision's adjustment features.

## 3. Outcome models: one shared dataset, several targets

Start with the existing XGBoost infrastructure: a small set of globally pooled models, one per target,
trained across champions and slots with context and candidate action as inputs. This is not one model
per champion, item pair or complete route, nor a claim that separate trees share neural-network weights.

Predict these outcomes for each supported candidate action:

| Outcome family | Initial targets | Purpose |
| --- | --- | --- |
| Final result | Team win | Long-term objective and check on proxy rankings |
| Short-term progress | Gold-lead change and frozen-model win-chance change at 5 and 10 minutes | Immediate advantage and timing |
| Combat and survival | Takedowns, deaths, time alive, champion damage and damage share over fixed windows | How a choice performs in combat |
| Clean fights | Observed 1v1/2v2/teamfight results, participation and survival, with extraction quality flags | Conditional fighting profile and diagnostic |

Extract windows for everyone, including players who do not fight or who die. Do not divide all outcomes
only by time alive, discard no-kill windows, or stop measurement when an item is sold. Define terminal
game handling before fitting: final win chance becomes the result, event accumulation stops at game
end, and observed exposure is recorded. Report incomplete-window sensitivity.

Kill events do not reveal every combat participant or every no-kill fight. Verify available timeline
fields and reconstruction quality before advertising clean fights. Fight-conditioned results describe
selected fights, not the total effect of a purchase. They are not an independent causal test.

## 4. Recommendation model and learned use of the extra outcomes

Fit an item-choice model from pre-decision context alongside the outcome models. Restrict comparisons
to contexts where alternatives were actually observed. Use cross-fitted, doubly robust comparisons
and shrinkage toward supported broader estimates to reduce observed purchase-selection bias and noise.
This does not remove unmeasured skill, intent or coordination bias.

Train a regularized shared decision model against adjusted final-win targets. Its candidate features
include out-of-fold predictions from the short-term progress and player-impact models, plus context
and candidate identity. Thus it can learn when expected damage, survival or early advantage is useful
for winning, instead of assigning hand-written points to kills or damage.

All auxiliary predictions for a training row must come from models that did not train on that match;
the outer validation period is excluded from every fitting and selection stage. Use pre-decision
predictions of impact, not the player's realized future impact, as decision-model inputs or row weights.
Keep the same decision model without auxiliary predictions as an explicit ablation. The richer version
ships only if it improves held-out recommendation performance; these extra features do not guarantee
more information or statistical power.

Rank supported candidates by estimated final-win value with a validation-chosen uncertainty penalty.
Return a leading choice, reasonable alternative, support/uncertainty and the independently evaluated
impact profile. When alternatives cannot be distinguished reliably, show the observed baseline and
say the model has no clear preference. Do not describe predictive differences as established causal
win-rate gains. Explanations must report evidence, not invent an item mechanic from a feature weight.

This keeps final win as the success criterion while actually training on richer outcomes. It is not
the old score of merely averaging final result minus pre-purchase win chance for item buyers.

## 5. Build order and patch reuse

Learn next-purchase comparisons conditioned on existing inventory and recipe progress, including boots
type and timing. Display a small set of common legal route templates, not every possible four-step path.
Do not add independent item effects to assert a whole-route gain: earlier purchases change later
states. Until a route-level evaluation exists, label the full path an observed template with model
guidance at supported decisions.

For pregame advice, average over a documented distribution of plausible states for that champion/role
and matchup using earlier data only. Do not pretend the player's future gold or completed inventory
is known. Show conditional alternatives where the preferred action changes with game state.

Reuse compatible evidence across recent patches; encode item/champion/system changes and recency.
Unchanged item stats do not guarantee unchanged performance when the surrounding game changes. Test
transfer using historical forward-patch splits, and reduce or withhold transferred confidence after
material changes. New or changed items are provisional; their attributes alone do not prove superiority.
On day one, show carried-over or observed defaults with provenance. Retrain shared models periodically,
not a separate model for every path on every patch.

## 6. Evaluation and release

During development, use forward temporal folds for model selection and comparison. Repeatedly inspected
development scores are exploratory. At release, lock the complete method and compare its policy with
the most-common-choice baseline on subsequently collected general-crawl games. Evaluate the supported
population, report the share of decisions covered, and retain the baseline on unsupported decisions.

Use overlap-aware off-policy estimates of final-win value, with uncertainty accounting for matches and
repeat players; separately report direct observed outcomes and short-term metrics. These evaluations
still require observational assumptions and do not prove counterfactual outcomes for unseen choices.
Check region, patch, champion/role and ahead/even/behind slices. Compare calibration and placebo behavior,
and verify that any revised reliability gate detects planted signals without inflating false positives.
No single split-half correlation or larger win-prediction AUC is the release criterion.

Before the final evaluation, specify minimum practical improvement, uncertainty/support rules and
material subgroup-regression limits using development data. Do not adjust these after seeing the final
result. One general-method evaluation replaces a separate sealed experiment for every item pair.
Retain a future evaluation stream as the deployed model evolves.

Pre-purchase controls must predate the action under study, not merely the final item's completion.
First-item completion versus not-yet-completed is a confounded diagnostic, not a guaranteed positive
control. Validate extraction and effect recovery using synthetic data with known ground truth too.

## Implementation order

1. [Withdrawal recorded.] Release its data for general-model development by updating the loader,
   and implement source-aware, match-grouped temporal splits. Preserve the old experiment.
2. Build generic observable purchase decisions and the fixed-window impact extractor for all champions;
   add clean-fight diagnostics with measured reconstruction quality.
3. Fit the shared outcome and choice models, then the decision model with/without auxiliary outcomes.
   Report coverage, bias diagnostics and performance versus most-common choice.
4. Integrate the actual model output into the site: first item, second item, boots and third item where
   supported, with situational alternatives and combat/survival explanations. Route templates stay
   labeled until route-level validation exists. Mark development results experimental.
5. Freeze and evaluate the general method on future games; add cross-patch transfer after historical
   tests demonstrate it works. A failed evaluation is a reason to improve the method, not weaken the gate.

First-release acceptance: the site consumes a versioned model-generated recommendation artifact,
shows supported model preferences where evidence warrants departing from the popularity baseline,
and exposes coverage and uncertainty. The report must compare recommendations with that baseline on
held-out development games, including the auxiliary-outcome ablation. Do not force differences from
popularity to meet a demo target. If there is no supported improvement, report that implementation
result honestly and continue improving the model; displaying observed builds alone is not completion.

The study withdrawal is recorded. This document does not itself change data loaders, train models,
or deploy recommendations; those are implementation steps above.

## Method references

- [EconML: doubly robust learning](https://www.pywhy.org/EconML/spec/estimation/dr.html): outcome and
  action-choice models support adjusted comparisons under identification and overlap assumptions.
- [Athey et al.: surrogate index](https://www.nber.org/papers/w26463): replacing long-term effects with
  short-term surrogate effects requires strong assumptions. This plan retains direct final-win
  evaluation and does not assume that fight or damage gains establish a win benefit.
