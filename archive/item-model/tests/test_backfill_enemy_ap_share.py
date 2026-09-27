"""backfill_enemy_ap_share: adds enemy_ap_share to an existing branch_comparison table in place, matched
by (match_id, team_id), without disturbing rows it has no data for."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))


def participant(champion, team_id):
    return dict(championName=champion, teamId=team_id)


class BackfillTests(unittest.TestCase):
    def test_updates_every_row_by_its_own_match_and_team(self):
        from backfill_enemy_ap_share import build
        with tempfile.TemporaryDirectory() as tmp:
            main_path, branches_path = Path(tmp) / "main.sqlite", Path(tmp) / "branches.sqlite"
            main = sqlite3.connect(main_path)
            main.execute("CREATE TABLE matches (id TEXT PRIMARY KEY, detail TEXT)")
            m1 = dict(info=dict(participants=[participant("Anivia", 100)] * 5 + [participant("Caitlyn", 200)] * 5))
            m2 = dict(info=dict(participants=[participant("Caitlyn", 100)] * 5 + [participant("Anivia", 200)] * 5))
            main.executemany("INSERT INTO matches VALUES (?,?)", [("M1", json.dumps(m1)), ("M2", json.dumps(m2))])
            main.commit(), main.close()

            branches = sqlite3.connect(branches_path)
            branches.execute("CREATE TABLE branch_comparison (match_id TEXT, team_id INTEGER, row_id INTEGER)")
            # Two rows share (M1, 100); every (match_id, team_id) pair must land the same value.
            branches.executemany("INSERT INTO branch_comparison VALUES (?,?,?)",
                                 [("M1", 100, 1), ("M1", 100, 2), ("M1", 200, 3), ("M2", 100, 4), ("M2", 200, 5)])
            branches.commit(), branches.close()

            build(str(branches_path), str(main_path), batch=1, log=lambda *a, **k: None)

            out = sqlite3.connect(branches_path)
            rows = {r[0]: r[1] for r in out.execute("SELECT row_id, enemy_ap_share FROM branch_comparison")}
            out.close()
        self.assertAlmostEqual(rows[1], 0.05)  # M1 team 100: enemies are 5x Caitlyn (pure Marksman)
        self.assertAlmostEqual(rows[2], 0.05)  # the duplicate (M1, 100) row gets the same value
        self.assertAlmostEqual(rows[3], 0.85)  # M1 team 200: enemies are 5x Anivia (pure Mage)
        self.assertAlmostEqual(rows[4], 0.85)  # M2 team 100: enemies are 5x Anivia
        self.assertAlmostEqual(rows[5], 0.05)  # M2 team 200: enemies are 5x Caitlyn

    def test_is_idempotent_and_leaves_the_index_dropped(self):
        from backfill_enemy_ap_share import build
        with tempfile.TemporaryDirectory() as tmp:
            main_path, branches_path = Path(tmp) / "main.sqlite", Path(tmp) / "branches.sqlite"
            main = sqlite3.connect(main_path)
            main.execute("CREATE TABLE matches (id TEXT PRIMARY KEY, detail TEXT)")
            m1 = dict(info=dict(participants=[participant("Anivia", 100)] * 5 + [participant("Caitlyn", 200)] * 5))
            main.execute("INSERT INTO matches VALUES (?,?)", ("M1", json.dumps(m1)))
            main.commit(), main.close()
            branches = sqlite3.connect(branches_path)
            branches.execute("CREATE TABLE branch_comparison (match_id TEXT, team_id INTEGER, row_id INTEGER)")
            branches.execute("INSERT INTO branch_comparison VALUES ('M1', 100, 1)")
            branches.commit(), branches.close()

            build(str(branches_path), str(main_path), log=lambda *a, **k: None)
            build(str(branches_path), str(main_path), log=lambda *a, **k: None)  # must not error the second time

            out = sqlite3.connect(branches_path)
            value = out.execute("SELECT enemy_ap_share FROM branch_comparison").fetchone()[0]
            indexes = out.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
            out.close()
            self.assertAlmostEqual(value, 0.05)
            self.assertEqual(indexes, [])


if __name__ == "__main__":
    unittest.main()
