# GPU experiments and aggregate refreshes

The visitor chooses a pregame champion, role, matchup and patch. The displayed route
stays fixed. Game state enters local training only. This change does not generate an
Optimized path or claim a best route: the forward policy, overlap, uncertainty,
state-stratum, placebo and sensitivity checks in [the plan](optimized-build-plan.md)
remain outstanding. AUC measures outcome prediction, not item impact.

## Local commands

Install `.venv/Scripts/python.exe -m pip install -r requirements-gpu.txt` once on Windows.
The npm Python launcher now prefers the project virtual environment; `PYTHON` overrides it.

- `npm run train:compare`: private forward outcome benchmark in `data/research/gpu-comparison.json`.
- `npm run train:gpu`: WPA training forced onto CUDA. `pipeline/wpa.py` defaults to `--backend auto`:
  the GPU when a real CUDA fit succeeds, otherwise the same XGBoost model on CPU.
- `npm run data:refresh`: compare, train (auto), export, build, validate and publish in order.
- `npm run data:refresh -- --cpu`: train with the older HistGB learner instead.
- `npm run data:validate`: check the assembled public aggregates without uploading.
- `npm run data:publish`: publish an already successful training/export/build output.

The refresh stops on any failing stage and never runs a production deploy. Keep the
collectors running. It uses their WAL database and does not stop or restart them.
Training writes only local prediction tables. Local export retains its existing retention policy.
Run a single refresh at a time; the remote manifest also protects against competing publishers.
For unattended use, provide `NETLIFY_SITE_ID` and `NETLIFY_AUTH_TOKEN` in the local process
environment. Interactive use can reuse the linked site and existing Netlify CLI login.
Neither credential is copied to an artifact, source control, or the browser.

## Feature cache

Parsing match JSON and rebuilding game state dominated training (about 10 of 11 minutes). A GPU
cannot do that work, so `pipeline/features.py` makes it cheap instead: numpy computes every
moment of a game at once, a process pool uses all cores but two (the collectors keep theirs),
and each match is parsed once into both WPA arrays and export records. The private cache
`data/settistics.features.sqlite` (beside the database it derives from) is reused until the extraction code (engine, wpa, features) or a
patch's item catalog changes; then exactly the affected matches are recomputed. Matches that
left the main database are removed from the cache on every read, and purge, forget and
anonymize delete the cache file. Tests compare the vectorized features with the reference
`Game.state` and cached records with direct extraction.

## Frozen model comparison

`pipeline/backends.py` uses XGBoost 3.2.0 histogram trees with `device=cuda:0`.
After every fit it checks the trained booster configuration and fails if CUDA silently
fell back to CPU. The XGBoost CPU comparison has the same hyperparameters and seed.
The original histogram boosting, logistic and Extra Trees families are baselines.

The benchmark runs two populations: one state strictly before minute 10 for each participant
in games lasting over 10 minutes, and every two-minute WPA training moment (the production
workload, used for the hardware decision). It reads cached features without writing to the
collectors' database. Every learner
gets identical features, rows, time boundaries and a separate earlier calibration window.
Three expanding windows evaluate the later 40–60%, 60–80% and 80–100% of distinct match
start times. Their calibration windows are the immediately preceding 10%; fitting uses
only times before that. Ties and all participants in a match stay together. Player IDs
never enter features; player history is not encoded. Separate cold-player metrics audit
participants absent from both fitting and calibration windows. Repeated players in the
main temporal evaluation do not imply cold-player or causal generalization.

Parameters are fixed before evaluation. The private report includes split fingerprint,
counts, cutoffs, patch/region coverage, AUC, Brier, log loss, calibration and wall times.
Do not compare its minute-10 AUC directly with the all-moment production WPA AUC.
CPU/CUDA timing includes fitting, calibration and prediction, excluding shared extraction.
A single run is not a general speed guarantee; GPU transfer cost can outweigh fitting time.

Results and the per-stage hardware choice are in [the benchmark](gpu-benchmark-2026-09-24.md).
The full WPA backend retains the existing five match-hash folds, separate base-model
and calibration training, pre-purchase features and unchanged split-half display gates.
The optional GPU changes the learner, not the interpretation of residuals. Post-purchase
curves describe subsequent game evolution and do not enter pre-purchase features.

## Publication protocol and fallback

The site-wide Netlify Blobs store is `settistics-aggregate-v1`, accessed with strong
consistency. It is independent of a deployment. Only `index.json` and
`champions/<Champion>.json` are accepted. The closed recursive schema checks types,
finite counts, wins, provenance, coverage, known dimensions and reliability thresholds.
Unknown fields, unexpected paths, symlinks, raw records and secret-shaped strings fail
before any upload. New schema fields require code review. Research reports, SQLite,
player/match IDs and credentials are not accepted artifacts.

1. Read and validate the complete local release in memory.
2. Upload immutable gzip files under `releases/<timestamp-uuid>/` with create-only writes
   and four bounded workers. The manifest declares the encoding; older uncompressed
   releases remain readable. Delivery size is checked before any upload (4 MB compressed).
3. Read every file back, decompress it and check its uncompressed SHA-256 digest.
4. Write and verify an immutable release manifest.
5. Replace `manifest.json` with an ETag-conditional write (or create-only on first use).
   It contains both the current and previous release descriptors. A conflict fails.

Partial uploads are unreachable through the active pointer. Old versions are retained;
there is no automatic deletion. The browser fetches the pointer once per page session,
pins that version, then downloads champion files on demand. Every live file is hash-
checked and schema-validated by the function before serving. If the live index fails,
the browser tries the previous release, then the deployed aggregate export. If a pinned
champion file fails, the page resets its data and caches before trying that fallback,
so it does not mix new champion buckets with old metadata. Reload the page to discover
a newer release. An already open page does not replan a route during a match.

`/api/live-data` is read-only. The existing wildcard password edge gate stays enabled;
the function independently checks `SITE_PASSWORD`, including direct function URLs.
Responses use `private, no-store`. With a missing password the function fails closed.
The bundled export remains available behind the same password gate during store outages.

References: [XGBoost GPU support](https://xgboost.readthedocs.io/en/stable/gpu/),
[Netlify Blobs](https://docs.netlify.com/build/data-and-storage/netlify-blobs/).
