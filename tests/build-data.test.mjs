import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,readdirSync,readFileSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {buildData,splitStats} from '../scripts/build-data.mjs';

const now=new Date('2026-09-25T08:00:00Z');
const choice=(id,label,games)=>({kind:'items',id,label,games,wins:1,timeSum:12.5,timeCount:games});
const bucket=(champion,opponent,region,games,extra={})=>({champion,opponent,role:'TOP',patch:'16.19',region,games,wins:1,eligible:{items:games},
  choices:[choice('3071','Black Cleaver 12" ] } [ { \\',games),choice('3053','Sterak’s Gage — ünïcødé',games)],...extra});
// Buckets sit between metadata fields, and pooledEffects follows them as in the real export.
const STATS={schemaVersion:2,generatedAt:'2026-09-25T04:08:16Z',status:'observed',wpaStatus:'prototype',wpaModel:null,rankStatus:'unavailable',uniqueMatches:7,
  sources:[{id:'riot-match-v5',name:'Riot Match-V5 timelines',type:'riot_match_timelines',generatedAt:'2026-09-25T04:08:16Z',supportsWpaResearch:true,note:'Local.'}],
  buckets:[bucket('Garen','Darius','EUW1',3),bucket('Riven','Garen','KR',2,{sourceId:'riot-match-v5'}),bucket('Garen','Darius','KR',1),bucket('Garen','Riven','EUW1',1)],
  pooledEffects:{wpa:{TOP:{slot1:{'3071':[4,.5,.25]}}}}};
const IMPORT={importSchemaVersion:1,rightsConfirmed:true,obtainedAt:'2026-09-24T00:00:00Z',
  provider:{id:'partner-a',name:'Partner A',sourceUrl:'https://example.org/data'},buckets:[bucket('Garen','Darius','EUW1',5),bucket('Aatrox','Garen','EUW1',2)]};

function fixture(statsText){
  const dir=mkdtempSync(join(tmpdir(),'build-data-'));
  mkdirSync(join(dir,'imports'));
  writeFileSync(join(dir,'stats.json'),statsText);
  writeFileSync(join(dir,'imports','a.json'),JSON.stringify(IMPORT));
  return {dir,stats:join(dir,'stats.json'),imports:join(dir,'imports')};
}
const tree=dir=>Object.fromEntries(['index.json',...readdirSync(join(dir,'champions')).sort().map(n=>'champions/'+n)].map(n=>[n,readFileSync(join(dir,n),'utf8')]));

test('the large-export path writes byte-identical shards and index to the in-memory path',async()=>{
  const f=fixture(JSON.stringify(STATS,null,2));
  try{
    const memory=await buildData({...f,out:join(f.dir,'a'),streamAbove:Infinity,now});
    const streamed=await buildData({...f,out:join(f.dir,'b'),streamAbove:0,now});
    assert.equal(memory.streamed,false);assert.equal(streamed.streamed,true);
    const a=tree(join(f.dir,'a')),b=tree(join(f.dir,'b'));
    assert.deepEqual(Object.keys(b),['index.json','champions/Aatrox.json','champions/Garen.json','champions/Riven.json']);
    assert.deepEqual(b,a);
    assert.equal(streamed.buckets,6);assert.equal(streamed.imports,1);
    const garen=JSON.parse(b['champions/Garen.json']).buckets;
    assert.deepEqual(garen.map(x=>[x.region,x.games,x.sourceId]),[['EUW1',3,'riot-match-v5'],['KR',1,'riot-match-v5'],['EUW1',1,'riot-match-v5'],['EUW1',5,'partner-a']]);
    assert.equal(garen[0].choices[0].label,STATS.buckets[0].choices[0].label);
    const index=JSON.parse(b['index.json']);
    assert.deepEqual(index.pooledEffects,STATS.pooledEffects);
    assert.deepEqual(index.sources.map(s=>s.id),['riot-match-v5','partner-a']);
    assert.deepEqual(index.coverage.find(r=>r.champion==='Garen'&&r.opponent==='Darius'),{champion:'Garen',role:'TOP',opponent:'Darius',games:9});
  }finally{rmSync(f.dir,{recursive:true,force:true});}
});

test('splitting agrees with JSON.parse on compact and indented exports',()=>{
  for(const text of [JSON.stringify(STATS),JSON.stringify(STATS,null,'\t').replace(/\n/g,'\r\n')]){
    const buf=Buffer.from(text),{meta,ranges}=splitStats(buf);
    assert.deepEqual({...meta,buckets:ranges.map(([s,e])=>JSON.parse(buf.toString('utf8',s,e)))},JSON.parse(text));
  }
  assert.deepEqual(splitStats(Buffer.from('{"schemaVersion":2,"buckets":[]}')).ranges,[]);
});

test('a malformed large export fails closed and leaves the previous output untouched',async()=>{
  const good=JSON.stringify(STATS),extra=structuredClone(STATS);extra.buckets[1].matchId='EUW1_1';
  const cases={
    truncated:[good.slice(0,-40),SyntaxError],
    'trailing comma':[good.replace('}],"pooledEffects"','},],"pooledEffects"'),SyntaxError],
    'bad token in a bucket':[good.replace('"games":3,','"games":3x,'),SyntaxError],
    'trailing garbage':[good+'x',SyntaxError],
    'unterminated string':[good.slice(0,good.indexOf('Black')),SyntaxError],
    'duplicate buckets':[good.replace('{"schemaVersion":2,','{"schemaVersion":2,"buckets":[],'),SyntaxError],
    'missing buckets':[JSON.stringify({...STATS,buckets:undefined}),/Invalid Riot statistics export/],
    'unreviewed bucket field':[JSON.stringify(extra),/Invalid aggregate/],
  };
  for(const [name,[text,error]] of Object.entries(cases)){
    const f=fixture(text),out=join(f.dir,'out');
    mkdirSync(out);writeFileSync(join(out,'index.json'),'previous');
    try{
      await assert.rejects(buildData({...f,out,streamAbove:0,now}),error,name);
      assert.equal(readFileSync(join(out,'index.json'),'utf8'),'previous',name);
    }finally{rmSync(f.dir,{recursive:true,force:true});}
  }
});

test('imports are still checked on the large-export path',async()=>{
  const f=fixture(JSON.stringify(STATS));
  try{
    writeFileSync(join(f.imports,'b.json'),JSON.stringify(IMPORT));
    await assert.rejects(buildData({...f,out:join(f.dir,'out'),streamAbove:0,now}),/Duplicate source id: partner-a/);
    writeFileSync(join(f.imports,'b.json'),JSON.stringify({...IMPORT,rightsConfirmed:false}));
    await assert.rejects(buildData({...f,out:join(f.dir,'out'),streamAbove:0,now}),/Invalid aggregate import: b.json/);
  }finally{rmSync(f.dir,{recursive:true,force:true});}
});

test('the build cannot replace an unrelated directory',async()=>{
  const f=fixture(JSON.stringify(STATS));
  try{
    await assert.rejects(buildData({...f,out:f.dir,streamAbove:0,now}),/Refusing to replace/);
    assert.equal(readFileSync(f.stats,'utf8'),JSON.stringify(STATS));
  }finally{rmSync(f.dir,{recursive:true,force:true});}
});
