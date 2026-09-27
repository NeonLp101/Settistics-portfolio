# Settistics

Settistics is a League of Legends matchup and build explorer built from real Riot
match timelines. It shows observed item order, boots timing, runes and spells for a
champion, role and lane opponent, with sample sizes and uncertainty. It describes
what players build and how those games went; it does not claim that one item causes
more wins than another. The [site preview](https://settistics.netlify.app/) is
password-protected while the product is under review. This repository is a
sanitized portfolio snapshot for technical review; personal legal-notice details
remain in the private deployment source.

A causal item-recommendation model was researched from 2026-09-24 to 2026-09-27 and
ended: at this data scale, item-versus-item effects on winning are too small to
separate from noise and from who buys which item. Its code was removed; the thesis,
approach, hardships and reasons for stopping are in
[docs/item-model-retrospective.md](docs/item-model-retrospective.md).

## What is implemented

- The matchup guide uses real exported data: icon-led setup, first-item/boots-order
  routes, route-specific purchase timing, and connected slot comparisons. Sparse
  matchups use a labeled general champion setup; unsupported timing stays unavailable.
- Rune, spell and item artwork is self-hosted. Build routes preserve first-item and
  boots-order cohorts; later slot leaders are descriptive choices, not a claimed optimal build.
- Full current Data Dragon champion roster for both selectors.
- Server-side Riot bridge with real key verification (key never sent to browser).
- A resumable local Python collector backed by SQLite; ladder or Riot-ID seeds.
- A provider-neutral JSON/CSV import path for aggregate datasets the operator has
  permission to reuse. It performs no third-party web scraping.
- Source precedence that prevents overlapping providers from being added together:
  matching local Riot timeline buckets win; otherwise one aggregate source is used.
- Deduplicated match details and timelines; patch-matched item definitions.
- Observed matchup win rates, early starting packages, purchased items, full rune
  pages and spell pairs. Wilson intervals and sample denominators are displayed.
- A win-probability model (WPA) that predicts the winner from game state, and per-item pp: how much
  more often buyers won than their game state predicted. An item is marked a "possible edge" only with
  1,000+ scored games and a result three standard errors from the slot average.
- Route cards: most played, best adjusted win rate (shrunk toward the average), highest pp and best
  of both.
- Lane duels: gold lead in top and mid until the first gank, per matchup and per early choice.
- First-blood and first-tower rates per champion, shown against the role average.
- Tests with isolated synthetic fixtures; no synthetic match statistics are published.

Raw matches, player identifiers, local databases and private research outputs are
not part of this repository or the public-site build. Reproducing numerical model
results requires separately collected Riot API data; the tests use synthetic
fixtures solely to check the software.

## 1. Requirements and secrets

Install Node.js 20.12+ and Python 3.10+ (Windows: enable Python on PATH).
Collection, SQLite storage and export need only the Python standard library.

Copy `.env.example` to `.env` in this folder and replace the placeholders:

```text
RIOT_API_KEY=RGAPI-your-actual-key
SETTISTICS_ADMIN_TOKEN=your-long-random-private-token
```

Never send these values in chat, commit them, or put them in HTML. The Python
collector and local server load `.env` automatically; existing environment values
take priority. Development keys expire every 24 hours. Re-run after rotation to
resume. A key configured on Netlify is not automatically available on your PC.

## 2. Collect actual games

Run the following from the project folder. On Linux/macOS use `python3` where your
installation does not provide `python`.

```bash
python pipeline/engine.py seed --tier EMERALD --division I --players 50
python pipeline/engine.py discover --players 50 --per-player 50
python pipeline/engine.py collect --champion Sett --role TOP --patch 16.18 --max-scan 1000 --max-matches 100
python pipeline/engine.py export --champion Sett
npm start
```

Open http://127.0.0.1:8888. No npm install is needed for this local server.
Use the patch you want to study; `16.18` is an example, not a permanently current
version. Without `--patch`, the collector accepts discovered patches separately.
One seed page/division is a convenience sample, not representative Emerald+ data.
Add other pages/divisions/tiers explicitly to broaden it.

For a small connection/ingestion test, seed your own public Riot ID instead:

```bash
python pipeline/engine.py seed --riot-id "Your Game Name#TAG"
```

If the current ladder response lacks PUUIDs, collection stops with a message to
use Riot-ID seeds. It does not guess deprecated identifiers or bypass access.

### Continue or refresh

```bash
python pipeline/engine.py status
python pipeline/engine.py discover --refresh --players 50 --per-player 50
python pipeline/engine.py collect --champion Sett --role TOP --patch 16.18 --max-scan 1000 --max-matches 100
python pipeline/engine.py export --champion Sett
npm run build
```

- Repeating `discover` without `--refresh` pages backwards through each seed's
  history. Keep `--since` unchanged when using that historical cursor.
- `--refresh` fetches the latest page and does not change the historical cursor.
- Match IDs are deduplicated. Details are saved before timelines; interrupted
  runs resume from committed data. Irrelevant cached matches do not starve new work.
- `--max-scan` bounds scanned candidates, not qualifying matches. You may collect
  fewer timelines than requested, especially for rare champions.
- `collect --champion all --role ALL` retains all qualifying ranked matches.
  `export --champion all` publishes all extracted champion/role groups. This can
  create a large static export; partitioning/indexed serving is a later scale step.
- Run at most one collector per routing bucket without a shared limiter. Riot enforces
  application limits per routing value: EUW (`euw1`/`europe`) and KR (`kr`/`asia`)
  can crawl concurrently on one key. Workers sharing a routing value need a shared
  limiter. Each collector follows received rate-limit windows and full Retry-After delays.
- Focused crawl (used since 2026-09-25 for Kai'Sa bottom): `crawl --champion Kaisa --role BOTTOM
  --focus --snowball-cap 3000` checks only players of that champion and role, and grows
  that pool only with new ones found in kept games. Fill the pool first with
  `seed --focus --from-games` (players already in stored games) or `seed --focus` from the
  ladder; ladder seeds are checked once and then drop out. Games found this way are stored with
  source `focus kaisa:BOTTOM`: a targeted sample, not the general crawl, so exports and
  research should tell them apart. `pipeline/console.py` starts one per routing cluster (EUW, KR, NA, VN).
- No perpetual background service has been installed. Run bounded jobs yourself;
  deploying the frontend does not run the collector. Plan scheduled infrastructure
  separately before expecting continuous updates.

## 3. How data is interpreted

The inferred opponent is the unique enemy in the same `teamPosition`. Unknown or
ambiguous roles are excluded; lane swaps are not definitively reconstructed.
Non-Solo/Duo, non-Summoner's Rift, incomplete and early-surrender games are excluded.

Item purchases are read from timelines, never inferred from final inventory.
Matched undos cancel purchases/sales. Unmapped or cross-boundary undos make that
participant's purchase ledger ineligible. This deliberately loses some data rather
than guessing. Item occurrence counts an item once per participant-game; occurrence
across items does not sum to 100%. Average purchase time is the first observed buy.

Starting packages use net purchases through 60 seconds. Early upgrades, unknown
items, uncertain ledgers and games with pre-60-second kills are excluded. This is
an early-game proxy, not an exact shop-departure record or randomized treatment.

Stored purchase features use ONLY snapshots strictly earlier than each event. The
snapshot age is recorded; missing state stays missing. Available gold is explicitly
not an exact reconstruction of purchase-time budget.

Rank filters are disabled. A seed's rank today is NOT the rank of every player in
their historical matches. The export does not imply Emerald+ just because seeds
came from Emerald. Item/rune WR remains associational. Wilson intervals assume
independent games; repeated players and biased sampling can widen true uncertainty.

## 4. Optional licensed aggregate coverage

There is no U.GG downloader in this project. U.GG does not document a public bulk
dataset API, and Riot asks registered products to use supported ingestion services.
If a provider gives you an export and permission to reuse it, validate it with:

```bash
npm run import:aggregate -- \
  --input /path/to/provider-export.json \
  --provider-id licensed-provider \
  --provider-name "Licensed Provider" \
  --source-url "https://provider.example/dataset" \
  --rights-confirmed
npm run build
```

JSON input is either an array of buckets or `{ "buckets": [...] }`. Each bucket
uses the public fields `champion`, `opponent`, `role`, `patch`, `region`, `games`,
`wins`, `eligible`, and optional `choices`. A choice has `kind`, `id`, `label`,
`games`, `wins`, and optional `timeSum`/`timeCount`. The importer rejects negative
or impossible denominators, duplicate slices, unknown roles and malformed patches.

CSV uses the same dimension and count names plus `eligiblePackages`,
`eligibleItems`, `eligibleRunes`, `eligibleSpells`, and optional choice columns
`kind`, `id`, `label`, `choiceGames`, `choiceWins`, `timeSum`, `timeCount`.

Imported aggregates are descriptive only. They cannot enter WPA research because
the individual pre-choice states, timelines and player clusters are unavailable.
When several imported providers overlap, the interface selects the single provider
with the largest matching sample. It never adds them. Matching locally collected
Riot timeline data always takes precedence.

## Win Probability Added (research)

After collecting, train the win-probability model and score every build decision, then export as usual:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-model.txt
.venv/Scripts/python pipeline/wpa.py
python pipeline/engine.py export --champion all
npm run build
```

`wpa.py` rebuilds the game state every 2 minutes from each stored timeline (gold,
XP and level gaps, objectives, draft strength), trains cross-fitted gradient
boosting with Platt recalibration, and scores purchases using models that did not
train on those games. The ranking of every item in a slot does not reproduce across
split halves, so the site shows each item's pp with its range and flags only items far
beyond chance as a "possible edge", never as a proven item advantage.

## Lane 1v1 (prototype)

```bash
.venv/Scripts/python pipeline/lane.py
python pipeline/engine.py export --champion all
npm run build
```

`lane.py` measures top and mid lanes as pure duels: each lane counts from minute 1 (or from an item purchase)
until the first interference, meaning a kill or assist with a third champion or a second per-minute snapshot
with another champion nearby. The outcome is the change in the gold lead against the lane opponent; a
cross-fitted model predicts it from the state at the start, and the site shows "Lane Advantage Added" in gold,
compared with the average choice. Keystone and summoner spells need no item data; starting items, first item
and boots appear once the patch's Data Dragon item list is available.

## 5. Private/public boundary and deployment

- `data/settistics.sqlite`: PRIVATE raw matches, timelines, player identifiers.
- `data/research/`: PRIVATE experimental reports.
- `data/imports/`: validated, permission-confirmed aggregate inputs; ignored by Git.
- `data/public/stats.json`: aggregate counts only; export via the provided script.
- `public/`: explicit build output with HTML/JS/CSS and aggregate JSON only.

`npm run build` copies an allowlist into `public/`. The local server only serves
that allowlist. Never serve or drag-upload the whole project directory. Back up
the SQLite database securely while the worker is stopped; keep its WAL files if
copying an active database. Raw data needs an appropriate retention/deletion policy
before production-scale ingestion.

For Netlify, use its CLI or Git-based build so functions are deployed:

```bash
npm install
npx netlify login
npx netlify init
npx netlify env:set RIOT_API_KEY
npx netlify env:set SETTISTICS_ADMIN_TOKEN
npm run build
npx netlify deploy --prod
```

The publish directory is `public`, functions live in `netlify/functions`. Keep the
site private while using development/personal access. Public launch requires the
appropriate Riot production approval. Publishing/redeploying has not been done
by this package. After local collection/export, rebuild and redeploy to refresh
the published aggregate snapshot; a hosted frontend does not see your local DB.

## 6. Tests and current limits

```bash
python -m unittest discover -s tests -v
node --test tests/backend.test.mjs
npm run build
```

Model tests skip if optional dependencies are missing. Browser tests in
`tests/browser.cjs` require Playwright and Chromium, and a local server on 8888.
They intercept test statistics in memory; no fixture data is written to the site.
`tests/dom.cjs` offers DOM interaction checks with optional `jsdom` and no network.
Run `npm test` for the maintained Python and Node suites. The site build accepts
only an explicit allowlist of frontend files and aggregates; private databases,
keys and research reports are excluded.

Observed win rates are associations. Observational match data retains selection
bias (who buys an item, against which team) that adjustment only partly removes;
the ended item-model research found that bias as large as the item effects.

## Source availability

This public repository is a sanitized source snapshot with a fresh history. The
legal-notice pages contain links to the hosted site; the actual notices remain in
the private deployment source. Do not deploy this snapshot as a public website
without supplying the legally required notices. No open-source license is granted
for reuse or redistribution; contact the maintainer for permission.

Sources:
- https://developer.riotgames.com/apis
- https://developer.riotgames.com/docs/portal
- https://developer.riotgames.com/policies/general
- https://u.gg/faq
- https://u.gg/terms-of-service
- https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html
