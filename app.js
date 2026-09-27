// Observed data only. There is no synthetic statistics generator; WPA is not shown until a validated model exists.
const $=s=>document.querySelector(s);
// Riot names patches after the year (26.19) while the game data still says 16.19.
const patchLabel=p=>{const m=/^(\d+)\.(\d+)$/.exec(p||'');return m?`${Number(m[1])+10}.${m[2]}`:p;};
const REGION_NAMES={EUW1:'EUW',EUN1:'EUNE',NA1:'NA',KR:'KR',JP1:'JP',TR1:'TR',BR1:'BR',LA1:'LAN',LA2:'LAS',OC1:'OCE',RU:'RU',SG2:'SEA',TW2:'TW',VN2:'VN',ME1:'ME'};
const regionLabel=r=>REGION_NAMES[r]||r;
// Riot API test panel: a local developer tool, never loaded on the deployed site.
if(['localhost','127.0.0.1','[::1]'].includes(globalThis.location?.hostname)){const l=document.createElement('link');l.rel='stylesheet';l.href='api-ui.css';document.head.append(l);const s=document.createElement('script');s.src='api-ui.js';document.head.append(s);}
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state={champion:'Sett',opponent:'All matchups',tab:'build',sort:'games',direction:-1};
// Until the visitor (or a direct link) picks a champion, open on the one with the most collected games.
let championChosen=false;
let stats={buckets:[],sources:[],status:'loading'},roster=[],runeNames={},runeTree={},spellNames={},completedItems=new Set(),cdn='';
let loadError='',rosterError='',coverageRows=[];const championCache=new Map();
// Champions whose file failed to load in the background (as an opponent): picking them retries the download.
const failedLoads=new Set();
const options=(node,values,current)=>{node.replaceChildren(...values.map(v=>new Option(v.label??v,v.value??v)));if([...node.options].some(o=>o.value===current))node.value=current;};
let dataGeneration=0,firstStats=true;
// Versioned live-data files never change, so the browser cache may answer them; everything else revalidates.
async function fetchJson(url){const pinned=url.includes('version='),res=await fetch(url,{cache:pinned?'default':'no-cache',signal:AbortSignal.timeout(pinned?30000:15000)});if(!res.ok)throw new Error(`HTTP ${res.status}`);return res.json();}
const liveData=typeof createDataClient==='function'?createDataClient(fetchJson,()=>{dataGeneration++;championCache.clear();stats.buckets=[];setTimeout(()=>loadStats(),0);}):null;
async function json(url){return liveData&&url.startsWith('data/')?liveData.get(url.slice(5)):fetchJson(url);}
// A large champion is split into parts (<id>.json, <id>.2.json, ...) to fit the live-data response limit.
async function championBuckets(id){
  const base=`data/champions/${encodeURIComponent(id)}`,first=await json(`${base}.json`);
  const rest=first.parts>1?await Promise.all(Array.from({length:first.parts-1},(_,i)=>json(`${base}.${i+2}.json`))):[];
  return [first,...rest].flatMap(d=>d.buckets||[]);
}
function wilson(w,n){if(!n)return null;const z=1.96,p=w/n,d=1+z*z/n,c=(p+z*z/(2*n))/d,h=z*Math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d;return[(c-h)*100,(c+h)*100];}
function pct(w,n){return n?`${(100*w/n).toFixed(1)}%`:'—';}
// How far a count can be trusted, in words.
const trust=n=>n<30?'very small sample':n<100?'small sample':n<400?'fair sample':'large sample';
function filtered(){return stats.buckets.filter(b=>b.champion===state.champion&&(state.opponent==='All matchups'||b.opponent===state.opponent)&&b.role===$('#role').value&&($('#patch').value==='All collected patches'||b.patch===$('#patch').value)&&($('#region').value==='All collected regions'||b.region===$('#region').value));}
function source(id){return (stats.sources||[]).find(s=>s.id===id)||{id,name:id||'Unknown source',type:'unknown',supportsWpaResearch:false};}
function selection(){
  const candidates=filtered();if(!candidates.length)return{buckets:[],source:null};
  const grouped=new Map();for(const bucket of candidates){const id=bucket.sourceId||'riot-match-v5';if(!grouped.has(id))grouped.set(id,[]);grouped.get(id).push(bucket);}
  const raw=[...grouped].filter(([id])=>source(id).type==='riot_match_timelines');
  const pool=raw.length?raw:[...grouped];
  pool.sort((a,b)=>b[1].reduce((n,x)=>n+x.games,0)-a[1].reduce((n,x)=>n+x.games,0)||a[0].localeCompare(b[0]));
  const [id,buckets]=pool[0];return{buckets,source:source(id)};
}
function matches(){return selection().buckets;}
const FIRSTS=['firstBloodKill','firstBloodAssist','firstTowerKill','firstTowerAssist'];
function merged(buckets){const out={games:0,wins:0,firsts:{games:0,...Object.fromEntries(FIRSTS.map(k=>[k,0]))},eligible:{packages:0,items:0,runes:0,spells:0,build:0,keystone:0,skills:0},choices:new Map()};for(const b of buckets){out.games+=b.games;out.wins+=b.wins;if(b.firstBloodKill!==undefined){out.firsts.games+=b.games;for(const k of FIRSTS)out.firsts[k]+=b[k]||0;}for(const k of Object.keys(out.eligible))out.eligible[k]+=b.eligible?.[k]||0;for(const c of b.choices){const key=c.kind+':'+c.id;const v=out.choices.get(key)||{...c,games:0,wins:0,timeSum:0,timeCount:0,residSum:0,residSq:0,residN:0,laneSum:0,laneSq:0,laneN:0,laneDelta:0,laneUp:0,curveN:0,preSum:0,curveSum:null,curveSq:null};for(const k of ['games','wins','timeSum','timeCount','residSum','residSq','residN','laneSum','laneSq','laneN','laneDelta','laneUp','preSum'])v[k]+=c[k]||0;if(c.curveN){v.curveN=(v.curveN||0)+c.curveN;for(const key of ['curveSum','curveSq']){v[key]=v[key]||c[key].map(()=>0);c[key].forEach((x,i)=>v[key][i]+=x);}}out.choices.set(key,v);}}return out;}
function champion(id){return roster.find(c=>c.id===id)||{id,name:id,tags:[]};}
// Collected games per champion, role and opponent, so selectors can lead to data instead of empty tables.
// Built once per data source: renders call this many times, and the index has tens of thousands of rows.
let coverageMemo=null,coverageSource=null;
function coverage(){const src=coverageRows.length?coverageRows:stats.buckets;if(src===coverageSource&&coverageMemo)return coverageMemo;const out=new Map();for(const b of src){const c=out.get(b.champion)||{games:0,roles:new Map()};c.games+=b.games;const r=c.roles.get(b.role)||{games:0,opponents:new Map()};r.games+=b.games;r.opponents.set(b.opponent,(r.opponents.get(b.opponent)||0)+b.games);c.roles.set(b.role,r);out.set(b.champion,c);}coverageSource=src;coverageMemo=out;return out;}
function bestRole(id){const roles=[...(coverage().get(id)?.roles||[])].sort((a,b)=>b[1].games-a[1].games);return roles[0]?.[0];}
function topOpponents(id,role,n=10){return[...(coverage().get(id)?.roles.get(role)?.opponents||[])].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0])).slice(0,n).map(([opp])=>opp);}
function champOptions(){const cov=coverage();const sel=$('#champ');
  const group=(label,list)=>{const g=document.createElement('optgroup');g.label=label;list.forEach(c=>g.append(new Option(c.name,c.id)));return g;};
  sel.replaceChildren(group('Champions with games',roster.filter(c=>cov.has(c.id))),group('No games yet',roster.filter(c=>!cov.has(c.id))));
  if([...sel.options].some(o=>o.value===state.champion))sel.value=state.champion;
  options($('#opponent'),[{value:'All matchups',label:'All matchups'},...roster.map(c=>({value:c.id,label:c.name}))],state.opponent);}
