"""The vectorized reliability gate must decide exactly like the original pure-Python version."""
import importlib.util
from pathlib import Path
import random
import statistics
import sys
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))


def reference(obs, target, min_n=30, groups=20, threshold=0.4):
    """The pre-numpy implementation (placebo omitted: it depends on the random generator)."""
    def estimates(rows, n_min):
        by_item, by_slot = {}, {}
        for slot, item, _, v in rows:
            by_item.setdefault((slot, item), []).append(v)
            by_slot.setdefault(slot, []).append(v)
        ref = {s: statistics.fmean(v) for s, v in by_slot.items()}
        return {k: (statistics.fmean(v) - ref[k[0]], 1.96 * statistics.pstdev(v) / len(v) ** .5, len(v))
                for k, v in by_item.items() if len(v) >= n_min}

    def ranks(values):
        order = sorted(range(len(values)), key=values.__getitem__)
        out, i = [0.0] * len(values), 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2
            i = j + 1
        return out

    real = estimates(obs, min_n)
    half = [estimates([o for o in obs if zlib.crc32(o[2].encode()) % 2 == k], 1) for k in (0, 1)]
    pairs = [(half[0][g][0], half[1][g][0]) for g in real if g in half[0] and g in half[1]]
    a, b = zip(*pairs) if pairs else ((), ())
    r = statistics.correlation(ranks(list(a)), ranks(list(b))) if len(pairs) >= 3 and len(set(a)) > 1 and len(set(b)) > 1 else 0.0
    sd = statistics.pstdev([o[3] for o in obs]) if len(obs) > 1 else 0.0
    return {"splitHalfR": round(r, 3), "groups": len(pairs),
            "flagged": round(sum(abs(m) > h for m, h, _ in real.values()) / len(real), 4) if real else 0.0,
            "neededGames": int((1.96 * sd / target) ** 2) if sd else None,
            "pass": len(pairs) >= groups and r >= threshold}


@unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("scipy"), "numpy/scipy not installed")
class VectorizedReliabilityTests(unittest.TestCase):
    def observations(self, seed, items, games, signal):
        rng = random.Random(seed)
        obs = []
        for g in range(games):
            for slot in (("Sett", "TOP", "slot1"), ("Sett", "TOP", "slot2"), ("Garen", "TOP", "slot1")):
                item = rng.randrange(items)
                obs.append((slot, str(item), f"EUW1_{g}", rng.gauss(signal * (item % 3 - 1), 0.4)))
        return obs

    def test_matches_reference_on_noise_signal_and_sparse_data(self):
        from engine import _reliability
        for seed, items, games, signal in ((1, 40, 3000, 0.0), (2, 25, 4000, 0.2), (3, 60, 150, 0.1), (4, 3, 50, 0.3)):
            obs = self.observations(seed, items, games, signal)
            fast = _reliability(obs, 0.02, shuffles=3)
            slow = reference(obs, 0.02)
            self.assertEqual({k: fast[k] for k in slow}, slow, (seed, items, games, signal))
            self.assertGreaterEqual(fast["placebo"], 0.0)

    def test_empty_input(self):
        from engine import _reliability
        self.assertEqual(_reliability([], 0.02), {"splitHalfR": 0.0, "groups": 0, "flagged": 0.0, "placebo": 0.0,
                                                  "neededGames": None, "pass": False})

    def test_placebo_permutes_items_only_within_their_slot(self):
        import numpy as np
        from engine import _obs_arrays
        obs = self.observations(5, 10, 500, 0.0)
        slot, item, _, _, _ = _obs_arrays(obs)
        rng, grouped = np.random.default_rng(1), np.argsort(slot, kind="stable")
        fake = np.empty_like(item)
        fake[grouped] = item[np.lexsort((rng.random(len(item)), slot))]
        for s in np.unique(slot):
            self.assertEqual(sorted(item[slot == s]), sorted(fake[slot == s]))
        self.assertFalse((fake == item).all())


if __name__ == "__main__":
    unittest.main()
