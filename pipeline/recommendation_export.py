"""Public research-preview aggregate of the saved experimental recommender. Loads models; never fits one.

    python pipeline/recommendation_export.py --model-dir data/research/recommender/full-dev-v0 \\
        --branches data/branch-comparison.sqlite --out data/public/recommendations.json

For every accepted champion/role/stage pair of the held-out fold, the saved win and 5-minute auxiliary models
predict both arms (route A, route B) over that pair's pre-purchase contexts from the fold's 'train' rows: the rows
the models were fitted on, read with context and action columns only. No outcome column, no 'evaluate' row and no
'train_focus' row is read. The output holds means of predictions and training support counts only: no match,
player, participant or pair ids, no per-row values and no invented uncertainty.

Every entry is a research preview with no confirmed advantage. The recommendation stays the observed baseline
(route A, the pair's most-completed route in train games); a model lean is a prediction, not a validated choice.
Scope is the first distinguishing component purchase (or the boots upgrade), never a full route. The exporter
refuses a report whose policy verdict claims a supported improvement: that needs a prospective test and its own
release path, not this preview.
"""
import argparse
import hashlib
import json
import math
import re
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from decisions import KEYS, OUTCOME
from engine import ROOT
from recommender import (ACTION_NAMES, ACTION_SOURCES, AUX, CONTEXT_NAMES, CONTEXT_SOURCES, REPORT_VERSION, STAGES,
                         Encoder, parse)

SCHEMA_VERSION = 1
FOLD = "heldout"
# Only these columns are selected from branch_comparison. match_id counts distinct games per pair and never leaves.
COLUMNS = tuple(dict.fromkeys(CONTEXT_SOURCES + ACTION_SOURCES + ("pair_id", "scope", "match_id")))
assert not set(COLUMNS) & set(OUTCOME), "an outcome column would be read"
# Public name, saved model, decimals.
PREDICTIONS = (("finalWin", "win", 4), ("goldLeadChange5", "aux_gold_lead_change_5", 0),
               ("takedowns5", "aux_takedowns_5", 2), ("deaths5", "aux_deaths_5", 2),
               ("championDamage5", "aux_champion_damage_5", 0), ("timeAlive5s", "aux_time_alive_s_5", 1))
assert {m for _, m, _ in PREDICTIONS} == {"win"} | {f"aux_{a}" for a in AUX}
OUTCOME_LABELS = {
    "finalWin": "Model-expected final win probability",
    "goldLeadChange5": "Model-expected team gold-lead change over the next 5 minutes",
    "takedowns5": "Model-expected takedowns over the next 5 minutes",
    "deaths5": "Model-expected deaths over the next 5 minutes",
    "championDamage5": "Model-expected champion damage over the next 5 minutes",
    "timeAlive5s": "Model-expected seconds alive over the next 5 minutes",
}
SCOPES = {"first_distinguishing_component": "First distinguishing component purchase toward route A or B, not a completed item",
          "boots_upgrade_purchase": "Boots upgrade purchase, conditional on buying an upgrade"}
CAVEATS = [
    "Research preview: model predictions, not measured results. No confirmed advantage for either route.",
    "The recommendation stays the observed most-common route (route A); the policy found no supported gain.",
    "Pooled across all matchups, regions and players in the training games; not specific to one matchup.",
    "Averages of predictions over the training contexts where the choice was observed; not a causal effect.",
    "Scope is a single purchase decision. No full-route or completed-item claim.",
    "No uncertainty interval is available for these averages; support counts are training rows, not precision.",
]
FORBIDDEN_KEYS = {"match_id", "matchId", "player_ref", "playerRef", "puuid", "participant_id", "participantId",
                  "pair_id", "pairId", "team_id", "summonerId", "gameName", "riotId"} | set(KEYS)
MATCH_ID = re.compile(r"^[A-Z]{2,4}\d?_\d+$")


