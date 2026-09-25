"""Synthetic fixtures never leave tests or enter exported production data."""
from copy import deepcopy
import io
import json
import random
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from engine import extract, aggregate, purchases, pre_features, connect, Client, RiotError, collect, valid_match
from candidates import recipe_cost, screen

ITEMS={
 '1054':{'name':"Doran's Shield",'gold':{'total':450,'purchasable':True},'maps':{'11':True}},
 '2003':{'name':'Health Potion','gold':{'total':50,'purchasable':True},'tags':['Consumable'],'maps':{'11':True}},
 '1036':{'name':'Long Sword','gold':{'total':350,'purchasable':True},'maps':{'11':True}},
 '1001':{'name':'Boots','gold':{'total':300,'purchasable':True},'maps':{'11':True}},
 '9999':{'name':'Fixture upgrade','from':['1036','1036'],'gold':{'total':1000,'purchasable':True},'maps':{'11':True}},
}
def fixture():
    players=[]
    roles=['TOP','JUNGLE','MIDDLE','BOTTOM','UTILITY']
    for i in range(10):
        players.append(dict(participantId=i+1,puuid=f'private-player-{i}',teamId=100 if i<5 else 200,
                            teamPosition=roles[i%5],championName='Sett' if i==0 else 'Teemo' if i==5 else f'Champion{i}',
                            win=i<5,summoner1Id=4,summoner2Id=12,perks={'styles':[{'selections':[{'perk':8005}]}],'statPerks':{'offense':5008}}))
    match={'metadata':{'matchId':'EUW1_123'},'info':dict(queueId=420,mapId=11,gameDuration=1200,gameStartTimestamp=1000000,gameVersion='16.18.1',platformId='EUW1',participants=players)}
    events=[dict(type='ITEM_PURCHASED',participantId=1,itemId=1054,timestamp=10000),dict(type='ITEM_PURCHASED',participantId=1,itemId=2003,timestamp=11000),dict(type='ITEM_PURCHASED',participantId=1,itemId=1036,timestamp=70000),dict(type='ITEM_PURCHASED',participantId=1,itemId=1036,timestamp=71000)]
    timeline={'metadata':{'matchId':'EUW1_123'},'info':{'frames':[{'timestamp':0,'participantFrames':{str(i+1):dict(totalGold=500,currentGold=500,level=1) for i in range(10)},'events':[]},{'timestamp':60000,'participantFrames':{str(i+1):dict(totalGold=1000,currentGold=200,level=2) for i in range(10)},'events':events},{'timestamp':120000,'participantFrames':{'1':dict(totalGold=999999,currentGold=999999,level=18)},'events':[]}]}}
    return match,timeline

class ExtractionTests(unittest.TestCase):
    def test_true_same_role_opponent(self):
        m,t=fixture();rows=extract(m,t,ITEMS)
        self.assertEqual(rows[0]['opponent'],'Teemo');self.assertEqual(rows[0]['role'],'TOP')
    def test_starting_package(self):
        m,t=fixture();r=extract(m,t,ITEMS)[0]
        self.assertEqual(r['package'],'1054x1+2003x1');self.assertEqual(len(r['purchases']),4)
    def test_undo_purchase(self):
        m,t=fixture();t['info']['frames'][1]['events'].append(dict(type='ITEM_UNDO',participantId=1,beforeId=2003,afterId=0,timestamp=12000))
        self.assertEqual(extract(m,t,ITEMS)[0]['package'],'1054x1')
    def test_undo_unknown_excludes_ledger(self):
        m,t=fixture();t['info']['frames'][1]['events'].append(dict(type='ITEM_UNDO',participantId=1,beforeId=99999,timestamp=12000))
        r=extract(m,t,ITEMS)[0];self.assertIsNone(r['package']);self.assertEqual(r['purchases'],[])
    def test_sale_and_undo_sale(self):
        m,t=fixture();t['info']['frames'][1]['events'] += [dict(type='ITEM_SOLD',participantId=1,itemId=2003,timestamp=12000),dict(type='ITEM_UNDO',participantId=1,beforeId=0,afterId=2003,timestamp=13000)]
        self.assertEqual(extract(m,t,ITEMS)[0]['package'],'1054x1+2003x1')
    def test_late_undo_does_not_rewrite_early_treatment(self):
        m,t=fixture();t['info']['frames'][1]['events'].append(dict(type='ITEM_UNDO',participantId=1,beforeId=2003,timestamp=90000))
        self.assertIsNone(extract(m,t,ITEMS)[0]['package'])
    def test_no_future_frame_leakage(self):
        m,t=fixture();p=pre_features(t,m['info']['participants'],m['info']['participants'][0],70000)
        self.assertEqual(p['currentGoldSnapshot'],200);self.assertEqual(p['snapshotAgeMs'],10000);self.assertFalse(p['budgetExact'])
    def test_equal_timestamp_frame_excluded(self):
        m,t=fixture();p=pre_features(t,m['info']['participants'],m['info']['participants'][0],60000)
        self.assertEqual(p['currentGoldSnapshot'],500)
    def test_unknown_role_excluded(self):
        m,t=fixture();m['info']['participants'][0]['teamPosition']=''
        self.assertFalse(any(r['champion']=='Sett' for r in extract(m,t,ITEMS)))
    def test_ambiguous_roles_excluded(self):
        m,t=fixture();m['info']['participants'][6]['teamPosition']='TOP'
        self.assertFalse(any(r['champion']=='Sett' for r in extract(m,t,ITEMS)))
    def test_remake_excluded(self):
        m,t=fixture();m['info']['gameDuration']=120;self.assertEqual(extract(m,t,ITEMS),[])
    def test_mismatched_timeline_excluded(self):
        m,t=fixture();t['metadata']['matchId']='EUW1_124';self.assertEqual(extract(m,t,ITEMS),[])
    def test_early_combat_excludes_package(self):
        m,t=fixture();t['info']['frames'][1]['events'].append(dict(type='CHAMPION_KILL',timestamp=20000))
        self.assertIsNone(extract(m,t,ITEMS)[0]['package'])
    def test_dedupe_purchase_occurrence(self):
        m,t=fixture();b=aggregate([extract(m,t,ITEMS)[0]])[0]
        sword=next(c for c in b['choices'] if c['id']=='1036')
        self.assertEqual(sword['games'],1);self.assertAlmostEqual(sword['timeSum'],70/60)
    def test_export_contains_no_player_identifiers(self):
        m,t=fixture();text=json.dumps(aggregate(extract(m,t,ITEMS)))
        self.assertNotIn('private-player',text);self.assertNotIn('EUW1_123',text)
    def test_no_outcome_in_features(self):
        m,t=fixture();f=extract(m,t,ITEMS)[0]['features']
        self.assertNotIn('win',f);self.assertNotIn('gameDuration',f);self.assertNotIn('package',f)

