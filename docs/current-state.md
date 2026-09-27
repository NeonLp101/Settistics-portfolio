# Settistics: current state

**Last updated 2026-09-27.** Settistics is a descriptive matchup and build explorer: it shows what players
build, when, and how those games went, with sample sizes and uncertainty. It does not claim that an item
causes wins.

The causal item-recommendation model was researched and **archived on 2026-09-27**. Its thesis, approach,
hardships and the reasons for ending it are in [archive/item-model/README.md](../archive/item-model/README.md);
the full state of that work as it stood is in
[archive/item-model/docs/current-state-at-archive-2026-09-27.md](../archive/item-model/docs/current-state-at-archive-2026-09-27.md).

## What is live

- **The site** (password-protected preview until Riot production approval): build routes by first item and boots order, route-specific
  purchase timing, slot comparisons, runes, spells, counters and lane results, all from observed games.
- **Win-probability model** (`pipeline/wpa.py`): predicts the winner from game state (gold, XP, levels,
  objectives, side), cross-fitted and Platt-recalibrated. Retrained 2026-09-27 on every unsealed game:
  173,763 games, 25.5M snapshots, AUC 0.780, calibration error 0.15 pp (features 16 min, GPU fit 81 s).
  Its per-item WPA scores are shown only where they replicate across halves.
- **Lane model** (`pipeline/lane.py`): gold lead in top and mid lanes until the first interference ("pre-gank
  gold lead"), plus clean-window lane gold per starting item, keystone, spells, first item and boots. Rerun
  2026-09-27 on every unsealed game, parsing in parallel: 1,320,639 clean windows (was 265,480); the model
  explains 14.5% of the gold swing (lane state alone: 4.5%). Per-item lane numbers show only where they
  replicate across halves (gate 0.40). With 5x the windows, per champion: starting items 0.37, keystone
  0.36, spells 0.34, first item 0.14, boots 0.08 (the old window's 0.33 / 0.29 for first item and boots did
  not hold up); pooled by role, keystone passes (0.76) and is shown. This is lane gold against the average
  choice in the same matchup, not proven item power: player skill is not adjusted for.
- **First blood and first tower** (a count, no model): the guide header shows how often the champion draws
  first blood and helps take the first tower in the selected games, against the role average, from 100 games.

## Data

- `engine.USABLE_SQL` decides what the site and its models use: finished games with timelines, excluding
  focus crawls, and starting before **2026-09-26 05:27 UTC** (`engine.FUTURE_SEAL_MS`). Games after that
  are sealed, set for the archived model's evaluation, and still held back.
- Four all-champion crawlers, one per routing cluster: EUW (`data/collect.log`), KR, NA and VN
  (`data/collect-<region>.log`). A second crawler in the same cluster only splits Riot's rate limit.
- The development key expires every 24 h; crawlers stop with HTTP 401 until a new key is pasted into
  `.env` and they are restarted from the console.
- Focus-crawl games stay tagged (`matches.source = 'focus …'`) and out of every public aggregate.

## Where ML can still help

Prediction and description hold up on this data; causal item claims did not. Candidates, none started:

1. A **win-chance timeline** per match with its biggest swings (the WPA model already exists).
2. **Build archetypes**: cluster observed builds into a few named styles per champion.
3. **Shrunk win rates** on every descriptive stat (empirical Bayes, as in the archived `dr_shrink.py`).
4. **Patch-change detection** for items and champions.
5. **Player benchmarking** against the same champion and rank (needs the production key).
6. **Items against composition types.** Cluster champions from real per-game stats (time spent crowd-
   controlling, magic vs. physical damage share, damage mitigated, attack range) into scores rather than hard
   classes, describe each enemy team by those scores ("CC 72%, magic 40%, tank 55%"), and show what players
   build against teams like that and how it went. Pooling by composition type fixes the sample-size problem;
   it does not fix why players chose the item (they already buy Mercury's Treads *because* the enemy is
   CC-heavy), so the claim stays descriptive. The defensive-boots split by enemy magic share in the archive
   is a small working example.

## Rules that must hold

- Never average item timings across build orders.
- Keep focus-crawl provenance; no invented sampling weights.
- Commit, push and deploy without asking once tests and browser checks pass. Ask before touching
  secrets, the password gate or Riot keys.
