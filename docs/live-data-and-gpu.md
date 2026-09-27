# GPU experiments and aggregate refreshes

The WPA model trains locally on the GPU; visitors only ever receive the resulting aggregates, published
as a live-data release. AUC measures outcome prediction, not item impact.

## Local commands

Install `.venv/Scripts/python.exe -m pip install -r requirements-gpu.txt` once on Windows.
The npm Python launcher now prefers the project virtual environment; `PYTHON` overrides it.

- `npm run train:gpu`: WPA training forced onto CUDA. `pipeline/wpa.py` defaults to `--backend auto`:
  the GPU when a real CUDA fit succeeds, otherwise the same XGBoost model on CPU.
- `npm run data:refresh`: train (auto), export, build, validate and publish in order. It does not rerun
  `pipeline/lane.py`.
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

## Model backend

`pipeline/backends.py` uses XGBoost histogram trees with `device=cuda:0`. After every fit it checks the
trained booster configuration and fails if CUDA silently fell back to CPU; `auto` falls back to the same
model on CPU only when a real one-round CUDA fit fails. A forward benchmark on 2026-09-24 (three later
time windows, identical rows and features) found boosted trees tied on held-out quality (AUC ~0.78) and
the GPU fastest at the production size (~2.6 s per fold versus CPU time doubling as the window grew),
while below ~100k rows GPU overhead exceeds the fit. So:

| Stage | Hardware |
|---|---|
| JSON parse, game-state reconstruction, export records | all CPU cores but two, cached per match |
| WPA outcome model (cross-fitted folds, ~25M snapshots) | GPU (`--backend auto`) |
| Platt calibration, metrics, split-half and placebo gates | CPU, numpy |
| Lane model | CPU |

The benchmark script was removed after the decision; the WPA backend keeps its match-hash folds,
separate calibration and pre-purchase features.

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
checked and schema-validated by the function the first time a warm instance serves it; release keys
are create-only, so later requests on that instance send the stored gzip bytes unchanged (no
recompression). If the live index fails,
the browser tries the previous release, then the deployed aggregate export. If a pinned
champion file fails, the page resets its data and caches before trying that fallback,
so it does not mix new champion buckets with old metadata. Reload the page to discover
a newer release. An already open page does not replan a route during a match.

`/api/live-data` is read-only. The existing wildcard password edge gate stays enabled;
the function independently checks `SITE_PASSWORD`, including direct function URLs.
The pointer uses `private, no-store`; versioned release files use `private, max-age=31536000,
immutable`, so a browser downloads each champion file once per release. With a missing password the function fails closed.
The bundled export remains available behind the same password gate during store outages.

References: [XGBoost GPU support](https://xgboost.readthedocs.io/en/stable/gpu/),
[Netlify Blobs](https://docs.netlify.com/build/data-and-storage/netlify-blobs/).
