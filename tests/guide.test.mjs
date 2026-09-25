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
test('builds merge across buckets and play out in purchase order',()=>{
 const merged=run(`guideBuilds(${BUILDS})`);
 const b=merged.find(w=>w.id==='6631>3053>3071');
 assert.equal(b.games,12);assert.equal(b.wins,9);assert.equal(b.timeSum[0],144);assert.equal(b.components['3044'].games,10);
 const plan=json(`guideBuildPlan(guideBuilds(${BUILDS}).find(w=>w.id==='6631>3053>3071')).map(s=>s.kind+':'+s.id+':'+s.minute)`);
 // first component first, then item 1, boots after one item, then items 2 and 3
 assert.deepEqual(plan,['component:3044:7','slot1:6631:12','boots:3047:14','slot2:3053:21','slot3:3071:27']);
 assert.deepEqual(json(`guideBuildPlan(guideBuilds(${BUILDS}).find(w=>w.id==='6631>2501>3053')).map(s=>s.kind)`),['boots','slot1','slot2','slot3']);
 assert.deepEqual(json(`guideBuildPlan({items:['6631','3053','3071'],names:['A','B','C'],games:10,timeSum:[120,200,270],boots:{'3047:0':{name:'Boots',games:10,timeSum:80}},components:{'3044':{name:'Part',games:10,timeSum:90}}}).map(s=>s.kind)`),['boots','component','slot1','slot2','slot3']);
});
test('build route keeps each observed item in order and exposes the decorative path to reduced-motion users',()=>{
 const html=run(`guideBuildMarkup(guideBuildPlan(guideBuilds(${BUILDS}).find(w=>w.id==='6631>3053>3071')))`);
 assert.match(html,/class="build-route" aria-hidden="true"/);
 assert.match(html,/Step 1 of 5, Phage/);
 assert.match(html,/Step 5 of 5, Black Cleaver/);
 assert.equal((html.match(/class="build-step/g)||[]).length,5);
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
const RESEARCH=JSON.stringify({schemaVersion:1,kind:'item_model_research_preview',status:'research_preview',claimStatus:'no_confirmed_advantage',
 recommendation:'observed_baseline_route_a',routeLevelClaim:false,sourcePatches:['16.19'],predictionFields:['finalWin','goldLeadChange5','takedowns5','deaths5','championDamage5','timeAlive5s'],
 model:{version:'recommender-experimental-v0',trainedAt:'2026-09-25T06:27:33Z',fitRows:460896,policyVerdicts:{base:'insufficient_evidence',enriched:'insufficient_evidence'},headlineClaimsAllowed:false,minArmTrainRows:30,preferenceMargin:.03},
 entries:[{champion:'Kaisa',role:'BOTTOM',stage:'slot1',patch:'16.19',scope:'first_distinguishing_component',baselineRoute:'6672',alternativeRoute:'3087',support:{contexts:6057,routeA:4864,routeB:1193,matches:6057},status:'research_preview',supported:true,
   predicted:{routeA:[.4949,-13,1.82,.9,1989,285.2],routeB:[.5012,26,1.82,.9,2001,285.4]},modelLean:'routeB'},
  {champion:'Kaisa',role:'BOTTOM',stage:'boots',patch:'16.19',scope:'boots_upgrade_purchase',baselineRoute:'3006',alternativeRoute:'3008',support:{contexts:40,routeA:25,routeB:15,matches:40},status:'research_preview',supported:false,predicted:null,modelLean:null}]});
const card=(slot,research=`{status:'ready',doc:${RESEARCH}}`)=>run(`(()=>{guideAssets={itemNames:{'6672':'Kraken Slayer','3087':'Statikk Shiv','3006':"Berserker's Greaves",'3008':'Swiftmarch'},itemIds:[]};return guideResearchCard(${research},'Kaisa','BOTTOM','${slot}')})()`);
test('model research card shows predictions as an experimental, pooled preview and keeps the observed route',()=>{
 const html=card('slot1');
 assert.match(html,/Experimental · research preview/);
 assert.match(html,/Pooled across all matchups and regions · patch 26\.19 · not specific to this opponent/);
 assert.match(html,/No model-backed switch/);
 assert.match(html,/Kraken Slayer is this comparison’s most common training route/);
 assert.match(html,/First component bought toward Kraken Slayer or Statikk Shiv; not the finished items/);
 assert.match(html,/<td>49\.5%<i class=\"mr-bar a\"[^]*?<th scope=\"row\">Final win chance<\/th><td>50\.1%/);
 assert.match(html,/<td>−13<i[^]*?Team gold-lead change<\/th><td>26<i/);
 // The model's side of the card never labels an item as the model's pick.
 assert.doesNotMatch(html,/mr-tag">[^<]*lean/i);
 assert.match(html,/Model lean: Statikk Shiv, by 0\.6 points of predicted win chance, smaller than the 3-point margin/);
 assert.match(html,/A prediction, not a measured or confirmed advantage/);
 assert.match(html,/6[.,]057 training decisions/);assert.match(html,/No uncertainty interval/);
 assert.match(html,/held-out policy check: insufficient evidence · no route-level claim/);
 // No win-rate gain wording: no signed pp values, no "better", no model pick presented as the recommendation.
 assert.doesNotMatch(html,/\+\s?\d|\bpp\b|better|recommend(ed)? Statikk|Recommendation stays Statikk/i);
});
test('model research card degrades gracefully',()=>{
 assert.match(card('boots'),/No model-backed switch/);
 assert.match(card('boots'),/Berserker&#39;s Greaves is this comparison’s most common training route/);
 assert.match(card('boots'),/Too few training decisions for model predictions: 25 toward .* and 15 toward .*; needs 30 of each/);
 assert.doesNotMatch(card('boots'),/Model lean|<table/);
 const tiny=RESEARCH.replace('[0.5012,26,','[0.4953,26,');
 assert.match(card('slot1',`{status:'ready',doc:${tiny}}`),/No model lean: the predicted final win chances are equal to one decimal/);
 assert.doesNotMatch(card('slot1',`{status:'ready',doc:${tiny}}`),/Model lean:/);
 assert.match(card('slot2'),/No model comparison for Kaisa’s 2nd item/);
 assert.match(run(`guideResearchCard({status:'ready',doc:${RESEARCH}},'Kaisa','BOTTOM','slot1','16.18')`),/No model comparison/);
 assert.match(card('slot4'),/covers the 1st, 2nd and 3rd item and boots decisions only/);
 assert.match(card('slot1',`{status:'missing',doc:null}`),/not part of this build\. The observed build below is unaffected/);
 assert.match(card('slot1',`{status:'loading',doc:null}`),/Loading the model preview/);
});
test('model research loader accepts only the unclaimed preview schema',()=>{
 assert.equal(run(`guideResearchValid(${RESEARCH})`),true);
 for(const change of ["d.claimStatus='supported'","d.routeLevelClaim=true","d.recommendation='model'","d.model.headlineClaimsAllowed=true","d.predictionFields.reverse()","d.schemaVersion=2","d.entries=null"])
  assert.equal(run(`(()=>{const d=${RESEARCH};${change};return guideResearchValid(d)})()`),false,change);
 assert.equal(run(`guideResearchValid(null)`),false);
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
