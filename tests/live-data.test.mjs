import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {validateRelease} from '../scripts/aggregate-schema.mjs';
import {publishRelease,readReleaseFile,hash} from '../scripts/live-store.mjs';
import {gunzipSync} from 'node:zlib';
import {serveData} from '../netlify/functions/live-data.mjs';
import {refresh} from '../scripts/refresh-data.mjs';

const fixture=()=>new Map([
  ['index.json',{schemaVersion:2,status:'observed',generatedAt:'2026-09-24T00:00:00Z',wpaStatus:'unavailable',wpaModel:null,laneModel:null,reliability:null,pooledEffects:null,itemDataMissing:[],itemDataProvisional:{},rankStatus:'unavailable',samplePolicy:'Convenience sample',uniqueMatches:2,sources:[{id:'riot-match-v5',name:'Riot',type:'riot_match_timelines',supportsWpaResearch:true}],patches:['16.19'],regions:['EUW1'],coverage:[{champion:'Sett',role:'TOP',opponent:'Garen',games:2}]}],
  ['champions/Sett.json',{schemaVersion:2,champion:'Sett',buckets:[{champion:'Sett',role:'TOP',opponent:'Garen',patch:'16.19',region:'EUW1',sourceId:'riot-match-v5',games:2,wins:1,eligible:{items:2},choices:[]}]}]
]);
class Store{
  data=new Map();etag=0;writes=[];failAt=null;conflict=false;
  async get(k,{type}={}){const v=this.data.get(k)?.body;return v===undefined?null:type==='json'?JSON.parse(v):v;}
  async getWithMetadata(k){const v=this.data.get(k);return v?{data:JSON.parse(v.body),etag:v.etag}:null;}
  async set(k,body,options={}){
    if(k===this.failAt||k.endsWith(this.failAt??'__none__'))throw Error('injected upload failure');
    const old=this.data.get(k);this.writes.push(k);
    if((options.onlyIfNew&&old)||(options.onlyIfMatch&&old?.etag!==options.onlyIfMatch)||(this.conflict&&k==='manifest.json'))return {modified:false};
    this.data.set(k,{body,etag:String(++this.etag)});return {modified:true};
  }
  async setJSON(k,v,o){return this.set(k,JSON.stringify(v),o);}
}
test('closed aggregate boundary rejects private fields, credentials, raw files and malformed counts before any upload',async()=>{
  for(const mutate of [
    f=>f.set('settistics.sqlite',{}),
    f=>f.get('index.json').playerId='private-player',
    f=>f.get('champions/Sett.json').buckets[0].puuid='secret',
    f=>f.get('champions/Sett.json').buckets[0].choices.push({kind:'items',id:'1234',label:'RGAPI-private-secret',games:1,wins:0,timeSum:0,timeCount:0}),
    f=>f.get('champions/Sett.json').buckets[0].wins=3,
    f=>f.get('champions/Sett.json').buckets[0].games=NaN,
    f=>f.get('index.json').coverage[0].games=8,
    f=>f.delete('champions/Sett.json'),
    f=>f.get('index.json').wpaModel={auc:1,playerId:'secret'}
  ]){const f=fixture(),s=new Store();mutate(f);await assert.rejects(publishRelease(s,f));assert.equal(s.writes.length,0);}
  assert.equal(validateRelease(fixture()).size,2);
});
test('route timing cohorts are accepted only when complete and well-formed',()=>{
  const withRoutes=routes=>{const f=fixture();f.get('champions/Sett.json').buckets[0].builds=[{id:'6631>3053>3071',items:['6631','3053','3071'],names:['A','B','C'],games:2,wins:1,timeSum:[26,44,56],components:{},boots:{},...(routes===undefined?{}:{routes})}];return f;};
  const route=(games,extra={})=>({bootsName:'Plated Steelcaps',componentName:'Phage',games,timeSum:[12*games,21*games,27*games],bootsTimeSum:14*games,componentTimeSum:7*games,...extra});
  assert.equal(validateRelease(withRoutes({'3047:1:3044':route(1),'-:0:-':route(1,{bootsName:'',componentName:''})})).size,2);
  assert.equal(validateRelease(withRoutes(undefined)).size,2);
  for(const routes of [{'3047:1:3044':route(1)},{'3047:5:3044':route(2)},{'3047:1':route(2)},{'3047:1:3044':route(2,{playerId:'x'})},{'3047:1:3044':route(2,{timeSum:[1,2,3,4]})}])
    assert.throws(()=>validateRelease(withRoutes(routes)),/Invalid aggregate/,JSON.stringify(routes));
});
test('publication is versioned, verified, and switches manifest last; old release remains readable',async()=>{
  const s=new Store(),one=await publishRelease(s,fixture()),two=await publishRelease(s,fixture());
  assert.equal(two.previous.version,one.current.version);assert.notEqual(one.current.version,two.current.version);
  assert.equal(s.writes.at(-1),'manifest.json');
  assert.equal(JSON.parse(await readReleaseFile(s,one.current.version,'index.json')).uniqueMatches,2);
});
test('partial upload, corrupted upload, and concurrent writer preserve last-good pointer',async()=>{
  for(const mode of ['upload','corrupt','conflict']){
    const s=new Store();await publishRelease(s,fixture());const old=await s.get('manifest.json');
    if(mode==='upload')s.failAt='champions/Sett.json';
    if(mode==='conflict')s.conflict=true;
    if(mode==='corrupt'){const get=s.get.bind(s);s.get=async(k,o)=>k.endsWith('/index.json')?'{}':get(k,o);}
    await assert.rejects(publishRelease(s,fixture()));assert.equal(await s.get('manifest.json'),old);
  }
});
test('API requires password even on direct invocation, rejects writes, traversal and corrupt releases',async()=>{
  const s=new Store(),p=await publishRelease(s,fixture());
  const request=(query='',pass='pw',method='GET')=>new Request('https://example.com/.netlify/functions/live-data'+query,{method,headers:pass?{authorization:'Basic '+Buffer.from('u:'+pass).toString('base64')}: {}});
  assert.equal((await serveData(request('',null),s,'pw')).status,401);
  assert.equal((await serveData(request(),s,undefined)).status,401);
  assert.equal((await serveData(request('','pw','POST'),s,'pw')).status,405);
  const res=await serveData(request(),s,'pw');assert.equal(res.status,200);assert.equal(res.headers.get('cache-control'),'private, no-store');
  for(const f of ['../settistics.sqlite','.env','champions/Sett.json/../../raw.json'])assert.equal((await serveData(request(`?version=${p.current.version}&file=${encodeURIComponent(f)}`),s,'pw')).status,503);
  const url=`?version=${p.current.version}&file=champions/Sett.json`;
  const checked=new Set(),delivered=await serveData(request(url),s,'pw',checked);assert.equal(delivered.status,200);
  assert.equal(delivered.headers.get('content-encoding'),'gzip');
  // Versioned files are immutable: the browser may cache them, and the stored gzip is served unchanged.
  assert.equal(delivered.headers.get('cache-control'),'private, max-age=31536000, immutable');
  const bytes=Buffer.from(await delivered.arrayBuffer());
  assert.deepEqual(JSON.parse(gunzipSync(bytes)),fixture().get('champions/Sett.json'));
  assert.ok(checked.has(`${p.current.version}/champions/Sett.json`));
  assert.deepEqual(Buffer.from(await (await serveData(request(url),s,'pw',checked)).arrayBuffer()),bytes);
  s.data.get(`releases/${p.current.version}/champions/Sett.json`).body='{}';
  // An instance that has not checked the file yet still refuses a corrupted one.
  assert.equal((await serveData(request(url),s,'pw',new Set())).status,503);
});
test('reader retains compatibility with an earlier uncompressed last-good release',async()=>{
  const s=new Store(),version='1790212345678-11111111-1111-1111-1111-111111111111',body=JSON.stringify(fixture().get('index.json'));
  await s.set(`releases/${version}/index.json`,body);
  await s.setJSON(`releases/${version}/manifest.json`,{schemaVersion:1,version,createdAt:'2026-09-24T00:00:00Z',files:{'index.json':hash(body)}});
  assert.equal(await readReleaseFile(s,version,'index.json'),body);
});
test('client pins a release; failed current falls back to previous, then bundled data',async()=>{
  const ctx={};vm.createContext(ctx);vm.runInContext(await readFile('data-client.js','utf8'),ctx);
  const s=new Store();const first=await publishRelease(s,fixture()),second=await publishRelease(s,fixture());
  let failed=false,reset=0;const calls=[];
  const fetch=async url=>{calls.push(url);if(url==='/api/live-data')return second;if(url==='data/index.json')return fixture().get('index.json');const u=new URL(url,'https://example.com');if(u.searchParams.get('version')===second.current.version||failed)throw Error('offline');return JSON.parse(await readReleaseFile(s,u.searchParams.get('version'),u.searchParams.get('file')));};
  const client=ctx.createDataClient(fetch,()=>reset++);
  assert.equal((await client.get('index.json')).dataVersion,first.current.version);
  assert.equal((await client.get('champions/Sett.json')).buckets.length,1);
  assert.equal(calls.filter(u=>u==='/api/live-data').length,1);
  failed=true;await assert.rejects(client.get('champions/Sett.json'));assert.equal(reset,1);
  assert.equal((await client.get('index.json')).dataVersion,undefined);
});
test('refresh cannot publish after training, export or build fails',()=>{
  for(let fail=0;fail<3;fail++){
    const calls=[];
    assert.throws(()=>refresh((exe,args)=>{calls.push(args[0]);if(calls.length===fail+1)throw Error('stage failed');},'python','xgb-cuda'));
    assert.equal(calls.includes('scripts/publish-data.mjs'),false);
  }
  const calls=[];refresh((exe,args)=>calls.push(args),'python','auto');
  assert.equal(calls.at(-1)[0],'scripts/publish-data.mjs');assert.deepEqual(calls[0],['pipeline/wpa.py','--backend','auto']);
  assert.equal(calls.some(c=>c.includes('deploy')),false);
});
