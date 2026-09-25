// Optional DOM integration QA (npm install --no-save jsdom). No network or fixture files.
const {JSDOM}=require('jsdom');
const {readFileSync}=require('node:fs');
const assert=require('node:assert/strict');
(async()=>{
 const dom=new JSDOM(readFileSync('index.html','utf8'),{url:'http://localhost/',runScripts:'outside-only'});
 const w=dom.window,errors=[];
 w.addEventListener('error',e=>errors.push(e.message));
 w.AbortSignal=globalThis.AbortSignal;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
 w.HTMLDialogElement.prototype.close=function(){this.open=false;};
 let stats={schemaVersion:2,status:'empty',uniqueMatches:0,sources:[{id:'riot',name:'Riot Match-V5 timelines',type:'riot_match_timelines',supportsWpaResearch:true}],buckets:[]};
 const roster=['Sett','Teemo','Ahri','MonkeyKing'].map(id=>({id,name:id==='MonkeyKing'?'Wukong':id,tags:['Fighter']}));
 w.fetch=async url=>({ok:true,json:async()=>url==='data/stats.json'?stats:String(url).includes('static-data')?{version:'16.18.1',champions:roster}:String(url).includes('health')?{configured:false,connected:false}:String(url).includes('runesReforged')?[]:{data:{}}});
 w.eval(readFileSync('app.js','utf8'));w.eval(readFileSync('api-ui.js','utf8'));
 const settle=()=>new Promise(r=>setTimeout(r,20));await settle();
 const $=s=>w.document.querySelector(s),change=(s,v)=>{$(s).value=v;$(s).dispatchEvent(new w.Event('change',{bubbles:true}));};
 assert.equal($('#champ').options.length,4);assert.match($('#results').textContent,/No collected matches/);
 assert.equal($('#matchup-wpa').textContent,'—');assert.match($('.api-status').textContent,/setup needed/);
 change('#champ','Ahri');change('#opponent','MonkeyKing');assert.match($('#opp-avatar img').src,/MonkeyKing.png/);
 change('#champ','Sett');change('#opponent','Teemo');
 stats={schemaVersion:2,status:'observed',uniqueMatches:10,generatedAt:new Date().toISOString(),sources:[{id:'riot',name:'Riot Match-V5 timelines',type:'riot_match_timelines',supportsWpaResearch:true}],buckets:[{sourceId:'riot',champion:'Sett',opponent:'Teemo',role:'TOP',patch:'16.18',region:'EUW1',games:10,wins:6,eligible:{packages:10,items:10,runes:10,spells:10},choices:[{kind:'packages',id:'1054x1',label:"Doran's Shield",games:10,wins:6,timeSum:0,timeCount:0},{kind:'items',id:'1036',label:'Long Sword',games:5,wins:4,timeSum:30,timeCount:5}]}]};
 $('#retry-data').click();await settle();assert.equal($('#games').textContent,'10');assert.equal($('#observed-wr').textContent,'60.0%');assert.match($('#results').textContent,/Long Sword/);
 $('[data-sort="wr"]').click();for(const tab of ['runes','spells','items'])$(`[data-tab="${tab}"]`).click();
 $('#theme').click();assert.equal(w.document.documentElement.dataset.theme,'light');
 $('#learn').click();assert.equal($('#method').open,true);$('#method .close').click();assert.equal($('#method').open,false);
 $('#riot-connect').click();assert.equal($('.api-dialog').open,true);$('.api-close').click();
 change('#role','BOTTOM');assert.match($('#results').textContent,/No collected matches/);
 change('#role','TOP');change('#opponent','All matchups');assert.equal($('#games').textContent,'10');
 // Simulate broken champion image, then a rerender; fallback must not break selectors.
 $('#champ-img').dispatchEvent(new w.Event('error'));$('#theme').click();change('#champ','Ahri');
 assert.deepEqual(errors,[]);dom.window.close();console.log('DOM integration passed: roster, empty/observed states, filters, sorting, tabs, modals, theme, image fallback.');
})().catch(e=>{console.error(e);process.exit(1);});
