"""Experimental shared-model item recommender over observable branch comparisons (development slice).

    python pipeline/recommender.py --branches data/branch-comparison.sqlite            # held-out fold (needs --include-heldout)
    python pipeline/recommender.py --branches data/branch-comparison-sample-s10.sqlite --fold 4   # exploratory smoke

Input: branch_comparison and branch_pairs from pipeline/branches.py. One fold only, never several folds stacked.
With --fold heldout the fold's 'train' rows fit every model, and its 'evaluate' rows (general-source games from
the later held-out development period) are used for the report only. This existing data has already been
inspected, so the report is exploratory and allows no headline claim. Other folds are smoke runs. 'train_focus'
rows enter only a separately reported sensitivity fit, never evaluation. Evaluation labels never reach fitting,
the candidate set, thresholds or calibration: the gate below is fixed before any data is read.

Decision features are pre-action context only (CONTEXT_SOURCES): the pre-purchase state, stage, timing, gold
snapshot, inventory counts and coded champion/opponent/region/patch/pair identity. No OUTCOME column, win,
game length, continuation diagnostic, later completion, match or player id enters a model. The arm (route A or
B of the pair) is an action feature, not context: every candidate prediction swaps the arm and holds the context
fixed. The decision is dated at the first distinguishing purchase (or the boots upgrade), so the observed timing
of that purchase is part of the held-fixed context.

Models (XGBoost, all champions pooled, one model per target):
- win: final result from context + arm; the outcome model of the doubly robust estimate.
- auxiliary (t, min(t+5 min, game end)]: team gold-lead change, takedowns, deaths, observed champion damage
  over the frame-covered interval, time alive. Partial windows remain in training with their observed outcomes;
  damage needs its frame counters; time alive needs the row's
  dead-time check within DEAD_ERROR_MAX_S. Zero-fight windows are zeros, never dropped.
- propensity: P(route B | context, pair identity); the arm is its label, never its input.
- decision base / enriched: regularized final-win models; enriched adds auxiliary predictions for the candidate.
  Its training rows get auxiliary predictions from one-way temporal cross-fitting: fit games are cut into
  chronological match blocks, and each block is scored by auxiliary models fitted only on games that ended
  before that block began. The first block has no earlier games, so both decision models train on the later
  blocks (the same rows, for a fair ablation); a later block with too few earlier rows stays unscored as well.
  Only auxiliary targets fitted in every scored block are used. If too few rows are scored or no target is
  usable, the enriched model is not fitted and the base model trains on every fit row.

Policy: route A (the pair's most-completed route in train games) unless the pair has enough train support in
both arms, the propensity lies in the overlap band, and the decision model's predicted win-chance gain for B
exceeds the preference margin. Nothing forces a departure. Evaluation: doubly robust policy value versus
always-A on the evaluation rows, with match-clustered standard errors, propensity clipping/extreme diagnostics,
coverage, direct observed descriptive outcomes and the enriched-versus-base ablation. This is observational:
it adjusts for the measured context only, not for unmeasured skill, intent or team coordination.

The report and models go to a private directory (default data/research/recommender/...), never the site.
"""
import argparse
import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from backends import resolve, verify_device
from decisions import KEYS, OUTCOME
from engine import ROOT
from wpa import FEATURES

REPORT_VERSION = "recommender-experimental-v0"
STAGES = ("slot1", "slot2", "slot3", "boots")
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
CODED = ("champion", "opponent", "region", "patch", "pair_id")  # codes from the fit rows' vocabulary; unseen -> NaN
CONTEXT_SOURCES = ("state_pre", "stage", "stage_step", "role", "snapshot_age_ms", "current_gold_snapshot",
                   "dead_at_decision", "boots_before", "completed_before", "inventory_before") + CODED
ACTION_SOURCES = ("branch", "route_a", "route_b")
CONTEXT_NAMES = ([f"state_{f}" for f in FEATURES] + [f"stage_{s}" for s in STAGES] + [f"role_{r}" for r in ROLES]
                 + ["stage_step", "snapshot_age_s", "current_gold", "dead_at_decision", "has_boots", "n_completed",
                    "n_inventory"] + [f"{c}_code" for c in CODED])
ACTION_NAMES = ["arm_b", "candidate_route"]
PROPENSITY_NAMES = CONTEXT_NAMES + ["route_a", "route_b"]
assert not set(CONTEXT_SOURCES + ACTION_SOURCES) & set(KEYS + OUTCOME), "a key or outcome column among the features"

