import {createHash, randomUUID} from 'node:crypto';
import {gzipSync,gunzipSync} from 'node:zlib';
import {validateRelease, filePattern} from './aggregate-schema.mjs';
export const STORE_NAME='settistics-aggregate-v1';
export const hash=text=>createHash('sha256').update(text).digest('hex');
export function wireBody(body){const compressed=gzipSync(body);if(compressed.byteLength>4_000_000)throw new Error('Aggregate exceeds safe response size; split the schema before publishing');return compressed;}
export const versionPattern=/^[0-9]{13}-[a-f0-9-]{36}$/;
export function validateManifest(m){
  if(!m||m.schemaVersion!==1||!versionPattern.test(m.version)||!m.files||typeof m.files!=='object'||Array.isArray(m.files)||!m.files['index.json'])throw new Error('Invalid release manifest');
  if(Object.keys(m).some(k=>!['schemaVersion','version','createdAt','files','encoding'].includes(k)))throw new Error('Unknown manifest field');
  if(m.encoding!==undefined&&m.encoding!=='gzip')throw new Error('Unknown release encoding');
  if(!Number.isFinite(Date.parse(m.createdAt)))throw new Error('Invalid release date');
  for(const [name,digest] of Object.entries(m.files))if(!filePattern.test(name)||!/^\w{64}$/.test(digest)||!/^[a-f0-9]+$/.test(digest))throw new Error('Invalid manifest file');
  return m;
}

export async function publishRelease(store,files){
  validateRelease(files); // Complete validation before the first remote write.
  const bodies=new Map([...files].map(([name,value])=>[name,JSON.stringify(value)]));
  const encoded=new Map([...bodies].map(([name,body])=>[name,wireBody(body)])); // Check delivery size before any upload.
  const old=await store.getWithMetadata('manifest.json',{type:'json',consistency:'strong'});
  const version=`${Date.now()}-${randomUUID()}`,manifest={schemaVersion:1,version,createdAt:new Date().toISOString(),encoding:'gzip',files:Object.fromEntries([...bodies].map(([name,body])=>[name,hash(body)]))};
  const pending=[...encoded];let next=0;
  // Independent immutable objects can upload concurrently. Wait for every worker,
  // including failures, before considering a manifest switch.
  const uploaded=await Promise.allSettled(Array.from({length:4},async()=>{
    while(next<pending.length){
      const [name,body]=pending[next++],key=`releases/${version}/${name}`;
      const bytes=body.buffer.slice(body.byteOffset,body.byteOffset+body.byteLength);
      if(!(await store.set(key,bytes,{onlyIfNew:true})).modified)throw new Error('Immutable release collision');
      const saved=await store.get(key,{type:'arrayBuffer',consistency:'strong'});
      if(!saved||hash(gunzipSync(Buffer.from(saved)))!==manifest.files[name])throw new Error('Upload verification failed');
    }
  }));
  const failed=uploaded.find(r=>r.status==='rejected');if(failed)throw failed.reason;
  const releaseKey=`releases/${version}/manifest.json`;
  if(!(await store.setJSON(releaseKey,manifest,{onlyIfNew:true})).modified)throw new Error('Manifest collision');
  if(JSON.stringify(await store.get(releaseKey,{type:'json',consistency:'strong'}))!==JSON.stringify(manifest))throw new Error('Manifest verification failed');
  const pointer={schemaVersion:1,current:manifest,previous:old?.data?.current??null};
  if(old?.data?.current)validateManifest(old.data.current);
  if(old&&!old.etag)throw new Error('Missing manifest ETag');
  const result=await store.setJSON('manifest.json',pointer,old?{onlyIfMatch:old.etag}:{onlyIfNew:true});
  if(!result.modified)throw new Error('Another publication won; live manifest unchanged by this run');
  return pointer;
}

// Returns the checked body and, for gzip releases, the stored bytes so they can be served without recompressing.
// trusted: this instance already checked the same immutable file, so gzip bytes are returned unchecked.
export async function readReleaseEntry(store,version,name,trusted=false){
  if(!versionPattern.test(version)||!filePattern.test(name))throw new Error('Invalid data path');
  const manifest=validateManifest(await store.get(`releases/${version}/manifest.json`,{type:'json'}));
  if(manifest.version!==version||!manifest.files[name])throw new Error('File outside release');
  const key=`releases/${version}/${name}`,gzip=manifest.encoding==='gzip';
  const stored=await store.get(key,{type:gzip?'arrayBuffer':'text'});
  if(trusted&&gzip&&stored!==null)return {body:null,gzipped:Buffer.from(stored)};
  const body=stored===null?null:gzip?gunzipSync(Buffer.from(stored)).toString('utf8'):stored;
  if(body===null||hash(body)!==manifest.files[name])throw new Error('Release integrity check failed');
  return {body,gzipped:gzip?Buffer.from(stored):null};
}
export async function readReleaseFile(store,version,name){return (await readReleaseEntry(store,version,name)).body;}
