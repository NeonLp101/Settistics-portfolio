"""Experimental recommender: pre-action features, held-out-only fitting, one-way cross-fitting, DR formulas, fallbacks."""
import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

HOUR = 3_600_000
T0 = 1_790_000_000_000
# Two accepted pairs: a slot1 component pair and a boots pair (champion Kai, BOTTOM).
PAIRS = [("slot1", "6000", "6001", "first_distinguishing_component"), ("boots", "3020", "3047", "boots_upgrade_purchase")]


def synthetic(n_train=300, n_eval=200, effect=0.0, seed=1, focus=0, fold="heldout", players=4):
    """Branch rows with a known truth: P(B) rises with the gold lead, win depends on the lead plus effect * B (logit).

    Games start an hour apart and last 30 minutes, so every fit game has ended before the first evaluation game."""
    import numpy as np
    from branches import OUT_COLUMNS
    rng = np.random.default_rng(seed)
    eval_role = "evaluate" if fold == "heldout" else "validate"
    rows = []
    for m in range(focus + n_train + n_eval):
        role = "train_focus" if m < focus else "train" if m < focus + n_train else eval_role
        for pid in range(1, players + 1):
            stage, a, b, scope = PAIRS[pid % 2]
            lead = rng.normal()
            arm = int(rng.random() < 1 / (1 + math.exp(-lead)))
            win = int(rng.random() < 1 / (1 + math.exp(-(0.8 * lead + effect * arm))))
            row = dict.fromkeys(OUT_COLUMNS)
            row.update(fold=fold, split_role=role, pair_id=f"{fold}|Kai|BOTTOM|{stage}|16.19|{a}|{b}", scope=scope,
                       route_a=a, route_b=b, branch="ab"[arm], branch_route=(a, b)[arm], assigned_part=(a, b)[arm],
                       part_exclusive=1, match_id=f"EUW1_{m}", source="focus kai" if role == "train_focus" else "snowball",
                       focus_source=int(role == "train_focus"), sealed_period=0, player_ref=f"p{m}-{pid}",
                       participant_id=pid, team_id=100, region="EUW1", patch="16.19", started_at=T0 + m * HOUR,
                       champion="Kai", role="BOTTOM", opponent=f"Opp{pid}", stage=stage, stage_step=1, t_ms=600_000,
                       completed_before="[]", inventory_before="[]", boots_before="none", snapshot_age_ms=20_000,
                       current_gold_snapshot=900, budget_exact=0, dead_at_decision=0,
                       state_pre=json.dumps([10.0, 1000 * lead, 0, 300 * lead, 0, 0, 0, 0, 0, 0, 0, 0, 1.0]),
                       action_item=(a, b)[arm], action_kind="component", win=win, duration_ms=1_800_000,
                       dead_time_error_s=0.0, exposure_ms_5=300_000, complete_5=1,
                       team_gold_lead_change_5=500 * lead + 300 * arm + rng.normal(0, 200),
                       takedowns_5=int(rng.poisson(1 + arm)), deaths_5=int(rng.poisson(1)),
                       time_alive_ms_5=280_000 - 20_000 * int(rng.poisson(1)),
                       champion_damage_dealt_5=int(2000 + 800 * arm + rng.normal(0, 300)), damage_exposure_ms_5=300_000)
            rows.append(row)
    return rows


