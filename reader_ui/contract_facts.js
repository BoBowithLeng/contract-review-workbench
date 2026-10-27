(()=>{
  'use strict';
  const el=(tag,cls='',text)=>{const n=document.createElement(tag);n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  let reportId='',state={revision:0,facts:[],questions:[]},generation=0,onDiscuss=()=>{},report=null,textContext={};
  const confirmed=new Set();
  function receive(value){state=value;for(const entry of value.confirmations||[]){const candidate=entry.candidate,source=candidate?.source;if(candidate?.id&&source)confirmed.add([source.session_id,source.turn_id,candidate.id].join(':'));}}
  const activeFacts=()=>state.facts.filter(fact=>fact.active!==false);
  async function request(path,payload){const response=await fetch(path,payload===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const value=await response.json();if(!response.ok){const error=new Error(value.error||'合同信息未能保存。');error.status=response.status;throw error;}return value.facts;}
  const endpoint=()=>'/api/reports/'+encodeURIComponent(reportId)+'/facts';
  function action(text,handler){const b=el('button','fact-action',text);b.type='button';if(handler)b.addEventListener('click',handler);return b;}
  function source(target,value,shown=''){if(!value?.quote||value.quote.trim()===shown.trim())return;const detail=el('details','fact-source');detail.append(el('summary','','原话出处'),el('blockquote','',value.quote));target.append(detail);}
  const findingIds=q=>[...new Set([...(q.finding_ids||[]),q.finding_id].filter(Boolean))];
  const questionsForFinding=id=>(state.questions||[]).filter(q=>q.status!=='answered'&&findingIds(q).includes(id));
  const readable=value=>window.ReaderText?.humanize(value,textContext,{plain:true,shortLocations:true,hideUnknown:true})||value;
  const explicitlyUnknown=q=>q.status==='unknown'&&(q.response_recorded===true||(q.response_recorded!==false&&q.source?.kind==='preparation'));
  function discussionLinks(target,q){for(const id of findingIds(q)){const finding=report?.findings?.find(f=>f.id===id);target.append(action(finding?'讨论：'+readable(finding.title):'讨论',()=>onDiscuss(id)));}}
  function focusQuestion(id){const q=(state.questions||[]).find(q=>q.id===id);const entry=document.getElementById('fact-question-'+id)||document.getElementById('contract-fact-'+q?.fact_id);entry?.scrollIntoView({block:'center',behavior:'smooth'});entry?.focus({preventScroll:true});}
  function render(){
    const host=document.getElementById('review-contract-facts');if(!host)return;
    host.replaceChildren();const pane=el('section','contract-facts');pane.append(el('h2','','审查信息'));
    const applied=report?.metadata?.post_review_information;
    const currentApplied=Number.isInteger(applied?.facts_revision)&&state.revision===applied.facts_revision;
    if(currentApplied&&applied.affected_finding_ids?.length)pane.append(el('p','fact-update-note','这份报告的相关意见已根据这些补充信息调整。'));
    else if(state.has_updates_since_review&&!currentApplied)pane.append(el('p','fact-update-note','信息已保存，后续讨论会用到；还没有按这些信息复审全文。'));
    pane.append(el('h3','','已提供的信息'));
    if(!activeFacts().length)pane.append(el('p','contract-facts-empty','尚未提供补充信息。'));
    async function save(path,payload,error,done){const ticket=generation;try{const value=await request(endpoint()+path,{expected_revision:state.revision,...payload});if(ticket!==generation)return;receive(value);done?.();render();}catch(reason){if(ticket!==generation)return;if(reason.status===409){try{receive(await request(endpoint()));}catch{}error.textContent='信息已在另一处更新，本次没有覆盖，当前输入仍保留。请核对后再保存。';}else error.textContent=reason.message;error.hidden=false;}}
    function editor(target,text='',replaceId){
      const form=el('form','fact-editor'),input=el('textarea');input.value=text;input.maxLength=8000;input.setAttribute('aria-label',replaceId?'更正信息':'新增信息');
      const error=el('p','contract-facts-error');error.hidden=true;const submit=action('保存信息');submit.type='submit';form.append(input,submit,action('取消',()=>form.remove()),error);
      form.addEventListener('submit',async event=>{event.preventDefault();if(submit.disabled)return;if(!input.value.trim()){error.textContent='请填写信息内容。';error.hidden=false;return;}submit.disabled=true;await save('/save',{text:input.value.trim(),...(replaceId?{replace_id:replaceId}:{})},error);submit.disabled=false;});target.append(form);input.focus();
    }
    for(const fact of activeFacts()){
      const entry=el('section','contract-fact');entry.id='contract-fact-'+fact.id;entry.tabIndex=-1;const question=(state.questions||[]).find(q=>q.fact_id===fact.id);const prefix=question?question.question+'\n答：':'';const shown=prefix&&fact.text.startsWith(prefix)?fact.text.slice(prefix.length).trim():fact.text;if(question){entry.append(el('h4','',readable(question.question)),el('p','fact-question-status','已回答'));}entry.append(el('p','',shown));source(entry,fact.source,shown);const controls=el('div','fact-actions'),error=el('p','contract-facts-error');error.hidden=true;
      controls.append(action('编辑更正',()=>{if(!entry.querySelector('.fact-editor'))editor(entry,shown,fact.id);}),action('撤回',async event=>{const b=event?.target;if(b)b.disabled=true;await save('/withdraw',{fact_id:fact.id},error);if(b)b.disabled=false;}));if(question)discussionLinks(controls,question);entry.append(controls,error);pane.append(entry);
    }
    const addHost=el('div');addHost.append(action('＋ 新增信息',()=>{if(!addHost.querySelector('.fact-editor'))editor(addHost);}));pane.append(addHost,el('h3','','待补充的问题'));
    const questions=(state.questions||[]).filter(q=>q.status!=='answered');if(!questions.length)pane.append(el('p','contract-facts-empty','当前没有待补充的问题。'));
    const ordered=[...questions.filter(q=>!explicitlyUnknown(q)),...questions.filter(explicitlyUnknown)];let unknownHeading=false;
    for(const q of ordered){
      if(explicitlyUnknown(q)&&!unknownHeading){pane.append(el('h3','','暂不能提供的信息'));unknownHeading=true;}
      const entry=el('section','contract-question');entry.id='fact-question-'+q.id;entry.tabIndex=-1;entry.append(el('h4','',readable(q.question)));if(q.reason){const detail=el('details','fact-question-why');detail.append(el('summary','','为什么需要这条信息'),el('p','fact-question-reason',readable(q.reason)));entry.append(detail);}entry.append(el('p','fact-question-status',explicitlyUnknown(q)?'暂不能提供':'尚未回答'));source(entry,q.source);
      const form=el('form','fact-answer-form'),answer=el('textarea');answer.value='';answer.maxLength=8000;answer.setAttribute('aria-label',q.question);
      const unknown=el('input');unknown.type='checkbox';unknown.checked=explicitlyUnknown(q);answer.disabled=unknown.checked;unknown.addEventListener('change',()=>{answer.disabled=unknown.checked;});const label=el('label','fact-unknown');label.append(unknown,el('span','','暂不能提供'));
      const submit=action('保存回答');submit.type='submit';const error=el('p','contract-facts-error');error.hidden=true;form.append(answer,label,submit,error);
      form.addEventListener('submit',async event=>{event.preventDefault();if(submit.disabled)return;if(!unknown.checked&&!answer.value.trim()){error.textContent='请填写回答，或选择暂时无法提供。';error.hidden=false;return;}submit.disabled=true;await save('/answer',{question_id:q.id,answer:unknown.checked?'':answer.value.trim(),status:unknown.checked?'unknown':'answered'},error);submit.disabled=false;});entry.append(form);
      discussionLinks(entry,q);pane.append(entry);
    }
    host.append(pane);
  }
  async function load(bundle){
    reportId=bundle.id;report=bundle.report;const ticket=++generation;confirmed.clear();state={revision:0,facts:[],questions:[]};
    // Frozen so ReaderText reuses one label table for every fact and question on this report.
    textContext={locations:Object.freeze(Object.fromEntries((bundle.paragraphs||[]).map(p=>[p.id,p.location||p.text||'对应原文']))),inline_locations:Object.freeze(Object.fromEntries((bundle.paragraphs||[]).map(p=>[p.id,window.ReaderText?.shortReference(p)||'对应原文'])))};
    try{const value=await request(endpoint());if(ticket!==generation)return;receive(value);render();}
    catch(error){if(ticket!==generation)return;const host=document.getElementById('review-contract-facts');if(host){host.replaceChildren(el('p','contract-facts-error','审查信息暂时无法读取：'+error.message));host.hidden=false;}}
  }
  function renderCandidates(target,turn,context){
    const candidates=(Array.isArray(turn.fact_candidates)?turn.fact_candidates:[]).filter(candidate=>candidate&&typeof candidate.id==='string'&&typeof candidate.text==='string'&&candidate.text.trim()&&typeof (candidate.source_quote||candidate.source?.quote)==='string'&&(candidate.source_quote||candidate.source?.quote).trim());
    if(!candidates.length||context.reportId!==reportId)return;
    const wrap=el('section','fact-candidates');wrap.append(el('h4','','本轮提出的合同信息'));
    for(const candidate of candidates){
      const section=el('section','fact-candidate');section.append(el('p','fact-candidate-text',candidate.text),el('blockquote','fact-candidate-quote','原话：'+(candidate.source_quote||candidate.source?.quote||'')));
      const label=el('label','fact-replacement','保存方式'),select=el('select');select.setAttribute('aria-label','新增信息或更正已有信息');const fresh=el('option','','新增一条合同信息');fresh.value='';select.append(fresh);
      for(const fact of activeFacts()){const option=el('option','','更正：'+fact.text);option.value=fact.id;select.append(option);}select.value='';label.append(select);section.append(label);
      const note=el('p','fact-candidate-note',candidate.kind==='correction'?'请选择要更正的旧信息。更正后会保留旧值的记录，后续审查使用新信息。':'确认后才会保存；也可以选择更正已有的信息。');section.append(note);
      const submit=el('button','fact-confirm','确认保存信息');submit.type='button';
      const key=[context.sessionId,turn.id,candidate.id].join(':');
      if(confirmed.has(key)){submit.textContent='已保存到本合同信息';submit.disabled=true;select.disabled=true;note.textContent='这条信息已保存，可在“审查信息”中查看。';}
      select.addEventListener('change',()=>{submit.disabled=candidate.kind==='correction'&&!select.value||confirmed.has(key);});
      if(candidate.kind==='correction'&&!confirmed.has(key))submit.disabled=true;
      const error=el('p','contract-facts-error');error.hidden=true;
      submit.addEventListener('click',async()=>{
        if(submit.disabled)return;submit.disabled=true;error.hidden=true;const ticket=generation;
        try{const payload={expected_revision:state.revision,finding_id:context.findingId,session_id:context.sessionId,turn_id:turn.id,candidate_id:candidate.id};if(select.value)payload.replace_id=select.value;const value=await request(endpoint()+'/confirm',payload);if(ticket!==generation)return;state=value;confirmed.add(key);render();submit.textContent='已保存到本合同信息';select.disabled=true;note.textContent='信息已保存，后续讨论会用到；还没有按这些信息复审全文。';}
        catch(reason){if(ticket!==generation)return;if(reason.status===409){try{state=await request(endpoint());render();}catch{}error.textContent='合同信息已在另一处修改，这次没有覆盖。请重新选择要更正的信息后再确认。';select.replaceChildren(fresh);for(const fact of activeFacts()){const option=el('option','','更正：'+fact.text);option.value=fact.id;select.append(option);}select.value='';}else error.textContent=reason.message;error.hidden=false;}
        finally{if(ticket===generation)submit.disabled=confirmed.has(key)||candidate.kind==='correction'&&!select.value;}
      });
      section.append(submit,error);wrap.append(section);
    }
    target.append(wrap);
  }
  window.ContractFactsUI={load,renderCandidates,activeFacts,questionsForFinding,focusQuestion,configure(options={}){onDiscuss=options.onDiscuss||onDiscuss;}};
})();
