"""Doubly robust per-pair estimates: cross-fit nuisance models recover a planted effect and stay
null where there is none."""
import importlib.util
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

HOUR = 3_600_000
T0 = 1_000_000_000_000  # far enough before dr_pairs.CUTOFF_MS that 1200 hourly rows never cross it
# One pair with a true +5pp win-rate bump for route B, one with none.
PAIRS = [("slot1", "6000", "6001", "first_distinguishing_component", 0.05),
         ("slot2", "6002", "6003", "first_distinguishing_component", 0.0)]


def synthetic(n=1200, seed=3):
    import numpy as np
    from branches import OUT_COLUMNS
    rng = np.random.default_rng(seed)
    rows = []
    for m in range(n):
        stage, a, b, scope, effect = PAIRS[m % 2]
        lead = rng.normal()
        arm = int(rng.random() < 1 / (1 + math.exp(-lead)))
        p_win = min(max(0.5 + 0.1 * lead + effect * arm, 0.02), 0.98)
        win = int(rng.random() < p_win)
        row = dict.fromkeys(OUT_COLUMNS)
        row.update(fold="heldout", split_role="train", pair_id=f"heldout|Kai|BOTTOM|{stage}|16.19|{a}|{b}", scope=scope,
                   route_a=a, route_b=b, branch="ab"[arm], branch_route=(a, b)[arm], assigned_part=(a, b)[arm],
                   part_exclusive=1, match_id=f"EUW1_{m}", source="snowball", focus_source=0, sealed_period=0,
                   player_ref=f"p{m}", participant_id=1, team_id=100, region="EUW1", patch="16.19",
                   started_at=T0 + m * HOUR, champion="Kai", role="BOTTOM", opponent="Opp", stage=stage, stage_step=1,
                   t_ms=600_000, completed_before="[]", inventory_before="[]", boots_before="none",
                   snapshot_age_ms=20_000, current_gold_snapshot=900, budget_exact=0, dead_at_decision=0,
                   state_pre=json.dumps([10.0, 1000 * lead, 0, 300 * lead, 0, 0, 0, 0, 0, 0, 0, 0, 1.0]),
                   action_item=(a, b)[arm], action_kind="component", win=win, duration_ms=1_800_000,
                   dead_time_error_s=0.0, exposure_ms_5=300_000, complete_5=1, team_gold_lead_change_5=0.0,
                   takedowns_5=0, deaths_5=0, time_alive_ms_5=280_000, champion_damage_dealt_5=2000,
                   damage_exposure_ms_5=300_000)
        rows.append(row)
    return rows


def write_branches(path, rows):
    import sqlite3
    from branches import OUT_COLUMNS
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE branch_comparison ({', '.join(OUT_COLUMNS)})")
    db.executemany(f"INSERT INTO branch_comparison VALUES ({','.join('?' * len(OUT_COLUMNS))})",
                   [[r[c] for c in OUT_COLUMNS] for r in rows])
    db.commit()
    db.close()


class FoldOfTests(unittest.TestCase):
    def test_salted_fold_is_not_correlated_with_the_unsalted_one(self):
        # Regression test for the bug this replaced: zlib.crc32 is affine over GF(2), so for same-length
        # keys (nearly all match_ids: one fixed-width id per region), crc32(salt + key) % 2 turned out to
        # be a fixed function of crc32(key) % 2 rather than an independent hash -- every "EUW1_..." id
        # salted the same way landed in the same fold as its unsalted split, silently collapsing an inner
        # cross-fit onto the outer split it was supposed to be independent of.
        from dr_pairs import fold_of
        keys = [f"EUW1_{1_000_000_000 + i}" for i in range(2000)]  # same length, like real match_ids
        plain = [fold_of(k, 2) for k in keys]
        salted = [fold_of("inner" + k, 2) for k in keys]
        agree = sum(p == s for p, s in zip(plain, salted))
        self.assertTrue(400 < agree < 1600, f"salted and unsalted folds should look independent (~50% "
                        f"agreement expected by chance), got {agree}/2000 -- a hash with crc32's affine "
                        f"structure can make them collapse onto each other instead")


