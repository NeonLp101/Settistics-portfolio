// Deliberately closed schema. New exporter fields require an explicit review here.
const fail=path=>{throw new Error(`Invalid aggregate at ${path}`);};
const num=(v,p)=>{if(typeof v!=='number'||!Number.isFinite(v)||Math.abs(v)>1e15)fail(p);};
const count=(v,p)=>{num(v,p);if(v<0||!Number.isSafeInteger(v))fail(p);};
const text=(v,p)=>{if(typeof v!=='string'||v.length>1000||/RGAPI-|nfp_|gh[pousr]_|github_pat_|-----BEGIN|(?:password|token|puuid)\s*[:=]|Bearer\s|Basic\s+[A-Za-z0-9+/=]{12}|[A-Za-z0-9_-]{60,}|[\u0000-\u0008]/i.test(v))fail(p);};
const pattern=re=>(v,p)=>{text(v,p);if(!re.test(v))fail(p);};
const bool=(v,p)=>{if(typeof v!=='boolean')fail(p);};
const literal=x=>(v,p)=>{if(v!==x)fail(p);};
const nullable=f=>(v,p)=>{if(v!==null)f(v,p);};
const arr=(f,max=1000000)=>(v,p)=>{if(!Array.isArray(v)||v.length>max)fail(p);v.forEach((x,i)=>f(x,`${p}[${i}]`));};
const obj=(fields,required=Object.keys(fields))=>(v,p)=>{
  if(!v||typeof v!=='object'||Array.isArray(v))fail(p);
  for(const k of required)if(!Object.hasOwn(v,k))fail(p+'.'+k);
  for(const [k,x] of Object.entries(v)){if(!Object.hasOwn(fields,k))fail(p+'.'+k);fields[k](x,p+'.'+k);}
};
const dict=(re,f)=>(v,p)=>{if(!v||typeof v!=='object'||Array.isArray(v))fail(p);for(const [k,x] of Object.entries(v)){if(!re.test(k))fail(p);f(x,p+'.'+k);}};
const champ=pattern(/^[A-Za-z][A-Za-z0-9]{0,29}$/), role=pattern(/^(TOP|JUNGLE|MIDDLE|BOTTOM|UTILITY)$/);
const patch=pattern(/^\d{1,2}\.\d{1,2}$/), region=pattern(/^(EUW1|EUN1|KR|NA1|BR1|JP1|LA1|LA2|OC1|TR1|RU|PH2|SG2|TH2|TW2|VN2|ME1)$/);
const date=(v,p)=>{text(v,p);if(!/^\d{4}-\d\d-\d\dT/.test(v)||!Number.isFinite(Date.parse(v)))fail(p);};
const id=pattern(/^[0-9QWERx+,|:>a-z-]{1,250}$/);
const kinds=/^(slot[1-5]|boots|bootsTiming|component|core|packages|items|runes|spells|keystone|skills|skillStart|skillMax|build)$/;
const eligible=dict(kinds,count);
const totals={games:count,wins:count,timeSum:num,timeCount:count,residN:count,residSum:num,residSq:num,laneN:count,laneSum:num,laneSq:num,laneDelta:num,laneUp:count,curveN:count,preSum:num,curveSum:arr(num,10),curveSq:arr(num,10)};
const choice=obj({kind:pattern(kinds),id,label:text,...totals},['kind','id','label','games','wins','timeSum','timeCount']);
const component=obj({name:text,games:count,wins:count,timeSum:num});
// One timing cohort per core: boots item or "-", boots position (0-3), first component or "-". Optional for older exports.
const route=obj({bootsName:text,componentName:text,games:count,timeSum:arr(num,3),bootsTimeSum:num,componentTimeSum:num});
const build=obj({id,items:arr(id,6),names:arr(text,6),games:count,wins:count,timeSum:arr(num,6),components:dict(/^\d{1,6}$/,component),boots:dict(/^\d{1,6}:[0-6]$/,component),routes:dict(/^(\d{1,6}|-):[0-3]:(\d{1,6}|-)$/,route)},['id','items','names','games','wins','timeSum','components','boots']);
const path=obj({id,firstItem:id,bootsBefore:count,games:count,wins:count,eligible,choices:arr(choice)});
const FIRSTS=['firstBloodKill','firstBloodAssist','firstTowerKill','firstTowerAssist'];
const firstCounts=Object.fromEntries(FIRSTS.map(k=>[k,count]));
const bucket=obj({champion:champ,opponent:champ,role,patch,region,sourceId:pattern(/^[a-z0-9-]{1,50}$/),games:count,wins:count,...firstCounts,eligible,choices:arr(choice),builds:arr(build),paths:arr(path)},['champion','opponent','role','patch','region','sourceId','games','wins','eligible','choices']);
const check=obj({flagged:num,groups:count,neededGames:nullable(num),pass:bool,placebo:num,splitHalfR:num});
const slots=/^(boots|slot[1-5]|keystone|packages|spells)$/;
const reliability=obj({wpa:dict(slots,check),lane:dict(slots,check),wpaPooled:dict(slots,check),lanePooled:dict(slots,check),curve:dict(slots,dict(/^(10|[1-9])$/,check)),curvePooled:dict(slots,dict(/^(10|[1-9])$/,check))},[]);
const triple=curve=>(v,p)=>{if(!Array.isArray(v)||v.length!==3)fail(p);count(v[0],p);for(const x of v.slice(1))(curve?arr(num,10):num)(x,p);};
const pool=curve=>dict(/^(TOP|JUNGLE|MIDDLE|BOTTOM|UTILITY)$/,dict(slots,dict(/^[0-9x+,|:a-z-]{1,250}$/,triple(curve))));
const pooled=obj({wpa:pool(false),lane:pool(false),curve:pool(true)},[]);
const wpa=obj({generatedAt:date,method:text,games:count,snapshots:count,decisions:count,slotOffsetsPp:dict(slots,num),auc:num,brier:num,calibrationErrorPp:num});
const lane=obj({generatedAt:date,windows:count,cleanShare:dict(slots,num),maeGold:num,baselineMaeGold:num,stateOnlyMaeGold:num,r2:num,stateOnlyR2:num,matchupR2:num,features:arr(pattern(/^[a-z_]{1,30}$/),100)});
const source=obj({id:pattern(/^[a-z0-9-]{1,50}$/),name:text,type:pattern(/^(riot_match_timelines|aggregate_import)$/),generatedAt:nullable(date),supportsWpaResearch:bool,note:text,sourceUrl:pattern(/^https:\/\/[^\s@]+$/)},['id','name','type','supportsWpaResearch']);
const coverage=obj({champion:champ,opponent:champ,role,games:count});
const firstObjectives=dict(/^(TOP|JUNGLE|MIDDLE|BOTTOM|UTILITY)$/,obj({games:count,...firstCounts}));
const indexFields={schemaVersion:literal(2),status:pattern(/^(observed|empty)$/),generatedAt:date,wpaStatus:pattern(/^(prototype|unavailable)$/),wpaModel:nullable(wpa),laneModel:nullable(lane),reliability:nullable(reliability),pooledEffects:nullable(pooled),itemDataMissing:arr(patch),itemDataProvisional:dict(/^\d{1,2}\.\d{1,2}$/,pattern(/^\d{1,2}\.\d{1,2}\.\d{1,2}$/)),rankStatus:text,samplePolicy:text,uniqueMatches:count,firstObjectives:nullable(firstObjectives),sources:arr(source,100),patches:arr(patch,100),regions:arr(region,50),coverage:arr(coverage)};
// firstObjectives is optional: exports from before it existed stay valid.
const index=obj(indexFields,Object.keys(indexFields).filter(k=>k!=='firstObjectives'));

