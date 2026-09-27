"""How large are the real item-route effects, and do they survive skill and team-composition adjustment?

    python pipeline/effects.py --backend xgb-cuda
    python pipeline/effects.py --r2-dir D:/path/to/r2-worktree/data --backend xgb-cuda   # r2 files elsewhere
    python pipeline/effects.py --sample-games 20000                                    # quick trial run

Development diagnostic on every collected game (development, r1 and r2 test games). After the decision to run
no further rounds of the current model (docs/item-policy-structural-fix.md section 11), all of them may be used
for development. It reuses the round datasets and never changes them:
data/prospective-r1-{decisions,branches}.sqlite and data/prospective-r2-{decisions,branches}.sqlite (r2 is
optional), plus the main database, read-only, for team compositions.

It answers the supervisor memo's six questions:
1. Pair effects: a cross-fitted AIPW effect of route B vs A on final win for every pair, with base features and
   with team composition and player history added, plus a DerSimonian-Laird estimate of how much true
   effects vary between pairs (tau) and empirical-Bayes shrunk pair effects.
2. The r1/r2 departures rescored with the adjusted features: does the +12 pp collapse?
3. Negative controls: the "effect" of the route on the team gold-lead change between the start of the stage and
   the decision (it happened before the purchase), and on the player's earlier win rate. Both should be zero.
4. The split of the r1/r2 gain per departure into the evaluator's own predicted gap and the outcome part.
5. Timing: the gap between the stage's first purchase and the distinguishing purchase.
6. Player-history coverage.

Nuisance models are XGBoost with native categoricals: separate outcome models per route (T-learner) and a
propensity model, 5-fold cross-fitted with folds grouped by match. Rows with a propensity outside [0.05, 0.95]
are trimmed. Pair intervals use the larger of the match- and player-clustered standard errors. Observational:
the adjustment covers measured features only. Output: text summary and data/research/effects/effects.json.
"""
import argparse
import bisect
import json
import math
import sqlite3
import zlib
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

import recommender
from engine import ROOT
from wpa import FEATURES

STAGES = ("slot1", "slot2", "slot3", "boots")
GOLD = FEATURES.index("team_gold")
TRIM = (0.05, 0.95)
FOLDS = 5
MIN_ARM_ROWS = 30
HIST_MIN = 5  # earlier games a player needs before their win rate is a negative control
PARAMS = dict(eta=0.1, max_depth=6, min_child_weight=20, subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0,
              tree_method="hist", max_bin=256, max_cat_to_onehot=4, seed=0)
ROUNDS = 300
COLS = ["match_id", "player_ref", "participant_id", "team_id", "pair_id", "branch", "win", "started_at", "duration_ms",
        "champion", "role", "opponent", "region", "patch", "stage", "stage_step", "t_ms", "state_pre", "snapshot_age_ms",
        "current_gold_snapshot", "dead_at_decision", "boots_before", "completed_before", "inventory_before"]
NUMERIC = [f"state_{f}" for f in FEATURES] + ["stage_step", "snapshot_age_s", "current_gold", "dead_at_decision",
                                              "has_boots", "n_completed", "n_inventory"]
CATEGORICAL = ["stage", "role", "champion", "opponent", "region", "patch", "pair_id"]
ADJUSTED = ["enemy_ap", "enemy_tank", "ally_ap", "ally_tank", "hist_games", "hist_win_rate", "hist_champion_games",
            "hist_pair_games", "hist_pair_b_share"]


def ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)


# ---------- loading ----------

