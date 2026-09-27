"""Empirical-Bayes shrinkage over pipeline/dr_pairs.py's per-pair doubly robust estimates.

    python pipeline/dr_shrink.py

Groups eligible pairs by (stage, unordered route pair) across champions and fits a two-level
Gaussian random-effects model: an item-pair group effect (tau_g^2) shared by every champion facing
that comparison, and a champion-specific deviation around it (tau_c^2), on top of each pair's own
(already inflated) sampling variance from dr_pairs.py. The prior mean is fixed at 0 -- there are no
fixed effects to estimate -- so maximum likelihood and REML coincide, and both variances are found
by exact grid search over the group log-likelihoods (a rank-1 Sherman-Morrison update per group,
never a dense inversion).

Orientation matters before anything is pooled: the same two items compared on two champions must
point the same direction, since which item is "route A" vs "route B" in a pair_id is arbitrary.
Every group computation here works in a fixed (higher item id) minus (lower item id) orientation,
flipped back to the pair's own route-B-minus-route-A orientation only when a result is reported.

A pair alone in its group shrinks toward 0; a pair sharing a group with others is pulled toward
that group's precision-weighted mean; an unseen pair on patch day starts from its group's posterior
(or 0, for an unseen group). Two boundary likelihood-ratio tests (Self & Liang 1987: reject at
2*delta > 2.71, not the usual 3.84, since the null sits on the edge of the parameter space) ask
whether either variance is distinguishable from 0 at all.

Checkpoint before any of this is trusted as a product number: fit tau on half 0 alone, predict
half 1, and compare inverse-variance-weighted MSE against zero (the shipped model's effective
answer) and against the raw, unshrunk half-0 estimate. Proceed only if shrinkage beats zero with a
group-resampled bootstrap interval excluding 0; otherwise "can't tell" remains the honest answer,
by design, not as a failure of the method.

Reads only pipeline/dr_pairs.py's private output (data/research/dr-pairs/report.json). Output is
private research data for internal use, never the site.
"""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

from dr_pairs import eligible_pairs
from engine import ROOT
from recommender import clean, private_dir

# 0 (no variance component) plus 79 log-spaced candidates covering plausible standard-error scales.
GRID = np.concatenate([[0.0], np.geomspace(1e-4, 0.25, 79)])
LR_BOUNDARY_CRITICAL = 2.71  # Self & Liang (1987): 5% one-sided test when the null is on the boundary


def group_key(pair_id):
    """((stage, lo item id, hi item id), whether route_b is the higher id) for one pair_id."""
    _, _champion, _role, stage, _patch, route_a, route_b = pair_id.split("|")
    lo, hi = sorted((route_a, route_b), key=int)
    return (stage, lo, hi), route_b == hi


def oriented_estimates(table, pids):
    """{group key: [(pair_id, champion, oriented y, se)]}, y = P(win | hi item) - P(win | lo item)."""
    groups = defaultdict(list)
    for pid in pids:
        key, b_is_hi = group_key(pid)
        d = table[pid]["dr_effect"]
        if d["mean"] is None or d["se"] is None or not math.isfinite(d["se"]) or d["se"] <= 0:
            continue
        y = d["mean"] if b_is_hi else -d["mean"]
        groups[key].append((pid, pid.split("|")[1], y, d["se"]))
    return groups


def group_loglik(ys, ses, tau_g2, tau_c2):
    """One group's log-likelihood up to an additive constant, for Sigma = diag(se^2 + tau_c^2) +
    tau_g^2 * ones(n, n) -- a rank-1 update of a diagonal matrix, via Sherman-Morrison-Woodbury."""
    d = ses ** 2 + tau_c2
    inv_d = 1 / d
    S, T, Q = inv_d.sum(), (ys * inv_d).sum(), (ys ** 2 * inv_d).sum()
    return -0.5 * (np.log(d).sum() + math.log1p(tau_g2 * S) + Q - tau_g2 * T ** 2 / (1 + tau_g2 * S))


def total_loglik(groups, tau_g2, tau_c2):
    return sum(group_loglik(np.asarray([y for _, _, y, _ in g]), np.asarray([se for _, _, _, se in g]), tau_g2, tau_c2)
              for g in groups.values())


