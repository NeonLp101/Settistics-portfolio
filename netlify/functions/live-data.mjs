import {getStore} from '@netlify/blobs';
import {STORE_NAME,validateManifest,readReleaseFile,wireBody} from '../../scripts/live-store.mjs';
import {validateFile} from '../../scripts/aggregate-schema.mjs';
import {passwordFrom} from '../edge-functions/password.js';
import {timingSafeEqual} from 'node:crypto';

export function authorized(request,password){
  const supplied=passwordFrom(request.headers.get('authorization'));
  if(!password||supplied===null)return false;
  const a=Buffer.from(supplied),b=Buffer.from(password);
  return a.length===b.length&&timingSafeEqual(a,b);
}
export async function serveData(request,store,password){
  const headers={'content-type':'application/json','cache-control':'private, no-store','x-content-type-options':'nosniff'};
  // Defense in depth also protects direct /.netlify/functions/ calls.
  if(!authorized(request,password))return new Response('{"error":"Password required"}',{status:401,headers});
  if(request.method!=='GET')return new Response(null,{status:405,headers});
  try{
    const url=new URL(request.url),version=url.searchParams.get('version'),file=url.searchParams.get('file');
    if(!version&&!file){
      const pointer=await store.get('manifest.json',{type:'json',consistency:'strong'});
      validateManifest(pointer?.current);if(pointer.previous)validateManifest(pointer.previous);
      return new Response(JSON.stringify({schemaVersion:1,current:pointer.current,previous:pointer.previous??null}),{headers});
    }
    const body=await readReleaseFile(store,version??'',file??'');
    validateFile(file,JSON.parse(body));
    return new Response(wireBody(body),{headers:{...headers,'content-encoding':'gzip'}});
  }catch{return new Response('{"error":"Live data unavailable; use last good export"}',{status:503,headers});}
}
export default request=>serveData(request,getStore({name:STORE_NAME,consistency:'strong'}),process.env.SITE_PASSWORD);
export const config={path:'/api/live-data'};
