'use strict';
// Secrets live encrypted in the local service; this page holds a working copy.
window.APIProfiles=(()=>{
  let presets=null,promise=null,selected=null,revision=0,loaded=false;
  const configured=new Map();
  const savedProviders=new Set();
  const catalogs=new Map();
  async function catalog(config,{force=false}={}){
    const base_url=(config.base_url||'').trim(),api_key=(config.api_key||'').trim();
    if(!base_url||!api_key)throw new Error('先填写 API 地址和 API Key。');
    const key=JSON.stringify([config.provider||'',base_url,api_key]);
    let entry=catalogs.get(key);
    if(entry?.pending)return (await entry.pending).map(item=>({...item}));
    if(!force&&entry?.models&&Date.now()-entry.time<300000)return entry.models.map(item=>({...item}));
    entry={};catalogs.set(key,entry);
    if(catalogs.size>8)catalogs.delete(catalogs.keys().next().value);
    entry.pending=(async()=>{
      let response;
      try{response=await fetch('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:{base_url,api_key}}),cache:'no-store'});}
      catch{throw new Error('无法连接本机服务，请稍后重新获取。');}
      let body={};try{body=await response.json();}catch{}
      if(!response.ok)throw new Error(body.error||'模型列表获取失败，可重试或手动填写。');
      if(!Array.isArray(body.models))throw new Error('没能读取模型列表，可以重试或手动填写模型 ID。');
      entry.models=body.models.filter(m=>m&&typeof m.id==='string'&&m.id.trim()).map(m=>({id:m.id,name:typeof m.name==='string'?m.name:m.id}));
      entry.time=Date.now();return entry.models;
    })();
    try{return (await entry.pending).map(item=>({...item}));}finally{entry.pending=null;}
  }
  async function requestSettings(options){
    let response;try{response=await fetch('/api/model-settings',{cache:'no-store',...options});}
    catch{throw new Error('无法读取本机模型设置，请确认服务仍在运行。');}
    let body={};try{body=await response.json();}catch{}
    if(!response.ok)throw new Error(body.error||'本机模型设置无法读取，请重试。');
    if(!Number.isInteger(body.revision)||!Array.isArray(body.profiles))throw new Error('模型设置读取出错，请重新打开设置。');
    return body;
  }
  function hydrate(body){
    configured.clear();savedProviders.clear();revision=body.revision;
    for(const p of body.profiles){configured.set(p.provider,{base_url:p.base_url,api_key:p.api_key,model:p.model});savedProviders.add(p.provider);}
    selected=configured.has(body.active_provider)?body.active_provider:null;loaded=true;
    window.dispatchEvent(new Event('api-profiles-changed'));
  }
  async function load({reload=false}={}){
    if(presets&&loaded&&!reload)return presets;
    if(!promise)promise=(async()=>{
      if(!presets){
        const response=await fetch('/api/model-presets',{cache:'no-store'});
        const body=await response.json();if(!response.ok||!Array.isArray(body.providers))throw new Error('模型预设暂时无法读取。');
        presets=body.providers;
      }
      hydrate(await requestSettings());return presets;
    })().finally(()=>{promise=null;});
    return promise;
  }
  async function save(provider,config){
    hydrate(await requestSettings({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:revision,action:'save',provider,config})}));
  }
  async function remove(provider){
    hydrate(await requestSettings({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:revision,action:'remove',provider})}));
    catalogs.clear();
  }
  function get(provider){
    const preset=(presets||[]).find(p=>p.id===provider)||{};
    return {...{base_url:preset.base_url||'',model:preset.model||'',api_key:''},...(configured.get(provider)||{})};
  }
  function active(){return selected?{provider:selected,config:{...configured.get(selected)}}:null;}
  function use(provider,config){
    if(!(presets||[]).some(p=>p.id===provider))provider='自定义兼容接口';
    const value={base_url:config.base_url||'',api_key:config.api_key||'',model:config.model||''};
    configured.set(provider,value);selected=provider;
    window.dispatchEvent(new Event('api-profiles-changed'));
  }
  return {load,get,active,use,catalog,save,remove,hasSaved:provider=>savedProviders.has(provider)};
})();

