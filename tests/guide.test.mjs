import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const nodes=new Map();
const context=vm.createContext({document:{querySelector:()=>({value:'TOP'}),getElementById:id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);}},console,Map,Set,Date});
vm.runInContext(readFileSync('app.js','utf8').replace(/setup\(\);\s*$/,''),context);
vm.runInContext(readFileSync('guide.js','utf8'),context);
const run=s=>vm.runInContext(s,context);
const json=s=>JSON.parse(run(`JSON.stringify(${s})`));
test('path aggregation combines counts, timings and residuals within the same observed route',()=>{
 const out=run(`guidePaths([{paths:[{id:'6631:1',firstItem:'6631',bootsBefore:1,games:10,wins:6,choices:[{kind:'slot1',id:'6631',label:'Stride',games:10,wins:6,timeCount:10,timeSum:120,residN:10,residSum:1,residSq:2}]}]},
 {paths:[{id:'6631:1',firstItem:'6631',bootsBefore:1,games:20,wins:10,choices:[{kind:'slot1',id:'6631',label:'Stride',games:20,wins:10,timeCount:20,timeSum:300,residN:20,residSum:2,residSq:4}]},
 {id:'6631:0',firstItem:'6631',bootsBefore:0,games:5,wins:2,choices:[]}]}])`);
 assert.equal(out.length,2);assert.equal(out[0].games,30);
 assert.equal(out[0].choices.get('slot1:6631').timeSum,420);
 assert.equal(out[0].choices.get('slot1:6631').residN,30);
 assert.equal(out[1].games,5);
});
test('old exports remain usable without route or build fields',()=>{
 assert.equal(run(`guidePaths([{games:20,choices:[]}]).length`),0);
 assert.equal(run(`guideBuilds([{games:20,choices:[]}]).length`),0);
 assert.equal(run(`guideRankBuilds([]).best`),null);
 assert.match(run(`guideTimeline([],[],false)`),/No complete builds yet/);
});
const BUILDS=`[{builds:[
 {id:'6631>3053>3071',items:['6631','3053','3071'],names:['Stridebreaker',"Sterak's Gage",'Black Cleaver'],games:8,wins:6,timeSum:[96,168,216],
  components:{'3044':{name:'Phage',games:6,wins:5,timeSum:42},'3077':{name:'Tiamat',games:2,wins:1,timeSum:16}},boots:{'3047:1':{name:'Plated Steelcaps',games:7,wins:5,timeSum:98}}},
 {id:'6631>2501>3053',items:['6631','2501','3053'],names:['Stridebreaker','Bloodmail',"Sterak's Gage"],games:30,wins:15,timeSum:[390,630,810],components:{},boots:{'3047:0':{name:'Plated Steelcaps',games:30,wins:15,timeSum:270}}}]},
 {builds:[{id:'6631>3053>3071',items:['6631','3053','3071'],names:['Stridebreaker',"Sterak's Gage",'Black Cleaver'],games:4,wins:3,timeSum:[48,84,108],
  components:{'3044':{name:'Phage',games:4,wins:3,timeSum:28}},boots:{'3047:1':{name:'Plated Steelcaps',games:4,wins:3,timeSum:56}}}]}]`;
