"""Prospective round: fixed splits, the frozen game lists, the frozen-code lock, pass rules and the one-run ledger."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

MIN = 60_000
HAS_MODEL_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "sklearn"))


@unittest.skipUnless(HAS_MODEL_DEPS, "Optional model dependencies not installed")
class FixedCutoffTests(unittest.TestCase):
    def games(self, n):
        return [dict(match_id=f"EUW1_{i}", started_at=i * 10 * MIN, ended_at=i * 10 * MIN + 30 * MIN, focus_source=0)
                for i in range(n)]

    def test_fixed_cutoff_never_moves_when_games_are_added(self):
        from decisions import split_manifest
        cutoff = 60 * 10 * MIN
        roles = lambda ms: {m: r for f, r, m in ms if f == "heldout"}
        _, before, meta = split_manifest(self.games(80), folds=3, heldout_from=cutoff)
        _, after, _ = split_manifest(self.games(200), folds=3, heldout_from=cutoff)
        self.assertEqual(meta["heldout_start"], cutoff)
        self.assertIsNone(meta["heldout_fraction"])
        early, late = roles(before), roles(after)
        for mid, role in early.items():  # later collection only adds test games, never moves earlier ones
            self.assertEqual(late[mid], role)
        tested = [m for m, r in late.items() if r == "evaluate"]
        self.assertEqual(len(tested), 140)  # games 60..199
        trained = [int(m.split("_")[1]) for m, r in late.items() if r == "train"]
        self.assertEqual(max(trained), 56)  # games 57-59 were still running at the cutoff: in neither group

    def test_fixed_lists_train_on_development_games_and_test_later_collected_ones(self):
        from decisions import fixed_manifest
        games = self.games(10)
        games[8]["focus_source"] = 1
        games[1]["started_at"] = games[9]["started_at"] + MIN  # test games may interleave in time with training
        assignments, memberships, meta = fixed_manifest(games, {"EUW1_0", "EUW1_2", "EUW1_8"}, {"EUW1_1", "EUW1_3", "EUW1_8"} - {"EUW1_8"})
        self.assertEqual(sorted(memberships), [("heldout", "evaluate", "EUW1_1"), ("heldout", "evaluate", "EUW1_3"),
                                               ("heldout", "train", "EUW1_0"), ("heldout", "train", "EUW1_2"),
                                               ("heldout", "train_focus", "EUW1_8")])
        self.assertEqual(assignments["EUW1_5"], ("unused", None))
        self.assertEqual(meta["roles"], {"heldout:evaluate": 2, "heldout:train": 2, "heldout:train_focus": 1})
        _, members, _ = fixed_manifest(games, {"EUW1_0"}, {"EUW1_8"})  # a focus game never tests
        self.assertEqual(members, [("heldout", "train", "EUW1_0")])
        with self.assertRaises(ValueError):
            fixed_manifest(games, {"EUW1_0"}, {"EUW1_0"})

    def test_a_cutoff_after_every_game_is_an_error(self):
        from decisions import split_manifest
        with self.assertRaises(ValueError):
            split_manifest(self.games(50), folds=3, heldout_from=10**12)


@unittest.skipUnless(HAS_MODEL_DEPS, "Optional model dependencies not installed")
class RuleTests(unittest.TestCase):
    def cell(self, mean, low, high, departures=100):
        return dict(departures=departures, gain_per_departure=dict(mean=mean, ci_low=low, ci_high=high))

    def section(self, verdict):
        return dict(verdict=verdict, departures=500)

    def test_pass_needs_every_rule(self):
        from prospective import judge
        good = dict(mean=0.03, ci_low=0.01, ci_high=0.05)
        slices = {"region:EUW1": self.cell(0.02, -0.01, 0.05), "patch:16.19": self.cell(-0.2, -0.5, 0.1)}
        self.assertEqual(judge(self.section("supported_improvement"), good, 0.2, slices), ("pass", []))
        outcome, reasons = judge(self.section("no_supported_improvement"), good, 0.2, slices)
        self.assertEqual(outcome, "fail")
        self.assertIn("95% lower bound", reasons[0])
        self.assertEqual(judge(self.section("supported_improvement"), dict(good, mean=0.005), 0.2, slices)[0], "fail")
        self.assertEqual(judge(self.section("supported_improvement"), good, 0.01, slices)[0], "fail")

    def test_a_clearly_harmed_slice_fails_but_a_small_one_is_only_reported(self):
        from prospective import judge
        good = dict(mean=0.03, ci_low=0.01, ci_high=0.05)
        harmed = {"gold_state:behind": self.cell(-0.08, -0.12, -0.02)}
        outcome, reasons = judge(self.section("supported_improvement"), good, 0.2, harmed)
        self.assertEqual(outcome, "fail")
        self.assertIn("gold_state:behind", reasons[0])
        small = {"gold_state:behind": self.cell(-0.08, -0.12, -0.02, departures=49)}
        self.assertEqual(judge(self.section("supported_improvement"), good, 0.2, small)[0], "pass")

    def test_gate_shortfalls_are_inconclusive_not_failures(self):
        from prospective import judge
        good = dict(mean=0.03, ci_low=0.01, ci_high=0.05)
        for verdict in ("insufficient_evidence", "insufficient_overlap"):
            self.assertEqual(judge(self.section(verdict), good, 0.2, {})[0], "inconclusive")
        self.assertEqual(judge(dict(status="not_fitted"), good, 0.2, {})[0], "inconclusive")

    def test_gold_state_uses_the_team_gold_lead_before_the_purchase(self):
        from prospective import gold_state
        state = lambda lead: dict(state_pre=[10.0, lead] + [0.0] * 11)
        self.assertEqual([gold_state(state(v)) for v in (-1500, -1000, 0, 1000, 1001)],
                         ["behind", "even", "even", "even", "ahead"])


@unittest.skipUnless(HAS_MODEL_DEPS, "Optional model dependencies not installed")
class LockTests(unittest.TestCase):
    def test_line_endings_do_not_break_the_lock(self):
        from prospective import digest
        self.assertEqual(digest("a\r\nb\r\n"), digest("a\nb\n"))

    def test_changed_code_and_a_used_round_block_the_run(self):
        import prospective
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock.json"
            work = Path(tmp) / "work"
            frozen = dict(round=prospective.ROUND, design=prospective.DESIGN, hashes=prospective.fingerprint())
            lock.write_text(json.dumps(frozen))
            self.assertEqual(prospective.blockers(lock, work), [])
            frozen["hashes"]["pipeline/recommender.py"] = "0" * 64
            lock.write_text(json.dumps(frozen))
            self.assertEqual(prospective.blockers(lock, work), ["frozen code: pipeline/recommender.py changed since the freeze"])
            frozen["design"] = "something else"
            lock.write_text(json.dumps(frozen))
            self.assertIn("another round", prospective.blockers(lock, work)[0])
            prospective.write_ledger(dict(status="opened"), work)
            self.assertIn("already opened", prospective.blockers(Path(tmp) / "missing.json", work)[-1])

    def test_game_lists_come_from_the_recorded_development_dataset(self):
        import sqlite3
        import prospective
        with tempfile.TemporaryDirectory() as tmp:
            dev_db, main_db, work = Path(tmp) / "decisions.sqlite", Path(tmp) / "main.sqlite", Path(tmp) / "work"
            con = sqlite3.connect(dev_db)
            con.execute("CREATE TABLE split_matches (match_id TEXT, started_at INTEGER)")
            con.executemany("INSERT INTO split_matches VALUES (?,?)", [("EUW1_1", 5), ("EUW1_2", 7)])
            con.commit(); con.close()
            saved = prospective.DEVELOPMENT_GAMES, prospective.DEVELOPMENT_LAST_START_MS
            try:
                prospective.DEVELOPMENT_GAMES, prospective.DEVELOPMENT_LAST_START_MS = 2, 6
                with self.assertRaises(ValueError):  # a rebuilt development dataset is refused
                    prospective.development_games(work, dev_db)
                prospective.DEVELOPMENT_LAST_START_MS = 7
                self.assertEqual(prospective.development_games(work, dev_db), {"EUW1_1", "EUW1_2"})
                dev_db.unlink()  # the snapshot keeps the list even if the dataset is rebuilt or deleted
                self.assertEqual(prospective.development_games(work, dev_db), {"EUW1_1", "EUW1_2"})
            finally:
                prospective.DEVELOPMENT_GAMES, prospective.DEVELOPMENT_LAST_START_MS = saved
            con = sqlite3.connect(main_db)
            con.execute("CREATE TABLE matches (id TEXT, source TEXT, status TEXT, timeline TEXT, collected_at TEXT)")
            con.executemany("INSERT INTO matches VALUES (?,?,?,?,?)", [
                ("EUW1_1", "snowball", "done", "{}", "2026-09-25T04:00:00+00:00"),
                ("EUW1_2", "snowball", "done", "{}", "2026-09-25T05:00:00+00:00"),
                ("EUW1_3", "snowball", "done", "{}", "2026-09-25T04:30:00+00:00"),   # collected before the build: skipped
                ("EUW1_4", "snowball", "done", "{}", "2026-09-25T09:00:00+00:00"),   # test, whenever it was played
                ("EUW1_5", "focus kaisa:BOTTOM", "done", "{}", "2026-09-25T09:00:00+00:00"),
                ("EUW1_6", "snowball", "queued", None, None)])
            con.commit(); con.close()
            tested, built = prospective.test_games(main_db, {"EUW1_1", "EUW1_2"})
            self.assertEqual((tested, built), ({"EUW1_4"}, "2026-09-25T05:00:00+00:00"))

    def test_the_committed_lock_records_every_frozen_file(self):
        # r1 ran on 2026-09-25; later code changes are expected, so only the historical record is checked.
        import prospective
        lock = json.loads(prospective.LOCK.read_text(encoding="utf-8"))
        self.assertEqual((lock["round"], lock["design"]), (prospective.ROUND, prospective.DESIGN))
        self.assertEqual(set(lock["hashes"]), set(prospective.FROZEN_FILES) | {f"engine.{n}" for n in prospective.FROZEN_ENGINE})


@unittest.skipUnless(importlib.util.find_spec("xgboost"), "Optional model dependencies not installed")
class RoundTests(unittest.TestCase):
    def run_round(self, effect, seed):
        import prospective
        from test_recommender import small, synthetic, write_branches
        rows = synthetic(effect=effect, seed=seed)
        train = {r["match_id"] for r in rows if r["split_role"] == "train"}
        tested = {r["match_id"] for r in rows if r["split_role"] == "evaluate"}
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "branches.sqlite"
            write_branches(src, rows)
            work = Path(tmp) / "work"
            with self.assertRaises(ValueError):  # rows outside the frozen lists are refused before anything is read
                prospective.test(src, train, tested - {"EUW1_450"}, work=work, log=lambda *a: None, _small=small())
            report = prospective.test(src, train, tested, work=work, log=lambda *a: None, _small=small())
            ledger = prospective.read_ledger(work)
            with self.assertRaises(RuntimeError):  # the same games are never tested twice
                prospective.test(src, train, tested, work=work, log=lambda *a: None, _small=small())
            self.assertTrue((work / "report.json").exists())
        return report, ledger

    def test_planted_effect_passes_and_null_does_not(self):
        planted, ledger = self.run_round(effect=1.5, seed=3)
        self.assertEqual(planted["outcome"], "pass", planted["reasons"])
        self.assertEqual(ledger["status"], "done")
        self.assertEqual(ledger["outcome"], "pass")
        self.assertGreater(planted["gain_per_departure"]["mean"], 0.01)
        self.assertIn("gold_state:ahead", planted["slices"])
        self.assertTrue(planted["unit_test"])
        null, _ = self.run_round(effect=0.0, seed=4)
        self.assertNotEqual(null["outcome"], "pass")


if __name__ == "__main__":
    unittest.main()
