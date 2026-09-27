"""Defensive-boots deep check (docs/item-policy-structural-fix.md section 12, pre-check C round 3 follow-up):
the last checks before the Plated Steelcaps (3047) vs. Mercury's Treads (3111) group can be pre-registered for
a sealed-cohort test. Confirms the pre-check-C result is not itself an artifact of the single, shared 2-fold
cross-fit, and that it survives a richer, real (not class-tag) measure of the enemy/ally damage mix.

    python pipeline/defensive_boots_deepcheck.py --backend xgb-cuda

1. Independent-halves oriented AIPW (dr_pairs.independent_halves): two match-grouped outer halves, each
   internally cross-fit using ONLY its own rows, so no row's score depends on a model that saw the other
   half's labels. Both halves should agree in sign; sizing uses the more conservative (smaller-magnitude) one,
   since this group was singled out precisely because it was the strongest survivor (winner's-curse concern).
2. Composition-enriched AIPW: adds real per-champion damage-mix features (mean magic-damage-dealt share and
   mean damage-mitigated share, from actual participant stats over a broad match sample, not the coarse
   Data-Dragon class-tag `enemy_ap_share` already in context) for both the enemy team and the player's own
   team (excluding the player), to the win and propensity models' features, then re-estimates the oriented
   effect. The point of this check: players choose Steelcaps/Mercs partly by looking at the enemy team's
   actual damage mix, which is exactly the kind of confounding a within-player check (C2', already run) cannot
   see, since the same player faces different compositions in different games.
3. Descriptive-only split by enemy magic-damage-share tercile: if the item effect is real, Mercury's Treads
   (magic resist) should look relatively better against magic-heavy teams. If Steelcaps looks best exactly
   against magic-heavy teams too, that is a red flag for leftover confounding, not corroboration.
4. Sizing: rows needed for an 80%-power detection of 1.1pp and of 0.75pp (the more conservative, winner's-
   curse-adjusted number), from the per-row AIPW score's standard deviation, converted to sealed games via
   this group's own rows-per-game rate.

Reads data/fulltrain-r3-branches.sqlite (via pipeline/boots_confound_check.load_rows) and data/settistics.sqlite
(team rosters and real damage stats), both read-only. Never opens the sealed cohort. Output: text summary and
data/research/defensive-boots-deepcheck/report.json.
"""
import argparse
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

from boots_confound_check import GROUPS, ci_str, load_rows as load_group_rows, pp
from dr_pairs import apply_offset, fold_of, independent_halves, pair_offsets
from engine import ROOT
from recommender import GATE, Encoder, XGB, clean, cluster_mean, dr_scores, fit as fit_model, private_dir

PROFILE_SAMPLE_MATCHES = 20000


def ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)


def build_champion_profiles(main_db, sample_size=PROFILE_SAMPLE_MATCHES, log=print):
    """{champion: (mean magic-damage-dealt share, mean damage-mitigated share)} from a broad, hash-sampled set
    of matches (independent of which matches are in the defensive-boots group), so this is not fit on the same
    games it will be used to explain."""
    con = ro(main_db)
    ids = [m for m, in con.execute("SELECT id FROM matches WHERE status='done'")]
    ids = sorted(ids, key=lambda m: fold_of(m, 1 << 30))[:sample_size]
    sums = defaultdict(lambda: np.zeros(3))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for champ, magic, phys, true, mitigated, taken in con.execute(
                "SELECT json_extract(p.value,'$.championName'), json_extract(p.value,'$.magicDamageDealtToChampions'), "
                "json_extract(p.value,'$.physicalDamageDealtToChampions'), json_extract(p.value,'$.trueDamageDealtToChampions'), "
                "json_extract(p.value,'$.damageSelfMitigated'), json_extract(p.value,'$.totalDamageTaken') "
                f"FROM matches m, json_each(m.detail,'$.info.participants') p WHERE m.id IN ({','.join('?' * len(chunk))})", chunk):
            dealt = (magic or 0) + (phys or 0) + (true or 0)
            absorbed = (mitigated or 0) + (taken or 0)
            sums[champ] += ((magic or 0) / dealt if dealt else 0.5, (mitigated or 0) / absorbed if absorbed else 0.5, 1)
    con.close()
    profiles = {c: (s[0] / s[2], s[1] / s[2]) for c, s in sums.items() if s[2] >= 20}
    log(f"champion damage profiles: {len(profiles)} champions from {len(ids):,} sampled matches")
    return profiles


