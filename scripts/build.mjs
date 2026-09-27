// Explicit public allowlist: no .env, SQLite, raw Riot data or research reports.
import {mkdir, copyFile, readdir} from 'node:fs/promises';
import {fetchStatic} from './fetch-static.mjs';
import {buildData} from './build-data.mjs';
await mkdir('public/data',{recursive:true});
for (const file of ['index.html','data-client.js','app.js','guide.js','guide.css','theme.js','api-ui.js','api-ui.css','fonts.css','impressum.html','datenschutz.html','riot.txt']) await copyFile(file,`public/${file}`);
await mkdir('public/fonts',{recursive:true});
for (const file of await readdir('fonts')) if(file.endsWith('.woff2')) await copyFile(`fonts/${file}`,`public/fonts/${file}`);
const ddragon=await fetchStatic('public/ddragon');
console.log(`Self-hosted fonts and Data Dragon ${ddragon.version} (${ddragon.champions.length} champions).`);
const data=await buildData();
console.log(`Aggregates: ${data.buckets} buckets in ${data.champions} champion shards${data.splitChampions?` (${data.splitChampions} split into parts)`:''}${data.streamed?' (large export, parsed per bucket)':''}.`);
console.log(`Built public site: ${data.index.uniqueMatches} locally collected matches, ${data.imports} aggregate import(s). No private files copied.`);