class DatabaseTests(unittest.TestCase):
    def test_persistent_queue_deduplication(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'test.sqlite';db=connect(path)
            for _ in range(2):db.execute("INSERT OR IGNORE INTO matches(id,platform) VALUES('EUW1_1','euw1')")
            db.commit();db.close();db=connect(path)
            self.assertEqual(db.execute('SELECT count(*) FROM matches').fetchone()[0],1);db.close()
    def test_disqualified_cached_matches_do_not_starve_queue(self):
        with tempfile.TemporaryDirectory() as d:
            db=connect(Path(d)/'test.sqlite')
            db.execute("INSERT INTO matches(id,collected_at) VALUES('EUW1_99','2026-01-01')")
            db.execute("INSERT INTO matches(id) VALUES('EUW1_1')")
            r=db.execute('SELECT id FROM matches ORDER BY collected_at IS NOT NULL,collected_at,id DESC').fetchone()
            self.assertEqual(r[0],'EUW1_1');db.close()
    def test_site_data_excludes_sealed_and_focus_games(self):
        from engine import SEALED_FROM_MS, USABLE_SQL
        with tempfile.TemporaryDirectory() as d:
            db=connect(Path(d)/'test.sqlite')
            for mid,source,start in [('EUW1_1','ladder',SEALED_FROM_MS-1),('EUW1_2','focus kaisa:BOTTOM',SEALED_FROM_MS-1),
                                     ('EUW1_3','snowball',SEALED_FROM_MS),('EUW1_4',None,SEALED_FROM_MS-60000)]:
                db.execute("INSERT INTO matches(id,platform,source,detail,timeline,status) VALUES(?,'euw1',?,?,'{}','done')",
                           (mid,source,json.dumps({'info':{'gameStartTimestamp':start}})))
            self.assertEqual([r[0] for r in db.execute(f"SELECT id FROM matches WHERE {USABLE_SQL} ORDER BY id")],['EUW1_1','EUW1_4'])
            db.close()

class RateTests(unittest.TestCase):
    def test_missing_key(self):
        with self.assertRaises(ValueError):Client('')
    def test_key_rejection_stops_immediately(self):
        c=Client('RGAPI-test-only')
        with patch('engine.urlopen',side_effect=HTTPError('https://euw1.api.riotgames.com',403,'forbidden',{},None)) as op,patch.object(c,'pace'):
            with self.assertRaises(RiotError):c.get('euw1.api.riotgames.com','/test','test')
            self.assertEqual(op.call_count,1)
    def test_full_retry_after_honored(self):
        c=Client('RGAPI-test-only')
        error=HTTPError('https://euw1.api.riotgames.com',429,'limited',{'Retry-After':'90'},None)
        class Response(io.BytesIO):
            headers={}
        with patch('engine.urlopen',side_effect=[error,Response(b'{}')]),patch.object(c,'pace'),patch.object(c,'pause') as pause:
            self.assertEqual(c.get('euw1.api.riotgames.com','/test','test'),{})
            pause.assert_called_once_with(91)

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


class PrivacyTests(unittest.TestCase):
    def test_minimize_removes_names_and_account_ids(self):
        import engine
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, 'PSEUDONYM_KEY', Path(tmp) / 'k'), patch.object(engine, '_pseudonym_key', None):
            match, _ = fixture()
            for p in match['info']['participants']:
                p.update(riotIdGameName='Someone', riotIdTagline='EUW', summonerName='Someone', summonerId='s', profileIcon=1)
            match['metadata']['participants'] = [p['puuid'] for p in match['info']['participants']]
            text = json.dumps(engine.minimize(match))
            for secret in ('private-player-', 'Someone', 'riotIdTagline', 'summonerId', 'profileIcon', '"puuid"'):
                self.assertNotIn(secret, text)
            refs = [p['playerRef'] for p in match['info']['participants']]
            self.assertEqual(len(set(refs)), 10)  # still distinguishes players
            self.assertEqual(engine.minimize(deepcopy(match)), match)  # idempotent

    def test_purge_deletes_only_games_and_seeds_past_retention(self):
        import engine
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'db.sqlite')
            now_ms = int(engine.datetime.now(engine.timezone.utc).timestamp() * 1000)
            day = 86400000
            for mid, age in (('EUW1_1', 800), ('EUW1_2', 10), ('EUW1_3', 729)):
                db.execute("INSERT INTO matches(id,platform,source,detail,status) VALUES(?,?,?,?,?)",
                           (mid, 'euw1', 'test', json.dumps({'info': {'gameStartTimestamp': now_ms - age * day}}), 'done'))
            db.execute("INSERT INTO matches(id,platform,source,status) VALUES('EUW1_4','euw1','test','queued')")
            db.execute("INSERT INTO players(puuid,platform,source,observed_at) VALUES('old','euw1','t','2020-01-01T00:00:00+00:00')")
            db.execute("INSERT INTO players(puuid,platform,source,observed_at) VALUES('new','euw1','t',?)", (engine.utc(),))
            ago = lambda d: (engine.datetime.now(engine.timezone.utc) - engine.timedelta(days=d)).isoformat()
            # idle: found 40 days ago, never a new game; active: found 40 days ago, new game 5 days ago
            db.execute("INSERT INTO players(puuid,platform,source,observed_at) VALUES('idle','euw1','snowball',?)", (ago(40),))
            db.execute("INSERT INTO players(puuid,platform,source,observed_at,last_active) VALUES('active','euw1','snowball',?,?)", (ago(40), ago(5)))
            db.commit()
            with patch('sys.stdout', new_callable=io.StringIO):
                engine.purge(db, None, SimpleNamespace(retention_days=730))
            self.assertEqual({r[0] for r in db.execute("SELECT id FROM matches")}, {'EUW1_2', 'EUW1_3', 'EUW1_4'})
            self.assertEqual({r[0] for r in db.execute("SELECT puuid FROM players")}, {'new', 'active'})
            db.close()