@unittest.skipUnless(importlib.util.find_spec("xgboost"), "Optional model dependencies not installed")
class DrPairsTests(unittest.TestCase):
    def test_recovers_a_planted_effect_and_stays_null_without_one(self):
        from dr_pairs import XGB, build
        rows = synthetic()
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "branches.sqlite"
            write_branches(src, rows)
            report = build(src, recommendations_path=None, params=dict(XGB, n_estimators=60, n_jobs=2))
        treat = report["table"]["heldout|Kai|BOTTOM|slot1|16.19|6000|6001"]["dr_effect"]
        null = report["table"]["heldout|Kai|BOTTOM|slot2|16.19|6002|6003"]["dr_effect"]
        self.assertGreater(treat["ci_low"], 0, "the planted +5pp effect should clear a 95% interval above 0")
        self.assertLess(treat["ci_high"] - treat["ci_low"], 0.2, "interval should be reasonably tight at this n")
        self.assertLessEqual(null["ci_low"], 0)
        self.assertGreaterEqual(null["ci_high"], 0)
        self.assertIsNotNone(report["null_calibration"]["z_std"], "the permutation check should find eligible pairs too")
        rep = report["split_half_replication"]
        self.assertGreaterEqual(rep["checked"], 0)
        self.assertGreaterEqual(rep["half0_survivors"], rep["checked"])
        self.assertGreaterEqual(rep["agree"], rep["replicated_at_p05"], "p<0.05 on half 1 implies sign agreement")

    def test_independent_halves_never_uses_the_other_halfs_labels(self):
        from dr_pairs import XGB, independent_halves
        from recommender import parse
        import numpy as np
        params = dict(XGB, n_estimators=30, n_jobs=2)
        parsed = [parse(dict(r)) for r in synthetic(n=400)]
        outer, win_oof, prop_oof = independent_halves(parsed, params=params, seed=1)
        corrupted = [dict(r) for r in parsed]
        for i, r in enumerate(corrupted):
            if outer[i] == 1:
                r["win"] = 1 - r["win"]
        outer2, win_oof2, prop_oof2 = independent_halves(corrupted, params=params, seed=1)
        np.testing.assert_array_equal(outer, outer2, "the outer split must not depend on the (corrupted) labels")
        idx0 = outer == 0
        self.assertTrue(np.isfinite(win_oof[idx0]).all(), "a NaN fallback here would let the equality check below "
                        "pass vacuously (assert_allclose treats NaN as equal to NaN by default)")
        self.assertTrue(np.isfinite(prop_oof[idx0]).all())
        np.testing.assert_allclose(win_oof[idx0], win_oof2[idx0], err_msg="half 0's scores must be unaffected "
                                    "by flipping half 1's win labels -- otherwise the halves are not independent")
        np.testing.assert_allclose(prop_oof[idx0], prop_oof2[idx0])

    def test_pair_offsets_uses_each_pairs_own_rate_and_falls_back_when_unseen(self):
        from dr_pairs import apply_offset, pair_offsets
        from recommender import parse
        rows = [parse(dict(r)) for r in synthetic(n=40)]
        arms = [r["arm"] for r in rows]
        offsets, pooled = pair_offsets(rows, arms)
        self.assertEqual(set(offsets), {r["pair_id"] for r in rows})
        unseen = dict(rows[0], pair_id="heldout|Kai|BOTTOM|slot9|16.19|9999|9998")
        self.assertEqual(apply_offset([unseen], offsets, pooled)[0], pooled,
                         "a pair absent from these rows must fall back to the pooled rate")
        self.assertEqual(apply_offset([rows[0]], offsets, pooled)[0], offsets[rows[0]["pair_id"]])

    def test_permutation_preserves_each_pairs_arm_counts(self):
        from dr_pairs import permute_arms_within_pair
        rows = synthetic(n=200)
        from recommender import parse
        parsed = [parse(dict(r)) for r in rows]
        permuted = permute_arms_within_pair(parsed, seed=7)
        import numpy as np
        real = np.asarray([r["arm"] for r in parsed])
        by_pair = {}
        for r, a in zip(parsed, permuted):
            by_pair.setdefault(r["pair_id"], []).append(a)
        for pid, arms in by_pair.items():
            real_arms = [r["arm"] for r in parsed if r["pair_id"] == pid]
            self.assertEqual(sorted(arms), sorted(real_arms), f"{pid}: permutation must not change the arm counts")
        self.assertFalse(np.array_equal(real, permuted), "a 200-row permutation should move at least one label")

    def test_benjamini_hochberg_keeps_only_pairs_the_correction_allows(self):
        from dr_pairs import benjamini_hochberg
        # 5 tests at alpha=0.05: BH thresholds are i/5*0.05 = .01, .02, .03, .04, .05
        pvals = [0.001, 0.03, 0.2, None, 0.049]
        sig = benjamini_hochberg(pvals, alpha=0.05)
        self.assertEqual(sig.tolist(), [True, False, False, False, False],
                         "only the smallest p-value clears its BH-adjusted threshold here")

    def test_future_start_row_is_refused(self):
        from dr_pairs import CUTOFF_MS, load_rows
        rows = synthetic(n=60)
        rows[0]["started_at"] = CUTOFF_MS
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "branches.sqlite"
            write_branches(src, rows)
            with self.assertRaises(ValueError):
                load_rows(src)

    def test_join_recommendations_matches_shipped_pair_id(self):
        from dr_pairs import join_recommendations
        table = {"heldout|Kai|BOTTOM|slot1|16.19|6000|6001": {}}
        doc = dict(entries=[dict(champion="Kai", role="BOTTOM", stage="slot1", patch="16.19",
                                 baselineRoute="6000", alternativeRoute="6001", modelLean="indistinguishable",
                                 predicted=dict(routeA=[0.5], routeB=[0.5]))])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recommendations.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            join_recommendations(table, path)
        self.assertEqual(table["heldout|Kai|BOTTOM|slot1|16.19|6000|6001"]["shipped"]["modelLean"], "indistinguishable")


if __name__ == "__main__":
    unittest.main()
