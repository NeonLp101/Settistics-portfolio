"""Pre-check C, round 2 (docs/item-policy-structural-fix.md section 12): are the two boots item-group effects
found by dr_pairs.py/dr_shrink.py (Berserker's Greaves 3006 -> Gluttonous Greaves 3008, and Plated Steelcaps
3047 <-> Mercury's Treads 3111) explained by player skill or in-game state rather than the item?

    python pipeline/boots_confound_check.py --backend xgb-cuda

Round 1 of this check (kept below as c1_future_win_rate/c2_within_player, still reported for reference) had two
flaws caught on review:
    - C1 ("later games can't be caused by this choice") is contaminated by habit: a player who buys Gluttonous
      tends to keep buying it, so a real item effect shows up in "later games" too, through the repeat
      purchase, not through skill.
    - C2 (within-player) was misread backwards: it removes *stable* skill by construction, so a non-zero C2 is
      evidence against stable-skill confounding, not for it. Its real remaining threat is in-game state
      ("buy the upgrade when already ahead"), which raw win cannot separate from the item's effect.

Round 2 fixes both:

    C1' (purged future win rate). Only counts a later game in the control if the player made no decision in
        THIS SAME GROUP in it either (excludes the habitual-repurchase channel). Reports one player-clustered
        OLS coefficient on the hi/lo indicator (cluster-robust sandwich SE), not two separately-eyeballed CIs.

    C2' (arm-blind within-player residual). Fits a win model on pre-purchase context ONLY (no item/arm
        feature at all -- Encoder.contexts(), 5-fold cross-fit grouped by match, dr_pairs.fold_of) and looks at
        each row's residual (actual win - out-of-fold predicted win). Among players who chose both sides of a
        group, compares their own mean residual on each side. This isolates in-game-state confounding
        ("bought when already ahead") that a raw within-player win comparison cannot see.

Reads data/fulltrain-r3-branches.sqlite and data/fulltrain-r3-decisions.sqlite, read-only, 'train' rows before
the frozen r3 cutoff only (same guard as dr_pairs.load_rows) -- the sealed cohort is never opened.
Output: text summary and data/research/boots-confound/report.json.
"""
import argparse
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

from dr_pairs import crossfit_nuisance, fold_of
from engine import ROOT
from recommendation_export import COLUMNS
from recommendation_export_r3 import CUTOFF_MS
from recommender import GATE, Encoder, XGB, clean, cluster_mean, dr_scores, fit as fit_model, parse, private_dir

GROUPS = {
    "greaves": dict(lo="3006", hi="3008", lo_name="Berserker's Greaves", hi_name="Gluttonous Greaves"),
    "defensive_boots": dict(lo="3047", hi="3111", lo_name="Plated Steelcaps", hi_name="Mercury's Treads"),
}
HIST_MIN = 5  # earlier/later games needed before a player's shrunk win rate is trusted much
FOLDS = 5


def ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)


def load_rows(branches_path):
    """All 'train', pre-cutoff, non-focus boots-branch rows in either group, as recommender.parse()-shaped
    dicts (so Encoder.contexts() works unmodified), plus player_ref/duration_ms for the checks here."""
    cols = COLUMNS + ("started_at", "focus_source", "win", "player_ref", "duration_ms", "team_id", "participant_id", "t_ms")
    con = ro(branches_path)
    rows = [parse(dict(zip(cols, r))) for r in con.execute(
        f"SELECT {', '.join(cols)} FROM branch_comparison WHERE fold='heldout' AND split_role='train' AND stage='boots'")]
    con.close()
    if any(r["started_at"] >= CUTOFF_MS or r["focus_source"] for r in rows):
        raise ValueError("future-start or focus row in frozen r3 training branches")
    out = {}
    for name, g in GROUPS.items():
        member = [r for r in rows if {r["route_a"], r["route_b"]} == {g["lo"], g["hi"]}]
        for r in member:
            chosen = r["route_a"] if r["branch"] == "a" else r["route_b"]
            r["hi"] = int(chosen == g["hi"])
        out[name] = member
        n_hi = sum(r["hi"] for r in member)
        print(f"{name}: {len(member):,} boots-branch rows ({n_hi:,} hi / {len(member) - n_hi:,} lo)")
    return out


