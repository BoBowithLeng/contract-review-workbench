(()=>{
  'use strict';
  const host=document.getElementById('information-gate');
  const node=(tag,cls='',text)=>{const n=document.createElement(tag);n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  const button=(text,handler)=>{const n=node('button','review-job-secondary',text);n.type='button';n.addEventListener('click',handler);return n;};
  let reportId='',gate=null,generation=0,timer=null,onReady=async()=>{},submitting=false;
  const drafts=new Map();
  const endpoint=()=>'/api/reports/'+encodeURIComponent(reportId)+'/information-gate';
  async function request(path,payload){
    const response=await fetch(path,payload===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    let value;try{value=await response.json();}catch{throw new Error('没能读取结果，请重试。');}
    if(!response.ok){const error=new Error(value.error||'审查信息处理未完成。');error.status=response.status;error.gate=value.information_gate;throw error;}
    return value;
  }
  function close(){generation++;clearTimeout(timer);timer=null;reportId='';submitting=false;if(host)host.hidden=true;}
  function showError(target,message){const p=node('p','review-job-error',message);p.setAttribute('role','alert');target.append(p);return p;}
  async function publish(ticket){
    if(ticket!==generation)return;
    try{await onReady(reportId);}catch(error){if(ticket!==generation)return;host.hidden=false;host.replaceChildren(node('h2','','报告尚未打开'));showError(host,error.message);host.append(button('重试打开报告',()=>publish(ticket)));}
  }
  function schedule(ticket){clearTimeout(timer);timer=setTimeout(()=>refresh(ticket),3000);}
  async function refresh(ticket=generation){
    if(ticket!==generation||!reportId)return;
    try{const value=await request(endpoint());if(ticket!==generation)return;gate=value.information_gate;await render(ticket);}
    catch(error){if(ticket!==generation)return;clearTimeout(timer);showError(host,'暂时无法读取处理状态：'+error.message);host.append(button('重新读取处理状态',()=>refresh(ticket)));}
  }
  async function render(ticket){
    if(ticket!==generation||!host)return;
    host.hidden=false;host.replaceChildren();
    const pane=node('section','information-gate-pane');host.append(pane);
    if(!gate?.enabled||gate.status==='ready'){pane.append(node('h2','','审查信息已确认'),node('p','review-job-help','正在打开报告…'));return publish(ticket);}
    pane.append(node('p','information-eyebrow','审查信息'),node('h1','','先确认这些信息，再查看报告'));
    pane.append(node('p','review-job-help','这些问题会影响审查判断。请逐条回答，或选择“暂不能提供”。无法提供的信息仍列为待确认，其他能检查的内容照常显示。全部无法提供时，直接打开报告，不额外调用模型。'));
    const questions=gate.questions||[];
    if(gate.status==='running'){
      const status=node('p','review-job-status');status.setAttribute('role','status');status.append(node('span','review-job-spinner'),node('span','','正在根据补充信息调整相关意见，完成后可以打开报告。'));
      pane.append(status,node('p','review-job-help','这次只调整受这些答案影响的意见，不重新审查全文。可以先返回工作台，稍后再打开这份记录。'));
      for(const q of questions){const row=node('section','information-answer');row.append(node('h3','',q.question),node('p','',q.status==='answered'?q.answer:q.status==='unknown'?'暂不能提供':'尚未回答'));pane.append(row);}
      schedule(ticket);return;
    }
    const retry=gate.status==='failed';
    if(retry){pane.append(node('h2','','相关意见调整未完成'));showError(pane,gate.error||'模型处理失败，回答已经保存。');pane.append(node('p','review-job-help','回答已保存。请选好模型，点击“重试调整相关意见”；完成后再查看报告。'));}
    const form=node('form','review-job-form information-gate-form');form.noValidate=true;form.autocomplete='off';pane.append(form);
    const records=questions.map(q=>{
      const key=reportId+':'+q.id;let draft=drafts.get(key);
      if(!draft){draft={answer:q.answer||'',unknown:(retry||gate.revision>0)&&q.status==='unknown'};drafts.set(key,draft);}
      const row=node('section','preparation-question');row.id='gate-question-'+q.id;
      row.append(node('h3','',q.question));
      const titles=(q.finding_titles||[]).filter(title=>typeof title==='string'&&!/\b(?:P\d+|F\d+|T\d+R\d+C\d+P\d+)\b/.test(title));
      const why=titles.length?'用于核对“'+titles.join('”“')+'”的相关判断。':q.reason&&!/\b(?:P\d+|F\d+|T\d+R\d+C\d+P\d+|finding|clause_draft)\b| \/ /.test(q.reason)&&q.reason.length<120?q.reason:'';
      if(why){const detail=node('details','information-question-why');detail.append(node('summary','','为什么要确认'),node('p','fact-question-reason',why));row.append(detail);}
      const value=node('textarea');value.name='gate_answer_'+q.id;value.maxLength=8000;value.value=draft.answer;value.setAttribute('aria-label',q.question);
      const unknown=node('input');unknown.type='checkbox';unknown.name='gate_unknown_'+q.id;unknown.checked=draft.unknown;value.disabled=retry||unknown.checked;unknown.disabled=retry;
      const label=node('label','fact-unknown');label.append(unknown,node('span','','暂不能提供'));
      const status=node('p','fact-question-status');const update=()=>{draft.answer=value.value;draft.unknown=unknown.checked;value.disabled=retry||unknown.checked;status.textContent=unknown.checked?'暂不能提供':value.value.trim()?'已填写回答':'尚未回答';updateModelNeed();};
      value.addEventListener('input',update);unknown.addEventListener('change',update);row.append(status,value,label);form.append(row);
      status.textContent=unknown.checked?'暂不能提供':value.value.trim()?'已填写回答':'尚未回答';
      return {q,draft,value,unknown};
    });
    const modelSection=node('section','information-model'),modelHost=node('div','review-model-selector');
    modelSection.append(node('h3','','调整相关意见使用的模型'),node('p','review-job-help','填写新答案后，会把补充信息、合同全文和相关审查资料发给下方模型，再调用一次来调整相关意见。全部选择“暂不能提供”时，直接打开报告，不调用模型。'),modelHost);
    const consent=node('input');consent.type='checkbox';consent.name='gate_consent';const consentLabel=node('label','review-job-consent');consentLabel.append(consent,node('span','','我同意将补充信息、合同全文与相关审查资料发送给所选模型，调整相关意见。'));modelSection.append(consentLabel);form.append(modelSection);
    const error=node('p','review-job-error');error.setAttribute('role','alert');error.hidden=true;
    const progress=node('p','information-answer-count');progress.setAttribute('role','status');
    const submit=button(retry?'重试调整相关意见':'确认信息并查看报告',()=>{});submit.type='submit';submit.className='review-job-primary';const actions=node('div','review-job-actions');
    if(!retry)actions.append(button('本次全部暂不能提供',()=>{for(const r of records){r.unknown.checked=true;r.draft.unknown=true;r.value.disabled=true;const status=r.value.parentNode.querySelector('.fact-question-status');status.textContent='暂不能提供';}updateModelNeed();}));
    actions.append(submit);form.append(progress,error,actions);
    let selector=null,fingerprint;
    function needsModel(){return retry?gate.requires_model!==false:records.some(r=>!r.unknown.checked&&r.value.value.trim());}
    function updateModelNeed(){if(!modelSection)return;modelSection.hidden=!needsModel();const done=records.filter(r=>r.unknown.checked||r.value.value.trim()).length;if(progress)progress.textContent='已确认 '+done+' / '+records.length+' 条';}
    updateModelNeed();
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(submitting||ticket!==generation)return;error.hidden=true;
      if(!retry){const missing=records.find(r=>!r.unknown.checked&&!r.value.value.trim());if(missing){error.textContent='请逐条回答，或选择“暂不能提供”。';error.hidden=false;missing.value.focus();return;}}
      const requires=needsModel(),choice=selector?.value();
      if(requires&&(!choice||!consent.checked)){error.textContent=!choice?'请选择当前可用的模型服务。':'请先确认所选模型，并勾选同意发送补充信息。';error.hidden=false;return;}
      const payload={expected_revision:gate.revision,expected_facts_revision:gate.facts_revision,...(retry?{retry:true}:{answers:records.map(r=>({question_id:r.q.id,status:r.unknown.checked?'unknown':'answered',answer:r.unknown.checked?'':r.value.value.trim()}))}),...(requires?{engine_key:choice.engine_key,config:choice.config,consent:true}:{})};
      submitting=true;submit.disabled=true;submit.textContent=requires?'正在提交补充信息…':'正在打开报告…';
      try{const result=await request(endpoint(),payload);if(ticket!==generation)return;gate=result.information_gate;submitting=false;await render(ticket);}
      catch(reason){if(ticket!==generation)return;error.textContent=reason.status===409?'信息已在另一处更新，本次没有覆盖。当前输入仍保留，重新读取后再确认。':reason.message;error.hidden=false;if(reason.status===409)actions.append(button('重新读取，保留当前输入',()=>refresh(ticket)));}
      finally{if(ticket===generation){submitting=false;submit.disabled=false;submit.textContent=retry?'重试调整相关意见':'确认信息并查看报告';}}
    });
    try{
      const caps=await request('/api/review-capabilities');if(ticket!==generation)return;
      if(!window.ReviewModelSelector)throw new Error('模型选择器未能载入，请刷新页面。');
      selector=window.ReviewModelSelector.mount(modelHost,caps.engines||[],()=>{const current=selector?.value();const next=JSON.stringify(current);if(fingerprint!==undefined&&fingerprint!==next)consent.checked=false;fingerprint=next;});
      await selector.ready;if(ticket!==generation)return;fingerprint=JSON.stringify(selector.value());
    }catch(reason){if(ticket===generation)showError(modelSection,'模型服务暂时无法读取：'+reason.message);}
  }
  function open(id,value){close();reportId=id;gate=value;const ticket=generation;render(ticket).catch(error=>{if(ticket===generation)showError(host,error.message);});}
  window.InformationGateUI={open,close,configure(options={}){if(options.onReady)onReady=options.onReady;}};
})();
