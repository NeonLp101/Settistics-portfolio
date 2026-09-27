"""Split out of tests/test_pipeline.py when candidates.py was archived."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from candidates import recipe_cost, screen

ITEMS={
 '1054':{'name':"Doran's Shield",'gold':{'total':450,'purchasable':True},'maps':{'11':True}},
 '2003':{'name':'Health Potion','gold':{'total':50,'purchasable':True},'tags':['Consumable'],'maps':{'11':True}},
 '1036':{'name':'Long Sword','gold':{'total':350,'purchasable':True},'maps':{'11':True}},
 '1001':{'name':'Boots','gold':{'total':300,'purchasable':True},'maps':{'11':True}},
 '9999':{'name':'Fixture upgrade','from':['1036','1036'],'gold':{'total':1000,'purchasable':True},'maps':{'11':True}},
}

class CandidateTests(unittest.TestCase):
    def test_recipe_discount(self):
        cost,used=recipe_cost('9999',['1036'],ITEMS)
        self.assertEqual(cost,650);self.assertEqual(used['1036'],1)
    def test_two_owned_components_not_double_counted(self):
        cost,used=recipe_cost('9999',['1036','1036'],ITEMS)
        self.assertEqual(cost,300);self.assertEqual(sum(used.values()),2)
    def test_affordability(self):
        result=screen(['9999'],['1036'],649,ITEMS)[0]
        self.assertFalse(result['budgetAndSlotsPass']);self.assertFalse(result['gameLegalityVerified'])
    def test_slots(self):
        result=screen(['1001'],['1054']*6,1000,ITEMS)[0]
        self.assertFalse(result['budgetAndSlotsPass'])
    def test_special_rules_declined(self):
        self.assertFalse(screen(['2003'],[],1000,ITEMS)[0]['supported'])

if __name__=='__main__':unittest.main()