def load_player_games(decisions_path):
    """{player_ref: sorted [(started_at, ended_at, win)]} over every distinct game in the decisions file."""
    con = ro(decisions_path)
    games = defaultdict(set)
    for m, pid, ref, started, dur, win in con.execute(
            "SELECT match_id, participant_id, player_ref, started_at, duration_ms, win FROM purchase_decisions "
            "WHERE player_ref IS NOT NULL GROUP BY match_id, participant_id"):
        games[ref].add((started, started + (dur or 0), win))
    con.close()
    out = {ref: sorted(g) for ref, g in games.items()}
    print(f"player history: {len(out):,} distinct players")
    return out


# ---------- shared helpers ----------

def clustered(values, clusters):
    v = np.asarray(values, dtype=float)
    if len(v) < 2:
        return dict(mean=float(v.mean()) if len(v) else None, se=None, n=len(v))
    mean = float(v.mean())
    sums = defaultdict(float)
    for x, c in zip(v, clusters):
        sums[c] += x - mean
    g = len(sums)
    se = math.sqrt(g / (g - 1) * sum(s * s for s in sums.values())) / len(v) if g > 1 else None
    return dict(mean=mean, se=se, n=len(v), clusters=g)


def estimate(values, idx, matches, players):
    vals = [values[i] for i in idx]
    by_match = clustered(vals, [matches[i] for i in idx])
    by_player = clustered(vals, [players[i] or f"row{i}" for i in idx])
    se = max(s for s in (by_match["se"], by_player["se"]) if s is not None) if by_match["se"] is not None else None
    return dict(mean=by_match["mean"], se=se, se_match=by_match["se"], se_player=by_player["se"], n=len(idx))


def cluster_robust_diff(y, x, clusters):
    """Cluster-robust (CR1) SE of the coefficient on `x` in y = a + b*x + e, clusters possibly shared between
    the x=0 and x=1 rows (as here: the same player can have rows on both sides) -- a plain two-sample
    clustered-mean comparison assumes independent clusters per side, which does not hold here."""
    y, x = np.asarray(y, dtype=float), np.asarray(x, dtype=float)
    n = len(y)
    X = np.column_stack([np.ones(n), x])
    xtx = X.T @ X
    xtx_inv = np.linalg.inv(xtx)
    beta = xtx_inv @ (X.T @ y)
    resid = y - X @ beta
    by_cluster = defaultdict(lambda: np.zeros(2))
    for i, c in enumerate(clusters):
        by_cluster[c] += X[i] * resid[i]
    g = len(by_cluster)
    meat = sum(np.outer(s, s) for s in by_cluster.values())
    correction = g / (g - 1) * (n - 1) / (n - 2) if g > 1 and n > 2 else 1.0
    var = correction * xtx_inv @ meat @ xtx_inv
    se = math.sqrt(var[1, 1]) if var[1, 1] > 0 else None
    return dict(coef=float(beta[1]), se=se, n=n, clusters=g)


# ---------- round 1 (kept for reference; see the module docstring for its flaws) ----------

def future_win_rate(rows, games):
    rate, n_later = np.full(len(rows), np.nan), np.zeros(len(rows), dtype=int)
    for i, r in enumerate(rows):
        g = games.get(r["player_ref"])
        if not g:
            continue
        end = r["started_at"] + r["duration_ms"]
        later = [w for s, e, w in g if e > end]
        n_later[i] = len(later)
        if later:
            rate[i] = (sum(later) + 5) / (len(later) + 10)
    return rate, n_later


