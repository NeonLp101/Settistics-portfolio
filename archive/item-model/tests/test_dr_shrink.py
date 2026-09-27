"""Empirical-Bayes shrinkage: the Sherman-Morrison log-likelihood and closed-form posteriors match
brute-force dense computations, the fit recovers known variances (and the true null), and the
checkpoint prefers shrinkage only when it actually helps."""
import math
import sys
import unittest
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))


def dense_loglik(ys, ses, tau_g2, tau_c2):
    """Reference: build Sigma = diag(se^2 + tau_c^2) + tau_g^2 * ones(n, n) densely and invert it."""
    n = len(ys)
    sigma = np.diag(np.asarray(ses) ** 2 + tau_c2) + tau_g2 * np.ones((n, n))
    _, logdet = np.linalg.slogdet(sigma)
    quad = np.asarray(ys) @ np.linalg.solve(sigma, np.asarray(ys))
    return -0.5 * (logdet + quad)


def dense_posterior(ys, ses, tau_g2, tau_c2):
    """Reference: E[mu_g|y], Var[mu_g|y], and E[theta_i|y], Var[theta_i|y] (theta_i = mu_g + delta_i) via
    the standard joint-Gaussian conditioning formula, with Sigma built densely."""
    ys, ses, n = np.asarray(ys), np.asarray(ses), len(ys)
    sigma = np.diag(ses ** 2 + tau_c2) + tau_g2 * np.ones((n, n))
    sigma_inv_y = np.linalg.solve(sigma, ys)
    cov_mu = tau_g2 * np.ones(n)
    m_g = cov_mu @ sigma_inv_y
    v_g = tau_g2 - cov_mu @ np.linalg.solve(sigma, cov_mu)
    means, variances = np.empty(n), np.empty(n)
    for i in range(n):
        cov_theta = tau_g2 * np.ones(n) + tau_c2 * np.eye(n)[i]
        means[i] = cov_theta @ sigma_inv_y
        variances[i] = (tau_g2 + tau_c2) - cov_theta @ np.linalg.solve(sigma, cov_theta)
    return m_g, v_g, means, variances


class GroupKeyTests(unittest.TestCase):
    def test_orientation_flips_by_item_id_not_by_route_a_b(self):
        from dr_shrink import group_key
        key_a, flip_a = group_key("heldout|Sett|TOP|slot1|16.19|3153|6631")  # route_a=3153 (lo), route_b=6631 (hi)
        key_b, flip_b = group_key("heldout|Aatrox|TOP|slot1|16.19|6631|3153")  # route_a=6631 (hi), route_b=3153 (lo)
        self.assertEqual(key_a, key_b, "the same two items in the same slot must land in the same group")
        self.assertTrue(flip_a)
        self.assertFalse(flip_b)


class LoglikTests(unittest.TestCase):
    def test_group_loglik_matches_dense_computation(self):
        from dr_shrink import group_loglik
        rng = np.random.default_rng(0)
        for _ in range(30):
            n = int(rng.integers(2, 7))
            ys = rng.normal(scale=0.05, size=n)
            ses = rng.uniform(0.01, 0.05, size=n)
            tau_g2 = float(rng.choice([0.0, 1e-4, 0.001, 0.01]))
            tau_c2 = float(rng.choice([0.0, 1e-4, 0.002]))
            got = group_loglik(ys, ses, tau_g2, tau_c2)
            want = dense_loglik(ys, ses, tau_g2, tau_c2)
            self.assertAlmostEqual(got, want, places=6)