def load_rows(r1_branches, r2_branches=None, sample_games=None, log=print):
    """Column dict of development + r1 (+ r2) branch rows, with a 'round' column. Development rows come from the r1
    file only (both rounds trained on the same games)."""
    parts = [(r1_branches, "split_role IN ('train','evaluate')")]
    if r2_branches:
        parts.append((r2_branches, "split_role='evaluate'"))
    out = {c: [] for c in COLS + ["round"]}
    for i, (path, where) in enumerate(parts):
        con = ro(path)
        for r in con.execute(f"SELECT {', '.join(COLS)}, split_role FROM branch_comparison WHERE fold='heldout' AND {where}"):
            for c, v in zip(COLS, r):
                out[c].append(v)
            out["round"].append("dev" if r[-1] == "train" else ("r1" if i == 0 else "r2"))
        con.close()
    if sample_games:
        games = sorted(set(out["match_id"]), key=lambda m: zlib.crc32(m.encode()))[:sample_games]
        keep = set(games)
        idx = [i for i, m in enumerate(out["match_id"]) if m in keep]
        out = {c: [v[i] for i in idx] for c, v in out.items()}
    log(f"{len(out['match_id']):,} branch rows: {dict(Counter(out['round']))}")
    return out


def load_players(decision_paths):
    """{(match, participant): (player_ref, team_id, champion, role, started_at, ended_at, win)} and each stage's first
    purchase {(match, participant, stage): (t_ms, team gold lead)} from the round decision files."""
    players, starts = {}, {}
    for path in decision_paths:
        con = ro(path)
        for m, pid, ref, team, champ, role, start, dur, win in con.execute(
                "SELECT match_id, participant_id, player_ref, team_id, champion, role, started_at, duration_ms, win "
                "FROM purchase_decisions GROUP BY match_id, participant_id"):
            players[(m, pid)] = (ref, team, champ, role, start, start + dur, win)
        for m, pid, stage, t, state in con.execute(
                f"SELECT match_id, participant_id, stage, MIN(t_ms), state_pre FROM purchase_decisions "
                f"WHERE stage IN ({','.join('?' * len(STAGES))}) GROUP BY match_id, participant_id, stage", STAGES):
            s = json.loads(state) if isinstance(state, str) else state
            starts[(m, pid, stage)] = (t, s[GOLD] if s and len(s) > GOLD else math.nan)
        con.close()
    return players, starts


def load_compositions(main_db, match_ids, profile_ids, log=print):
    """{match: {team: [champion, ...]}} for every listed game, and champion damage profiles {champion: (ap, tank)}
    averaged over profile_ids games. Reads only participant fields, in chunks."""
    teams, sums = defaultdict(lambda: defaultdict(list)), defaultdict(lambda: np.zeros(3))
    ids, profile_ids = sorted(set(match_ids)), set(profile_ids)
    con = ro(main_db)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for m, team, champ, magic, phys, true, mitigated, taken in con.execute(
                "SELECT m.id, json_extract(p.value,'$.teamId'), json_extract(p.value,'$.championName'), "
                "json_extract(p.value,'$.magicDamageDealtToChampions'), json_extract(p.value,'$.physicalDamageDealtToChampions'), "
                "json_extract(p.value,'$.trueDamageDealtToChampions'), json_extract(p.value,'$.damageSelfMitigated'), "
                "json_extract(p.value,'$.totalDamageTaken') "
                f"FROM matches m, json_each(m.detail,'$.info.participants') p WHERE m.id IN ({','.join('?' * len(chunk))})", chunk):
            teams[m][team].append(champ)
            if m in profile_ids:
                dealt = (magic or 0) + (phys or 0) + (true or 0)
                absorbed = (mitigated or 0) + (taken or 0)
                sums[champ] += ((magic or 0) / dealt if dealt else 0.5, (mitigated or 0) / absorbed if absorbed else 0.5, 1)
    con.close()
    profiles = {c: (s[0] / s[2], s[1] / s[2]) for c, s in sums.items() if s[2]}
    log(f"team compositions for {len(teams):,} games; damage profiles for {len(profiles)} champions")
    return teams, profiles


# ---------- features ----------

