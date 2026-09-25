"""The scored match must be absent from both probability fitting stages."""
import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))


@unittest.skipUnless(importlib.util.find_spec("sklearn"), "Optional model dependencies not installed")
class WpaCrossFitTests(unittest.TestCase):
    def test_changing_scored_fold_outcomes_does_not_change_its_predictions(self):
        from wpa import FEATURES, cross_fit

        rows = []
        for i in range(500):
            row = {feature: 0.0 for feature in FEATURES}
            row.update(fold=i % 5, win=int((i // 5) % 3 != 0), minute=float(i % 17))
            rows.append(row)
        original = cross_fit(rows, [rows])[0]
        flipped = [{**row, "win": 1 - row["win"]} if row["fold"] == 0 else row
                   for row in rows]
        changed = cross_fit(flipped, [flipped])[0]
        for a, b, row in zip(original, changed, rows):
            if row["fold"] == 0:
                self.assertAlmostEqual(a, b, places=12)

    @unittest.skipUnless(importlib.util.find_spec('xgboost'), 'Optional GPU dependency not installed')
    def test_xgboost_keeps_scored_labels_out_of_both_fitting_stages(self):
        from wpa import FEATURES, cross_fit
        rows=[]
        for i in range(500):
            row={f:0.0 for f in FEATURES}
            row.update(fold=i%5,win=int((i//5)%3!=0),minute=float(i%17))
            rows.append(row)
        original=cross_fit(rows,[rows],backend='xgb-cpu')[0]
        flipped=[{**r,'win':1-r['win']} if r['fold']==0 else r for r in rows]
        changed=cross_fit(flipped,[flipped],backend='xgb-cpu')[0]
        for a,b,r in zip(original,changed,rows):
            if r['fold']==0:self.assertAlmostEqual(a,b,places=12)


if __name__ == "__main__":
    unittest.main()