def c1_future_win_rate(rows, games):
    matches, players = [r["match_id"] for r in rows], [r["player_ref"] for r in rows]
    hi = np.asarray([r["hi"] for r in rows])
    rate, n_later = future_win_rate(rows, games)
    covered = np.isfinite(rate) & (n_later >= 1)
    covered_min = covered & (n_later >= HIST_MIN)
    result = {}
    for label, mask in (("any_later_game", covered), (f"at_least_{HIST_MIN}_later_games", covered_min)):
        idx = np.flatnonzero(mask)
        if len(idx) < 2:
            result[label] = dict(coverage=float(mask.mean()), hi=None, lo=None, diff=None)
            continue
        hi_idx, lo_idx = [i for i in idx if hi[i] == 1], [i for i in idx if hi[i] == 0]
        e_hi = estimate(rate, hi_idx, matches, players) if len(hi_idx) >= 2 else None
        e_lo = estimate(rate, lo_idx, matches, players) if len(lo_idx) >= 2 else None
        result[label] = dict(coverage=float(mask.mean()), hi=e_hi, lo=e_lo,
                             diff=(e_hi["mean"] - e_lo["mean"]) if e_hi and e_lo and e_hi["mean"] is not None
                             and e_lo["mean"] is not None else None)
    return result


def c2_within_player(rows):
    by_player = defaultdict(lambda: {"hi": [], "lo": []})
    for r in rows:
        if r["player_ref"]:
            by_player[r["player_ref"]]["hi" if r["hi"] else "lo"].append(r["win"])
    diffs = [float(np.mean(d["hi"])) - float(np.mean(d["lo"])) for d in by_player.values() if d["hi"] and d["lo"]]
    if len(diffs) < 2:
        return dict(players_with_both=len(diffs), mean=None, se=None)
    diffs = np.asarray(diffs)
    return dict(players_with_both=len(diffs), mean=float(diffs.mean()), se=float(diffs.std(ddof=1) / math.sqrt(len(diffs))))


# ---------- round 2: C1' and C2' ----------

def group_game_ends(rows):
    """{player_ref: {ended_at, ...}} -- every game end-time where that player made a decision in THIS group,
    to exclude from C1's "later games" pool (purges the habitual-repurchase channel)."""
    ends = defaultdict(set)
    for r in rows:
        if r["player_ref"]:
            ends[r["player_ref"]].add(r["started_at"] + r["duration_ms"])
    return ends


def c1_prime(rows, games):
    """Purged future win rate: later games exclude any game where the player made a decision in this same
    group. One player-clustered OLS diff (hi vs lo), not two separately-eyeballed CIs."""
    ends = group_game_ends(rows)
    rate, n_later = np.full(len(rows), np.nan), np.zeros(len(rows), dtype=int)
    for i, r in enumerate(rows):
        g = games.get(r["player_ref"])
        if not g:
            continue
        end = r["started_at"] + r["duration_ms"]
        own_ends = ends.get(r["player_ref"], ())
        later = [w for s, e, w in g if e > end and e not in own_ends]
        n_later[i] = len(later)
        if later:
            rate[i] = (sum(later) + 5) / (len(later) + 10)
    hi = np.asarray([r["hi"] for r in rows])
    players = [r["player_ref"] for r in rows]
    result = {}
    for label, min_n in (("any_later_game", 1), (f"at_least_{HIST_MIN}_later_games", HIST_MIN)):
        mask = np.isfinite(rate) & (n_later >= min_n)
        idx = np.flatnonzero(mask)
        if len(idx) < 4:
            result[label] = dict(coverage=float(mask.mean()), diff=None)
            continue
        d = cluster_robust_diff(rate[idx], hi[idx], [players[i] for i in idx])
        result[label] = dict(coverage=float(mask.mean()), diff=d["coef"], se=d["se"], n=d["n"], clusters=d["clusters"])
    return result