def base_features(rows):
    n = len(rows["match_id"])
    X = np.full((n, len(NUMERIC)), np.nan)
    for i in range(n):
        s = rows["state_pre"][i]
        s = json.loads(s) if isinstance(s, str) else s
        if s and len(s) == len(FEATURES):
            X[i, :len(FEATURES)] = s
        num = lambda v: math.nan if v is None else float(v)
        comp = rows["completed_before"][i]
        inv = rows["inventory_before"][i]
        comp = json.loads(comp) if isinstance(comp, str) else (comp or [])
        inv = json.loads(inv) if isinstance(inv, str) else (inv or [])
        X[i, len(FEATURES):] = [num(rows["stage_step"][i]), num(rows["snapshot_age_ms"][i]) / 1000,
                                num(rows["current_gold_snapshot"][i]), num(rows["dead_at_decision"][i]),
                                float(rows["boots_before"][i] not in (None, "none")), float(len(comp)), float(len(inv))]
    codes = np.zeros((n, len(CATEGORICAL)))
    for j, c in enumerate(CATEGORICAL):
        vocab = {v: k for k, v in enumerate(sorted({str(v) for v in rows[c]}))}
        codes[:, j] = [vocab[str(v)] for v in rows[c]]
    return np.hstack([X, codes]), ["q"] * len(NUMERIC) + ["c"] * len(CATEGORICAL)


def history(rows, players):
    """Per row, from the same player's games that ended before this game started: games, shrunk win rate, games on
    this champion, and earlier choices in this pair (count and shrunk share of route B). Missing ref -> NaN."""
    games = defaultdict(list)  # ref -> sorted [(ended_at, win, champion)]
    for ref, _, champ, _, start, end, win in players.values():
        if ref:
            games[ref].append((end, win, champ))
    for g in games.values():
        g.sort()
    ends = {ref: [e for e, _, _ in g] for ref, g in games.items()}
    pair_hist = defaultdict(list)  # (ref, pair) -> sorted [(ended_at, arm)]
    n = len(rows["match_id"])
    arm = [int(b == "b") for b in rows["branch"]]
    for i in range(n):
        ref = rows["player_ref"][i]
        if ref:
            pair_hist[(ref, rows["pair_id"][i])].append((rows["started_at"][i] + rows["duration_ms"][i], arm[i]))
    for h in pair_hist.values():
        h.sort()
    out = np.full((n, 5), np.nan)
    for i in range(n):
        ref, start = rows["player_ref"][i], rows["started_at"][i]
        if not ref or ref not in games:
            continue
        k = bisect.bisect_left(ends[ref], start)
        earlier = games[ref][:k]
        wins = sum(w for _, w, _ in earlier)
        same = sum(c == rows["champion"][i] for _, _, c in earlier)
        ph = pair_hist[(ref, rows["pair_id"][i])]
        j = bisect.bisect_left(ph, (start, -1))
        b = sum(a for _, a in ph[:j])
        out[i] = [k, (wins + 5) / (k + 10), same, j, (b + 1) / (j + 2)]
    return out


def composition(rows, players, teams, profiles):
    """Enemy and ally (excluding the player) mean magic-damage share and mitigation share."""
    n = len(rows["match_id"])
    out = np.full((n, 4), np.nan)
    for i in range(n):
        m, pid = rows["match_id"][i], rows["participant_id"][i]
        info = players.get((m, pid))
        team = rows["team_id"][i] if rows["team_id"][i] is not None else (info[1] if info else None)
        if m not in teams or team is None:
            continue
        own = list(teams[m].get(team, []))
        if rows["champion"][i] in own:
            own.remove(rows["champion"][i])
        enemy = [c for t, cs in teams[m].items() if t != team for c in cs]
        prof = lambda cs, k: np.mean([profiles[c][k] for c in cs if c in profiles]) if any(c in profiles for c in cs) else math.nan
        out[i] = [prof(enemy, 0), prof(enemy, 1), prof(own, 0), prof(own, 1)]
    return out


# ---------- estimation ----------

