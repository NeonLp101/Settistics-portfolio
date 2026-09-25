import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))


@unittest.skipUnless(importlib.util.find_spec("sklearn"), "Optional model dependencies not installed")
class ItemResearchTests(unittest.TestCase):
    def test_uses_only_the_matching_pre_purchase_snapshot(self):
        from item_research import decision_rows

        row = {"champion": "Sett", "opponent": "Garen", "role": "TOP", "ledgerUncertain": False,
               "build": [("6631", "Stridebreaker", 12.0)], "win": 1,
               "features": {"champion": "Sett"}, "startedAt": 123, "matchId": "m", "playerId": "p",
               "purchases": [
                   {"item": 1001, "time": 60000, "pre": {"snapshotAvailable": False}},
                   {"item": 6631, "time": 720000, "pre": {"snapshotAvailable": True,
                    "snapshotAgeMs": 20000, "currentGoldSnapshot": 1400,
                    "levelSnapshot": 8, "teamGoldDifferenceSnapshot": -250}},
               ]}
        rows = list(decision_rows([row], "Sett", "TOP", 1, "6631", "3153", "Garen"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["features"]["teamGoldDifferenceSnapshot"], -250)
        self.assertEqual(rows[0]["features"]["purchaseMinute"], 12)
        self.assertEqual(list(decision_rows([{**row, "ledgerUncertain": True}], "Sett", "TOP", 1,
                                            "6631", "3153")), [])


if __name__ == "__main__":
    unittest.main()
