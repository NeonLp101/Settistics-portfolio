import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
test('local server serves only public files and safe API states',async()=>{
 const env={...process.env,PORT:'18988',RIOT_API_KEY:''};
 const server=spawn(process.execPath,['scripts/serve.mjs'],{env,stdio:['ignore','pipe','pipe']});
 try{
  await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('Server start timeout')),5000);server.stdout.once('data',()=>{clearTimeout(timer);resolve();});server.once('error',reject);});
  const root='http://127.0.0.1:18988';
  assert.equal((await fetch(root+'/')).status,200);
  for(const p of ['/.env','/data/settistics.sqlite','/pipeline/engine.py','/data/research/comparison.json','/../README.md'])assert.equal((await fetch(root+p)).status,404);
  const index=await (await fetch(root+'/data/index.json')).json();assert.equal(index.schemaVersion,2);assert.ok(Array.isArray(index.coverage));
  const preview=await fetch(root+'/data/recommendations.json');
  if(preview.status===200){const recommendations=await preview.json();assert.equal(recommendations.schemaVersion,1);assert.ok(Array.isArray(recommendations.entries));}
  else assert.equal(preview.status,404); // Fresh clones have no private model export.
  assert.equal((await fetch(root+'/data/champions/..%2F..%2F.env')).status,404);
  assert.ok(index.coverage.every(r=>r.champion&&r.role!==undefined&&typeof r.games==="number"));
 }finally{server.kill();}
});