function renderChips(){const opps=topOpponents(state.champion,$('#role').value);$('#matchups').innerHTML='<span class="matchups-label">'+(opps.length?'Most collected matchups':'No collected matchups for this role yet')+'</span>'+['All matchups',...opps].map(id=>`<button class="matchup-chip" data-opp="${esc(id)}" aria-pressed="${id===state.opponent}">${esc(id==='All matchups'?id:champion(id).name)}</button>`).join('');}
function nameChoice(c){if(c.kind==='spells')return c.id.split('+').map(id=>spellNames[id]||`Spell ${id}`).join(' + ');if(c.kind==='runes'){const [perks,shards]=c.id.split('|');return perks.split(',').map(id=>runeNames[id]||`Rune ${id}`).join(' · ')+(shards?' · Shards '+shards:'');}return c.label;}
function avatar(node,id){node.replaceChildren();if(!id||!cdn){node.classList.add('any');node.textContent=id?'…':'?';return;}node.classList.remove('any');const img=document.createElement('img');img.src=cdn+'champion/'+encodeURIComponent(id)+'.png';img.alt=champion(id).name;img.onerror=()=>{node.classList.add('any');node.textContent=champion(id).name.slice(0,2);};node.append(img);}
function render(){
  const chosen=selection(),selected=merged(chosen.buckets),activeSource=chosen.source;
  avatar($('#champ-img').parentElement,state.champion);
  // Keep a stable selector after rebuilding the avatar.
  const champImage=document.querySelector('#champ-portrait img');if(champImage)champImage.id='champ-img';
  avatar($('#opp-avatar'),state.opponent==='All matchups'?null:state.opponent);
  // Class tags only where they tell the two apart; game counts say what they count.
  {const total=id=>{const g=coverage().get(id)?.games||0;return g?`${g.toLocaleString('en-US')} game${g===1?'':'s'} in total`:'no games yet';};
   const mine=champion(state.champion).tags.join(' · ');$('#champ-role').textContent=[mine,total(state.champion)].filter(Boolean).join(' · ');
   const theirs=state.opponent==='All matchups'?'':champion(state.opponent).tags.join(' · ');
   $('#damage-type').textContent=state.opponent==='All matchups'?'every lane opponent together':[theirs!==mine?theirs:'',total(state.opponent)].filter(Boolean).join(' · ');}
  $('#opponent').classList.toggle('is-any',state.opponent==='All matchups');
  const base=state.opponent==='All matchups'?null:selectionFor('All matchups');
  renderTape(selected,base);
  
  document.querySelectorAll('.matchup-chip').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.opp===state.opponent)));
  const updated=activeSource?.generatedAt||stats.generatedAt;
  const shownPatches=(stats.patches||[]).map(patchLabel).sort().reverse();
  const sourceStatus=activeSource?.type==='riot_match_timelines'
    ?`Patch ${shownPatches.slice(0,2).join(', ')||'–'} · ${Number(stats.uniqueMatches||0).toLocaleString('en-US')} ranked games · ${(stats.regions||[]).map(regionLabel).join(' + ')}`
    :activeSource?`${activeSource.name} · imported aggregate counts`:'';
  $('#data-status').textContent=loadError|| (stats.status==='loading'?'Loading collected statistics…':(coverageRows.length||stats.buckets.length)?`${sourceStatus||`${stats.sources?.length||0} data source(s)`}${updated?` · updated ${new Date(updated).toLocaleString([],{dateStyle:'medium',timeStyle:'short'})}`:''}`:'No games collected yet.');
  if(rosterError)$('#data-status').textContent+=' '+rosterError;
  const root=$('#results');
  if(!selected.games){root.innerHTML=`<section class="panel empty-state"><h2>${loadError?'Statistics unavailable':'No collected matches for these filters'}</h2><p>${loadError?'Check the deployment and retry.':'We have not collected enough games for this exact selection yet. Try All matchups, another role, or one of the matchups listed above.'}</p><button class="icon-btn" id="retry-data">Reload data</button></section>`;$('#retry-data').onclick=loadStats;}
  else if(state.tab==='lane'){root.innerHTML=laneView(selected);}
  else if(state.tab==='build'){root.innerHTML=buildView(selected,base);}
  else{const kinds=state.tab==='items'?['packages','items']:state.tab==='runes'?['runes']:['spells'];root.innerHTML=kinds.map(k=>table(k,selected)).join('');}
  {const m=stats.wpaModel;
   $('#wpa-quality').textContent=m?`Model trained on ${m.games.toLocaleString()} games (${m.snapshots.toLocaleString()} game states), AUC ${m.auc.toFixed(2)}, predictions calibrated within ${m.calibrationErrorPp.toFixed(1)} percentage points. Every game is scored by a model that never saw it. With this much data most intervals are still wide.`:'';}
  const provenance=activeSource?.type==='aggregate_import'
    ?'Published aggregate counts only. Underlying matches, player overlap and timelines cannot be independently audited here; this source can never produce WPA.'
    :'Locally collected Riot matches and timelines. The ladder/manual seed remains a convenience sample; rank at match time is unknown.';
  $('#insights').innerHTML=`<div class="insight"><strong>Evidence for this selection</strong><span>${selected.games.toLocaleString()} champion-game observations; ${selected.eligible.packages.toLocaleString()} eligible early packages. Source: ${esc(activeSource?.name||'none')}.</span></div><div class="insight"><strong>Source boundary</strong><span>${provenance}</span></div>`;
}

