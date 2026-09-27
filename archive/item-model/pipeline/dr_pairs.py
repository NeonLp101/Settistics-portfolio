"""Research-only doubly robust per-pair estimates over the frozen r3 training rows.

    python pipeline/dr_pairs.py

Cross-fits fresh win and propensity models on two match-grouped halves of the same training
rows the shipped r3 model used, and scores every row out-of-fold. The shipped r3 win/propensity
weights are never reused here: they were fit on these same rows, so scoring in-sample would bias
the doubly robust estimate toward zero. Reports, per pair, the adjusted treatment estimate the
shipped model's counterfactual win predictions do not carry (see docs/model-release-status-2026-09-26.md,
"Post-release fix"), with a match-clustered confidence interval and the same propensity
diagnostics used for policy evaluation in recommender.py.

Before any headline count, several checks guard against the doubly robust estimate looking more
certain than it is:
- the propensity model is handed each pair's own empirical route-B rate (from the training half
  only) as a fixed XGBoost base_margin offset (see pair_offsets), because min_child_weight is
  measured in hessian units and a rare arm can never earn its own split otherwise -- without this,
  a pair like a 46-row rare arm out of 16,000+ can have every row's out-of-fold propensity
  confidently wrong by 30x or more, which collapses the doubly robust estimate toward the outcome
  model's own (circular) contrast and can flip its adjusted sign versus the raw gap;
- an overlap filter (a pair's propensity extreme share must clear the same gate the shipped policy
  uses) and a separate propensity-calibration check (mean predicted route-B share vs. the pair's
  observed share, reported but not filtered on) catch what the offset does not fully fix;
- a within-pair arm permutation, refit exactly like the real estimate, checks the real pipeline's
  z-scores are calibrated (std near 1, ~5% exceed |z|>1.96 by chance) before trusting them,
  inflating every standard error by the null's if it is not;
- a free split-half replication (the out-of-fold halves are already scored): Benjamini-Hochberg
  survivors are selected on half 0 alone and checked for sign agreement and p<0.05 on half 1 alone
  -- selecting on the pooled estimate and then checking the halves it already contains would make
  agreement close to guaranteed. The headline correction itself is applied once across the whole
  eligible family, not per shipped-lean group.

Reads only 'train' rows before the frozen r3 cutoff; the sealed future-start cohort is never
opened. This is exploratory, observational research data for internal use: it adjusts only for
measured context, is not a causal claim, and never reaches the site or its schema.
"""
import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

from engine import ROOT
from recommendation_export import COLUMNS, FOLD
from recommendation_export_r3 import CUTOFF_MS
from recommender import GATE, XGB, Encoder, clean, cluster_mean, dr_scores, fit, parse, private_dir, propensity_diagnostics

FOLDS = 2  # match-grouped cross-fit halves for the nuisance models
MIN_ESS = 30  # both arms' effective sample size, for the headline counts only


def fold_of(key, folds):
    """A deterministic fold index for `key`. Uses sha256, not zlib.crc32: CRC is an affine function of
    the input over GF(2), so for same-length keys (nearly all match_ids: one fixed-width id per region)
    crc32(salt + key) turns out to be a fixed, correlated function of crc32(key) rather than an
    independent hash -- concretely, salting an inner cross-fit split this way made it collapse onto the
    outer split it was supposed to be independent of, silently training every inner fold on zero rows."""
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "little") % folds