class PosteriorTests(unittest.TestCase):
    def test_posteriors_match_dense_gaussian_conditioning(self):
        from dr_shrink import posteriors
        rng = np.random.default_rng(1)
        for tau_g2, tau_c2 in [(0.01, 0.005), (0.0, 0.004), (0.02, 0.0), (0.0, 0.0)]:
            n = 4
            ys = rng.normal(scale=0.05, size=n)
            ses = rng.uniform(0.01, 0.04, size=n)
            members = [(f"p{i}", "Champ", float(ys[i]), float(ses[i])) for i in range(n)]
            got = posteriors({"g": members}, tau_g2, tau_c2)
            m_g, v_g, means, variances = dense_posterior(ys, ses, tau_g2, tau_c2)
            for i in range(n):
                self.assertAlmostEqual(got[f"p{i}"]["group_mean"], m_g, places=6)
                self.assertAlmostEqual(got[f"p{i}"]["group_var"], v_g, places=6)
                self.assertAlmostEqual(got[f"p{i}"]["mean"], means[i], places=6)
                self.assertAlmostEqual(got[f"p{i}"]["var"], variances[i], places=6)


def simulate_groups(n_groups, tau_g, tau_c, seed, se_range=(0.01, 0.04), max_size=5):
    rng = np.random.default_rng(seed)
    groups = {}
    for g in range(n_groups):
        n = int(rng.integers(1, max_size + 1))
        mu_g = rng.normal(scale=tau_g) if tau_g else 0.0
        members = []
        for i in range(n):
            se = rng.uniform(*se_range)
            delta = rng.normal(scale=tau_c) if tau_c else 0.0
            y = mu_g + delta + rng.normal(scale=se)
            members.append((f"g{g}p{i}", f"Champ{g}_{i}", float(y), float(se)))
        groups[f"g{g}"] = members
    return groups


class LoglikGridTests(unittest.TestCase):
    def test_vectorized_grid_matches_pointwise_total_loglik(self):
        from dr_shrink import loglik_grid, total_loglik
        groups = simulate_groups(12, tau_g=0.02, tau_c=0.01, seed=9, max_size=4)
        tg_grid, tc_grid = [0.0, 1e-4, 0.001, 0.01], [0.0, 5e-4, 0.005]
        grid = loglik_grid(groups, tg_grid, tc_grid)
        for i, tg in enumerate(tg_grid):
            for j, tc in enumerate(tc_grid):
                self.assertAlmostEqual(grid[i, j], total_loglik(groups, tg, tc), places=6)


class FitVarianceTests(unittest.TestCase):
    def test_recovers_known_nonzero_variances(self):
        from dr_shrink import fit_variances
        groups = simulate_groups(300, tau_g=0.03, tau_c=0.02, seed=2)
        (tau_g2, tau_c2), _ = fit_variances(groups)
        self.assertAlmostEqual(math.sqrt(tau_g2), 0.03, delta=0.015)
        self.assertAlmostEqual(math.sqrt(tau_c2), 0.02, delta=0.015)

    def test_true_null_is_not_forced_away_from_zero(self):
        from dr_shrink import fit_variances, lr_test_boundary
        groups = simulate_groups(300, tau_g=0.0, tau_c=0.0, seed=4)
        (tau_g2, tau_c2), ll = fit_variances(groups)
        self.assertLess(tau_g2, 0.0002)
        self.assertLess(tau_c2, 0.0002)
        self.assertFalse(lr_test_boundary(groups, ll, "group")["rejects_zero"])
        self.assertFalse(lr_test_boundary(groups, ll, "champ")["rejects_zero"])


def table_entry(mean, se):
    return dict(dr_effect=dict(mean=mean, se=se), propensity=dict(ess_a=1000, ess_b=1000, extreme_share=0.0))


