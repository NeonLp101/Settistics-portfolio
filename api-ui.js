(()=>{
  const esc=value=>String(value).replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const api=async path=>{
    const response=await fetch(path,{headers:{Accept:'application/json'}});
    let body={};
    try{body=await response.json()}catch{}
    if(!response.ok){
      const error=new Error(body.message||`Request failed (${response.status})`);
      error.status=response.status;
      error.payload=body;
      throw error;
    }
    return body;
  };

  function addUi(){
    // The page layout has changed before; attach to whichever header exists.
    const actions=document.querySelector('.top-actions')||document.querySelector('.nav');
    const button=document.createElement('button');
    button.className='icon-btn riot-connect';
    button.id='riot-connect';
    button.textContent='Riot API';
    button.setAttribute('aria-label','Open Riot API connection test');
    if(actions.matches('.nav'))actions.append(button);else actions.prepend(button);

    const notice=document.querySelector('.notice');
    const status=document.createElement('span');
    status.className='api-status';
    status.dataset.state='loading';
    status.innerHTML='<span class="api-status-dot"></span><span>Verifying Riot API…</span>';
    // Without a notice bar, the badge gets its own line under the header instead of crowding the nav.
    if(notice)notice.append(status);else{status.style.cssText='display:flex;margin:6px 0';actions.after(status);}

    const dialog=document.createElement('dialog');
    dialog.className='api-dialog';
    dialog.innerHTML=`<div class="modal-head"><h2>Riot API connection</h2><button class="close api-close" aria-label="Close">×</button></div><div class="modal-body"><p>This verifies the server-side key by making a real request to Riot, then lets you test account and match-ID ingestion. The key is never sent to this browser.</p><div class="api-scope"><strong>Riot access</strong><span>Champion roster, account lookup and recent match IDs</span><strong>Separate data pipeline</strong><span>Observed statistics come from the collector/export. WPA stays unavailable pending validation.</span></div><form class="api-form" id="riot-form"><div class="api-field"><label for="riot-name">Game name</label><input id="riot-name" name="gameName" autocomplete="off" placeholder="Hide on bush" required maxlength="40"></div><div class="api-field"><label for="riot-tag">Tag</label><input id="riot-tag" name="tagLine" autocomplete="off" placeholder="EUW" required maxlength="10"></div><div class="api-field region"><label for="riot-region">Platform</label><select id="riot-region" name="platform"><option value="euw1">EUW</option><option value="eun1">EUNE</option><option value="na1">NA</option><option value="kr">KR</option><option value="br1">BR</option><option value="tr1">TR</option><option value="oc1">OCE</option></select></div><button class="api-submit" type="submit">Test</button></form><div class="api-result" id="riot-result">Enter a public Riot ID to resolve its PUUID and retrieve five recent ranked match IDs.</div><p class="api-help">A successful test proves raw Riot ingestion works; it does not populate matchup statistics.</p></div>`;
    document.body.append(dialog);


    button.onclick=()=>dialog.showModal();
    dialog.querySelector('.api-close').onclick=()=>dialog.close();
    dialog.onclick=event=>{if(event.target===dialog)dialog.close()};
    dialog.querySelector('form').onsubmit=submitLookup;
    checkHealth(status);

  }

  async function checkHealth(status){
    try{
      const data=await api('/api/riot/health?platform=euw1');
      if(data.connected){
        status.dataset.state='ready';
        status.lastElementChild.textContent='Riot API verified';
      }else if(data.configured){
        status.dataset.state='error';
        status.lastElementChild.textContent=[401,403].includes(data.upstreamStatus)?'Riot key rejected':'Riot verification unavailable';
      }else{
        status.dataset.state='setup';
        status.lastElementChild.textContent='API key setup needed';
      }
    }catch{
      status.dataset.state='error';
      status.lastElementChild.textContent='API backend unavailable';
    }
  }

  async function submitLookup(event){
    event.preventDefault();
    const form=event.currentTarget;
    const submit=form.querySelector('button');
    const result=document.querySelector('#riot-result');
    const data=new FormData(form);
    const gameName=data.get('gameName').trim();
    const tagLine=data.get('tagLine').trim();
    const platform=data.get('platform');
    submit.disabled=true;
    submit.textContent='Loading…';
    result.textContent='Resolving Riot ID…';
    try{
      const account=await api(`/api/riot/account?gameName=${encodeURIComponent(gameName)}&tagLine=${encodeURIComponent(tagLine)}&platform=${encodeURIComponent(platform)}`);
      result.textContent='Loading recent ranked matches…';
      const recent=await api(`/api/riot/recent?puuid=${encodeURIComponent(account.puuid)}&platform=${encodeURIComponent(platform)}&count=5&queue=420`);
      const short=account.puuid.length>16?`${account.puuid.slice(0,8)}…${account.puuid.slice(-6)}`:account.puuid;
      result.innerHTML=`<div class="api-result-id"><strong>${esc(account.gameName)} #${esc(account.tagLine)}</strong><span>PUUID ${esc(short)}</span></div><div class="api-match-list">${recent.matchIds.length?recent.matchIds.map((id,index)=>`<div class="api-match"><span>${esc(id)}</span><span>ranked match ${index+1}</span></div>`).join(''):'<span>No ranked matches found.</span>'}</div>`;
    }catch(error){
      const hint=error.payload?.setupRequired?'Set RIOT_API_KEY in Netlify environment variables.':error.message;
      result.innerHTML=`<span class="api-error">${esc(hint)}</span>`;
    }finally{
      submit.disabled=false;
      submit.textContent='Test';
    }
  }

  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',addUi);else addUi();
})();