// ---- Build view: 1st-5th finished item, boots and when they are bought, per matchup ----
const ORD=['1st','2nd','3rd','4th','5th'];
const TIMING=['Before 1st item','After 1st item','After 2nd item','After 3rd item or later'];
const WPA_MIN=15,WPA_TAG=30;
// WPA in percentage points with a 95% interval: mean of (won - predicted) over the players who chose it.
function wpaOf(c,ref=0){if(!c||!c.residN||c.residN<WPA_MIN)return null;const raw=c.residSum/c.residN,v=Math.max(0,c.residSq/c.residN-raw*raw),h=1.96*Math.sqrt(v/c.residN);return{m:(raw-ref)*100,h:h*100,n:c.residN};}
function wpaBadge(c,ref){
  const w=wpaOf(c,ref);
  if(!w)return c&&c.residN?`<span class="wpa" title="WPA needs at least ${WPA_MIN} games">WPA: needs more games</span>`:'';
  const cls=w.m-w.h>0?'pos':w.m+w.h<0?'neg':'';
  const tag=w.n>=WPA_TAG&&w.m-w.h>0?'<span class="tag edge">Edge</span>':w.n>=WPA_TAG&&w.m+w.h<0?'<span class="tag costly">Costly</span>':'';
  return `<span class="wpa ${cls||(w.m>=0?'':'')}" title="Win Probability Added compared with the average choice for this slot, 95% interval ±${w.h.toFixed(1)} pp over ${w.n} games">WPA ${w.m>=0?'+':'−'}${Math.abs(w.m).toFixed(1)} pp <i>±${w.h.toFixed(1)}</i></span>${tag}`;
}
const minutes=c=>c&&c.timeCount?c.timeSum/c.timeCount:null;
const icon=(id,size='')=>`<span class="ico ${size}">${cdn?`<img src="${cdn}item/${encodeURIComponent(id)}.png" alt="" loading="lazy">`:''}</span>`;
function rowsOf(summary,kind){const rows=[...summary.choices.values()].filter(c=>c.kind===kind).sort((a,b)=>b.games-a.games||a.id.localeCompare(b.id));const rn=rows.reduce((n,c)=>n+(c.residN||0),0),rs=rows.reduce((n,c)=>n+(c.residSum||0),0);return{rows,total:rows.reduce((n,c)=>n+c.games,0),ref:rn?rs/rn:0};}
function selectionFor(opponent){const prev=state.opponent;state.opponent=opponent;try{return merged(selection().buckets);}finally{state.opponent=prev;}}
function versus(base,kind,c,total){
  if(!base)return'';
  const b=rowsOf(base,kind),other=b.rows.find(x=>x.id===c.id),here=c.games/total,there=other&&b.total?other.games/b.total:0,d=(here-there)*100;
  if(Math.abs(d)<5)return'<span class="vs">about the same as all matchups</span>';
  return `<span class="vs ${d>0?'up':'down'}">${d>0?'+':'−'}${Math.abs(d).toFixed(0)} pp vs all matchups (${(there*100).toFixed(0)}%)</span>`;
}

