import {readFile,readdir,lstat} from 'node:fs/promises';
import {resolve,join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {getStore} from '@netlify/blobs';
import {publishRelease,STORE_NAME} from './live-store.mjs';
import {validateRelease} from './aggregate-schema.mjs';

export async function readAggregateDirectory(directory){
  const files=new Map();
  // No recursive upload, arbitrary paths, or symlinks. Only these JSON aggregates.
  async function read(name){
    const path=join(directory,name);if(!(await lstat(path)).isFile()||(await lstat(path)).isSymbolicLink())throw Error('Not a regular aggregate file');
    files.set(name,JSON.parse(await readFile(path,'utf8')));
  }
  if((await lstat(directory)).isSymbolicLink()||(await lstat(join(directory,'champions'))).isSymbolicLink())throw Error('Symlink directory refused');
  const entries=await readdir(directory);if(entries.some(n=>!['index.json','champions'].includes(n)))throw Error('Unexpected file in aggregate directory');
  await read('index.json');
  for(const name of await readdir(join(directory,'champions'))){if(!/^[A-Za-z][A-Za-z0-9]{0,29}\.json$/.test(name))throw Error('Unexpected champion filename');await read('champions/'+name);}
  return validateRelease(files);
}

export async function main(){
  const files=await readAggregateDirectory(resolve('public/data'));
  if(process.argv.includes('--validate-only')){console.log(`Validated ${files.size} aggregate files.`);return;}
  // Credentials stay in process memory. Never include them in artifacts or diagnostics.
  let siteID=process.env.NETLIFY_SITE_ID,token=process.env.NETLIFY_AUTH_TOKEN;
  if(!siteID)siteID=JSON.parse(await readFile('.netlify/state.json','utf8')).siteId;
  if(!token){
    const configPath=process.platform==='win32'?join(process.env.APPDATA,'netlify','Config','config.json'):join(process.env.HOME,'.config','netlify','config.json');
    const config=JSON.parse(await readFile(configPath,'utf8'));
    token=config.users?.[config.userId]?.auth?.token;
  }
  if(!siteID||!token)throw Error('Netlify site/login required; set NETLIFY_SITE_ID and NETLIFY_AUTH_TOKEN locally');
  const result=await publishRelease(getStore({name:STORE_NAME,siteID,token,consistency:'strong'}),files);
  console.log(`Published aggregate release ${result.current.version} (${files.size} files); previous version retained.`);
}
if(import.meta.url===pathToFileURL(process.argv[1]??'').href)main().catch(error=>{console.error(error.message);process.exitCode=1;});
