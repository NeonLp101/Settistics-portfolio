"""Experimental forward-held-out doubly robust starting-package comparison.

Never publishes WPA to the website. Compare cheap regularized logistic regression
with a nonlinear tree baseline before paying for a more complex learner.
"""
import argparse
from collections import Counter
import math
from pathlib import Path
import time
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.pipeline import make_pipeline
from engine import ROOT, connect, dataset, utc, write_json

def learner(kind):
    model = (LogisticRegression(C=.3, max_iter=1000, solver="liblinear",random_state=42)
             if kind == "logistic" else ExtraTreesClassifier(n_estimators=100,min_samples_leaf=25,max_depth=12,n_jobs=-1,random_state=42))
    return make_pipeline(DictVectorizer(sparse=True),model)

def feature(row, treatment=None):
    x = dict(row["features"])
    # Explicit allowlist: outcomes, final inventory, game duration and future stats cannot enter.
    if treatment is not None:
        x["packageTreatment"] = str(treatment)
        x["packageByOpponent"] = str(treatment)+":"+row["opponent"]
    return x

def cluster_interval(values, clusters):
    values = np.asarray(values,dtype=float)
    groups = {}
    mean = float(values.mean())
    for v,g in zip(values,clusters):
        groups[g] = groups.get(g,0) + v-mean
    k,n = len(groups),len(values)
    if k < 2:
        return None
    se = math.sqrt(k/(k-1) * sum(v*v for v in groups.values()))/n
    return [mean-1.96*se,mean+1.96*se]

def ess(weights):
    return float(weights.sum()**2 / (weights@weights)) if len(weights) and weights@weights else 0

