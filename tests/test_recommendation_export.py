"""Public model research preview: saved models only, train contexts only, no identifiers, no claims."""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_recommender import small, synthetic, write_branches  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class RecommendationExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import recommender
        cls.tmp = tempfile.TemporaryDirectory()
        cls.branches = str(Path(cls.tmp.name) / "branches.sqlite")
        write_branches(cls.branches, synthetic(n_train=300, n_eval=150, effect=0.5))
        cls.model_dir = Path(cls.tmp.name) / "models-v0"
        cfg = small()
        recommender.run(cls.branches, cls.model_dir, backend="xgb-cpu", params=cfg["params"],
                        decision_params=cfg["decision_params"], gate=cfg["gate"],
                        min_crossfit_rows=cfg["min_crossfit_rows"], sensitivity=False, log=lambda *_: None)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def copy(self, name):
        """A private copy of the saved model directory and branch table, for tests that alter them."""
        out = Path(self.tmp.name) / name
        shutil.copytree(self.model_dir, out / "models-v0")
        shutil.copy(self.branches, out / "branches.sqlite")
        return out / "models-v0", str(out / "branches.sqlite")

    def test_reads_no_outcome_column_and_never_fits(self):
        import recommendation_export as rx
        from decisions import KEYS, OUTCOME
        self.assertFalse(set(rx.COLUMNS) & set(OUTCOME))
        self.assertEqual(set(rx.COLUMNS) & set(KEYS), {"match_id"})  # counted, never written
        from xgboost import XGBClassifier, XGBRegressor
        refuse = mock.Mock(side_effect=AssertionError("the exporter must not fit a model"))
        with mock.patch.object(XGBClassifier, "fit", refuse), mock.patch.object(XGBRegressor, "fit", refuse), \
                mock.patch("recommender.fit", refuse):
            doc = rx.build(self.model_dir, self.branches, now=0)
        self.assertEqual(len(doc["entries"]), 2)

    def test_labels_and_evaluation_rows_do_not_change_the_output(self):
        import recommendation_export as rx
        from decisions import OUTCOME
        before = rx.build(self.model_dir, self.branches, now=0)
        model_dir, branches = self.copy("stripped")
        db = sqlite3.connect(branches)
        db.execute("DELETE FROM branch_comparison WHERE split_role != 'train'")
        db.execute(f"UPDATE branch_comparison SET {', '.join(f'{c}=NULL' for c in OUTCOME)}")
        db.commit()
        db.close()
        after = rx.build(model_dir, branches, now=0)
        self.assertEqual(before["entries"], after["entries"])

    def test_output_is_public_research_preview_without_identifiers(self):
        import recommendation_export as rx
        doc = rx.build(self.model_dir, self.branches, now=0)
        text = json.dumps(doc)
        for private in ("EUW1_", "p1-", "Opp1", "heldout|", "match_id", "player_ref", "pair_id", "participant"):
            self.assertNotIn(private, text)
        self.assertEqual((doc["status"], doc["claimStatus"], doc["recommendation"], doc["routeLevelClaim"]),
                         ("research_preview", "no_confirmed_advantage", "observed_baseline_route_a", False))
        self.assertEqual(doc["pooling"], "all_matchups_regions_players")
        self.assertEqual(doc["sourcePatches"], ["16.19"])
        self.assertRegex(doc["model"]["artifactSha256"], r"^[a-f0-9]{64}$")
        self.assertFalse(doc["model"]["headlineClaimsAllowed"])
        self.assertEqual(doc["predictionFields"], [p for p, _, _ in rx.PREDICTIONS])
        for e in doc["entries"]:
            self.assertEqual(e["status"], "research_preview")
            self.assertNotIn("recommended", json.dumps(e).lower())
            self.assertTrue(e["supported"])
            self.assertEqual(e["support"]["routeA"] + e["support"]["routeB"], e["support"]["contexts"])
            self.assertEqual(e["support"]["contexts"], 300 * 2)  # train rows only: 2 players per pair per game
            for arm in (e["predicted"]["routeA"], e["predicted"]["routeB"]):
                self.assertEqual(len(arm), 6)
                self.assertTrue(0 <= arm[0] <= 1)
            self.assertIn(e["modelLean"], ("routeA", "routeB", "none"))
        boots = next(e for e in doc["entries"] if e["stage"] == "boots")
        self.assertEqual((boots["scope"], boots["baselineRoute"], boots["alternativeRoute"]), ("boots_upgrade_purchase", "3020", "3047"))

    def test_predictions_are_the_saved_models_averaged_over_both_arms(self):
        import numpy as np
        import recommendation_export as rx
        _, _, encoder, models = rx.load_models(self.model_dir)
        rows, _ = rx.load_train(self.branches)
        rows = [r for r in rows if r["stage"] == "slot1"]
        doc = rx.build(self.model_dir, self.branches, now=0)
        slot1 = next(e for e in doc["entries"] if e["stage"] == "slot1")
        ctx = encoder.contexts(rows)
        for arm, key in ((0, "routeA"), (1, "routeB")):
            X = np.hstack([ctx, encoder.actions(rows, [arm] * len(rows))])
            self.assertAlmostEqual(slot1["predicted"][key][0], float(models["win"].predict_proba(X)[:, 1].mean()), places=4)

    def test_unsupported_pair_has_no_predictions(self):
        import recommendation_export as rx
        model_dir, branches = self.copy("unsupported")
        manifest = json.loads((model_dir / "manifest.json").read_text())
        manifest["gate"]["min_arm_train_rows"] = 10_000
        (model_dir / "manifest.json").write_text(json.dumps(manifest))
        doc = rx.build(model_dir, branches, now=0)
        for e in doc["entries"]:
            self.assertEqual((e["supported"], e["predicted"], e["modelLean"]), (False, None, None))
            self.assertGreater(e["support"]["contexts"], 0)

    def test_refuses_mismatched_rows_claims_and_changed_artifacts(self):
        import recommendation_export as rx
        model_dir, branches = self.copy("mismatch")
        db = sqlite3.connect(branches)
        db.execute("DELETE FROM branch_comparison WHERE rowid = (SELECT min(rowid) FROM branch_comparison WHERE split_role='train')")
        db.commit()
        db.close()
        with self.assertRaisesRegex(ValueError, "fitted on"):
            rx.build(model_dir, branches)
        model_dir, branches = self.copy("claim")
        report = json.loads((model_dir / "report.json").read_text())
        report["evaluation"]["policies"]["base"]["verdict"] = "supported_improvement"
        (model_dir / "report.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "supported improvement"):
            rx.build(model_dir, branches)
        model_dir, _ = self.copy("hash")
        before = rx.artifact_identity(model_dir)
        with open(model_dir / "models" / "win.json", "a") as f:
            f.write(" ")
        self.assertNotEqual(before, rx.artifact_identity(model_dir))

    def test_public_check_rejects_identifiers(self):
        import recommendation_export as rx
        doc = rx.build(self.model_dir, self.branches, now=0)
        for bad in ({"matchId": "x"}, {"note": "EUW1_7512345"}, {"note": "heldout|Kai|BOTTOM"}):
            with self.assertRaises(ValueError):
                rx.check_public(dict(doc, extra=bad))
        with self.assertRaises(ValueError):
            rx.check_public(dict(doc, claimStatus="supported"))

    def test_site_schema_accepts_the_export(self):
        import recommendation_export as rx
        node = shutil.which("node")
        if not node:
            self.skipTest("node not on PATH")
        path = Path(self.tmp.name) / "recommendations.json"
        path.write_text(json.dumps(rx.build(self.model_dir, self.branches), separators=(",", ":")), encoding="utf-8")
        script = ("import {validateRecommendations} from './scripts/recommendation-schema.mjs';import {readFileSync} from 'node:fs';"
                  "validateRecommendations(JSON.parse(readFileSync(process.argv[1],'utf8')));")
        out = subprocess.run([node, "--input-type=module", "-e", script, str(path)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
