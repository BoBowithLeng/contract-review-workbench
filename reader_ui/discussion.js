'use strict';
window.DiscussionUI=(()=>{
  const $=id=>document.getElementById(id);
  const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;};
  let active=null,thread=null,apiConfig=null,poll=null,generation=0,sending=false,stopping=false,creating=false,sessionList=[],modelChoice=null,modelPickerTicket=0,modelProviders=[];
  const drafts=new Map(),uncertain=new Map(),uncertainSessions=new Map();
  const modelCatalogs=new Map();
  let fullscreenState=null;
  const storage={
    read:key=>{try{return localStorage.getItem(key);}catch{return null;}},
    write:(key,value)=>{try{localStorage.setItem(key,value);}catch{}}
  };
  const modelStorageKey='contract-review-discussion-model-v1',searchStorageKey='contract-review-web-search-v1';
  try{const choice=JSON.parse(storage.read(modelStorageKey));if(typeof choice?.provider==='string'&&typeof choice?.model==='string')modelChoice=choice;}catch{}
  $('discussion-web-search').checked=storage.read(searchStorageKey)==='true';
  function syncConfig(){
    if(modelChoice&&APIProfiles.hasSaved(modelChoice.provider)){
      const config=APIProfiles.get(modelChoice.provider);
      apiConfig=config.base_url&&config.api_key&&modelChoice.model?{...config,model:modelChoice.model}:null;
    }else{apiConfig=APIProfiles.active()?.config||null;}
  }
  syncConfig();
  const findingKey=()=>active?active.reportId+'/'+active.findingId:'';
  const key=()=>active?.sessionId?findingKey()+'/'+active.sessionId:'';
  function searchAvailable(){
    if(!apiConfig)return false;
    try{const url=new URL(apiConfig.base_url);return url.protocol==='https:'&&url.hostname==='api.deepseek.com'&&(!url.port||url.port==='443')&&['','/v1','/chat/completions','/v1/chat/completions'].includes(url.pathname.replace(/\/+$/,''));}catch{return false;}
  }
  const endpoint=()=>'/api/reports/'+encodeURIComponent(active.reportId)+'/discussions/'+encodeURIComponent(active.findingId);
  const threadUrl=()=>endpoint()+(active.sessionId?'?session_id='+encodeURIComponent(active.sessionId):'');
  function saveDraft(){if(active?.sessionId)drafts.set(key(),$('discussion-input').value);}
  function sessionLabel(session,index){
    const date=(session.last_at||session.created_at||'').slice(0,10);
    return '讨论'+(index+1)+(date?' · '+date:'')+(session.turn_count?' · '+session.turn_count+'轮':'');
  }
  function renderSessions(){
    const select=$('discussion-session'),sessions=sessionList;
    select.replaceChildren();
    for(const [index,session] of sessions.entries()){
      const option=el('option','',sessionLabel(session,index));option.value=session.id;select.add(option);
    }
    select.value=active?.sessionId||'';
    select.disabled=creating||sessions.length<2;
    $('discussion-new-session').disabled=!thread||creating||sending||!!thread.pending_turn_id;
    $('discussion-new-session').textContent=creating?'新建中…':'新建讨论';
  }
  function close(){
    saveDraft();
    generation++;clearTimeout(poll);active=null;thread=null;sending=false;stopping=false;creating=false;sessionList=[];
    window.OpinionRevisions?.close();
    closeModelPicker();setExpanded(false);
    $('discussion-pane').hidden=true;$('rail').classList.remove('discussion-mode');
  }
  async function open(report,item){
    close();active={reportId:report.id,findingId:item.id,title:item.title,sessionId:null};
    const ticket=generation;
    $('rail').classList.add('discussion-mode');$('discussion-pane').hidden=false;
    $('discussion-topic').textContent=item.title;$('discussion-topic').title=item.title;
    $('discussion-context-note').textContent='正在读取这条意见的讨论记录…';
    $('discussion-input').value='';
    resizeInput();
    $('discussion-error').hidden=true;$('discussion-log').replaceChildren(el('p','discussion-empty','正在读取…'));
    updateControls();await refresh(ticket);if(ticket===generation)$('discussion-input').focus();
  }
  function showError(message){$('discussion-error').textContent=message;$('discussion-error').hidden=!message;}
  function updateControls(){
    const pending=!!thread?.pending_turn_id||(thread?.turns||[]).some(t=>t.status==='running');
    $('discussion-send').disabled=!active||!thread||!active.sessionId||sending||creating||pending||!$('discussion-input').value.trim();
    $('discussion-send').textContent=sending?'发送中…':pending?'回复中…':'发送';
    const canStop=!!active&&!sending&&pending;
    $('discussion-send').hidden=canStop;$('discussion-stop').hidden=!canStop;
    $('discussion-stop').disabled=stopping;$('discussion-stop').textContent=stopping?'停止中…':'停止';
    const info=apiConfig?apiConfig.model:'选择模型';
    $('discussion-model').textContent=info;$('discussion-model').title='选择讨论模型';
    $('discussion-model').setAttribute('aria-label',apiConfig?'选择讨论模型，当前 '+apiConfig.model:'选择讨论模型');
    const canSearch=searchAvailable();$('discussion-search-control').hidden=!canSearch;
    $('discussion-web-search').disabled=!canSearch||sending||creating||pending;
    $('discussion-generate-opinion').disabled=!active||!thread||!active.sessionId||sending||creating||pending;
    $('discussion-opinion-history').disabled=!active||!thread||!active.sessionId;
    renderSessions();
  }
  function setExpanded(value){
    const workspace=$('reader-workspace'),log=$('discussion-log'),logTop=log.scrollTop;
    if(value&&!fullscreenState){
      fullscreenState={overflow:document.body.style.overflow,scrollY:window.scrollY||0,documentTop:$('document-area')?.scrollTop||0,inert:[]};
      for(const sibling of Array.from(workspace.parentElement?.children||[]))if(sibling!==workspace&&sibling.tagName!=='DIALOG'){
        fullscreenState.inert.push([sibling,sibling.inert]);sibling.inert=true;
      }
      document.body.style.overflow='hidden';workspace.setAttribute('role','dialog');workspace.setAttribute('aria-modal','true');workspace.setAttribute('aria-label','全屏讨论');
    }
    $('reader-workspace').classList.toggle('discussion-expanded',value);
    $('discussion-expand').textContent=value?'退出全屏':'全屏讨论';
    $('discussion-expand').setAttribute('aria-pressed',String(value));
    if(!value&&fullscreenState){
      const saved=fullscreenState;fullscreenState=null;
      for(const [element,inert] of saved.inert)element.inert=inert;
      document.body.style.overflow=saved.overflow;workspace.removeAttribute('role');workspace.removeAttribute('aria-modal');workspace.removeAttribute('aria-label');
      if($('document-area'))$('document-area').scrollTop=saved.documentTop;
      window.scrollTo?.(0,saved.scrollY);
    }
    resizeInput();log.scrollTop=logTop;
  }
  function resizeInput(){
    const input=$('discussion-input');input.style.height='auto';
    const cap=Math.min(210,Math.max(72,Math.round((window.innerHeight||720)*.24)));
    input.style.height=Math.min(cap,Math.max(54,input.scrollHeight||54))+'px';
  }
  function closeModelPicker(){
    modelPickerTicket++;$('discussion-model-picker').hidden=true;
    $('discussion-model').setAttribute('aria-expanded','false');
  }
  function modelItem(provider,model){
    const item=el('button','discussion-model-option',model.id);
    item.type='button';item.title=model.id;
    const selected=modelChoice?.provider===provider.id&&modelChoice.model===model.id
      ||!modelChoice&&APIProfiles.active()?.provider===provider.id&&apiConfig?.model===model.id;
    item.classList.toggle('selected',selected);
    item.onclick=()=>{
      modelChoice={provider:provider.id,model:model.id};storage.write(modelStorageKey,JSON.stringify(modelChoice));
      syncConfig();closeModelPicker();updateControls();$('discussion-model').focus();
    };
    return item;
  }
  function renderModelPicker(){
    const list=$('discussion-model-list'),query=$('discussion-model-search').value.trim().toLowerCase();list.replaceChildren();
    for(const provider of modelProviders){
      const saved=APIProfiles.get(provider.id),models=modelCatalogs.get(provider.id)||[];
      const options=[...models];
      if(saved.model&&!options.some(item=>item.id===saved.model))options.unshift({id:saved.model,name:saved.model});
      const matches=options.filter(item=>(provider.label+' '+provider.id+' '+item.name+' '+item.id).toLowerCase().includes(query));
      if(!matches.length)continue;
      const group=el('section','discussion-model-group');group.append(el('h4','',provider.label||provider.id));
      try{group.append(el('small','discussion-provider-host',new URL(saved.base_url).host));}catch{}
      for(const model of matches)group.append(modelItem(provider,model));
      list.append(group);
    }
    if(!list.children.length)list.append(el('p','discussion-model-empty',modelProviders.length?'没有匹配的模型。':'还没有保存模型配置；先去模型设置保存一份。'));
  }
  async function openModelPicker(){
    if(!$('discussion-model-picker').hidden){closeModelPicker();return;}
    $('discussion-model-picker').hidden=false;$('discussion-model').setAttribute('aria-expanded','true');
    $('discussion-model-search').value='';$('discussion-model-search').focus();
    const ticket=++modelPickerTicket;
    $('discussion-model-status').textContent='正在读取已保存的模型…';
    try{
      const providers=await APIProfiles.load();if(ticket!==modelPickerTicket)return;
      modelProviders=providers.filter(provider=>APIProfiles.hasSaved(provider.id));renderModelPicker();
      $('discussion-model-status').textContent=modelProviders.length?'正在获取模型列表…':'';
      await Promise.all(modelProviders.map(async provider=>{
        try{const models=await APIProfiles.catalog({...APIProfiles.get(provider.id),provider:provider.id});
          if(ticket!==modelPickerTicket)return;modelCatalogs.set(provider.id,models);renderModelPicker();}
        catch(error){if(ticket===modelPickerTicket)$('discussion-model-status').textContent='部分模型列表没能读取，仍可选择已保存的模型。';}
      }));
      if(ticket===modelPickerTicket&&$('discussion-model-status').textContent==='正在获取模型列表…')$('discussion-model-status').textContent='';
    }catch(error){if(ticket===modelPickerTicket)$('discussion-model-status').textContent=error.message;}
  }
  function renderSearch(reply,turn){
    if(!turn.web_search)return;
    const data=turn.search;
    if(turn.status==='running')return;
    const labels={searched:'已联网检索',not_used:'本轮未使用联网检索',no_results:'本次未取得可用检索结果',error:'本次联网检索失败',partial:turn.status==='completed'?'部分检索未完成，回答依据已取得的结果':'联网检索部分完成'};
    const note=el('div','discussion-search-note',data?labels[data.status]||'联网检索状态未确认':'联网请求未完成');
    if(data&&Number.isInteger(data.request_count)&&data.request_count>0)note.append(document.createTextNode(' · '+data.request_count+' 次'));
    reply.append(note);
    if(!data)return;
    const sources=(data.sources||[]).filter(source=>{try{return ['http:','https:'].includes(new URL(source.url).protocol);}catch{return false;}});
    const queries=(data.queries||[]).filter(q=>typeof q==='string');
    if(!sources.length&&!queries.length)return;
    const details=el('details','discussion-sources');details.append(el('summary','','查看检索结果'+(sources.length?'（'+sources.length+'）':'')));
    if(queries.length)details.append(el('p','discussion-search-queries','搜索词：'+queries.join('；')));
    const reasons={max_uses_exceeded:'部分搜索请求达到次数限制',too_many_requests:'部分搜索请求被限流',unavailable:'部分搜索服务暂不可用',invalid_tool_input:'部分搜索参数未被接受',query_too_long:'部分搜索词过长',request_too_large:'部分搜索请求过大'};
    if(data.error_codes?.length)details.append(el('p','discussion-search-queries','检索提示：'+data.error_codes.map(code=>reasons[code]||'部分检索未成功').join('；')));
    if(sources.length){const list=el('ul');for(const source of sources){const item=el('li'),link=el('a','',source.title||source.url);link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';link.title=source.url;item.append(link);list.append(item);}details.append(list);}
    reply.append(details);
  }
  let markdownReady=false;
  const escapeHtml=value=>String(value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  function richText(container,text){
    let display=String(text||'');
    if(window.ReaderText?.humanize){try{display=window.ReaderText.humanize(display,thread?.context||{});}catch{}}
    const parser=window.marked?.marked||window.marked?.parse;
    if(!parser||!window.DOMPurify){container.textContent=display;return;}
    if(!markdownReady){
      window.marked.use({gfm:true,renderer:{html(token){return escapeHtml(token.text||'');}}});
      markdownReady=true;
    }
    const parsed=parser(display,{gfm:true,breaks:false,async:false});
    container.innerHTML=window.DOMPurify.sanitize(parsed,{
      USE_PROFILES:{html:true},
      ALLOWED_TAGS:['p','h1','h2','h3','h4','h5','h6','blockquote','ul','ol','li','table','thead','tbody','tr','th','td','pre','code','strong','em','del','a','hr','br','input'],
      ALLOWED_ATTR:['href','title','type','checked','disabled','start','align','class'],
      ALLOW_DATA_ATTR:false
    });
    for(const table of container.querySelectorAll('table')){
      const wrap=el('div','discussion-table-wrap');table.parentNode.insertBefore(wrap,table);wrap.append(table);
    }
    for(const link of container.querySelectorAll('a')){
      try{if(!['http:','https:'].includes(new URL(link.href).protocol))link.removeAttribute('href');}
      catch{link.removeAttribute('href');}
      link.target='_blank';link.rel='noopener noreferrer';
    }
    for(const checkbox of container.querySelectorAll('input'))checkbox.disabled=true;
  }
  function render(){
    const log=$('discussion-log'),oldTop=log.scrollTop,nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<90;
    log.replaceChildren();
    const turns=thread?.turns||[];
    if(!turns.length){
      const empty=el('div','discussion-empty');
      empty.append(el('strong','','围绕这一条，继续讨论'),el('p','','可以质疑判断、追问依据，或补充业务事实。'));
      log.append(empty);
    }
    for(const turn of turns){
      const user=el('article','discussion-message from-user');
      user.append(el('div','message-author','你'),el('div','message-text',turn.user||''));log.append(user);
      const reply=el('article','discussion-message from-model');
      reply.append(el('div','message-author',turn.model||'审查助手'));
      if(turn.status==='running'){
        const pending=el('p','discussion-waiting',turn.web_search?'正在处理联网请求并回答…':'正在回应这条问题…');pending.setAttribute('role','status');reply.append(pending);
      }else if(turn.status==='failed'){
        if(turn.assistant){
          reply.append(el('p','discussion-version-note','已生成的部分回答'));
          const partial=el('div','message-text');richText(partial,turn.assistant);reply.append(partial);
        }
        reply.append(el('p','discussion-turn-error'+(turn.error?.startsWith('已停止回答')?' stopped':''),turn.error||'这一轮没有收到完整回复。'));
      }else{
        const text=el('div','message-text');richText(text,turn.assistant||'');reply.append(text);
      }
      renderSearch(reply,turn);
      window.ContractFactsUI?.renderCandidates(reply,turn,active);
      if(turn.delivery_revision!==undefined&&thread.context?.delivery_revision!==undefined&&turn.delivery_revision!==thread.context.delivery_revision){
        reply.append(el('small','discussion-version-note','这轮回复基于修改前的合同记录。'));
      }
      log.append(reply);
    }
    const ctx=thread?.context||{};
    const info=[ctx.document_name,ctx.location,ctx.party_label].filter(Boolean).join(' · ');
    $('discussion-context-note').textContent=info||'这轮讨论会参考当前合同和这条审阅意见';
    $('discussion-context-note').title=info;
    log.scrollTop=nearBottom||sending?log.scrollHeight:oldTop;
    updateControls();
  }
  async function refresh(ticket=generation){
    if(!active||ticket!==generation)return;
    const path=threadUrl(),initial=!active.sessionId;
    try{
      const value=await request(path);
      if(ticket!==generation||!active)return;
      thread=value.state||value;
      active.sessionId=thread.session_id;
      sessionList=thread.sessions||[];
      const localKey=key();
      if(initial){$('discussion-input').value=drafts.get(localKey)||'';resizeInput();}
      const pending=uncertain.get(localKey);
      if(pending&&(thread.turns||[]).some(t=>t.request_id===pending.id)){
        if($('discussion-input').value.trim()===pending.message)$('discussion-input').value='';
        uncertain.delete(localKey);drafts.delete(localKey);
      }
      render();clearTimeout(poll);
      if(thread.pending_turn_id||(thread.turns||[]).some(t=>t.status==='running'))poll=setTimeout(()=>refresh(ticket),1500);
    }catch(e){
      if(ticket!==generation)return;
      showError(e.message);updateControls();
    }
  }
  async function switchSession(sessionId){
    if(!active||!sessionId||sessionId===active.sessionId||creating)return;
    saveDraft();generation++;clearTimeout(poll);window.OpinionRevisions?.close();
    active.sessionId=sessionId;thread=null;sending=false;stopping=false;
    const ticket=generation;
    $('discussion-input').value=drafts.get(key())||'';
    resizeInput();
    $('discussion-log').replaceChildren(el('p','discussion-empty','正在读取这轮讨论…'));
    showError('');updateControls();await refresh(ticket);
    if(ticket===generation)$('discussion-input').focus();
  }
  async function newSession(){
    if(!active||!thread||creating||sending)return;
    saveDraft();const ticket=generation,localFinding=findingKey();
    const requestId=uncertainSessions.get(localFinding)||crypto.randomUUID();
    uncertainSessions.set(localFinding,requestId);
    creating=true;showError('');updateControls();let applied=false;
    try{
      const value=await request(endpoint()+'/sessions',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({expected_sessions_revision:thread.sessions_revision,
          expected_delivery_revision:thread.context?.delivery_revision,
          expected_opinion_revision:thread.opinion_revision??thread.context?.opinion_revision,
          request_id:requestId})});
      if(ticket!==generation)return;
      uncertainSessions.delete(localFinding);
      generation++;clearTimeout(poll);window.OpinionRevisions?.close();
      applied=true;
      active.sessionId=(value.state||value).session_id;
      thread=value.state||value;sessionList=thread.sessions||[];
      $('discussion-input').value=drafts.get(key())||'';
      resizeInput();
      render();
      $('discussion-input').focus();
    }catch(error){
      if(ticket!==generation)return;
      if(error.status===409){uncertainSessions.delete(localFinding);await refresh(ticket);}
      showError(error.status===409?'有一轮回复还在生成，或另一处刚新建了讨论。列表已刷新，请再试。':error.message);
    }finally{if(active&&(ticket===generation||applied&&ticket+1===generation)){creating=false;updateControls();}}
  }
  async function send(event){
    event.preventDefault();
    if(!active||sending)return;
    const message=$('discussion-input').value.trim();if(!message)return;
    const webSearch=searchAvailable()&&$('discussion-web-search').checked;
    if(!apiConfig){showError('先在设置中保存模型配置。');window.ModelSettings.open();return;}
    if(!thread){showError('讨论记录尚未载入，请返回这条意见后重新打开。');return;}
    const ticket=generation,localKey=key(),path=endpoint();
    const old=uncertain.get(localKey);
    const requestId=old?.message===message&&old.webSearch===webSearch?old.id:crypto.randomUUID();
    uncertain.set(localKey,{id:requestId,message,webSearch});
    sending=true;showError('');updateControls();
    try{
      const value=await request(path,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({session_id:active.sessionId,expected_revision:thread.revision,expected_delivery_revision:thread.context.delivery_revision,
          expected_opinion_revision:thread.opinion_revision??thread.context?.opinion_revision,
          message,request_id:requestId,config:{...apiConfig},consent:true,web_search:webSearch})});
      if(ticket!==generation)return;
      thread=value.state||value;sessionList=thread.sessions||sessionList;$('discussion-input').value='';resizeInput();drafts.delete(localKey);uncertain.delete(localKey);
      render();$('discussion-log').scrollTop=$('discussion-log').scrollHeight;
    }catch(e){
      if(ticket!==generation)return;
      showError(e.message);await refresh(ticket);
    }finally{
      if(ticket===generation){sending=false;updateControls();await refresh(ticket);}
    }
  }
  async function stop(){
    const running=(thread?.turns||[]).find(t=>t.status==='running');
    if(!active||!running||stopping)return;
    const ticket=generation;stopping=true;showError('');updateControls();
    try{
      const value=await request(endpoint()+'/stop',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({session_id:active.sessionId,turn_id:running.id})});
      if(ticket!==generation)return;
      thread=value.state||value;sessionList=thread.sessions||sessionList;render();
    }catch(e){
      if(ticket!==generation)return;
      showError(e.message);
    }finally{
      if(ticket===generation){stopping=false;updateControls();await refresh(ticket);}
    }
  }
  $('discussion-stop').onclick=stop;
  $('discussion-back').onclick=close;$('discussion-settings').onclick=()=>{closeModelPicker();window.ModelSettings.open();};
  $('discussion-expand').onclick=()=>setExpanded(!$('reader-workspace').classList.contains('discussion-expanded'));
  $('discussion-model').onclick=openModelPicker;
  $('discussion-model-close').onclick=()=>{closeModelPicker();$('discussion-model').focus();};
  $('discussion-model-search').oninput=renderModelPicker;
  $('discussion-model-picker').onkeydown=event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeModelPicker();$('discussion-model').focus();}};
  window.addEventListener('keydown',event=>{
    if(event.key==='Escape'&&!event.defaultPrevented&&active&&$('reader-workspace').classList.contains('discussion-expanded')&&!document.querySelector('dialog[open]')){
      event.preventDefault();setExpanded(false);$('discussion-expand').focus();
    }
  });
  window.addEventListener('resize',()=>{if(active)resizeInput();});
  $('discussion-web-search').onchange=()=>storage.write(searchStorageKey,String($('discussion-web-search').checked));
  $('discussion-session').onchange=event=>switchSession(event.target.value);
  $('discussion-new-session').onclick=newSession;
  $('discussion-generate-opinion').onclick=()=>window.OpinionRevisions?.open({...active,sessionLabel:$('discussion-session').selectedOptions[0]?.textContent},thread,{generate:true});
  $('discussion-opinion-history').onclick=()=>window.OpinionRevisions?.open({...active,sessionLabel:$('discussion-session').selectedOptions[0]?.textContent},thread);
  window.addEventListener('api-profiles-changed',()=>{modelCatalogs.clear();syncConfig();updateControls();if(!$('discussion-model-picker').hidden){closeModelPicker();openModelPicker();}});
  $('discussion-form').onsubmit=send;
  $('discussion-input').oninput=()=>{if(active?.sessionId)drafts.set(key(),$('discussion-input').value);resizeInput();updateControls();};
  $('discussion-input').onkeydown=event=>{
    if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!$('discussion-send').disabled)$('discussion-form').requestSubmit();}
  };
  $('about-product').onclick=()=>$('about-dialog').showModal();
  $('about-close').onclick=()=>$('about-dialog').close();
  $('about-dialog').addEventListener('click',e=>{if(e.target===$('about-dialog'))$('about-dialog').close();});
  function updateCurrentItem(item){
    if(!active||active.findingId!==item.id)return;
    active.title=item.title;
    $('discussion-topic').textContent=item.title;$('discussion-topic').title=item.title;
  }
  return {open,close,refresh:()=>refresh(generation),updateCurrentItem,current:()=>active?{...active,thread}:null,
    modelConfig:()=>apiConfig?{provider:modelChoice&&APIProfiles.hasSaved(modelChoice.provider)?modelChoice.provider:APIProfiles.active()?.provider,
      config:{...apiConfig}}:null};
})();