def load_teams(main_db, match_ids, log=print):
    """{match: {team_id: [champion, ...]}} for exactly these matches."""
    con = ro(main_db)
    teams = defaultdict(lambda: defaultdict(list))
    ids = sorted(set(match_ids))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for m, team, champ in con.execute(
                "SELECT m.id, json_extract(p.value,'$.teamId'), json_extract(p.value,'$.championName') "
                f"FROM matches m, json_each(m.detail,'$.info.participants') p WHERE m.id IN ({','.join('?' * len(chunk))})", chunk):
            teams[m][team].append(champ)
    con.close()
    log(f"team rosters loaded for {len(teams):,} matches")
    return teams


def composition_features(rows, teams, profiles):
    """(n, 4): enemy magic-share, enemy mitigation-share, ally magic-share, ally mitigation-share (own team
    excluding self), NaN where the match or profile is missing."""
    out = np.full((len(rows), 4), np.nan)
    for i, r in enumerate(rows):
        roster = teams.get(r["match_id"])
        if not roster or r["team_id"] not in roster:
            continue
        own = [c for c in roster[r["team_id"]] if c != r["champion"]]
        enemy = [c for t, cs in roster.items() if t != r["team_id"] for c in cs]
        prof = lambda cs, k: np.mean([profiles[c][k] for c in cs if c in profiles]) if any(c in profiles for c in cs) else math.nan
        out[i] = [prof(enemy, 0), prof(enemy, 1), prof(own, 0), prof(own, 1)]
    return out


def frame_before(timeline, t_ms):
    best = None
    for f in timeline.get("info", {}).get("frames", []):
        ts = f.get("timestamp", 0)
        if ts < t_ms and (best is None or ts > best.get("timestamp", 0)):
            best = f
    return best


def load_participant_teams(main_db, match_ids, log=print):
    """{match: {participant_id (str): (team_id, champion)}} -- timeline participantFrames are keyed by
    participantId, not champion name, so this is needed to tell enemy from ally and to classify by damage
    profile."""
    con = ro(main_db)
    out = defaultdict(dict)
    ids = sorted(set(match_ids))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for m, pid, team, champ in con.execute(
                "SELECT m.id, json_extract(p.value,'$.participantId'), json_extract(p.value,'$.teamId'), "
                "json_extract(p.value,'$.championName') FROM matches m, json_each(m.detail,'$.info.participants') p "
                f"WHERE m.id IN ({','.join('?' * len(chunk))})", chunk):
            out[m][str(pid)] = (team, champ)
    con.close()
    log(f"participant-team map loaded for {len(out):,} matches")
    return out


def gold_weighted_ap_share(frame, participant_teams, own_team_id, profiles):
    """Enemy team's totalGold-weighted mean magic-damage-dealt share at this frame; None if incomplete."""
    pf = frame.get("participantFrames", {})
    gold, weighted = 0.0, 0.0
    n = 0
    for pid_str, (team, champ) in participant_teams.items():
        if team == own_team_id or champ not in profiles:
            continue
        g = pf.get(pid_str, {}).get("totalGold")
        if g is None:
            continue
        gold += g
        weighted += g * profiles[champ][0]
        n += 1
    return weighted / gold if n == 5 and gold > 0 else None


LIVE_THREAT_SAMPLE_MATCHES = 20000