def train(X, ft, y, objective, backend):
    import xgboost as xgb
    params = dict(PARAMS, objective=objective, device="cuda" if backend == "xgb-cuda" else "cpu")
    return xgb.train(params, xgb.DMatrix(X, label=y, feature_types=ft, enable_categorical=True), ROUNDS)


def predict(model, X, ft):
    import xgboost as xgb
    return model.predict(xgb.DMatrix(X, feature_types=ft, enable_categorical=True))


def fold_of(match_ids, k=FOLDS):
    return np.asarray([zlib.crc32(m.encode()) % k for m in match_ids])


def crossfit_outcome(X, ft, y, arm, folds, objective, backend, log=print, label=""):
    """Out-of-fold mu_0, mu_1: a separate outcome model per route, fitted on rows with a known outcome."""
    mu = np.full((len(y), 2), np.nan)
    for k in range(folds.max() + 1):
        test, fit = folds == k, folds != k
        for a in (0, 1):
            sel = fit & (arm == a) & np.isfinite(y)
            mu[test, a] = predict(train(X[sel], ft, y[sel], objective, backend), X[test], ft)
        log(f"  {label} outcome fold {k + 1}/{folds.max() + 1}")
    return mu


def crossfit_propensity(X, ft, arm, folds, backend, log=print, label=""):
    e = np.full(len(arm), np.nan)
    for k in range(folds.max() + 1):
        test, fit = folds == k, folds != k
        e[test] = predict(train(X[fit], ft, arm[fit].astype(float), "binary:logistic", backend), X[test], ft)
        log(f"  {label} propensity fold {k + 1}/{folds.max() + 1}")
    return e


def aipw(y, arm, mu, e):
    """Per-row doubly robust effect of B vs A; NaN where the propensity is outside TRIM."""
    keep = (e >= TRIM[0]) & (e <= TRIM[1]) & np.isfinite(y)
    phi = np.full(len(y), np.nan)
    a, yy, m, ee = arm[keep], y[keep], mu[keep], e[keep]
    phi[keep] = m[:, 1] - m[:, 0] + a * (yy - m[:, 1]) / ee - (1 - a) * (yy - m[:, 0]) / (1 - ee)
    return phi


def clustered(values, clusters):
    v = np.asarray(values, dtype=float)
    if len(v) < 2:
        return dict(mean=float(v.mean()) if len(v) else None, se=None, n=len(v))
    mean = float(v.mean())
    sums = defaultdict(float)
    for x, c in zip(v, clusters):
        sums[c] += x - mean
    g = len(sums)
    se = math.sqrt(g / (g - 1) * sum(s * s for s in sums.values())) / len(v) if g > 1 else math.nan
    return dict(mean=mean, se=se, n=len(v), clusters=g)


def estimate(phi, idx, matches, refs):
    """Mean effect over rows idx with the larger of the match- and player-clustered standard errors."""
    vals = phi[idx]
    by_match = clustered(vals, [matches[i] for i in idx])
    by_player = clustered(vals, [refs[i] or f"row{i}" for i in idx])
    se = max(s for s in (by_match["se"], by_player["se"]) if s is not None) if by_match["se"] is not None else None
    return dict(mean=by_match["mean"], se=se, se_match=by_match["se"], se_player=by_player["se"], rows=len(idx))


def dersimonian_laird(est, se):
    """(pooled mean, tau, Q, I2, shrunk estimates) for independent estimates with standard errors."""
    est, se = np.asarray(est, dtype=float), np.asarray(se, dtype=float)
    w = 1 / se ** 2
    fixed = float((w * est).sum() / w.sum())
    q = float((w * (est - fixed) ** 2).sum())
    k = len(est)
    c = w.sum() - (w ** 2).sum() / w.sum()
    tau2 = max(0.0, (q - (k - 1)) / c) if c > 0 else 0.0
    wr = 1 / (se ** 2 + tau2)
    pooled = float((wr * est).sum() / wr.sum())
    shrunk = pooled + tau2 / (tau2 + se ** 2) * (est - pooled)
    return dict(pooled=pooled, tau=math.sqrt(tau2), q=q, pairs=k, i2=max(0.0, (q - (k - 1)) / q) if q > 0 else 0.0,
                shrunk=shrunk)