class BuildPathTests(unittest.TestCase):
    CATALOG = {
        '1001': {'name': 'Boots', 'tags': ['Boots'], 'gold': {'total': 300}},
        '3047': {'name': 'Plated Steelcaps', 'tags': ['Boots'], 'from': ['1001'], 'gold': {'total': 1200}},
        '3111': {'name': "Mercury's Treads", 'tags': ['Boots'], 'from': ['1001'], 'gold': {'total': 1250}},
        '3044': {'name': 'Phage', 'into': ['6631'], 'gold': {'total': 1100}},
        '6631': {'name': 'Stridebreaker', 'gold': {'total': 3300}},
        '3053': {'name': "Sterak's Gage", 'gold': {'total': 3200}},
        '2003': {'name': 'Health Potion', 'tags': ['Consumable'], 'gold': {'total': 50}},
    }

    def test_build_order_and_boots_position(self):
        from engine import build_path
        buys = [dict(item=i, time=t * 60000) for i, t in
                [(2003, 0), (1001, 5), (3044, 7), (6631, 12), (3047, 14), (3053, 20), (3111, 25), (6631, 30)]]
        build, boots = build_path(buys, self.CATALOG)
        self.assertEqual([b[0] for b in build], ['6631', '3053'])      # components, potions, repeats ignored
        self.assertEqual(build[0][2], 12)                               # minute the item was finished
        self.assertEqual((boots[0], boots[3]), ('3047', 1))             # first upgraded boots, after 1 item

    def test_aggregate_emits_slots_and_boots_timing(self):
        from engine import aggregate
        rec = dict(champion='Sett', opponent='Teemo', role='TOP', patch='16.18', region='EUW1', win=1, package=None,
                   ledgerUncertain=False, purchases=[], runes=None, spells=None,
                   build=[('6631', 'Stridebreaker', 12.0), ('3053', "Sterak's Gage", 20.0)],
                   boots=('3047', 'Plated Steelcaps', 14.0, 1))
        choices = {c['kind'] + ':' + c['id']: c for c in aggregate([rec])[0]['choices']}
        self.assertIn('slot1:6631', choices)
        self.assertIn('slot2:3053', choices)
        self.assertEqual(choices['bootsTiming:1']['games'], 1)
        self.assertEqual(choices['boots:3047']['timeSum'], 14.0)
        self.assertNotIn('core:6631>3053', choices)  # fewer than three finished items: no core
        rec['build'].append(('3071', 'Black Cleaver', 26.0))
        choices = {c['kind'] + ':' + c['id']: c for c in aggregate([rec])[0]['choices']}
        self.assertEqual(choices['core:6631>3053>3071']['label'], "Stridebreaker \u2192 Sterak's Gage \u2192 Black Cleaver")


