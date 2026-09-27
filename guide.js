// The live matchup guide. All statistics come from the selected public export.
let guideAssets={};
const guideLoadErrors=new Map();
const guideState={path:null,route:null,slot:'slot2',view:'build',setupFolded:false,setupMore:false};
try{guideState.setupFolded=localStorage.getItem('settistics-setup-folded')==='1';guideState.setupMore=localStorage.getItem('settistics-setup-more')==='1';}catch{}
const GUIDE_MIN=30,GUIDE_TIMING_MIN=15;
const guideSlots=[['slot1','1st item'],['boots','Boots'],['slot2','2nd item'],['slot3','3rd item'],['slot4','4th item'],['slot5','5th item']];
const g=id=>document.getElementById(id);
const guideTime=m=>{const s=Math.round(m*60);return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;};
const guideBootOrder=n=>['Boots first','Boots after 1st','Boots after 2nd','Boots after 3rd','Boots after 4th','Boots after 5th'][n]||'Boots order unknown';
function guideArt(kind,id,label=''){
  id=String(id||'');
  const file=kind==='rune'?guideAssets.runeIcons?.[id]:kind==='spell'?guideAssets.spellIcons?.[id]:kind==='champion'?`champion/${id}.png`:guideAssets.itemIds?.includes(id)||completedItems.has(id)?`item/${id}.png`:null;
  return file?`<img class="game-icon ${esc(kind)}" src="ddragon/${esc(file)}" alt="${esc(label)}" loading="lazy">`:`<span class="icon-fallback" aria-hidden="true">${kind==='rune'?'✧':kind==='spell'?'↗':kind==='champion'?'?':'◇'}</span>`;
}
function guidePaths(buckets){
  const groups=new Map();
  for(const bucket of buckets)for(const p of bucket.paths||[]){if(!groups.has(p.id))groups.set(p.id,[]);groups.get(p.id).push(p);}
  return [...groups].map(([id,list])=>({...merged(list),id,firstItem:list[0].firstItem,bootsBefore:list[0].bootsBefore}))
    .sort((a,b)=>b.games-a.games||a.id.localeCompare(b.id));
}
// ---- Builds: games grouped by their first three finished items, each with its own timings ----
const BUILD_MIN=10;
const guideStepLabel={component:'First buy',slot1:'1st item',boots:'Boots',slot2:'2nd item',slot3:'3rd item',slot4:'4th item',slot5:'5th item'};
function guideBuilds(buckets){
  const out=new Map();
  for(const b of buckets)for(const w of b.builds||[]){
    const m=out.get(w.id)||{id:w.id,items:w.items,names:w.names,games:0,wins:0,timeSum:[0,0,0],components:{},boots:{},routes:w.routes?{}:null};
    m.games+=w.games;m.wins+=w.wins;w.timeSum.forEach((t,i)=>m.timeSum[i]+=t);
    for(const key of ['components','boots'])for(const [k,v] of Object.entries(w[key]||{})){const t=m[key][k]||(m[key][k]={name:v.name,games:0,wins:0,timeSum:0});t.games+=v.games;t.wins+=v.wins;t.timeSum+=v.timeSum;}
    // Same route key = same cohort definition, so summing across patches, regions and opponents stays one cohort.
    // A bucket from an older export without routes makes the merged timings incomplete: drop them rather than mix.
    if(!w.routes)m.routes=null;
    else if(m.routes)for(const [k,v] of Object.entries(w.routes)){const t=m.routes[k]||(m.routes[k]={bootsName:v.bootsName,componentName:v.componentName,games:0,timeSum:[0,0,0],bootsTimeSum:0,componentTimeSum:0});
      t.games+=v.games;v.timeSum.forEach((x,i)=>t.timeSum[i]+=x);t.bootsTimeSum+=v.bootsTimeSum;t.componentTimeSum+=v.componentTimeSum;}
    out.set(w.id,m);
  }
  return [...out.values()];
}
const guideTop=obj=>Object.entries(obj||{}).sort((a,b)=>b[1].games-a[1].games)[0];
// Boots routes of one core: boots item and position (boots first, after the 1st item, ...). Each is its own observed
// route; the first component splits it further. "-" means the players never finished upgraded boots.
function guideBootsRoutes(w){
  const out=new Map();
  for(const [key,v] of Object.entries(w?.routes||{})){
    const [bootsId,before,componentId]=key.split(':'),id=`${bootsId}:${before}`;
    const r=out.get(id)||{id,bootsId,before:Number(before),bootsName:v.bootsName,games:0,timeSum:[0,0,0],bootsTimeSum:0,cohorts:[]};
    r.games+=v.games;v.timeSum.forEach((x,i)=>r.timeSum[i]+=x);r.bootsTimeSum+=v.bootsTimeSum;
    if(componentId!=='-')r.cohorts.push({componentId,componentName:v.componentName,games:v.games,timeSum:v.timeSum,bootsTimeSum:v.bootsTimeSum,componentTimeSum:v.componentTimeSum});
    out.set(id,r);
  }
  return [...out.values()].map(r=>({...r,cohorts:r.cohorts.sort((a,b)=>b.games-a.games||a.componentId.localeCompare(b.componentId))}))
    .sort((a,b)=>b.games-a.games||a.id.localeCompare(b.id));
}
// The build record (all games with the core) and the timing cohort (games on one route) are different samples.
function guideCohortNote(build,planned){
  const t=planned.timing,record=`<span><b>Build record</b> ${pct(build.wins,build.games)} win rate · ${build.games.toLocaleString()} games with these three items</span>`;
  const timing=t.level==='route'?`${t.games.toLocaleString()} games on this route that started with ${esc(t.component)}`
    :t.level==='boots'?`${t.games.toLocaleString()} games on this route · first component varies, too few games to time one (${GUIDE_TIMING_MIN} needed)`
    :t.reason==='noRoutes'?'not available in this data release':`unavailable · ${t.games} game${t.games===1?'':'s'} on this route, ${GUIDE_TIMING_MIN} needed`;
  return `${record}<span><b>Timings</b> ${timing}</span>`;
}
function guideRouteLabel(r){return r.bootsId==='-'?'No boots in these items':`${r.bootsName||guideItemName(r.bootsId)} · ${guideBootOrder(r.before)}`;}
function guideBuildPlan(w,routeId=null){
  // One observed route, every time averaged over the same games. Returns the steps and which cohort timed them.
  const none={steps:[],timing:{level:'none',games:0},route:null};
  if(!w)return none;
  const items=w.items.map((id,i)=>({kind:`slot${i+1}`,id,label:w.names[i],minute:null}));
  const routes=guideBootsRoutes(w),route=routes.find(r=>r.id===routeId)||routes[0]||null;
  const place=(steps,boots,before)=>{if(boots)steps.splice(Math.min(before,steps.length),0,boots);return steps;};
  if(!route){
    // Older export without route cohorts: keep the observed order, but show no times rather than mix averages.
    const top=guideTop(w.boots),[bootsId,before]=top?top[0].split(':'):[];
    return{steps:place(items,top?{kind:'boots',id:bootsId,label:top[1].name,minute:null}:null,Number(before)),timing:{level:'none',games:0,reason:'noRoutes'},route:null,routes};
  }
  const boots=route.bootsId==='-'?null:{kind:'boots',id:route.bootsId,label:route.bootsName||guideItemName(route.bootsId),minute:null};
  const cohort=route.cohorts[0];
  const timed=(c,withComponent)=>{
    const steps=place(items.map((s,i)=>({...s,minute:c.timeSum[i]/c.games})),boots&&{...boots,minute:c.bootsTimeSum/c.games},route.before);
    if(withComponent)steps.unshift({kind:'component',id:c.componentId,label:c.componentName||guideItemName(c.componentId),minute:c.componentTimeSum/c.games});
    return steps.sort((a,b)=>a.minute-b.minute);
  };
  if(cohort&&cohort.games>=GUIDE_TIMING_MIN)return{steps:timed(cohort,true),timing:{level:'route',games:cohort.games,component:cohort.componentName},route,routes};
  if(route.games>=GUIDE_TIMING_MIN)return{steps:timed(route,false),timing:{level:'boots',games:route.games},route,routes};
  return{steps:place(items,boots,route.before),timing:{level:'none',games:route.games,reason:'thin'},route,routes};
}
function guideBuildMarkup(plan){
  if(!plan.length)return '<p class="empty-copy">No games with three finished items for these filters yet.</p>';
  return `<svg class="build-route" aria-hidden="true"><path class="route-shadow"/><path class="route-line"/><path class="route-flow"/></svg>`+
    plan.map((s,i)=>`<button type="button" class="build-step${s.kind==='component'?' part':''}" data-item-kind="${s.kind}" data-item-id="${esc(s.id)}" aria-label="Step ${i+1} of ${plan.length}, ${esc(s.label)}${s.minute==null?'':` at ${guideTime(s.minute)}`}: open statistics"><span class="build-order">${String(i+1).padStart(2,'0')}</span><span class="eyebrow">${guideStepLabel[s.kind]}</span>${guideArt('item',s.id)}<b>${esc(s.label)}</b><span class="when${s.minute==null?' untimed':''}">${s.minute==null?'—':guideTime(s.minute)}</span></button>`).join('');
}
let guidePathTimer=null,guidePathFrame=null,guidePathBend=0;
function guideDrawBuildPath(bend=0){
  const box=g('build'),svg=box?.querySelector('.build-route'),cards=[...(box?.querySelectorAll('.build-step')||[])];
  if(!svg||cards.length<2)return;
  const root=box.getBoundingClientRect(),width=Math.max(1,box.clientWidth),height=Math.max(1,box.clientHeight);
  svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
  const rects=cards.map(card=>{const r=card.getBoundingClientRect();return{x:r.left-root.left,y:r.top-root.top,w:r.width,h:r.height};});
  const parts=[];
  for(let i=0;i<rects.length-1;i++){
    const a=rects[i],b=rects[i+1],horizontal=b.x>a.x+a.w/2;
    if(horizontal){
      const x1=a.x+a.w,y1=a.y+a.h/2,x2=b.x,y2=b.y+b.h/2,mid=(x1+x2)/2;
      parts.push(`M ${x1.toFixed(1)} ${y1.toFixed(1)} C ${(mid+bend).toFixed(1)} ${y1.toFixed(1)}, ${(mid-bend).toFixed(1)} ${y2.toFixed(1)}, ${x2.toFixed(1)} ${y2.toFixed(1)}`);
    }else{
      const x1=a.x+a.w/2,y1=a.y+a.h,x2=b.x+b.w/2,y2=b.y,mid=(y1+y2)/2;
      parts.push(`M ${x1.toFixed(1)} ${y1.toFixed(1)} C ${x1.toFixed(1)} ${(mid+bend).toFixed(1)}, ${x2.toFixed(1)} ${(mid-bend).toFixed(1)}, ${x2.toFixed(1)} ${y2.toFixed(1)}`);
    }
  }
  for(const path of svg.querySelectorAll('path'))path.setAttribute('d',parts.join(' '));
}
function guideStartBuildPath(){
  clearInterval(guidePathTimer);cancelAnimationFrame(guidePathFrame);guidePathTimer=null;
  if((g('build')?.querySelectorAll('.build-step').length||0)<2)return;
  guidePathBend=0;requestAnimationFrame(()=>guideDrawBuildPath(0));
  if(window.matchMedia('(prefers-reduced-motion: reduce)').matches)return;
  let direction=1;
  guidePathTimer=setInterval(()=>{
    if(document.hidden)return;
    const from=guidePathBend,to=direction*8,start=performance.now();direction*=-1;
    const tick=now=>{const t=Math.min(1,(now-start)/500),ease=t*t*(3-2*t);guideDrawBuildPath(from+(to-from)*ease);if(t<1)guidePathFrame=requestAnimationFrame(tick);else guidePathBend=to;};
    guidePathFrame=requestAnimationFrame(tick);
  },500);
}
// A build's win rate counts as if it also had 400 average games. Real differences between builds are a few points
// at most (see the reliability audit), so a build needs hundreds of games before its own record moves it far.
const BUILD_PRIOR=400,BUILD_EDGE=.01;
function guideAdjusted(w,base){return (w.wins+BUILD_PRIOR*base)/(w.games+BUILD_PRIOR);}
function guideRankBuilds(builds){
  const games=builds.reduce((a,w)=>a+w.games,0),base=games?builds.reduce((a,w)=>a+w.wins,0)/games:.5;
  for(const w of builds)w.adjusted=guideAdjusted(w,base);
  const eligible=builds.filter(w=>w.games>=BUILD_MIN).sort((a,b)=>b.adjusted-a.adjusted||b.games-a.games);
  const popular=[...builds].sort((a,b)=>b.games-a.games)[0]||null;
    // No build credibly ahead (by at least a point after adjustment): recommend the most played, it has the most evidence.
  const top=eligible[0],best=top&&popular&&top!==popular&&popular.games>=BUILD_MIN&&top.adjusted-popular.adjusted<BUILD_EDGE?popular:top||popular;
  return{eligible,popular,best,base};
}
function guideBuildName(w){return w.names.join(' › ');}
function guideBase(){const previous=state.opponent;try{state.opponent='All matchups';return selection();}finally{state.opponent=previous;}}
function guideItemName(id){return guideAssets.itemNames?.[id]||`Item ${id}`;}
function guidePathLabel(p){return `${guideItemName(p.firstItem)} · ${guideBootOrder(p.bootsBefore)}`;}
function guideComparisonSample(paths,path,summary){
  // The route fixes the first item. Compare first-item alternatives across routes
  // with the same boots order rather than presenting a meaningless one-item ranking.
  if(guideState.slot!=='slot1'||!path)return summary;
  return merged(paths.filter(p=>p.bootsBefore===path.bootsBefore).map(p=>({...p,choices:[...p.choices.values()]})));
}
// Per-item adjusted associations appear only after the split-half stability check.
function guideGate(metric,kind){return stats.reliability?.[metric]?.[kind]||null;}
function guidePassed(metric,kind){return !!guideGate(metric,kind)?.pass;}
function guidePooled(metric,kind,id){
  const t=stats.pooledEffects?.[metric]?.[$('#role').value]?.[kind]?.[id];if(!t)return null;
  const [n,s1,s2]=t,m=s1/n,v=Math.max(0,s2/n-m*m),k=metric==='wpa'?100:1;
  return{m:m*k,h:1.96*Math.sqrt(v/n)*k,n};
}
function guideModelScore(c,ref){
  if(!stats.wpaModel||!guidePassed('wpa',c.kind))return null;
  return wpaOf(c,ref);
}
function guideRowIcon(c){
  if(!c)return '<span class="icon-fallback" aria-hidden="true">◇</span>';
  if(c.kind==='keystone')return guideArt('rune',c.id);
  if(c.kind==='spells')return c.id.split('+').map(id=>guideArt('spell',id)).join('');
  if(c.kind==='packages')return c.id.split('+').map(i=>guideArt('item',i.split('x')[0])).join('');
  return guideArt('item',c.id);
}
// Skill order is common knowledge, so it only appears when this matchup's usual max order differs from the champion's.
function guideSkillShift(summary,base){
  const max=topChoice(summary,'skillMax'),usual=base&&topChoice(base,'skillMax');
  if(!max||!usual||max.id===usual.id||max.games<GUIDE_MIN)return null;
  return{max,usual};
}
// The first skill point is the part of skill order that changes by matchup (e.g. W instead of E into a lane bully).
function guideFirstPoint(summary){
  const counts=new Map();let total=0;
  for(const c of rowsOf(summary,'skillStart').rows){const k=c.id.split('>')[0];counts.set(k,(counts.get(k)||0)+c.games);total+=c.games;}
  const [key,games]=[...counts].sort((a,b)=>b[1]-a[1])[0]||[];
  return key?{key,games,total}:null;
}
function guideSetupSummary(summary){
  const page=topChoice(summary,'runes'),spells=topChoice(summary,'spells'),start=topChoice(summary,'packages'),first=guideFirstPoint(summary);
  const perks=page?.id.split('|')[0].split(',')||[],key=perks.find(id=>runeTree[id]?.[1]===0)||perks[0];
  const part=(art,text)=>`<span class="sum-part">${art}<span>${esc(text)}</span></span>`;
  return [page&&part(guideArt('rune',key),runeNames[key]||'Keystone'),spells&&part(`<span class="sum-icons">${guideRowIcon(spells)}</span>`,nameChoice(spells)),
    start&&part(`<span class="sum-icons">${guideRowIcon(start)}</span>`,start.label),first&&part(`<span class="key">${esc(first.key)}</span>`,'Level 1')].filter(Boolean).join('')||'<span class="muted">No setup data yet.</span>';
}
function guideSetup(summary,base){
  const page=topChoice(summary,'runes'),spells=topChoice(summary,'spells'),start=topChoice(summary,'packages');
  const shift=guideSkillShift(summary,base),first=guideFirstPoint(summary),usualFirst=base&&guideFirstPoint(base);
  const firstLine=first?`<p class="first-point"><span class="key">${esc(first.key)}</span>Level 1 · ${pct(first.games,first.total)} of ${first.total} games${usualFirst&&usualFirst.key!==first.key&&first.games>=GUIDE_TIMING_MIN?` <b class="shift">usually ${esc(usualFirst.key)}</b>`:''}</p>`:'';
  const perks=page?.id.split('|')[0].split(',')||[];
  const key=perks.find(id=>runeTree[id]?.[1]===0)||perks[0];
  const chosen=perks.filter(id=>id!==key).sort((a,b)=>(runeTree[a]?.[0]||0)-(runeTree[b]?.[0]||0)||(runeTree[a]?.[1]||0)-(runeTree[b]?.[1]||0));
  const share=(c,kind)=>c?`${pct(c.games,rowsOf(summary,kind).total)} · ${c.games} games`:'No observations yet';
  // The page is shown the way the client lays it out: keystone, then each tree with its picks. Always visible; it is only five runes.
  const ks=rowsOf(summary,'keystone'),ksRow=ks.rows.find(c=>c.id===key);
  const trees=[...new Set(chosen.map(id=>runeTree[id]?.[0]).filter(Boolean))];
  const treeRow=t=>`<div class="rune-tree">${guideArt('rune',t)}<span class="tree-name">${esc(runeNames[t]||'')}</span><div class="rune-chips">${chosen.filter(id=>runeTree[id]?.[0]===t).map(id=>`<span>${guideArt('rune',id)}${esc(runeNames[id]||id)}</span>`).join('')}</div></div>`;
  // Stat shards (offense, flex, defense), e.g. Adaptive Force, Attack Speed, Health.
  const shardMap=Object.fromEntries((page?.id.split('|')[1]||'').split(',').filter(Boolean).map(x=>x.split(':')));
  const shards=['offense','flex','defense'].map(k=>shardMap[k]).filter(Boolean);
  const shardRow=shards.length?`<div class="rune-tree shards"><span class="shard-mark" aria-hidden="true">◆</span><span class="tree-name">Shards</span><div class="rune-chips">${shards.map(id=>`<span>${guideArt('rune',id)}${esc(guideAssets.shardNames?.[id]||`Shard ${id}`)}</span>`).join('')}</div></div>`:'';
  const otherKeys=ks.rows.filter(c=>c.id!==key).slice(0,4).length?`<ul class="setup-alts"><li class="alts-head">Other keystones</li>${ks.rows.filter(c=>c.id!==key).slice(0,4).map(c=>`<li><span class="alt-art">${guideArt('rune',c.id)}</span><span>${esc(runeNames[c.id]||c.id)}</span><b>${pct(c.games,ks.total)}</b></li>`).join('')}</ul>`:'';
  const runeBody=page?`<div class="setup-pick">${guideArt('rune',key)}<div><strong>${esc(runeNames[key]||'Keystone')}</strong><p>${ksRow?`${pct(ksRow.games,ks.total)} take it · `:''}this exact page in ${pct(page.games,rowsOf(summary,'runes').total)}</p></div></div><div class="rune-page">${trees.map(treeRow).join('')}${shardRow}</div>${guideState.setupMore?otherKeys:''}<button type="button" class="setup-more" data-view="runes">All rune pages →</button>`:'<p>No rune pages collected.</p>';
  // Runner-up choices, so a cell answers "what else do people take?"
  const alts=(kind,label)=>{const r=rowsOf(summary,kind);const rest=r.rows.slice(1,guideState.setupMore?6:3);return rest.length?`<ul class="setup-alts">${rest.map(c=>`<li><span class="alt-art">${guideRowIcon(c)}</span><span>${esc(label(c))}</span><b>${pct(c.games,r.total)}</b></li>`).join('')}</ul>`:'';};
  const cell=(title,body)=>`<div><div class="eyebrow">${title}</div>${body}</div>`;
  return cell('Runes',runeBody)+cell('Summoner spells',spells?`<div class="setup-pick"><div class="spell-art">${guideRowIcon(spells)}</div><strong>${esc(nameChoice(spells))}</strong></div><p>${share(spells,'spells')}</p>${alts('spells',nameChoice)}<button type="button" class="setup-more" data-view="spells">All spell pairs →</button>`:'<p>No spell pairs collected.</p>')+
    cell('Start',start?`<div class="start"><div class="start-art">${guideRowIcon(start)}</div><strong>${esc(start.label)}</strong></div><p>${share(start,'packages')}</p>${alts('packages',c=>c.label)}${firstLine}`:(firstLine||'<p>No eligible starts recorded.</p>'))+
    (shift?cell('Skill order changes',`<div class="keys">${shift.max.id.split('>').map(k=>`<span class="key">${esc(k)}</span>`).join('<span class="muted">›</span>')}</div><p>Usually ${esc(shift.usual.id.split('>').join(' › '))} · here ${share(shift.max,'skillMax')}</p>`):'');
}
function guideBuildSteps(summary,path){
  const used=new Set(),steps=[];
  for(let i=1;i<=5;i++){
    const kind=`slot${i}`,choice=rowsOf(summary,kind).rows.find(c=>!used.has(c.id));
    if(choice){used.add(choice.id);steps.push({kind,c:choice});}
  }
  const boots=topChoice(summary,'boots');
  const before=path?.bootsBefore??Number(topChoice(summary,'bootsTiming')?.id||0);
  if(boots)steps.splice(Math.min(before,steps.length),0,{kind:'boots',c:boots});
  return steps;
}
function guideStep(s,summary,timingAllowed){
  const label=guideSlots.find(([k])=>k===s.kind)[1],n=s.c.timeCount||0;
  const time=timingAllowed&&n>=GUIDE_TIMING_MIN?guideTime(minutes(s.c)):null;
  return `<button type="button" class="build-step" data-guide-slot="${s.kind}" data-item-kind="${s.kind}" data-item-id="${esc(s.c.id)}" aria-pressed="${guideState.slot===s.kind}" aria-label="Compare ${label}: ${esc(s.c.label)}"><span class="eyebrow">${label}</span>${guideArt('item',s.c.id)}<b>${esc(s.c.label)}</b><span class="when">${time||`${s.c.games} buys`}</span><span class="share">${pct(s.c.games,rowsOf(summary,s.kind).total)} of this slot</span></button>`;
}
function guideOpponent(sourceId){
  if(state.opponent==='All matchups')return {summary:null,paths:[],builds:[],loading:false};
  const id=state.opponent;
  if(!championCache.has(id)){
    const generation=dataGeneration;
    championCache.set(id,null);
    json(`data/champions/${encodeURIComponent(id)}.json`).then(d=>{if(generation!==dataGeneration)return;championCache.set(id,d.buckets||[]);renderSafely();}).catch(()=>{if(generation!==dataGeneration)return;championCache.set(id,[]);renderSafely();});
  }
  const buckets=championCache.get(id);
  if(buckets===null)return {summary:null,paths:[],builds:[],loading:true};
  const list=(buckets||[]).filter(b=>b.champion===id&&b.opponent===state.champion&&b.role===$('#role').value&&
    (b.sourceId||'riot-match-v5')===sourceId&&($('#patch').value==='All collected patches'||b.patch===$('#patch').value)&&
    ($('#region').value==='All collected regions'||b.region===$('#region').value));
  return {summary:merged(list),paths:guidePaths(list),builds:guideBuilds(list),loading:false};
}
function guideTimeline(steps,theirs,thin,timing={level:'route'}){
  g('timing-legend').innerHTML=`<span>${esc(champion(state.champion).name)}</span>${theirs.length?`<span>${esc(champion(state.opponent).name)}</span>`:''}`;
  const empty=(title,text)=>`<div class="empty-timing"><h3>${title}</h3><p>${text}</p></div>`;
  if(!steps.length)return empty('No complete builds yet','Timings appear once games with three finished items are collected for this selection.');
  if(timing.level==='none')return empty('Timing unavailable for this route',timing.reason==='noRoutes'?'This data release has no per-route timings yet. Item order is shown above; times appear after the next data update.'
    :`Only ${timing.games} game${timing.games===1?'':'s'} followed this exact route; timings need ${GUIDE_TIMING_MIN}. Choose a more common boots order, or widen the patch or region filters.`);
  const max=Math.max(20,...steps.concat(theirs).map(s=>s.minute)),end=Math.ceil((max+2)/5)*5,x=m=>m/end*100;
  const events=(list,enemy)=>{let previous=-100,tier=0;return [...list].sort((a,b)=>a.minute-b.minute).map(s=>{
    const pos=x(s.minute);tier=pos-previous<15?1-tier:0;previous=pos;
    const attrs=enemy?'':`data-item-kind="${s.kind}" data-item-id="${esc(s.id)}" aria-label="${esc(s.label)} at ${guideTime(s.minute)}: open statistics"`;
    return `<${enemy?'div':'button type="button"'} class="live-event ${enemy?'enemy':''} tier-${tier}${s.kind==='component'?' part':''}" style="left:${pos}%" ${attrs}>${guideArt('item',s.id)}<b>${guideTime(s.minute)}</b><small>${esc(s.label)}</small></${enemy?'div':'button'}>`;
  }).join('');};
  const mine=steps.find(s=>s.kind==='slot1'),their=theirs.find(s=>s.kind==='slot1');
  let note=thin?'General champion sample: too few games in this matchup for its own timings.':'Average times in the games that followed this route, not recommended purchase times. Click an item for its full statistics.';
  if(mine&&their){const d=mine.minute-their.minute;note=`Your ${mine.label} lands ${guideTime(Math.abs(d))} ${d<0?'before':'after'} their ${their.label}. Click an item for its full statistics.`;}
  return `<div class="chart-scroll" tabindex="0" role="region" aria-label="Item timings of the selected build"><div class="live-chart ${theirs.length?'two-sides':''}"><div class="live-rail"></div>${events(steps,false)}${theirs.length?`<div class="live-rail enemy"></div>${events(theirs,true)}`:''}${Array.from({length:end/5+1},(_,i)=>`<span class="tick" style="left:${x(i*5)}%">${i*5}′</span>`).join('')}</div></div><div class="timing-summary"><b>${esc(note)}</b></div>`;
}
function guideComparison(summary,allowed,shownId){
  const {rows,total,ref}=rowsOf(summary,guideState.slot);
  if(!rows.length)return '<p class="empty-copy">No purchases recorded for this slot.</p>';
  return rows.slice(0,5).map((c,i)=>{
    const score=allowed?guideModelScore(c,ref):null,positive=score&&score.m-score.h>0,negative=score&&score.m+score.h<0;
    const clamp=v=>Math.max(0,Math.min(100,50+v*4));
    const bar=score?`<div class="scale" aria-hidden="true"><i style="left:${clamp(Math.min(0,score.m))}%;width:${Math.abs(clamp(score.m)-50)}%;background:${positive?'var(--green)':negative?'var(--neg)':'var(--faint)'}"></i><i class="ci" style="left:${clamp(score.m-score.h)}%;width:${clamp(score.m+score.h)-clamp(score.m-score.h)}%"></i></div>`:'';
    return `<button type="button" class="choice${c.id===shownId?' selected':''}" data-item-kind="${guideState.slot}" data-item-id="${esc(c.id)}"><div class="choice-title">${guideArt('item',c.id)}<div><b>${esc(c.label)}</b><small>${pct(c.games,total)} · ${c.games} buys · ${pct(c.wins,c.games)} WR</small></div></div><div class="score ${positive?'good':'muted'}"><span>${score?fmtPp(score.m):'—'}</span><small>${score?`adjusted association · ±${score.h.toFixed(1)} · ${score.n} scored`:guidePassed('wpa',guideState.slot)?'not enough scored games':guideGate('wpa',guideState.slot)?'adjusted result: not reproducible yet':'adjusted result: unavailable'}</small></div>${bar}</button>`;
  }).join('');
}
// ---- Win chance after buying: minute by minute, relative to the average choice in the same slot ----
function guideCurve(rows,c){
  if(!c?.curveN)return null;
  const withCurve=rows.filter(r=>r.curveN),n=withCurve.reduce((a,r)=>a+r.curveN,0),K=c.curveSum.length;
  return Array.from({length:K},(_,i)=>{
    const ref=withCurve.reduce((a,r)=>a+r.curveSum[i],0)/n,m=c.curveSum[i]/c.curveN,v=Math.max(0,c.curveSq[i]/c.curveN-m*m);
    return{minute:i+1,m:(m-ref)*100,h:1.96*Math.sqrt(v/c.curveN)*100,raw:m*100,rawRef:ref*100,n:c.curveN};
  });
}
function guideCurveChart(shown,slot){
  if(!shown.length)return'';
  const W=600,H=170,pad=28,top=Math.max(4,...shown.map(p=>Math.abs(p.m)+p.h)),x=m=>pad+m/10*(W-2*pad),y=v=>H/2-v/top*(H/2-14);
  const band=shown.map(p=>`${x(p.minute)},${y(p.m+p.h)}`).join(' ')+' '+[...shown].reverse().map(p=>`${x(p.minute)},${y(p.m-p.h)}`).join(' ');
  const line=[`${x(0)},${y(0)}`,...shown.map(p=>`${x(p.minute)},${y(p.m)}`)].join(' ');
  return`<svg class="im-curve" viewBox="0 0 ${W} ${H}" role="img" aria-label="Observed win-probability trajectory versus the average choice, minute by minute after buying">
    <line x1="${pad}" x2="${W-pad}" y1="${y(0)}" y2="${y(0)}" class="zero"/><polygon points="${band}" class="band"/><polyline points="${line}" class="line"/>
    ${shown.map(p=>`<circle cx="${x(p.minute)}" cy="${y(p.m)}" r="3.5" class="${p.m-p.h>0?'pos':p.m+p.h<0?'neg':'flat'}"><title>${p.minute} min: ${fmtPp(p.m)} ±${p.h.toFixed(1)}</title></circle>`).join('')}
    ${[0,2,4,6,8,10].map(m=>`<text x="${x(m)}" y="${H-2}" class="tick">${m}′</text>`).join('')}
    <text x="${W-pad}" y="12" class="tick end">vs average ${esc(slot.toLowerCase())}</text></svg>`;
}
function guideCurvePooled(kind,id){
  const t=stats.pooledEffects?.curve?.[$('#role').value]?.[kind]?.[id];if(!t)return null;
  const [n,sums,sqs]=t;
  return sums.map((s1,i)=>{const m=s1/n,v=Math.max(0,sqs[i]/n-m*m);return{minute:i+1,m:m*100,h:1.96*Math.sqrt(v/n)*100,n};});
}
function guideCurveSection(rows,c,kind,name,slot){
  const pts=guideCurve(rows,c);
  if(!pts)return kind==='component'?'':`<section class="im-sec"><h3>Observed game trajectory</h3><p class="im-note">No scored purchases in this slot yet.</p></section>`;
  const gate=k=>stats.reliability?.curve?.[kind]?.[String(k)],shown=pts.filter(p=>gate(p.minute)?.pass);
  const five=pts[Math.min(4,pts.length-1)];
  const plain=`<p>Buyers' win chance changed by <b>${five.raw>=0?'+':'−'}${Math.abs(five.raw).toFixed(1)} pp</b> in the 5 minutes after finishing ${esc(name)}, versus ${five.rawRef>=0?'+':'−'}${Math.abs(five.rawRef).toFixed(1)} pp for the average ${esc(slot.toLowerCase())} (${c.curveN.toLocaleString()} games).</p>`;
  const chart=guideCurveChart(shown,slot);
  // Not ready for this champion: fall back to everyone in the role who buys it, if that passes its own check.
  const pooledPts=guideCurvePooled(kind,c.id),pooledGate=k=>stats.reliability?.curvePooled?.[kind]?.[String(k)];
  const pooledShown=!shown.length&&pooledPts?pooledPts.filter(p=>pooledGate(p.minute)?.pass):[];
  const role=($('#role').selectedOptions?.[0]?.textContent||'').toLowerCase();
  const pooledBlock=pooledShown.length?`<p class="im-pooled">Across <b>all ${esc(role)} players</b> who buy ${esc(name)} as ${esc(slot.toLowerCase())} (${pooledPts[0].n.toLocaleString()} games), compared with other ${esc(slot.toLowerCase())}s. This pooled curve passes our reproducibility check.</p>${guideCurveChart(pooledShown,slot)}<p class="im-note">Reproducible does not have to mean caused by the item: players may choose it in particular situations.</p>`:'';
  const g5=gate(5),need=g5?.neededGames||0;
  const status=shown.length===pts.length?'':shown.length?(m=>`<p class="im-note">${m.length===1?`Minute ${m[0]} is`:`Minutes ${m.join(', ')} are`} not reproducible yet.</p>`)(pts.filter(p=>!gate(p.minute)?.pass).map(p=>p.minute))
    :`<div class="im-measure"><p><b>Not reproducible yet.</b> This curve uses ${c.curveN.toLocaleString()} games, but the item rankings disagree between two halves of the data.</p><p class="im-note">${g5?`Split-half check at 5 minutes: r = ${g5.splitHalfR.toFixed(2)} (needs 0.40). `:''}${need?`About ${need.toLocaleString()} games per item would give ±2 pp precision; that alone would not guarantee agreement.`:''}</p></div>`;
  return `<section class="im-sec"><h3>Observed game trajectory</h3>${plain}<p class="im-note">The following minutes also include fights, objectives, later purchases and player decisions. This curve does not isolate the item's effect.</p>${chart}${pooledBlock}${pooledBlock?`<p class="im-note">For this champion alone, the curve has not passed the reproducibility check (${c.curveN.toLocaleString()} games).</p>`:status}<details><summary>How this is measured</summary><p>For every buyer we take the model's win chance just before the purchase and again each minute for the next 10 minutes. The chart compares buyers of ${esc(name)} with the average ${esc(slot.toLowerCase())}. If a game ends within 10 minutes, its result counts from that minute on. The estimate is observational and may reflect why players chose the item.</p></details></section>`;
}
// ---- Item pop-up: everything we know about one item in one slot ----
function guidePreChance(rows,c){
  if(!c||(c.curveN||0)<GUIDE_MIN)return null;
  const n=rows.reduce((a,r)=>a+(r.curveN||0),0);
  return{mine:c.preSum/c.curveN,slot:rows.reduce((a,r)=>a+(r.preSum||0),0)/n,n:c.curveN};
}
function guideLaneRef(summary,kind){const rows=rowsOf(summary,kind).rows.filter(c=>c.laneN);const n=rows.reduce((a,c)=>a+c.laneN,0);return n?rows.reduce((a,c)=>a+c.laneSum,0)/n:0;}
function guideItemDetail(kind,id){
  const ctx=guideState.ctx;if(!ctx)return'';
  const {rows,total,ref}=rowsOf(ctx.summary,kind),c=rows.find(r=>r.id===id);
  const name=c?.label||guideItemName(id),slot=guideStepLabel[kind]||'Item';
  const head=`<header class="im-head">${guideArt('item',id,name)}<div><div class="eyebrow">${esc(slot)} · ${esc(ctx.you)} into ${esc(ctx.them)}</div><h2 id="item-modal-title">${esc(name)}</h2></div><button type="button" class="im-close" data-close-modal aria-label="Close">×</button></header>`;
  if(!c)return head+`<p class="im-empty">No purchases of ${esc(name)} as ${esc(slot.toLowerCase())} in this selection.</p>`;
  const slotGames=rows.reduce((a,r)=>a+r.games,0),slotWins=rows.reduce((a,r)=>a+r.wins,0),ci=wilson(c.wins,c.games);
  const w=ctx.allowed?guideModelScore(c,ref):null,m=stats.wpaModel,gate=guideGate('wpa',kind),poolGate=guideGate('wpaPooled',kind),pool=guidePooled('wpa',kind,id);
  const tile=(label,value,sub)=>`<div class="im-tile"><div class="eyebrow">${label}</div><b>${value}</b><small>${sub}</small></div>`;
  // The model's win chance just before buying: says whether an item is bought when ahead or behind. Descriptive, not a ranking.
  const pre=guidePreChance(rows,c),state=pre&&(pre.mine-pre.slot>=.02?'usually bought when ahead':pre.mine-pre.slot<=-.02?'usually bought when behind':'bought at a typical game state');
  const tiles=tile('Bought',pct(c.games,total),`${c.games} of ${total} games in this slot`)+
    tile('Win rate',pct(c.wins,c.games),`${ci?`likely ${ci[0].toFixed(0)}–${ci[1].toFixed(0)}% · `:''}slot average ${pct(slotWins,slotGames)}`)+
    (kind==='component'?'':tile('Win chance when bought',pre?pct(pre.mine,1):'—',pre?`${state} · slot average ${pct(pre.slot,1)}`:`needs ${GUIDE_MIN} scored games`))+
    tile(kind==='component'?'Bought at':'Finished at',c.timeCount?guideTime(c.timeSum/c.timeCount):'—',c.timeCount?`average of ${c.timeCount} buyers`:'no timing recorded')+
    (kind==='component'?'':tile('Adjusted result',w?fmtPp(w.m):gate&&!gate.pass?'Not reproducible':'Not enough data',w?`±${w.h.toFixed(1)} pp · ${w.n} scored games`:gate&&!gate.pass?`split-half r = ${gate.splitHalfR.toFixed(2)}; needs 0.40`:`${(c.residN||0).toLocaleString()} scored games`));
  // The graph: bar from zero to the estimate, thin line for the 95% range, same scale as the comparison panel.
  const X=v=>Math.max(0,Math.min(100,50+v*5)),cls=w?(w.m-w.h>0?'pos':w.m+w.h<0?'neg':'flat'):'flat';
  const graph=w?`<div class="im-graph ${cls}" aria-hidden="true"><div class="im-track"><i class="bar" style="left:${X(Math.min(0,w.m))}%;width:${Math.abs(X(w.m)-50)}%"></i><i class="ci" style="left:${X(w.m-w.h)}%;width:${X(w.m+w.h)-X(w.m-w.h)}%"></i></div><div class="im-axis"><span>−10 pp</span><span>average choice</span><span>+10 pp</span></div></div>`:'';
  const verdict=!w?'':cls==='pos'?`Buyers won more often than this model predicted.`:cls==='neg'?`Buyers won less often than this model predicted.`:`The range crosses zero: this model cannot distinguish these buyers from the slot average.`;
  // Components are not scored by the model; say so instead of showing an empty graph.
  const have=c.residN||0,need=gate?.neededGames||0;
  const pooledLine=pool&&poolGate?.pass?`<p class="im-pooled"><b>${fmtPp(pool.m)}</b> ±${pool.h.toFixed(1)} pp across all ${esc(($('#role').selectedOptions?.[0]?.textContent||'').toLowerCase())} players who buy ${esc(name)} as ${esc(slot.toLowerCase())} (${pool.n.toLocaleString()} games). This pooled estimate passes our reproducibility check.</p>`
    :pool?`<p class="im-note">Pooled across all players in this role: ${pool.n.toLocaleString()} games, also not reproducible yet.</p>`:'';
  const missing=kind==='component'?'Adjusted results are scored for finished items and boots. Components have no score of their own.'
    :!gate?'No scored purchases in this slot yet.'
    :gate.pass?`<p>This item has ${have.toLocaleString()} scored games, too few for an individual estimate.</p>`
    :`<p><b>Not reproducible yet.</b> This item has ${have.toLocaleString()} scored games. The item rankings for ${esc(slot.toLowerCase())}s disagree between two halves of the data (r = ${gate.splitHalfR.toFixed(2)}; needs 0.40).</p><p class="im-note">${need?`About ${need.toLocaleString()} games per item would give ±2 pp precision, but more games alone may not make the rankings agree. `:''}With item labels shuffled, ${(gate.placebo*100).toFixed(1)}% of items still looked significant, versus ${(gate.flagged*100).toFixed(1)}% for real. Showing an adjusted score now would be misleading.</p>${pooledLine}`;
  const meaning=w?`<b>${fmtPp(w.m)}</b> means buyers of ${esc(name)} here won ${Math.abs(w.m).toFixed(1)} percentage points ${w.m<0?'less':'more'} often than the game state predicted, compared with the average ${esc(slot.toLowerCase())}.`:'For example, <b>+2 pp</b> would mean buyers won 2 percentage points more often than the game state predicted, compared with the average choice in the same slot.';
  const impact=kind==='component'?`<section class="im-sec"><h3>Adjusted whole-game result</h3><p class="im-note">Components are not scored. The adjusted result and the win chance when bought apply once an item is finished: open the finished item to see them.</p></section>`:`<section class="im-sec"><h3>Adjusted whole-game result</h3>${w?graph:`<div class="im-measure">${missing.startsWith('<')?missing:`<p>${missing}</p>`}</div>`}${verdict?`<p class="im-verdict ${cls}">${verdict}</p>`:''}
    <details ${w?'open':''}><summary>How the pp is calculated</summary><p>Just before every purchase, our model estimates the buyer's chance to win from recorded gold and level leads, objectives, side and other available pre-purchase context. We then compare how often buyers actually won with that estimate. ${meaning} This adjusts for measured game state, but players also choose items for reasons the model cannot see. It does not identify the item's causal impact.</p></details>
    <details><summary>How to read the graph</summary><p>The bar starts at zero, the average choice for this slot. The thin line is an approximate 95% interval for this adjusted association, assuming these games are independent. A line above or below zero is evidence of a difference among buyers, not proof the item helps or hurts. Wide lines mean few games.</p></details>
    ${m?`<p class="im-note">Model: ${Number(m.games).toLocaleString()} games, accuracy (AUC) ${m.auc.toFixed(2)}, predictions within ${m.calibrationErrorPp.toFixed(1)} pp of what happened.</p>`:''}</section>`;
  // 1v1 lane: only items bought while laning are measured.
  let lane;
  if(c.laneN){const l=laneOf(c,guideLaneRef(ctx.summary,kind)),up=c.laneUp||0;
    lane=`<section class="im-sec"><h3>In the 1v1 lane</h3><div class="im-lane"><div><b>${pct(up,c.laneN)}</b><small>of ${c.laneN} clean lanes: buyer's gold lead grew</small></div><div><b>${gold(c.laneDelta/c.laneN)}</b><small>gold vs the lane opponent, 5 min after buying</small></div><div><b>${l?gold(l.m):guideGate('lane',kind)&&!guidePassed('lane',kind)?'Not reproducible':'Not enough data'}</b><small>${l?`vs the average choice · ±${Math.round(l.h)}`:guidePassed('lane',kind)?`needs ${LANE_MIN} clean lanes`:'vs the average choice: split-half check failed'}</small></div></div>
      <p class="im-note">Counted only while the lane stays a true 1v1, until the first gank or another champion keeps showing up.</p></section>`;}
  else lane=`<section class="im-sec"><h3>In the 1v1 lane</h3><p class="im-note">${['slot1','boots'].includes(kind)?'No clean 1v1 lanes recorded for this purchase yet.':'Lane result is measured for the first item and boots only, because later items are bought after laning ends.'}</p></section>`;
  // Same item across all matchups, when a specific opponent is selected.
  let across='';
  if(ctx.base){const b=rowsOf(ctx.base,kind),o=b.rows.find(r=>r.id===id);
    if(o)across=`<section class="im-sec"><h3>Against ${esc(ctx.them)} vs all matchups</h3><div class="im-lane"><div><b>${pct(c.games,total)}</b><small>bought here</small></div><div><b>${pct(o.games,b.total)}</b><small>bought across all matchups</small></div><div><b>${pct(o.wins,o.games)}</b><small>win rate across all matchups</small></div></div></section>`;}
  const alts=rows.filter(r=>r.id!==id).slice(0,4).map(r=>{const s2=ctx.allowed?guideModelScore(r,ref):null;return`<button type="button" class="im-alt" data-item-kind="${kind}" data-item-id="${esc(r.id)}">${guideArt('item',r.id)}<span>${esc(r.label)}</span><small>${pct(r.games,total)} · ${pct(r.wins,r.games)} WR${s2?` · ${fmtPp(s2.m)}`:''}</small></button>`;}).join('');
  return head+`<div class="im-tiles" style="--tiles:${kind==='component'?3:5}">${tiles}</div>`+guideCurveSection(rows,c,kind,name,slot)+impact+lane+across+(alts?`<section class="im-sec"><h3>Other ${esc(slot.toLowerCase())} choices</h3><div class="im-alts">${alts}</div></section>`:'');
}
// ---- Counters: the selected champion's win rate against each lane opponent, relative to its usual win rate ----
// Small samples are pulled toward the usual win rate before ranking, so a lucky 8-game streak cannot top the list.
const COUNTER_MIN=15,COUNTER_PRIOR=30,COUNTER_SHOW=10;
function guideMatchups(buckets){
  const byOpp=new Map();let games=0,wins=0;
  for(const b of buckets){games+=b.games;wins+=b.wins;if(!b.opponent||b.opponent==='All matchups')continue;if(!byOpp.has(b.opponent))byOpp.set(b.opponent,[]);byOpp.get(b.opponent).push(b);}
  const usual=games?wins/games:0;
  const rows=[...byOpp].map(([id,list])=>{const s=merged(list),adj=(s.wins+COUNTER_PRIOR*usual)/(s.games+COUNTER_PRIOR),ci=wilson(s.wins,s.games);
    return{id,games:s.games,wins:s.wins,wr:s.wins/s.games*100,delta:(adj-usual)*100,ci,clear:!!ci&&(ci[0]>usual*100||ci[1]<usual*100),lane:laneDuel(s)};})
    .filter(m=>m.games>=COUNTER_MIN);
  return{usual:usual*100,games,rows,counters:rows.filter(m=>m.delta>0).sort((a,b)=>b.delta-a.delta||b.games-a.games).slice(0,COUNTER_SHOW),
    countered:rows.filter(m=>m.delta<0).sort((a,b)=>a.delta-b.delta||b.games-a.games).slice(0,COUNTER_SHOW),thin:[...byOpp.values()].filter(l=>l.reduce((n,b)=>n+b.games,0)<COUNTER_MIN).length};
}
function guideCounterView(kind){
  const m=guideMatchups(guideBase().buckets),you=esc(champion(state.champion).name),good=kind==='counters';
  const list=good?m.counters:m.countered,role=$('#role').selectedOptions?.[0]?.textContent||$('#role').value;
  const title=good?`${you} counters`:`${you} is countered by`;
  const lede=good?`Lane opponents ${you} beats more often than usual. Pick ${you} into these.`:`Lane opponents ${you} loses to more often than usual. Consider another pick, or study the matchup guide.`;
  const lane=r=>r.lane&&r.lane.n>=LANE_MIN?`<span class="counter-lane ${r.lane.gold>=0?'pos':'neg'}" title="Average gold lead in lane until the first gank, over ${r.lane.n} clean duels">${gold(r.lane.gold)} gold in lane</span>`:'';
  const row=(r,i)=>{const name=champion(r.id).name,w=Math.min(100,Math.abs(r.delta)/10*100);
    return`<li><button type="button" class="counter-row" data-counter-opp="${esc(r.id)}" aria-label="${esc(name)}: ${r.wr.toFixed(1)}% win rate over ${r.games} games. Open the matchup guide."><span class="counter-rank">${i+1}</span>${guideArt('champion',r.id,'')}<span class="counter-name"><b>${esc(name)}</b><small>${r.games.toLocaleString()} games${r.ci?` · ${r.ci[0].toFixed(0)}–${r.ci[1].toFixed(0)}% interval`:''}</small></span><span class="counter-wr"><b>${r.wr.toFixed(1)}%</b><small>${fmtPp(r.delta)} vs usual${r.clear?'':' · not yet clear'}</small><i class="counter-bar ${good?'pos':'neg'}" aria-hidden="true"><i style="width:${w.toFixed(0)}%"></i></i></span>${lane(r)}</button></li>`;};
  const empty=!m.games?'No collected games for this champion, role, patch and region yet.':m.rows.length?`No lane opponent ${good?'is beaten':'wins'} more often than usual yet.`:`No lane opponent has ${COUNTER_MIN} or more games yet. Try All patches and All regions.`;
  return`<section class="card counters"><div class="card-head"><div><h2>${title}</h2><p>${lede} ${esc(role)} · ${you}'s usual win rate ${m.games?m.usual.toFixed(1)+'%':'—'} over ${m.games.toLocaleString()} games. Ranked by win rate after pulling small samples toward the usual rate; opponents need ${COUNTER_MIN} games${m.thin?` (${m.thin} with fewer not shown)`:''}. “Not yet clear” means the 95% interval still includes the usual rate. Observed results, not a cause: who picks into whom, and when, also matters.</p></div></div>${list.length?`<ol class="counter-list">${list.map(row).join('')}</ol>`:`<p class="panel-note">${empty}</p>`}</section>`;
}
function guideOpenItem(kind,id){
  const d=g('item-modal');if(!d)return;
  g('item-modal-body').innerHTML=guideItemDetail(kind,id).replaceAll(' loading="lazy"','');
  if(!d.open)d.showModal();
  d.scrollTop=0;
}
// A fun fact, not a recommendation: how often this champion is part of the game's first kill and first tower.
const FACT_MIN=100;
function guideFact(sel,you,role,roleName){
  const f=sel.firsts,r=stats.firstObjectives?.[role];
  if(!f||f.games<FACT_MIN)return '';
  const rate=(n,d)=>`${Math.round(100*n/d)}%`,blood=o=>o.firstBloodKill,tower=o=>o.firstTowerKill+o.firstTowerAssist;
  const avg=get=>r?.games?` (${esc(roleName.toLowerCase())} average ${rate(get(r),r.games)})`:'';
  return `Fun fact: ${esc(you)} draws first blood in ${rate(blood(f),f.games)} of these games${avg(blood)} and helps take the first tower in ${rate(tower(f),f.games)}${avg(tower)}.`;
}
function renderGuide(){
  if(!g('guide-root'))return;
  const chosen=selection(),sel=merged(chosen.buckets),baseSelection=guideBase(),base=merged(baseSelection.buckets);
  const thin=state.opponent!=='All matchups'&&sel.games<GUIDE_MIN&&base.games>0;
  const active=thin?baseSelection:chosen,summary=thin?base:sel,paths=guidePaths(active.buckets);
  const builds=guideBuilds(active.buckets),rank=guideRankBuilds(builds);
  if(!builds.some(w=>w.id===guideState.build))guideState.build=rank.popular?.id||null;
  const build=builds.find(w=>w.id===guideState.build)||null,planned=guideBuildPlan(build,guideState.route),plan=planned.steps,timing=planned.timing;
  const path=null,buildSummary=summary,steps=guideBuildSteps(summary,null);
  const opposite=guideOpponent(chosen.source?.id||active.source?.id||'riot-match-v5');
  const you=champion(state.champion).name,them=state.opponent==='All matchups'?'All matchups':champion(state.opponent).name;
  g('guide-title').innerHTML=`${esc(you)} <span>into</span> ${esc(them)}`;
  g('guide-portraits').innerHTML=guideArt('champion',state.champion,you)+(state.opponent==='All matchups'?'<span class="all-opponents" aria-hidden="true">?</span>':guideArt('champion',state.opponent,them));
  g('guide-role').textContent=`The matchup guide · ${$('#role').selectedOptions?.[0]?.textContent||$('#role').value}`;
  g('guide-subtitle').textContent=`${$('#patch').selectedOptions?.[0]?.textContent||'All patches'} · ${$('#region').selectedOptions?.[0]?.textContent||'All regions'}`;
  const fact=guideFact(sel,you,$('#role').value,$('#role').selectedOptions?.[0]?.textContent||$('#role').value);
  g('guide-fact').hidden=!fact;g('guide-fact').innerHTML=fact;
  const ci=wilson(sel.wins,sel.games),wr=sel.games?sel.wins/sel.games*100:null;
  const delta=wr!=null&&base.games&&sel.games>=GUIDE_MIN&&chosen.source?.id===baseSelection.source?.id&&state.opponent!=='All matchups'?wr-base.wins/base.games*100:null;
  const lane=laneDuel(sel);
  const laneStat=lane&&lane.n>=15?`<div class="lane-stat"><div class="big-number">${gold(lane.gold)}</div><small>gold lead in lane, until the first gank
    <details class="info"><summary aria-label="How lane gold is measured">i</summary><p>Measured only while the lane is a true 1v1: from minute 1 until the first gank kill or assist, or until another champion keeps showing up. ${lane.n} lanes stayed clean long enough to count. Games where the jungler arrived early only count up to that point.</p></details></small></div>`:'';
  g('guide-metrics').innerHTML=`<div><div class="big-number">${pct(sel.wins,sel.games)}</div><small>observed win rate${ci?` · ${ci[0].toFixed(0)}–${ci[1].toFixed(0)}% interval`:''}</small><div class="delta">${delta!=null?`${fmtPp(delta)} vs usual`:sel.games?trust(sel.games):'No matching observations'}</div></div>${laneStat}<div><div class="big-number">${sel.games.toLocaleString()}</div><small>games in this selection</small></div>`;
  g('data-status').textContent=loadError||`${Number(stats.uniqueMatches||0).toLocaleString()} collected matches`;
  renderChips();
  const notices=[];
  if(loadError)notices.push(`${esc(loadError)} <button type="button" data-reload>Retry data</button>`);
  else if(guideLoadErrors.has(state.champion))notices.push(`${esc(guideLoadErrors.get(state.champion))} <button type="button" data-reload>Retry data</button>`);
  else if(stats.status==='loading')notices.push('Loading collected statistics…');
  else if(thin)notices.push(`<strong>${sel.games} games against ${esc(them)}.</strong> Showing ${esc(you)}’s general setup and routes from ${base.games} games. Matchup timing and adjusted comparisons need more evidence.`);
  else if(!sel.games)notices.push(`No collected matches for these filters. Try another matchup or region. <button type="button" data-reset>Show all matchups and regions</button>`);
  else if(sel.games<GUIDE_MIN)notices.push(`${sel.games} games in this selection. Treat these common choices as an early sample.`);
  if(stats.itemDataMissing?.length)notices.push(`Item data is unavailable for patch ${stats.itemDataMissing.map(patchLabel).map(esc).join(', ')}. Runes and spells remain available.`);
  const provisional=Object.keys(stats.itemDataProvisional||{});
  if(provisional.length)notices.push(`Item definitions are provisional for patch ${provisional.map(patchLabel).map(esc).join(', ')}; some new items may be missing.`);
  g('guide-notice').hidden=!notices.length;g('guide-notice').innerHTML=notices.join('<br>');
  g('setup-source').textContent=`${thin?'All matchups':them} · ${summary.games} games · most common choices`;
  const setupBase=!thin&&state.opponent!=='All matchups'&&chosen.source?.id===baseSelection.source?.id?base:null;
  g('guide-setup').innerHTML=guideSetup(summary,setupBase);
  g('setup-summary').innerHTML=guideSetupSummary(summary);
  g('guide-setup').hidden=guideState.setupFolded;g('setup-summary').hidden=!guideState.setupFolded;
  g('setup-fold').setAttribute('aria-expanded',String(!guideState.setupFolded));
  g('setup-more').hidden=guideState.setupFolded;g('setup-more').setAttribute('aria-expanded',String(guideState.setupMore));
  g('setup-more').textContent=guideState.setupMore?'Fewer options':'More options';
  g('guide-setup').classList.toggle('three',!guideSkillShift(summary,setupBase));
  // Show the most observed complete build first. Win rate alone cannot establish the best treatment.
  const allowed=!thin&&active.source?.type==='riot_match_timelines';
  guideState.ctx={summary,base:setupBase,allowed,you,them:thin?'All matchups':them};
  const buildCi=build?wilson(build.wins,build.games):null;
  g('build-eyebrow').textContent=!build?'Builds':build===rank.popular?'Suggested route · prototype':'Selected observed build';
  g('path-badge').textContent=build?`${pct(build.wins,build.games)} win rate · ${build.games} games`:`${summary.eligible.build} eligible builds`;
  const pick=[rank.popular,...rank.eligible.filter(w=>w!==rank.popular)].filter(Boolean).slice(0,4);
  g('path-tabs').hidden=pick.length<2;
  g('path-tabs').innerHTML=pick.map(w=>`<button type="button" data-guide-build="${esc(w.id)}" aria-pressed="${w===build}"><span class="bt-tag">${w===rank.popular?'Most played':w===rank.best?'Higher observed WR':'Alternative'}</span><span class="bt-icons">${w.items.map(id=>guideArt('item',id)).join('')}</span><b>${pct(w.wins,w.games)}</b><span>${w.games} games</span></button>`).join('');
  const routeOptions=(planned.routes||[]).slice(0,4);
  g('route-tabs').hidden=!routeOptions.length;
  g('route-tabs').innerHTML=routeOptions.length?`<span class="route-label" id="route-label">Boots route</span><div class="route-options" role="group" aria-labelledby="route-label">${routeOptions.map(r=>`<button type="button" data-guide-route="${esc(r.id)}" aria-pressed="${r===planned.route}">${r.bootsId==='-'?'':guideArt('item',r.bootsId)}<span><b>${esc(guideRouteLabel(r))}</b><small>${r.games.toLocaleString()} game${r.games===1?'':'s'}</small></span></button>`).join('')}</div>`:'';
  g('build').style.setProperty('--path-count',String(Math.max(1,plan.length)));
  g('build').innerHTML=guideBuildMarkup(plan);guideStartBuildPath();
  // "Why this route?": only facts the export knows (counts, coverage, timing). No invented winner.
  const totalBuilds=builds.reduce((a,w)=>a+w.games,0),firstItem=plan.find(s=>s.kind==='slot1'),why=[];
  if(build){
    why.push(build===rank.popular?`Most played complete build: <b>${build.games}</b> of ${totalBuilds} games with three finished items (${pct(build.games,totalBuilds)}).`:`An alternative you selected: <b>${build.games}</b> of ${totalBuilds} games with three finished items (${pct(build.games,totalBuilds)}).`);
    why.push(thin?`Only ${sel.games} games against ${esc(them)}, too few for a matchup route. This is ${esc(you)}’s usual route across all matchups (${base.games} games).`
      :state.opponent==='All matchups'?`Across all matchups: ${summary.games} games.`:`${sel.games} games against ${esc(them)}: enough for a route of its own.`);
    why.push(rank.best&&rank.best!==rank.popular?`${esc(guideBuildName(rank.best))} has a higher observed win rate (${pct(rank.best.wins,rank.best.games)} over ${rank.best.games} games), even after allowing for its smaller sample. It is listed as an alternative, not as proven better.`
      :'No other route is ahead by a point once sample size is taken into account, so the most played one is suggested.');
    if(firstItem&&firstItem.minute!=null)why.push(`First item finished at ${guideTime(firstItem.minute)} on average in the ${timing.games} games that followed this route.`);
    why.push(`${pct(build.wins,build.games)} observed win rate${buildCi?` (likely ${buildCi[0].toFixed(0)}–${buildCi[1].toFixed(0)}%)`:''}.`);
  }
  g('build-hint').innerHTML=build?`<div class="why"><h3>Why this route?</h3><ul>${why.map(x=>`<li>${x}</li>`).join('')}</ul><p>Prototype suggestion: an observed route, not an optimized one. The three finished items were played together. Boots-first and item-first are separate routes: pick one above, and every time shown comes only from the games that followed it. Click an item for details. A win rate does not prove the build caused wins.</p></div>`:'';
  const theirBuild=guideRankBuilds(opposite.builds||[]).popular;
  const theirPlan=thin?null:guideBuildPlan(theirBuild),theirs=theirPlan&&theirPlan.timing.level!=='none'?theirPlan.steps:[];
  g('timing-subtitle').textContent=build?`${thin?'All matchups (too few games in this matchup) · ':''}${guideBuildName(build)}${planned.route?` · ${guideRouteLabel(planned.route)}`:''}`:'';
  g('route-cohort').innerHTML=build?guideCohortNote(build,planned):'';
  g('guide-timeline').innerHTML=guideTimeline(plan,theirs,thin,timing);
  g('guide-later').innerHTML=steps.filter(s=>['slot4','slot5'].includes(s.kind)).map(s=>guideStep(s,buildSummary,!thin&&!!path)).join('')+coreList(buildSummary)||'<p class="empty-copy">No later builds observed yet.</p>';
  const slotLabel=guideSlots.find(([k])=>k===guideState.slot)?.[1]||'2nd item';
  g('compare-title').textContent=`Compare ${slotLabel.toLowerCase()}`;
  g('compare-context').textContent=`${path?(guideState.slot==='slot1'?`${guideBootOrder(path.bootsBefore)} · all first-item choices`:guidePathLabel(path)):'All observed purchases'}${thin?' · general champion sample':''}`;
  g('compare-tabs').innerHTML=guideSlots.map(([k,label])=>`<button type="button" data-guide-slot="${k}" aria-pressed="${guideState.slot===k}">${label}</button>`).join('');
  g('choices').innerHTML=guideComparison(guideComparisonSample(paths,path,buildSummary),!thin&&active.source?.type==='riot_match_timelines',steps.find(s=>s.kind===guideState.slot)?.c.id);
  const theirItem=opposite.summary&&topChoice(opposite.summary,'slot1');
  g('opponent-note').hidden=state.opponent==='All matchups';
  g('opponent-note').innerHTML=`<div class="eyebrow">Across the lane</div><h3>${esc(them)}’s common first item</h3>${theirItem?`${guideArt('item',theirItem.id)}<p>${esc(theirItem.label)} · ${theirItem.games} purchases</p>`:`<p>${opposite.loading?'Loading…':'No matching opponent item observations.'}</p>`}`;
  const updated=active.source?.generatedAt||stats.generatedAt;
  g('guide-provenance').innerHTML=`<div><h3>${esc(active.source?.name||'No matching source')}</h3><p>${summary.games} champion-game observations in the displayed setup. ${updated?`Source updated ${esc(new Date(updated).toLocaleString())}.`:''} ${esc(active.source?.note||'')}</p><p>Matching local Riot data takes precedence. Imported providers are never added together. ${thin?'The setup uses all matchups; the header reports only your selected opponent.':''}</p></div><div><h3>Model coverage</h3><p>Data release: ${esc(stats.dataVersion||'bundled export')}.</p><p>${stats.wpaModel?`Outcome model trained on ${Number(stats.wpaModel.games).toLocaleString()} games. Its adjusted associations use scored purchases and remain observational.`:'No trained outcome model is available. Descriptive purchase counts still appear.'}</p><p>Timing needs ${GUIDE_TIMING_MIN} purchases per item and slot. No observations means unavailable, never a zero effect.</p></div>`;
  g('guide-build-view').hidden=guideState.view!=='build';g('guide-detail').hidden=guideState.view==='build';
  document.querySelectorAll('[data-view]').forEach(b=>{b.setAttribute('aria-selected',String(b.dataset.view===guideState.view));b.tabIndex=b.dataset.view===guideState.view?0:-1;});
  if(guideState.view!=='build'){
    g('guide-detail').setAttribute('aria-labelledby',`view-${guideState.view}`);
    g('guide-detail').innerHTML=guideState.view==='lane'?laneView(sel):['counters','countered'].includes(guideState.view)?guideCounterView(guideState.view):table(guideState.view,sel);
  }
}
function initGuide(){
  // Direct links such as ?champion=Kaisa&vs=Jhin open one matchup (used for the demo walkthrough).
  try{const q=new URLSearchParams(location.search),id=/^[A-Za-z]{2,20}$/;
    if(id.test(q.get('champion')||'')){state.champion=q.get('champion');championChosen=true;if(id.test(q.get('vs')||''))state.opponent=q.get('vs');}}catch{}
  window.addEventListener('resize',()=>guideDrawBuildPath(guidePathBend));
  options($('#champ'),['Sett'],'Sett');options($('#opponent'),['All matchups'],'All matchups');
  options($('#patch'),['All collected patches']);options($('#region'),['All collected regions']);
  options($('#role'),[{value:'TOP',label:'Top'},{value:'JUNGLE',label:'Jungle'},{value:'MIDDLE',label:'Mid'},{value:'BOTTOM',label:'Bottom'},{value:'UTILITY',label:'Support'}],'TOP');
  $('#champ').onchange=e=>{championChosen=true;state.champion=e.target.value;state.opponent='All matchups';$('#opponent').value=state.opponent;guideState.path=null;const role=bestRole(state.champion);if(role)$('#role').value=role;stats.buckets=championCache.get(state.champion)||[];renderGuide();loadChampion(state.champion);};
  $('#opponent').onchange=e=>{state.opponent=e.target.value;guideState.path=null;renderGuide();};
  document.querySelector('.filters').onchange=()=>{guideState.path=null;renderGuide();};
  g('guide-root').addEventListener('change',e=>{if(e.target.id==='more-routes'&&e.target.value){guideState.path=e.target.value;renderGuide();g('more-routes')?.focus();}});
  // Clicking the dimmed backdrop (the dialog element itself) closes the pop-up.
  // Theme switch: Pit (default) or Void, remembered per browser.
  const themeBtn=g('theme-toggle'),themeLabel=()=>{if(themeBtn)themeBtn.lastChild.textContent=document.documentElement.dataset.palette==='void'?'Pit theme':'Void theme';};
  themeLabel();
  themeBtn?.addEventListener('click',()=>{const next=document.documentElement.dataset.palette==='void'?'':'void';
    if(next)document.documentElement.dataset.palette=next;else delete document.documentElement.dataset.palette;
    try{localStorage.setItem('settistics-theme',next||'pit');}catch{}themeLabel();});
  g('item-modal')?.addEventListener('click',e=>{if(e.target===g('item-modal'))g('item-modal').close();});
  g('guide-root').addEventListener('click',e=>{
    const path=e.target.closest('[data-guide-path]'),slot=e.target.closest('[data-guide-slot]'),opp=e.target.closest('[data-opp]'),view=e.target.closest('[data-view]'),sort=e.target.closest('[data-sort]');
    const build=e.target.closest('[data-guide-build]'),route=e.target.closest('[data-guide-route]'),item=e.target.closest('[data-item-kind]'),counter=e.target.closest('[data-counter-opp]');
    if(counter){state.opponent=counter.dataset.counterOpp;if(![...$('#opponent').options].some(o=>o.value===state.opponent))$('#opponent').add(new Option(champion(state.opponent).name,state.opponent));$('#opponent').value=state.opponent;guideState.path=null;guideState.view='build';renderGuide();g('view-build').focus({preventScroll:true});window.scrollTo({top:0,behavior:window.matchMedia('(prefers-reduced-motion:reduce)').matches?'instant':'smooth'});return;}
    if(e.target.closest('[data-close-modal]')){g('item-modal').close();return;}
    if(e.target.closest('#setup-fold,#setup-summary')){guideState.setupFolded=!guideState.setupFolded;try{localStorage.setItem('settistics-setup-folded',guideState.setupFolded?'1':'0');}catch{}renderGuide();return;}
    if(e.target.closest('#setup-more')){guideState.setupMore=!guideState.setupMore;try{localStorage.setItem('settistics-setup-more',guideState.setupMore?'1':'0');}catch{}renderGuide();return;}
    if(route){guideState.route=route.dataset.guideRoute;renderGuide();document.querySelector(`[data-guide-route="${CSS.escape(guideState.route)}"]`)?.focus();return;}
    if(build){guideState.build=build.dataset.guideBuild;guideState.route=null;renderGuide();document.querySelector(`[data-guide-build="${CSS.escape(guideState.build)}"]`)?.focus();return;}
    if(item){guideOpenItem(item.dataset.itemKind,item.dataset.itemId);if(!item.closest('.choices,.later-build,#item-modal'))return;}
    if(path){guideState.path=path.dataset.guidePath;renderGuide();document.querySelector(`[data-guide-path="${CSS.escape(guideState.path)}"]`)?.focus();}
    if(slot){const origin=e.target.closest('.build,.compare-tabs,.live-chart,.later-build');guideState.slot=slot.dataset.guideSlot;renderGuide();const narrow=window.matchMedia('(max-width:900px)').matches;(origin?.classList.contains('build')&&!narrow?g('build'):g('compare-tabs')).querySelector(`[data-guide-slot="${guideState.slot}"]`)?.focus({preventScroll:true});if(narrow&&!origin?.classList.contains('compare-tabs'))document.querySelector('.comparison').scrollIntoView({block:'start',behavior:window.matchMedia('(prefers-reduced-motion:reduce)').matches?'instant':'smooth'});}
    if(opp){state.opponent=opp.dataset.opp;$('#opponent').value=state.opponent;guideState.path=null;renderGuide();}
    if(view){guideState.view=view.dataset.view;renderGuide();}
    if(sort){state.direction=state.sort===sort.dataset.sort?-state.direction:-1;state.sort=sort.dataset.sort;renderGuide();}
    if(e.target.closest('[data-reload]')){championCache.delete(state.champion);guideLoadErrors.delete(state.champion);loadStats();}
    if(e.target.closest('[data-reset]')){state.opponent='All matchups';$('#opponent').value=state.opponent;$('#region').value='All collected regions';$('#patch').value='All collected patches';guideState.path=null;renderGuide();}
    if(e.target.closest('#method-link,#nav-method'))g('evidence').open=true;
  });
  document.querySelector('.view-tabs').addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const views=['build','counters','countered','lane','runes','spells'],n=views.length,i=views.indexOf(guideState.view);guideState.view=views[e.key==='Home'?0:e.key==='End'?n-1:(i+(e.key==='ArrowRight'?1:n-1))%n];renderGuide();g(`view-${guideState.view}`).focus();});
  const about=g('about');g('nav-about').onclick=e=>{e.preventDefault();about.showModal();};about.querySelector('.close').onclick=()=>about.close();about.onclick=e=>{if(e.target===about)about.close();};
  // Keep a broken third-party artwork export from leaving an inaccessible image.
  g('guide-root').addEventListener('error',e=>{if(e.target.tagName==='IMG'){const fallback=document.createElement('span');fallback.className='icon-fallback';fallback.textContent=e.target.alt||'◇';e.target.replaceWith(fallback);}},true);
  renderGuide();loadStats();loadRoster();
}
