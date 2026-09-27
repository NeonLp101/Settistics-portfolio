"""retrain_with_ap_feature: loads only unsealed, non-focus training rows and fits the shared model set
with the enemy_ap_share feature included."""
import importlib.util
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

HOUR = 3_600_000
T0 = 1_000_000_000_000
PAIRS = [("slot1", "6000", "6001", "first_distinguishing_component")]


def synthetic(n=400, seed=1):
    import numpy as np
    from branches import OUT_COLUMNS
    rng = np.random.default_rng(seed)
    rows = []
    for m in range(n):
        stage, a, b, scope = PAIRS[0]
        lead = rng.normal()
        arm = int(rng.random() < 1 / (1 + math.exp(-lead)))
        win = int(rng.random() < 1 / (1 + math.exp(-0.5 * lead)))
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
                   damage_exposure_ms_5=300_000, enemy_ap_share=rng.uniform(0, 1))
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


@unittest.skipUnless(importlib.util.find_spec("xgboost"), "Optional model dependencies not installed")
class RetrainTests(unittest.TestCase):
    def test_future_start_row_is_refused(self):
        from retrain_with_ap_feature import CUTOFF_MS, load_train_rows
        rows = synthetic(n=20)
        rows[0]["started_at"] = CUTOFF_MS
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "branches.sqlite"
            write_branches(src, rows)
            with self.assertRaises(ValueError):
                load_train_rows(src)

    def test_focus_row_is_refused(self):
        from retrain_with_ap_feature import load_train_rows
        rows = synthetic(n=20)
        rows[0]["focus_source"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "branches.sqlite"
            write_branches(src, rows)
            with self.assertRaises(ValueError):
                load_train_rows(src)

    def test_build_fits_models_and_saves_a_private_manifest(self):
        from recommender import XGB, DECISION_XGB
        from retrain_with_ap_feature import build
        rows = synthetic()
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "branches.sqlite", Path(tmp) / "out"
            write_branches(src, rows)
            report = build(src, out, params=dict(XGB, n_estimators=30, n_jobs=2),
                          decision_params=dict(DECISION_XGB, n_estimators=30, n_jobs=2),
                          min_crossfit_rows=100, log=lambda *a, **k: None)
            self.assertEqual(report["fit"]["models"]["win"], "fitted")
            self.assertIn("enemy_ap_share", report["features"]["context"])
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("enemy_ap_share", manifest["features"]["context"])
            self.assertTrue((out / "models" / "win.json").exists())


if __name__ == "__main__":
    unittest.main()