class WpaStateTests(unittest.TestCase):
    def test_state_uses_only_information_from_before_the_decision(self):
        try:
            import wpa
        except ImportError:
            self.skipTest('numpy/scikit-learn not installed (requirements-model.txt)')
        players = [dict(participantId=i, teamId=100 if i <= 5 else 200, teamPosition='TOP', championName=f'C{i}', win=i <= 5)
                   for i in range(1, 11)]
        match = {'info': {'participants': players, 'gameDuration': 600}}
        frame = lambda ts, gold: {'timestamp': ts, 'events': [], 'participantFrames': {
            str(i): {'totalGold': gold if i == 1 else 500, 'xp': 0, 'level': 1} for i in range(1, 11)}}
        frames = [frame(0, 500), frame(60000, 900), frame(120000, 5000)]
        frames[1]['events'] = [{'type': 'CHAMPION_KILL', 'victimId': 6, 'timestamp': 90000},
                               {'type': 'ELITE_MONSTER_KILL', 'monsterType': 'DRAGON', 'killerTeamId': 100, 'timestamp': 110000}]
        game = wpa.Game(match, {'info': {'frames': frames}})
        at_kill = game.state(1, 6, 90000)
        self.assertEqual(at_kill['kills'], 0)          # the kill at exactly t is not "before" t
        self.assertEqual(at_kill['lane_gold'], 400)    # frame at 60s, not the later one
        later = game.state(1, 6, 120000)
        self.assertEqual((later['kills'], later['dragons']), (1, 1))
        self.assertEqual(later['lane_gold'], 400)      # the 120s frame is not yet visible at exactly 120s


class SmartDiscoverTests(unittest.TestCase):
    def test_checks_active_and_stale_players_only_and_records_activity(self):
        import engine
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'db.sqlite')
            now = engine.datetime.now(engine.timezone.utc)
            iso = lambda hours: (now - engine.timedelta(hours=hours)).isoformat()
            rows = [('new', None, None), ('active', iso(0.2), iso(1)), ('idle', iso(0.2), iso(30)), ('stale', iso(5), iso(40))]
            for puuid, checked, active in rows:
                db.execute("INSERT INTO players(puuid,platform,source,observed_at,last_checked,last_active) VALUES(?,?,?,?,?,?)",
                           (puuid, 'euw1', 'test', iso(50), checked, active))
            db.commit()
            asked = []
            class Client:
                def get(self, host, path, method):
                    puuid = path.split('/by-puuid/')[1].split('/')[0]
                    asked.append(puuid)
                    return ['EUW1_1'] if puuid == 'new' else []
            args = SimpleNamespace(platform='euw1', players=100, smart=True, active_hours=6, recheck_hours=3,
                                   refresh=True, since=None, per_player=100)
            with patch('sys.stdout', new_callable=io.StringIO):
                engine.discover(db, Client(), args)
            self.assertEqual(sorted(asked), ['active', 'new', 'stale'])      # 'idle' was checked 12 min ago
            active = dict(db.execute("SELECT puuid, last_active FROM players").fetchall())
            self.assertIsNotNone(active['new'])                              # found a new game: now active
            db.close()


class LaneTests(unittest.TestCase):
    def test_lane_is_measured_until_the_first_interference_only(self):
        try:
            import lane
        except ImportError:
            self.skipTest('numpy/scikit-learn not installed (requirements-model.txt)')
        players = [dict(participantId=i, teamId=100 if i <= 5 else 200) for i in range(1, 11)]
        far = {'x': 14000, 'y': 1000}
        def frame(minute, jungler_near=False):
            pos = {1: {'x': 1000, 'y': 12000}, 6: {'x': 1200, 'y': 12200}, 2: {'x': 1300, 'y': 12100} if jungler_near else far}
            return {'timestamp': minute * 60000, 'events': [], 'participantFrames': {
                str(i): {'totalGold': 500 + minute * (400 if i == 1 else 350), 'level': 1, 'position': pos.get(i, far)} for i in range(1, 11)}}
        frames = [frame(m) for m in range(0, 11)]
        frames[6]['events'] = [{'type': 'CHAMPION_KILL', 'timestamp': 6 * 60000 + 30000, 'killerId': 7, 'victimId': 1,
                                'assistingParticipantIds': [6]}]                  # enemy jungler ganks at 6:30
        L = lane.Lane({'info': {'participants': players}}, {'info': {'frames': frames}})
        end = L.clean_until(1, 6, 60000, 600000)
        self.assertEqual(end, 6 * 60000)                                          # last snapshot before the gank
        self.assertEqual(L.lead(1, 6, end)[0] - L.lead(1, 6, 60000)[0], 250)      # 50 gold/min lead over 5 min
        frames[3] = frame(3, jungler_near=True); frames[4] = frame(4, jungler_near=True)
        L = lane.Lane({'info': {'participants': players}}, {'info': {'frames': frames}})
        self.assertIsNone(L.clean_until(1, 6, 60000, 600000))                     # 2nd nearby snapshot at 4:00: under 3 min clean


