import test from 'node:test';
import assert from 'node:assert/strict';
import {existsSync,mkdtempSync,readFileSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {validateRecommendations,copyRecommendations,MAX_RECOMMENDATION_BYTES} from '../scripts/recommendation-schema.mjs';

export const PREVIEW={schemaVersion:1,kind:'item_model_research_preview',generatedAt:'2026-09-25T06:36:03Z',status:'research_preview',
  claimStatus:'no_confirmed_advantage',recommendation:'observed_baseline_route_a',routeLevelClaim:false,pooling:'all_matchups_regions_players',
  sourcePatches:['16.19'],catalogVersions:{'16.19':'16.19.1'},regions:['EUW1','KR'],
  model:{version:'recommender-experimental-v0',artifactSha256:'d'.repeat(64),trainedAt:'2026-09-25T06:27:33Z',fitRows:1000,fitMatches:500,
    evaluationRows:300,evaluationMatches:150,policyVerdicts:{base:'insufficient_evidence',enriched:'insufficient_evidence'},headlineClaimsAllowed:false,minArmTrainRows:30,preferenceMargin:.03},
  scopes:{first_distinguishing_component:'First component',boots_upgrade_purchase:'Boots upgrade'},
  predictionFields:['finalWin','goldLeadChange5','takedowns5','deaths5','championDamage5','timeAlive5s'],
  outcomes:{finalWin:'a',goldLeadChange5:'b',takedowns5:'c',deaths5:'d',championDamage5:'e',timeAlive5s:'f'},caveats:['Research preview.'],
  entries:[
    {champion:'Kaisa',role:'BOTTOM',stage:'slot1',scope:'first_distinguishing_component',patch:'16.19',baselineRoute:'6672',alternativeRoute:'3087',
     support:{contexts:600,routeA:480,routeB:120,matches:600},status:'research_preview',supported:true,
     predicted:{routeA:[.4949,-13,1.82,.9,1989,285.2],routeB:[.5012,-13,1.82,.9,2001,285.4]},modelLean:'routeB'},
    {champion:'Kaisa',role:'BOTTOM',stage:'boots',scope:'boots_upgrade_purchase',patch:'16.19',baselineRoute:'3006',alternativeRoute:'3008',
     support:{contexts:40,routeA:25,routeB:15,matches:40},status:'research_preview',supported:false,predicted:null,modelLean:null}]};
const copy=()=>structuredClone(PREVIEW);
const rejects=(mutate,label)=>{const d=copy();mutate(d);assert.throws(()=>validateRecommendations(d),/Invalid aggregate/,label);};

test('model preview schema accepts the research preview',()=>{assert.equal(validateRecommendations(copy()).entries.length,2);});

test('full-data retrain stays explicitly untested in the public preview',()=>{
  const d=copy();d.model.version='recommender-experimental-r3-fulltrain';d.model.evaluationRows=0;d.model.evaluationMatches=0;
  d.model.policyVerdicts={base:'not_fitted',enriched:'not_fitted'};
  assert.equal(validateRecommendations(d).model.evaluationMatches,0);
  d.model.evaluationMatches=1;assert.throws(()=>validateRecommendations(d),/Invalid aggregate/);
});

test('model preview schema is closed to identifiers, per-game data and validated picks',()=>{
  rejects(d=>{d.entries[0].matchId='EUW1_123';},'match id');
  rejects(d=>{d.entries[0].playerRef='p1';},'player ref');
  rejects(d=>{d.entries[0].recommendedItem='3087';},'model-selected pick');
  rejects(d=>{d.recommendedRoute=['3087'];},'top-level pick');
  rejects(d=>{d.rows=[];},'per-row data');
  rejects(d=>{d.caveats.push('RGAPI-00000000-0000-0000-0000-000000000000');},'secret-looking text');
  rejects(d=>{d.model.artifactSha256='not-a-hash';},'artifact identity');
});

test('model preview schema cannot carry a claim',()=>{
  rejects(d=>{d.claimStatus='supported_improvement';},'claim status');
  rejects(d=>{d.status='validated';},'status');
  rejects(d=>{d.recommendation='model_route_b';},'recommendation');
  rejects(d=>{d.routeLevelClaim=true;},'route claim');
  rejects(d=>{d.model.headlineClaimsAllowed=true;},'headline');
  rejects(d=>{d.model.policyVerdicts.enriched='supported_improvement';},'verdict');
  rejects(d=>{d.entries[0].status='recommended';},'entry status');
});

test('model preview schema enforces support, lean and prediction invariants',()=>{
  rejects(d=>{d.entries[0].modelLean='routeA';},'lean disagrees with predictions');
  rejects(d=>{d.entries[1].predicted=d.entries[0].predicted;d.entries[1].modelLean='routeB';},'unsupported pair with predictions');
  rejects(d=>{d.entries[1].supported=true;d.entries[1].predicted=d.entries[0].predicted;d.entries[1].modelLean='routeB';},'support below minimum');
  rejects(d=>{d.entries[0].support.routeA=1;},'support does not add up');
  rejects(d=>{d.entries[0].predicted.routeA[0]=1.2;},'probability out of range');
  rejects(d=>{d.entries[0].predicted.routeA.pop();},'missing prediction');
  rejects(d=>{d.entries[0].predicted.routeA[0]=null;},'null prediction');
  rejects(d=>{d.predictionFields.reverse();},'field order');
  rejects(d=>{d.entries.push(structuredClone(d.entries[0]));},'duplicate pair');
  rejects(d=>{d.entries[1].scope='first_distinguishing_component';},'boots scope');
  rejects(d=>{d.entries[0].patch='16.20';},'unknown patch');
});

test('build copy is optional, fails closed and writes only validated data',async()=>{
  const dir=mkdtempSync(join(tmpdir(),'preview-')),from=join(dir,'in.json'),to=join(dir,'out.json');
  try{
    assert.equal(await copyRecommendations(from,to),null);assert.equal(existsSync(to),false);
    writeFileSync(from,'{"schemaVersion":1,');await assert.rejects(copyRecommendations(from,to),SyntaxError);assert.equal(existsSync(to),false);
    const bad=copy();bad.entries[0].matchId='EUW1_1';writeFileSync(from,JSON.stringify(bad));
    await assert.rejects(copyRecommendations(from,to),/Invalid aggregate/);assert.equal(existsSync(to),false);
    writeFileSync(from,JSON.stringify(copy())+' '.repeat(MAX_RECOMMENDATION_BYTES));await assert.rejects(copyRecommendations(from,to),/larger than/);
    writeFileSync(from,JSON.stringify(copy(),null,2));assert.equal((await copyRecommendations(from,to)).entries.length,2);
    assert.deepEqual(JSON.parse(readFileSync(to,'utf8')),PREVIEW);
  }finally{rmSync(dir,{recursive:true,force:true});}
});

test('the exported preview, when present, passes the schema and stays modest',{skip:!existsSync('data/public/recommendations.json')&&'no local export'},()=>{
  const raw=readFileSync('data/public/recommendations.json','utf8');
  assert.ok(Buffer.byteLength(raw)<=MAX_RECOMMENDATION_BYTES);
  validateRecommendations(JSON.parse(raw));
  assert.doesNotMatch(raw,/[A-Z]{2,4}\d?_\d{6,}|puuid|player_?ref|match_?id|pair_?id/i);
});