function invariants(v,p='root') {
  if(!v||typeof v!=='object')return;
  if(Object.hasOwn(v,'wins')&&v.wins>v.games)fail(p+'.wins');
  if(Object.hasOwn(v,'games'))for(const k of ['residN','laneN','curveN','timeCount',...FIRSTS])if(v[k]>v.games)fail(p+'.'+k);
  if(v.pass===true&&(v.groups<20||v.splitHalfR<.4))fail(p+'.pass');
  // Every game with a core belongs to exactly one timing route.
  if(v.routes&&typeof v.routes==='object'&&Object.values(v.routes).reduce((n,r)=>n+(r?.games||0),0)!==v.games)fail(p+'.routes');
  if(v.auc!==undefined&&(v.auc<0||v.auc>1||v.brier<0||v.brier>1))fail(p+'.metrics');
  for(const [k,x] of Object.entries(v))invariants(x,p+'.'+k);
}

export const filePattern=/^(index\.json|champions\/[A-Za-z][A-Za-z0-9]{0,29}\.json)$/;
export function validateFile(name,value){
  if(!filePattern.test(name))fail('filename');
  if(name==='index.json')index(value,name);
  else {obj({schemaVersion:literal(2),champion:champ,buckets:arr(bucket)})(value,name);if(name!==`champions/${value.champion}.json`||value.buckets.some(b=>b.champion!==value.champion))fail(name);}
  invariants(value,name);
  return value;
}

export function validateRelease(files){
  if(!files.has('index.json'))fail('missing index');
  for(const [name,value] of files)validateFile(name,value);
  const meta=files.get('index.json'), sums=new Map(), sources=new Set(meta.sources.map(s=>s.id)),seen=new Set();
  for(const [name,v] of files)if(name!=='index.json')for(const b of v.buckets){
    if(!sources.has(b.sourceId)||!meta.patches.includes(b.patch)||!meta.regions.includes(b.region))fail('provenance');
    const key=[b.champion,b.role,b.opponent].join('|');sums.set(key,(sums.get(key)||0)+b.games);
    const unique=[key,b.patch,b.region,b.sourceId].join('|');if(seen.has(unique))fail('duplicate bucket');seen.add(unique);
  }
  for(const r of meta.coverage){const key=[r.champion,r.role,r.opponent].join('|');if(sums.get(key)!==r.games)fail('coverage');sums.delete(key);}
  if(sums.size)fail('missing coverage');
  return files;
}