def pair_effects(phi, arm, rows):
    pairs = defaultdict(list)
    for i, p in enumerate(rows["pair_id"]):
        if np.isfinite(phi[i]):
            pairs[p].append(i)
    table = []
    for p, idx in sorted(pairs.items()):
        if min(sum(arm[i] == a for i in idx) for a in (0, 1)) < MIN_ARM_ROWS:
            continue
        cell = estimate(phi, idx, rows["match_id"], rows["player_ref"])
        if cell["se"]:
            table.append(dict(pair=p, **cell))
    if len(table) < 2:
        return dict(pairs=table, heterogeneity=None)
    dl = dersimonian_laird([t["mean"] for t in table], [t["se"] for t in table])
    for t, s in zip(table, dl.pop("shrunk")):
        t["shrunk"] = float(s)
    dl["share_shrunk_over_1pp"] = float(np.mean([abs(t["shrunk"]) > 0.01 for t in table]))
    return dict(pairs=table, heterogeneity=dl)


def overall(phi, rows, mask=None):
    idx = [i for i in range(len(phi)) if np.isfinite(phi[i]) and (mask is None or mask[i])]
    return estimate(phi, idx, rows["match_id"], rows["player_ref"])


# ---------- r1/r2 departures (questions 2 and 4) ----------

def departures(r1_branches, r2_branches, backend, log=print):
    """Refit the frozen r1 policy on the development rows, recover the departures in each test set, and split the
    evaluator's gain per departure into its own predicted gap and the outcome part. {round: [(key, total, model)]}."""
    def full(path, role):
        con = ro(path)
        con.row_factory = sqlite3.Row
        rows = [recommender.parse(dict(r)) for r in con.execute(
            "SELECT * FROM branch_comparison WHERE fold='heldout' AND split_role=?", (role,))]
        con.close()
        return rows
    fitted = recommender.fit_models(full(r1_branches, "train"), backend=backend, seed=0)
    policy = "enriched" if "decision_enriched" in fitted["models"] else "base"
    out = {}
    for name, path in (("r1", r1_branches), ("r2", r2_branches)):
        if not path:
            continue
        ev = full(path, "evaluate")
        pred = recommender.candidates(fitted, ev)
        arms, _ = recommender.choose(ev, pred[policy][:, 1] - pred[policy][:, 0], pred["propensity"], fitted["support"],
                                     recommender.GATE)
        g = recommender.dr_scores([r["win"] for r in ev], [r["arm"] for r in ev], pred["win"], pred["propensity"],
                                  recommender.GATE["weight_clip"])
        out[name] = [((r["match_id"], r["participant_id"], r["stage"]), float(g[i, 1] - g[i, 0]),
                      float(pred["win"][i, 1] - pred["win"][i, 0])) for i, r in enumerate(ev) if arms[i]]
        log(f"{name}: {len(out[name])} departures recovered from the refit ({policy} policy)")
        del ev, pred
    return out


def departure_section(deps, phis, rows):
    key_index = {(m, p, s): i for i, (m, p, s) in enumerate(zip(rows["match_id"], rows["participant_id"], rows["stage"]))}
    section = {}
    for name, items in deps.items():
        idx = [key_index[k] for k, _, _ in items if k in key_index]
        cell = dict(departures=len(items),
                    evaluator_total=clustered([t for _, t, _ in items], [k[0] for k, _, _ in items]),
                    evaluator_model_part=clustered([m for _, _, m in items], [k[0] for k, _, _ in items]),
                    evaluator_outcome_part=clustered([t - m for _, t, m in items], [k[0] for k, _, _ in items]))
        for label, phi in phis.items():
            kept = [i for i in idx if np.isfinite(phi[i])]
            cell[f"crossfit_{label}"] = estimate(phi, kept, rows["match_id"], rows["player_ref"]) if kept else None
            cell[f"crossfit_{label}_trimmed"] = len(idx) - len(kept)
        section[name] = cell
    return section