def arm_blind_residual(rows, backend):
    """Out-of-fold win residual (actual - predicted) from a model that never sees the item/arm choice at all
    -- context only (Encoder.contexts()), 5-fold cross-fit grouped by match (dr_pairs.fold_of, sha256-based)."""
    enc = Encoder(rows)
    ctx = enc.contexts(rows)
    win = np.asarray([r["win"] for r in rows], dtype=float)
    half = np.asarray([fold_of(r["match_id"], FOLDS) for r in rows])
    pred = np.full(len(rows), np.nan)
    for k in range(FOLDS):
        train, score = half != k, half == k
        model = fit_model(ctx[train], win[train], "binary:logistic", XGB, backend, seed=0)
        pred[score] = model.predict(ctx[score])
    return win - pred


def c2_prime(rows, residual):
    """Within-player mean of (arm-blind residual | hi) - (arm-blind residual | lo), among players who chose
    both sides -- isolates in-game-state confounding a raw within-player win comparison cannot see."""
    by_player = defaultdict(lambda: {"hi": [], "lo": []})
    for r, res in zip(rows, residual):
        if r["player_ref"]:
            by_player[r["player_ref"]]["hi" if r["hi"] else "lo"].append(res)
    diffs = [float(np.mean(d["hi"])) - float(np.mean(d["lo"])) for d in by_player.values() if d["hi"] and d["lo"]]
    if len(diffs) < 2:
        return dict(players_with_both=len(diffs), mean=None, se=None)
    diffs = np.asarray(diffs)
    return dict(players_with_both=len(diffs), mean=float(diffs.mean()), se=float(diffs.std(ddof=1) / math.sqrt(len(diffs))))


def switcher_players(rows):
    """{player_ref} who bought BOTH sides of this group -- the same set c2_prime/c2_within_player use."""
    by_player = defaultdict(lambda: {"hi": False, "lo": False})
    for r in rows:
        if r["player_ref"]:
            by_player[r["player_ref"]]["hi" if r["hi"] else "lo"] = True
    return {ref for ref, d in by_player.items() if d["hi"] and d["lo"]}


def oriented_dr(rows, hi_item, backend, seed=0):
    """Cluster-mean oriented (hi-lo) doubly robust effect over `rows`, via dr_pairs.crossfit_nuisance
    (match-grouped cross-fit, propensity base_margin offset already applied inside it)."""
    if len(rows) < 60:
        return dict(mean=None, se=None, ci_low=None, ci_high=None, rows=len(rows), clusters=0)
    win_oof, prop_oof, arms, win, _ = crossfit_nuisance(rows, XGB, backend, seed)
    g = dr_scores(win, arms, win_oof, prop_oof, GATE["weight_clip"])
    effect = g[:, 1] - g[:, 0]
    sign = np.asarray([1.0 if r["route_b"] == hi_item else -1.0 for r in rows])
    return cluster_mean(effect * sign, [r["match_id"] for r in rows])


def same_sample_diagnostic(name, rows, g, backend, log=print):
    """The deciding check: does the between-player DR estimate, restricted to just the switcher players C2'
    uses, still show the full-population effect, or does it collapse toward C2' -- i.e. is the population-wide
    DR gap explained by WHICH players switch (player-level confounding) rather than by the item?"""
    switchers = switcher_players(rows)
    switch_rows = [r for r in rows if r["player_ref"] in switchers]
    log(f"  [{name}] same-sample DR: {len(switch_rows):,} rows from {len(switchers):,} switcher players")
    full_dr = oriented_dr(rows, g["hi"], backend, seed=0)
    switch_dr = oriented_dr(switch_rows, g["hi"], backend, seed=1)
    return dict(switchers=len(switchers), switch_rows=len(switch_rows), full_population_dr=full_dr, switchers_only_dr=switch_dr)


def pp(v):
    return "n/a" if v is None else f"{100 * v:+.2f}"