// Same core, two boots orders with their own timings. Item-first: Phage 7, Stridebreaker 12, Steelcaps 14, Sterak's 21, Cleaver 27.
// Boots-first: Steelcaps 8, Phage 10, Stridebreaker 15, Sterak's 23, Cleaver 29. Split over two buckets (e.g. two regions).
const ROUTED=`[{builds:[{id:'6631>3053>3071',items:['6631','3053','3071'],names:['Stridebreaker',"Sterak's Gage",'Black Cleaver'],games:30,wins:16,timeSum:[400,650,830],
  components:{'3044':{name:'Phage',games:28,wins:15,timeSum:230}},boots:{'3047:0':{name:'Plated Steelcaps',games:14,wins:7,timeSum:112},'3047:1':{name:'Plated Steelcaps',games:10,wins:6,timeSum:140}},
  routes:{'3047:1:3044':{bootsName:'Plated Steelcaps',componentName:'Phage',games:10,timeSum:[120,210,270],bootsTimeSum:140,componentTimeSum:70},
   '3047:0:3044':{bootsName:'Plated Steelcaps',componentName:'Phage',games:12,timeSum:[180,276,348],bootsTimeSum:96,componentTimeSum:120},
   '3047:0:3077':{bootsName:'Plated Steelcaps',componentName:'Tiamat',games:2,timeSum:[32,48,60],bootsTimeSum:16,componentTimeSum:18},
   '3047:2:-':{bootsName:'Plated Steelcaps',componentName:'',games:3,timeSum:[39,63,81],bootsTimeSum:69,componentTimeSum:0}}}]},
 {builds:[{id:'6631>3053>3071',items:['6631','3053','3071'],names:['Stridebreaker',"Sterak's Gage",'Black Cleaver'],games:14,wins:7,timeSum:[200,300,400],components:{},boots:{},
  routes:{'3047:1:3044':{bootsName:'Plated Steelcaps',componentName:'Phage',games:6,timeSum:[72,126,162],bootsTimeSum:84,componentTimeSum:42},
   '3047:0:3044':{bootsName:'Plated Steelcaps',componentName:'Phage',games:8,timeSum:[120,184,232],bootsTimeSum:64,componentTimeSum:80}}}]}]`;