# ---------- run ----------

def pp(v):
    return "n/a" if v is None or not np.isfinite(v) else f"{100 * v:+.2f}"


def ci(cell):
    if not cell or cell.get("mean") is None or not cell.get("se"):
        return "n/a"
    return f"{pp(cell['mean'])} pp [{pp(cell['mean'] - 1.96 * cell['se'])}, {pp(cell['mean'] + 1.96 * cell['se'])}]"


def run(data_dir, r2_dir=None, main_db=None, backend="xgb-cpu", sample_games=None, skip_departures=False, log=print):
    data_dir = Path(data_dir)
    r2_dir = Path(r2_dir) if r2_dir else data_dir
    r1b, r1d = data_dir / "prospective-r1-branches.sqlite", data_dir / "prospective-r1-decisions.sqlite"
    r2b, r2d = r2_dir / "prospective-r2-branches.sqlite", r2_dir / "prospective-r2-decisions.sqlite"
    if not r1b.exists() or not r1d.exists():
        raise FileNotFoundError(f"{r1b} and {r1d} are needed (built by prospective.py run)")
    has_r2 = r2b.exists() and r2d.exists()
    if not has_r2:
        log(f"r2 files not found in {r2_dir}; continuing with development and r1 games (use --r2-dir)")
    result = dict(backend=backend, sample_games=sample_games, rounds=["dev", "r1"] + (["r2"] if has_r2 else []))

    deps = {} if skip_departures else departures(r1b, r2b if has_r2 else None, backend, log)
    rows = load_rows(r1b, r2b if has_r2 else None, sample_games, log)
    n = len(rows["match_id"])
    arm = np.asarray([int(b == "b") for b in rows["branch"]])
    win = np.asarray(rows["win"], dtype=float)
    players, starts = load_players([r1d] + ([r2d] if has_r2 else []))

    # 5. timing and 6. coverage
    gap, lead_change = np.full(n, np.nan), np.full(n, np.nan)
    for i in range(n):
        st = starts.get((rows["match_id"][i], rows["participant_id"][i], rows["stage"][i]))
        s = rows["state_pre"][i]
        s = json.loads(s) if isinstance(s, str) else s
        if st:
            gap[i] = (rows["t_ms"][i] - st[0]) / 1000
            if s and len(s) > GOLD:
                lead_change[i] = s[GOLD] - st[1]
    g = gap[np.isfinite(gap)]
    if not len(g):
        raise ValueError("no stage start found for any branch row: the decision files do not match the branch files")
    result["timing"] = dict(rows=int(len(g)), quantiles_s={str(q): float(np.quantile(g, q)) for q in (0.25, 0.5, 0.75, 0.9)},
                            share_at_stage_start=float(np.mean(g == 0)), share_within_30s=float(np.mean(g <= 30)),
                            share_within_60s=float(np.mean(g <= 60)), share_within_120s=float(np.mean(g <= 120)),
                            mean_abs_lead_change=float(np.nanmean(np.abs(lead_change))))
    hist = history(rows, players)
    known = np.isfinite(hist[:, 0])
    result["coverage"] = dict(rows_with_player_ref=float(known.mean()),
                              **{f"share_with_{k}_earlier_games": float(np.mean(np.nan_to_num(hist[:, 0]) >= k)) for k in (1, 5, 20)},
                              share_with_earlier_pair_choice=float(np.mean(np.nan_to_num(hist[:, 3]) >= 1)))

    main_db = Path(main_db) if main_db else data_dir / "settistics.sqlite"
    if main_db.exists():
        dev_games = [m for m, r in zip(rows["match_id"], rows["round"]) if r == "dev"]
        teams, profiles = load_compositions(main_db, rows["match_id"], sorted(set(dev_games))[:20000], log)
        comp = composition(rows, players, teams, profiles)
    else:
        log(f"{main_db} not found: team-composition features are missing")
        comp = np.full((n, 4), np.nan)

    X_base, ft_base = base_features(rows)
    X_adj = np.hstack([X_base, comp, hist])
    ft_adj = ft_base + ["q"] * (comp.shape[1] + hist.shape[1])
    folds = fold_of(rows["match_id"])

    by_pair = defaultdict(list)
    for i, p in enumerate(rows["pair_id"]):
        by_pair[p].append(i)
    phis = {}
    for label, X, ft in (("base", X_base, ft_base), ("adjusted", X_adj, ft_adj)):
        log(f"cross-fitting {label} nuisance models on {n:,} rows")
        e = crossfit_propensity(X, ft, arm, folds, backend, log, label)
        mu = crossfit_outcome(X, ft, win, arm, folds, "binary:logistic", backend, log, label)
        phis[label] = aipw(win, arm, mu, e)
        # 3. negative controls with the same features and propensity
        placebo = {}
        mu_p = crossfit_outcome(X, ft, lead_change, arm, folds, "reg:squarederror", backend, log, f"{label} gold placebo")
        placebo["pre_decision_gold_change"] = overall(aipw(lead_change, arm, mu_p, e), rows)
        if label == "base":  # the adjusted features contain the player's win rate itself
            wr = np.where(np.nan_to_num(hist[:, 0]) >= HIST_MIN, hist[:, 1], np.nan)
            if np.isfinite(wr).sum() >= 100:
                mu_w = crossfit_outcome(X, ft, wr, arm, folds, "reg:squarederror", backend, log, f"{label} skill placebo")
                placebo["earlier_win_rate"] = overall(aipw(wr, arm, mu_w, e), rows)
        effects = pair_effects(phis[label], arm, rows)
        calib = [abs(np.mean(e[idx]) - np.mean(arm[idx])) for idx in by_pair.values() if len(idx) >= 100]
        result[label] = dict(trimmed_share=float(np.mean(~np.isfinite(phis[label]))),
                             overall=overall(phis[label], rows),
                             by_round={r: overall(phis[label], rows, np.asarray(rows["round"]) == r) for r in result["rounds"]},
                             pair_propensity_gap_mean=float(np.mean(calib)) if calib else None,
                             heterogeneity=effects["heterogeneity"], negative_controls=placebo,
                             top_pairs=sorted(effects["pairs"], key=lambda t: -abs(t["shrunk"]))[:15] if effects["heterogeneity"] else [],
                             pairs_estimated=len(effects["pairs"]))
    if deps:
        result["departures"] = departure_section(deps, phis, rows)
    report(result, log)
    return result