// ---- Win impact: WPA of each option in one build slot, as a bar from the centre with its 95% interval ----
const JUDGE_SLOTS=[...ORD.map((o,i)=>[`slot${i+1}`,`${o} item`]),['boots','Boots']];
const JUDGE_RANGE=10;  // bars span ±10 percentage points
const fmtPp=v=>`${v>=0?'+':'−'}${Math.abs(v).toFixed(1)} pp`;
// Say the finding in words: the clearest winner, and any option whose win rate is inflated by being bought while ahead.
function judgeLede(rows,ref,label){
  const games=rows.reduce((n,c)=>n+c.games,0),avg=games?rows.reduce((n,c)=>n+c.wins,0)/games:0;
  const scored=rows.slice(0,6).map(c=>({c,w:wpaOf(c,ref),wr:c.wins/c.games})).filter(s=>s.w&&s.w.n>=WPA_TAG);
  const best=scored.filter(s=>s.w.m-s.w.h>0).sort((a,b)=>b.w.m-a.w.m)[0];
  const trap=scored.filter(s=>s.w.m<0&&s.wr-avg>=.03).sort((a,b)=>(b.wr-avg)-(a.wr-avg))[0];
  const out=[];
  if(best)out.push(`<b>${esc(nameChoice(best.c))}</b> is the strongest ${label}: ${fmtPp(best.w.m)} win chance (±${best.w.h.toFixed(1)}).`);
  if(trap)out.push(`<b>${esc(nameChoice(trap.c))}</b> wins ${pct(trap.c.wins,trap.c.games)} of games, but mostly because it is bought while already ahead: its own effect is ${fmtPp(trap.w.m)}.`);
  if(!out.length&&scored.length)out.push(`No ${label} clearly beats the average yet.`);
  return out.length?`<p class="judge-lede">${out.join(' ')}</p>`:'';
}
function judges(sel,base){
  const kind=state.judge||'slot1',{rows,total,ref}=rowsOf(sel,kind);
  const x=v=>Math.max(0,Math.min(100,50+v/JUDGE_RANGE*50)),slotName=(JUDGE_SLOTS.find(([k])=>k===kind)||[,''])[1].toLowerCase();
  const list=rows.slice(0,6).map(c=>{
    const w=wpaOf(c,ref),share=`${pct(c.games,total)} · ${pct(c.wins,c.games)} WR`;
    const who=`<div class="who">${icon(c.id)}<div><b>${esc(nameChoice(c))}</b><span>${share}</span>${versus(base,kind,c,total)}</div></div>`;
    if(!w)return`<div class="judge dimmed">${who}<div class="scale"></div><div class="score dim">—<small>${c.residN||0} of ${WPA_MIN} games</small></div></div>`;
    const cls=w.m-w.h>0?'pos':w.m+w.h<0?'neg':'dim',lo=x(Math.min(0,w.m)),hi=x(Math.max(0,w.m));
    return`<div class="judge">${who}<div class="scale" aria-hidden="true"><i class="bar ${cls}" style="left:${lo}%;width:${hi-lo}%"></i><i class="ci" style="left:${x(w.m-w.h)}%;width:${x(w.m+w.h)-x(w.m-w.h)}%"></i></div>
      <div class="score ${cls}">${fmtPp(w.m)}<small>±${w.h.toFixed(1)} · ${w.n} scored games</small></div></div>`;
  }).join('')||'<p class="panel-note">No purchases recorded for this slot yet.</p>';
  return`<section class="card judges-card"><div class="card-head"><div><h2>Win impact</h2><p>Win Probability Added: how much each option changed the win chance, compared with the average choice for this slot. The thin line shows the likely range (95%).</p></div></div>
    <div class="judge-tabs" role="tablist">${JUDGE_SLOTS.map(([k,l])=>`<button class="judge-tab" data-judge="${k}" aria-pressed="${k===kind}">${l}</button>`).join('')}</div>
    ${judgeLede(rows,ref,slotName)}<div class="judges">${list}${rows.slice(0,6).some(c=>wpaOf(c,ref))?`<div class="judge axis" aria-hidden="true"><div></div><div class="ticks"><span>−${JUDGE_RANGE} pp</span><span>0</span><span>+${JUDGE_RANGE} pp</span></div><div></div></div>`:''}</div>
    <p class="judge-foot">Win rate alone misleads: items bought while already ahead win more whatever they do. Gold helps, red hurts, grey is not distinguishable from zero yet. Scores count only games the model could score. Scores need ${WPA_MIN} games; with many items compared at once, a single borderline score can be chance.</p></section>`;
}
function coreList(sel){
  const {rows,total}=rowsOf(sel,'core');
  if(!total)return'';
  return `<div class="cores"><h3 class="mini">Most common first three items</h3>${rows.slice(0,4).map(c=>`<div class="core"><span class="icons">${c.id.split('>').map(id=>icon(id,'sm')).join('')}</span><strong>${esc(c.label)}</strong><span>${pct(c.games,total)} of ${total} complete cores · ${pct(c.wins,c.games)} WR</span></div>`).join('')}</div>`;
}
// ---- Champ select: what has to be set before the game, in the order it is set ----
const LOADOUT_MIN=30;
// Rest of a rune page as the client lays it out: primary tree top to bottom, then the secondary tree by name.
function runeLines(perks,keyId,rune){
  const rest=perks.filter(id=>id!==keyId),tree=id=>runeTree[id]?.[0],primary=tree(keyId);
  if(!primary||rest.some(id=>!runeTree[id]))return`<span class="lo-sub">${rest.map(rune).join(' · ')}</span>`;
  const byRow=list=>list.sort((a,b)=>runeTree[a][1]-runeTree[b][1]).map(rune).join(' · ');
  const main=rest.filter(id=>tree(id)===primary),second=rest.filter(id=>tree(id)!==primary);
  return`<span class="lo-sub">${byRow(main)}</span>${second.length?`<span class="lo-sub">${rune(tree(second[0]))}: ${byRow(second)}</span>`:''}`;
}
function loadoutView(sel,base){
  // Too few games in this matchup to recommend a setup: use the champion's usual one and say so.
  const thin=!!base&&sel.games<LOADOUT_MIN,src=thin?base:sel,you=esc(champion(state.champion).name);
  const top=kind=>{const r=rowsOf(src,kind);return r.rows[0]?{c:r.rows[0],total:r.total}:null;};
  const page=top('runes'),spells=top('spells'),start=top('packages'),first=top('slot1'),ks=rowsOf(src,'keystone');
  // Skill order: most common max order and opener, counted separately (short games have an opener but no max).
  const maxes=top('skillMax'),opener=top('skillStart'),skill=id=>esc(id.split('>').join(' > ')),dash=id=>esc(id.split('>').join('-'));
  const perks=page?page.c.id.split('|')[0].split(','):[],ksIds=new Set(ks.rows.map(c=>c.id));
  const keyId=perks.find(id=>ksIds.has(id))||perks[0],keyRow=ks.rows.find(c=>c.id===keyId);
  const rune=id=>esc(runeNames[id]||`Rune ${id}`),share=(c,total)=>`${pct(c.games,total)} of ${total.toLocaleString('en-US')} games`;
  const cell=(label,body,evidence)=>`<div class="lo"><div class="lo-k">${label}</div>${body}<span class="lo-n">${evidence}</span></div>`;
  const cells=[
    page?cell('Runes',`<strong>${rune(keyId)}</strong>${runeLines(perks,keyId,rune)}`,
      `${keyRow?`${rune(keyId)} in ${pct(keyRow.games,ks.total)}`:''}${keyRow?' · ':''}this exact page in ${pct(page.c.games,page.total)}`):'',
    spells?cell('Summoner spells',`<strong>${esc(nameChoice(spells.c))}</strong>`,share(spells.c,spells.total)):'',
    maxes?cell('Skill order',`<strong>${skill(maxes.c.id)}</strong>${opener?`<span class="lo-sub">start ${dash(opener.c.id)} · ${pct(opener.c.games,opener.total)} of openers</span>`:''}`,share(maxes.c,maxes.total))
      :opener?cell('Skill order',`<strong>start ${dash(opener.c.id)}</strong><span class="lo-sub">max order needs longer games</span>`,share(opener.c,opener.total)):'',
    start?cell('Start',`<strong>${esc(nameChoice(start.c))}</strong>`,share(start.c,start.total)):'',
    first?cell('Rush',`<span class="lo-item">${icon(first.c.id)}<strong>${esc(nameChoice(first.c))}</strong></span>`,`${share(first.c,first.total)}${minutes(first.c)?` · done ~${minutes(first.c).toFixed(0)} min`:''}`):''
  ].filter(Boolean);
  if(!cells.length)return'';
  // One plain sentence first; the cards below back it up.
  const who=state.opponent==='All matchups'||thin?`${you} players`:`${you} players against ${esc(champion(state.opponent).name)}`;
  const setup=[page&&rune(keyId),spells&&esc(nameChoice(spells.c))].filter(Boolean).join(' with ');
  const levels=[opener&&`level ${dash(opener.c.id)} first`,maxes&&`max ${skill(maxes.c.id)}`].filter(Boolean).join(' and ');
  const parts=[setup&&`take ${setup}`,levels,start&&`start ${esc(nameChoice(start.c))}`,first&&`finish ${esc(nameChoice(first.c))} first${minutes(first.c)?` around minute ${minutes(first.c).toFixed(0)}`:''}`].filter(Boolean);
  const list=parts.length>1?parts.slice(0,-1).join(', ')+' and '+parts.at(-1):parts[0];
  const note=thin?`<p class="thin">Only ${sel.games} game${sel.games===1?'':'s'} against ${esc(champion(state.opponent).name)} so far, too few to recommend a setup. This is ${you}'s usual setup across all matchups.</p>`:'';
  return`<section class="card loadout"><div class="card-head"><div><h2>Champ select</h2><p class="lo-lede">${who} usually ${list}. Based on ${src.games.toLocaleString('en-US')} games (${trust(src.games)}).</p></div></div>${note}<div class="lo-grid">${cells.join('')}</div></section>`;
}
function buildView(sel,base){
  const games=sel.eligible.build||0;
  if(!games&&stats.itemDataMissing?.length)return`<section class="panel empty-state"><h2>Item data for patch ${esc(stats.itemDataMissing.map(patchLabel).join(', '))} is not published yet</h2><p>Riot publishes item data (Data Dragon) by hand, usually within a day or two of a patch. Games are already being collected; builds appear here as soon as the item data is out. Lane 1v1, runes and spells work already.</p></section>`;
  // Most common item per slot, skipping items already placed earlier, so the path is a buildable sequence.
  const used=new Set(),tops=ORD.map((_,i)=>{const c=rowsOf(sel,`slot${i+1}`).rows.find(x=>!used.has(x.id));if(c)used.add(c.id);return c;});
  const boots=rowsOf(sel,'boots'),timing=rowsOf(sel,'bootsTiming');
  const when=timing.rows[0]?Number(timing.rows[0].id):null;
  // Most common path, with boots inserted where players usually finish them.
  const steps=[];
  const slotMinute=i=>{const r=rowsOf(sel,`slot${i+1}`).rows;const n=r.reduce((a,c)=>a+c.timeCount,0);return n?r.reduce((a,c)=>a+c.timeSum,0)/n:null;};
  const bootsStep=()=>({c:boots.rows[0],label:'Boots',n:'B',m:minutes(boots.rows[0]),total:boots.total});
  tops.forEach((c,i)=>{if(when===i&&boots.rows[0])steps.push(bootsStep());if(c)steps.push({c,label:`${ORD[i]} item`,n:String(i+1),m:slotMinute(i),total:rowsOf(sel,`slot${i+1}`).total});});
  if(boots.rows[0]&&when!==null&&when>=tops.filter(Boolean).length)steps.push(bootsStep());
  const path=steps.length?steps.map(s=>`<div class="round${s.n==='B'?' boots':''}" data-n="${s.n}"><div class="rk">${s.label}</div><div class="rt">${s.m?'~'+s.m.toFixed(0)+' min':''}</div>${icon(s.c.id,'lg')}<strong>${esc(nameChoice(s.c))}</strong><span>${pct(s.c.games,s.total)} of games</span></div>`).join(''):'<p class="panel-note">No complete builds recorded for this selection yet.</p>';
  const matchup=state.opponent==='All matchups'?'across all matchups':`vs ${esc(champion(state.opponent).name)}`;
  const prov=Object.keys(stats.itemDataProvisional||{}).map(patchLabel);
  const provNote=prov.length?`<p class="thin">Patch ${esc(prov.join(', '))} item data is provisional until Riot publishes it; the new items are not included yet.</p>`:'';
  const warn=games&&games<30?`<p class="thin">Based on only ${games} game${games===1?'':'s'} ${matchup}. Treat this as a rough guide.</p>`:'';
  const bootsRows=boots.rows.slice(0,4).map(c=>`<div class="pick">${icon(c.id)}<div><strong>${esc(nameChoice(c))}</strong><span>${pct(c.games,boots.total)} · ${pct(c.wins,c.games)} WR${minutes(c)?` · ~${minutes(c).toFixed(0)} min`:''}</span><div class="wpaline">${wpaBadge(c,boots.ref)}</div>${versus(base,'boots',c,boots.total)}</div></div>`).join('')||'<p class="panel-note">No boots recorded yet.</p>';
  const seg=i=>timing.rows.find(x=>x.id===String(i));
  const bar=timing.total?`<div class="tbar">${TIMING.map((l,i)=>{const c=seg(i);const w=c?c.games/timing.total*100:0;return w?`<i style="width:${w}%;opacity:${1-i*.2}" title="${l}: ${w.toFixed(0)}%"></i>`:''}).join('')}</div>
    <div class="tlegend">${TIMING.map((l,i)=>{const c=seg(i);return c?`<span><i style="opacity:${1-i*.2}"></i>${l}<b>${pct(c.games,timing.total)}</b></span>`:''}).join('')}</div>`:'';
  const pk=rowsOf(sel,'packages');
  const starts=pk.rows.slice(0,3).map(c=>`<div class="pick alt"><div><strong>${esc(nameChoice(c))}</strong><span>${pct(c.games,pk.total)} · ${pct(c.wins,c.games)} WR</span></div></div>`).join('')||'<p class="panel-note">No clean starting purchases recorded yet.</p>';
  return `${loadoutView(sel,base)}<section class="card"><div class="card-head"><div><h2>Game plan ${matchup}</h2><p>The most common item at each step, in the order items are finished, with boots where they are usually bought. Few games contain every step; the most common real item sets are listed below. ${games.toLocaleString()} games.</p></div></div>
      ${provNote}${warn}<div class="rounds">${path}</div>${coreList(sel)}</section>
    ${judges(sel,base)}
    <div class="twocol"><section class="card"><div class="card-head"><div><h2>Boots</h2><p>Which boots, and when they are finished relative to the item build.</p></div></div><div class="cardbody">${bootsRows}${bar?`<h3 class="mini">When</h3>${bar}`:''}</div></section>
      <section class="card"><div class="card-head"><div><h2>Starting options</h2><p>Every common first-minute purchase, most popular first.</p></div></div><div class="cardbody">${starts}</div></section></div>
    <details class="card log"><summary>Full purchase log <span>every item bought, including components and consumables</span></summary>${table('items',sel)}</details>`;
}

