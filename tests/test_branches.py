"""Branch comparisons: train-only route pairs, first distinguishing purchase, noncompleters kept, no future items."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

MIN = 60000
# A (6000) = Caulfield (3133) + Long Sword; B (6001) = Pickaxe (1037) + Long Sword; C (6002) = Caulfield.
# Long Sword is shared; Pickaxe only builds into B (exclusive); Caulfield also feeds C (needs the fidelity gate).
ITEMS = {"1036": {"name": "Long Sword", "gold": {"total": 350}, "into": ["3133", "6000", "6001"]},
         "3133": {"name": "Caulfield", "from": ["1036"], "into": ["6000", "6002"], "gold": {"total": 1100}},
         "1037": {"name": "Pickaxe", "gold": {"total": 875}, "into": ["6001"]},
         "6000": {"name": "A", "from": ["3133", "1036"], "gold": {"total": 3000}},
         "6001": {"name": "B", "from": ["1037", "1036"], "gold": {"total": 3000}},
         "6002": {"name": "C", "from": ["3133"], "gold": {"total": 2800}},
         "1001": {"name": "Boots", "tags": ["Boots"], "gold": {"total": 300}, "into": ["3047", "3020"]},
         "3047": {"name": "Steelcaps", "tags": ["Boots"], "from": ["1001", "1029"], "gold": {"total": 1200}},
         "3020": {"name": "Sorcs", "tags": ["Boots"], "from": ["1001"], "gold": {"total": 1100}}}
CATALOGS = {"16.19": ITEMS}


def decision(mid, pid, stage, t_min, item, kind, inventory=(), completed=(), champion="Kai", role="BOTTOM", focus=0):
    from decisions import COLUMNS
    row = dict.fromkeys(COLUMNS)
    row.update(match_id=mid, source="focus kaisa:BOTTOM" if focus else "snowball", focus_source=focus, player_ref=f"{mid}-{pid}",
               participant_id=pid, team_id=100, region="EUW1", patch="16.19", champion=champion, role=role, stage=stage,
               stage_step=1, t_ms=int(t_min * MIN), action_item=item, action_kind=kind, inventory_before=json.dumps(list(inventory)),
               completed_before=json.dumps(list(completed)), boots_before="none", current_gold_snapshot=int(t_min * 100),
               state_pre=json.dumps([t_min, pid]), win=pid % 2, kills_5=int(t_min) + pid, deaths_5=0)
    return row


def route(mid, pid, parts, done, stage="slot1", start=5):
    """One player-stage: parts bought a minute apart, then the finished item done (if any)."""
    out, held = [], []
    for k, part in enumerate(parts):
        out.append(decision(mid, pid, stage, start + k, part, "component", inventory=held))
        held = held + [part]
    if done:
        out.append(decision(mid, pid, stage, start + len(parts), done, "completion", inventory=held))
    return out


def train_rows(mid):
    """Five A via Caulfield, four B via Pickaxe, two C via Caulfield: Caulfield leads to A 5/7 times."""
    rows = []
    for pid in range(1, 6):
        rows += route(mid, pid, ["1036", "3133"], "6000")
    for pid in range(6, 10):
        rows += route(mid, pid, ["1037"], "6001")
    rows += route(mid, 10, ["3133"], "6002") + route(mid, 11, ["3133"], "6002")
    rows += [dict(r, stage="slot2", t_ms=r["t_ms"] + 10 * MIN, completed_before='["6002"]') for r in rows]  # the same choice in slot 2
    for pid, boots in [(1, "3047"), (2, "3047"), (3, "3047"), (4, "3020"), (5, "3020"), (6, "3020")]:
        rows += [decision(mid, pid, "boots", 6, "1001", "boots_basic"), decision(mid, pid, "boots", 10, boots, "boots_upgrade")]
    return rows


def validate_rows(mid, later=True):
    rows = (route(mid, 1, ["1036", "3133"], None)  # never completes: kept on branch A
            + route(mid, 2, ["1037"], None)  # Pickaxe first: branch B whatever is completed later
            + route(mid, 3, ["1036"], None)  # shared part only: unassigned
            + route(mid, 4, [], "6000")  # the finished item bought outright: unassigned
            + [decision(mid, 5, "slot1", 5, "1037", "component", inventory=["3133"])]  # Caulfield carried from earlier
            + [decision(mid, 6, "slot2", 15, "1037", "component", completed=["6001"])]  # B already finished
            + [decision(mid, 7, "boots", 6, "1001", "boots_basic"), decision(mid, 7, "boots", 12, "3047", "boots_upgrade")]
            + [decision(mid, 8, "boots", 6, "1001", "boots_basic")])  # never upgrades
    if later:  # player 2's later purchases, all after the branch purchase
        rows += [decision(mid, 2, "slot1", 9, "3133", "component", inventory=["1037"]),
                 decision(mid, 2, "slot1", 11, "6000", "completion", inventory=["1037", "3133"])]
    return rows


def flood(mid, focus=0):
    """Many C completions: they may never move the train-only route choice."""
    rows = [r for pid in range(1, 11) for r in route(mid, pid, ["3133"], "6002")]
    if focus:  # plus one buyer per branch
        rows += route(mid, 11, ["3133"], None) + route(mid, 12, ["1037"], None)
    return [dict(r, focus_source=focus, source="focus kaisa:BOTTOM" if focus else "snowball") for r in rows]


def write_db(path, rows, folds):
    from decisions import COLUMNS
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE purchase_decisions ({', '.join(COLUMNS)})")
    db.executemany(f"INSERT INTO purchase_decisions VALUES ({','.join('?' * len(COLUMNS))})", [[r[c] for c in COLUMNS] for r in rows])
    db.execute("CREATE TABLE split_folds (fold TEXT, role TEXT, match_id TEXT, PRIMARY KEY (fold, match_id))")
    db.executemany("INSERT INTO split_folds VALUES (?,?,?)", folds)
    db.commit()
    db.close()


FOLDS = [("1", "train", "T1"), ("1", "validate", "V1"), ("1", "validate", "V2"), ("1", "train_focus", "F1"),
         ("heldout", "train", "T1"), ("heldout", "train", "V1"), ("heldout", "train", "V2"), ("heldout", "evaluate", "H1")]


@unittest.skipUnless(importlib.util.find_spec("numpy"), "Optional model dependencies not installed")
class BranchTests(unittest.TestCase):
    def run_build(self, rows=None, folds=FOLDS, **kw):
        from branches import build
        rows = rows if rows is not None else train_rows("T1") + validate_rows("V1") + flood("V2") + flood("F1", focus=1) + flood("H1")
        kw = dict(dict(min_support=3, fidelity_threshold=0.7, fidelity_min=5), **kw)
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "decisions.sqlite", Path(tmp) / "branches.sqlite"
            write_db(src, rows, folds)
            build(src, out, CATALOGS, {"16.19": "test"}, log=lambda *a: None, **kw)
            db = sqlite3.connect(out)
            db.row_factory = sqlite3.Row
            got = {t: [dict(r) for r in db.execute(f"SELECT * FROM {t}")]
                   for t in ("branch_comparison", "branch_pairs", "branch_parts", "branch_coverage", "branch_diagnostics", "branch_meta")}
            db.close()
        return got

    def test_distinguishing_parts_and_exclusivity(self):
        from branches import distinguishing_parts, recipe_tree
        self.assertEqual(recipe_tree("6000", ITEMS), {"3133", "1036"})
        self.assertEqual(distinguishing_parts("6000", "6001", ITEMS), {"3133": ("a", False), "1037": ("b", True)})
        self.assertEqual(distinguishing_parts("6000", "6002", ITEMS), {})  # identical recipe trees

    def test_pairs_come_from_train_games_only(self):
        got = self.run_build()
        slot = [p for p in got["branch_pairs"] if p["stage"] == "slot1"]
        self.assertEqual([(p["fold"], p["route_a"], p["support_a"], p["route_b"], p["support_b"], p["status"]) for p in slot],
                         [("1", "6000", 5, "6001", 4, "accepted")])  # C floods validate, focus and held-out games
        boots = [p for p in got["branch_pairs"] if p["stage"] == "boots"]
        self.assertEqual([(p["route_a"], p["route_b"], p["scope"], p["status"]) for p in boots],
                         [("3020", "3047", "boots_upgrade_purchase", "accepted")])
        # the same pairs if the non-train games are removed entirely
        alone = self.run_build(rows=train_rows("T1") + validate_rows("V1"))
        self.assertEqual(alone["branch_pairs"], got["branch_pairs"])
        self.assertEqual(alone["branch_parts"], got["branch_parts"])
        # explicit minimum support: B has only four completing train players
        self.assertEqual({p["status"] for p in self.run_build(min_support=5)["branch_pairs"]}, {"fewer_than_two_supported_routes"})

    def test_fidelity_gate_and_rejection_without_usable_parts(self):
        parts = {p["part"]: p for p in self.run_build()["branch_parts"]}
        self.assertEqual((parts["3133"]["exclusive"], parts["3133"]["train_first_buyers_completing"], parts["3133"]["usable"]),
                         (0, 7, 1))
        self.assertAlmostEqual(parts["3133"]["train_fidelity"], 5 / 7)
        self.assertEqual((parts["1037"]["exclusive"], parts["1037"]["usable"]), (1, 1))
        strict = self.run_build(fidelity_threshold=0.8)
        self.assertEqual([p["status"] for p in strict["branch_pairs"] if p["stage"] == "slot1"], ["no_part_passes_fidelity_gate"])
        self.assertFalse([r for r in strict["branch_comparison"] if r["stage"] == "slot1"])
        # A and C share every part C has: no distinguishing part for C, so the pair is rejected
        rows = train_rows("T1") + [r for pid in range(20, 26) for r in route("T1", pid, ["3133"], "6002")]
        rejected = self.run_build(rows=rows + validate_rows("V1"))
        self.assertEqual([(p["route_a"], p["route_b"], p["status"]) for p in rejected["branch_pairs"] if p["stage"] == "slot1"],
                         [("6002", "6000", "no_distinguishing_part")])

    def test_assignment_at_first_distinguishing_purchase_keeps_noncompleters(self):
        got = self.run_build()
        rows = {(r["participant_id"], r["stage"]): r for r in got["branch_comparison"] if r["match_id"] == "V1"}
        self.assertEqual(sorted(rows), [(1, "slot1"), (2, "slot1"), (7, "boots")])
        p1, p2, p7 = rows[(1, "slot1")], rows[(2, "slot1")], rows[(7, "boots")]
        self.assertEqual((p1["branch"], p1["branch_route"], p1["assigned_part"], p1["t_ms"], p1["part_exclusive"]),
                         ("a", "6000", "3133", 6 * MIN, 0))  # the Caulfield purchase; never completed
        self.assertAlmostEqual(p1["part_train_fidelity"], 5 / 7)
        self.assertEqual((p2["branch"], p2["assigned_part"], p2["t_ms"]), ("b", "1037", 5 * MIN))  # later completes A
        self.assertEqual((p7["branch_route"], p7["scope"], p7["t_ms"]), ("3047", "boots_upgrade_purchase", 12 * MIN))
        self.assertEqual((p1["split_role"], p1["current_gold_snapshot"], json.loads(p1["inventory_before"])), ("validate", 600, ["1036"]))
        coverage = {}
        for r in got["branch_coverage"]:
            if r["fold"] == "1" and r["split_role"] == "validate":
                coverage[r["status"]] = coverage.get(r["status"], 0) + r["player_stages"]
        self.assertEqual({k: coverage.get(k) for k in ("no_distinguishing_purchase", "completion_without_part", "carried_part",
                                                        "route_already_completed")}, dict(no_distinguishing_purchase=2,
                          completion_without_part=1, carried_part=1, route_already_completed=1))  # p3, p8; p4; p5; p6
        diag = {(r["split_role"], r["branch"]): r for r in got["branch_diagnostics"] if r["pair_id"].endswith("6000|6001")}
        # V2's ten Caulfield buyers are on branch A too, although all of them go on to build C: descriptive only
        slot1 = {k[1]: v for k, v in diag.items() if "|slot1|" in v["pair_id"]}
        self.assertEqual({k: slot1[k]["buyers"] for k in "ab"}, {"a": 11, "b": 1})
        self.assertEqual((slot1["a"]["completed_neither"], slot1["b"]["completed_other"]), (11, 1))

    def test_no_future_item_leakage(self):
        """A player's purchases after the branch purchase change neither the branch nor the row."""
        from decisions import ACTION, CONTEXT, KEYS, OUTCOME
        with_later = self.run_build()
        without = self.run_build(rows=train_rows("T1") + validate_rows("V1", later=False) + flood("V2") + flood("F1", focus=1))
        pick = lambda got: next(r for r in got["branch_comparison"] if r["match_id"] == "V1" and r["participant_id"] == 2)
        a, b = pick(with_later), pick(without)
        for col in ["branch", "branch_route", "assigned_part"] + KEYS + CONTEXT + ACTION + OUTCOME:
            self.assertEqual(a[col], b[col], col)
        self.assertNotIn("later_completed", a)
        self.assertEqual(set(a), set(json.loads(next(m["value"] for m in with_later["branch_meta"] if m["key"] == "column_groups"))["pair"])
                         | set(KEYS + CONTEXT + ACTION + OUTCOME))

    def test_group_disjointness_and_focus_never_evaluated(self):
        got = self.run_build(include_heldout=True)
        folds = {(f, m): r for f, r, m in FOLDS}
        seen = {}
        for r in got["branch_comparison"]:
            self.assertEqual(r["split_role"], folds[(r["fold"], r["match_id"])])  # the manifest's role, per match
            key = (r["fold"], r["match_id"], r["participant_id"], r["stage"])
            self.assertNotIn(key, seen)  # one row per player-stage and fold
            seen[key] = r
            if r["focus_source"]:
                self.assertEqual(r["split_role"], "train_focus")
            if r["split_role"] in ("validate", "evaluate"):
                self.assertEqual(r["focus_source"], 0)
        roles = {}
        for r in got["branch_comparison"]:
            roles.setdefault((r["fold"], r["match_id"]), set()).add(r["split_role"])
        self.assertTrue(all(len(v) == 1 for v in roles.values()))
        self.assertIn(("1", "train_focus"), {(r["fold"], r["split_role"]) for r in got["branch_comparison"]})
        # held-out rows use pairs chosen on the held-out group's own train games, and only with the flag
        self.assertIn("evaluate", {r["split_role"] for r in got["branch_coverage"]})
        self.assertNotIn("heldout", {r["fold"] for r in self.run_build()["branch_comparison"]})


if __name__ == "__main__":
    unittest.main()