def load_rows(branches_path):
    cols = COLUMNS + ("started_at", "focus_source", "win")
    db = sqlite3.connect(Path(branches_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    rows = [parse(dict(zip(cols, r)))
            for r in db.execute(f"SELECT {', '.join(cols)} FROM branch_comparison WHERE fold=? AND split_role='train'", (FOLD,))]
    db.close()
    if any(r["started_at"] >= CUTOFF_MS or r["focus_source"] for r in rows):
        raise ValueError("future-start or focus row in frozen r3 training branches")
    return rows


def logit(p):
    return math.log(p / (1 - p))


def pair_offsets(rows, arms, smoothing=1.0):
    """({pair_id: logit smoothed route-B share}, pooled fallback logit), from these rows only. The
    propensity model is handed this as a fixed base_margin so its trees only need to learn the deviation
    from a pair's own empirical rate, instead of needing enough rows to carve out a leaf for a rare pair
    from scratch -- min_child_weight is measured in hessian units (~p(1-p) per row), so a rare arm can
    otherwise never earn its own split and the model falls back to a badly wrong pooled rate for it."""
    counts = defaultdict(lambda: [0, 0])
    for r, a in zip(rows, arms):
        counts[r["pair_id"]][a] += 1
    pooled_a, pooled_b = (sum(c[0] for c in counts.values()), sum(c[1] for c in counts.values())) if counts else (0, 0)
    pooled = logit((pooled_b + smoothing) / (pooled_a + pooled_b + 2 * smoothing))
    offsets = {pid: logit((c[1] + smoothing) / (c[0] + c[1] + 2 * smoothing)) for pid, c in counts.items()}
    return offsets, pooled


def apply_offset(rows, offsets, pooled):
    return np.asarray([offsets.get(r["pair_id"], pooled) for r in rows])


def crossfit_nuisance(rows, params=None, backend="xgb-cpu", seed=0, folds=FOLDS, arms=None, fold_salt=""):
    """(win_oof (n, 2), propensity_oof (n,), arms (n,), win (n,), half (n,)): every row scored by a win
    and a propensity model fit only on the match-grouped half it is not in. `arms` overrides the rows'
    observed arm (only for the permutation null check below); the win label always stays observed. The
    propensity model gets each pair's own empirical route-B rate (from the training half only) as a
    base_margin offset; see pair_offsets. `fold_salt` changes which half a match falls in without
    changing anything else -- needed when this fold split must be independent of another one already
    computed from the same match_ids (see independent_halves), since otherwise the two splits collapse
    onto each other and one fold trains on zero rows."""
    params = params or XGB
    enc = Encoder(rows)
    ctx = enc.contexts(rows)
    routes = enc.routes(rows)
    arms = np.asarray([r["arm"] for r in rows]) if arms is None else np.asarray(arms)
    win = np.asarray([r["win"] for r in rows], dtype=float)
    half = np.asarray([fold_of(fold_salt + r["match_id"], folds) for r in rows])
    win_oof, prop_oof = np.full((len(rows), 2), np.nan), np.full(len(rows), np.nan)
    for h in range(folds):
        train, score = half != h, half == h
        train_rows = [r for r, t in zip(rows, train) if t]
        scored_rows = [r for r, s in zip(rows, score) if s]
        win_model = fit(np.hstack([ctx[train], Encoder.actions(train_rows, arms[train])]), win[train],
                        "binary:logistic", params, backend, seed)
        offsets, pooled = pair_offsets(train_rows, arms[train])
        prop_model = fit(np.hstack([ctx[train], routes[train]]), arms[train].astype(float),
                         "binary:logistic", params, backend, seed, base_margin=apply_offset(train_rows, offsets, pooled))
        for arm in (0, 1):
            win_oof[score, arm] = win_model.predict(np.hstack([ctx[score], Encoder.actions(scored_rows, [arm] * len(scored_rows))]))
        prop_oof[score] = prop_model.predict(np.hstack([ctx[score], routes[score]]), base_margin=apply_offset(scored_rows, offsets, pooled))
    return win_oof, prop_oof, arms, win, half


def independent_halves(rows, params=None, backend="xgb-cpu", seed=0):
    """(outer half (n,), win_oof (n, 2), propensity_oof (n,)): two match-grouped outer halves, each
    internally 2-fold cross-fit using ONLY its own rows. Unlike crossfit_nuisance's single shared 2-fold
    split -- where half 0's scores come from a model trained on half 1's rows and vice versa, so the two
    halves share information both ways through the opposite-trained models -- no row's out-of-fold score
    here ever depends on a model that saw the other outer half's labels. Needed wherever two genuinely
    independent halves are required (split-half replication, the shrinkage checkpoint), not just two
    folds of one cross-fit."""
    outer = np.asarray([fold_of(r["match_id"], 2) for r in rows])
    win_oof, prop_oof = np.full((len(rows), 2), np.nan), np.full(len(rows), np.nan)
    for o in (0, 1):
        idx = np.flatnonzero(outer == o)
        # A different salt than the (none) used for `outer`, or every row in this subset would hash to
        # the same inner fold as its outer one and one inner fold would train on zero rows.
        wo, po, _, _, _ = crossfit_nuisance([rows[i] for i in idx], params, backend, seed, folds=2, fold_salt="inner")
        win_oof[idx], prop_oof[idx] = wo, po
    return outer, win_oof, prop_oof


def permute_arms_within_pair(rows, seed):
    """Real arm labels reshuffled among each pair's own rows: the null that arm assignment carries no
    information, holding context, pair identity and the observed win outcome fixed."""
    rng = np.random.default_rng(seed)
    arms = np.asarray([r["arm"] for r in rows])
    permuted = arms.copy()
    by_pair = defaultdict(list)
    for i, r in enumerate(rows):
        by_pair[r["pair_id"]].append(i)
    for idx in by_pair.values():
        idx = np.asarray(idx)
        permuted[idx] = rng.permutation(arms[idx])
    return permuted


def two_sided_p(mean, se):
    if se is None or not math.isfinite(se) or se == 0:
        return None
    return math.erfc(abs(mean / se) / math.sqrt(2))


def benjamini_hochberg(pvals, alpha=0.05):
    """Boolean array: which p-values (NaN treated as 1) survive BH correction at alpha."""
    p = np.asarray([1.0 if v is None else v for v in pvals])
    order = np.argsort(p)
    ranks = np.arange(1, len(p) + 1)
    below = p[order] <= ranks / len(p) * alpha
    sig = np.zeros(len(p), dtype=bool)
    if below.any():
        sig[order[:np.max(np.where(below)) + 1]] = True
    return sig


def pair_table(rows, win_oof, prop_oof, arms, win, gate=GATE):
    """{pair_id: doubly robust estimate, raw win rates, support and propensity diagnostics}."""
    g = dr_scores(win, arms, win_oof, prop_oof, gate["weight_clip"])
    effect = g[:, 1] - g[:, 0]
    by_pair = defaultdict(list)
    for i, r in enumerate(rows):
        by_pair[r["pair_id"]].append(i)
    table = {}
    for pid, idx in by_pair.items():
        idx = np.asarray(idx)
        a = arms[idx]
        est = cluster_mean(effect[idx], [rows[i]["match_id"] for i in idx])
        raw_a, raw_b = win[idx][a == 0], win[idx][a == 1]
        diag = propensity_diagnostics(prop_oof[idx], a, gate)
        table[pid] = dict(dr_effect=dict(est, p_value=two_sided_p(est["mean"], est["se"])),
                          raw_win_rate_a=float(raw_a.mean()) if len(raw_a) else None,
                          raw_win_rate_b=float(raw_b.mean()) if len(raw_b) else None,
                          rows_a=int((a == 0).sum()), rows_b=int((a == 1).sum()), propensity=diag)
    return table


def inflate_se(table, scale):
    """Widen every pair's interval and p-value by `scale`, in place, when the null check found the raw
    standard errors overconfident."""
    if scale <= 1.0:
        return table
    for row in table.values():
        d = row["dr_effect"]
        if d["se"] is None:
            continue
        se = d["se"] * scale
        d["se"], d["ci_low"], d["ci_high"] = se, d["mean"] - 1.96 * se, d["mean"] + 1.96 * se
        d["p_value"] = two_sided_p(d["mean"], se)
    return table


def null_calibration(rows, params=None, backend="xgb-cpu", seed=1, gate=GATE):
    """Refit under a within-pair arm permutation (no true effect by construction). Reports how the real
    pipeline's z-scores would be calibrated: std near 1 and ~5% exceeding |z|>1.96 is a pass. Uses the
    same eligibility filter (ESS and overlap) as the real analysis, not ESS alone."""
    win_oof, prop_oof, arms, win, _ = crossfit_nuisance(rows, params, backend, seed, arms=permute_arms_within_pair(rows, seed))
    table = pair_table(rows, win_oof, prop_oof, arms, win, gate)
    pids = eligible_pairs(table, gate)
    z = np.asarray([table[pid]["dr_effect"]["mean"] / table[pid]["dr_effect"]["se"] for pid in pids
                   if table[pid]["dr_effect"]["se"] not in (None, 0) and math.isfinite(table[pid]["dr_effect"]["se"])])
    z_std = float(z.std()) if len(z) else None
    return dict(pairs=len(pids), z_std=z_std, share_significant=float(np.mean(np.abs(z) > 1.96)) if len(z) else None,
               se_inflation=max(1.0, z_std) if z_std else 1.0)


def half_tables(rows, win_oof, prop_oof, arms, win, half, gate=GATE):
    """{0: pair_table over half 0's rows, 1: pair_table over half 1's rows}: both already out-of-fold, so
    this is free -- no extra fitting. Downstream consumers (split-half replication, dr_shrink.py's
    checkpoint) need two independent halves, not the pooled table."""
    return {h: pair_table([r for r, hh in zip(rows, half) if hh == h], win_oof[half == h], prop_oof[half == h],
                          arms[half == h], win[half == h], gate) for h in (0, 1)}


def split_half_replication(tables, gate=GATE):
    """Independent replication: select Benjamini-Hochberg survivors using half 0 alone, then check sign
    agreement and p < 0.05 on half 1 alone. Selecting on the pooled estimate (which already contains both
    halves) would make agreement close to guaranteed, since half 1 is not then independent of the
    selection."""
    pids0 = eligible_pairs(tables[0], gate)
    survivors0 = {pid for pid, s in zip(pids0, benjamini_hochberg([tables[0][pid]["dr_effect"]["p_value"] for pid in pids0])) if s}
    agree = replicated = checked = 0
    for pid in survivors0:
        m1 = tables[1].get(pid)
        if m1 is None or m1["dr_effect"]["mean"] is None:
            continue
        checked += 1
        same_sign = np.sign(tables[0][pid]["dr_effect"]["mean"]) == np.sign(m1["dr_effect"]["mean"])
        agree += int(same_sign)
        replicated += int(same_sign and m1["dr_effect"]["p_value"] is not None and m1["dr_effect"]["p_value"] < 0.05)
    return dict(half0_survivors=len(survivors0), checked=checked, agree=agree, replicated_at_p05=replicated)


def join_recommendations(table, recommendations_path):
    """Attach the shipped modelLean and predicted win gap to each pair, read-only, for comparison only."""
    try:
        doc = json.loads(Path(recommendations_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for e in doc["entries"]:
        pid = f"{FOLD}|{e['champion']}|{e['role']}|{e['stage']}|{e['patch']}|{e['baselineRoute']}|{e['alternativeRoute']}"
        if pid in table:
            table[pid]["shipped"] = dict(modelLean=e["modelLean"], predicted=e["predicted"])


def lean_label(table, pid):
    shipped = table[pid].get("shipped")
    return "unjoined" if shipped is None else (shipped["modelLean"] or "unsupported")


def eligible_pairs(table, gate=GATE):
    return [pid for pid, r in table.items() if r["propensity"]["ess_a"] >= MIN_ESS and r["propensity"]["ess_b"] >= MIN_ESS
            and r["propensity"]["extreme_share"] is not None and r["propensity"]["extreme_share"] <= gate["max_extreme_share"]]


def propensity_miscalibration(table, pids, tolerance=1.5):
    """How many pairs' mean out-of-fold propensity differs from the pair's own observed route-B share by
    more than `tolerance`x. High counts mean the propensity model's per-pair base rate is untrustworthy
    even where the overlap filter sees nothing extreme -- a rare, well-separated pair can have every row's
    propensity confidently wrong in the same direction, which no per-row extreme-share check catches."""
    bad = 0
    for pid in pids:
        r = table[pid]
        n = r["rows_a"] + r["rows_b"]
        share, mp = (r["rows_b"] / n if n else None), r["propensity"]["mean_propensity"]
        if not share or not mp:
            continue
        ratio = mp / share
        if ratio > tolerance or ratio < 1 / tolerance:
            bad += 1
    return bad


def summarize(table, pids, survivors, null, replication, gate=GATE):
    """~10-15 lines: family-wide significance and calibration, then a breakdown by shipped lean and by
    boots vs. non-boots slots."""
    z = np.asarray([table[pid]["dr_effect"]["mean"] / table[pid]["dr_effect"]["se"] for pid in pids
                   if table[pid]["dr_effect"]["se"] not in (None, 0) and math.isfinite(table[pid]["dr_effect"]["se"])])
    excludes_zero = sum(1 for pid in pids if table[pid]["dr_effect"]["ci_low"] is not None
                        and (table[pid]["dr_effect"]["ci_low"] > 0 or table[pid]["dr_effect"]["ci_high"] < 0))
    lines = []
    if pids:
        lines.append(f"family: {len(pids)}/{len(table)} pairs pass ESS>={MIN_ESS} both arms and overlap "
                     f"(extreme share <= {gate['max_extreme_share']}); {excludes_zero} exclude 0 at 95% "
                     f"({excludes_zero / len(pids) * 100:.1f}% vs ~5% expected by chance); {len(survivors)} survive "
                     f"Benjamini-Hochberg across the whole family; z std {z.std():.2f} (1.0 expected if well calibrated)")
    else:
        lines.append("family: 0 pairs pass the eligibility filter")
    if null["z_std"] is not None:
        verdict = "well calibrated" if null["se_inflation"] <= 1.1 else f"standard errors inflated x{null['se_inflation']:.2f} below"
        lines.append(f"null calibration (within-pair arm permutation, {null['pairs']} pairs): z std {null['z_std']:.2f}, "
                     f"{null['share_significant'] * 100:.1f}% exceed |z|>1.96 by chance -- {verdict}")
    else:
        lines.append("null calibration: too few eligible pairs to check")
    bad = propensity_miscalibration(table, pids)
    lines.append(f"propensity calibration: {bad}/{len(pids)} pairs' mean propensity differs from their observed "
                f"route-B share by more than 1.5x (target: close to 0)")
    lines.append(f"independent split-half replication: of {replication['half0_survivors']} pairs surviving "
                f"Benjamini-Hochberg on half 0 alone, {replication['agree']}/{replication['checked']} agree in sign on "
                f"half 1 and {replication['replicated_at_p05']} also clear p<0.05 there")

    def bucket(name, keep):
        rows = [pid for pid in pids if keep(pid)]
        surv = sum(1 for pid in rows if pid in survivors)
        lines.append(f"  {name}: {len(rows)} eligible, {surv} survive BH" if rows else f"  {name}: 0 eligible")
    groups = defaultdict(list)
    for pid in pids:
        groups[lean_label(table, pid)].append(pid)
    for name in sorted(groups):
        bucket(name, lambda pid, name=name: pid in groups[name])
    is_boots = lambda pid: pid.split("|")[3] == "boots"
    bucket("boots", is_boots)
    bucket("non-boots slots", lambda pid: not is_boots(pid))
    return lines


def build(branches_path, recommendations_path=None, params=None, backend="xgb-cpu", seed=0):
    rows = load_rows(branches_path)
    win_oof, prop_oof, arms, win, _ = crossfit_nuisance(rows, params, backend, seed)
    table = pair_table(rows, win_oof, prop_oof, arms, win)
    null = null_calibration(rows, params, backend, seed + 1)
    inflate_se(table, null["se_inflation"])
    if recommendations_path and Path(recommendations_path).exists():
        join_recommendations(table, recommendations_path)
    pids = eligible_pairs(table)
    survivors = {pid for pid, s in zip(pids, benjamini_hochberg([table[pid]["dr_effect"]["p_value"] for pid in pids])) if s}
    outer, ind_win_oof, ind_prop_oof = independent_halves(rows, params, backend, seed)
    halves = half_tables(rows, ind_win_oof, ind_prop_oof, arms, win, outer)
    for t in halves.values():
        inflate_se(t, null["se_inflation"])
    replication = split_half_replication(halves)
    summary = summarize(table, pids, survivors, null, replication)
    return clean(dict(rows=len(rows), pairs=len(table), folds=FOLDS, min_ess=MIN_ESS, null_calibration=null,
                      split_half_replication=replication, bh_survivors=sorted(survivors), table=table,
                      half_tables={str(h): t for h, t in halves.items()}, summary=summary))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--branches", default=str(ROOT / "data" / "fulltrain-r3-branches.sqlite"))
    parser.add_argument("--recommendations", default=str(ROOT / "data" / "public" / "recommendations.json"))
    parser.add_argument("--out-dir", default=str(ROOT / "data" / "research" / "dr-pairs"))
    parser.add_argument("--backend", default="xgb-cpu", choices=("auto", "xgb-cpu", "xgb-cuda"))
    args = parser.parse_args()
    report = build(args.branches, args.recommendations, backend=args.backend)
    out = private_dir(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"{report['rows']:,} rows, {report['pairs']} pairs -> {out / 'report.json'}")
    print("\n".join(report["summary"]))


if __name__ == "__main__":
    main()