class CheckpointTests(unittest.TestCase):
    def test_prefers_shrinkage_when_the_group_signal_is_real_and_replicates(self):
        from dr_shrink import checkpoint
        rng = np.random.default_rng(5)
        half0, half1 = {}, {}
        # 60 groups, each with a real +0.05 group effect shared by 3 "champions" (pairs); per-pair noise
        # is large relative to that shared effect, so shrinking toward the group mean should beat both a
        # flat zero prediction and each pair's own noisy raw estimate.
        for g in range(60):
            mu = 0.05
            for c in range(3):
                pid = f"heldout|Champ{g}_{c}|TOP|slot1|16.19|{1000 + g}|{2000 + g}"
                se0, se1 = 0.06, 0.06
                half0[pid] = table_entry(mu + rng.normal(scale=se0), se0)
                half1[pid] = table_entry(mu + rng.normal(scale=se1), se1)
        result = checkpoint(half0, half1, n_boot=300, seed=6)
        self.assertLess(result["mse"]["shrunk"], result["mse"]["zero"])
        self.assertTrue(result["proceed"], "a real, shared, replicating group effect should clear the checkpoint")

    def test_stops_when_there_is_no_real_signal(self):
        from dr_shrink import checkpoint
        rng = np.random.default_rng(7)
        half0, half1 = {}, {}
        for g in range(60):
            for c in range(3):
                pid = f"heldout|Champ{g}_{c}|TOP|slot1|16.19|{1000 + g}|{2000 + g}"
                se0, se1 = 0.06, 0.06
                half0[pid] = table_entry(rng.normal(scale=se0), se0)
                half1[pid] = table_entry(rng.normal(scale=se1), se1)
        result = checkpoint(half0, half1, n_boot=300, seed=8)
        self.assertFalse(result["proceed"], "pure noise should not clear the checkpoint")


class RobustnessTests(unittest.TestCase):
    def test_boots_and_non_boots_partition_the_pairs(self):
        from dr_shrink import is_boots, robustness_checks
        rng = np.random.default_rng(10)
        half0, half1 = {}, {}
        for g in range(20):
            stage = "boots" if g % 2 == 0 else "slot1"
            for c in range(3):
                pid = f"heldout|Champ{g}_{c}|TOP|{stage}|16.19|{1000 + g}|{2000 + g}"
                half0[pid] = table_entry(rng.normal(scale=0.06), 0.06)
                half1[pid] = table_entry(rng.normal(scale=0.06), 0.06)
        r = robustness_checks(half0, half1, n_leave_out=1)
        boots_pids = {p for p in half0 if is_boots(p)}
        non_boots_pids = {p for p in half0 if not is_boots(p)}
        self.assertEqual(r["boots"]["checked_pairs"], len(boots_pids))
        self.assertEqual(r["non_boots"]["checked_pairs"], len(non_boots_pids))
        self.assertEqual(r["boots"]["checked_pairs"] + r["non_boots"]["checked_pairs"], len(half0))

    def test_leave_one_group_out_drops_exactly_that_groups_pairs(self):
        from dr_shrink import checkpoint, robustness_checks
        rng = np.random.default_rng(11)
        half0, half1 = {}, {}
        for g in range(15):
            for c in range(4):  # every group the same size, so "size" alone identifies which was dropped
                pid = f"heldout|Champ{g}_{c}|TOP|slot1|16.19|{1000 + g}|{2000 + g}"
                half0[pid] = table_entry(rng.normal(scale=0.06), 0.06)
                half1[pid] = table_entry(rng.normal(scale=0.06), 0.06)
        full = checkpoint(half0, half1, n_boot=50)
        loo = robustness_checks(half0, half1, n_leave_out=1)["leave_one_group_out"][0]
        self.assertEqual(loo["size"], 4)
        self.assertEqual(loo["checkpoint"]["checked_pairs"], full["checked_pairs"] - 4)

    def test_all_slot_pairs_gives_an_empty_boots_checkpoint_not_a_nan(self):
        from dr_shrink import robustness_checks
        rng = np.random.default_rng(12)
        half0, half1 = {}, {}
        for g in range(10):
            pid = f"heldout|Champ{g}|TOP|slot1|16.19|{1000 + g}|{2000 + g}"
            half0[pid] = table_entry(rng.normal(scale=0.06), 0.06)
            half1[pid] = table_entry(rng.normal(scale=0.06), 0.06)
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            r = robustness_checks(half0, half1, n_leave_out=1)
        self.assertEqual(r["boots"]["checked_pairs"], 0)
        self.assertIsNone(r["boots"]["mse"])
        self.assertFalse(r["boots"]["proceed"])


if __name__ == "__main__":
    unittest.main()
