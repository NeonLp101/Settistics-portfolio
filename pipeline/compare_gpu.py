"""Private, frozen forward benchmark of outcome prediction; never item-effect validation.

Two populations, identical protocol:
  minute10  one state strictly before minute 10 per participant in games lasting >10 minutes
  all       every WPA training moment (2-minute snapshots, matches with an item catalog):
            the real training workload, so hardware timing reflects production scale
All models share features, rows, chronological match splits and earlier-only Platt calibration.
No model selection or hyperparameter tuning on the held-out windows.
"""
import argparse
import hashlib
from pathlib import Path
import time
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits
from backends import cuda_available, outcome_model, verify_device
from engine import ROOT, utc, write_json
from wpa import FEATURES, calibration

KINDS = ('logistic', 'trees', 'cpu', 'xgb-cpu', 'xgb-cuda')


def populations(matches):
    """Column arrays for both benchmark populations from cached per-match features."""
    out = {name: {k: [] for k in ('X', 'win', 'match', 'player', 'startedAt', 'region', 'patch')} for name in ('minute10', 'all')}
    for mid, a in matches.items():
        meta = a['meta']
        for name, rows in (('minute10', np.flatnonzero(a['snap_X'][:, 0] == 10.0)),
                           ('all', np.arange(len(a['snap_X'])) if meta['hasCatalog'] else np.arange(0))):
            p = a['snap_p'][rows]
            o = out[name]
            o['X'].append(a['snap_X'][rows]); o['win'].append(a['player_win'][p]); o['player'].append(a['player_ref'][p])
            for key, value in (('match', mid), ('startedAt', meta['startedAt']), ('region', meta['region']), ('patch', meta['patch'])):
                o[key].append(np.full(len(rows), value))
    return {name: {k: np.concatenate(v) for k, v in o.items()} for name, o in out.items()}


def splits(rows):
    """Row-dict form of forward_windows (kept for tests)."""
    return forward_windows(np.array([r['startedAt'] for r in rows]))


def forward_windows(ts):
    times = np.unique(ts)
    if len(times) < 200:
        raise ValueError('At least 200 match start times required')
    for end in (.6, .8, 1.):
        test_start = times[int(len(times)*(end-.2))]
        cal_start = times[int(len(times)*(end-.3))]
        test_end = times[int(len(times)*end)] if end < 1 else times[-1]+1
        yield ts < cal_start, (ts >= cal_start) & (ts < test_start), (ts >= test_start) & (ts < test_end)


def learner(kind, seed):
    if kind == 'logistic':
        return make_pipeline(StandardScaler(), LogisticRegression(C=.3, max_iter=1000, random_state=42))
    if kind == 'trees':
        return ExtraTreesClassifier(n_estimators=100, min_samples_leaf=25, max_depth=12, n_jobs=8, random_state=42)
    return outcome_model(kind, seed)


def run(data, kinds=KINDS):
    X, y, ts = data['X'].astype(float), data['win'].astype(int), data['startedAt']
    plan = list(forward_windows(ts))
    digest = hashlib.sha256('\n'.join(f'{m}|{p}|{t}' for m, p, t in zip(data['match'], data['player'], ts)).encode()).hexdigest()
    z = lambda p: np.log(np.clip(p, 1e-4, 1-1e-4)/(1-np.clip(p, 1e-4, 1-1e-4))).reshape(-1, 1)
    reports = []
    for kind in kinds:
        folds = []
        for seed, (fit, cal, test) in enumerate(plan):
            model = learner(kind, seed)
            start = time.perf_counter()
            with threadpool_limits(limits=8):
                model.fit(X[fit], y[fit]); verify_device(model, kind)
                fit_seconds = time.perf_counter()-start
                platt = LogisticRegression().fit(z(model.predict_proba(X[cal])[:, 1]), y[cal])
                pred = platt.predict_proba(z(model.predict_proba(X[test])[:, 1]))[:, 1]
            seen = set(data['player'][fit | cal])
            cold = np.array([bool(p) and p not in seen for p in data['player'][test]])
            yt = y[test]
            fold = dict(fitSeconds=round(fit_seconds, 3), totalSeconds=round(time.perf_counter()-start, 3),
                        fitRows=int(fit.sum()), fitMatches=len(set(data['match'][fit])),
                        calibrationMatches=len(set(data['match'][cal])), testMatches=len(set(data['match'][test])),
                        fitBefore=int(ts[cal].min()), testFrom=int(ts[test].min()), testThrough=int(ts[test].max()),
                        metrics=calibration(yt, pred), coldPlayerRows=int(cold.sum()),
                        coldPlayerMetrics=calibration(yt[cold], pred[cold]) if cold.sum() > 20 and len(set(yt[cold])) == 2 else None,
                        patches=sorted(set(data['patch'][test])), regions=sorted(set(data['region'][test])))
            folds.append(fold)
            print(f"  {kind} window {seed+1}: AUC {fold['metrics']['auc']}, {fold['totalSeconds']} s", flush=True)
        reports.append(dict(backend=kind, folds=folds))
    return dict(splitFingerprint=digest, matches=len(set(data['match'])), rows=len(y),
                trainingCutoff=int(ts.max()), models=reports)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(ROOT/'data'/'settistics.sqlite'))
    parser.add_argument('--out', default=str(ROOT/'data'/'research'/'gpu-comparison.json'))
    parser.add_argument('--workers', type=int)
    args = parser.parse_args()
    from features import load_matches
    kinds = KINDS if cuda_available() else tuple(k for k in KINDS if k != 'xgb-cuda')
    print('Reading cached features without changing collector data...', flush=True)
    data = populations(load_matches(args.db, workers=args.workers))
    report = dict(schemaVersion=2, generatedAt=utc(), status='research_only', causalValidationPassed=False,
        publishableRecommendations=False, features=FEATURES, populations={},
        limitations=['Outcome prediction is not item impact or route policy evaluation.',
          'Repeated players occur across time; cold-player results are a separate audit, not a causal claim.',
          'No player identity, outcome-derived draft encoding, final inventory or future state features.',
          'Fixed parameters; same-learner CPU/CUDA isolates hardware. Other families are quality baselines.',
          'The all-moment population has many correlated rows per game; its AUC is not a per-game accuracy.',
          'Timing includes calibration and prediction; shared data preparation excluded. Single run, not a speed guarantee.'])
    for name, description in (('minute10', 'Participants in games lasting >10 minutes, state strictly before minute 10'),
                              ('all', 'Every 2-minute WPA training moment in matches with an item catalog')):
        print(f'{name}: {len(data[name]["win"]):,} rows', flush=True)
        report['populations'][name] = dict(population=description, **run(data[name], kinds))
    write_json(Path(args.out), report)
    print(f"Saved private comparison: {args.out}")


if __name__ == '__main__':
    main()