DEAD_ERROR_MAX_S = 10.0
AUX = {"gold_lead_change_5": "team_gold_lead_change_5", "takedowns_5": "takedowns_5", "deaths_5": "deaths_5",
       "champion_damage_5": "champion_damage_dealt_5", "time_alive_s_5": "time_alive_ms_5"}
AUX_OBJECTIVE = {"takedowns_5": "count:poisson", "deaths_5": "count:poisson"}

# Predeclared before any evaluation row is read; never tuned on evaluation results.
GATE = dict(preference_margin=0.03,        # predicted win-chance gain B over A needed to depart
            min_arm_train_rows=30,         # train rows in each arm of the pair
            overlap=(0.1, 0.9),            # propensity band where a departure is allowed
            weight_clip=0.02,              # propensities clipped to [c, 1 - c] in the estimate
            extreme=0.05,                  # a propensity below this or above 1 - this counts as extreme
            max_extreme_share=0.25,
            min_eval_matches=1000, min_departure_rows=200,
            min_ci_lower=0.0)              # lower 95% bound of the value gain must exceed this
XGB = dict(n_estimators=200, learning_rate=0.05, max_depth=4, min_child_weight=20, reg_lambda=5.0,
           tree_method="hist", max_bin=256, n_jobs=4)
DECISION_XGB = dict(XGB, max_depth=3, min_child_weight=30, reg_lambda=10.0)
CROSSFIT_BLOCKS = 3
MIN_CROSSFIT_ROWS = 200  # earlier-game rows each later block's auxiliary models need
MIN_MODEL_ROWS = 50

CAVEATS = [
    "Observational: adjusts for measured pre-purchase context only; unmeasured skill, intent and coordination remain.",
    "Scope: slot comparisons start at the first distinguishing component purchase, not at a completed item; boots "
    "comparisons condition on buying an upgrade. No full-route or completed-item effect is claimed.",
    "Counterfactual predictions keep the observed purchase time and state; the choice may also change that timing.",
    "Doubly robust values need overlap and correct nuisance models; clipped propensities trade bias for variance.",
    "Repeat players are not clustered (match clusters only).",
]


def parse(row):
    for c in ("state_pre", "completed_before", "inventory_before"):
        if isinstance(row.get(c), str):
            row[c] = json.loads(row[c])
    row["arm"] = int(row["branch"] == "b")
    return row


