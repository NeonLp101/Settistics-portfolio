import{mkdir,writeFile}from'node:fs/promises';import{join}from'node:path';
const MAP={br1:'americas',la1:'americas',la2:'americas',na1:'americas',eun1:'europe',euw1:'europe',tr1:'europe',ru:'europe',jp1:'asia',kr:'asia',oc1:'sea',ph2:'sea',sg2:'sea',th2:'sea',tw2:'sea',vn2:'sea'};
const key=process.env.RIOT_API_KEY,[gameName,tagLine,platformArg='euw1',countArg='10']=process.argv.slice(2),platform=platformArg.toLowerCase(),cluster=MAP[platform],count=Math.max(1,Math.min(Number(countArg)||10,100));
if(!key){console.error('Missing RIOT_API_KEY environment variable.');process.exit(1)}if(!gameName||!tagLine||!cluster){console.error('Usage: npm run collect -- "Game Name" TAG euw1 10');process.exit(1)}
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function get(url,retry=true){const res=await fetch(url,{headers:{'X-Riot-Token':key,Accept:'application/json'}});if(res.status===429&&retry){const seconds=Number(res.headers.get('retry-after'))||1;console.log(`Rate limited; waiting ${seconds}s`);await wait(seconds*1000);return get(url,false)}const body=await res.json().catch(()=>({}));if(!res.ok)throw new Error(`Riot ${res.status}: ${body?.status?.message||'request failed'}`);return body}
const account=await get(`https://${cluster}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/${encodeURIComponent(gameName)}/${encodeURIComponent(tagLine)}`);
const ids=await get(`https://${cluster}.api.riotgames.com/lol/match/v5/matches/by-puuid/${encodeURIComponent(account.puuid)}/ids?start=0&count=${count}&queue=420`);
await mkdir(join('data','raw'),{recursive:true});console.log(`Collecting ${ids.length} matches for ${account.gameName} #${account.tagLine}`);
for(const[idIndex,id]of ids.entries()){const base=`https://${cluster}.api.riotgames.com/lol/match/v5/matches/${encodeURIComponent(id)}`;const match=await get(base),timeline=await get(`${base}/timeline`);await writeFile(join('data','raw',`${id}.json`),JSON.stringify({match,timeline}));console.log(`${idIndex+1}/${ids.length} ${id}`)}
console.log('Done. Raw bundles are in data/raw/.');