// Reusable model selector; directory cache remains page-local.
window.ModelPicker=(()=>{
  function mount(host,input,{base,key,provider}){
    const make=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text)e.textContent=text;return e;};
    const root=make('div','api-model-picker'),row=make('div','api-model-row');
    const select=make('select'),fetchButton=make('button','api-model-fetch','获取模型');
    const search=make('input','api-model-search');search.type='search';search.placeholder='搜索模型';search.setAttribute('aria-label','搜索模型');
    const foot=make('div','api-model-foot'),status=make('span','api-model-status');status.setAttribute('role','status');
    const manualButton=make('button','api-model-manual','手动填写');
    select.setAttribute('aria-label','模型');input.setAttribute('aria-label','模型 ID');
    input.placeholder='填写服务商提供的模型 ID';fetchButton.type=manualButton.type='button';
    row.append(select,input,fetchButton);foot.append(status,manualButton);root.append(search,row,foot);host.append(root);
    let models=[],manual=false,serial=0,timer=null,disposed=false,desired=input.value,loading=false;
    const config=()=>({provider:provider.value,base_url:base.value.trim(),api_key:key.value.trim()});
    function message(text,error=false){status.textContent=text;status.classList.toggle('is-error',error);}
    function mode(){input.hidden=!manual;input.required=manual;select.hidden=manual;select.required=!manual;manualButton.textContent=manual?'从列表选择':'手动填写';search.hidden=manual||models.length<16;}
    function options(){
      const selected=input.value,filter=search.value.trim().toLowerCase();select.replaceChildren();
      const placeholder=make('option','',loading?'正在获取模型…':models.length?'请选择模型':key.value.trim()?'点击获取模型':'填入 API Key 后获取模型');placeholder.value='';select.append(placeholder);
      for(const m of models){if(filter&&m.id!==selected&&!(m.name+' '+m.id).toLowerCase().includes(filter))continue;
        const option=make('option','',m.id);option.value=m.id;select.append(option);}
      select.value=models.some(m=>m.id===selected)?selected:'';select.disabled=loading||!models.length;
    }
    async function fetchModels(force=false){
      clearTimeout(timer);const value=config();if(!value.base_url||!value.api_key){message('填写 API Key 后自动获取');return;}
      const ticket=++serial;loading=true;fetchButton.disabled=true;fetchButton.textContent='获取中…';message('正在读取模型列表…');options();
      try{
        const result=await window.APIProfiles.catalog(value,{force});if(disposed||ticket!==serial)return;
        models=result;loading=false;
        if(!manual)input.value=models.some(m=>m.id===desired)?desired:'';
        message(models.length?'已获取 '+models.length+' 个模型':'没有取得模型列表，可以手动填写模型 ID。');
        options();mode();
      }catch(error){if(disposed||ticket!==serial)return;loading=false;models=[];if(!manual)input.value='';message(error.message,true);options();mode();}
      finally{if(!disposed&&ticket===serial){loading=false;fetchButton.disabled=false;fetchButton.textContent=models.length?'刷新模型':'重新获取';}}
    }
    function invalidate(){
      serial++;clearTimeout(timer);models=[];search.value='';loading=false;fetchButton.disabled=false;fetchButton.textContent='获取模型';
      desired=input.value||desired;if(!manual)input.value='';message('填写 API Key 后自动获取');options();mode();
    }
    function reset(){desired=input.value;manual=false;invalidate();if(base.value.trim()&&key.value.trim())fetchModels();}
    function schedule(){clearTimeout(timer);if(base.value.trim()&&key.value.trim())timer=setTimeout(()=>fetchModels(),100);}
    base.addEventListener('input',invalidate);key.addEventListener('input',invalidate);
    base.addEventListener('change',schedule);key.addEventListener('change',schedule);
    select.onchange=()=>{input.value=select.value;desired=select.value;};
    input.addEventListener('input',()=>{desired=input.value;});search.oninput=options;
    fetchButton.onclick=()=>fetchModels(true);
    manualButton.onclick=()=>{manual=!manual;if(manual)input.value=desired;else input.value=models.some(m=>m.id===desired)?desired:'';options();mode();if(manual)input.focus();else select.focus();};
    reset();
    return {element:root,reset,validate(){if(!input.value.trim()){message(manual?'请填写模型 ID。':'请选择模型，或切换为手动填写。',true);(manual?input:select).focus();return false;}return true;},destroy(){disposed=true;serial++;clearTimeout(timer);base.removeEventListener('input',invalidate);key.removeEventListener('input',invalidate);base.removeEventListener('change',schedule);key.removeEventListener('change',schedule);}};
  }
  return {mount};
})();