def write_branches(path, rows, pairs_fold="heldout"):
    from branches import OUT_COLUMNS
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE branch_comparison ({', '.join(OUT_COLUMNS)})")
    db.executemany(f"INSERT INTO branch_comparison VALUES ({','.join('?' * len(OUT_COLUMNS))})",
                   [[r[c] for c in OUT_COLUMNS] for r in rows])
    db.execute("CREATE TABLE branch_pairs (fold, champion, role, stage, patch, route_a, support_a, route_b, support_b, scope, status)")
    db.executemany("INSERT INTO branch_pairs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   [(pairs_fold, "Kai", "BOTTOM", s, "16.19", a, 40, b, 35, scope, "accepted") for s, a, b, scope in PAIRS])
    db.execute("CREATE TABLE branch_meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO branch_meta VALUES ('include_heldout', 'true')")
    db.commit()
    db.close()


def small():
    from recommender import DECISION_XGB, GATE, XGB
    return dict(params=dict(XGB, n_estimators=40, n_jobs=2), decision_params=dict(DECISION_XGB, n_estimators=40, n_jobs=2),
                gate=dict(GATE, min_eval_matches=50, min_departure_rows=20), min_crossfit_rows=100)


def parsed(rows):
    from recommender import parse
    return [parse(dict(r)) for r in rows]


@unittest.skipUnless(importlib.util.find_spec("xgboost"), "Optional model dependencies not installed")
class RecommenderTests(unittest.TestCase):
    def run_rec(self, rows, fold="heldout", save=False, **kw):
        from recommender import run
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "branches.sqlite"
            write_branches(src, rows, fold)
            report = run(src, Path(tmp) / "out", fold=fold, save=save, log=lambda *a: None, **dict(small(), **kw))
            files = {p.name: p.read_bytes() for p in (Path(tmp) / "out" / "models").glob("*.json")} if save else {}
        return report, files

    def test_features_exclude_outcomes_ids_and_future(self):
        from decisions import KEYS, OUTCOME
        from branches import OUT_COLUMNS
        from recommender import CONTEXT_NAMES, CONTEXT_SOURCES, Encoder
        self.assertFalse(set(CONTEXT_SOURCES) & set(KEYS + OUTCOME))
        self.assertFalse(set(CONTEXT_NAMES) & set(KEYS + OUTCOME + ["win", "duration_ms", "match_id", "player_ref"]))
        rows = parsed(synthetic(n_train=5, n_eval=1))
        enc = Encoder(rows)
        base = rows[0]
        changed = dict(base)
        for c in OUT_COLUMNS:  # every column that is not pre-action context (and not the pair identity) is scrambled
            if c not in CONTEXT_SOURCES:
                changed[c] = 12345 if c not in ("route_a", "route_b", "branch") else base[c]
        changed.update(win=1 - base["win"], win_chance_pre=0.99, assigned_part="9999", part_train_fidelity=0.1)
        self.assertEqual(enc.context(base), enc.context(changed))
        self.assertEqual(Encoder.routes([base]).tolist(), Encoder.routes([dict(base, branch="b" if base["branch"] == "a" else "a")]).tolist())

    def test_arm_is_an_action_swapped_with_context_fixed(self):
        import numpy as np
        from recommender import ACTION_NAMES, CONTEXT_NAMES, PROPENSITY_NAMES, Encoder, candidates, fit_models
        self.assertNotIn("arm_b", CONTEXT_NAMES + PROPENSITY_NAMES)
        rows = parsed(synthetic(n_train=60, n_eval=1))
        a0, a1 = Encoder.actions(rows[:1], [0]), Encoder.actions(rows[:1], [1])
        self.assertEqual((a0[0][ACTION_NAMES.index("arm_b")], a1[0][ACTION_NAMES.index("arm_b")]), (0.0, 1.0))
        self.assertEqual((a0[0][1], a1[0][1]), (float(rows[0]["route_a"]), float(rows[0]["route_b"])))
        fitted = fit_models(rows, params=small()["params"], decision_params=small()["decision_params"], min_crossfit_rows=50)
        flipped = [dict(r, branch="b" if r["branch"] == "a" else "a", arm=1 - r["arm"]) for r in rows[:20]]
        p, q = candidates(fitted, rows[:20]), candidates(fitted, flipped)
        for key in ("win", "base", "propensity"):  # the observed arm plays no part in candidate predictions
            np.testing.assert_array_equal(p[key], q[key])

    def test_split_uses_one_fold_and_refuses_leakage(self):
        from recommender import split
        rows = parsed(synthetic(n_train=10, n_eval=5, focus=3))
        fit, focus, ev = split(rows, "heldout")
        self.assertEqual({r["split_role"] for r in fit} | {r["focus_source"] for r in fit}, {"train", 0})
        self.assertEqual((len(focus), {r["split_role"] for r in ev}), (12, {"evaluate"}))
        with self.assertRaisesRegex(ValueError, "focus"):
            split(rows + [dict(rows[-1], match_id="EUW1_x", focus_source=1)], "heldout")
        late = dict(rows[0], match_id="EUW1_late", started_at=max(r["started_at"] for r in ev))
        with self.assertRaisesRegex(ValueError, "ends after"):
            split(rows + [late], "heldout")
        with self.assertRaisesRegex(ValueError, "both"):
            split(rows + [dict(ev[0], split_role="train", started_at=T0 - 5 * HOUR)], "heldout")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            split(rows + [dict(ev[0], split_role="validate")], "heldout")
        from recommender import load
        with tempfile.TemporaryDirectory() as tmp:  # a build without --include-heldout has no held-out fold
            write_branches(Path(tmp) / "b.sqlite", synthetic(n_train=10, n_eval=5, fold="2"), "2")
            with self.assertRaisesRegex(ValueError, "include-heldout"):
                load(Path(tmp) / "b.sqlite", "heldout")

    def test_evaluation_labels_and_other_rows_never_reach_fitting_or_choices(self):
        rows = synthetic(effect=1.0, focus=40)
        report, files = self.run_rec(rows, save=True)
        # flip every evaluation outcome, scramble focus rows and add a development fold: the fit must not move
        scrambled = [dict(r, win=1 - r["win"], takedowns_5=0, champion_damage_dealt_5=None) if r["split_role"] == "evaluate"
                     else dict(r, win=1 - r["win"]) if r["split_role"] == "train_focus" else r for r in rows]
        dev = [dict(r, fold="1", pair_id="1" + r["pair_id"][7:], split_role="validate" if r["split_role"] == "evaluate" else r["split_role"])
               for r in rows]
        other, other_files = self.run_rec(scrambled + dev, save=True)
        self.assertEqual(files, other_files)  # byte-identical models
        self.assertEqual(report["fit"], other["fit"])
        for name in ("base", "enriched"):
            self.assertEqual(report["evaluation"]["policies"][name]["reasons"], other["evaluation"]["policies"][name]["reasons"])
        self.assertEqual([s["base_policy_departures"] for s in report["support"]], [s["base_policy_departures"] for s in other["support"]])
        self.assertNotEqual(report["evaluation"]["observed"], other["evaluation"]["observed"])  # evaluation did read them
        self.assertNotEqual(report["sensitivity_focus_augmented"], other["sensitivity_focus_augmented"])

    def test_one_way_temporal_crossfit_uses_only_earlier_games(self):
        import numpy as np
        from recommender import Encoder, crossfit_aux, temporal_blocks
        rows = parsed(synthetic(n_train=150, n_eval=1))
        rows = [r for r in rows if r["split_role"] == "train"]
        blocks = temporal_blocks(rows, 3)
        self.assertEqual(blocks[0][1], [])
        for idx, earlier in blocks[1:]:
            start = min(rows[i]["started_at"] for i in idx)
            self.assertTrue(earlier and all(rows[i]["started_at"] + rows[i]["duration_ms"] < start for i in earlier))
            self.assertFalse(set(idx) & set(earlier))
        enc = Encoder(rows)
        X = np.hstack([enc.contexts(rows), enc.actions(rows, [r["arm"] for r in rows])])
        kw = dict(params=small()["params"], backend="xgb-cpu", seed=0, min_rows=100)
        oof, scored, usable, _ = crossfit_aux(rows, X, **kw)
        self.assertFalse(scored[blocks[0][0]].any())
        self.assertTrue(np.isnan(oof[blocks[0][0]]).all())
        self.assertEqual(usable, ["gold_lead_change_5", "takedowns_5", "deaths_5", "champion_damage_5", "time_alive_s_5"])
        last = set(blocks[2][0])  # changing the last block's outcomes leaves the earlier blocks' predictions alone
        changed = [dict(r, takedowns_5=9, team_gold_lead_change_5=-9999.0) if i in last else r for i, r in enumerate(rows)]
        oof2, _, _, _ = crossfit_aux(changed, X, **kw)
        np.testing.assert_array_equal(oof[blocks[1][0]], oof2[blocks[1][0]])
        # too few earlier rows anywhere: nothing is scored and the enriched model is not fitted
        _, scored3, usable3, status = crossfit_aux(rows, X, **dict(kw, min_rows=10_000))
        self.assertEqual((scored3.any(), usable3), (False, []))
        self.assertEqual({s["status"] for s in status}, {"too_few_earlier_rows_unscored"})

    def test_doubly_robust_and_cluster_formulas(self):
        import numpy as np
        from recommender import cluster_mean, dr_scores, propensity_diagnostics, GATE
        g = dr_scores([1, 0], [1, 0], [[0.4, 0.6], [0.5, 0.3]], [0.5, 0.25], clip=0.02)
        np.testing.assert_allclose(g, [[0.4, 0.6 + 0.4 / 0.5], [0.5 - 0.5 / 0.75, 0.3]])
        clipped = dr_scores([1], [1], [[0.5, 0.5]], [0.001], clip=0.02)  # weight 1/0.02, not 1/0.001
        self.assertAlmostEqual(clipped[0, 1], 0.5 + 0.5 / 0.02)
        est = cluster_mean([1, 2, 3, 4], ["m1", "m1", "m2", "m2"])
        self.assertAlmostEqual(est["mean"], 2.5)
        self.assertAlmostEqual(est["se"], 1.0)  # sqrt(2/1 * ((-2)^2 + 2^2)) / 4
        self.assertEqual((est["clusters"], est["rows"]), (2, 4))
        self.assertTrue(math.isnan(cluster_mean([1, 2], ["m", "m"])["se"]))
        d = propensity_diagnostics([0.01, 0.5, 0.99, 0.5], [0, 1, 1, 0], GATE)
        self.assertAlmostEqual(d["extreme_share"], 0.5)
        self.assertAlmostEqual(d["clipped_share"], 0.5)
        self.assertAlmostEqual(d["max_weight"], 2.0)  # the extreme rows took their likely arm

    def test_auxiliary_targets_keep_zero_fights_and_partial_windows(self):
        from recommender import AUX, aux_targets, aux_value
        base = parsed(synthetic(n_train=1, n_eval=1))[0]
        zero = dict(base, takedowns_5=0, deaths_5=0, champion_damage_dealt_5=0)
        self.assertEqual([aux_value(zero, n)[0] for n in ("takedowns_5", "deaths_5", "champion_damage_5")], [0.0, 0.0, 0.0])
        no_damage = dict(base, champion_damage_dealt_5=None)
        self.assertEqual(aux_value(no_damage, "champion_damage_5"), (None, "missing_damage"))
        self.assertIsNotNone(aux_value(no_damage, "takedowns_5")[0])
        self.assertEqual(aux_value(dict(base, damage_exposure_ms_5=0), "champion_damage_5"), (None, "missing_damage"))
        self.assertAlmostEqual(aux_value(dict(base, champion_damage_dealt_5=1000, damage_exposure_ms_5=240_000), "champion_damage_5")[0], 1000)
        partial = dict(base, complete_5=0, exposure_ms_5=120_000, damage_exposure_ms_5=120_000,
                       champion_damage_dealt_5=100, takedowns_5=0, deaths_5=0)
        self.assertTrue(all(aux_value(partial, n)[1] is None for n in AUX))
        unreliable = dict(base, dead_time_error_s=30.0)
        self.assertEqual(aux_value(unreliable, "time_alive_s_5"), (None, "dead_time_unreliable"))
        self.assertIsNotNone(aux_value(unreliable, "deaths_5")[0])
        mask, values, excluded = aux_targets([zero, no_damage, partial], "champion_damage_5")
        self.assertEqual((mask.tolist(), values.tolist(), dict(excluded)),
                         ([True, False, True], [0.0, 100.0], {"missing_damage": 1}))

    def test_deterministic_fallbacks(self):
        import numpy as np
        from recommender import GATE, choose, fit
        support = {("p", 0): 50, ("p", 1): 50, ("thin", 0): 50, ("thin", 1): 3}
        rows = [dict(pair_id=p) for p in ("unseen", "thin", "p", "p", "p", "p")]
        arms, reasons = choose(rows, [0.5, 0.5, math.nan, 0.5, 0.01, 0.5], [0.5, 0.5, 0.5, 0.95, 0.5, 0.5], support, GATE)
        self.assertEqual(reasons, ["unsupported_pair", "arm_support_below_min", "model_unavailable", "outside_overlap",
                                   "below_preference_margin", "depart"])
        self.assertEqual(arms.tolist(), [0, 0, 0, 0, 0, 1])
        one = fit(np.zeros((80, 2)), np.ones(80), "binary:logistic", small()["params"], "xgb-cpu", 0)
        self.assertEqual((one.status, one.predict(np.zeros((3, 2))).tolist()), ("single_class", [1.0, 1.0, 1.0]))
        self.assertEqual(fit(np.zeros((5, 2)), np.arange(5.0), "reg:squarederror", small()["params"], "xgb-cpu", 0).status, "too_few_rows")
        # a whole run is reproducible
        a, _ = self.run_rec(synthetic(n_train=120, n_eval=60))
        b, _ = self.run_rec(synthetic(n_train=120, n_eval=60))
        for r in (a, b):
            r.pop("created_at"), r["input"].pop("branches")  # the time and the temporary path
        self.assertEqual(a, b)

    def test_planted_effect_is_found_and_null_is_not_forced(self):
        planted, _ = self.run_rec(synthetic(effect=1.5, seed=3))
        base = planted["evaluation"]["policies"]["base"]
        self.assertEqual(planted["fit"]["crossfit"]["status"], "one_way_temporal")
        self.assertIn("gain_vs_baseline", planted["evaluation"]["policies"]["enriched"])
        self.assertIsNotNone(planted["evaluation"]["ablation"])
        self.assertGreater(base["departure_share"], 0.5)
        self.assertGreater(base["gain_vs_baseline"]["ci_low"], 0)
        self.assertEqual(base["verdict"], "supported_improvement")
        self.assertFalse(planted["headline_claims_allowed"])  # existing holdout is development, not prospective confirmation
        null, _ = self.run_rec(synthetic(effect=0.0, seed=4))
        self.assertNotEqual(null["evaluation"]["policies"]["base"]["verdict"], "supported_improvement")
        self.assertFalse(null["headline_claims_allowed"])
        strict, _ = self.run_rec(synthetic(effect=1.5, seed=3), gate=dict(small()["gate"], preference_margin=0.99))
        self.assertEqual(strict["evaluation"]["policies"]["base"]["departures"], 0)  # never a forced departure
        self.assertEqual(strict["evaluation"]["policies"]["base"]["gain_vs_baseline"]["mean"], 0)

    def test_development_fold_is_a_smoke_run_and_artifacts_stay_private(self):
        from engine import ROOT
        from recommender import private_dir
        smoke, files = self.run_rec(synthetic(effect=1.5, seed=3, fold="3"), fold="3", save=True)
        self.assertIn("smoke", smoke["evaluation_population"])
        self.assertFalse(smoke["headline_claims_allowed"])
        self.assertIn("win.json", files)
        for bad in (ROOT / "public" / "x", ROOT / "data" / "public", ROOT):
            with self.assertRaises(ValueError):
                private_dir(bad)


if __name__ == "__main__":
    unittest.main()
