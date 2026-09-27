// Local development server; public files only. Never exposes the project root.
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {handler} from '../netlify/functions/riot.mjs';
try {
  for(const line of (await readFile('.env','utf8')).split(/\r?\n/)) {
    const m=line.match(/^\s*(RIOT_API_KEY|SETTISTICS_ADMIN_TOKEN)\s*=\s*(.*?)\s*$/);
    if(m&&!process.env[m[1]])process.env[m[1]]=m[2].replace(/^['"]|['"]$/g,'');
  }
} catch(error) {if(error.code!=='ENOENT')throw error;}
const assets=new Map([['/','index.html'],['/index.html','index.html'],['/app.js','app.js'],['/api-ui.js','api-ui.js'],['/api-ui.css','api-ui.css'],['/data/index.json','data/index.json']]);
for (const page of ['data-client.js','guide.js','guide.css','theme.js','fonts.css','impressum.html','datenschutz.html','riot.txt']) assets.set('/'+page,page);
// Self-hosted assets: strict patterns only, so no path can escape public/.
const assetPattern=/^\/(data\/champions\/[A-Za-z0-9]+\.json|fonts\/[A-Za-z0-9-]+\.woff2|ddragon\/static\.json|ddragon\/(?:champion\/[A-Za-z0-9]+|(?:item|rune|spell)\/[0-9]+)\.png)$/;
// Same policy as netlify.toml, so CSP problems show up locally first.
const CSP="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'";
const types={txt:'text/plain',html:'text/html',js:'text/javascript',css:'text/css',json:'application/json',woff2:'font/woff2',png:'image/png'};
const server=createServer(async(req,res)=>{
  try {
    const url=new URL(req.url,'http://localhost');
    if(url.pathname.startsWith('/api/riot/')) {
      const result=await handler({httpMethod:req.method,path:url.pathname,queryStringParameters:Object.fromEntries(url.searchParams),headers:{...req.headers,'x-admin-token':process.env.SETTISTICS_ADMIN_TOKEN||''}});
      res.writeHead(result.statusCode,result.headers);res.end(result.body);return;
    }
    const file=assets.get(url.pathname)||(assetPattern.test(url.pathname)?url.pathname.slice(1):null);
    if(!file||!['GET','HEAD'].includes(req.method)){res.writeHead(404);res.end('Not found');return;}
    let body;
    try {body=await readFile('public/'+file);}
    catch(error) {if(error.code==='ENOENT'){res.writeHead(404);res.end('Not found');return;}throw error;}
    const ext=file.split('.').pop();
    res.writeHead(200,{'content-type':types[ext]+(['woff2','png'].includes(ext)?'':'; charset=utf-8'),'cache-control':'no-store','x-content-type-options':'nosniff','x-frame-options':'DENY','content-security-policy':CSP});
    res.end(req.method==='HEAD'?undefined:body);
  }catch {res.writeHead(500);res.end('Server error. Check build and API configuration.');}
});
server.listen(Number(process.env.PORT)||8888,'127.0.0.1',()=>console.log('Settistics: http://127.0.0.1:'+(Number(process.env.PORT)||8888)));
