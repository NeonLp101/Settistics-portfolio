// Visitors download a small index plus only the champion they look at.
// data/public/stats.json can outgrow V8's maximum string length (~512 MiB), so a large export is read
// as bytes and every bucket is parsed on its own; shards are then built one champion at a time.
import {lstat,mkdir,open,readFile,readdir,realpath,rm,stat,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {basename,dirname,isAbsolute,relative,resolve,sep} from 'node:path';
import {validateRelease} from './aggregate-schema.mjs';

export const STREAM_ABOVE=64*2**20;
const WS=new Set([0x20,0x09,0x0a,0x0d]);

async function safeOutput(out){
  const target=resolve(out),site=resolve('public/data'),temp=resolve(tmpdir());
  const rel=relative(temp,target),fixture=!rel.startsWith('..')&&!isAbsolute(rel)&&rel.split(sep)[0].startsWith('build-data-');
  if(target!==site&&!fixture)throw new Error(`Refusing to replace an unexpected output directory: ${target}`);
  const parent=await realpath(dirname(target));
  if(target===site&&parent!==await realpath(resolve('public')))throw new Error('Public output parent changed');
  if(fixture&&basename(dirname(target)).startsWith('build-data-')===false)throw new Error('Refusing to replace the test fixture root');
  try{if((await lstat(target)).isSymbolicLink())throw new Error('Refusing a symlink output directory');}
  catch(error){if(error.code!=='ENOENT')throw error;}
  return target;
}

// Splits the top-level object into its metadata and the byte range of every bucket. Only structure is
// scanned here; each piece still goes through JSON.parse, so malformed input throws instead of being skipped.
export function splitStats(buf){
  let i=0;
  const bad=()=>{throw new SyntaxError(`Malformed statistics export at byte ${i}`);};
  const ws=()=>{while(WS.has(buf[i]))i++;};
  const string=()=>{if(buf[i]!==0x22)bad();for(i++;i<buf.length&&buf[i]!==0x22;i++)if(buf[i]===0x5c)i++;if(i>=buf.length)bad();i++;};
  const value=()=>{
    if(buf[i]===0x22)return string();
    if(buf[i]===0x7b||buf[i]===0x5b){
      let depth=0;
      do{const c=buf[i];if(c===0x22){string();continue;}if(c===0x7b||c===0x5b)depth++;else if(c===0x7d||c===0x5d)depth--;i++;}while(depth>0&&i<buf.length);
      if(depth)bad();return;
    }
    const start=i;while(i<buf.length&&!WS.has(buf[i])&&![0x2c,0x7d,0x5d].includes(buf[i]))i++;if(i===start)bad();
  };
  let ranges=null,from,to;
  ws();if(buf[i]!==0x7b)bad();i++;ws();
  if(buf[i]!==0x7d)for(;;){
    ws();const k=i;string();const key=JSON.parse(buf.toString('utf8',k,i));ws();if(buf[i]!==0x3a)bad();i++;ws();
    if(key==='buckets'&&ranges)bad();
    if(key==='buckets'&&buf[i]===0x5b){
      from=i++;ranges=[];ws();
      if(buf[i]!==0x5d)for(;;){ws();const s=i;value();ranges.push([s,i]);ws();if(buf[i]===0x2c){i++;continue;}if(buf[i]===0x5d)break;bad();}
      to=++i;
    }else value();
    ws();if(buf[i]===0x2c){i++;continue;}if(buf[i]===0x7d)break;bad();
  }
  i++;ws();if(i!==buf.length)bad();
  if(!ranges)throw new Error('Invalid Riot statistics export');
  return {meta:JSON.parse(buf.toString('utf8',0,from)+'[]'+buf.toString('utf8',to)),ranges};
}

async function readBase(path,streamAbove){
  let size;
  try{size=(await stat(path)).size;}
  catch(error){if(error.code!=='ENOENT')throw error;return {base:{schemaVersion:2,status:'empty',wpaStatus:'unavailable',uniqueMatches:0,sources:[],buckets:[]},count:0,streamed:false};}
  if(size<=streamAbove){const base=JSON.parse(await readFile(path,'utf8'));return {base,count:base.buckets?.length,at:i=>base.buckets[i],streamed:false};}
  const buf=await readLarge(path,size),{meta,ranges}=splitStats(buf);
  return {base:meta,count:ranges.length,at:i=>JSON.parse(buf.toString('utf8',ranges[i][0],ranges[i][1])),streamed:true};
}

// readFile refuses anything over 2 GiB; a Buffer itself can be far larger.
async function readLarge(path,size){
  const buf=Buffer.allocUnsafe(size),fh=await open(path,'r');
  try{for(let at=0;at<size;){const {bytesRead}=await fh.read(buf,at,Math.min(2**30,size-at),at);if(!bytesRead)throw new Error(`${path} shrank while reading`);at+=bytesRead;}}
  finally{await fh.close();}
  return buf;
}

export async function buildData({stats:statsPath='data/public/stats.json',imports:importDir='data/imports',out='public/data',streamAbove=STREAM_ABOVE,now=new Date()}={}){
  out=await safeOutput(out);
  const {base,count,at,streamed}=await readBase(statsPath,streamAbove);
  if(![1,2].includes(base.schemaVersion)||!Array.isArray(base.buckets))throw new Error('Invalid Riot statistics export');
  const sources=base.schemaVersion===2&&Array.isArray(base.sources)?[...base.sources]:[];
  const rawId=sources.find(s=>s.type==='riot_match_timelines')?.id||'riot-match-v5';
  if(!sources.some(s=>s.id===rawId))sources.push({id:rawId,name:'Riot Match-V5 timelines',type:'riot_match_timelines',generatedAt:base.generatedAt||null,supportsWpaResearch:true,note:'Locally collected match details and timelines; convenience sample.'});
  const imported=[];
  let imports=[];
  try{imports=(await readdir(importDir)).filter(name=>name.endsWith('.json')).sort();}
  catch(error){if(error.code!=='ENOENT')throw error;}
  const dimensions=b=>[b.champion,b.opponent,b.role,b.patch,b.region].join('\u0000');
  for(const name of imports){
    const value=JSON.parse(await readFile(`${importDir}/${name}`,'utf8'));
    if(value.importSchemaVersion!==1||!value.provider?.id||!value.provider?.name||value.rightsConfirmed!==true||!Array.isArray(value.buckets))throw new Error(`Invalid aggregate import: ${name}`);
    if(sources.some(s=>s.id===value.provider.id))throw new Error(`Duplicate source id: ${value.provider.id}`);
    const seen=new Set();
    for(const bucket of value.buckets){
      const key=dimensions(bucket);if(seen.has(key))throw new Error(`Duplicate bucket in ${name}: ${key}`);seen.add(key);
      imported.push({...bucket,sourceId:value.provider.id});
    }
    sources.push({id:value.provider.id,name:value.provider.name,type:'aggregate_import',generatedAt:value.obtainedAt,sourceUrl:value.provider.sourceUrl,supportsWpaResearch:false,note:value.provider.note||'Imported aggregate counts; underlying matches and timelines are unavailable.'});
  }
  // Raw buckets first, then imports, exactly as a single in-memory list would be ordered.
  const total=count+imported.length,bucket=j=>{if(j>=count)return imported[j-count];const b=at(j);return {...b,sourceId:b.sourceId||rawId};};
  const meta={schemaVersion:2,status:total?'observed':'empty',generatedAt:now.toISOString(),wpaStatus:base.wpaStatus||'unavailable',wpaModel:base.wpaModel||null,laneModel:base.laneModel||null,reliability:base.reliability||null,pooledEffects:base.pooledEffects||null,itemDataMissing:base.itemDataMissing||[],itemDataProvisional:base.itemDataProvisional||{},rankStatus:base.rankStatus||'unavailable',samplePolicy:base.samplePolicy||'Source-specific sampling; inspect provenance.',uniqueMatches:Number(base.uniqueMatches)||0,firstObjectives:base.firstObjectives||null,sources};
  const coverage=new Map(),byChampion=new Map(),patches=new Set(),regions=new Set();
  for(let j=0;j<total;j++){
    const b=bucket(j);
    if(!/^[A-Za-z0-9]+$/.test(b.champion))throw new Error(`Unexpected champion id: ${b.champion}`);
    const key=`${b.champion}|${b.role}|${b.opponent}`;coverage.set(key,(coverage.get(key)||0)+b.games);
    if(!byChampion.has(b.champion))byChampion.set(b.champion,[]);byChampion.get(b.champion).push(j);
    patches.add(b.patch);regions.add(b.region);
  }
  const index={...meta,patches:[...patches],regions:[...regions],
    coverage:[...coverage].map(([key,games])=>{const [champion,role,opponent]=key.split('|');return{champion,role,opponent,games};})};
  const shard=champion=>({schemaVersion:2,champion,buckets:byChampion.get(champion).map(bucket)});
  // validateRelease only uses has/get/iteration, so shards are materialised one at a time.
  validateRelease({has:name=>name==='index.json',get:name=>name==='index.json'?index:undefined,
    *[Symbol.iterator](){yield ['index.json',index];for(const champion of byChampion.keys())yield [`champions/${champion}.json`,shard(champion)];}});
  await rm(out,{recursive:true,force:true});
  await mkdir(`${out}/champions`,{recursive:true});
  for(const champion of byChampion.keys())await writeFile(`${out}/champions/${champion}.json`,JSON.stringify(shard(champion)));
  await writeFile(`${out}/index.json`,JSON.stringify(index));
  return {index,buckets:total,champions:byChampion.size,imports:imports.length,streamed};
}