def live_threat_features(rows, main_db, profiles, log=print, sample_matches=LIVE_THREAT_SAMPLE_MATCHES):
    """(n, 2): [gold-weighted enemy AP share at t_ms, its change over the preceding ~2.5 minutes] -- the
    enemy team's ACTUAL fed-ness by damage type at the moment of the decision, not just which champions are on
    the team (which the composition-enriched check already used). NaN where no timeline/frame is available,
    or where the match was not in the (hash-sampled, reproducible) subset -- parsing every match's full
    per-minute timeline for all ~116k matches is far slower than the end-of-game stats the other checks use, so
    this is descriptive/confirmatory on a large sample, not the full population."""
    by_match = defaultdict(list)
    for i, r in enumerate(rows):
        by_match[r["match_id"]].append(i)
    all_ids = list(by_match.keys())
    if sample_matches and len(all_ids) > sample_matches:
        all_ids = sorted(all_ids, key=lambda m: fold_of(m, 1 << 30))[:sample_matches]
        log(f"   sampling {sample_matches:,} of {len(by_match):,} matches for the live-threat check")
    participant_teams = load_participant_teams(main_db, all_ids, log)
    out = np.full((len(rows), 2), np.nan)
    con = ro(main_db)
    ids = all_ids
    for i in range(0, len(ids), 300):
        chunk = ids[i:i + 300]
        for match_id, timeline_json in con.execute(
                f"SELECT id, timeline FROM matches WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            if not timeline_json or match_id not in participant_teams:
                continue
            timeline = json.loads(timeline_json)
            pteams = participant_teams[match_id]
            for idx in by_match[match_id]:
                r = rows[idx]
                f_now, f_before = frame_before(timeline, r["t_ms"]), frame_before(timeline, r["t_ms"] - 150000)
                now = gold_weighted_ap_share(f_now, pteams, r["team_id"], profiles) if f_now else None
                before = gold_weighted_ap_share(f_before, pteams, r["team_id"], profiles) if f_before else None
                if now is not None:
                    out[idx, 0] = now
                if now is not None and before is not None:
                    out[idx, 1] = now - before
        log(f"   live-threat frames: {min(i + 300, len(ids)):,}/{len(ids):,} matches scanned")
    con.close()
    coverage = float(np.isfinite(out[:, 0]).mean())
    log(f"   live-threat coverage: {coverage:.0%} of rows")
    return out


def crossfit_nuisance_enriched(rows, extra, params, backend, seed=0, folds=2, fold_salt=""):
    """dr_pairs.crossfit_nuisance's body, with `extra` (n, k) extra columns appended to the context features
    for both the win and propensity models -- needed because dr_pairs.crossfit_nuisance itself only builds
    Encoder-only features and takes no extra-feature argument."""
    enc = Encoder(rows)
    ctx = np.hstack([enc.contexts(rows), extra])
    routes = enc.routes(rows)
    arms = np.asarray([r["arm"] for r in rows])
    win = np.asarray([r["win"] for r in rows], dtype=float)
    half = np.asarray([fold_of(fold_salt + r["match_id"], folds) for r in rows])
    win_oof, prop_oof = np.full((len(rows), 2), np.nan), np.full(len(rows), np.nan)
    for h in range(folds):
        train, score = half != h, half == h
        train_rows = [r for r, t in zip(rows, train) if t]
        scored_rows = [r for r, s in zip(rows, score) if s]
        win_model = fit_model(np.hstack([ctx[train], Encoder.actions(train_rows, arms[train])]), win[train],
                              "binary:logistic", params, backend, seed)
        offsets, pooled = pair_offsets(train_rows, arms[train])
        prop_model = fit_model(np.hstack([ctx[train], routes[train]]), arms[train].astype(float),
                               "binary:logistic", params, backend, seed, base_margin=apply_offset(train_rows, offsets, pooled))
        for arm in (0, 1):
            win_oof[score, arm] = win_model.predict(np.hstack([ctx[score], Encoder.actions(scored_rows, [arm] * len(scored_rows))]))
        prop_oof[score] = prop_model.predict(np.hstack([ctx[score], routes[score]]), base_margin=apply_offset(scored_rows, offsets, pooled))
    return win_oof, prop_oof, arms, win, half


def oriented_effect(win_oof, prop_oof, arms, win, rows, hi_item):
    g = dr_scores(win, arms, win_oof, prop_oof, GATE["weight_clip"])
    effect = g[:, 1] - g[:, 0]
    sign = np.asarray([1.0 if r["route_b"] == hi_item else -1.0 for r in rows])
    return effect * sign


def run(branches_path, main_db, backend="xgb-cpu", log=print):
    rows = load_group_rows(branches_path)["defensive_boots"]
    g = GROUPS["defensive_boots"]
    result = dict(rows=len(rows), definition=g)

    log("\n1. independent-halves oriented AIPW")
    outer, win_oof, prop_oof = independent_halves(rows, XGB, backend, seed=0)
    arms = np.asarray([r["arm"] for r in rows])
    win = np.asarray([r["win"] for r in rows], dtype=float)
    oriented = oriented_effect(win_oof, prop_oof, arms, win, rows, g["hi"])
    halves = {}
    for h in (0, 1):
        idx = np.flatnonzero(outer == h)
        halves[h] = cluster_mean(oriented[idx], [rows[i]["match_id"] for i in idx])
        log(f"   half {h}: {ci_str(halves[h]['mean'], halves[h]['se'])} ({halves[h]['rows']:,} rows, {halves[h]['clusters']:,} matches)")
    result["independent_halves"] = halves

    log("\n2. composition-enriched AIPW")
    match_ids = [r["match_id"] for r in rows]
    profiles = build_champion_profiles(main_db, log=log)
    teams = load_teams(main_db, match_ids, log=log)
    extra = composition_features(rows, teams, profiles)
    coverage = float(np.isfinite(extra).all(axis=1).mean())
    log(f"   composition feature coverage: {coverage:.0%} of rows")
    win_oof_e, prop_oof_e, arms_e, win_e, _ = crossfit_nuisance_enriched(rows, np.nan_to_num(extra, nan=-1.0), XGB, backend, seed=2, folds=5)
    oriented_e = oriented_effect(win_oof_e, prop_oof_e, arms_e, win_e, rows, g["hi"])
    enriched = cluster_mean(oriented_e, match_ids)
    baseline = cluster_mean(oriented, [r["match_id"] for r in rows])  # from step 1's pooled (both outer halves) scores
    log(f"   baseline (independent-halves, pooled): {ci_str(baseline['mean'], baseline['se'])}")
    log(f"   composition-enriched: {ci_str(enriched['mean'], enriched['se'])}")
    result["composition_enriched"] = dict(coverage=coverage, baseline_pooled=baseline, enriched=enriched)

    log("\n3. descriptive: split by enemy magic-damage-share tercile")
    enemy_magic = extra[:, 0]
    known = np.isfinite(enemy_magic)
    terciles = np.nanquantile(enemy_magic[known], [1 / 3, 2 / 3]) if known.sum() >= 30 else None
    tercile_result = []
    if terciles is not None:
        bins = np.digitize(enemy_magic, terciles)
        for b, label in enumerate(("low enemy AP", "mid enemy AP", "high enemy AP")):
            idx = np.flatnonzero(known & (bins == b))
            if len(idx) < 60:
                tercile_result.append(dict(label=label, rows=len(idx), effect=None))
                continue
            sub_rows = [rows[i] for i in idx]
            _, wo, po = independent_halves(sub_rows, XGB, backend, seed=3)
            ar = np.asarray([r["arm"] for r in sub_rows])
            wn = np.asarray([r["win"] for r in sub_rows], dtype=float)
            eff = cluster_mean(oriented_effect(wo, po, ar, wn, sub_rows, g["hi"]), [r["match_id"] for r in sub_rows])
            tercile_result.append(dict(label=label, rows=len(idx), effect=eff))
            log(f"   {label} ({len(idx):,} rows): {ci_str(eff['mean'], eff['se'])}")
    result["enemy_ap_tercile_descriptive"] = tercile_result

    log("\n3b. live-threat covariate: does the tercile crossover survive per-champion fed-ness?")
    from boots_confound_check import cluster_robust_diff
    live = live_threat_features(rows, main_db, profiles, log)
    live_cov = float(np.isfinite(live[:, 0]).mean())
    placebo = None
    change_known = np.isfinite(live[:, 1])
    if change_known.sum() >= 30:
        d = cluster_robust_diff(live[change_known, 1], arms[change_known].astype(float),
                                [rows[i]["match_id"] for i in np.flatnonzero(change_known)])
        placebo = dict(mean=d["coef"], se=d["se"], n=d["n"])
        log(f"   placebo (change in live AP-gold-share over preceding 2.5min, by arm): "
            f"{ci_str(d['coef'], d['se'])} (should be ~0)")
    live_tercile_result = []
    if terciles is not None:
        bins = np.digitize(enemy_magic, terciles)
        for b, label in enumerate(("low enemy AP", "mid enemy AP", "high enemy AP")):
            idx = np.flatnonzero(known & (bins == b) & np.isfinite(live[:, 0]))
            if len(idx) < 60:
                live_tercile_result.append(dict(label=label, rows=len(idx), effect=None))
                continue
            sub_rows = [rows[i] for i in idx]
            combined = np.hstack([np.nan_to_num(extra[idx], nan=-1.0), live[idx, 0:1]])
            wo, po, ar, wn, _ = crossfit_nuisance_enriched(sub_rows, combined, XGB, backend, seed=4, folds=5)
            eff = cluster_mean(oriented_effect(wo, po, ar, wn, sub_rows, g["hi"]), [r["match_id"] for r in sub_rows])
            live_tercile_result.append(dict(label=label, rows=len(idx), effect=eff))
            log(f"   {label}, with live-threat covariate ({len(idx):,} rows): {ci_str(eff['mean'], eff['se'])}")
    result["live_threat_check"] = dict(coverage=live_cov, placebo=placebo, tercile_with_live_threat=live_tercile_result)

    log("\n4. sizing")
    per_row_sd = float(np.nanstd(oriented))
    design_effect = 1.86  # matches vs. rows here are close to 1:1 already (2.1 rows/match); use a mild inflation
    rows_per_game = len(rows) / len({r["match_id"] for r in rows})
    sizing = {}
    for target_pp, label in ((0.011, "1.1pp (raw estimate)"), (0.0075, "0.75pp (winner's-curse-adjusted)")):
        n_rows = (1.96 + 0.84) ** 2 * (per_row_sd ** 2) / (target_pp ** 2) * design_effect
        n_games = n_rows / rows_per_game
        sizing[label] = dict(target_pp=target_pp, rows_needed=float(n_rows), games_needed=float(n_games))
        log(f"   {label}: ~{n_rows:,.0f} rows -> ~{n_games:,.0f} sealed games at {rows_per_game:.2f} rows/game "
            f"(before trimming losses)")
    result["sizing"] = dict(per_row_sd=per_row_sd, rows_per_game=rows_per_game, design_effect=design_effect, targets=sizing)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--branches", default=str(ROOT / "data" / "fulltrain-r3-branches.sqlite"))
    parser.add_argument("--db", default=str(ROOT / "data" / "settistics.sqlite"))
    parser.add_argument("--out-dir", default=str(ROOT / "data" / "research" / "defensive-boots-deepcheck"))
    parser.add_argument("--backend", default="xgb-cpu", choices=("auto", "xgb-cpu", "xgb-cuda"))
    args = parser.parse_args()
    result = run(args.branches, args.db, args.backend)
    out = private_dir(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(clean(result), indent=1, default=float), encoding="utf-8")
    print(f"\n-> {out / 'report.json'}")


if __name__ == "__main__":
    main()