def load(path, fold):
    """(rows of one fold, accepted branch_pairs of that fold, branch_meta). Opened read-only."""
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    db.row_factory = sqlite3.Row
    folds = sorted(f for f, in db.execute("SELECT DISTINCT fold FROM branch_comparison"))
    if fold not in folds:
        db.close()
        raise ValueError(f"fold {fold!r} not in {path} (folds {folds}); the held-out fold needs branches.py --include-heldout")
    rows = [parse(dict(r)) for r in db.execute("SELECT * FROM branch_comparison WHERE fold=?", (fold,))]
    pairs = [dict(r) for r in db.execute("SELECT * FROM branch_pairs WHERE fold=? AND status='accepted'", (fold,))]
    meta = {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM branch_meta")}
    db.close()
    return rows, pairs, meta


def split(rows, fold):
    """(fit, focus, evaluation rows). Refuses focus rows in fit/evaluation and any game overlap in time."""
    eval_role = "evaluate" if fold == "heldout" else "validate"
    fit = [r for r in rows if r["split_role"] == "train"]
    focus = [r for r in rows if r["split_role"] == "train_focus"]
    ev = [r for r in rows if r["split_role"] == eval_role]
    other = Counter(r["split_role"] for r in rows) - Counter({"train": len(fit), "train_focus": len(focus), eval_role: len(ev)})
    if other:
        raise ValueError(f"unexpected split roles in fold {fold}: {dict(other)}")
    if any(r["focus_source"] for r in fit + ev):
        raise ValueError("focus-source rows outside train_focus")
    if not fit or not ev:
        raise ValueError(f"fold {fold}: {len(fit)} fit rows, {len(ev)} evaluation rows")
    if {r["match_id"] for r in fit + focus} & {r["match_id"] for r in ev}:
        raise ValueError("a match is in both fitting and evaluation rows")
    if max(r["started_at"] + r["duration_ms"] for r in fit + focus) >= min(r["started_at"] for r in ev):
        raise ValueError("a fitting game ends after the first evaluation game starts")
    return fit, focus, ev


def item_code(iid):
    try:
        return float(iid)
    except (TypeError, ValueError):
        return math.nan


class Encoder:
    """Pre-action context and action features; category codes come from the fit rows only."""

    def __init__(self, fit_rows):
        self.vocab = {c: {v: i for i, v in enumerate(sorted({str(r[c]) for r in fit_rows}))} for c in CODED}

    def context(self, r):
        state = r["state_pre"] if r["state_pre"] and len(r["state_pre"]) == len(FEATURES) else [math.nan] * len(FEATURES)
        num = lambda v: math.nan if v is None else float(v)
        return ([float(v) for v in state] + [float(r["stage"] == s) for s in STAGES] + [float(r["role"] == x) for x in ROLES]
                + [num(r["stage_step"]), num(r["snapshot_age_ms"]) / 1000, num(r["current_gold_snapshot"]),
                   num(r["dead_at_decision"]), float(r["boots_before"] not in (None, "none")),
                   float(len(r["completed_before"] or [])), float(len(r["inventory_before"] or []))]
                + [float(self.vocab[c].get(str(r[c]), math.nan)) for c in CODED])

    def contexts(self, rows):
        return np.asarray([self.context(r) for r in rows], dtype=float).reshape(len(rows), len(CONTEXT_NAMES))

    @staticmethod
    def actions(rows, arms):
        return np.asarray([[float(a), item_code(r["route_b"] if a else r["route_a"])] for r, a in zip(rows, arms)],
                          dtype=float).reshape(len(rows), len(ACTION_NAMES))

    @staticmethod
    def routes(rows):
        return np.asarray([[item_code(r["route_a"]), item_code(r["route_b"])] for r in rows], dtype=float).reshape(len(rows), 2)


def aux_value(r, name):
    """(value, None) or (None, reason) over (t, min(t+5 min, game end)]."""
    if name == "champion_damage_5":
        dealt, span = r["champion_damage_dealt_5"], r["damage_exposure_ms_5"]
        if dealt is None or not span:
            return None, "missing_damage"
        return float(dealt), None
    if name == "time_alive_s_5":
        err = r["dead_time_error_s"]
        if err is None or abs(err) > DEAD_ERROR_MAX_S or r["time_alive_ms_5"] is None:
            return None, "dead_time_unreliable"
        return r["time_alive_ms_5"] / 1000, None
    v = r[AUX[name]]
    return (None, "missing") if v is None else (float(v), None)


def aux_targets(rows, name):
    """(mask, values, Counter of exclusion reasons)."""
    got = [aux_value(r, name) for r in rows]
    mask = np.asarray([v is not None for v, _ in got], dtype=bool)
    return mask, np.asarray([v for v, _ in got if v is not None], dtype=float), Counter(e for _, e in got if e)


class Fitted:
    """An XGBoost model, or a constant fallback when it could not be fitted."""

    def __init__(self, model, fallback, status, classifier):
        self.model, self.fallback, self.status, self.classifier = model, fallback, status, classifier

    def predict(self, X):
        if self.model is None or not len(X):
            return np.full(len(X), self.fallback, dtype=float)
        return self.model.predict_proba(X)[:, 1] if self.classifier else self.model.predict(X).astype(float)


def fit(X, y, objective, params, backend, seed):
    from xgboost import XGBClassifier, XGBRegressor
    classifier = objective == "binary:logistic"
    fallback = float(np.mean(y)) if len(y) else math.nan
    if len(y) < MIN_MODEL_ROWS:
        return Fitted(None, fallback, "too_few_rows", classifier)
    if classifier and len(set(y.tolist())) < 2:
        return Fitted(None, fallback, "single_class", classifier)
    kw = dict(params, objective=objective, random_state=seed, device="cuda:0" if backend == "xgb-cuda" else "cpu")
    model = (XGBClassifier if classifier else XGBRegressor)(**kw)
    model.fit(X, y)
    verify_device(model, backend)
    return Fitted(model, fallback, "fitted", classifier)


def temporal_blocks(rows, k):
    """[(block row indices, earlier row indices)] over k chronological match blocks. 'Earlier' rows belong to games
    that ended before the block's first game started."""
    matches = sorted({(r["started_at"], r["match_id"]) for r in rows})
    block_of = {m: i * k // len(matches) for i, (_, m) in enumerate(matches)}
    out = []
    for b in range(k):
        starts = [s for s, m in matches if block_of[m] == b]
        if not starts:
            continue
        idx = [i for i, r in enumerate(rows) if block_of[r["match_id"]] == b]
        earlier = [i for i, r in enumerate(rows) if r["started_at"] + r["duration_ms"] < starts[0]]
        out.append((idx, earlier))
    return out


def crossfit_aux(rows, X, params, backend, seed, blocks=CROSSFIT_BLOCKS, min_rows=MIN_CROSSFIT_ROWS):
    """(out-of-time auxiliary predictions (n, len(AUX)) at the observed arm, NaN where unscored; scored mask;
    auxiliary names fitted in every scored block; per-block status). A block with fewer than min_rows earlier
    rows stays unscored; with fewer than min_rows scored rows in all, nothing is usable."""
    oof = np.full((len(rows), len(AUX)), np.nan)
    scored = np.zeros(len(rows), dtype=bool)
    usable, status = set(AUX), []
    for b, (idx, earlier) in enumerate(temporal_blocks(rows, blocks)):
        if b == 0:
            continue
        if len(earlier) < min_rows:
            status.append(dict(block=b, rows=len(idx), earlier_rows=len(earlier), status="too_few_earlier_rows_unscored"))
            continue
        sub = [rows[i] for i in earlier]
        block = dict(block=b, rows=len(idx), earlier_rows=len(earlier), models={})
        for k, name in enumerate(AUX):
            mask, y, _ = aux_targets(sub, name)
            m = fit(X[earlier][mask], y, AUX_OBJECTIVE.get(name, "reg:squarederror"), params, backend, seed)
            block["models"][name] = m.status
            if m.status != "fitted":
                usable.discard(name)
            oof[idx, k] = m.predict(X[idx])
        scored[idx] = True
        status.append(block)
    if scored.sum() < min_rows:
        usable = set()
    return oof, scored, [n for n in AUX if n in usable], status


def fit_models(fit_rows, backend="xgb-cpu", params=None, decision_params=None, seed=0, min_crossfit_rows=MIN_CROSSFIT_ROWS):
    params, decision_params = params or XGB, decision_params or DECISION_XGB
    enc = Encoder(fit_rows)
    ctx = enc.contexts(fit_rows)
    arms = np.asarray([r["arm"] for r in fit_rows])
    X = np.hstack([ctx, enc.actions(fit_rows, arms)])
    win = np.asarray([r["win"] for r in fit_rows], dtype=float)
    models = dict(win=fit(X, win, "binary:logistic", params, backend, seed),
                  propensity=fit(np.hstack([ctx, enc.routes(fit_rows)]), arms.astype(float), "binary:logistic",
                                 params, backend, seed))
    exclusions = {}
    for name in AUX:
        mask, y, excluded = aux_targets(fit_rows, name)
        models[f"aux_{name}"] = fit(X[mask], y, AUX_OBJECTIVE.get(name, "reg:squarederror"), params, backend, seed)
        exclusions[name] = dict(used=int(mask.sum()), excluded=dict(excluded))
    oof, scored, usable, blocks = crossfit_aux(fit_rows, X, params, backend, seed, min_rows=min_crossfit_rows)
    usable = [n for n in usable if models[f"aux_{n}"].status == "fitted"]
    if usable:
        cols = [list(AUX).index(n) for n in usable]
        models["decision_base"] = fit(X[scored], win[scored], "binary:logistic", decision_params, backend, seed)
        models["decision_enriched"] = fit(np.hstack([X[scored], oof[scored][:, cols]]), win[scored], "binary:logistic",
                                          decision_params, backend, seed)
        crossfit = dict(status="one_way_temporal", decision_rows=int(scored.sum()), blocks=blocks, aux_used=usable)
    else:
        models["decision_base"] = fit(X, win, "binary:logistic", decision_params, backend, seed)
        crossfit = dict(status="not_robust_enriched_not_fitted", decision_rows=len(fit_rows), blocks=blocks, aux_used=[])
    support = Counter((r["pair_id"], r["arm"]) for r in fit_rows)
    return dict(encoder=enc, models=models, crossfit=crossfit, support=support, aux_exclusions=exclusions,
                rows=len(fit_rows), matches=len({r["match_id"] for r in fit_rows}))


def candidates(fitted, rows):
    """Predictions for both arms with the context held fixed: win (n, 2), decision models (n, 2), aux (2, n, k),
    raw propensity of B (n,)."""
    enc, models, n = fitted["encoder"], fitted["models"], len(rows)
    ctx = enc.contexts(rows)
    out = dict(win=np.zeros((n, 2)), base=np.full((n, 2), np.nan), enriched=np.full((n, 2), np.nan),
               aux=np.zeros((2, n, len(AUX))))
    used = [list(AUX).index(a) for a in fitted["crossfit"]["aux_used"]]
    for arm in (0, 1):
        Xa = np.hstack([ctx, enc.actions(rows, [arm] * n)])
        out["win"][:, arm] = models["win"].predict(Xa)
        for k, name in enumerate(AUX):
            out["aux"][arm, :, k] = models[f"aux_{name}"].predict(Xa)
        if models["decision_base"].model is not None:
            out["base"][:, arm] = models["decision_base"].predict(Xa)
        if "decision_enriched" in models and models["decision_enriched"].model is not None:
            out["enriched"][:, arm] = models["decision_enriched"].predict(np.hstack([Xa, out["aux"][arm][:, used]]))
    out["propensity"] = models["propensity"].predict(np.hstack([ctx, enc.routes(rows)]))
    return out


def choose(rows, delta, propensity, support, gate=GATE):
    """(arms, reasons): route A unless the pair is supported, the propensity is in the overlap band and the
    predicted gain for B exceeds the margin. Deterministic; any missing piece falls back to A."""
    arms, reasons = [], []
    for r, d, p in zip(rows, delta, propensity):
        n_a, n_b = support.get((r["pair_id"], 0), 0), support.get((r["pair_id"], 1), 0)
        if not n_a and not n_b:
            reason = "unsupported_pair"
        elif min(n_a, n_b) < gate["min_arm_train_rows"]:
            reason = "arm_support_below_min"
        elif not math.isfinite(d):
            reason = "model_unavailable"
        elif not gate["overlap"][0] <= p <= gate["overlap"][1]:
            reason = "outside_overlap"
        elif not d > gate["preference_margin"]:
            reason = "below_preference_margin"
        else:
            reason = "depart"
        arms.append(int(reason == "depart"))
        reasons.append(reason)
    return np.asarray(arms, dtype=int), reasons


def dr_scores(y, a, mu, e, clip):
    """(n, 2) doubly robust pseudo-outcomes: G(arm) = mu(arm) + 1{A = arm} (Y - mu(A)) / P(A = arm | x), with
    P(B | x) = e clipped to [clip, 1 - clip]."""
    y, a, mu = np.asarray(y, dtype=float), np.asarray(a, dtype=int), np.asarray(mu, dtype=float)
    e = np.clip(np.asarray(e, dtype=float), clip, 1 - clip)
    p = np.stack([1 - e, e], axis=1)
    g = mu.copy()
    i = np.arange(len(y))
    g[i, a] += (y - mu[i, a]) / p[i, a]
    return g


def cluster_mean(values, clusters):
    """Mean and match-clustered standard error (CR1: G/(G-1) small-sample factor) with a normal 95% interval."""
    v = np.asarray(values, dtype=float)
    if not len(v):
        return dict(mean=None, se=None, ci_low=None, ci_high=None, rows=0, clusters=0)
    mean = float(v.mean())
    sums = defaultdict(float)
    for x, c in zip(v, clusters):
        sums[c] += x - mean
    g = len(sums)
    se = math.sqrt(g / (g - 1) * sum(s * s for s in sums.values())) / len(v) if g > 1 else math.nan
    return dict(mean=mean, se=se, ci_low=mean - 1.96 * se, ci_high=mean + 1.96 * se, rows=len(v), clusters=g)


def propensity_diagnostics(e, a, gate):
    e, a = np.asarray(e, dtype=float), np.asarray(a, dtype=int)
    c = np.clip(e, gate["weight_clip"], 1 - gate["weight_clip"])
    w = np.where(a == 1, 1 / c, 1 / (1 - c))
    ess = {arm: float(w[a == arm].sum() ** 2 / (w[a == arm] ** 2).sum()) if (a == arm).any() else 0.0 for arm in (0, 1)}
    return dict(quantiles={q: float(np.quantile(e, q)) for q in (0, 0.01, 0.05, 0.5, 0.95, 0.99, 1)} if len(e) else {},
                extreme_share=float(np.mean((e < gate["extreme"]) | (e > 1 - gate["extreme"]))) if len(e) else None,
                clipped_share=float(np.mean(c != e)) if len(e) else None,
                outside_overlap_share=float(np.mean((e < gate["overlap"][0]) | (e > gate["overlap"][1]))) if len(e) else None,
                max_weight=float(w.max()) if len(w) else None, ess_a=ess[0], ess_b=ess[1],
                rows_a=int((a == 0).sum()), rows_b=int((a == 1).sum()), mean_propensity=float(e.mean()) if len(e) else None)


def classification_quality(y, p):
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
    y, p = np.asarray(y, dtype=float), np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    if not len(y) or not np.isfinite(p).all():
        return None
    return dict(log_loss=float(log_loss(y, p, labels=[0, 1])), brier=float(brier_score_loss(y, p)),
                auc=float(roc_auc_score(y, p)) if len(set(y.tolist())) == 2 else None,
                mean_predicted=float(p.mean()), mean_observed=float(y.mean()))


def policy_section(rows, arms, reasons, g, overlap):
    """Coverage and doubly robust value of one policy versus always-A (arm 0)."""
    clusters = [r["match_id"] for r in rows]
    i = np.arange(len(rows))
    gain = g[i, arms] - g[:, 0]
    out = dict(departures=int(arms.sum()), departure_share=float(arms.mean()), reasons=dict(Counter(reasons)),
               value_policy=cluster_mean(g[i, arms], clusters), value_baseline_a=cluster_mean(g[:, 0], clusters),
               gain_vs_baseline=cluster_mean(gain, clusters),
               gain_vs_baseline_overlap=cluster_mean(gain[overlap], [c for c, o in zip(clusters, overlap) if o]),
               by_scope={})
    for scope in sorted({r["scope"] for r in rows}):
        m = np.asarray([r["scope"] == scope for r in rows])
        out["by_scope"][scope] = dict(rows=int(m.sum()), departures=int(arms[m].sum()),
                                      gain_vs_baseline=cluster_mean(gain[m], [c for c, k in zip(clusters, m) if k]))
    return out, gain


def verdict(section, eval_matches, extreme_share, gate):
    if eval_matches < gate["min_eval_matches"] or section["departures"] < gate["min_departure_rows"]:
        return "insufficient_evidence"
    if extreme_share is None or extreme_share > gate["max_extreme_share"]:
        return "insufficient_overlap"
    low = section["gain_vs_baseline"]["ci_low"]
    return "supported_improvement" if low is not None and low > gate["min_ci_lower"] else "no_supported_improvement"


def observed(rows):
    """Direct observed outcomes by scope and arm: descriptive, not adjusted."""
    groups = defaultdict(list)
    for r in rows:
        groups[(r["scope"], "ab"[r["arm"]])].append(r)
    out = {}
    for (scope, arm), g in sorted(groups.items()):
        cell = dict(rows=len(g), matches=len({r["match_id"] for r in g}), win_rate=float(np.mean([r["win"] for r in g])))
        for name in AUX:
            mask, v, excluded = aux_targets(g, name)
            cell[name] = dict(mean=float(v.mean()) if len(v) else None, rows=int(mask.sum()), excluded=dict(excluded))
        out[f"{scope}|{arm}"] = cell
    return out


def evaluate(fitted, rows, gate=GATE):
    """Every evaluation section for one fitted model set. Evaluation labels are read here and only here."""
    pred = candidates(fitted, rows)
    y = np.asarray([r["win"] for r in rows], dtype=float)
    a = np.asarray([r["arm"] for r in rows], dtype=int)
    e = pred["propensity"]
    g = dr_scores(y, a, pred["win"], e, gate["weight_clip"])
    overlap = (e >= gate["overlap"][0]) & (e <= gate["overlap"][1])
    diag = propensity_diagnostics(e, a, gate)
    matches = len({r["match_id"] for r in rows})
    policies, gains, choices = {}, {}, {}
    for name in ("base", "enriched"):
        if np.isnan(pred[name]).all():
            policies[name] = dict(status="not_fitted")
            continue
        arms, reasons = choose(rows, pred[name][:, 1] - pred[name][:, 0], e, fitted["support"], gate)
        section, gains[name] = policy_section(rows, arms, reasons, g, overlap)
        section["verdict"] = verdict(section, matches, diag["extreme_share"], gate)
        section["decision_model_quality"] = classification_quality(y, pred[name][np.arange(len(rows)), a])
        policies[name], choices[name] = section, arms
    ablation = None
    if len(gains) == 2:
        ablation = dict(enriched_minus_base=cluster_mean(gains["enriched"] - gains["base"], [r["match_id"] for r in rows]),
                        disagreements=int((choices["enriched"] != choices["base"]).sum()))
        low = ablation["enriched_minus_base"]["ci_low"]
        ablation["enriched_adopted"] = bool(policies["enriched"]["verdict"] == "supported_improvement"
                                            and low is not None and low > gate["min_ci_lower"])
    aux_quality = {}
    for k, name in enumerate(AUX):
        mask, v, _ = aux_targets(rows, name)
        p = pred["aux"][a, np.arange(len(rows)), k][mask]
        base = fitted["models"][f"aux_{name}"].fallback
        aux_quality[name] = dict(rows=int(mask.sum()), rmse=float(np.sqrt(np.mean((v - p) ** 2))) if len(v) else None,
                                 rmse_train_mean=float(np.sqrt(np.mean((v - base) ** 2))) if len(v) else None)
    return dict(rows=len(rows), matches=matches, policies=policies, ablation=ablation, propensity=diag,
                propensity_quality=classification_quality(a, e),
                win_model_quality=classification_quality(y, pred["win"][np.arange(len(rows)), a]),
                aux_model_quality=aux_quality, observed=observed(rows)), choices


def support_table(fitted, ev, pairs, choices, fold):
    accepted = {f"{fold}|{p['champion']}|{p['role']}|{p['stage']}|{p['patch']}|{p['route_a']}|{p['route_b']}": p for p in pairs}
    ev_counts = Counter((r["pair_id"], r["arm"]) for r in ev)
    departs = Counter(r["pair_id"] for r, d in zip(ev, choices.get("base", [0] * len(ev))) if d)
    ids = sorted({p for p, _ in fitted["support"]} | {p for p, _ in ev_counts})
    table = []
    for pid in ids:
        p = accepted.get(pid, {})
        table.append(dict(pair_id=pid, scope="boots_upgrade_purchase" if pid.split("|")[3] == "boots" else "first_distinguishing_component",
                          accepted=pid in accepted, completion_support_a=p.get("support_a"), completion_support_b=p.get("support_b"),
                          fit_rows_a=fitted["support"].get((pid, 0), 0), fit_rows_b=fitted["support"].get((pid, 1), 0),
                          eval_rows_a=ev_counts.get((pid, 0), 0), eval_rows_b=ev_counts.get((pid, 1), 0),
                          base_policy_departures=departs.get(pid, 0)))
    return table


def clean(o):
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.integer, np.bool_)):
        return o.item()
    if isinstance(o, (float, np.floating)):
        return float(o) if math.isfinite(o) else None
    return o


