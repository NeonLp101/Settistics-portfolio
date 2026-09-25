// A visitor pins one immutable aggregate release. State is pregame-only.
(function(root){
  function createDataClient(fetchJson,onFallback=()=>{}){
    let selected=null,candidates=[],initialized=false,indexPromise=null;
    const endpoint=(m,file)=>`/api/live-data?version=${encodeURIComponent(m.version)}&file=${encodeURIComponent(file)}`;
    const valid=m=>m&&m.schemaVersion===1&&/^[0-9]{13}-[a-f0-9-]{36}$/.test(m.version)&&m.files?.['index.json'];
    async function loadIndex(){
      if(!initialized){
        initialized=true;
        try{const p=await fetchJson('/api/live-data');candidates=[p.current,p.previous].filter(valid);}catch{candidates=[];}
      }
      while(candidates.length){
        const candidate=candidates[0];
        try{const data=await fetchJson(endpoint(candidate,'index.json'));if(data.schemaVersion!==2||!Array.isArray(data.coverage))throw Error('Invalid index');selected=candidate;return {...data,dataVersion:candidate.version};}
        catch{candidates.shift();}
      }
      selected=null;return fetchJson('data/index.json');
    }
    function index(){if(!indexPromise)indexPromise=loadIndex().finally(()=>{indexPromise=null;});return indexPromise;}
    async function get(file){
      if(file==='index.json')return index();
      if(!selected)return fetchJson('data/'+file);
      if(!selected.files[file])return {schemaVersion:2,buckets:[]};
      const release=selected;
      try{return await fetchJson(endpoint(release,file));}
      catch(error){if(selected===release){candidates.shift();selected=null;onFallback();}throw error;}
    }
    return {get};
  }
  root.createDataClient=createDataClient;
})(globalThis);