// The review flow uses the same saved profiles and catalog cache as discussions.
window.ReviewModelSelector=(()=>{
  const catalogs=new Map();let lastChoice=null;
  const catalogKey=provider=>{const config=window.APIProfiles.get(provider);return JSON.stringify([provider,config.base_url,config.api_key]);};
  function mount(host,engines,onChange=()=>{}){
    const make=(tag,text='')=>{const n=document.createElement(tag);n.textContent=text;return n;};
    const select=make('select');select.name='review_model';select.setAttribute('aria-label','模型服务');
    const foot=make('div');foot.className='review-model-foot';const status=make('span'),settings=make('button','模型设置');status.setAttribute('role','status');settings.type='button';settings.className='review-model-settings';settings.onclick=()=>window.ModelSettings.open();foot.append(status,settings);host.append(select,foot);
    let providers=[],choice=lastChoice,ticket=0,notifiedFingerprint;
    const encode=(provider,model)=>JSON.stringify([provider,model]);
    function value(){if(select.value==='codex_cli')return {engine_key:'codex_cli',config:{}};try{const [provider,model]=JSON.parse(select.value);return {engine_key:'compatible_api',provider,config:{...APIProfiles.get(provider),model}};}catch{return null;}}
    function notifyChange(){const current=value();const fingerprint=JSON.stringify(current?[current.engine_key,current.provider||'',current.config.base_url||'',current.config.api_key||'',current.config.model||'']:null);if(fingerprint!==notifiedFingerprint){notifiedFingerprint=fingerprint;onChange();}}

    function render(){
      const desired=select.value||choice;select.replaceChildren();
      if(engines.some(engine=>engine.available&&engine.key==='compatible_api'))for(const provider of providers){
        const config=APIProfiles.get(provider.id),group=make('optgroup');group.label=provider.label||provider.id;
        const models=[...(catalogs.get(catalogKey(provider.id))||[])];if(config.model&&!models.some(m=>m.id===config.model))models.unshift({id:config.model});
        if(desired){try{const [id,model]=JSON.parse(desired);if(id===provider.id&&!models.some(m=>m.id===model))models.unshift({id:model});}catch{}}
        for(const model of models){const option=make('option',model.id);option.value=encode(provider.id,model.id);group.append(option);}select.append(group);
      }
      const native=engines.find(engine=>engine.available&&engine.key==='codex_cli');if(native){const group=make('optgroup');group.label='本机 Codex';const option=make('option','Sol');option.value='codex_cli';group.append(option);select.append(group);}
      const values=[...select.querySelectorAll('option')].map(option=>option.value);const active=APIProfiles.active();
      select.value=values.includes(desired)?desired:active&&values.includes(encode(active.provider,active.config.model))?encode(active.provider,active.config.model):values[0]||'';
      choice=select.value;lastChoice=choice;notifyChange();
    }
    async function refresh(){const current=++ticket;try{providers=(await APIProfiles.load()).filter(p=>APIProfiles.hasSaved(p.id));if(current!==ticket)return;render();status.textContent=providers.length?'正在刷新模型列表…':'';
      let failed=false;Promise.all(providers.map(async provider=>{try{const models=await APIProfiles.catalog({...APIProfiles.get(provider.id),provider:provider.id},{force:true});if(current!==ticket)return;catalogs.set(catalogKey(provider.id),models);if(catalogs.size>8)catalogs.delete(catalogs.keys().next().value);render();}catch{failed=true;}})).then(()=>{if(current===ticket)status.textContent=failed?'模型列表刷新失败，仍可使用当前模型。':'';});
    }catch(error){if(current===ticket){render();status.textContent=error.message;}}}
    select.addEventListener('change',()=>{choice=select.value;lastChoice=choice;notifyChange();});
    window.addEventListener('api-profiles-changed',()=>{if(host.isConnected)refresh();});
    const ready=refresh();return {ready,refresh,value};
  }
  return {mount};
})();
