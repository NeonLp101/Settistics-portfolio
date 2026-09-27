"""Effect diagnostic: runs end to end on synthetic round files and recovers a planted route effect."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def round_files(tmp, effect=1.0):
    """r1 and r2 branch and decision files plus a main database, from the recommender's synthetic rows."""
    from test_recommender import synthetic, write_branches
    rows = synthetic(n_train=400, n_eval=150, effect=effect, seed=3)
    for r in rows:  # players recur across games, so history exists
        m = int(r["match_id"].split("_")[1])
        r["player_ref"] = f"p{r['participant_id']}-{m % 40}"
    train = [r for r in rows if r["split_role"] == "train"]
    r2_eval = [dict(r, match_id="R2_" + r["match_id"], started_at=r["started_at"] + 10**9)
               for r in synthetic(n_train=0, n_eval=150, effect=effect, seed=5)]
    for r in r2_eval:
        r["player_ref"] = f"p{r['participant_id']}-{int(r['match_id'].split('_')[-1]) % 40}"
    write_branches(tmp / "prospective-r1-branches.sqlite", rows)
    write_branches(tmp / "prospective-r2-branches.sqlite", train + r2_eval)
    for name, rs in (("r1", rows), ("r2", train + r2_eval)):
        con = sqlite3.connect(tmp / f"prospective-{name}-decisions.sqlite")
        con.execute("CREATE TABLE purchase_decisions (match_id, participant_id, player_ref, team_id, champion, role, "
                    "started_at, duration_ms, win, stage, t_ms, state_pre)")
        for r in rs:
            base = (r["match_id"], r["participant_id"], r["player_ref"], r["team_id"], r["champion"], r["role"],
                    r["started_at"], r["duration_ms"], r["win"], r["stage"])
            early = json.loads(r["state_pre"])
            early[1] -= 200
            con.execute("INSERT INTO purchase_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", base + (r["t_ms"] - 30_000, json.dumps(early)))
            con.execute("INSERT INTO purchase_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", base + (r["t_ms"], r["state_pre"]))
        con.commit(); con.close()
    con = sqlite3.connect(tmp / "settistics.sqlite")
    con.execute("CREATE TABLE matches (id TEXT PRIMARY KEY, detail TEXT)")
    for m in sorted({r["match_id"] for r in rows + r2_eval}):
        parts = [dict(teamId=100 if k < 5 else 200, championName=["Kai", "Ahri", "Garen", "Lux", "Jinx"][k % 5],
                      magicDamageDealtToChampions=1000 * (k % 3), physicalDamageDealtToChampions=2000,
                      trueDamageDealtToChampions=100, damageSelfMitigated=500 * (k % 2), totalDamageTaken=3000)
                 for k in range(10)]
        con.execute("INSERT INTO matches VALUES (?,?)", (m, json.dumps({"info": {"participants": parts}})))
    con.commit(); con.close()


@unittest.skipUnless(importlib.util.find_spec("xgboost"), "Optional model dependencies not installed")
class EffectTests(unittest.TestCase):
    def test_dersimonian_laird_finds_spread_only_when_it_exists(self):
        from effects import dersimonian_laird
        same = dersimonian_laird([0.01, 0.011, 0.009, 0.010], [0.01] * 4)
        self.assertEqual(same["tau"], 0.0)
        self.assertTrue(all(abs(s - same["pooled"]) < 1e-12 for s in same["shrunk"]))  # no spread: all shrink to the mean
        spread = dersimonian_laird([-0.05, 0.05, -0.04, 0.06], [0.005] * 4)
        self.assertGreater(spread["tau"], 0.03)

    def test_aipw_trims_poor_overlap(self):
        import numpy as np
        from effects import aipw
        phi = aipw(np.array([1.0, 0.0, 1.0]), np.array([1, 0, 1]), np.array([[0.4, 0.6]] * 3), np.array([0.5, 0.5, 0.99]))
        self.assertAlmostEqual(phi[0], 0.2 + 0.4 / 0.5)
        self.assertTrue(np.isnan(phi[2]))

    def test_history_counts_only_games_that_ended_before(self):
        from effects import history
        rows = dict(match_id=["m2"], player_ref=["p"], pair_id=["x"], branch=["b"], started_at=[100], duration_ms=[10],
                    champion=["Kai"])
        players = {("m1", 1): ("p", 100, "Kai", "BOTTOM", 0, 50, 1), ("m3", 1): ("p", 100, "Kai", "BOTTOM", 90, 150, 0),
                   ("m2", 1): ("p", 100, "Kai", "BOTTOM", 100, 110, 1)}
        h = history(rows, players)[0]
        self.assertEqual(list(h[:1]), [1])  # m3 was still running, m2 is this game
        self.assertAlmostEqual(h[1], 6 / 11)

    def test_end_to_end_on_synthetic_rounds(self):
        import effects
        effects.ROUNDS = 30
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            round_files(tmp)
            r = effects.run(tmp, backend="xgb-cpu", log=lambda *a: None)
        self.assertEqual(r["rounds"], ["dev", "r1", "r2"])
        self.assertAlmostEqual(r["timing"]["quantiles_s"]["0.5"], 30.0)
        self.assertGreater(r["coverage"]["share_with_5_earlier_games"], 0.5)
        for label in ("base", "adjusted"):
            self.assertGreater(r[label]["overall"]["mean"], 0.05)  # planted effect of B: about +20 pp
            self.assertIsNotNone(r[label]["heterogeneity"])
            self.assertIn("pre_decision_gold_change", r[label]["negative_controls"])
        self.assertIn("earlier_win_rate", r["base"]["negative_controls"])
        self.assertEqual(set(r["departures"]), {"r1", "r2"})
        d = r["departures"]["r1"]
        self.assertGreater(d["departures"], 0)
        parts = d["evaluator_model_part"]["mean"] + d["evaluator_outcome_part"]["mean"]
        self.assertAlmostEqual(parts, d["evaluator_total"]["mean"])


if __name__ == "__main__":
    unittest.main()
