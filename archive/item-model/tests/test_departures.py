"""Departure diagnostic: reads training rows only and reports the funnel and margin sweep."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@unittest.skipUnless(importlib.util.find_spec("xgboost"), "Optional model dependencies not installed")
class DepartureTests(unittest.TestCase):
    def test_training_rows_only_and_a_planted_effect_departs(self):
        import departures
        from test_recommender import synthetic, write_branches
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "b.sqlite"
            write_branches(src, synthetic(n_train=600, n_eval=50, effect=1.0, seed=3))
            rows = departures.load_training(src)
            self.assertEqual({r["split_role"] for r in rows}, {"train"})  # never a test row
            early, late = departures.time_split(rows)
            self.assertLess(max(r["started_at"] + r["duration_ms"] for r in early), min(r["started_at"] for r in late))
            out = departures.refit_diagnostics(rows, log=lambda *a: None)
            base = out["policies"]["base"]
            self.assertGreater(base["reasons"]["depart"], 0)
            self.assertGreater(base["route_gain_share"], 0)
            counts = [base["margins"][str(m)]["departures"] for m in departures.MARGINS]
            self.assertEqual(counts, sorted(counts))  # a smaller margin never departs less
            report = Path(tmp) / "r.json"
            report.write_text(json.dumps(dict(evaluation=dict(rows=10, matches=2, propensity={}, policies=dict(
                base=dict(reasons=dict(depart=1, below_preference_margin=9)))))))
            funnels = departures.report_funnels({"r1": report, "r2": Path(tmp) / "none.json"}, log=lambda *a: None)
            self.assertEqual(funnels, {"r1": {"base": {"depart": 1, "below_preference_margin": 9}}})


if __name__ == "__main__":
    unittest.main()
