// Cross-platform Python launcher. PYTHON may select a virtualenv interpreter.
import {spawnSync} from 'node:child_process';
import {existsSync} from 'node:fs';
import {resolve} from 'node:path';
const local=process.platform==='win32'?'.venv/Scripts/python.exe':'.venv/bin/python';
const python=process.env.PYTHON||(existsSync(local)?resolve(local):(process.platform==='win32'?'python':'python3'));
const args=process.argv.slice(2);
const result=spawnSync(python,args,{stdio:'inherit'});
if(result.error){console.error('Python 3.10+ is required on PATH (or set PYTHON to its executable).');process.exit(1);}
process.exit(result.status??1);
