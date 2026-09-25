import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const code=readFileSync(new URL('../app.js',import.meta.url),'utf8').replace(/setup\(\);\s*$/,'');
const values={'#role':'TOP','#patch':'All collected patches','#region':'All collected regions'};
const context=vm.createContext({document:{querySelector:s=>({value:values[s]})},console,Map,Set,Date});
vm.runInContext(code,context);
const run=expression=>vm.runInContext(expression,context);
test('Wilson endpoints and empty sample are honest',()=>{
 assert.equal(run('wilson(0,0)'),null);
 assert.equal(run('pct(0,0)'),'—');
 const ci=run('wilson(50,100)');assert.ok(ci[0]<50&&ci[1]>50);
});
test('no fabricated numbers when no records exist',()=>{
 assert.equal(run('merged([]).games'),0);assert.equal(run('merged([]).choices.size'),0);
});
test('matchup, role, region and patch filters apply exactly',()=>{
 run(`stats={buckets:[{champion:'Sett',opponent:'Teemo',role:'TOP',patch:'16.18',region:'EUW1'},{champion:'Sett',opponent:'Darius',role:'TOP',patch:'16.18',region:'EUW1'}]}`);
 assert.equal(run('matches().length'),2);
 run(`state.opponent='Teemo'`);
 assert.equal(run('matches().length'),1);
 values['#region']='NA1';assert.equal(run('matches().length'),0);values['#region']='All collected regions';
 values['#role']='BOTTOM';assert.equal(run('matches().length'),0);values['#role']='TOP';
 values['#patch']='16.17';assert.equal(run('matches().length'),0);values['#patch']='All collected patches';
});
test('untrusted labels are escaped',()=>{
 assert.equal(run(`esc('<img src=x onerror=alert(1)>')`),'&lt;img src=x onerror=alert(1)&gt;');
});
test('counts merge before percentages are calculated',()=>{
 const value=run(`merged([{games:10,wins:8,eligible:{items:10},choices:[{kind:'items',id:'1',label:'A',games:5,wins:4,timeSum:20,timeCount:5}]},{games:90,wins:27,eligible:{items:90},choices:[{kind:'items',id:'1',label:'A',games:15,wins:3,timeSum:60,timeCount:15}]}])`);
 assert.equal(value.games,100);assert.equal(value.wins,35);assert.equal(value.choices.get('items:1').games,20);assert.equal(value.choices.get('items:1').wins,7);
});
test('raw Riot buckets take precedence over overlapping aggregate imports',()=>{
 run(`stats={sources:[{id:'riot',type:'riot_match_timelines'},{id:'licensed',type:'aggregate_import'}],buckets:[{sourceId:'licensed',champion:'Sett',opponent:'Teemo',role:'TOP',patch:'16.18',region:'EUW1',games:900},{sourceId:'riot',champion:'Sett',opponent:'Teemo',role:'TOP',patch:'16.18',region:'EUW1',games:25}]}`);
 assert.equal(run('selection().source.id'),'riot');assert.equal(run('matches()[0].games'),25);
});
test('largest single aggregate source is selected without double counting providers',()=>{
 run(`stats={sources:[{id:'a',type:'aggregate_import'},{id:'b',type:'aggregate_import'}],buckets:[{sourceId:'a',champion:'Sett',opponent:'Teemo',role:'TOP',patch:'16.18',region:'EUW1',games:100,wins:50,eligible:{},choices:[]},{sourceId:'b',champion:'Sett',opponent:'Teemo',role:'TOP',patch:'16.18',region:'EUW1',games:200,wins:90,eligible:{},choices:[]}]}`);
 assert.equal(run('selection().source.id'),'b');assert.equal(run('merged(matches()).games'),200);
});
test('champ select shows the most common skill order; old buckets without it still render',()=>{
 run(`state.champion='Sett';state.opponent='All matchups'`);
 const old=`{games:40,wins:20,eligible:{spells:40},choices:[{kind:'spells',id:'4+12',label:'4+12',games:40,wins:20}]}`;
 const html=run(`loadoutView(merged([${old}]),null)`);
 assert.match(html,/Summoner spells/);assert.doesNotMatch(html,/Skill order/);
 assert.equal(run(`merged([{games:1,wins:0,choices:[]}]).eligible.skills`),0);
 const now=`{games:40,wins:20,eligible:{spells:40,skills:40},choices:[{kind:'spells',id:'4+12',label:'4+12',games:40,wins:20},
  {kind:'skillMax',id:'Q>W>E',label:'Q>W>E',games:24,wins:12},{kind:'skillMax',id:'Q>E>W',label:'Q>E>W',games:6,wins:3},
  {kind:'skillStart',id:'W>E>Q',label:'W>E>Q',games:30,wins:15},{kind:'skillStart',id:'Q>E>W',label:'Q>E>W',games:10,wins:5}]}`;
 const out=run(`loadoutView(merged([${now}]),null)`);
 assert.match(out,/Summoner spells.*Skill order/s);
 assert.match(out,/<strong>Q &gt; W &gt; E<\/strong>/);assert.match(out,/start W-E-Q/);
 assert.match(out,/80\.0% of 30 games/);
 assert.match(out,/level W-E-Q first and max Q &gt; W &gt; E/);
});