class ProvisionalCatalogTests(unittest.TestCase):
    def test_previous_patch_is_used_until_the_real_list_appears(self):
        import engine
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'db.sqlite')
            old = {'1001': {'name': 'Boots'}}
            new = {'1001': {'name': 'Boots'}, '9999': {'name': 'New item'}}
            versions = ['16.18.1', '16.17.1', 'lolpatch_3.7']  # Data Dragon really lists legacy entries
            def fake(url):
                if url.endswith('versions.json'):
                    return versions
                return {'data': new if '/16.19.1/' in url else old}
            with patch('engine.static_json', side_effect=fake), patch('sys.stdout', new_callable=io.StringIO):
                engine._catalog_checked.clear()
                self.assertEqual(engine.catalog(db, '16.19'), old)
                self.assertTrue(db.execute("SELECT version FROM catalog WHERE patch='16.19'").fetchone()[0].startswith('provisional:16.18.1'))
                versions.insert(0, '16.19.1')
                engine._catalog_checked.clear()
                self.assertEqual(engine.catalog(db, '16.19'), new)
                self.assertEqual(db.execute("SELECT version FROM catalog WHERE patch='16.19'").fetchone()[0], '16.19.1')
            db.close()

    def test_unknown_item_drops_item_statistics_only(self):
        match, timeline = fixture()
        timeline['info']['frames'][-1]['events'].append(dict(type='ITEM_PURCHASED', participantId=1, itemId=7777, timestamp=500000))
        rec = next(r for r in extract(match, timeline, ITEMS) if r['champion'] == 'Sett')
        self.assertTrue(rec['ledgerUncertain'])
        self.assertEqual(rec['purchases'], [])
        self.assertIsNotNone(rec['spells'])


