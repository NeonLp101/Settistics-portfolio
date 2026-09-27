"""engine.champion_ap_shares / enemy_ap_share: the enemy-team damage-mix context feature."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))


def participant(champion, team_id):
    return dict(championName=champion, teamId=team_id)


class ChampionApSharesTests(unittest.TestCase):
    def test_shares_are_bounded_and_reflect_known_classes(self):
        from engine import champion_ap_shares
        shares = champion_ap_shares()
        self.assertTrue(all(0 <= v <= 1 for v in shares.values()))
        self.assertGreater(shares["Anivia"], shares["Caitlyn"], "a pure Mage should score higher AP share "
                           "than a pure Marksman")
        self.assertAlmostEqual(shares["Anivia"], 0.85)  # single-tag Mage: exactly TAG_AP_SHARE["Mage"]
        self.assertAlmostEqual(shares["Caitlyn"], 0.05)  # single-tag Marksman

    def test_multi_tag_champion_averages_its_tags(self):
        from engine import TAG_AP_SHARE, champion_ap_shares
        shares = champion_ap_shares()
        # Malphite is Tank+Mage in the current catalog; if that ever changes the averaging is still
        # what's under test, so recompute the expectation from its actual tags rather than hardcoding it.
        import json
        from engine import ROOT
        champions = json.loads((ROOT / "public" / "ddragon" / "static.json").read_text(encoding="utf-8"))["champions"]
        tags = next(c["tags"] for c in champions if c["id"] == "Malphite")
        expected = sum(TAG_AP_SHARE.get(t, 0.5) for t in tags) / len(tags)
        self.assertAlmostEqual(shares["Malphite"], expected)


class EnemyApShareTests(unittest.TestCase):
    def test_averages_only_the_opposing_team(self):
        from engine import enemy_ap_share
        # Team 100 (the player's own team) must never affect the result; only team 200's 5 champions do.
        participants = ([participant("Anivia", 100)] * 5 + [participant("Caitlyn", 200)] * 5)
        got = enemy_ap_share(participants, team_id=100)
        self.assertAlmostEqual(got, 0.05)  # all 5 enemies are the pure-Marksman fixture
        got_other_side = enemy_ap_share(participants, team_id=200)
        self.assertAlmostEqual(got_other_side, 0.85)  # from team 200's perspective, the enemies are Anivia

    def test_mixed_enemy_team_averages_across_champions(self):
        from engine import enemy_ap_share
        participants = [participant(c, 100) for c in ["Ashe"] * 5] + \
            [participant(c, 200) for c in ["Anivia", "Anivia", "Caitlyn", "Caitlyn", "Caitlyn"]]
        got = enemy_ap_share(participants, team_id=100)
        self.assertAlmostEqual(got, (0.85 * 2 + 0.05 * 3) / 5)

    def test_none_when_the_enemy_team_is_not_five_champions_or_has_an_unknown_one(self):
        from engine import enemy_ap_share
        self.assertIsNone(enemy_ap_share([participant("Anivia", 100)] * 5 + [participant("Caitlyn", 200)] * 4, 100))
        self.assertIsNone(enemy_ap_share([participant("Anivia", 100)] * 5 + [participant("NotAChampion", 200)] * 5, 100))


if __name__ == "__main__":
    unittest.main()
