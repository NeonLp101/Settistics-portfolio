import test from 'node:test';
import assert from 'node:assert/strict';
const event=(route,query={},headers={})=>({httpMethod:'GET',path:'/api/riot/'+route,queryStringParameters:query,headers});
const fresh=()=>import('../netlify/functions/riot.mjs?test='+Math.random());
test('missing key never reports verified',async()=>{
  const old=process.env.RIOT_API_KEY;delete process.env.RIOT_API_KEY;
  try{const{handler}=await fresh();const r=await handler(event('health'));assert.equal(JSON.parse(r.body).connected,false);assert.equal(JSON.parse(r.body).configured,false);}
  finally{if(old===undefined)delete process.env.RIOT_API_KEY;else process.env.RIOT_API_KEY=old;}
});
test('configured but rejected key is not verified',async()=>{
  const old=process.env.RIOT_API_KEY,fetch=globalThis.fetch;process.env.RIOT_API_KEY='RGAPI-test-only';
  globalThis.fetch=async()=>new Response('{}',{status:403});
  try{const{handler}=await fresh();const r=JSON.parse((await handler(event('health'))).body);assert.equal(r.configured,true);assert.equal(r.connected,false);assert.equal(r.upstreamStatus,403);assert.ok(!JSON.stringify(r).includes('RGAPI-test-only'));}
  finally{globalThis.fetch=fetch;if(old===undefined)delete process.env.RIOT_API_KEY;else process.env.RIOT_API_KEY=old;}
});
test('server returns full Retry-After without early retry',async()=>{
  const old=process.env.RIOT_API_KEY,oldToken=process.env.SETTISTICS_ADMIN_TOKEN,fetch=globalThis.fetch;process.env.RIOT_API_KEY='RGAPI-test-only';process.env.SETTISTICS_ADMIN_TOKEN='admin-test';const admin={'x-admin-token':'admin-test'};let calls=0;
  globalThis.fetch=async()=>{calls++;return new Response('{}',{status:429,headers:{'retry-after':'90'}});};
  try{const{handler}=await fresh();const r=await handler(event('account',{gameName:'test',tagLine:'EUW'},admin));assert.equal(r.statusCode,429);assert.equal(r.headers['retry-after'],'90');assert.equal(calls,1);await handler(event('recent',{puuid:'x'},admin));assert.equal(calls,1);}
  finally{globalThis.fetch=fetch;if(old===undefined)delete process.env.RIOT_API_KEY;else process.env.RIOT_API_KEY=old;;if(oldToken===undefined)delete process.env.SETTISTICS_ADMIN_TOKEN;else process.env.SETTISTICS_ADMIN_TOKEN=oldToken;}
});
test('raw ingestion endpoint requires private admin token',async()=>{
  const old=process.env.RIOT_API_KEY,admin=process.env.SETTISTICS_ADMIN_TOKEN;process.env.RIOT_API_KEY='RGAPI-test-only';delete process.env.SETTISTICS_ADMIN_TOKEN;
  try{const{handler}=await fresh();assert.equal((await handler(event('bundle',{matchId:'EUW1_123'}))).statusCode,403);}
  finally{if(old===undefined)delete process.env.RIOT_API_KEY;else process.env.RIOT_API_KEY=old;if(admin!==undefined)process.env.SETTISTICS_ADMIN_TOKEN=admin;}
});

test('routes that spend the Riot key are closed to the public',async()=>{
  const old=process.env.RIOT_API_KEY,oldToken=process.env.SETTISTICS_ADMIN_TOKEN,fetch=globalThis.fetch;let calls=0;
  process.env.RIOT_API_KEY='RGAPI-test-only';process.env.SETTISTICS_ADMIN_TOKEN='admin-test';globalThis.fetch=async()=>{calls++;return new Response('{}');};
  try{const{handler}=await fresh();
    for(const[route,query]of[['account',{gameName:'a',tagLine:'b'}],['recent',{puuid:'x'}],['match',{matchId:'EUW1_1'}],['timeline',{matchId:'EUW1_1'}],['bundle',{matchId:'EUW1_1'}]]){
      assert.equal((await handler(event(route,query))).statusCode,403,route);
      assert.equal((await handler(event(route,query,{'x-admin-token':'wrong'}))).statusCode,403,route);
    }
    assert.equal(calls,0);
    const r=await handler(event('health'));assert.equal(r.headers['access-control-allow-origin'],undefined);
  }finally{globalThis.fetch=fetch;if(old===undefined)delete process.env.RIOT_API_KEY;else process.env.RIOT_API_KEY=old;if(oldToken===undefined)delete process.env.SETTISTICS_ADMIN_TOKEN;else process.env.SETTISTICS_ADMIN_TOKEN=oldToken;}
});