class CrawlTests(unittest.TestCase):
    def test_crawl_downloads_new_games_right_after_checking_a_player(self):
        import engine
        from types import SimpleNamespace
        match, timeline = fixture()
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, 'PSEUDONYM_KEY', Path(tmp) / 'k'), \
                patch.object(engine, '_pseudonym_key', None), patch.object(engine, 'catalog', lambda db, p: ITEMS):
            db = connect(Path(tmp) / 'db.sqlite')
            for puuid in ('has-game', 'no-game'):
                db.execute("INSERT INTO players(puuid,platform,source,observed_at) VALUES(?,?,?,?)", (puuid, 'euw1', 't', engine.utc()))
            db.commit()
            calls = []
            class Client:
                def get(self, host, path, method):
                    calls.append(method)
                    if method == 'match-list':
                        return ['EUW1_123'] if 'has-game' in path else []
                    return deepcopy(timeline) if path.endswith('/timeline') else deepcopy(match)
            args = SimpleNamespace(platform='euw1', patch=None, champion='all', role='ALL', recheck_hours=2, since=None,
                                   refresh=True, per_player=100, retention_days=730)
            out = io.StringIO()
            # when nobody is due the crawler sleeps; stop it there
            with patch('engine.time.sleep', side_effect=KeyboardInterrupt), patch('sys.stdout', out):
                with self.assertRaises(KeyboardInterrupt):
                    engine.crawl(db, Client(), args)
            self.assertEqual(db.execute("SELECT status FROM matches WHERE id='EUW1_123'").fetchone()[0], 'done')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM players WHERE last_checked IS NULL").fetchone()[0], 0)
            self.assertIn('1 new game, 1 saved', out.getvalue())
            self.assertIn('nobody due', out.getvalue())
            i = calls.index('match-list')
            self.assertEqual(calls[i + 1:i + 3], ['match', 'timeline'])   # downloaded right after that player's check
            db.close()

    def test_snowball_adds_the_other_players_and_keeps_stored_games_pseudonymous(self):
        import engine
        from types import SimpleNamespace
        match, timeline = fixture()
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, 'PSEUDONYM_KEY', Path(tmp) / 'k'), \
                patch.object(engine, '_pseudonym_key', None), patch.object(engine, 'catalog', lambda db, p: ITEMS):
            db = connect(Path(tmp) / 'db.sqlite')
            other_writer = sqlite3.connect(Path(tmp) / 'db.sqlite', timeout=0.1)
            other_writer.execute('CREATE TABLE write_probe (value INTEGER)')
            other_writer.commit()
            db.execute("INSERT INTO players(puuid,platform,source,observed_at) VALUES('private-player-0','euw1','t',?)", (engine.utc(),))
            db.commit()
            lists = []
            class Client:
                def get(self, host, path, method):
                    if method == 'match-list':
                        lists.append(path)
                        return ['EUW1_123'] if len(lists) == 1 else []
                    if path.endswith('/timeline'):
                        # A second regional crawler must be able to write while this
                        # one waits for Riot after inserting snowball players.
                        other_writer.execute('INSERT INTO write_probe VALUES (1)')
                        other_writer.commit()
                    return deepcopy(timeline) if path.endswith('/timeline') else deepcopy(match)
            args = SimpleNamespace(platform='euw1', patch=None, champion='all', role='ALL', recheck_hours=2, since=None,
                                   refresh=True, per_player=100, retention_days=730, snowball=True, snowball_cap=6)
            with patch('engine.time.sleep', side_effect=KeyboardInterrupt), patch('sys.stdout', new_callable=io.StringIO):
                with self.assertRaises(KeyboardInterrupt):
                    engine.crawl(db, Client(), args)
            pool = {r[0]: r[1] for r in db.execute("SELECT puuid, source FROM players")}
            self.assertEqual(len(pool), 6)                                   # capped: the seed plus 5 of the 9 others
            self.assertEqual(sum(s == 'snowball' for s in pool.values()), 5)
            self.assertEqual(len(lists), 6)                                  # the new players were checked too
            self.assertNotIn('private-player-', db.execute("SELECT detail || timeline FROM matches").fetchone()[0])
            self.assertGreater(other_writer.execute('SELECT COUNT(*) FROM write_probe').fetchone()[0], 0)
            other_writer.close()
            db.close()

    def test_focus_crawl_checks_only_focus_players_and_promotes_only_the_focus_champion(self):
        import engine
        from types import SimpleNamespace
        match, timeline = fixture()
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, 'PSEUDONYM_KEY', Path(tmp) / 'k'), \
                patch.object(engine, '_pseudonym_key', None), patch.object(engine, 'catalog', lambda db, p: ITEMS):
            db = connect(Path(tmp) / 'db.sqlite')
            # a ladder seed who is in the game but not on Sett, and a general-crawl player who must be left alone
            db.execute("INSERT INTO players(puuid,platform,source,observed_at,focus) VALUES('private-player-3','euw1','ladder',?,'seed:sett:TOP')", (engine.utc(),))
            db.execute("INSERT INTO players(puuid,platform,source,observed_at) VALUES('general','euw1','snowball',?)", (engine.utc(),))
            db.commit()
            lists = []
            class Client:
                def get(self, host, path, method):
                    if method == 'match-list':
                        lists.append(path)
                        return ['EUW1_123'] if len(lists) == 1 else []
                    return deepcopy(timeline) if path.endswith('/timeline') else deepcopy(match)
            args = SimpleNamespace(platform='euw1', patch=None, champion='Sett', role='TOP', recheck_hours=2, since=None,
                                   refresh=True, per_player=100, retention_days=730, snowball=True, snowball_cap=100, focus=True)
            with patch('engine.time.sleep', side_effect=KeyboardInterrupt), patch('sys.stdout', new_callable=io.StringIO):
                with self.assertRaises(KeyboardInterrupt):
                    engine.crawl(db, Client(), args)
            pool = {r[0]: r[1] for r in db.execute("SELECT puuid, focus FROM players")}
            self.assertEqual(pool, {'private-player-3': None, 'general': None, 'private-player-0': 'sett:TOP'})
            self.assertEqual(len(lists), 2)                                  # the seed, then the promoted Sett player
            self.assertNotIn('general', ''.join(lists))
            self.assertEqual(tuple(db.execute("SELECT source, status FROM matches").fetchone()), ('focus sett:TOP', 'done'))
            db.close()

    def test_focus_seed_from_games_finds_known_players_of_the_champion(self):
        import engine
        from types import SimpleNamespace
        match, _ = fixture()
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, 'PSEUDONYM_KEY', Path(tmp) / 'k'), \
                patch.object(engine, '_pseudonym_key', None):
            db = connect(Path(tmp) / 'db.sqlite')
            for i in range(10):
                db.execute("INSERT INTO players(puuid,platform,source,observed_at) VALUES(?,'euw1','snowball',?)", (f'private-player-{i}', engine.utc()))
            db.execute("INSERT INTO matches(id,platform,source,detail,status) VALUES('EUW1_123','euw1','t',?,'done')", (json.dumps(engine.minimize(deepcopy(match))),))
            db.commit()
            args = SimpleNamespace(platform='euw1', champion='Sett', role='TOP', focus=True, from_games=True, players=50, snowball_cap=100)
            with patch('sys.stdout', new_callable=io.StringIO):
                engine.seed(db, None, args)
            self.assertEqual([r[0] for r in db.execute("SELECT puuid FROM players WHERE focus='sett:TOP'")], ['private-player-0'])
            db.close()