def private_dir(out_dir):
    out = Path(out_dir).resolve()
    for public in (ROOT / "public", ROOT / "data" / "public"):
        if out == public.resolve() or public.resolve() in out.parents:
            raise ValueError(f"{out} is a public directory; recommender artifacts stay private")
    if out == ROOT.resolve():
        raise ValueError("refusing to write artifacts into the site root")
    return out


def run(branches_path, out_dir=None, fold="heldout", backend="xgb-cpu", seed=0, params=None, decision_params=None,
        gate=GATE, min_crossfit_rows=MIN_CROSSFIT_ROWS, sensitivity=True, save=True, log=print):
    backend = resolve(backend)
    rows, pairs, meta = load(branches_path, fold)
    fit_rows, focus_rows, ev = split(rows, fold)
    kw = dict(backend=backend, params=params, decision_params=decision_params, seed=seed, min_crossfit_rows=min_crossfit_rows)
    fitted = fit_models(fit_rows, **kw)
    main, choices = evaluate(fitted, ev, gate)
    smoke = fold != "heldout"
    report = dict(
        version=REPORT_VERSION, created_at=int(time.time()), experimental=True,
        input=dict(branches=str(branches_path), fold=fold, branch_meta=meta),
        evaluation_population=("development fold 'validate' rows: exploratory smoke run" if smoke else
                               "later general-source development holdout: exploratory because these games have "
                               "already been inspected"),
        headline_claims_allowed=False,
        gate=gate, backend=backend, seed=seed, xgb=params or XGB, decision_xgb=decision_params or DECISION_XGB,
        features=dict(context=CONTEXT_NAMES, action=ACTION_NAMES, propensity=PROPENSITY_NAMES,
                      context_sources=CONTEXT_SOURCES, action_sources=ACTION_SOURCES),
        fit=dict(rows=fitted["rows"], matches=fitted["matches"], aux_targets=fitted["aux_exclusions"],
                 models={k: m.status for k, m in fitted["models"].items()}, crossfit=fitted["crossfit"]),
        evaluation=main, support=support_table(fitted, ev, pairs, choices, fold),
        coverage=dict(eval_rows=len(ev), eval_rows_with_supported_pair=sum(
            min(fitted["support"].get((r["pair_id"], 0), 0), fitted["support"].get((r["pair_id"], 1), 0)) >= gate["min_arm_train_rows"]
            for r in ev), by_scope=dict(Counter(r["scope"] for r in ev))),
        caveats=CAVEATS)
    if sensitivity and focus_rows:
        aug = fit_models(fit_rows + focus_rows, **kw)
        sens, _ = evaluate(aug, ev, gate)
        report["sensitivity_focus_augmented"] = dict(
            note="train + train_focus fit, same evaluation rows; never used for the gate or the adopted model",
            fit_rows=aug["rows"], focus_rows=len(focus_rows),
            policies={k: {f: v.get(f) for f in ("status", "departures", "gain_vs_baseline", "verdict")}
                      for k, v in sens["policies"].items()})
    report = clean(report)
    if save:
        out = private_dir(out_dir or ROOT / "data" / "research" / "recommender" / f"{REPORT_VERSION}-fold-{fold}-{report['created_at']}")
        (out / "models").mkdir(parents=True, exist_ok=True)
        for name, m in fitted["models"].items():
            if m.model is not None:
                m.model.save_model(str(out / "models" / f"{name}.json"))
        manifest = dict(version=REPORT_VERSION, vocab=fitted["encoder"].vocab,
                        fallbacks={k: m.fallback for k, m in fitted["models"].items()},
                        features=report["features"], aux_used=fitted["crossfit"]["aux_used"], gate=gate)
        (out / "manifest.json").write_text(json.dumps(clean(manifest), indent=1), encoding="utf-8")
        (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        report["artifact_dir"] = str(out)
        log(f"report and models -> {out}")
    return report


def summary(report):
    ev = report["evaluation"]
    lines = [f"{report['version']} fold {report['input']['fold']}: {report['fit']['rows']:,} fit rows, "
             f"{ev['rows']:,} evaluation rows in {ev['matches']:,} matches ({report['evaluation_population']})",
             f"cross-fitting: {report['fit']['crossfit']['status']}, aux used {report['fit']['crossfit']['aux_used']}",
             f"propensity: extreme share {ev['propensity']['extreme_share']}, max weight {ev['propensity']['max_weight']}"]
    for name, p in ev["policies"].items():
        if "gain_vs_baseline" in p:
            gv = p["gain_vs_baseline"]
            lines.append(f"{name}: {p['departures']} departures ({p['departure_share']:.1%}); DR gain vs route A "
                         f"{gv['mean']:+.4f} [{gv['ci_low']:+.4f}, {gv['ci_high']:+.4f}] -> {p['verdict']}")
        else:
            lines.append(f"{name}: {p['status']}")
    lines.append(f"headline claims allowed: {report['headline_claims_allowed']}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--branches", default=str(ROOT / "data" / "branch-comparison.sqlite"))
    parser.add_argument("--fold", default="heldout", help="'heldout' for the report; a development fold is a smoke run")
    parser.add_argument("--out-dir", help="private artifact directory (default data/research/recommender/...)")
    parser.add_argument("--backend", default="xgb-cpu", choices=("auto", "xgb-cpu", "xgb-cuda"))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--no-sensitivity", action="store_true", help="skip the train_focus sensitivity fit")
    args = parser.parse_args()
    report = run(args.branches, args.out_dir, args.fold, args.backend, args.seed, sensitivity=not args.no_sensitivity)
    print(summary(report))


if __name__ == "__main__":
    main()