def report(r, log=print):
    log("\n=== Route effects on final win: development diagnostic ===")
    t = r["timing"]
    log(f"5. Timing: distinguishing purchase {t['quantiles_s']['0.5']:.0f} s after the stage's first purchase (median; "
        f"p75 {t['quantiles_s']['0.75']:.0f} s, p90 {t['quantiles_s']['0.9']:.0f} s). At the first purchase: "
        f"{t['share_at_stage_start']:.0%}; within 30 s {t['share_within_30s']:.0%}, 60 s {t['share_within_60s']:.0%}, "
        f"120 s {t['share_within_120s']:.0%}. Mean |team gold-lead change| in between: {t['mean_abs_lead_change']:.0f}")
    c = r["coverage"]
    log(f"6. Player history: {c['share_with_1_earlier_games']:.0%} of rows have an earlier game by the same player, "
        f"{c['share_with_5_earlier_games']:.0%} at least 5, {c['share_with_20_earlier_games']:.0%} at least 20; "
        f"{c['share_with_earlier_pair_choice']:.0%} made this same choice before")
    for label in ("base", "adjusted"):
        s = r[label]
        h = s["heterogeneity"] or {}
        log(f"\n[{label}] {s['trimmed_share']:.1%} of rows trimmed for overlap; average effect of B vs A {ci(s['overall'])}")
        log("   by round: " + "; ".join(f"{k} {ci(v)}" for k, v in s["by_round"].items()))
        if h:
            log(f"1. Pair effects: {s['pairs_estimated']} pairs. Spread of true effects between pairs tau = "
                f"{100 * h['tau']:.2f} pp (I2 {h['i2']:.0%}, Q {h['q']:.0f} on {h['pairs'] - 1} df); "
                f"{h['share_shrunk_over_1pp']:.0%} of pairs have a shrunk effect beyond 1 pp")
            for p in s["top_pairs"][:5]:
                log(f"     {p['pair']}: shrunk {pp(p['shrunk'])} pp, raw {pp(p['mean'])} ± {100 * 1.96 * p['se']:.1f}, {p['rows']:,} rows")
        nc = s["negative_controls"]
        gc = nc["pre_decision_gold_change"]
        gold = "n/a" if gc["mean"] is None else f"{gc['mean']:+.0f} gold" + (
            f" [{gc['mean'] - 1.96 * gc['se']:+.0f}, {gc['mean'] + 1.96 * gc['se']:+.0f}]" if gc["se"] else "")
        log(f"3. Negative controls (should be 0): pre-decision gold-lead change {gold}"
            + (f"; earlier win rate {ci(nc['earlier_win_rate'])}" if "earlier_win_rate" in nc else ""))
        if s["pair_propensity_gap_mean"] is not None:
            log(f"   propensity calibration: mean |predicted - observed B share| per pair {s['pair_propensity_gap_mean']:.3f}")
    for name, d in r.get("departures", {}).items():
        log(f"\n2/4. {name} departures ({d['departures']}): evaluator {ci(d['evaluator_total'])} = model's own gap "
            f"{ci(d['evaluator_model_part'])} + outcome part {ci(d['evaluator_outcome_part'])}")
        log(f"     cross-fitted, base features {ci(d.get('crossfit_base'))}; with composition and history "
            f"{ci(d.get('crossfit_adjusted'))} ({d.get('crossfit_adjusted_trimmed', 0)} trimmed)")
    h = (r["adjusted"]["heterogeneity"] or {})
    if h:
        verdict = ("true pair effects differ by more than 1 pp after adjustment: build the pregame effect model"
                   if h["tau"] > 0.01 else
                   "true pair effects differ by 1 pp or less after adjustment: ship the four labels with "
                   "'no clear difference' as the default")
        log(f"\nDecision rule (memo, section 11): tau {100 * h['tau']:.2f} pp -> {verdict}. Check the negative controls "
            f"first: a clearly non-zero control means the adjustment is incomplete.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=str(ROOT / "data"), help="folder with the r1 files and settistics.sqlite")
    parser.add_argument("--r2-dir", help="folder with the r2 files, if not --data-dir")
    parser.add_argument("--db", help="main database (default: <data-dir>/settistics.sqlite)")
    parser.add_argument("--backend", default="xgb-cpu", choices=("auto", "xgb-cpu", "xgb-cuda"))
    parser.add_argument("--sample-games", type=int, help="use only this many games (quick trial)")
    parser.add_argument("--skip-departures", action="store_true", help="skip the r1/r2 policy refit (questions 2 and 4)")
    args = parser.parse_args()
    result = run(args.data_dir, args.r2_dir, args.db, recommender.resolve(args.backend), args.sample_games,
                 args.skip_departures)
    out = ROOT / "data" / "research" / "effects"
    out.mkdir(parents=True, exist_ok=True)
    (out / "effects.json").write_text(json.dumps(recommender.clean(result), indent=1, default=float), encoding="utf-8")
    print(f"\n-> {out / 'effects.json'}")


if __name__ == "__main__":
    main()
