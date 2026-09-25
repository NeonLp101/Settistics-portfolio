// Fail-fast local workflow. No deploy command: publication only switches aggregate data.
import {spawnSync} from 'node:child_process';
import {existsSync,openSync,closeSync,unlinkSync,mkdirSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const python=process.env.PYTHON||(existsSync('.venv/Scripts/python.exe')?resolve('.venv/Scripts/python.exe'):existsSync('.venv/bin/python')?resolve('.venv/bin/python'):'python');
// auto: the GPU when CUDA trains, else the same XGBoost model on CPU. --cpu keeps the old HistGB learner.
const backend=process.argv.includes('--cpu')?'cpu':'auto';
function run(exe,args){
  const started=Date.now(),r=spawnSync(exe,args,{stdio:'inherit'});
  if(r.error||r.status!==0)throw Error(`Refresh stopped: ${args[0]}`);
  console.log(`[${args[0]}: ${((Date.now()-started)/1000).toFixed(0)} s]`);
}
export function refresh(run,python,backend){
  run(python,['pipeline/compare_gpu.py']);
  run(python,['pipeline/wpa.py','--backend',backend]);
  run(python,['pipeline/engine.py','export','--champion','all']);
  run(process.execPath,['scripts/build.mjs']);
  run(process.execPath,['scripts/publish-data.mjs']);
}
if(import.meta.url===pathToFileURL(process.argv[1]??'').href){
  let lock;
  try{mkdirSync('data',{recursive:true});lock=openSync('data/refresh.lock','wx');refresh(run,python,backend);}
  catch(error){console.error(error.code==='EEXIST'?'Another refresh owns data/refresh.lock. If its process exited, remove that stale lock before retrying.':error.message);process.exitCode=1;}
  finally{if(lock!==undefined){closeSync(lock);unlinkSync('data/refresh.lock');}}
}