class SkillOrderTests(unittest.TestCase):
    @staticmethod
    def level_ups(keys, pid=1, start=70000):
        slot = {'Q': 1, 'W': 2, 'E': 3, 'R': 4}
        return [dict(type='SKILL_LEVEL_UP', participantId=pid, skillSlot=slot[k], levelUpType='NORMAL', timestamp=start + i * 1000)
                for i, k in enumerate(keys)]

    def with_skills(self, keys, extra=()):
        match, timeline = fixture()
        timeline['info']['frames'][-1]['events'] += self.level_ups(keys) + list(extra)
        return next(r for r in extract(match, timeline, ITEMS) if r['champion'] == 'Sett')

    def test_max_order_and_opener(self):
        # W E Q opener, then Q maxed first, W second (level 13 profile)
        rec = self.with_skills(list('WEQQQRQQWRWWWE'))
        self.assertEqual(rec['skillStart'], 'W>E>Q')
        self.assertEqual(rec['skillMax'], 'Q>W>E')

    def test_evolve_and_other_players_are_ignored(self):
        evolve = [dict(type='SKILL_LEVEL_UP', participantId=1, skillSlot=3, levelUpType='EVOLVE', timestamp=69000)]
        other = self.level_ups('EEE', pid=2, start=60000)
        rec = self.with_skills(list('QWE'), evolve + other)
        self.assertEqual(rec['skillStart'], 'Q>W>E')

    def test_unfinished_max_has_opener_only(self):
        rec = self.with_skills(list('QEWQQQ'))          # Q maxed, nothing else: second max unknown
        self.assertEqual(rec['skillStart'], 'Q>E>W')
        self.assertIsNone(rec['skillMax'])
        match, timeline = fixture()
        rec = next(r for r in extract(match, timeline, ITEMS) if r['champion'] == 'Sett')
        self.assertIsNone(rec['skillStart'])            # no skill events at all

    def test_aggregate_emits_skill_kinds_with_eligibility(self):
        long_game = self.with_skills(list('WEQQQRQQWRWWWE'))
        short_game = dict(self.with_skills(list('WEQQ')), win=0)
        bucket = aggregate([long_game, short_game])[0]
        choices = {c['kind'] + ':' + c['id']: c for c in bucket['choices']}
        self.assertEqual(bucket['eligible']['skills'], 2)
        self.assertEqual((choices['skillStart:W>E>Q']['games'], choices['skillStart:W>E>Q']['wins']), (2, 1))
        self.assertEqual(choices['skillMax:Q>W>E']['games'], 1)
        self.assertEqual({c['kind'] for c in bucket['choices']} & {'skillMax', 'skillStart'}, {'skillMax', 'skillStart'})
        long_game['wpa'] = {('skillMax', 'Q>W>E'): 0.4}  # residual fields follow the same path as other kinds
        c = next(c for c in aggregate([long_game])[0]['choices'] if c['kind'] == 'skillMax')
        self.assertAlmostEqual(c['residSum'], 1 - 0.4)


class RouteAggregationTests(unittest.TestCase):
    def record(self, first_minute, boots_minute, before, win=1):
        m, t = fixture()
        r = extract(m, t, ITEMS)[0]
        r.update(build=[('6631', 'Stridebreaker', first_minute), ('3053', 'Sterak', 22)],
                 boots=('3047', 'Steelcaps', boots_minute, before), win=win,
                 wpa={('slot1', '6631'): .4})
        return r

    def test_route_timings_and_model_counts_do_not_mix_boots_orders(self):
        rows = [self.record(12, 14, 1), self.record(14, 16, 1, 0), self.record(18, 9, 0)]
        b = aggregate(rows)[0]
        paths = {p['id']: p for p in b['paths']}
        self.assertEqual((paths['6631:1']['games'], paths['6631:1']['wins']), (2, 1))
        item_first = next(c for c in paths['6631:1']['choices'] if c['kind'] == 'slot1')
        boots_first = next(c for c in paths['6631:0']['choices'] if c['kind'] == 'slot1')
        self.assertEqual(item_first['timeSum'] / item_first['timeCount'], 13)
        self.assertEqual(boots_first['timeSum'] / boots_first['timeCount'], 18)
        self.assertEqual(item_first['residN'], 2)
        self.assertAlmostEqual(item_first['residSum'], .2)
        self.assertNotIn('matchId', json.dumps(paths))
        self.assertNotIn('playerId', json.dumps(paths))
        self.assertNotIn('_paths', json.dumps(b))

    def test_missing_boots_and_uncertain_ledgers_never_invent_a_route(self):
        no_boots = self.record(12, 14, 1)
        no_boots['boots'] = None
        uncertain = self.record(12, 14, 1)
        uncertain['ledgerUncertain'] = True
        self.assertEqual(aggregate([no_boots, uncertain])[0]['paths'], [])

    def test_late_boots_and_dimensions_remain_separate(self):
        third, fourth = self.record(12, 28, 3), self.record(12, 33, 4)
        other_region = dict(third, region='KR')
        groups = aggregate([third, fourth, other_region])
        self.assertEqual(len(groups), 2)
        self.assertEqual({p['id'] for p in groups[0]['paths']}, {'6631:3', '6631:4'})
        self.assertEqual(groups[1]['paths'][0]['games'], 1)


