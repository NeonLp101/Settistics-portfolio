"""Generic purchase decisions: dated at observable purchases, no future intent, fixed-window outcomes."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
ITEMS = {"1001": {"name": "Boots", "tags": ["Boots"], "gold": {"total": 300}, "into": ["3047"]},
         "3047": {"name": "Plated Steelcaps", "tags": ["Boots"], "from": ["1001"], "into": ["9047"], "gold": {"total": 1200}},
         "9047": {"name": "Tier-3 Steelcaps", "tags": ["Boots"], "from": ["3047"], "gold": {"total": 1500}},
         "1036": {"name": "Long Sword", "gold": {"total": 350}, "into": ["6000", "6001"]},
         "6000": {"name": "Legendary A", "from": ["1036", "1036"], "gold": {"total": 2500}},
         "6001": {"name": "Legendary B", "from": ["1036"], "gold": {"total": 3000}},
         "1055": {"name": "Doran's Blade", "gold": {"total": 450}},
         "2003": {"name": "Health Potion", "tags": ["Consumable"], "gold": {"total": 50}},
         "3340": {"name": "Stealth Ward", "tags": ["Trinket"], "gold": {"total": 0}}}
MIN = 60000
# Player 1: starting items, a shared component twice, boots, two completions, then a component never finished.
P1_BUYS = [(20000, "1055"), (20000, "2003"), (20000, "3340"), (5 * MIN, "1036"), (6 * MIN, "1001"), (8 * MIN, "1036"),
           (12 * MIN, "6000"), (13 * MIN, "3047"), (19 * MIN, "6001"), (20 * MIN, "9047"), (22 * MIN, "1036")]


def fake_game(match_id="EUW1_1", buys=P1_BUYS, duration_min=25, start=1_000_000, extra_events=(), blue_wins=True,
              damage=True, game_end=True, late_damage=0):
    end = duration_min * MIN + 400  # GAME_END comes after the whole-second gameDuration
    players = [dict(participantId=i, teamId=100 if i <= 5 else 200, teamPosition=ROLES[(i - 1) % 5],
                    championName=f"C{i}", win=(i <= 5) == blue_wins, playerRef=f"p{i}",
                    totalTimeSpentDead=20 if i == 1 else 0) for i in range(1, 11)]
    match = {"metadata": {"matchId": match_id}, "info": dict(queueId=420, mapId=11, gameDuration=duration_min * 60,
             gameStartTimestamp=start, gameVersion="16.19.1", platformId="EUW1", participants=players)}
    events = [{"type": "ITEM_PURCHASED", "participantId": 1, "itemId": int(i), "timestamp": t} for t, i in buys]
    events += [{"type": "CHAMPION_KILL", "killerId": 1, "victimId": 6, "assistingParticipantIds": [2], "timestamp": 5 * MIN},
               {"type": "CHAMPION_KILL", "killerId": 1, "victimId": 6, "timestamp": int(6.5 * MIN)},
               {"type": "LEVEL_UP", "participantId": 1, "level": 7, "timestamp": int(8.5 * MIN)},
               {"type": "CHAMPION_KILL", "killerId": 6, "victimId": 1, "timestamp": 9 * MIN},  # level 7: 20 s dead
               {"type": "CHAMPION_KILL", "killerId": 2, "victimId": 7, "assistingParticipantIds": [1], "timestamp": 11 * MIN},
               # Player 2 undoes a purchase that cannot be matched: its ledger is ineligible.
               {"type": "ITEM_UNDO", "participantId": 2, "beforeId": 6000, "afterId": 0, "timestamp": 7 * MIN}]
    if game_end:
        events.append({"type": "GAME_END", "timestamp": end})
    events += list(extra_events)
    frames = []
    for m in range(duration_min + 1):
        # Blue players gain 100 gold a minute more, player 1 another 50: team lead +550/min, lane lead +150/min.
        # Player 1 deals 100 champion damage and takes 50 a minute (plus late_damage a minute after 5:00).
        pf = {str(i): {"totalGold": 500 + m * (400 if i <= 5 else 300) + (50 * m if i == 1 else 0),
                       "currentGold": 100 + m, "xp": 100 * m, "level": 1 + m // 3} for i in range(1, 11)}
        if damage:
            for i in range(1, 11):
                pf[str(i)]["damageStats"] = {"totalDamageDoneToChampions": (100 if i == 1 else 10) * m + (late_damage * max(0, m - 5) if i == 1 else 0),
                                             "totalDamageTaken": (50 if i == 1 else 10) * m}
        ts = end if m == duration_min else m * MIN
        frames.append({"timestamp": ts, "participantFrames": pf,
                       "events": [e for e in events if m * MIN <= e["timestamp"] < (m + 1) * MIN]})
    return match, {"metadata": {"matchId": match_id}, "info": {"frames": frames}}


def rows_for(pid, rows):
    return [r for r in rows if r["participant_id"] == pid]


@unittest.skipUnless(importlib.util.find_spec("numpy"), "Optional model dependencies not installed")
class DecisionTests(unittest.TestCase):
    def decisions(self, **kw):
        from decisions import match_decisions
        match, timeline = fake_game(**kw)
        return match_decisions("EUW1_1", "snowball", match, timeline, ITEMS)

    def test_actions_are_dated_at_purchases_and_keep_non_completers(self):
        rows, skipped = self.decisions()
        got = [(r["stage"], r["stage_step"], r["action_item"], r["action_kind"], r["t_ms"] // MIN) for r in rows_for(1, rows)]
        self.assertEqual(got, [("slot1", 1, "1036", "component", 5), ("boots", 1, "1001", "boots_basic", 6),
                               ("slot1", 2, "1036", "component", 8), ("slot1", 3, "6000", "completion", 12),
                               ("boots", 2, "3047", "boots_upgrade", 13), ("slot2", 1, "6001", "completion", 19),
                               ("slot3", 1, "1036", "component", 22)])  # starting items, tier-3 boots: no rows
        self.assertEqual(skipped["uncertain ledger"], 1)
        self.assertEqual(rows_for(2, rows), [])

    def test_inventory_before_the_purchase_follows_recipes(self):
        by_time = {r["t_ms"] // MIN: r for r in rows_for(1, self.decisions()[0])}
        self.assertEqual(by_time[12]["inventory_before"], ["1001", "1036", "1036", "1055"])
        self.assertEqual(by_time[13]["boots_before"], "1001")
        self.assertEqual(by_time[19]["inventory_before"], ["1055", "3047", "6000"])  # swords and basic boots used up
        self.assertEqual(by_time[19]["completed_before"], ["6000"])
        self.assertEqual(by_time[22]["boots_before"], "9047")
        self.assertEqual(by_time[22]["completed_before"], ["6000", "6001"])

    def test_columns_split_into_keys_context_action_and_outcomes(self):
        from decisions import ACTION, COLUMNS, CONTEXT, KEYS, OUTCOME
        self.assertEqual(len(COLUMNS), len(set(COLUMNS)))
        self.assertEqual(set(COLUMNS), set(KEYS) | set(CONTEXT) | set(ACTION) | set(OUTCOME))
        for future in ("win", "duration_ms", "dead_time_error_s", "damage_lag_ms"):
            self.assertIn(future, OUTCOME)
        unscored = {"win_chance_pre"} | {f"win_chance{s}_{w}" for s in ("", "_change") for w in (5, 10)}
        self.assertEqual(set(self.decisions()[0][0]), set(COLUMNS) - unscored)

    def test_no_future_information_in_decision_context(self):
        """Context and action of a decision are the same whatever happens later: result, game length, later
        purchases, kills, deaths, level-ups and damage. No target item is inferred."""
        from decisions import ACTION, CONTEXT
        full = rows_for(1, self.decisions()[0])
        other_future = [(t, i) for t, i in P1_BUYS if t <= 5 * MIN] + [(8 * MIN, "6001")]
        late = [{"type": "CHAMPION_KILL", "killerId": 1, "victimId": 6, "timestamp": 5 * MIN + 1000 * k} for k in range(1, 30)]
        late += [{"type": "LEVEL_UP", "participantId": 1, "level": 9, "timestamp": 5 * MIN + 500}]
        changed = rows_for(1, self.decisions(buys=other_future, extra_events=late, blue_wins=False, duration_min=31,
                                             late_damage=700)[0])
        cols = CONTEXT + ACTION
        self.assertEqual({k: full[0].get(k) for k in cols}, {k: changed[0].get(k) for k in cols})
        self.assertEqual(full[0]["builds_into"], 2)
        self.assertNotIn("target_item", full[0])
        for outcome in ("win", "duration_ms", "kills_5", "champion_damage_dealt_5"):  # the future only moves outcomes
            self.assertNotEqual(full[0][outcome], changed[0][outcome])

    def test_fixed_window_outcomes(self):
        from wpa import FEATURES
        first = rows_for(1, self.decisions()[0])[0]  # Long Sword at exactly 5:00
        # (5, 10]: the kill at 5:00 is before the window, 6:30 kill counts, 9:00 death counts.
        self.assertEqual((first["kills_5"], first["deaths_5"], first["takedowns_5"]), (1, 1, 1))
        # (5, 15]: plus the 11:00 assist.
        self.assertEqual((first["kills_10"], first["deaths_10"], first["takedowns_10"]), (1, 1, 2))
        # States use the last frame strictly before each moment: 4:00 -> 9:00 and 4:00 -> 14:00.
        self.assertEqual((first["team_gold_lead_change_5"], first["lane_gold_lead_change_5"]), (2750, 750))
        self.assertEqual((first["team_gold_lead_change_10"], first["lane_gold_lead_change_10"]), (5500, 1500))
        self.assertEqual(first["state_pre"][FEATURES.index("minute")], 5)
        self.assertEqual(first["state_pre"][FEATURES.index("kills")], 0)  # the 5:00 kill is not before 5:00
        self.assertEqual((first["exposure_ms_10"], first["complete_10"], first["budget_exact"]), (10 * MIN, 1, 0))
        self.assertEqual((first["snapshot_age_ms"], first["current_gold_snapshot"]), (MIN, 104))
        # Dead 9:00-9:20 (level 7 before 15 minutes: 20 s).
        self.assertEqual((first["time_alive_ms_5"], first["time_alive_ms_10"]), (5 * MIN - 20000, 10 * MIN - 20000))
        self.assertEqual((first["dead_at_decision"], first["dead_time_error_s"]), (0, 0))
        # Damage from the 5:00 frame (at or after the purchase) to the 10:00 and 15:00 frames.
        self.assertEqual(first["damage_lag_ms"], 0)
        self.assertEqual((first["champion_damage_dealt_5"], first["damage_taken_5"], first["damage_exposure_ms_5"]), (500, 250, 5 * MIN))
        self.assertEqual((first["champion_damage_dealt_10"], first["damage_taken_10"], first["damage_exposure_ms_10"]), (1000, 500, 10 * MIN))

    def test_windows_without_fights_are_kept_with_zeros(self):
        slot2 = next(r for r in rows_for(1, self.decisions()[0]) if r["t_ms"] == 19 * MIN)  # nothing happens in (19, 24]
        self.assertEqual((slot2["kills_5"], slot2["deaths_5"], slot2["takedowns_5"]), (0, 0, 0))
        self.assertEqual((slot2["time_alive_ms_5"], slot2["complete_5"]), (5 * MIN, 1))
        quiet = rows_for(1, self.decisions(late_damage=-100)[0])  # player 1 deals no damage after 5:00
        self.assertEqual(next(r for r in quiet if r["t_ms"] == 19 * MIN)["champion_damage_dealt_5"], 0)

    def test_purchase_while_dead(self):
        rows = rows_for(1, self.decisions(buys=[(20000, "1055"), (9 * MIN + 5000, "1036")])[0])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["dead_at_decision"], r["deaths_5"]), (1, 0))  # the death is before the window
        self.assertEqual(r["time_alive_ms_5"], 5 * MIN - 15000)  # dead until 9:20
        # The requested window ends at 14:05. The 15:00 frame would contain future damage.
        self.assertEqual((r["damage_lag_ms"], r["damage_exposure_ms_5"]), (MIN - 5000, 4 * MIN))  # 10:00 -> 14:00
        self.assertEqual(r["champion_damage_dealt_5"], 400)

    def test_window_cut_by_game_end_records_exposure_and_final_state(self):
        # The game ends at GAME_END, 400 ms after the truncated gameDuration: a kill in that gap still counts,
        # and a death shortly before the end is cut at the end.
        tail = [{"type": "CHAMPION_KILL", "killerId": 6, "victimId": 1, "timestamp": 24 * MIN + 55000},
                {"type": "CHAMPION_KILL", "killerId": 1, "victimId": 6, "timestamp": 25 * MIN + 200}]
        last = rows_for(1, self.decisions(extra_events=tail)[0])[-1]  # 22:00 in a 25:00.4 game
        self.assertEqual((last["duration_ms"], last["exposure_ms_5"], last["complete_5"]), (25 * MIN + 400, 3 * MIN + 400, 0))
        self.assertIsNone(last["state_5"])
        self.assertEqual(last["team_gold_lead_change_5"], 550 * (25 - 21))  # to the final frame
        self.assertEqual((last["kills_5"], last["deaths_5"]), (1, 1))
        self.assertEqual(last["time_alive_ms_5"], 3 * MIN + 400 - 5400)
        self.assertEqual(last["dead_time_error_s"], 0)  # Riot's total leaves out a death still running at the end
        self.assertEqual((last["champion_damage_dealt_5"], last["damage_exposure_ms_5"]), (300, 3 * MIN + 400))

    def test_game_end_without_end_event_and_missing_damage(self):
        last = rows_for(1, self.decisions(game_end=False, damage=False)[0])[-1]
        self.assertEqual(last["duration_ms"], 25 * MIN + 400)  # the final frame, not the truncated gameDuration
        self.assertEqual((last["champion_damage_dealt_5"], last["damage_taken_5"], last["damage_lag_ms"]), (None, None, None))

    def test_respawn_timer(self):
        from decisions import respawn_ms
        self.assertEqual(respawn_ms(1, 5 * MIN), 10000)
        self.assertEqual(respawn_ms(7, 15 * MIN), 20000)
        self.assertAlmostEqual(respawn_ms(12, 16.26 * MIN), 37500 * (1 + 3 * 0.00425))
        self.assertAlmostEqual(respawn_ms(18, 35 * MIN), 52500 * (1 + 0.1275 + 10 * 0.003))
        self.assertAlmostEqual(respawn_ms(25, 70 * MIN), 52500 * 1.5)  # level and time factor capped

    def test_win_chance_change_with_frozen_model_and_terminal_result(self):
        import numpy as np
        from decisions import match_decisions
        from wpa import FEATURES
        col = FEATURES.index("team_gold")
        model = lambda X: 1 / (1 + np.exp(-np.asarray(X)[:, col] / 5000))
        match, timeline = fake_game()
        rows = rows_for(1, match_decisions("EUW1_1", "snowball", match, timeline, ITEMS, model)[0])
        first, last = rows[0], rows[-1]
        pre = 1 / (1 + np.exp(-2200 / 5000))  # team lead at the 4:00 frame
        self.assertAlmostEqual(first["win_chance_pre"], pre)
        self.assertAlmostEqual(first["win_chance_change_5"], 1 / (1 + np.exp(-4950 / 5000)) - pre)
        self.assertEqual(last["win_chance_5"], 1.0)  # window reaches the end: the result
        self.assertAlmostEqual(last["win_chance_change_5"], 1.0 - last["win_chance_pre"])

    @unittest.skipUnless(importlib.util.find_spec("xgboost"), "xgboost not installed")
    def test_frozen_win_model_round_trip(self):
        import numpy as np
        from xgboost import XGBClassifier
        from decisions import load_win_model
        from wpa import FEATURES
        rng = np.random.default_rng(0)
        X = rng.normal(size=(200, len(FEATURES)))
        model = XGBClassifier(n_estimators=5, max_depth=2).fit(X, (X[:, 1] > 0).astype(int))
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "win.json")
            model.save_model(path)
            np.testing.assert_allclose(load_win_model(path)(X[:10]), model.predict_proba(X[:10])[:, 1], rtol=1e-6)

    def build_db(self, tmp):
        from engine import SEALED_FROM_MS
        db_path = Path(tmp) / "main.sqlite"
        db = sqlite3.connect(db_path)
        db.execute("CREATE TABLE matches (id TEXT PRIMARY KEY, platform TEXT, source TEXT, detail TEXT, timeline TEXT, status TEXT)")
        db.execute("CREATE TABLE catalog (patch TEXT PRIMARY KEY, version TEXT, items TEXT)")
        db.execute("INSERT INTO catalog VALUES ('16.19', '16.19.1', ?)", (json.dumps(ITEMS),))
        for mid, source, start in [("EUW1_2", "snowball", SEALED_FROM_MS - 2 * 10**7), ("EUW1_1", None, SEALED_FROM_MS - 3 * 10**7),
                                   ("EUW1_3", "focus kaisa:BOTTOM", SEALED_FROM_MS - 10**7), ("EUW1_4", "snowball", SEALED_FROM_MS),
                                   ("EUW1_5", "focus kaisa:BOTTOM", SEALED_FROM_MS + 10**7)]:
            match, timeline = fake_game(mid, start=start)
            db.execute("INSERT INTO matches VALUES (?,?,?,?,?,'done')", (mid, "euw1", source, json.dumps(match), json.dumps(timeline)))
        db.execute("INSERT INTO matches VALUES ('EUW1_6','euw1','snowball','{}',NULL,'queued')")
        db.commit(); db.close()
        return db_path

    def test_build_general_selection_releases_sealed_period_and_keeps_provenance(self):
        from decisions import build
        from engine import SEALED_FROM_MS
        with tempfile.TemporaryDirectory() as tmp:
            db_path, out_path = self.build_db(tmp), Path(tmp) / "decisions.sqlite"
            total, _, meta = build(db_path, out_path, workers=1, folds=1, log=lambda *a: None)
            out = sqlite3.connect(out_path)
            got = out.execute("SELECT DISTINCT match_id, source, focus_source, sealed_period, region, patch, started_at "
                              "FROM purchase_decisions ORDER BY rowid").fetchall()
            self.assertEqual(got, [("EUW1_1", None, 0, 0, "EUW1", "16.19", SEALED_FROM_MS - 3 * 10**7),
                                   ("EUW1_2", "snowball", 0, 0, "EUW1", "16.19", SEALED_FROM_MS - 2 * 10**7),
                                   ("EUW1_4", "snowball", 0, 1, "EUW1", "16.19", SEALED_FROM_MS)])
            self.assertEqual(total, 21)
            stored = out.execute("SELECT inventory_before, win_chance_pre FROM purchase_decisions WHERE t_ms=? LIMIT 1", (19 * MIN,)).fetchone()
            self.assertEqual(json.loads(stored[0]), ["1055", "3047", "6000"])
            self.assertIsNone(stored[1])  # no frozen model given: states stored, win chance left empty
            self.assertEqual(out.execute("SELECT fold, role, match_id FROM split_folds ORDER BY fold, match_id").fetchall(),
                             [("1", "train", "EUW1_1"), ("1", "validate", "EUW1_2"),
                              ("heldout", "train", "EUW1_1"), ("heldout", "train", "EUW1_2"), ("heldout", "evaluate", "EUW1_4")])
            self.assertFalse(json.loads(out.execute("SELECT value FROM split_meta WHERE key='include_focus'").fetchone()[0]))
            out.close()

    def test_build_with_focus_adds_training_only_games_without_moving_splits(self):
        from decisions import build
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self.build_db(tmp)
            general, focus = Path(tmp) / "general.sqlite", Path(tmp) / "focus.sqlite"
            _, _, meta_general = build(db_path, general, workers=1, folds=1, log=lambda *a: None)
            total, _, meta_focus = build(db_path, focus, workers=1, folds=1, include_focus=True, log=lambda *a: None)
            self.assertEqual(total, 35)
            self.assertEqual((meta_general["heldout_start"], meta_general["block_starts"]),
                             (meta_focus["heldout_start"], meta_focus["block_starts"]))
            out = sqlite3.connect(focus)
            self.assertEqual(out.execute("SELECT match_id, period FROM split_matches WHERE focus_source=1 ORDER BY match_id").fetchall(),
                             [("EUW1_3", "development"), ("EUW1_5", "late_focus_unused")])
            self.assertEqual(out.execute("SELECT fold, role FROM split_folds WHERE match_id='EUW1_3'").fetchall(), [("heldout", "train_focus")])
            general_db = sqlite3.connect(general)
            general_roles = general_db.execute("SELECT * FROM split_folds ORDER BY 1, 3").fetchall()
            general_db.close()
            self.assertEqual(out.execute("SELECT * FROM split_folds WHERE role != 'train_focus' ORDER BY 1, 3").fetchall(), general_roles)
            # every decision row belongs to a match in the manifest, so all rows of a match share its split
            self.assertEqual(out.execute("SELECT COUNT(*) FROM purchase_decisions WHERE match_id NOT IN "
                                         "(SELECT match_id FROM split_matches)").fetchone()[0], 0)
            out.close()


class SplitTests(unittest.TestCase):
    def games(self, n_general=200, n_focus=60, seed=3):
        import random
        rng = random.Random(seed)
        out = []
        for i in range(n_general + n_focus):
            start = rng.randrange(0, 50 * 3600 * 1000)
            out.append(dict(match_id=f"EUW1_{i}", started_at=start, ended_at=start + rng.randrange(15, 45) * MIN,
                            focus_source=int(i >= n_general)))
        return out

    def test_forward_match_grouped_splits(self):
        from decisions import split_manifest
        games = self.games()
        by_id = {g["match_id"]: g for g in games}
        assignments, memberships, meta = split_manifest(games, heldout_fraction=0.2, folds=4)
        self.assertEqual(sorted(assignments), sorted(by_id))
        folds = {}
        for fold, role, mid in memberships:
            folds.setdefault(fold, {}).setdefault(role, []).append(by_id[mid])
        self.assertEqual(sorted(folds), ["1", "2", "3", "4", "heldout"])
        for fold, roles in folds.items():
            ids = [g["match_id"] for rs in roles.values() for g in rs]
            self.assertEqual(len(ids), len(set(ids)))  # one match, one role per fold
            evaluated = roles.get("evaluate" if fold == "heldout" else "validate")
            self.assertTrue(evaluated)
            self.assertTrue(all(not g["focus_source"] for g in evaluated))  # focus games never validate or evaluate
            trained = roles.get("train", []) + roles.get("train_focus", [])
            self.assertTrue(trained)
            # later games never train an earlier evaluation: every training game ended before it began
            self.assertLess(max(g["ended_at"] for g in trained), min(g["started_at"] for g in evaluated))
            self.assertTrue(all(g["focus_source"] for g in roles.get("train_focus", [])))
            self.assertTrue(all(not g["focus_source"] for g in roles.get("train", [])))
        heldout = folds["heldout"]["evaluate"]
        dev_last = max(g["started_at"] for g in games if assignments[g["match_id"]][0] == "development")
        self.assertLess(dev_last, min(g["started_at"] for g in heldout))
        self.assertEqual(len(heldout), 40)  # the latest 20% of general games
        used = {m for _, _, m in memberships}
        for g in games:  # focus games from the held-out period are never used
            if assignments[g["match_id"]][0] == "late_focus_unused":
                self.assertTrue(g["focus_source"])
                self.assertNotIn(g["match_id"], used)
        starts = [min(g["started_at"] for g in folds[str(k)]["validate"]) for k in range(1, 5)]
        self.assertEqual(starts, sorted(starts))  # validation blocks follow each other in time

    def test_training_game_still_running_at_the_boundary_is_embargoed(self):
        from decisions import split_manifest
        games = [dict(match_id=f"EUW1_{i}", started_at=i * 10 * MIN, ended_at=i * 10 * MIN + 5 * MIN, focus_source=0) for i in range(10)]
        games[3]["ended_at"] = games[4]["started_at"] + MIN  # still running when the next block starts
        _, memberships, meta = split_manifest(games, heldout_fraction=0.2, folds=3)
        self.assertEqual(meta["block_starts"], [20 * MIN, 40 * MIN, 60 * MIN])
        fold2 = {(r, m) for f, r, m in memberships if f == "2"}
        self.assertNotIn(("train", "EUW1_3"), fold2)
        self.assertIn(("train", "EUW1_2"), fold2)
        self.assertIn(("train", "EUW1_3"), {(r, m) for f, r, m in memberships if f == "3"})

    def test_focus_games_do_not_move_boundaries(self):
        from decisions import split_manifest
        games = self.games()
        _, general_members, general_meta = split_manifest([g for g in games if not g["focus_source"]])
        _, members, meta = split_manifest(games)
        self.assertEqual((general_meta["heldout_start"], general_meta["block_starts"]), (meta["heldout_start"], meta["block_starts"]))
        self.assertEqual(sorted(general_members), sorted(m for m in members if m[1] != "train_focus"))

    def test_too_few_games_is_an_error(self):
        from decisions import split_manifest
        with self.assertRaises(ValueError):
            split_manifest(self.games(n_general=4, n_focus=0), folds=4)


if __name__ == "__main__":
    unittest.main()
