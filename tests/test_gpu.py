import importlib.util
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))

@unittest.skipUnless(importlib.util.find_spec('sklearn'),'model dependencies unavailable')
class GpuTests(unittest.TestCase):
    def test_forward_windows_have_disjoint_matches_and_strict_time_order(self):
        from compare_gpu import splits
        rows=[dict(match=str(i),startedAt=i//2) for i in range(1000) for _ in range(2)]
        for fit,cal,test in splits(rows):
            groups=[[r for r,m in zip(rows,mask) if m] for mask in (fit,cal,test)]
            self.assertLess(max(r['startedAt'] for r in groups[0]),min(r['startedAt'] for r in groups[1]))
            self.assertLess(max(r['startedAt'] for r in groups[1]),min(r['startedAt'] for r in groups[2]))
            self.assertFalse({r['match'] for r in groups[0]}&{r['match'] for r in groups[2]})

    def test_gpu_refuses_silent_cpu_fallback(self):
        from backends import verify_device
        class CPU:
            def get_booster(self):return self
            def save_config(self):return '{"learner":{"generic_param":{"device":"cpu"}}}'
        with self.assertRaises(RuntimeError):verify_device(CPU(),'xgb-cuda')

    def test_gpu_cpu_parameters_only_differ_in_device(self):
        if not importlib.util.find_spec('xgboost'):self.skipTest('optional XGBoost not installed')
        from backends import outcome_model
        cpu=outcome_model('xgb-cpu').get_params();gpu=outcome_model('xgb-cuda').get_params()
        self.assertEqual(cpu.pop('device'),'cpu');self.assertEqual(gpu.pop('device'),'cuda:0')
        self.assertEqual(cpu,gpu)

    def test_no_outcome_or_identity_features(self):
        from wpa import FEATURES
        self.assertFalse(set(FEATURES)&{'win','player','match','champ_str','opp_str','team_str','duration','inventory'})

if __name__=='__main__':unittest.main()
