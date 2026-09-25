import importlib.util
from pathlib import Path
import random
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))

@unittest.skipUnless(importlib.util.find_spec('sklearn'),'Optional model dependencies not installed')
class ModelTests(unittest.TestCase):
    def test_no_data_is_not_zero_effect(self):
        from model import evaluate
        report=evaluate([],'A','B','logistic')
        self.assertEqual(report['status'],'insufficient_data');self.assertNotIn('estimatedDifferencePP',report)
    def test_forward_evaluation_with_known_synthetic_effect(self):
        from model import evaluate
        rng=random.Random(73);rows=[]
        for i in range(4000):
            t=rng.random()<.5
            rows.append(dict(package='A' if t else 'B',win=int(rng.random()<(.65 if t else .45)),
                             features={'champion':'Sett','opponent':'Teemo','side':'100'},opponent='Teemo',
                             startedAt=i+1,matchId=str(i),playerId=str(i%500)))
        report=evaluate(rows,'A','B','logistic')
        self.assertEqual(report['status'],'research_only');self.assertFalse(report['causalValidationPassed'])
        self.assertAlmostEqual(report['estimatedDifferencePP'],20,delta=6)
        self.assertEqual(report['evaluatedGames'],2400)
        self.assertTrue(all(f['train']<f['cutoff'] for f in report['folds']))
    def test_single_treatment_refuses_effect(self):
        from model import evaluate
        rows=[dict(package='A',win=i%2,features={},startedAt=i,matchId=str(i),playerId=str(i),opponent='Teemo') for i in range(500)]
        self.assertEqual(evaluate(rows,'A','B','logistic')['status'],'insufficient_data')

if __name__=='__main__':unittest.main()