def loglik_grid(groups, tau_g_grid, tau_c_grid):
    """(len(tau_g_grid), len(tau_c_grid)) log-likelihood surface: the same Sherman-Morrison formula as
    group_loglik/total_loglik, vectorized over the whole grid at once instead of one point at a time, so
    fit_variances stays fast with hundreds of groups and an 80x80 grid."""
    tg, tc = np.asarray(tau_g_grid, dtype=float), np.asarray(tau_c_grid, dtype=float)
    total = np.zeros((len(tg), len(tc)))
    for members in groups.values():
        ys = np.asarray([y for _, _, y, _ in members])[:, None]
        ses2 = np.asarray([se for _, _, _, se in members])[:, None] ** 2
        d = ses2 + tc[None, :]  # (n, Gc)
        inv_d = 1 / d
        S, T, Q = inv_d.sum(0), (ys * inv_d).sum(0), (ys ** 2 * inv_d).sum(0)  # (Gc,)
        log_d_sum = np.log(d).sum(0)  # (Gc,)
        one_plus = 1 + tg[:, None] * S[None, :]  # (Ga, Gc)
        term = tg[:, None] * T[None, :] ** 2 / one_plus
        total += -0.5 * (log_d_sum[None, :] + np.log(one_plus) + Q[None, :] - term)
    return total


def fit_variances(groups, grid=GRID):
    """((tau_g^2, tau_c^2), max log-likelihood): exact grid search, refined once around the best point."""
    def best_on(gg, gc):
        ll = loglik_grid(groups, gg, gc)
        i, j = np.unravel_index(np.argmax(ll), ll.shape)
        return gg[i], gc[j], float(ll[i, j])

    def refine(center):
        if center == 0:
            return grid
        return np.unique(np.concatenate([[0.0], np.clip(center * np.geomspace(0.3, 3, 25), 1e-6, None)]))

    tg, tc, _ = best_on(grid, grid)
    tg, tc, ll = best_on(refine(tg), refine(tc))
    return (tg, tc), ll


def posteriors(groups, tau_g2, tau_c2):
    """{pair_id: dict(group, group_mean, group_var, mean, var)}, still in the (hi - lo) orientation."""
    out = {}
    for key, members in groups.items():
        ys = np.asarray([y for _, _, y, _ in members])
        ses = np.asarray([se for _, _, _, se in members])
        d = ses ** 2 + tau_c2
        inv_d = 1 / d
        S, T = inv_d.sum(), (ys * inv_d).sum()
        v_g = 1 / (1 / tau_g2 + S) if tau_g2 > 0 else 0.0
        m_g = v_g * T if tau_g2 > 0 else 0.0
        for (pid, champion, y, se), di in zip(members, d):
            mean = m_g + (tau_c2 / di) * (y - m_g)
            var = tau_c2 * se ** 2 / di + (se ** 2 / di) ** 2 * v_g
            out[pid] = dict(group=key, group_mean=m_g, group_var=v_g, mean=mean, var=var)
    return out


def lr_test_boundary(groups, ll_hat, which, grid=GRID):
    """Profile likelihood-ratio test that the `which` ('group' or 'champ') variance is 0, against the
    two-sided-boundary null: reject at 2*delta > LR_BOUNDARY_CRITICAL, the 50:50 chi-square(0, 1)
    mixture's critical value, not the usual chi-square(1) value of 3.84."""
    restricted = float(loglik_grid(groups, [0.0], grid).max()) if which == "group" else \
        float(loglik_grid(groups, grid, [0.0]).max())
    delta = 2 * (ll_hat - restricted)
    return dict(delta=delta, rejects_zero=bool(delta > LR_BOUNDARY_CRITICAL))