def artifact_identity(model_dir):
    """sha256 over manifest.json and every saved model file, in name order."""
    model_dir = Path(model_dir)
    h = hashlib.sha256()
    for path in [model_dir / "manifest.json", *sorted((model_dir / "models").glob("*.json"))]:
        h.update(path.relative_to(model_dir).as_posix().encode() + b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()


def load_models(model_dir):
    """(manifest, report, Encoder with the saved vocabulary, {model name: loaded XGBoost model})."""
    from xgboost import XGBClassifier, XGBRegressor
    model_dir = Path(model_dir)
    manifest = json.loads((model_dir / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads((model_dir / "report.json").read_text(encoding="utf-8"))
    if manifest.get("version") != REPORT_VERSION or report.get("version") != REPORT_VERSION:
        raise ValueError(f"model version {manifest.get('version')!r} is not {REPORT_VERSION!r}")
    if manifest["features"]["context"] != CONTEXT_NAMES or manifest["features"]["action"] != ACTION_NAMES:
        raise ValueError("saved feature layout differs from this code; the models cannot be scored here")
    if report["input"]["fold"] != FOLD:
        raise ValueError(f"report fold {report['input']['fold']!r}: only the held-out fit is exported")
    models = {}
    for _, name, _ in PREDICTIONS:
        path = model_dir / "models" / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"{path}: model was not fitted; nothing to export")
        model = XGBClassifier() if name == "win" else XGBRegressor()
        model.load_model(str(path))
        model.set_params(device="cpu")
        models[name] = model
    encoder = Encoder.__new__(Encoder)
    encoder.vocab = manifest["vocab"]
    return manifest, report, encoder, models


def load_train(path):
    """(held-out fold 'train' rows with context/action columns only, accepted pairs)."""
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    cur = db.execute(f"SELECT {', '.join(COLUMNS)} FROM branch_comparison WHERE fold=? AND split_role='train'", (FOLD,))
    rows = [parse(dict(zip(COLUMNS, r))) for r in cur]
    db.row_factory = sqlite3.Row
    pairs = [dict(r) for r in db.execute("SELECT * FROM branch_pairs WHERE fold=? AND status='accepted'", (FOLD,))]
    db.close()
    return rows, pairs


def predict(encoder, models, rows):
    """(2, n, len(PREDICTIONS)): each arm with the context held fixed."""
    ctx, n = encoder.contexts(rows), len(rows)
    out = np.zeros((2, n, len(PREDICTIONS)))
    for arm in (0, 1):
        X = np.hstack([ctx, encoder.actions(rows, [arm] * n)])
        for k, (_, name, _) in enumerate(PREDICTIONS):
            m = models[name]
            out[arm, :, k] = m.predict_proba(X)[:, 1] if name == "win" else m.predict(X)
    return out


def arm_means(pred, idx):
    """[route A, route B], each a list in PREDICTIONS order."""
    rounded = lambda v, digits: round(v, digits) if digits else int(round(v))
    return [[rounded(float(pred[arm, idx, k].mean()), digits) for k, (_, _, digits) in enumerate(PREDICTIONS)]
            for arm in (0, 1)]


def pair_key(p):
    return f"{FOLD}|{p['champion']}|{p['role']}|{p['stage']}|{p['patch']}|{p['route_a']}|{p['route_b']}"


def check_report(report, rows, support):
    """The rows must be exactly the ones the saved models were fitted on, and nothing may claim a gain."""
    if report["fit"]["rows"] != len(rows):
        raise ValueError(f"{len(rows)} train rows, but the models were fitted on {report['fit']['rows']}")
    for s in report["support"]:
        if (support.get((s["pair_id"], 0), 0), support.get((s["pair_id"], 1), 0)) != (s["fit_rows_a"], s["fit_rows_b"]):
            raise ValueError(f"training support differs from the saved report for {s['pair_id']}")
    verdicts = {k: p.get("verdict", p.get("status")) for k, p in report["evaluation"]["policies"].items()}
    if report.get("headline_claims_allowed") or "supported_improvement" in verdicts.values():
        raise ValueError("the report claims a supported improvement; this preview exporter does not publish claims")
    return verdicts


def build(model_dir, branches, now=None):
    manifest, report, encoder, models = load_models(model_dir)
    rows, pairs = load_train(branches)
    support = Counter((r["pair_id"], r["arm"]) for r in rows)
    verdicts = check_report(report, rows, support)
    by_pair = defaultdict(list)
    for i, r in enumerate(rows):
        by_pair[r["pair_id"]].append(i)
    pred = predict(encoder, models, rows)
    min_arm = manifest["gate"]["min_arm_train_rows"]
    entries = []
    for p in sorted(pairs, key=lambda p: (p["champion"], p["role"], STAGES.index(p["stage"]))):
        key, idx = pair_key(p), by_pair.get(pair_key(p), [])
        n_a, n_b = support.get((key, 0), 0), support.get((key, 1), 0)
        supported = min(n_a, n_b) >= min_arm and key in encoder.vocab["pair_id"]
        entry = dict(champion=p["champion"], role=p["role"], stage=p["stage"], scope=p["scope"],
                     patch=p["patch"], baselineRoute=str(p["route_a"]), alternativeRoute=str(p["route_b"]),
                     support=dict(contexts=len(idx), routeA=n_a, routeB=n_b,
                                  matches=len({rows[i]["match_id"] for i in idx})),
                     status="research_preview", supported=bool(supported), predicted=None, modelLean=None)
        if supported:
            a, b = arm_means(pred, np.asarray(idx))
            entry["predicted"] = dict(routeA=a, routeB=b)
            entry["modelLean"] = "none" if a[0] == b[0] else "routeA" if a[0] > b[0] else "routeB"
        entries.append(entry)
    catalogs = report["input"]["branch_meta"].get("catalog_versions", {})
    doc = dict(
        schemaVersion=SCHEMA_VERSION, kind="item_model_research_preview",
        generatedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time())),
        status="research_preview", claimStatus="no_confirmed_advantage", recommendation="observed_baseline_route_a",
        routeLevelClaim=False, pooling="all_matchups_regions_players",
        sourcePatches=sorted(manifest["vocab"]["patch"]), catalogVersions={k: catalogs[k] for k in sorted(catalogs)},
        regions=sorted(manifest["vocab"]["region"]),
        model=dict(version=manifest["version"], artifactSha256=artifact_identity(model_dir),
                   trainedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(report["created_at"])),
                   fitRows=report["fit"]["rows"], fitMatches=report["fit"]["matches"],
                   evaluationRows=report["evaluation"]["rows"], evaluationMatches=report["evaluation"]["matches"],
                   policyVerdicts=verdicts, headlineClaimsAllowed=False, minArmTrainRows=min_arm,
                   preferenceMargin=manifest["gate"]["preference_margin"]),
        scopes=SCOPES, predictionFields=[public for public, _, _ in PREDICTIONS], outcomes=OUTCOME_LABELS, caveats=CAVEATS, entries=entries)
    check_public(doc)
    return doc