// ---- Lane 1v1 view: gold lead change against the lane opponent while nobody else interferes ----
const LANE_MIN=15;
const LANE_ROLES={TOP:'Top',MIDDLE:'Mid'};
const LANE_SECTIONS=[['packages','Starting items','minute 1 until the first interference (max. 10)'],['keystone','Keystone','minute 1 until the first interference (max. 10)'],
  ['spells','Summoner spells','minute 1 until the first interference (max. 10)'],['slot1','First item','5 minutes after finishing it, if finished by minute 14'],
  ['boots','Boots','5 minutes after buying them, if bought by minute 14']];
function laneOf(c,ref){if(!c||!c.laneN||c.laneN<LANE_MIN)return null;if(stats.reliability&&!stats.reliability.lane?.[c.kind]?.pass)return null;const m=c.laneSum/c.laneN,v=Math.max(0,c.laneSq/c.laneN-m*m);return{m:m-ref,h:1.96*Math.sqrt(v/c.laneN),n:c.laneN,raw:c.laneDelta/c.laneN};}
const gold=v=>`${v>=0?'+':'−'}${Math.abs(Math.round(v)).toLocaleString('en-US')}`;
function laneLabel(c){return c.kind==='keystone'?(runeNames[c.id]||`Rune ${c.id}`):nameChoice(c);}
function laneView(sel){
  const role=$('#role').value;
  if(!LANE_ROLES[role])return`<section class="panel empty-state"><h2>Lane duels cover top and mid</h2><p>Jungle has no lane opponent and bot lane is a 2v2, so a clean 1v1 comparison is not possible there. Pick a top or mid laner.</p></section>`;
  const m=stats.laneModel,vs=state.opponent==='All matchups'?'all matchups':`vs ${esc(champion(state.opponent).name)}`;
  const intro=`<section class="card"><div class="card-head"><div><h2>Lane duel · ${LANE_ROLES[role]} ${vs}</h2><p>How each choice changed the gold lead against the lane opponent while nobody else interfered: every lane is measured only until the first gank kill or assist, or until another champion is repeatedly nearby. Values compare a choice with the average choice in the same slot. Prototype${m?` · ${m.windows.toLocaleString('en-US')} clean laning windows`:''}.</p></div></div></section>`;
  const cards=LANE_SECTIONS.map(([kind,title,window])=>{
    const rows=[...sel.choices.values()].filter(c=>c.kind===kind&&c.laneN).sort((a,b)=>b.laneN-a.laneN);
    const n=rows.reduce((a,c)=>a+c.laneN,0),ref=n?rows.reduce((a,c)=>a+c.laneSum,0)/n:0;
    const body=rows.length?rows.slice(0,8).map(c=>{const l=laneOf(c,ref);
      const badge=l?`<span class="wpa ${l.m-l.h>0?'pos':l.m+l.h<0?'neg':''}" title="95% interval ±${Math.round(l.h)} gold over ${l.n} clean windows">${gold(l.m)} gold <i>±${Math.round(l.h)}</i></span>${l.n>=30&&l.m-l.h>0?'<span class="tag edge">Edge</span>':l.n>=30&&l.m+l.h<0?'<span class="tag costly">Costly</span>':''}`:`<span class="wpa">${stats.reliability&&!stats.reliability.lane?.[kind]?.pass?'not reproducible yet':'needs more games'}</span>`;
      return`<div class="pick">${['slot1','boots'].includes(kind)?icon(c.id):''}<div><strong>${esc(laneLabel(c))}</strong><span>${c.laneN.toLocaleString('en-US')} clean window${c.laneN===1?'':'s'}${l?` · lane gold ${gold(l.raw)} on average`:''}</span><div class="wpaline">${badge}</div></div></div>`;}).join('')
      :`<p class="panel-note">${['packages','slot1','boots'].includes(kind)&&stats.itemDataMissing?.length?`Available once Riot publishes the item data for patch ${esc(stats.itemDataMissing.map(patchLabel).join(', '))}.`:'No clean 1v1 windows for this selection yet.'}</p>`;
    return`<section class="card"><div class="card-head"><div><h2>${title}</h2><p>Measured ${window}.</p></div></div><div class="cardbody">${body}</div></section>`;
  }).join('');
  return intro+`<div class="twocol">${cards}</div>`;
}