def shrink_table(table, pids):
    """{pair_id: dict(group, tau_g2, tau_c2, shrunk_mean, shrunk_se, raw_mean, raw_se)} in each pair's own
    route-B-minus-route-A orientation, plus the fit summary. Every eligible pair gets a shrunk estimate,
    even alone in its group (shrinks toward 0 there)."""
    groups = oriented_estimates(table, pids)
    (tau_g2, tau_c2), ll = fit_variances(groups)
    post = posteriors(groups, tau_g2, tau_c2)
    rows = {}
    for pid, p in post.items():
        _, b_is_hi = group_key(pid)
        sign = 1 if b_is_hi else -1
        rows[pid] = dict(group=list(p["group"]), shrunk_mean=sign * p["mean"],
                         shrunk_se=math.sqrt(p["var"]) if p["var"] >= 0 else None,
                         raw_mean=table[pid]["dr_effect"]["mean"], raw_se=table[pid]["dr_effect"]["se"])
    fit = dict(tau_g2=tau_g2, tau_c2=tau_c2, log_likelihood=ll, n_groups=len(groups), n_pairs=len(rows),
              lr_test_group_zero=lr_test_boundary(groups, ll, "group"),
              lr_test_champ_zero=lr_test_boundary(groups, ll, "champ"))
    return fit, rows


def ivw_mse(targets, ses, predictions):
    """Inverse-variance-weighted mean squared error: each squared error weighted by 1/se^2 of the target
    (half-1) measurement, so a noisier target counts for less."""
    w = 1 / np.asarray(ses) ** 2
    return float(np.sum(w * (np.asarray(targets) - np.asarray(predictions)) ** 2) / w.sum())


def is_boots(pair_id):
    return pair_id.split("|")[3] == "boots"


def filter_table(table, keep):
    return {pid: v for pid, v in table.items() if keep(pid)}


def group_sizes(table, pids):
    """[(group key, member count)], largest first -- to check whether a result depends on one big group."""
    groups = oriented_estimates(table, pids)
    return sorted(((key, len(members)) for key, members in groups.items()), key=lambda kv: -kv[1])


def exclude_group(table, group_key_to_drop):
    return {pid: v for pid, v in table.items() if group_key(pid)[0] != group_key_to_drop}


def checkpoint(half0, half1, n_boot=2000, seed=0):
    """Fit tau on half 0's eligible pairs, predict half 1: does shrinking beat predicting zero, and does
    it beat the raw unshrunk half-0 estimate? Bootstraps by resampling GROUPS, not pairs, since pairs in
    the same group are not independent draws once the group effect is shared."""
    pids0 = eligible_pairs(half0)
    fit, rows0 = shrink_table(half0, pids0)
    by_group = defaultdict(list)
    targets, ses1, zero_pred, raw_pred, shrunk_pred = [], [], [], [], []
    for pid, r in rows0.items():
        m1 = half1.get(pid, {}).get("dr_effect")
        if not m1 or m1["mean"] is None or m1["se"] is None or not math.isfinite(m1["se"]) or m1["se"] <= 0:
            continue
        by_group[tuple(r["group"])].append(len(targets))
        targets.append(m1["mean"]), ses1.append(m1["se"])
        zero_pred.append(0.0), raw_pred.append(r["raw_mean"]), shrunk_pred.append(r["shrunk_mean"])
    if not targets:
        return dict(fit_on_half=0, predicted_half=1, checked_pairs=0, n_groups=0,
                   half0_fit=dict(tau_g2=fit["tau_g2"], tau_c2=fit["tau_c2"]), mse=None,
                   gain_vs_zero=None, gain_vs_raw=None, proceed=False)
    targets, ses1 = np.asarray(targets), np.asarray(ses1)
    zero_pred, raw_pred, shrunk_pred = np.asarray(zero_pred), np.asarray(raw_pred), np.asarray(shrunk_pred)
    mse = dict(zero=ivw_mse(targets, ses1, zero_pred), raw=ivw_mse(targets, ses1, raw_pred),
              shrunk=ivw_mse(targets, ses1, shrunk_pred))
    groups = list(by_group.values())
    rng = np.random.default_rng(seed)

    def boot_diff(base, other):
        diffs = np.empty(n_boot)
        for b in range(n_boot):
            picked = rng.choice(len(groups), len(groups), replace=True)
            idx = np.concatenate([groups[g] for g in picked]) if groups else np.asarray([], dtype=int)
            diffs[b] = ivw_mse(targets[idx], ses1[idx], base[idx]) - ivw_mse(targets[idx], ses1[idx], other[idx])
        return dict(mean=float(diffs.mean()), ci_low=float(np.quantile(diffs, 0.025)), ci_high=float(np.quantile(diffs, 0.975)))

    gain_vs_zero = boot_diff(zero_pred, shrunk_pred)
    gain_vs_raw = boot_diff(raw_pred, shrunk_pred)
    proceed = gain_vs_zero["ci_low"] > 0
    return dict(fit_on_half=0, predicted_half=1, checked_pairs=len(targets), n_groups=len(groups),
               half0_fit=dict(tau_g2=fit["tau_g2"], tau_c2=fit["tau_c2"]), mse=mse,
               gain_vs_zero=gain_vs_zero, gain_vs_raw=gain_vs_raw, proceed=bool(proceed))


