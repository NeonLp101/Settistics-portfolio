"""Export the frozen r3 fit as an aggregate, training-only website research preview.

Only pre-purchase contexts and action columns are read from the frozen branch DB.
The sealed future-start cohort is never opened. This exports predictions, not a
validated policy or a replacement for the observed build recommendation.
"""
import argparse
import hashlib
import json
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from xgboost import XGBClassifier, XGBRegressor

from engine import ROOT
from recommender import ACTION_NAMES, CONTEXT_NAMES, Encoder, STAGES, parse
from recommendation_export import (CAVEATS, COLUMNS, FOLD, OUTCOME_LABELS,
                                   PREDICTIONS, SCOPES, artifact_identity,
                                   check_public)

VERSION = "recommender-experimental-r3-fulltrain"
CUTOFF = "2026-09-26T05:27:00Z"
CUTOFF_MS = 1790400420000
BATCH = 10000
# Below this, an arm-swap difference in a scored win probability is floating noise, not a real leaf change.
DISTINGUISHABLE_EPS = 1e-9


def load(model_dir):
    model_dir = Path(model_dir)
    manifest = json.loads((model_dir / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads((model_dir / "report.json").read_text(encoding="utf-8"))
    if (manifest.get("version"), report.get("version")) != (VERSION, VERSION):
        raise ValueError("not the frozen r3 model")
    if (manifest.get("cutoff_utc"), report.get("cutoff_utc")) != (CUTOFF, CUTOFF):
        raise ValueError("r3 cutoff differs")
    if not report.get("training_only") or report.get("evaluation_games") != 0:
        raise ValueError("r3 preview must have no post-cutoff evaluation")
    if manifest["features"]["context"] != CONTEXT_NAMES or manifest["features"]["action"] != ACTION_NAMES:
        raise ValueError("saved model feature layout differs")
    models = {}
    for _, name, _ in PREDICTIONS:
        path = model_dir / "models" / f"{name}.json"
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["model_hashes"][name]:
            raise ValueError(f"saved model hash differs: {name}")
        model = XGBClassifier() if name == "win" else XGBRegressor()
        model.load_model(str(path))
        model.set_params(device="cpu", n_jobs=4)
        models[name] = model
    encoder = Encoder.__new__(Encoder)
    encoder.vocab = manifest["vocab"]
    return manifest, report, encoder, models


def build(model_dir, branches_path):
    manifest, report, encoder, models = load(model_dir)
    db = sqlite3.connect(Path(branches_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    db.row_factory = sqlite3.Row
    pairs = [dict(r) for r in db.execute("SELECT * FROM branch_pairs WHERE fold=? AND status='accepted'", (FOLD,))]
    if len(pairs) != report["accepted_pairs"]:
        raise ValueError("accepted pair count differs from frozen training report")
    fields = ", ".join(COLUMNS + ("started_at", "focus_source"))
    cur = db.execute(f"SELECT {fields} FROM branch_comparison WHERE fold=? AND split_role='train'", (FOLD,))
    support, matches = Counter(), defaultdict(set)
    sums = defaultdict(lambda: np.zeros((2, len(PREDICTIONS))))
    # Per pair, the largest |win(arm0) - win(arm1)| seen on any single training row. Deterministic
    # scoring: if this stays at 0, swapping the item choice never sent a single row down a
    # different leaf in the win model, for this population -- the model cannot lean either way,
    # regardless of what the rounded average of an unmoved prediction happens to show.
    max_win_diff = defaultdict(float)
    contexts, seen = Counter(), 0
    while True:
        batch = cur.fetchmany(BATCH)
        if not batch:
            break
        rows = [parse(dict(r)) for r in batch]
        if any(r["started_at"] >= CUTOFF_MS or r["focus_source"] for r in rows):
            raise ValueError("future-start or focus row in frozen r3 training branches")
        ctx = encoder.contexts(rows)
        arm_scores = []
        for arm in (0, 1):
            x = np.hstack([ctx, encoder.actions(rows, [arm] * len(rows))])
            scores = np.column_stack([models[name].predict_proba(x)[:, 1] if name == "win" else models[name].predict(x)
                                      for _, name, _ in PREDICTIONS])
            arm_scores.append(scores)
            for i, r in enumerate(rows):
                sums[r["pair_id"]][arm] += scores[i]
        win_diff = np.abs(arm_scores[0][:, 0] - arm_scores[1][:, 0])
        for i, r in enumerate(rows):
            key = r["pair_id"]
            if win_diff[i] > max_win_diff[key]:
                max_win_diff[key] = float(win_diff[i])
        for r in rows:
            key = r["pair_id"]
            contexts[key] += 1
            support[(key, r["arm"])] += 1
            matches[key].add(r["match_id"])
        seen += len(rows)
        if seen % 200000 < len(rows):
            print(f"scored {seen:,} of {report['fit']['rows']:,} frozen training rows", flush=True)
    db.close()
    if seen != report["fit"]["rows"]:
        raise ValueError("branch row count differs from saved fit")
    entries = []
    for p in sorted(pairs, key=lambda p: (p["champion"], p["role"], STAGES.index(p["stage"]))):
        key = f"{FOLD}|{p['champion']}|{p['role']}|{p['stage']}|{p['patch']}|{p['route_a']}|{p['route_b']}"
        n = contexts[key]
        a, b = support[(key, 0)], support[(key, 1)]
        supported = min(a, b) >= manifest["gate"]["min_arm_train_rows"] and key in encoder.vocab["pair_id"]
        predicted, lean = None, None
        if supported:
            means = sums[key] / n
            predicted = {}
            for arm, label in enumerate(("routeA", "routeB")):
                predicted[label] = [round(float(means[arm, i]), digits) if digits else int(round(float(means[arm, i])))
                                    for i, (_, _, digits) in enumerate(PREDICTIONS)]
            wa, wb = predicted["routeA"][0], predicted["routeB"][0]
            if max_win_diff[key] <= DISTINGUISHABLE_EPS:
                lean = "indistinguishable"
            else:
                lean = "none" if wa == wb else "routeA" if wa > wb else "routeB"
        entries.append(dict(champion=p["champion"], role=p["role"], stage=p["stage"], scope=p["scope"],
                            patch=p["patch"], baselineRoute=str(p["route_a"]), alternativeRoute=str(p["route_b"]),
                            support=dict(contexts=n, routeA=a, routeB=b, matches=len(matches[key])),
                            status="research_preview", supported=bool(supported), predicted=predicted, modelLean=lean))
    caveats = [c for c in CAVEATS if "policy found no supported gain" not in c]
    caveats.insert(1, "This retrained model has not yet been tested on the newly sealed future games.")
    doc = dict(schemaVersion=1, kind="item_model_research_preview",
               generatedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               status="research_preview", claimStatus="no_confirmed_advantage",
               recommendation="observed_baseline_route_a", routeLevelClaim=False,
               pooling="all_matchups_regions_players", sourcePatches=sorted(manifest["vocab"]["patch"]),
               catalogVersions=report["branch_meta"]["catalog_versions"],
               regions=sorted(x.upper() for x in manifest["vocab"]["region"]),
               model=dict(version=VERSION, artifactSha256=artifact_identity(model_dir),
                          trainedAt=datetime.fromisoformat(report["created_at"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          fitRows=report["fit"]["rows"], fitMatches=report["fit"]["matches"],
                          evaluationRows=0, evaluationMatches=0,
                          policyVerdicts=dict(base="not_fitted", enriched="not_fitted"),
                          headlineClaimsAllowed=False, minArmTrainRows=manifest["gate"]["min_arm_train_rows"],
                          preferenceMargin=manifest["gate"]["preference_margin"]),
               scopes=SCOPES, predictionFields=[x for x, _, _ in PREDICTIONS], outcomes=OUTCOME_LABELS,
               caveats=caveats, entries=entries)
    return check_public(doc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", default=str(ROOT / "data/research/prospective/r3/model"))
    parser.add_argument("--branches", default=str(ROOT / "data/fulltrain-r3-branches.sqlite"))
    parser.add_argument("--out", default=str(ROOT / "data/public/recommendations.json"))
    args = parser.parse_args()
    doc = build(args.model_dir, args.branches)
    payload = json.dumps(doc, separators=(",", ":"))
    Path(args.out).write_text(payload, encoding="utf-8")
    print(f"{len(doc['entries']):,} pairs, {len(payload):,} bytes -> {args.out}")


if __name__ == "__main__":
    main()