const routePlan=(buckets,route)=>json(`(()=>{const p=guideBuildPlan(guideBuilds(${buckets})[0],${route?`'${route}'`:'null'});return{route:p.route?.id,timing:p.timing,steps:p.steps.map(s=>s.kind+':'+s.id+':'+(s.minute==null?'-':Math.round(s.minute*100)/100)),options:(p.routes||[]).map(r=>r.id+':'+r.games)}})()`);
test('boots-first and item-first are separate routes, each timed only from its own games',()=>{
 const first=routePlan(ROUTED,'3047:0');
 assert.equal(first.route,'3047:0');
 assert.deepEqual(first.timing,{level:'route',games:20,component:'Phage'});
 assert.deepEqual(first.steps,['boots:3047:8','component:3044:10','slot1:6631:15','slot2:3053:23','slot3:3071:29']);
 const second=routePlan(ROUTED,'3047:1');
 assert.deepEqual(second.timing,{level:'route',games:16,component:'Phage'});
 assert.deepEqual(second.steps,['component:3044:7','slot1:6631:12','boots:3047:14','slot2:3053:21','slot3:3071:27']);
 // Routes are offered most common first: boots-first has 22 games (20 with Phage, 2 with Tiamat) across both buckets.
 assert.deepEqual(first.options,['3047:0:22','3047:1:16','3047:2:3']);
 assert.equal(routePlan(ROUTED).route,'3047:0');
 assert.equal(routePlan(ROUTED,'9999:9').route,'3047:0');
});
test('a thin route shows its observed order without times; a thin component falls back to the whole route',()=>{
 const thin=routePlan(ROUTED,'3047:2');
 assert.deepEqual(thin.timing,{level:'none',games:3,reason:'thin'});
 assert.deepEqual(thin.steps,['slot1:6631:-','slot2:3053:-','boots:3047:-','slot3:3071:-']);
 // 20 route games, but no single first component reaches 15: time the route itself and leave the component out.
 const split=ROUTED.replace("'3047:0:3044':{bootsName:'Plated Steelcaps',componentName:'Phage',games:8","'3047:0:1036':{bootsName:'Plated Steelcaps',componentName:'Long Sword',games:8");
 const fallback=routePlan(split,'3047:0');
 assert.deepEqual(fallback.timing,{level:'boots',games:22});
 // (96 + 16 + 64) boots minutes over 12 + 2 + 8 games: the whole route, not one component's subset.
 assert.equal(fallback.steps[0],'boots:3047:8');
 assert.equal(fallback.steps[1],'slot1:6631:15.09');
 assert.ok(fallback.steps.every(s=>!s.startsWith('component')));
 assert.match(run(`guideCohortNote({games:44,wins:23},guideBuildPlan(guideBuilds(${split})[0],'3047:0'))`),/Build record.*44 games with these three items.*Timings.*22 games on this route · first component varies/);
 assert.match(run(`guideTimeline(guideBuildPlan(guideBuilds(${ROUTED})[0],'3047:2').steps,[],false,{level:'none',games:3,reason:'thin'})`),/Timing unavailable for this route[^]*Only 3 games followed this exact route/);
});
test('older exports without route cohorts never combine averages from different groups',()=>{
 const merged=run(`guideBuilds(${BUILDS})`);
 const b=merged.find(w=>w.id==='6631>3053>3071');
 assert.equal(b.games,12);assert.equal(b.wins,9);assert.equal(b.timeSum[0],144);assert.equal(b.components['3044'].games,10);
 const old=json(`(()=>{const p=guideBuildPlan(guideBuilds(${BUILDS}).find(w=>w.id==='6631>3053>3071'));return{t:p.timing,s:p.steps.map(s=>s.kind+':'+s.minute)}})()`);
 assert.deepEqual(old,{t:{level:'none',games:0,reason:'noRoutes'},s:['slot1:null','boots:null','slot2:null','slot3:null']});
 // One bucket from an older export makes the merged route timings incomplete, so they are dropped.
 const mixed=`[${ROUTED.slice(1,-1)},{builds:[{id:'6631>3053>3071',items:['6631','3053','3071'],names:['A','B','C'],games:5,wins:2,timeSum:[60,90,120],components:{},boots:{}}]}]`;
 assert.equal(run(`guideBuilds(${mixed})[0].routes`),null);
 assert.match(run(`guideBuildMarkup(guideBuildPlan(guideBuilds(${BUILDS})[0]).steps)`),/class="when untimed">—</);
});
test('build route keeps each observed item in order and exposes the decorative path to reduced-motion users',()=>{
 const html=run(`guideBuildMarkup(guideBuildPlan(guideBuilds(${ROUTED})[0],'3047:1').steps)`);
 assert.match(html,/class="build-route" aria-hidden="true"/);
 assert.match(html,/Step 1 of 5, Phage at 7:00/);
 assert.match(html,/Step 5 of 5, Black Cleaver at 27:00/);
 assert.equal((html.match(/class="build-step/g)||[]).length,5);
 assert.doesNotMatch(html,/of these games/);
});
test('build ranking pulls small samples toward the average',()=>{
 // 11 games at 64% must not beat 200 games at 55% when the average is 50%.
 const r=json(`(()=>{const r=guideRankBuilds([{id:'lucky',games:11,wins:7,items:[],names:[]},{id:'solid',games:200,wins:110,items:[],names:[]},{id:'meh',games:100,wins:50,items:[],names:[]}]);return[r.best.id,r.eligible.map(w=>w.id)]})()`);
 assert.equal(r[0],'solid');
});
test('best build needs enough games; the most popular is always available',()=>{
 const r=json(`(()=>{const r=guideRankBuilds(guideBuilds(${BUILDS}));return[r.best.id,r.popular.id,r.eligible.length]})()`);
 assert.deepEqual(r,['6631>3053>3071','6631>2501>3053',2]);
 // Raise the bar above every build: fall back to the most popular one instead of a tiny lucky sample.
 const only=run(`guideRankBuilds(guideBuilds(${BUILDS}).map(w=>w.id==='6631>3053>3071'?{...w,games:9}:w))`);
 assert.equal(only.best.id,'6631>2501>3053');
});
test('first-item alternatives use the same boots order across first-item routes',()=>{
 const result=run(`(()=>{guideState.slot='slot1';const paths=guidePaths([{paths:[
  {id:'6631:1',firstItem:'6631',bootsBefore:1,games:20,wins:10,choices:[{kind:'slot1',id:'6631',label:'Stride',games:20,wins:10}]},
  {id:'3153:1',firstItem:'3153',bootsBefore:1,games:15,wins:5,choices:[{kind:'slot1',id:'3153',label:'Blade',games:15,wins:5}]},
  {id:'3153:0',firstItem:'3153',bootsBefore:0,games:10,wins:5,choices:[{kind:'slot1',id:'3153',label:'Blade',games:10,wins:5}]}]}]);return guideComparisonSample(paths,paths[0],paths[0]);})()`);
 assert.equal(result.games,35);assert.equal(result.choices.get('slot1:3153').games,15);assert.equal(result.choices.size,2);
});
test('sparse matchup timing is labeled as the general champion sample and shows no opponent line',()=>{
 const html=run(`guideTimeline([{kind:'slot1',id:'6631',label:'Stridebreaker',minute:12}],[],true)`);
 assert.match(html,/General champion sample/);assert.doesNotMatch(html,/enemy/);
});
test('imported or unsupported data never receives adjusted estimates',()=>{
 run(`stats.wpaModel={games:1000};guideState.slot='slot1'`);
 const summary=`merged([{games:30,wins:20,choices:[{kind:'slot1',id:'6631',label:'Stridebreaker',games:30,wins:20,residN:30,residSum:3,residSq:5}]}])`;
 assert.doesNotMatch(run(`guideComparison(${summary},false)`),/ pp/);
 // Even local model data stays hidden until the slot's estimates reproduce between halves of the data.
 run(`stats.reliability={wpa:{slot1:{pass:false,splitHalfR:0.02,neededGames:2400}}}`);
 assert.doesNotMatch(run(`guideComparison(${summary},true)`),/ pp/);
 assert.match(run(`guideComparison(${summary},true)`),/not reproducible yet/);
 run(`stats.reliability={wpa:{slot1:{pass:true,splitHalfR:0.6}}}`);
 assert.match(run(`guideComparison(${summary},true)`),/ pp/);
 run(`delete stats.reliability`);
});
test('real labels are escaped and timing rounds across minute boundaries',()=>{
 assert.equal(run('guideTime(12.999)'), '13:00');
 run(`guideState.slot='slot1'`);
 assert.doesNotMatch(run(`guideComparison(merged([{games:1,wins:0,choices:[{kind:'slot1',id:'1',label:'<script>alert(1)</script>',games:1,wins:0}]}]),false)`),/<script>/);
});
test('win chance curve is relative to the slot and only drawn where each minute passed the check',()=>{
 const rows=`[{kind:'slot1',id:'a',label:'A',games:100,wins:50,curveN:100,curveSum:[3,6],curveSq:[1,2]},{kind:'slot1',id:'b',label:'B',games:100,wins:50,curveN:100,curveSum:[1,2],curveSq:[1,2]}]`;
 const pts=json(`guideCurve(${rows},${rows}[0])`);
 assert.equal(pts.length,2);
 assert.ok(Math.abs(pts[0].m-1)<1e-9);                 // 3% vs slot average 2% after 1 minute: +1 pp
 assert.ok(Math.abs(pts[1].m-2)<1e-9);                 // 6% vs 4% after 2 minutes: +2 pp
 run(`stats.reliability={curve:{slot1:{'1':{pass:true},'2':{pass:false,neededGames:400,splitHalfR:0.1}}}}`);
 const html=run(`guideCurveSection(${rows},${rows}[0],'slot1','A','1st item')`);
 assert.match(html,/<svg/);assert.match(html,/Minute 2 is not reproducible yet/);
 run(`stats.reliability={curve:{slot1:{'1':{pass:false,neededGames:400},'2':{pass:false,neededGames:400}}}}`);
 const hidden=run(`guideCurveSection(${rows},${rows}[0],'slot1','A','1st item')`);
 assert.doesNotMatch(hidden,/<svg/);assert.match(hidden,/Not reproducible yet/);
 run(`delete stats.reliability`);
});
test('pooled role curve is shown, labeled, only when the champion curve is not ready and the pooled one passes',()=>{
 const rows=`[{kind:'boots',id:'3047',label:'Steelcaps',games:40,wins:20,curveN:40,curveSum:[0.4,0.8],curveSq:[1,2]}]`;
 run(`stats.reliability={curve:{boots:{'1':{pass:false,neededGames:300},'2':{pass:false,neededGames:300}}},curvePooled:{boots:{'1':{pass:true},'2':{pass:true}}}};stats.pooledEffects={curve:{TOP:{boots:{'3047':[500,[5,10],[20,40]]}}}}`);
 const html=run(`guideCurveSection(${rows},${rows}[0],'boots','Steelcaps','Boots')`);
 assert.match(html,/Across <b>all .* players<\/b>/);assert.match(html,/<svg/);assert.match(html,/500 games/);
 run(`stats.reliability.curvePooled.boots['1'].pass=false;stats.reliability.curvePooled.boots['2'].pass=false`);
 assert.doesNotMatch(run(`guideCurveSection(${rows},${rows}[0],'boots','Steelcaps','Boots')`),/<svg/);
 run(`delete stats.reliability;delete stats.pooledEffects`);
});
test('fun fact compares first blood and first tower with the role average',()=>{
 const sel=json(`merged([{games:150,wins:75,firstBloodKill:24,firstBloodAssist:15,firstTowerKill:6,firstTowerAssist:30,choices:[]},
  {games:50,wins:25,firstBloodKill:8,firstBloodAssist:5,firstTowerKill:4,firstTowerAssist:10,choices:[]},{games:40,wins:20,choices:[]}])`);
 assert.deepEqual(sel.firsts,{games:200,firstBloodKill:32,firstBloodAssist:20,firstTowerKill:10,firstTowerAssist:40}); // imports without flags are left out
 run(`stats.firstObjectives={JUNGLE:{games:1000,firstBloodKill:140,firstBloodAssist:120,firstTowerKill:50,firstTowerAssist:150}}`);
 const fact=run(`guideFact(${JSON.stringify(sel)},'Sylas','JUNGLE','Jungle')`);
 assert.equal(fact,'Fun fact: Sylas draws first blood in 16% of these games (jungle average 14%) and helps take the first tower in 25% (jungle average 20%).');
 assert.equal(run(`guideFact({firsts:{games:99,firstBloodKill:9,firstBloodAssist:0,firstTowerKill:0,firstTowerAssist:0}},'Sylas','JUNGLE','Jungle')`),'');
 run(`delete stats.firstObjectives`);
 assert.match(run(`guideFact(${JSON.stringify(sel)},'Sylas','JUNGLE','Jungle')`),/in 16% of these games and helps take the first tower in 25%\.$/);
});
test('counters rank lane opponents by win rate pulled toward the usual rate',()=>{
 const buckets=`[
  {opponent:'Irelia',games:40,wins:28,choices:[{kind:'keystone',id:'8010',games:40,wins:28,laneN:20,laneDelta:2000}]},
  {opponent:'Irelia',games:20,wins:12,choices:[]},
  {opponent:'Lucky',games:16,wins:12,choices:[]},
  {opponent:'Darius',games:200,wins:80,choices:[]},
  {opponent:'Garen',games:300,wins:150,choices:[]},
  {opponent:'Rare',games:5,wins:0,choices:[]}]`;
 const m=json(`(()=>{const m=guideMatchups(${buckets});return{usual:m.usual,counters:m.counters.map(r=>r.id),countered:m.countered.map(r=>r.id),thin:m.thin,irelia:m.rows.find(r=>r.id==='Irelia')}})()`);
 assert.equal(m.usual,282/581*100);
 // Irelia (60 games at 67%) outranks Lucky (16 games at 75%) once both are pulled toward the usual rate.
 assert.deepEqual(m.counters,['Irelia','Lucky','Garen']);
 assert.deepEqual(m.countered,['Darius']);
 assert.equal(m.thin,1);assert.equal(m.irelia.games,60);assert.equal(m.irelia.lane.gold,100);assert.equal(m.irelia.clear,true);
 run(`guideBase=()=>({buckets:${buckets.replace('Darius','<b>x</b>')}})`);
 const html=run(`guideCounterView('countered')`);
 assert.match(html,/data-counter-opp="&lt;b&gt;x&lt;\/b&gt;"/);assert.doesNotMatch(html,/<b>x<\/b>/);
 assert.match(run(`guideCounterView('counters')`),/data-counter-opp="Irelia"[^]*data-counter-opp="Garen"/);
 run(`guideBase=()=>({buckets:[]})`);
 assert.match(run(`guideCounterView('counters')`),/No collected games/);
});