def check_public(doc):
    """Refuse identifiers, per-row data and claim fields anywhere in the document."""
    def walk(v, path):
        if isinstance(v, dict):
            for k, x in v.items():
                if k in FORBIDDEN_KEYS:
                    raise ValueError(f"private field {path}.{k}")
                walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
        elif isinstance(v, str) and (MATCH_ID.match(v) or "|" in v):
            raise ValueError(f"identifier-like string at {path}")
        elif isinstance(v, float) and not math.isfinite(v):
            raise ValueError(f"non-finite number at {path}")
    walk(doc, "root")
    if doc["claimStatus"] != "no_confirmed_advantage" or doc["routeLevelClaim"] is not False:
        raise ValueError("a research preview cannot carry a claim")
    return doc


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-dir", default=str(ROOT / "data" / "research" / "recommender" / "full-dev-v0"))
    parser.add_argument("--branches", default=str(ROOT / "data" / "branch-comparison.sqlite"))
    parser.add_argument("--out", default=str(ROOT / "data" / "public" / "recommendations.json"))
    args = parser.parse_args()
    doc = build(args.model_dir, args.branches)
    text = json.dumps(doc, separators=(",", ":"))
    Path(args.out).write_text(text, encoding="utf-8")
    shown = sum(e["supported"] for e in doc["entries"])
    print(f"{len(doc['entries'])} pairs ({shown} with predictions), {len(text):,} bytes -> {args.out}")


if __name__ == "__main__":
    main()
