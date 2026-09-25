import {timingSafeEqual} from 'node:crypto';
const sameToken=(a,b)=>{const x=Buffer.from(String(a||'')),y=Buffer.from(String(b||''));return x.length===y.length&&timingSafeEqual(x,y);};
const PLATFORM_TO_CLUSTER={br1:'americas',la1:'americas',la2:'americas',na1:'americas',eun1:'europe',euw1:'europe',tr1:'europe',ru:'europe',jp1:'asia',kr:'asia',oc1:'sea',ph2:'sea',sg2:'sea',th2:'sea',tw2:'sea',vn2:'sea'};
const JSON_HEADERS={'content-type':'application/json; charset=utf-8','cache-control':'no-store'};
const reply=(statusCode,body)=>({statusCode,headers:JSON_HEADERS,body:JSON.stringify(body)});
const clean=(value,max=100)=>String(value||'').trim().slice(0,max);
let healthCache={key:'',platform:'',expires:0,value:null};
let staticDataCache={expires:0,value:null};
let retryAt=0;

function routeName(path=''){
  const parts=path.split('/').filter(Boolean);
  return parts[parts.length-1]||'health';
}
function routing(query){
  const platform=clean(query.platform||'euw1',5).toLowerCase();
  const cluster=PLATFORM_TO_CLUSTER[platform];
  if(!cluster)throw Object.assign(new Error('Unsupported platform routing value.'),{status:400});
  return{platform,cluster};
}
async function riotFetch(url,key,retry=true){
  if(Date.now()<retryAt)throw Object.assign(new Error('Riot rate limit: retry after the indicated delay.'),{status:429,upstreamStatus:429,retryAfter:Math.ceil((retryAt-Date.now())/1000)});
  const response=await fetch(url,{headers:{'X-Riot-Token':key,Accept:'application/json'},signal:AbortSignal.timeout(12000)});
  if(response.status===429){
    const seconds=Math.max(1,Number(response.headers.get('retry-after'))||120);
    retryAt=Date.now()+seconds*1000;
    throw Object.assign(new Error('Riot rate limit: retry after the indicated delay.'),{status:429,upstreamStatus:429,retryAfter:seconds});
  }
  let body;try{body=await response.json()}catch{body={}}
  if(!response.ok){const message=response.status===401||response.status===403?'Riot rejected the API key. Development keys expire every 24 hours.':body?.status?.message||`Riot API returned ${response.status}.`;throw Object.assign(new Error(message),{status:response.status===404?404:502,upstreamStatus:response.status})}
  return body;
}
async function checkConnection(key,platform){
  const now=Date.now();
  if(healthCache.value&&healthCache.key===key&&healthCache.platform===platform&&healthCache.expires>now)return healthCache.value;
  let value;
  try{
    await riotFetch(`https://${platform}.api.riotgames.com/lol/status/v4/platform-data`,key,false);
    value={ok:true,configured:true,connected:true,service:'settistics-riot',apiVersion:2,checkedAt:new Date().toISOString()};
  }catch(error){
    value={ok:true,configured:true,connected:false,service:'settistics-riot',apiVersion:2,upstreamStatus:error.upstreamStatus||null,message:error.message,checkedAt:new Date().toISOString()};
  }
  healthCache={key,platform,expires:now+60000,value};
  return value;
}
async function getStaticData(){
  const now=Date.now();
  if(staticDataCache.value&&staticDataCache.expires>now)return staticDataCache.value;
  const versionsResponse=await fetch('https://ddragon.leagueoflegends.com/api/versions.json');
  if(!versionsResponse.ok)throw Object.assign(new Error('Riot Data Dragon versions are unavailable.'),{status:502});
  const versions=await versionsResponse.json(),version=versions[0];
  const championsResponse=await fetch(`https://ddragon.leagueoflegends.com/cdn/${encodeURIComponent(version)}/data/en_US/champion.json`);
  if(!championsResponse.ok)throw Object.assign(new Error('Riot champion roster is unavailable.'),{status:502});
  const payload=await championsResponse.json();
  const champions=Object.values(payload.data).map(champion=>({id:champion.id,key:champion.key,name:champion.name,title:champion.title,tags:champion.tags,partype:champion.partype,attackRange:champion.stats?.attackrange||0})).sort((a,b)=>a.name.localeCompare(b.name));
  const value={version,champions,source:'Riot Data Dragon',updatedAt:new Date().toISOString()};
  staticDataCache={expires:now+21600000,value};
  return value;
}
export const handler=async event=>{
  if(event.httpMethod==='OPTIONS')return{statusCode:204,headers:JSON_HEADERS,body:''};
  if(event.httpMethod!=='GET')return reply(405,{message:'Method not allowed.'});
  const key=process.env.RIOT_API_KEY;
  const route=routeName(event.path);
  const query=event.queryStringParameters||{};
  if(route==='health'){
    if(!key)return reply(200,{ok:true,configured:false,connected:false,service:'settistics-riot',apiVersion:2});
    try{const{platform}=routing(query);return reply(200,await checkConnection(key,platform))}catch(error){return reply(error.status||500,{message:error.message})}
  }
  if(route==='static-data'){
    try{return reply(200,await getStaticData())}catch(error){return reply(error.status||500,{message:error.message})}
  }
  if(!key)return reply(503,{message:'RIOT_API_KEY is not configured.',setupRequired:true});
  // Every route below spends the Riot key, so none of them is open to the public.
  {
    const supplied=event.headers?.['x-admin-token']||event.headers?.['X-Admin-Token'];
    if(!process.env.SETTISTICS_ADMIN_TOKEN||!sameToken(supplied,process.env.SETTISTICS_ADMIN_TOKEN))return reply(403,{message:'This ingestion route is restricted.'});
  }
  try{
    const{cluster}=routing(query);
    if(route==='account'){
      const gameName=clean(query.gameName,40),tagLine=clean(query.tagLine,10);
      if(!gameName||!tagLine)return reply(400,{message:'gameName and tagLine are required.'});
      const url=`https://${cluster}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/${encodeURIComponent(gameName)}/${encodeURIComponent(tagLine)}`;
      const account=await riotFetch(url,key);return reply(200,{puuid:account.puuid,gameName:account.gameName,tagLine:account.tagLine});
    }
    if(route==='recent'){
      const puuid=clean(query.puuid,120),count=Math.max(1,Math.min(Number(query.count)||20,100)),start=Math.max(0,Number(query.start)||0),queue=Number(query.queue)||420;
      if(!puuid)return reply(400,{message:'puuid is required.'});
      const url=`https://${cluster}.api.riotgames.com/lol/match/v5/matches/by-puuid/${encodeURIComponent(puuid)}/ids?start=${start}&count=${count}&queue=${queue}`;
      return reply(200,{matchIds:await riotFetch(url,key),start,count,queue});
    }
    if(route==='match'||route==='timeline'||route==='bundle'){
      const matchId=clean(query.matchId,40);
      if(!/^[A-Z0-9]+_\d+$/i.test(matchId))return reply(400,{message:'A valid matchId is required.'});
      const base=`https://${cluster}.api.riotgames.com/lol/match/v5/matches/${encodeURIComponent(matchId)}`;
      if(route==='match')return reply(200,await riotFetch(base,key));
      if(route==='timeline')return reply(200,await riotFetch(`${base}/timeline`,key));
      const[match,timeline]=await Promise.all([riotFetch(base,key),riotFetch(`${base}/timeline`,key)]);return reply(200,{match,timeline});
    }
    return reply(404,{message:'Unknown Riot API route.'});
  }catch(error){const result=reply(error.status||500,{message:error.message,upstreamStatus:error.upstreamStatus||null,retryAfter:error.retryAfter||null});if(error.retryAfter)result.headers={...result.headers,'retry-after':String(error.retryAfter)};return result}
};
