// Closed schema for the optional model research preview (pipeline/recommendation_export.py).
// Predictions only: it cannot carry a claim, a validated pick, identifiers or per-game rows.
import {fail,num,count,text,pattern,bool,literal,arr,obj,dict,champ,role,patch,region,date} from './aggregate-schema.mjs';

export const MAX_RECOMMENDATION_BYTES=600000;
const nullable=f=>(v,p)=>{if(v!==null)f(v,p);};
const range=(lo,hi)=>(v,p)=>{num(v,p);if(v<lo||v>hi)fail(p);};
const sha=(v,p)=>{if(typeof v!=='string'||!/^[a-f0-9]{64}$/.test(v))fail(p);};
const item=pattern(/^\d{1,6}$/);
export const PREDICTION_FIELDS=['finalWin','goldLeadChange5','takedowns5','deaths5','championDamage5','timeAlive5s'];
const bounds=[range(0,1),range(-20000,20000),range(0,50),range(0,50),range(0,200000),range(0,300)];
const arm=(v,p)=>{if(!Array.isArray(v)||v.length!==bounds.length)fail(p);bounds.forEach((f,i)=>f(v[i],`${p}[${i}]`));};
const fields=(v,p)=>{if(!Array.isArray(v)||v.join()!==PREDICTION_FIELDS.join())fail(p);};
const entry=obj({champion:champ,role,stage:pattern(/^(slot1|slot2|slot3|boots)$/),scope:pattern(/^(first_distinguishing_component|boots_upgrade_purchase)$/),
  patch,baselineRoute:item,alternativeRoute:item,support:obj({contexts:count,routeA:count,routeB:count,matches:count}),
  status:literal('research_preview'),supported:bool,predicted:nullable(obj({routeA:arm,routeB:arm})),modelLean:nullable(pattern(/^(routeA|routeB|none)$/))});
// A verdict of supported_improvement is deliberately not in this list: such a result needs its own release path.
const verdict=pattern(/^(insufficient_evidence|insufficient_overlap|no_supported_improvement|not_fitted)$/);
const model=obj({version:pattern(/^recommender-experimental-v\d+$/),artifactSha256:sha,trainedAt:date,fitRows:count,fitMatches:count,
  evaluationRows:count,evaluationMatches:count,policyVerdicts:dict(/^(base|enriched)$/,verdict),headlineClaimsAllowed:literal(false),minArmTrainRows:count,preferenceMargin:range(0,1)});
const outcomes=obj(Object.fromEntries(PREDICTION_FIELDS.map(k=>[k,text])));
const doc=obj({schemaVersion:literal(1),kind:literal('item_model_research_preview'),generatedAt:date,status:literal('research_preview'),
  claimStatus:literal('no_confirmed_advantage'),recommendation:literal('observed_baseline_route_a'),routeLevelClaim:literal(false),
  pooling:literal('all_matchups_regions_players'),sourcePatches:arr(patch,20),catalogVersions:dict(/^\d{1,2}\.\d{1,2}$/,pattern(/^\d{1,2}\.\d{1,2}\.\d{1,2}$/)),
  regions:arr(region,50),model,scopes:obj({first_distinguishing_component:text,boots_upgrade_purchase:text}),predictionFields:fields,outcomes,caveats:arr(text,20),entries:arr(entry,5000)});

export function validateRecommendations(value){
  doc(value,'recommendations');
  const seen=new Set();
  value.entries.forEach((e,i)=>{
    const p=`recommendations.entries[${i}]`,key=[e.champion,e.role,e.stage,e.patch].join('|');
    if(seen.has(key))fail(p+' duplicate');seen.add(key);
    if(!value.sourcePatches.includes(e.patch)||e.baselineRoute===e.alternativeRoute||(e.stage==='boots')!==(e.scope==='boots_upgrade_purchase'))fail(p);
    const s=e.support;if(s.routeA+s.routeB!==s.contexts||s.matches>s.contexts)fail(p+'.support');
    if(e.supported!==(e.predicted!==null)||e.supported!==(e.modelLean!==null))fail(p+'.supported');
    if(e.supported&&Math.min(s.routeA,s.routeB)<value.model.minArmTrainRows)fail(p+'.supported');
    if(e.predicted){const a=e.predicted.routeA[0],b=e.predicted.routeB[0];if(e.modelLean!==(a===b?'none':a>b?'routeA':'routeB'))fail(p+'.modelLean');}
  });
  return value;
}

// Optional in the build: a missing file is skipped; a present one must be valid or the build fails.
export async function copyRecommendations(from,to){
  const {readFile,writeFile}=await import('node:fs/promises');
  let raw;
  try{raw=await readFile(from,'utf8');}catch(error){if(error.code==='ENOENT')return null;throw error;}
  if(Buffer.byteLength(raw)>MAX_RECOMMENDATION_BYTES)throw new Error(`Model research preview is larger than ${MAX_RECOMMENDATION_BYTES} bytes`);
  const value=validateRecommendations(JSON.parse(raw));
  await writeFile(to,JSON.stringify(value));
  return value;
}
