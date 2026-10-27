'use strict';
// Opinion versions are independent from confirmed Word edits and comments.
window.OpinionRevisions=(()=>{
  const $=id=>document.getElementById(id);
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n;};
  const button=(label,cls,action)=>{const n=el('button',cls,label);n.type='button';n.onclick=action;return n;};
  const dialog=$('opinion-revision-dialog'),body=$('opinion-revision-body');
  const requestIds=new Map();
  let active=null,state=null,ticket=0,poll=null,busy=false,onAccepted=null,feedback=null;
  const key=()=>active?.reportId+'/'+active?.findingId+'/'+active?.sessionId;
  const endpoint=()=>'/api/reports/'+encodeURIComponent(active.reportId)+'/discussions/'+encodeURIComponent(active.findingId)+'/revisions';
  const selectedUrl=()=>endpoint()+'?session_id='+encodeURIComponent(active.sessionId);
  async function request(path,options){
    let response;try{response=await fetch(path,{cache:'no-store',...options});}
    catch{throw new Error('无法连接本机服务。请检查这一版是否已经生成，再重试。');}
    let data={};try{data=await response.json();}catch{throw new Error('没能读取这条意见的版本，请重新打开查看。');}
    if(!response.ok){const error=new Error(data.error||'意见版本操作未完成。');error.status=response.status;throw error;}
    return data.state||data;
  }
  function notice(text,error=false){feedback={text,error};const n=$('opinion-revision-status');if(!n)return;n.textContent=text;n.classList.toggle('is-error',error);}
  // Model text cites paragraphs, opinions and sources by internal ID; show clause numbers and titles as the opinion card does.
  // One frozen context per loaded state so ReaderText builds its label tables once per render.
  let textContext=null,textContextFor=null;
  function human(value){
    if(typeof value!=='string'||!window.ReaderText)return value;
    if(textContextFor!==state){
      const c=state?.context||{};
      const locations=Array.isArray(c.locations)?Object.fromEntries(c.locations.map(row=>[row.id,row.location])):{...(c.locations||{})};
      textContext={locations:Object.freeze(locations),finding_labels:Object.freeze({...(c.finding_labels||{})}),
        sources:Array.isArray(c.sources)?c.sources:Object.values(c.sources||{}),current_finding_id:active?.findingId};
      textContextFor=state;
    }
    return window.ReaderText.humanize(value,textContext,{plain:true,shortLocations:true,hideUnknown:true});
  }
  function field(parent,title,value){
    if(value===undefined||value===null||value===''||(Array.isArray(value)&&!value.length))return;
    const section=el('section','opinion-version-field');
    section.append(el('h4','',title));
    if(Array.isArray(value)){const list=el('ul');for(const item of value)list.append(el('li','',typeof item==='string'?human(item):JSON.stringify(item)));section.append(list);}
    else section.append(el('p','',human(String(value))));
    parent.append(section);
  }
  function referenceLocation(ref){
    const locations=state?.context?.locations||{};
    const location=Array.isArray(locations)?locations.find(row=>row.id===ref.id)?.location:locations[ref.id];
    const label=location||ref.location||ref.id;
    return ref.quote&&label.includes(ref.quote)?label:[label,ref.quote].filter(Boolean).join('\n');
  }
  function sources(parent,ids,evidence){
    if(!ids?.length)return;
    const catalog=state?.context?.sources||[],section=el('section','opinion-version-field');
    section.append(el('h4','','相关依据'));
    const list=el('ul');
    for(const id of ids){
      const source=Array.isArray(catalog)?catalog.find(item=>item.id===id):catalog[id];
      const item=el('li');
      const record=evidence?.origin==='frozen_source_records'?evidence.sources?.find(item=>item.id===id):null;
      const name=([source?.title,source?.article].filter(Boolean).join(' · ')||id)+(window.ReaderText?.sourceNote(record||source,Boolean(record))||'');
      let valid=false;try{valid=['http:','https:'].includes(new URL(source?.url).protocol);}catch{}
      if(valid){const link=el('a','',name);link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';item.append(link);}
      else item.textContent=name;
      list.append(item);
    }
    section.append(list);parent.append(section);
  }
  function summary(finding,action){
    const box=el('div','opinion-version-content');
    if(!finding){box.append(el('p','opinion-version-empty','这版没有完整意见可供查看。'));return box;}
    field(box,'标题',finding.title);
    field(box,'判断',[finding.risk?finding.risk+'风险':'',(action||finding.revision_action)==='withdraw'?'已撤回':finding.status].filter(Boolean).join(' · '));
    field(box,'原文位置',finding.refs?.length?finding.refs.map(referenceLocation):finding.location_note);
    field(box,'修改理由',finding.reason);
    field(box,'修改建议',finding.clause_draft);
    field(box,'处理办法',finding.suggestion);
    field(box,'待核实的信息',finding.questions);
    sources(box,finding.source_ids,finding.evidence);
    return box;
  }
  function candidateView(candidate,compact=false){
    const wrap=el('section','opinion-candidate');
    const status={running:'生成中',completed:'待采用',failed:'生成失败'}[candidate.status]||candidate.status;
    const accepted=state.accepted_candidate_id===candidate.id;
    const statusLabel=accepted?'已采用':candidate.accepted_at?'曾采用':status;
    const head=el('div','opinion-candidate-head');
    head.append(el('strong','',candidate.action==='withdraw'?'撤回这条意见':candidate.finding?.title||'新版意见'),el('span','',statusLabel));
    wrap.append(head);
    function presented(){
      if(!compact)return wrap;
      const details=el('details','opinion-history-item');details.dataset.key='candidate:'+candidate.id;
      details.append(el('summary','',(candidate.action==='withdraw'?'撤回这条意见':candidate.finding?.title||'新版意见')+' · '+statusLabel+(candidate.created_at?' · '+candidate.created_at.slice(0,10):'')),wrap);
      return details;
    }
    if(candidate.status==='running'){wrap.append(el('p','opinion-version-pending','正在根据这轮讨论生成新版意见，当前意见暂时不变。'));return presented();}
    if(candidate.status==='failed'){wrap.append(el('p','opinion-version-error',candidate.error||'这次生成没有完成。再次点击“生成新版意见”才会重新调用模型。'));return presented();}
    field(wrap,'为什么改',candidate.change_summary||'这版未提供修改说明。');
    if(candidate.related_finding_ids?.length)field(wrap,'关联意见需复核',candidate.related_finding_ids.map(id=>state.context?.finding_labels?.[id]||id));
    const compare=el('div','opinion-version-compare');
    const old=el('div','opinion-version-side'),next=el('div','opinion-version-side');
    old.append(el('h3','',candidate.base_finding||candidate.base_opinion_revision===0?'生成前':'最初版（没有保存生成前的那一版）'),summary(candidate.base_finding||state.original_finding));
    next.append(el('h3','','这一版'),summary(candidate.finding,candidate.action));
    compare.append(old,next);wrap.append(compare);
    if(!accepted){
      const currentBase=candidate.base_opinion_revision===state.opinion_revision;
      const accept=button('采用这版意见','opinion-accept',()=>acceptCandidate(candidate));
      accept.disabled=busy||!currentBase;
      wrap.append(accept);
      if(!currentBase)wrap.append(el('p','opinion-version-note','这版基于旧意见生成，不能直接覆盖当前版；请从当前讨论重新生成。'));
      else wrap.append(el('p','opinion-version-note','采用只更新审阅意见。合同改稿与批注仍需另行确认。'));
    }
    return presented();
  }
  function render(){
    if(!active||!dialog.open)return;
    // Polling re-renders every 1.6s while a version is generating; keep what the reader opened.
    const opened=new Set([...body.querySelectorAll('details[data-key]')].filter(d=>d.open).map(d=>d.dataset.key));
    const scrollTop=dialog.scrollTop;
    body.replaceChildren();
    const intro=el('p','opinion-version-intro','先看变化，再决定是否采用；原讨论和旧版意见都会保留。');
    const status=el('p','opinion-revision-status');status.id='opinion-revision-status';status.setAttribute('role','status');
    body.append(intro,status);
    if(!state){status.textContent='正在读取意见版本…';return;}
    const candidates=[...(state.candidates||[])].reverse();
    const otherRunning=!!state.global_pending_candidate_id&&state.pending_candidate_session_id!==active.sessionId;
    const running=!!state.pending_candidate_id||candidates.some(c=>c.status==='running');
    const controls=el('div','opinion-version-actions');
    const generate=button(running?'生成中…':'生成新版意见','opinion-generate',generateCandidate);
    generate.disabled=busy||running||otherRunning;
    controls.append(generate);body.append(controls);
    if(!candidates.length)body.append(el('p','opinion-version-empty','还没有生成过新版意见。'));
    else{body.append(el('h3','opinion-history-title','最新生成的一版'));body.append(candidateView(candidates[0]));}
    const current=el('details','opinion-history-item');current.dataset.key='current';
    const fromOther=state.accepted_candidate_session_id&&state.accepted_candidate_session_id!==active.sessionId;
    current.append(el('summary','',fromOther?'当前意见（采用自其他讨论）':'当前意见'),summary(state.current_finding));body.append(current);
    if(candidates.length>1){body.append(el('h3','opinion-history-title','较早版本'));for(const candidate of candidates.slice(1))body.append(candidateView(candidate,true));}
    if(feedback){status.textContent=feedback.text;status.classList.toggle('is-error',feedback.error);}
    else if(otherRunning)status.textContent='另一轮讨论正在生成新版意见，请等待那轮完成后再生成。';
    else if(running)status.textContent='正在生成新版意见。可以关闭窗口，稍后再查看。';
    for(const details of body.querySelectorAll('details[data-key]'))if(opened.has(details.dataset.key))details.open=true;
    dialog.scrollTop=scrollTop;
  }
  async function refresh(expected=ticket){
    if(!active||expected!==ticket)return;
    try{
      const value=await request(selectedUrl());
      if(expected!==ticket)return;
      state=value;
      const uncertain=requestIds.get(key());
      if(uncertain&&(state.candidates||[]).some(c=>c.request_id===uncertain))requestIds.delete(key());
      render();clearTimeout(poll);
      if(state.pending_candidate_id||state.global_pending_candidate_id)poll=setTimeout(()=>refresh(expected),1600);
    }catch(error){if(expected===ticket)notice(error.message,true);}
  }
  async function generateCandidate(){
    if(!active||!state||busy||state.pending_candidate_id||state.global_pending_candidate_id)return;
    const profile=window.DiscussionUI?.modelConfig?.()||window.APIProfiles.active();
    if(!profile?.config||!window.APIProfiles.hasSaved(profile.provider)){notice('先在模型设置中保存当前服务商配置。',true);return;}
    const expected=ticket,localKey=key(),requestId=requestIds.get(localKey)||crypto.randomUUID();
    requestIds.set(localKey,requestId);busy=true;feedback=null;render();notice('正在提交生成请求…');
    let failure='';
    try{
      const value=await request(endpoint(),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        session_id:active.sessionId,expected_revision:state.revision,expected_opinion_revision:state.opinion_revision,
        expected_discussion_revision:state.context?.discussion_revision,
        expected_delivery_revision:state.context?.delivery_revision,
        request_id:requestId,config:{...profile.config},consent:true
      })});
      if(expected!==ticket)return;
      state=value;
      if((state.candidates||[]).some(c=>c.request_id===requestId))requestIds.delete(localKey);
      feedback=null;
      render();
    }catch(error){
      if(expected!==ticket)return;
      failure=error.status===409?'讨论、意见或合同修改记录有了变化。请查看当前内容，再决定是否生成。':error.message;
    }finally{
      if(expected===ticket){busy=false;await refresh(expected);if(failure)notice(failure,true);}
    }
  }
  async function acceptCandidate(candidate){
    if(!active||!state||busy)return;
    const expected=ticket,reportId=active.reportId,findingId=active.findingId;
    busy=true;feedback=null;render();notice('正在采用这版意见…');
    try{
      const value=await request(endpoint()+'/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        session_id:active.sessionId,expected_revision:state.revision,expected_opinion_revision:state.opinion_revision,
        expected_discussion_revision:state.context?.discussion_revision,
        expected_delivery_revision:state.context?.delivery_revision,candidate_id:candidate.id
      })});
      if(expected!==ticket)return;
      state=value;
      if(onAccepted)await onAccepted({reportId,findingId});
      if(expected!==ticket)return;
      render();notice('已采用这版意见。合同改稿与批注没有自动改变。');
    }catch(error){if(expected===ticket)notice(error.status===409?'当前意见或合同修改记录有了变化，请核对后再操作。':error.message,true);}
    finally{if(expected===ticket){busy=false;await refresh(expected);}}
  }
  async function open(context,discussion,{generate=false}={}){
    if(!context)return;
    close();active={...context};const expected=ticket;
    $('opinion-revision-title').textContent=context.title+' · '+(context.sessionLabel||'当前讨论')+' · 意见版本';
    dialog.showModal();render();
    await refresh(expected);
    if(generate&&expected===ticket&&state&&!state.global_pending_candidate_id)generateCandidate();
    else $('opinion-revision-close').focus();
  }
  function close(){
    ticket++;clearTimeout(poll);active=null;state=null;busy=false;feedback=null;
    if(dialog.open)dialog.close();
  }
  $('opinion-revision-close').onclick=close;
  dialog.addEventListener('click',event=>{if(event.target===dialog)close();});
  dialog.addEventListener('close',()=>{if(active)close();});
  return {open,close,configure:config=>{onAccepted=config.onAccepted;},refresh:()=>refresh(ticket)};
})();
