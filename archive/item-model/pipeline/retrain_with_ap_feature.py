"""Fulltrain-style shared model refit, adding the enemy_ap_share context feature to the same unsealed
training population used for the frozen r3 model.

    python pipeline/retrain_with_ap_feature.py

Reads only 'train' rows from data/fulltrain-r3-branches.sqlite (fold 'heldout'), the same pre-cutoff,
non-focus population r3 was fit on -- now including enemy_ap_share (see engine.enemy_ap_share),
backfilled onto that file by pipeline/backfill_enemy_ap_share.py. Uses recommender.fit_models() exactly
as r3 did; there is no held-out split inside this file (dr_pairs.py's own checks fill that role for
research), so this mirrors r3's "training-only" shape rather than recommender.run()'s train+evaluate one.

This is a new experimental candidate model. It is saved to a private research directory and does not
touch, retrain or replace the frozen, sealed r3 model, and it is never exported to the site. Promotion to
a product number needs the same evidence gates as everything else in this project -- a prospective
evaluation on the sealed future-start cohort, not just being fit -- which has not happened.
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path

from backends import resolve
from engine import ROOT
from recommendation_export_r3 import CUTOFF_MS
from recommender import ACTION_NAMES, CONTEXT_NAMES, DECISION_XGB, MIN_CROSSFIT_ROWS, XGB, clean, fit_models, parse, private_dir

VERSION = "recommender-experimental-r4-enemy-ap"
FOLD = "heldout"


def load_train_rows(branches_path):
    db = sqlite3.connect(Path(branches_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    db.row_factory = sqlite3.Row
    rows = [parse(dict(r)) for r in db.execute("SELECT * FROM branch_comparison WHERE fold=? AND split_role='train'", (FOLD,))]
    db.close()
    if any(r["started_at"] >= CUTOFF_MS or r["focus_source"] for r in rows):
        raise ValueError("future-start or focus row in frozen r3 training branches")
    return rows


def build(branches_path, out_dir=None, backend="xgb-cpu", seed=0, params=None, decision_params=None,
         min_crossfit_rows=MIN_CROSSFIT_ROWS, log=print):
    backend = resolve(backend)
    rows = load_train_rows(branches_path)
    log(f"{len(rows):,} training rows from {len({r['match_id'] for r in rows}):,} matches", flush=True)
    fitted = fit_models(rows, backend=backend, params=params, decision_params=decision_params, seed=seed,
                        min_crossfit_rows=min_crossfit_rows)
    report = clean(dict(
        version=VERSION, created_at=int(time.time()), experimental=True, training_only=True, evaluation_games=0,
        input=dict(branches=str(branches_path), fold=FOLD),
        note="Adds enemy_ap_share (mean AP-class share of the 5 enemy champions, a rough proxy from Data "
             "Dragon class tags -- see engine.champion_ap_shares) to the shared context features, to test "
             "whether it explains away a boots comparison (Plated Steelcaps vs Mercury's Treads) flagged as "
             "possible enemy-composition confounding. See docs/model-release-status-2026-09-26.md.",
        headline_claims_allowed=False, backend=backend, seed=seed, xgb=params or XGB, decision_xgb=decision_params or DECISION_XGB,
        features=dict(context=CONTEXT_NAMES, action=ACTION_NAMES),
        fit=dict(rows=fitted["rows"], matches=fitted["matches"], aux_targets=fitted["aux_exclusions"],
                 models={k: m.status for k, m in fitted["models"].items()}, crossfit=fitted["crossfit"])))
    out = private_dir(out_dir or ROOT / "data" / "research" / "recommender" / f"r4-enemy-ap-{report['created_at']}")
    (out / "models").mkdir(parents=True, exist_ok=True)
    for name, m in fitted["models"].items():
        if m.model is not None:
            m.model.save_model(str(out / "models" / f"{name}.json"))
    manifest = clean(dict(version=VERSION, vocab=fitted["encoder"].vocab,
                          fallbacks={k: m.fallback for k, m in fitted["models"].items()},
                          features=report["features"], aux_used=fitted["crossfit"]["aux_used"]))
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    log(f"saved -> {out}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--branches", default=str(ROOT / "data" / "fulltrain-r3-branches.sqlite"))
    parser.add_argument("--out-dir")
    parser.add_argument("--backend", default="xgb-cpu", choices=("auto", "xgb-cpu", "xgb-cuda"))
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    report = build(args.branches, args.out_dir, args.backend, args.seed)
    print(f"models: {report['fit']['models']}")
    print(f"crossfit: {report['fit']['crossfit']['status']}, aux used {report['fit']['crossfit']['aux_used']}")


if __name__ == "__main__":
    main()
