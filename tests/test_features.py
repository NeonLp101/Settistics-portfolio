"""Vectorized, cached WPA features must equal the reference Game.state and honour deletion."""
import importlib.util
import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
ITEMS = {"1001": {"name": "Boots", "gold": {"total": 300}, "into": ["3047"]},
         "3047": {"name": "Plated Steelcaps", "tags": ["Boots"], "from": ["1001"], "gold": {"total": 1200}},
         "6631": {"name": "Stridebreaker", "gold": {"total": 3300}}}


def fake_game(match_id, seed):
    rng = random.Random(seed)
    players = [dict(participantId=i, teamId=100 if i <= 5 else 200, teamPosition=ROLES[(i - 1) % 5],
                    championName=f"C{i}", win=i <= 5, playerRef=f"p:{match_id}:{i}") for i in range(1, 11)]
    match = {"metadata": {"matchId": match_id}, "info": dict(queueId=420, mapId=11, gameDuration=1500,
             gameStartTimestamp=1_000_000 + seed, gameVersion="16.18.1", platformId="EUW1", participants=players)}
    frames = []
    for m in range(26):
        events = [{"type": "CHAMPION_KILL", "victimId": rng.randint(1, 10), "timestamp": m * 60000 + rng.randint(0, 59999)}
                  for _ in range(rng.randint(0, 2))]
        if m == 8:
            events.append({"type": "ELITE_MONSTER_KILL", "monsterType": "DRAGON", "killerTeamId": 200, "timestamp": m * 60000 + 5})
        if m in (9, 14):
            events += [{"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 1001 if m == 9 else 6631, "timestamp": m * 60000 + 100}]
        if m == 10:
            events += [{"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 3047, "timestamp": m * 60000 + 100}]
        frames.append({"timestamp": m * 60000, "events": events, "participantFrames": {
            str(i): {"totalGold": 500 + m * rng.randint(250, 450), "xp": m * rng.randint(300, 500), "level": 1 + m // 2}
            for i in range(1, 11)}})
    return match, {"metadata": {"matchId": match_id}, "info": {"frames": frames}}


def main_db(path, games):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE matches (id TEXT PRIMARY KEY, platform TEXT, source TEXT, detail TEXT, timeline TEXT, status TEXT, collected_at TEXT)")
    db.execute("CREATE TABLE catalog (patch TEXT PRIMARY KEY, version TEXT, items TEXT)")
    db.execute("INSERT INTO catalog VALUES ('16.18', '16.18.1', ?)", (json.dumps(ITEMS),))
    for mid, (match, timeline) in games.items():
        db.execute("INSERT INTO matches VALUES (?,?,?,?,?,'done',NULL)", (mid, "euw1", "t", json.dumps(match), json.dumps(timeline)))
    db.commit()
    return db


@unittest.skipUnless(importlib.util.find_spec("sklearn"), "Optional model dependencies not installed")
class FeatureTests(unittest.TestCase):
    def test_vectorized_state_equals_reference_at_every_moment(self):
        import numpy as np
        from features import FastGame
        from wpa import STATE
        match, timeline = fake_game("EUW1_1", 7)
        game = FastGame(match, timeline)
        times = list(range(0, 1_500_000, 7919)) + [60000, 120000, 480000, 480005, 480006]
        for pid, opp in ((1, 6), (7, 2)):
            fast = game.states(pid, opp, times)
            slow = np.array([[game.state(pid, opp, t)[f] for f in STATE] for t in times])
            np.testing.assert_array_equal(fast, slow)

    def test_cache_reuses_prunes_deleted_matches_and_follows_catalog_changes(self):
        import features
        with tempfile.TemporaryDirectory() as tmp:
            db_path, cache = Path(tmp) / "main.sqlite", Path(tmp) / "cache.sqlite"
            db = main_db(db_path, {f"EUW1_{i}": fake_game(f"EUW1_{i}", i) for i in range(3)})
            logs = []
            first = features.load_matches(db_path, cache, workers=1, log=lambda *a, **k: logs.append(a[0]))
            self.assertEqual(sorted(first), ["EUW1_0", "EUW1_1", "EUW1_2"])
            self.assertTrue(first["EUW1_0"]["meta"]["hasCatalog"])
            self.assertIn("slot1", list(first["EUW1_0"]["dec_kind"]))
            features.load_matches(db_path, cache, workers=1, log=lambda *a, **k: logs.append(a[0]))
            self.assertIn("Features: 3 cached, 0 to compute", logs[-1])
            # Erasure or retention in the main database removes the cached copy, player refs included.
            db.execute("DELETE FROM matches WHERE id='EUW1_1'"); db.commit()
            after = features.load_matches(db_path, cache, workers=1, log=lambda *a, **k: logs.append(a[0]))
            self.assertNotIn("EUW1_1", after)
            c = sqlite3.connect(cache)
            self.assertEqual(c.execute("SELECT count(*) FROM matches WHERE match_id='EUW1_1'").fetchone()[0], 0)
            self.assertFalse(any("p:EUW1_1:" in str(features._unpack(r[0])["player_ref"]) for r in c.execute("SELECT arrays FROM matches")))
            c.close()
            # A corrected item catalog recomputes the affected patch.
            db.execute("UPDATE catalog SET version='16.18.2'"); db.commit(); db.close()
            logs.clear()
            features.load_matches(db_path, cache, workers=1, log=lambda *a, **k: logs.append(a[0]))
            self.assertEqual(logs[0], "Features: 0 cached, 2 to compute")

    def test_export_records_equal_direct_extraction(self):
        import engine
        import features
        games = {f"EUW1_{i}": fake_game(f"EUW1_{i}", i) for i in range(3)}
        with tempfile.TemporaryDirectory() as tmp:
            db_path, cache = Path(tmp) / "main.sqlite", Path(tmp) / "cache.sqlite"
            db = main_db(db_path, games)
            cached = list(features.load_records(db, db_path, cache, workers=1, log=lambda *a, **k: None))
            db.close()
        direct = [r for mid in sorted(games) for r in engine.extract(*games[mid], ITEMS)]
        self.assertTrue(direct and any(r["build"] for r in direct))
        self.assertEqual(cached, json.loads(json.dumps(direct)))  # tuples arrive as lists

    def test_pack_round_trips_every_array(self):
        import numpy as np
        import features
        match, timeline = fake_game("EUW1_9", 9)
        arrays = features.match_arrays("EUW1_9", match, timeline, ITEMS)
        back = features._unpack(features._pack(arrays))
        self.assertEqual(back["meta"]["region"], "EUW1")
        for key, value in arrays.items():
            if key != "meta":
                np.testing.assert_array_equal(back[key], value)
                self.assertEqual(back[key].dtype, value.dtype)

    def test_tables_keep_matches_without_catalog_out_of_wpa(self):
        import features
        with tempfile.TemporaryDirectory() as tmp:
            db_path, cache = Path(tmp) / "main.sqlite", Path(tmp) / "cache.sqlite"
            db = main_db(db_path, {"EUW1_0": fake_game("EUW1_0", 0)})
            db.execute("DELETE FROM catalog"); db.commit(); db.close()
            matches = features.load_matches(db_path, cache, workers=1, log=lambda *a, **k: None)
            self.assertFalse(matches["EUW1_0"]["meta"]["hasCatalog"])
            self.assertEqual(len(matches["EUW1_0"]["dec_X"]), 0)
            self.assertEqual(features.wpa_tables(matches)["games"], 0)

    def test_forget_drops_only_its_own_databases_feature_cache(self):
        import engine
        import features
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            main, other = Path(tmp) / "main.sqlite", Path(tmp) / "other.sqlite"
            cache, other_cache = features.cache_for(main), features.cache_for(other)
            self.assertEqual(cache.name, "main.features.sqlite")
            cache.write_bytes(b"x"); other_cache.write_bytes(b"x")
            (Path(tmp) / "ids.txt").write_text("nobody\n", encoding="utf-8")
            db = engine.connect(main)
            engine.forget(db, None, SimpleNamespace(puuid_file=str(Path(tmp) / "ids.txt")))
            db.close()
            self.assertFalse(cache.exists())
            self.assertTrue(other_cache.exists())

    def test_auto_backend_is_a_real_xgboost_device(self):
        if not importlib.util.find_spec("xgboost"):
            self.skipTest("optional XGBoost not installed")
        from backends import resolve
        self.assertIn(resolve("auto"), ("xgb-cuda", "xgb-cpu"))
        self.assertEqual(resolve("cpu"), "cpu")


if __name__ == "__main__":
    unittest.main()