def evaluate(records,a,b,kind):
    rows = sorted([r for r in records if r["package"] in (a,b)],key=lambda r:(r["startedAt"],r["matchId"]))
    # Keep one focal player per match; mirror matches otherwise violate simple outcome independence.
    counts = Counter(r["matchId"] for r in rows)
    rows = [r for r in rows if counts[r["matchId"]] == 1]
    if len(rows)<400:
        return {"model":kind,"status":"insufficient_data","games":len(rows),"minimum":400}
    # Expanding-window cross-fitting: each prediction uses only earlier games.
    boundaries = [int(len(rows)*f) for f in (.4,.6,.8,1)]
    results, folds = [], []
    started = time.perf_counter()
    for i in range(3):
        test = rows[boundaries[i]:boundaries[i+1]]
        cutoff = test[0]["startedAt"]
        train = [r for r in rows[:boundaries[i]] if r["startedAt"]<cutoff]
        t = np.array([int(r["package"]==a) for r in train])
        y = np.array([r["win"] for r in train])
        if min(Counter(t).values(),default=0)<50 or len(set(t))<2 or len(set(y))<2:
            folds.append({"status":"insufficient_training_overlap"})
            continue
        outcome,propensity = learner(kind),learner("logistic")
        outcome.fit([feature(r,int(v)) for r,v in zip(train,t)],y)
        propensity.fit([feature(r) for r in train],t)
        m1 = outcome.predict_proba([feature(r,1) for r in test])[:,1]
        m0 = outcome.predict_proba([feature(r,0) for r in test])[:,1]
        e = propensity.predict_proba([feature(r) for r in test])[:,1]
        for r,p1,p0,p in zip(test,m1,m0,e):
            results.append({"row":r,"m1":p1,"m0":p0,"e":p,"t":int(r["package"]==a),"baseRate":float(y.mean())})
        folds.append({"status":"ok","train":len(train),"test":len(test),"cutoff":cutoff})
    if not results:
        return {"model":kind,"status":"insufficient_data","folds":folds}
    y = np.array([o["row"]["win"] for o in results])
    observed = np.array([o["m1"] if o["t"] else o["m0"] for o in results])
    overlap = [o for o in results if .1 <= o["e"] <= .9]
    report = {"model":kind,"status":"research_only","seconds":round(time.perf_counter()-started,3),
              "folds":folds,"evaluatedGames":len(results),"overlapGames":len(overlap),"overlapFraction":len(overlap)/len(results),
              "heldOutBrier":float(brier_score_loss(y,observed)),"heldOutLogLoss":float(log_loss(y,observed,labels=[0,1])),
              "heldOutBaseRateBrier":float(brier_score_loss(y,[o['baseRate'] for o in results])),
              "calibrationGap":float(observed.mean()-y.mean()),"causalValidationPassed":False}
    if not overlap:
        report["status"] = "no_overlap"
        return report
    scores,clusters,weights1,weights0 = [],[],[],[]
    for o in overlap:
        t,y,e = o["t"],o["row"]["win"],o["e"]
        scores.append(o["m1"]-o["m0"] + t*(y-o["m1"])/e - (1-t)*(y-o["m0"])/(1-e))
        clusters.append(o["row"]["playerId"] or o["row"]["matchId"])
        if t:
            weights1.append(1/e)
        else:
            weights0.append(1/(1-e))
    interval = cluster_interval(scores,clusters)
    report.update(estimatedDifferencePP=float(np.mean(scores)*100),
                  conditionalApprox95IntervalPP=[v*100 for v in interval] if interval else None,
                  effectiveSampleA=ess(np.array(weights1)),effectiveSampleB=ess(np.array(weights0)),
                  playerClusters=len(set(clusters)))
    report["basicSupportPassed"] = min(report["effectiveSampleA"],report["effectiveSampleB"])>=100 and report["playerClusters"]>=30 and report["overlapFraction"]>=.8
    return report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",default=str(ROOT/"data"/"settistics.sqlite"))
    parser.add_argument("--champion",default="Sett")
    parser.add_argument("--role",default="TOP")
    parser.add_argument("--patch",required=True,help="Do not pool changed item balance silently")
    parser.add_argument("--opponent",help="Optional: specific matchup (otherwise pooled matchups)")
    parser.add_argument("--a",help="Exact package ID from --list; preregister the contrast")
    parser.add_argument("--b",help="Exact comparator package ID")
    parser.add_argument("--list",action="store_true")
    args = parser.parse_args()
    db = connect(args.db)
    rows = [r for r in dataset(db,args.champion) if r["role"]==args.role and r["patch"]==args.patch and r["package"] and (not args.opponent or r["opponent"]==args.opponent)]
    db.close()
    if args.list:
        labels = {r["package"]:r["packageLabel"] for r in rows}
        print(json.dumps([{"id":p,"label":labels[p],"games":n} for p,n in Counter(r["package"] for r in rows).most_common()],indent=2))
        return
    if not args.a or not args.b or args.a == args.b:
        parser.error("Choose two distinct package IDs with --a and --b (see --list)")
    report = {"createdAt":utc(),"status":"research_only","publishableWPA":False,
              "contrast":{"a":args.a,"b":args.b,"champion":args.champion,"role":args.role,"patch":args.patch,"opponent":args.opponent},
              "estimand":"A minus B in the observed A/B chooser population with propensity 0.1..0.9; not all players or all situations",
              "limitations":["No historical player-skill adjustment yet; unmeasured confounding likely",
                 "Pre-60-second purchased packages are an early-game proxy, not a randomized pregame treatment",
                 "Intervals are player-clustered approximations conditional on fitted nuisance models; not full-pipeline uncertainty",
                 "Same players may recur across time; no cold-player generalization claim",
                 "Overlap trimming changes the target population; policy value and sequential builds are not estimated",
                 "Calibration and support checks do not validate a causal effect; report is never exported to the website"],
              "models":[evaluate(rows,args.a,args.b,kind) for kind in ("logistic","trees")]}
    out = ROOT/"data"/"research"/"comparison.json"
    write_json(out,report)
    print(json.dumps(report,indent=2))
    print(f"Private research report: {out}")

if __name__ == "__main__":
    main()