class BestBuildTests(unittest.TestCase):
    CATALOG = dict(BuildPathTests.CATALOG, **{
        '1036': {'name': 'Long Sword', 'into': ['3044'], 'gold': {'total': 350}},
        '3044': {'name': 'Phage', 'from': ['1036'], 'into': ['6631'], 'gold': {'total': 1100}},
        '6631': {'name': 'Stridebreaker', 'from': ['3044'], 'gold': {'total': 3300}},
    })

    def test_first_component_prefers_the_big_part_bought_after_the_start(self):
        from engine import first_component
        buys = [dict(item=i, time=t * 60000) for i, t in [(1036, 0.5), (1036, 4), (3044, 7), (6631, 12)]]
        build = [('6631', 'Stridebreaker', 12.0)]
        self.assertEqual(first_component(buys, build, self.CATALOG), ('3044', 'Phage', 7.0))
        # No big part before the item: fall back to the first component, never to the starting Long Sword.
        self.assertEqual(first_component(buys[:2] + buys[3:], build, self.CATALOG), ('1036', 'Long Sword', 4.0))
        self.assertIsNone(first_component(buys, [], self.CATALOG))

    def test_builds_keep_their_own_timings_component_and_boots(self):
        def rec(win, first, comp_min, boots_before):
            return dict(champion='Sett', opponent='Teemo', role='TOP', patch='16.19', region='EUW1', win=win, package=None,
                        ledgerUncertain=False, purchases=[], runes=None, spells=None,
                        build=[('6631', 'Stridebreaker', first), ('3053', "Sterak's Gage", 21.0), ('3071', 'Black Cleaver', 27.0)],
                        boots=('3047', 'Plated Steelcaps', 14.0, boots_before), component=('3044', 'Phage', comp_min))
        b = aggregate([rec(1, 12.0, 7.0, 1), rec(0, 14.0, 9.0, 1), rec(1, 13.0, 8.0, 0)])[0]
        build = b['builds'][0]
        self.assertEqual((build['id'], build['games'], build['wins']), ('6631>3053>3071', 3, 2))
        self.assertEqual(build['timeSum'][0], 39.0)
        self.assertEqual(build['components']['3044']['games'], 3)
        self.assertEqual(build['boots']['3047:1']['games'], 2)
        self.assertNotIn('_builds', json.dumps(b))
        self.assertEqual({c['kind'] for c in b['choices'] if c['id'] == '3044'}, {'component'})


class ReliabilityTests(unittest.TestCase):
    """The site may only show per-item effects that reproduce between halves of the data."""

    def records(self, effect, games=4000, items=('a', 'b', 'c', 'd'), seed=3):
        rng = random.Random(seed)
        out = []
        for g in range(games):
            for champ_i in range(25):
                item = items[(g + champ_i) % len(items)]
                p = 0.5
                win = int(rng.random() < p + effect.get(item, 0))
                out.append({'champion': f'C{champ_i}', 'role': 'TOP', 'matchId': f'M{g}-{champ_i}', 'win': win,
                            'wpa': {('slot1', item): p}, 'lane': {}})
        return out

    def test_pure_noise_is_never_published(self):
        from engine import reliability
        report, _ = reliability(self.records({}, games=400))
        self.assertFalse(report['wpa']['slot1']['pass'])
        self.assertLess(abs(report['wpa']['slot1']['splitHalfR']), 0.4)

    def test_real_differences_between_items_pass(self):
        from engine import reliability
        report, pooled = reliability(self.records({'a': .15, 'b': -.15}, games=400))
        self.assertTrue(report['wpa']['slot1']['pass'])
        n, total, _ = pooled['wpa']['TOP']['slot1']['a']
        self.assertGreater(total / n, 0.05)  # pooled centred effect of the good item is positive
        self.assertIsNotNone(report['wpa']['slot1']['neededGames'])


class CurveTests(unittest.TestCase):
    def test_curves_sum_the_change_after_each_purchase(self):
        rec = dict(champion='Sett', opponent='Teemo', role='TOP', patch='16.19', region='EUW1', win=1, package=None,
                   ledgerUncertain=False, purchases=[], runes=None, spells=None, build=[('6631', 'Stridebreaker', 12.0)],
                   boots=None, curve={('slot1', '6631'): [0.50, 0.52, 0.55]})
        other = dict(rec, curve={('slot1', '6631'): [0.40, 0.40, 0.46]})
        b = aggregate([rec, other])[0]
        c = next(c for c in b['choices'] if c['kind'] == 'slot1')
        self.assertEqual(c['curveN'], 2)
        self.assertAlmostEqual(c['curveSum'][0], 0.02)            # +0.02 and +0.00 after 1 minute
        self.assertAlmostEqual(c['curveSum'][1], 0.11)            # +0.05 and +0.06 after 2 minutes
        self.assertNotIn('curveSum', json.dumps(b['paths']))      # routes stay small


class RobustReliabilityTests(unittest.TestCase):
    def test_one_extreme_item_cannot_pass_the_check(self):
        """35 noise items plus one rare item bought only when already winning (like tier-3 boots).
        Plain correlation between halves is ~0.8 here, a false pass; the rank-based check must not pass it."""
        from engine import _reliability
        rng = random.Random(5)
        obs = []
        for g in range(9000):
            item = 'rare' if g % 250 == 0 else f'i{g % 34}'
            value = 0.13 + rng.gauss(0, 0.05) if item == 'rare' else rng.gauss(0, 0.1)
            obs.append((('TOP', 'boots'), item, f'M{g}', value))
        self.assertFalse(_reliability(obs, 0.02, shuffles=2)['pass'])

    def test_tier_three_boots_are_not_the_boots_choice(self):
        from engine import build_path
        items = dict(BuildPathTests.CATALOG, **{
            '3020': {'name': "Sorcerer's Shoes", 'tags': ['Boots'], 'from': ['1001'], 'gold': {'total': 1100}},
            '3175': {'name': "Spellslinger's Shoes", 'tags': ['Boots'], 'from': ['3020'], 'gold': {'total': 1600}},
        })
        buys = [dict(item=i, time=t * 60000) for i, t in [(1001, 5), (3175, 25), (3020, 9)]]
        _, boots = build_path(buys, items)
        self.assertEqual(boots[0], '3020')