def ci_str(mean, se):
    if mean is None or se is None or not math.isfinite(se):
        return f"{pp(mean)} pp [n/a]"
    return f"{pp(mean)} pp [{pp(mean - 1.96 * se)}, {pp(mean + 1.96 * se)}]"


def run(branches_path, decisions_path, backend="xgb-cpu", log=print):
    group_rows = load_rows(branches_path)
    games = load_player_games(decisions_path)
    result = {}
    for name, g in GROUPS.items():
        rows = group_rows.get(name, [])
        if not rows:
            result[name] = dict(rows=0)
            continue
        log(f"\n[{name}] fitting arm-blind context-only win model for C2' ({len(rows):,} rows)...")
        residual = arm_blind_residual(rows, backend)
        same_sample = same_sample_diagnostic(name, rows, g, backend, log)
        result[name] = dict(rows=len(rows), definition=g,
                            c1_round1=c1_future_win_rate(rows, games), c2_round1=c2_within_player(rows),
                            c1_prime=c1_prime(rows, games), c2_prime=c2_prime(rows, residual),
                            same_sample=same_sample)
    report(result, log)
    return result


def report(result, log=print):
    log("\n=== Pre-check C round 2: C1'/C2' on the two boots item groups ===")
    for name, r in result.items():
        if not r.get("rows"):
            log(f"\n[{name}] no rows")
            continue
        g = r["definition"]
        log(f"\n[{name}] {g['lo_name']} ({g['lo']}) -> {g['hi_name']} ({g['hi']}), {r['rows']:,} rows")
        for label, c in r["c1_prime"].items():
            if c.get("diff") is None:
                log(f"  C1' ({label}, coverage {c['coverage']:.0%}): too few rows")
                continue
            log(f"  C1' ({label}, coverage {c['coverage']:.0%}, {c['clusters']:,} player clusters): "
                f"{ci_str(c['diff'], c['se'])} (purged of habitual repurchase; should be ~0)")
        c2p = r["c2_prime"]
        if c2p["mean"] is None:
            log(f"  C2': only {c2p['players_with_both']} players bought both sides")
        else:
            log(f"  C2' ({c2p['players_with_both']} players bought both sides): "
                f"{ci_str(c2p['mean'], c2p['se'])} arm-blind residual difference (isolates in-game-state "
                f"confounding; should be ~0)")
        log(f"  [round 1, for reference] C1 diff (unpurged): "
            + "; ".join(f"{label} {pp(c.get('diff'))}pp" for label, c in r["c1_round1"].items()))
        c2 = r["c2_round1"]
        log(f"  [round 1, for reference] C2 raw within-player: {ci_str(c2['mean'], c2['se'])}")
        s = r["same_sample"]
        fd, sd = s["full_population_dr"], s["switchers_only_dr"]
        log(f"  same-sample DR ({s['switch_rows']:,} rows, {s['switchers']:,} switcher players): "
            f"full-population DR {ci_str(fd['mean'], fd['se'])} vs switchers-only DR {ci_str(sd['mean'], sd['se'])} "
            f"-- compare to C2' {ci_str(c2p['mean'], c2p['se'])} (agreement supports the effect; a collapse "
            f"toward C2' vs. the full-population number supports player-level confounding)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--branches", default=str(ROOT / "data" / "fulltrain-r3-branches.sqlite"))
    parser.add_argument("--decisions", default=str(ROOT / "data" / "fulltrain-r3-decisions.sqlite"))
    parser.add_argument("--out-dir", default=str(ROOT / "data" / "research" / "boots-confound"))
    parser.add_argument("--backend", default="xgb-cpu", choices=("auto", "xgb-cpu", "xgb-cuda"))
    args = parser.parse_args()
    result = run(args.branches, args.decisions, args.backend)
    out = private_dir(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(clean(result), indent=1, default=float), encoding="utf-8")
    print(f"\n-> {out / 'report.json'}")


if __name__ == "__main__":
    main()