def robustness_checks(half0, half1, n_leave_out=2):
    """Does the checkpoint's gain over zero hold up split by boots vs. non-boots slots, and does it
    survive dropping each of the largest groups one at a time? A result driven by one dominant group, or
    present only in boots (picked partly against the enemy team's composition, which this model's context
    cannot see), is easier to explain as confounding that happens to replicate than as a real item effect."""
    boots = checkpoint(filter_table(half0, is_boots), filter_table(half1, is_boots))
    non_boots = checkpoint(filter_table(half0, lambda p: not is_boots(p)), filter_table(half1, lambda p: not is_boots(p)))
    pids0 = eligible_pairs(half0)
    leave_one_out = []
    for key, size in group_sizes(half0, pids0)[:n_leave_out]:
        leave_one_out.append(dict(group=list(key), size=size,
                                  checkpoint=checkpoint(exclude_group(half0, key), exclude_group(half1, key))))
    return dict(boots=boots, non_boots=non_boots, leave_one_group_out=leave_one_out)


def build(report_path):
    doc = json.loads(Path(report_path).read_text(encoding="utf-8"))
    table = doc["table"]
    pids = eligible_pairs(table)
    fit, rows = shrink_table(table, pids)
    fit_boots, _ = shrink_table(table, [p for p in pids if is_boots(p)])
    fit_non_boots, _ = shrink_table(table, [p for p in pids if not is_boots(p)])
    half_tables = doc.get("half_tables")
    check = robustness = None
    if half_tables:
        check = checkpoint(half_tables["0"], half_tables["1"])
        robustness = robustness_checks(half_tables["0"], half_tables["1"])
    return clean(dict(fit=fit, fit_boots=fit_boots, fit_non_boots=fit_non_boots, pairs=rows,
                      checkpoint=check, robustness=robustness))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", default=str(ROOT / "data" / "research" / "dr-pairs" / "report.json"))
    parser.add_argument("--out-dir", default=str(ROOT / "data" / "research" / "dr-shrink"))
    args = parser.parse_args()
    report = build(args.report)
    out = private_dir(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    f = report["fit"]
    print(f"tau_g^2={f['tau_g2']:.5f} tau_c^2={f['tau_c2']:.5f}, {f['n_groups']} groups, {f['n_pairs']} pairs")
    print(f"LR test group!=0: delta={f['lr_test_group_zero']['delta']:.2f} rejects={f['lr_test_group_zero']['rejects_zero']}")
    print(f"LR test champ!=0: delta={f['lr_test_champ_zero']['delta']:.2f} rejects={f['lr_test_champ_zero']['rejects_zero']}")
    def print_checkpoint(label, c):
        if not c["mse"]:
            print(f"{label}: 0 checkable pairs")
            return
        print(f"{label} ({c['checked_pairs']} pairs, {c['n_groups']} groups): MSE zero={c['mse']['zero']:.5f} "
             f"raw={c['mse']['raw']:.5f} shrunk={c['mse']['shrunk']:.5f}; gain vs zero {c['gain_vs_zero']['mean']:+.5f} "
             f"[{c['gain_vs_zero']['ci_low']:+.5f}, {c['gain_vs_zero']['ci_high']:+.5f}] -> proceed={c['proceed']}")

    if report["checkpoint"]:
        print_checkpoint("checkpoint", report["checkpoint"])
    if report["robustness"]:
        r = report["robustness"]
        print_checkpoint("  boots only", r["boots"])
        print_checkpoint("  non-boots only", r["non_boots"])
        for loo in r["leave_one_group_out"]:
            print_checkpoint(f"  without group {loo['group']} ({loo['size']} pairs)", loo["checkpoint"])
    print(f"report -> {out / 'report.json'}")


if __name__ == "__main__":
    main()