// ---- Fight card: tale of the tape, opponent's own view, lane verdict ----
function opponentView(){
  // The opponent's file holds their games against us, seen from their side.
  const opp=state.opponent;if(opp==='All matchups')return null;
  if(!championCache.has(opp)){const generation=dataGeneration;championCache.set(opp,null);championBuckets(opp).then(b=>{if(generation!==dataGeneration)return;championCache.set(opp,b);renderSafely();}).catch(()=>{if(generation===dataGeneration){championCache.set(opp,[]);failedLoads.add(opp);}});return null;}
  const list=championCache.get(opp);if(!list)return null;
  const role=$('#role').value,patch=$('#patch').value,region=$('#region').value;
  return merged(list.filter(b=>b.champion===opp&&b.opponent===state.champion&&b.role===role&&(patch==='All collected patches'||b.patch===patch)&&(region==='All collected regions'||b.region===region)));
}
function topChoice(summary,kind){const rows=summary?[...summary.choices.values()].filter(c=>c.kind===kind).sort((a,b)=>b.games-a.games):[];return rows[0]||null;}
function laneDuel(summary){const rows=[...summary.choices.values()].filter(c=>c.kind==='keystone'&&c.laneN);const n=rows.reduce((a,c)=>a+c.laneN,0);return n?{gold:rows.reduce((a,c)=>a+(c.laneDelta||0),0)/n,n}:null;}
function tapeItem(c,label){if(!c)return'<span class="muted">—</span>';const m=c.timeCount?` · ${Math.round(c.timeSum/c.timeCount)} min`:'';return`<span class="tape-item">${c.kind==='keystone'?'':icon(c.id)}${esc(label||nameChoice(c))}${m}</span>`;}
function renderTape(sel,base){
  const any=state.opponent==='All matchups',opp=any?null:opponentView(),role=$('#role').value;
  const roleName={TOP:'Top lane',JUNGLE:'Jungle',MIDDLE:'Mid lane',BOTTOM:'Bot lane',UTILITY:'Support'}[role]||role;
  $('#bill').textContent=[roleName,`Patch ${((stats.patches||[]).map(patchLabel).sort().reverse()[0])||'–'}`,(stats.regions||[]).map(regionLabel).join(' + ')].join(' · ');
  $('#vs-games').textContent=sel.games?`${sel.games.toLocaleString('en-US')} game${sel.games===1?'':'s'} · ${trust(sel.games)}`:'no games';
  const wr=sel.games?100*sel.wins/sel.games:null,lane=laneDuel(sel),you=champion(state.champion).name,them=any?'the field':champion(state.opponent).name;
  // The lane duel is one number for both corners, so it lives only in the verdict.
  const v=$('#verdict'),duels=n=>`${n} clean duel${n===1?'':'s'}`;
  v.classList.remove('dim');
  if(lane&&lane.n>=15){v.hidden=false;v.innerHTML=`${Math.abs(lane.gold)<50?'Lane: even':lane.gold>0?`Lane: ${esc(you)} by +${Math.round(lane.gold)} gold`:`Lane: ${esc(any?'field':them)} by +${Math.round(-lane.gold)} gold`}<small>gold lead until the first gank · ${duels(lane.n)}</small>`;}
  else if(['TOP','MIDDLE'].includes(role)){v.hidden=false;v.classList.add('dim');v.innerHTML=`Lane: too few duels<small>${duels(lane?.n||0)} of 15 needed</small>`;}
  else v.hidden=true;
  const keyLabel=c=>c?(runeNames[c.id]||`Rune ${c.id}`):'';
  const mins=c=>c?.timeCount?` · ${Math.round(c.timeSum/c.timeCount)} min`:'';
  // Opponent side of a row; when both corners pick the same thing, say so instead of repeating it.
  const right=(kind,mine)=>{if(any)return'<span class="muted">varies by opponent</span>';if(!opp)return'<span class="muted">loading…</span>';
    const c=topChoice(opp,kind);if(c&&mine&&c.id===mine.id)return`<span class="same">Same${mins(c)}</span>`;return tapeItem(c,kind==='keystone'?keyLabel(c):null);};
  // A matchup win rate means little on its own; compare it with this champion's usual win rate once there are enough games.
  const usualWr=base?.games?100*base.wins/base.games:null,usual=wr!=null&&usualWr!=null&&sel.games>=30?`<small>${fmtPp(wr-usualWr)} vs ${esc(you)}'s usual ${usualWr.toFixed(1)}%</small>`:'';
  const ci=wilson(sel.wins,sel.games),slot1=topChoice(sel,'slot1'),boots=topChoice(sel,'boots'),ks=topChoice(sel,'keystone');
  $('#tape').innerHTML=
    `<div class="tape-row"><div class="l"><div class="tape-v">${wr==null?'—':wr.toFixed(1)+'%'}<small>${esc(you)} won${ci?` · likely ${ci[0].toFixed(0)}–${ci[1].toFixed(0)}%`:''}</small>${usual}</div></div><div class="k">Win rate</div><div class="r"><div class="tape-v">${wr==null?'—':(100-wr).toFixed(1)+'%'}<small>${any?'opponents won':`${esc(them)} won`}</small></div></div>`+
      (wr==null?'':`<div class="tape-split"><i class="a" style="width:${wr}%"></i><i class="b" style="width:${100-wr}%"></i></div>`)+`</div>`+
    (any?'':`<div class="tape-row tape-extra"><div class="l">${tapeItem(slot1)}</div><div class="k">First item</div><div class="r">${right('slot1',slot1)}</div></div>`+
    `<div class="tape-row tape-extra"><div class="l">${tapeItem(boots)}</div><div class="k">Boots</div><div class="r">${right('boots',boots)}</div></div>`+
    `<div class="tape-row tape-extra"><div class="l">${tapeItem(ks,keyLabel(ks))}</div><div class="k">Keystone</div><div class="r">${right('keystone',ks)}</div></div>`);
}
const titles={packages:'Early starting packages',items:'Items purchased at any time',runes:'Complete rune pages',spells:'Summoner spell pairs'};
const notes={packages:'Net purchases during the first 60 seconds; uncertain ledgers, early upgrades and early-combat games excluded.',items:'Each item counted once per player-game. Purchase order and game state create selection bias; this is not item strength.',runes:'Whole pages, including stat shards, are observed together; individual rune effects are not isolated.',spells:'Observed outcomes for the pair; not an estimated causal effect.'};
function table(kind,summary){
  const rows=[...summary.choices.values()].filter(c=>c.kind===kind);
  rows.sort((a,b)=>{const score=c=>state.sort==='wr'?c.wins/c.games:state.sort==='time'?(c.timeCount?c.timeSum/c.timeCount:Infinity):c.games;return(score(a)-score(b))*state.direction||a.id.localeCompare(b.id);});
  const header=(key,label)=>`<th><button data-sort="${key}">${label}${state.sort===key?(state.direction<0?' ↓':' ↑'):''}</button></th>`;
  return `<section class="card"><div class="card-head"><div><h2>${titles[kind]}</h2><p>${notes[kind]}</p></div></div><div class="table-scroll"><table><thead><tr><th>Choice</th>${header('wr','Observed WR')}<th>95% interval</th><th>Occurrence</th>${header('games','Games')}${kind==='items'?header('time','First buy · avg min'):''}</tr></thead><tbody>${rows.map(c=>{const ci=wilson(c.wins,c.games);return`<tr><td class="choice-text">${esc(nameChoice(c))}${c.games<100?'<span class="tag low">small sample</span>':''}</td><td class="mono">${pct(c.wins,c.games)}</td><td class="mono muted">${ci?`${ci[0].toFixed(1)}–${ci[1].toFixed(1)}%`:'—'}</td><td class="mono">${pct(c.games,summary.eligible[kind])}</td><td class="mono">${c.games.toLocaleString()}</td>${kind==='items'?`<td class="mono">${c.timeCount?(c.timeSum/c.timeCount).toFixed(1):'—'}</td>`:''}</tr>`;}).join('')||`<tr><td colspan="6">No eligible ${titles[kind].toLowerCase()} for this source.</td></tr>`}</tbody></table></div></section>`;
}
function renderSafely(){
  if(document.getElementById?.('guide-root')){renderGuide();return;}
  // Avatar fallback removes the image; restore its anchor before rendering.
  if(!$('#champ-img')){const img=document.createElement('img');img.id='champ-img';$('#champ-portrait').replaceChildren(img);}
  renderChips();
  render();
}
async function loadStats(){
  const generation=++dataGeneration;championCache.clear();failedLoads.clear();
  loadError='';
  try{const data=await json('data/index.json');if(generation!==dataGeneration)return;if(data.schemaVersion!==2||!Array.isArray(data.coverage))throw new Error('Unsupported data export');
    const {coverage:rows,patches:allPatches,regions,...meta}=data;stats={sources:[],...meta,buckets:[],patches:allPatches,regions};coverageRows=rows;
    const patches=[...allPatches].sort((a,b)=>b.localeCompare(a,undefined,{numeric:true}));
    options($('#patch'),[{value:'All collected patches',label:'All patches'},...patches.map(p=>({value:p,label:`Patch ${patchLabel(p)}`}))],$('#patch').value||patches[0]);
    options($('#region'),[{value:'All collected regions',label:'All regions'},...[...regions].sort().map(r=>({value:r,label:regionLabel(r)}))],$('#region').value);
    // Links such as ?champion=aatrox&vs=darius: match ids case-insensitively, fall back when unknown.
    if(state.fromUrl){const known=[...coverage().keys()],fix=id=>known.find(k=>k.toLowerCase()===String(id).toLowerCase());
      const c=fix(state.champion);if(c)state.champion=c;else championChosen=false;
      if(state.opponent!=='All matchups')state.opponent=(c&&fix(state.opponent))||'All matchups';state.fromUrl=false;}
    if(!championChosen){const top=[...coverage()].sort((a,b)=>b[1].games-a[1].games||a[0].localeCompare(b[0]))[0];if(top){state.champion=top[0];state.opponent='All matchups';}championChosen=true;}
    if(roster.length)champOptions();
    // Pick the champion's main role on first load; later reloads keep the visitor's role if it has games.
    const role=bestRole(state.champion);if(role&&(firstStats||!coverage().get(state.champion)?.roles.has($('#role').value)))$('#role').value=role;firstStats=false;
  }catch{stats={buckets:[],sources:[],status:'unavailable'};loadError='Cannot load the statistics export. Run npm run build and serve the public folder.';}
  renderSafely();
  await loadChampion(state.champion);
}
async function loadChampion(id){
  const generation=dataGeneration;
  if(!Array.isArray(championCache.get(id))||failedLoads.has(id)){failedLoads.delete(id);
    // Champions without collected games simply have no file.
    try{const buckets=await championBuckets(id);if(generation!==dataGeneration)return;championCache.set(id,buckets);if(typeof guideLoadErrors!=='undefined')guideLoadErrors.delete(id);}catch{if(generation!==dataGeneration)return;championCache.set(id,[]);if(typeof guideLoadErrors!=='undefined'&&coverageRows.some(r=>r.champion===id))guideLoadErrors.set(id,'Could not load this champion’s statistics. Retry to load the collected games.');}
  }
  if(state.champion===id){stats.buckets=championCache.get(id);renderSafely();}
}
async function loadRoster(){
  // Champion list, icons, rune and spell names are self-hosted at build time: the browser never contacts Riot's CDN.
  try{
    const data=await json('ddragon/static.json');
    if(!data.version||!data.champions?.length)throw new Error('No roster');
    roster=data.champions;cdn='ddragon/';runeNames=data.runeNames||{};runeTree=data.runeTree||{};spellNames=data.spellNames||{};completedItems=new Set(data.completedItems||[]);
    if(typeof guideAssets!=='undefined')guideAssets=data;
    champOptions();
    $('#roster-count').textContent='';
    renderSafely();
  }catch{rosterError='Champion data unavailable; reload the page.';$('#roster-count').textContent=rosterError;renderSafely();}
}
function setup(){
  if(document.getElementById?.('guide-root')){initGuide();return;}
  options($('#champ'),['Sett'],'Sett');options($('#opponent'),['All matchups'],'All matchups');
  options($('#patch'),['All collected patches']);options($('#region'),['All collected regions']);$('#rank').closest('.field').hidden=true;
  options($('#role'),[{value:'TOP',label:'Top'},{value:'JUNGLE',label:'Jungle'},{value:'MIDDLE',label:'Mid'},{value:'BOTTOM',label:'Bottom'},{value:'UTILITY',label:'Support'}],'TOP');
  $('#champ').onchange=e=>{championChosen=true;state.champion=e.target.value;state.opponent='All matchups';$('#opponent').value=state.opponent;const role=bestRole(state.champion);if(role)$('#role').value=role;stats.buckets=championCache.get(state.champion)||[];renderSafely();loadChampion(state.champion);};$('#opponent').onchange=e=>{state.opponent=e.target.value;renderSafely();};
  $('#matchups').onclick=e=>{const b=e.target.closest('[data-opp]');if(!b)return;state.opponent=b.dataset.opp;if(![...$('#opponent').options].some(o=>o.value===state.opponent))$('#opponent').add(new Option(state.opponent,state.opponent));$('#opponent').value=state.opponent;renderSafely();};
  $('.filters').onchange=renderSafely;
  $('.tabs').onclick=e=>{const b=e.target.closest('[data-tab]');if(!b)return;state.tab=b.dataset.tab;document.querySelectorAll('.tab').forEach(t=>t.setAttribute('aria-selected',String(t===b)));renderSafely();};
  $('#results').onclick=e=>{const j=e.target.closest('[data-judge]');if(j){state.judge=j.dataset.judge;renderSafely();return;}const b=e.target.closest('[data-sort]');if(!b)return;state.direction=state.sort===b.dataset.sort?-state.direction:-1;state.sort=b.dataset.sort;renderSafely();};
  const method=$('#method');$('#learn').onclick=()=>method.showModal();$('#nav-method').onclick=e=>{e.preventDefault();method.showModal();};method.querySelector('.close').onclick=()=>method.close();method.onclick=e=>{if(e.target===method)method.close();};
  const about=$('#about');$('#nav-about').onclick=e=>{e.preventDefault();about.showModal();};about.querySelector('.close').onclick=()=>about.close();about.onclick=e=>{if(e.target===about)about.close();};
  $('#theme').onclick=()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';};
  renderSafely();loadStats();loadRoster();
}
setup();
