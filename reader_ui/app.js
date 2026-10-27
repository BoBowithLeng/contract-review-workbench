'use strict';
const $ = id => document.getElementById(id);
const clone = value => JSON.parse(JSON.stringify(value));
const riskClass = risk => ({高:'high',中:'medium',低:'low'}[risk] || 'neutral');
const riskWeight = risk => ({高:3,中:2,低:1}[risk] || 0);
let bundle=null, mode='findings', selected=null, busy=false, toastTimer, checklistReturn=false;
const state = () => bundle.state;
const isInformation = item => !displayRisk(item)||displayRisk(item)==='待判断';
const items = () => ['facts','checklists'].includes(mode)?[]:mode==='findings' ? [...bundle.report.findings.filter(item=>!isInformation(item)),...bundle.report.findings.filter(isInformation)] : mode==='options' ? bundle.report.options : bundle.completeness;
const active = () => items().find(item=>item.id===selected) || items()[0];
const finding = id => bundle.report.findings.find(item=>item.id===id);
const findingNumber = id => bundle.report.findings.findIndex(item=>item.id===id)+1;
const optionNumber = id => bundle.report.options.findIndex(item=>item.id===id)+1;
const optionLabel = option => option.id+' '+readable(option.title);
const partySummary = context => {
  const role={client:'客户',provider:'服务商',employer:'用人单位',employee:'劳动者'}[context?.our_role]||context?.our_role_label||(context?.contract_type==='general'?context.our_role:'')||'';
  return [role,context?.our_party].filter(Boolean).join('·');
};
const displayRisk = item => {
  const review=state().reviews[item.id];
  const applies=review?.decision==='修改'&&(!item.opinion_revision||review.opinion_revision===item.opinion_revision);
  return applies ? (review.risk==='待判断'?null:review.risk) : item.risk;
};

function node(tag,cls,text) {
  const e=document.createElement(tag);
  if(cls)e.className=cls;
  if(text!==undefined)e.textContent=text;
  return e;
}
function button(text,cls,action) {
  const e=node('button',cls,text); e.type='button'; e.onclick=action; return e;
}
function notify(text) {
  $('toast').textContent=text; $('toast').classList.add('show');
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').classList.remove('show'),3200);
}
function showError(text) { $('error').textContent=text; $('error').hidden=!text; }
function setBusy(value) {
  busy=value;
  ['prev','next','export','undo','rereview'].forEach(id=>$(id).disabled=value||!bundle);
  document.querySelectorAll('#review-card button,#review-card input,#review-card select,#review-card textarea')
    .forEach(e=>e.disabled=value||e.dataset.unavailable==='true');
  if(bundle&&!value) {
    const i=items().findIndex(item=>item.id===selected);
    $('prev').disabled=i<=0; $('next').disabled=i<0||i>=items().length-1;
    $('undo').disabled=!state().can_undo;
  }
}
async function request(url,options) {
  const r=await fetch(url,{cache:'no-store',...options}); let data;
  try { data=await r.json(); } catch { throw new Error('没能读取结果，请刷新后重试。'); }
  if(!r.ok) { const error=new Error(data.error||'操作未完成'); error.status=r.status;error.code=data.code;error.information_gate=data.information_gate; throw error; }
  return data;
}
async function loadReport(id,{historyMode='push',clearIssue=false}={}) {
  if(busy)return false;
  DiscussionUI.close();
  setBusy(true); showError('');
  try {
    const loaded=await request('/api/reports/'+encodeURIComponent(id));
    if(loaded.information_gate?.enabled&&loaded.information_gate.status!=='ready')return openInformationGate(id,loaded.information_gate,{historyMode});
    window.InformationGateUI?.close();
    bundle=loaded;
    await Promise.all([DeliveryUI.load(bundle), window.ContractFactsUI?.load(bundle)]);
    window.RereviewUI?.load(bundle);
    mode='findings'; checklistReturn=false;
    // The server sorts display order by source position; IDs remain stable.
    const preferred=clearIssue?null:new URLSearchParams(location.search).get('issue');
    selected=bundle.report.findings.find(item=>item.id===preferred||item.rule_id===preferred)?.id;
    WorkbenchUI.enter(bundle);
    renderAll(true);
    const url=new URL(location);url.searchParams.set('report',id);if(clearIssue)url.searchParams.delete('issue');
    if(historyMode==='replace')history.replaceState(null,'',url);
    else if(historyMode==='push'&&location.href!==url.href)history.pushState(null,'',url);
    return true;
  } catch(e) {
    if(e.code==='information_gate_required'&&e.information_gate)return openInformationGate(id,e.information_gate,{historyMode});
    showError(e.message);
    if(WorkbenchUI.isHome()){$('workbench-error').textContent=e.message;$('workbench-error').hidden=false;}
    return false;
  }
  finally { setBusy(false); }
}
function openInformationGate(id,gate,{historyMode='push'}={}){
  bundle=null;selected=null;
  WorkbenchUI.enterInformationGate?.({id});
  const url=new URL(location);url.searchParams.set('report',id);url.searchParams.delete('issue');
  if(historyMode==='replace')history.replaceState(null,'',url);
  else if(historyMode==='push'&&location.href!==url.href)history.pushState(null,'',url);
  window.InformationGateUI.open(id,gate);return true;
}
async function persist(reviews,selectedOptions,message) {
  if(busy)return false;
  setBusy(true); showError('');
  try {
    const result=await request('/api/reports/'+bundle.id+'/state',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({expected_revision:state().revision,reviews,selected_options:selectedOptions})
    });
    bundle.state=result.state; renderAll(false); notify(message); return true;
  } catch(e) {
    showError(e.status===409?'另一处修改了这份记录，请刷新后再操作；本次未覆盖已有修改。':e.message);
    return false;
  } finally { setBusy(false); }
}
function refsFor(item=active()) {
  if(!item)return [];
  return mode==='options' ? item.finding_ids.flatMap(id=>finding(id)?.refs||[]) : item.refs||[];
}
function locationFor(refs) {
  const p=bundle.paragraphs.find(p=>refs.some(ref=>ref.id===p.id));
  return p ? p.location.replaceAll(' / ',' · ') : '未定位到原句';
}
function addDetail(container,title,fill) {
  const d=node('details'),body=node('div','detail-body');
  d.append(node('summary','',title)); fill(body); d.append(body); container.append(d); return d;
}
function addSources(container,ids,evidence) {
  const list=node('div','sources');
  for(const id of ids||[]) {
    const s=bundle.sources.find(s=>s.id===id); if(!s)continue;
    const record=evidence?.origin==='frozen_source_records'?evidence.sources?.find(source=>source.id===id):null;
    const sourceNote=window.ReaderText?.sourceNote(record||s,Boolean(record))||'';
    const link=node('a','',[s.title,s.article].filter(Boolean).join(' · ')+' ↗'+sourceNote);
    try { const u=new URL(s.url); if(['https:','http:'].includes(u.protocol)) {
      link.href=u.href; link.target='_blank'; link.rel='noopener';
    }} catch { /* Invalid source URLs remain plain text. */ }
    list.append(link);
  }
  container.append(list);
}
function navigate(nextMode,id,scroll=true) {
  if(busy||!bundle)return;
  if(nextMode!==mode||id!==selected)DiscussionUI.close();
  mode=nextMode; selected=id; $('issue-list').hidden=true;
  $('list-toggle').setAttribute('aria-expanded','false'); showError(''); renderAll(scroll);
  if(nextMode==='findings'&&selected){
    const url=new URL(location.href);url.searchParams.set('issue',selected);history.replaceState(null,'',url);
  }
  if(scroll&&nextMode==='options'&&window.matchMedia('(max-width: 920px)').matches){
    requestAnimationFrame(()=>$('review-card').scrollIntoView({block:'start',behavior:'instant'}));
  }
}
function researchSummary(research){
  if(!research||!['partial','error','no_results'].includes(research.status))return '';
  const count=Number.isInteger(research.source_count)?research.source_count:0;
  const retained=count>0?'已保留 '+count+' 条资料链接，供本次审查参考。':'本次未取得新的资料链接，审查使用已有材料。';
  if(research.status==='no_results')return retained;
  if(research.memo_truncated)return '检索说明过长，本次只保留了前一部分。'+retained;
  const d=research.diagnostics;
  if(!d)return '本次联网检索未完整完成，具体原因未记录。'+retained;
  const reasons=d.partial_reasons||[],codes=d.error_codes||[],last=(d.stages||[]).at(-1);
  let reason='联网检索没有完整结束。';
  if(codes.includes('max_uses_exceeded'))reason='搜索服务返回次数上限提示，有些搜索未能执行。';
  else if(last?.stop_reason==='max_tokens'&&!d.answer_complete)reason='检索结果的整理内容达到输出长度上限，尚未完整返回。';
  else if(reasons.includes('continuation_failed'))reason='检索的后续处理没有完成。';
  else if(reasons.includes('request_failed'))reason='这次联网请求没有完成。';
  else if(reasons.includes('missing_tool_result')||reasons.includes('unmatched_tool_result'))reason='有些搜索请求没有收到可对应的结果。';
  else if(reasons.includes('tool_error'))reason='有些搜索未能完成。';
  else if(reasons.includes('missing_final_text'))reason='搜索已返回，检索说明还没有生成完整。';
  return reason+retained;
}
function renderAll(scroll) {
  if(!items().some(item=>item.id===selected))selected=items()[0]?.id;
  for(const name of ['findings','checklists','facts','options','basics'])$('tab-'+name).classList.toggle('current',mode===name);
  $('findings-count').textContent=bundle.report.findings.length;
  $('options-count').textContent=bundle.report.options.length;
  const general=bundle.report.context?.contract_type==='general';
  const research=bundle.report.metadata?.research;
  const researchText=researchSummary(research);
  $('review-research-info').textContent=researchText;$('review-research-info').hidden=!general||!researchText;
  $('checklists-count').textContent=window.ChecklistsUI?.resultRows(bundle.report).length||'';
  $('tab-basics').hidden=(general||bundle.report.context?.contract_type==='software_service')&&!bundle.completeness.length;
  $('reader-workspace').hidden=['checklists','facts'].includes(mode);
  $('review-checklist-results').hidden=mode!=='checklists';
  $('review-contract-facts').hidden=mode!=='facts';
  if(mode==='checklists'){window.ChecklistsUI?.renderResults($('review-checklist-results'),bundle.report,id=>{checklistReturn=true;navigate('findings',id);},{reportId:bundle.id,textContext:readableContext(),onParagraph:(id,findingIds)=>{
    const related=bundle.report.findings.filter(f=>findingIds.includes(f.id));
    const target=related.find(f=>f.refs?.some(ref=>ref.id===id))||related[0]||bundle.report.findings.find(f=>f.refs?.some(ref=>ref.id===id));
    checklistReturn=true;navigate('findings',target?.id||selected,false);scrollParagraph(id);
  }});DeliveryUI.count();return;}
  $('basics-count').textContent=general&&!bundle.completeness.length?'随意见检查':bundle.report.context?.contract_type==='software_service'&&!bundle.completeness.length
    ? '未检查' : bundle.completeness.filter(i=>i.status!=='已找到').length+' 待确认';
  $('save-status').textContent='已保存';
  $('basic-info').hidden=mode!=='basics';
  const basicInfo=bundle.report.basic_info||bundle.report.metadata?.basic_info;
  const basicText=typeof basicInfo==='string' ? basicInfo : basicInfo?.description||basicInfo?.text;
  const service=bundle.report.context?.contract_type==='software_service'||general;
  $('basic-info').replaceChildren(node('p','',readable(basicText)||(service?'这里检查服务合同需要写明的基本内容，检查范围与劳动合同不同。':'这里只检查基本内容是否写入、是否留空；“已找到”的条款仍需核对是否合法、是否完整。')));
  if(Array.isArray(basicInfo?.source_ids))addSources($('basic-info'),basicInfo.source_ids);
  else if(!service)addSources($('basic-info'),['LCL17']);
  $('review-contract-facts').hidden=mode!=='facts';
  $('review-card').hidden=mode==='facts';
  $('review-research-info').hidden=mode!=='findings'||!general||!researchText;
  renderDocument(); renderNavigation(); if(mode!=='facts')renderCard();
  if(scroll){scrollToActive();$('rail').scrollTop=0;}
  DeliveryUI.count();
}
function undoDescription(action) {
  if(!action)return '上次保存的复核记录';
  if(action.kind==='review') {
    const verb={接受:'采纳',修改:'编辑',驳回:'不采用',未复核:'重置'}[action.decision]||'更新';
    return '对第'+findingNumber(action.finding_id)+'条意见的'+verb;
  }
  if(action.kind==='option')return (action.selected?'选择':'取消')+'方案'+optionNumber(action.option_id);
  return action.label||'上次保存的多项修改';
}
function renderNavigation() {
  const list=items(),index=list.findIndex(i=>i.id===selected);
  $('counter').textContent=list.length?(index+1)+' / '+list.length:'0 / 0';
  $('prev').disabled=busy||index<=0; $('next').disabled=busy||index<0||index>=list.length-1;
  $('list-toggle').hidden=mode==='facts';
  $('counter').hidden=mode==='facts';
  $('prev').hidden=$('next').hidden=mode==='facts';
  $('list-toggle').textContent={findings:'全部意见 ▾',options:'全部方案 ▾',basics:'全部基本项 ▾'}[mode];
  $('issue-list').replaceChildren();
  let informationHeading=false;
  list.forEach((item,i)=>{
    if(mode==='findings'&&isInformation(item)&&!informationHeading){$('issue-list').append(node('h4','information-group-heading','待确认信息与建议'));informationHeading=true;}
    const b=button('',item.id===selected?'current':'',()=>navigate(mode,item.id));
    if(mode==='findings')b.append(node('span','badge '+riskClass(displayRisk(item))));
    b.append(document.createTextNode(mode==='options'?optionLabel(item):(mode==='findings'?findingNumber(item.id):i+1)+'. '+readable(displayTitle(item),item))); $('issue-list').append(b);
  });
  const done=Object.values(state().reviews).filter(r=>r.decision!=='未复核').length;
  $('progress').textContent='已复核 '+done+' / '+bundle.report.findings.length;
  $('undo').hidden=true; $('undo').disabled=busy||!state().can_undo;
  $('undo').textContent='撤销：'+undoDescription(state().undo_action);
  $('undo').title='恢复这次操作之前保存的意见或方案选择';
}
// Keep overlapping quotes and selection distinct from their risk colour.
function markText(element,text,ranges) {
  const points=[...new Set([0,text.length,...ranges.flatMap(r=>[r.start,r.end])])].sort((a,b)=>a-b);
  for(let i=0;i<points.length-1;i++) {
    const start=points[i],end=points[i+1],covering=ranges.filter(r=>r.start<=start&&r.end>=end);
    if(!covering.length) { element.append(document.createTextNode(text.slice(start,end))); continue; }
    const best=[...covering].sort((a,b)=>Number(b.selected)-Number(a.selected)||riskWeight(b.risk)-riskWeight(a.risk))[0];
    const mark=node('mark',riskClass(best.risk)+(covering.some(r=>r.selected)?' focused':''),text.slice(start,end));
    mark.title=best.risk?best.risk+'风险':'定位文字 · 未标风险等级'; element.append(mark);
  }
}
function renderDocument() {
  const paper=$('paper'),beforeScroll=$('document-area').scrollTop,refs=refsFor(),byRef=new Map();
  paper.replaceChildren();
  bundle.report.findings.forEach((item,index)=>{ for(const ref of item.refs) {
    if(!byRef.has(ref.id))byRef.set(ref.id,[]);
    byRef.get(ref.id).push({item,index:index+1,quote:ref.quote});
  }});
  let table,tableKey,rows=new Map(),cells=new Map(),first=true;
  const outline=$('outline'); outline.replaceChildren();const headingDepth=new Map();
  for(const p of bundle.paragraphs) {
    if(!p.text.trim())continue;
    const block=node('div','paragraph-block'); block.id='p-'+p.id;
    const related=byRef.get(p.id)||[],specific=refs.filter(r=>r.id===p.id);
    if(related.length)block.classList.add('has-issue');
    if(specific.length)block.classList.add('selected');
    let type='clause';
    if(p.level===1)type='chapter'; else if(p.level>1)type='subheading';
    else if(first&&p.text.trim().length<38&&/合同|协议/.test(p.text))type='body-title';
    const content=node(type==='chapter'?'h2':'p',type),ranges=[];
    for(const r of related) {
      const at=p.text.indexOf(r.quote);
      if(r.quote&&at>=0)ranges.push({start:at,end:at+r.quote.length,risk:displayRisk(r.item),
        selected:mode!=='basics'&&specific.some(ref=>ref.quote===r.quote)});
    }
    if(mode==='basics')for(const ref of specific) {
      const at=p.text.indexOf(ref.quote);
      if(ref.quote&&at>=0)ranges.push({start:at,end:at+ref.quote.length,risk:null,selected:true});
    }
    if(p.numbering_label)content.append(node('span','word-numbering-label',p.numbering_label+' '));
    markText(content,p.text,ranges);
    if(related.length) {
      const anchors=node('div','anchors');
      for(const r of related.filter((r,i,all)=>all.findIndex(other=>other.item.id===r.item.id)===i)) {
        const shownRisk=displayRisk(r.item);
        const label='第'+r.index+'条意见 · '+(shownRisk?shownRisk+'风险':'未分级');
        const a=button(String(r.index),'anchor '+riskClass(shownRisk),e=>{
          e.stopPropagation(); navigate('findings',r.item.id,false);
        });
        a.setAttribute('aria-label',label); a.title=label; anchors.append(a);
      }
      block.append(anchors);
      block.onclick=()=>navigate('findings',(related.find(r=>r.item.id===selected)||related[0]).item.id,false);
    }
    block.append(content);
    const m=/^(T\d+)R(\d+)C(\d+)P\d+$/.exec(p.id);
    if(m) {
      if(tableKey!==m[1]) { table=node('table'); paper.append(table); tableKey=m[1]; rows=new Map(); cells=new Map(); }
      if(!rows.has(m[2])) { const row=node('tr'); rows.set(m[2],row); table.append(row); }
      const key=m[2]+'-'+m[3];
      if(!cells.has(key)) { const cell=node('td'); cells.set(key,cell); rows.get(m[2]).append(cell); }
      cells.get(key).append(block);
    } else { tableKey=null; paper.append(block); }
    if(p.level){
      const depth=Object.hasOwn(p,'heading_parent')?(p.heading_parent?(headingDepth.get(p.heading_parent)||0)+1:0):Math.max(0,p.level-1);headingDepth.set(p.id,depth);
      const entry=button((p.display_text||p.text).trim(),depth>0?'nested':'',()=>{
        scrollParagraph(p.id); outline.hidden=true; $('outline-toggle').setAttribute('aria-expanded','false');
      });
      entry.style.paddingLeft=(12+Math.min(depth,8)*16)+'px';outline.append(entry);
    }
    first=false;
  }
  $('outline-toggle').disabled=!outline.children.length;
  $('outline-toggle').title=outline.children.length?'查看原稿目录':'原稿未设置可用目录';
  if(!outline.children.length){outline.hidden=true;$('outline-toggle').setAttribute('aria-expanded','false');}
  paper.append(node('footer','paper-foot','合同原稿 · 意见复核记录另存到报告中，原文件不变'));
  $('document-area').scrollTop=beforeScroll;
  DeliveryUI.decorateDocument();
}
function scrollParagraph(id) {
  const e=$('p-'+id),area=$('document-area'); if(!e)return;
  area.scrollTop+=e.getBoundingClientRect().top-area.getBoundingClientRect().top-Math.max(52,(area.clientHeight-e.clientHeight)/3);
}
function scrollToActive() { const ref=refsFor()[0]; if(ref)scrollParagraph(ref.id); }

function renderCard() {
  const card=$('review-card');card.replaceChildren();const item=active();
  if(!item){const empty=node('div','empty-card');empty.append(node('h2','',mode==='basics'?'本次未运行基本内容检查':mode==='options'?'本次没有其他处理办法':'本次没有列出重点问题'));card.append(empty);return;}
  if(mode==='options'){renderOption(card,item);return;}
  const basic=mode==='basics',review=state().reviews[item.id],head=node('div','review-header'),meta=node('div','meta');
  const risk=basic?null:displayRisk(item),status=basic?item.status:DeliveryUI.statusFor(item);
  meta.append(node('span','risk '+riskClass(risk),basic?item.status:risk?risk+'风险':item.status==='未检出'?'未提示风险':'待判断'));
  if(!basic&&item.status==='处理失败')meta.append(node('span','check-status-light broken','尚未完成检查'));
  if(!basic&&item.issue_kind)meta.append(node('span','subtle-status',item.issue_kind+'问题'));
  if(!basic&&bundle.report.context?.contract_type==='general'){
    const sourcePending=item.evidence?.origin==='frozen_source_records'?['mixed','unverified','none'].includes(item.evidence.source_state):true;
    const pending=item.issue_kind==='法律'&&sourcePending?'依据待核验':item.status==='信息不足'?'待确认事项':'';
    if(pending)meta.append(node('span','subtle-status',pending));
  }
  if(!basic&&status!=='待处理')meta.append(node('span','subtle-status',DeliveryUI.statusLabel(item)));
  if(!basic&&item.revision_action==='withdraw')meta.append(node('span','subtle-status','已撤回'));
  if(!basic&&isInformation(item))head.append(node('p','information-group-heading','待确认信息与建议'));
  if(checklistReturn)head.append(button('← 返回刚才的检查项','checklist-return',()=>navigate('checklists',null,false)));
  head.append(meta,node('h2','',readable(displayTitle(item),item)));
  if(!basic&&item.post_review_update){
    // Says which of the person's answers changed this opinion before the report opened.
    const update=item.post_review_update,note=node('p','post-review-update-note'),asked=update.questions||[];
    note.append(node('strong','','补答后已更新'),'：'+readable(update.reason,item));
    if(asked.length){const basis=node('small','post-review-update-basis','依据你对'+asked.length+'个问题的回答');basis.title=asked.join('\n');note.append(basis);}
    head.append(note);
  }
  const pendingQuestions=!basic?window.ContractFactsUI?.questionsForFinding(item.id)||[]:[];
  if(pendingQuestions.length){
    const notice=node('section','finding-information-notice');notice.append(node('strong','','有'+pendingQuestions.length+'条信息待确认'));
    if(item.draft_plan?.status!=='needs_input'){
      notice.append(node('p','information-question-preview',readable(pendingQuestions[0].question,item)));
      if(pendingQuestions.length>1)notice.append(node('small','','另有'+(pendingQuestions.length-1)+'条，请一并查看。'));
    }
    notice.append(button('查看并回答','finding-information-link',()=>{navigate('facts',null,false);window.ContractFactsUI.focusQuestion(pendingQuestions[0].id);}));head.append(notice);
  }
  if(item.refs?.length&&!basic){
    const refs=node('section','source-location-links');refs.append(node('h3','','相关原文'));
    const unique=item.refs.filter((r,i,all)=>all.findIndex(x=>x.id===r.id)===i);
    const paragraphs=unique.map(ref=>bundle.paragraphs.find(p=>p.id===ref.id)||{id:ref.id,text:ref.quote,location:'对应原文'});
    const labels=paragraphs.map(p=>window.ReaderText.locationParts(p));
    unique.forEach((ref,i)=>{const p=labels[i],link=button('','source-location-item',()=>scrollParagraph(ref.id));
      link.append(node('strong','source-location-title',p.title));if(p.context)link.append(node('span','source-location-context',p.context));
      if(labels.some((other,j)=>j!==i&&other.title===p.title&&other.context===p.context))link.append(node('span','source-location-excerpt','“'+p.excerpt+'”'));
      refs.append(link);
    });head.append(refs);
  }
  if(!item.refs?.length&&item.location_note)head.append(node('p','human-note',readable(item.location_note,item)));
  card.append(head);
  if(basic){renderBasic(card,item);return;}
  if(item.status==='处理失败'){
    const pending=node('section','review-section');pending.append(node('h3','','这项检查尚未完成'),node('p','reason-text','请继续核对这项约定，暂时没有可用的风险结论。'));card.append(pending);return;
  }
  if(item.opinion_revision&&['已修改','已批注'].includes(status))card.append(node('p','human-note','这条意见有了新版。已经确认的改稿和批注仍在，请查看是否也需要调整。'));
  const reason=node('section','review-section');
  reason.append(node('h3','','修改理由'),node('p','reason-text',readableReason(item)));
  if(review?.decision==='修改'&&review.note)reason.append(node('p','human-note',(item.opinion_revision&&review.opinion_revision!==item.opinion_revision?'旧版人工复核（未覆盖新版）：':'复核意见：')+readable(review.note,item)));
  if(item.source_ids?.length){const laws=node('div','legal-links');laws.append(node('h4','',bundle.report.context?.contract_type==='general'?'相关依据与资料':'相关法条'));addSources(laws,item.source_ids,item.evidence);reason.append(laws);}
  card.append(reason);
  if(item.suggestion){const advice=node('section','review-section');advice.append(node('h3','','处理建议'),node('p','reason-text',readable(item.suggestion,item)));card.append(advice);}
  const plan=item.draft_plan;
  const draftText=review?.decision==='修改'&&(!item.opinion_revision||review.opinion_revision===item.opinion_revision)?review.clause_draft:item.clause_draft||
    (plan?.status==='ready'?[...(plan.edits||[]),...(plan.insertions||[])].map(edit=>edit.new_text).join('\n'):'');
  if(plan?.status==='needs_input'){
    const missing=node('section','review-section draft-input-needed');missing.append(node('h3','','改稿前需要补充的信息'));
    for(const question of plan.questions||[])missing.append(node('p','reason-text',readable(typeof question==='string'?question:question.question||question.text,item)));
    if(plan.comment)missing.append(node('p','reason-text',readable(plan.comment,item)));
    if(!plan.questions?.length&&!plan.comment)missing.append(node('p','reason-text','这条意见需要进一步确认信息，请进入讨论核对。'));
    missing.append(node('p','human-note','补充信息后，可在讨论中生成新版意见，再查看改稿。'),button('去补充信息','scope-discuss',()=>{navigate('facts',null,false);if(pendingQuestions[0])window.ContractFactsUI?.focusQuestion(pendingQuestions[0].id);}));appendDraftScope(missing,item,draftText);card.append(missing);
  }
  if(plan?.status==='not_needed'){const unchanged=node('section','review-section');unchanged.append(node('h3','','正文无需修改'));if(plan.comment)unchanged.append(node('p','reason-text',readable(plan.comment,item)));appendDraftScope(unchanged,item,draftText);card.append(unchanged);}
  if(draftText&&(!plan||plan.status==='ready'||status==='已修改')){
    const contexts=DeliveryUI.suggestions(item,draftText);
    const suggestion=node('section','review-section');suggestion.append(node('h3','',contexts.length?'修订建议':'修改建议'));
    if(contexts.length){for(const ctx of contexts){suggestion.append(node('p','draft-location','修改位置：'+ctx.location));const diff=node('div','suggestion-diff');DeliveryUI.renderDiff(diff,ctx.before,ctx.after);suggestion.append(diff);}}
    else suggestion.append(node('div','suggestion-text',readable(draftText,item)));
    appendDraftScope(suggestion,item,draftText);
    DeliveryUI.renderSuggestionScope?.(suggestion,item,draftText);
    const candidateIssue=DeliveryUI.candidateIssue?.(item);if(candidateIssue)suggestion.append(node('p','inline-error',candidateIssue));
    if(!plan&&status!=='已修改'&&DeliveryUI.needsPreparation(item,draftText)&&!DeliveryUI.hasCandidate?.(item,draftText))suggestion.append(node('p','human-note','这条旧建议还没确定具体改哪些文字。点击“整理改稿”后，先选好模型并确认调用，再看改稿。'));
    card.append(suggestion);
    if(DeliveryUI.ensureCandidate){const currentBundle=bundle;DeliveryUI.ensureCandidate(item,draftText).then(changed=>{if(changed&&bundle===currentBundle&&mode==='findings'&&active()?.id===item.id)renderCard();});}
  }
  renderReviewActions(card,item,draftText||'');
  const options=bundle.report.options.filter(option=>option.finding_ids.includes(item.id));
  if(options.length){const more=node('details','other-options');more.append(node('summary','','其他处理办法（'+options.length+'）'));
    for(const option of options){const entry=node('div','reading-option');entry.append(button(optionLabel(option),'option-link',()=>navigate('options',option.id)));more.append(entry);}card.append(more);}
}
function appendDraftScope(target,item,draftText){
  const scope=DeliveryUI.scope?.(item,draftText)||[];if(!scope.length)return;
  const details=node('section','draft-scope');details.append(node('h4','','相关条款怎么处理'));
  for(const row of scope){const entry=node('div','draft-scope-row');entry.append(node('strong','',readable(row.location,item)),node('span','draft-scope-status '+row.action,row.label),node('p','',readable(row.reason,item)));details.append(entry);}
  if(item.draft_plan?.status!=='needs_input'&&scope.some(row=>row.action==='needs_confirmation'))details.append(button('讨论待确认的位置','scope-discuss',()=>DiscussionUI.open(bundle,item)));
  target.append(details);
}
function displayTitle(item){return window.ReaderText?.findingTitle(item,bundle.report)||item.title;}
// Everything except the current finding depends only on the loaded report. A bundle is replaced, never
// edited in place, so build it once per bundle; frozen parts let ReaderText reuse its label tables too.
const readableShared=new WeakMap();
function readableContext(item){
  let shared=readableShared.get(bundle);
  if(!shared){
    const locations=Object.freeze(Object.fromEntries(bundle.paragraphs.map(p=>[p.id,(p.location||p.display_text||p.text||'对应原文').replaceAll(' / ',' · ')])));
    const inline_locations=Object.freeze(Object.fromEntries(bundle.paragraphs.map(p=>[p.id,window.ReaderText?.shortReference(p)||locations[p.id]])));
    const titleContext={locations,inline_locations,sources:bundle.sources};
    const title=f=>window.ReaderText?.humanize(displayTitle(f),titleContext,{plain:true,shortLocations:true,hideUnknown:true})||displayTitle(f);
    shared=Object.freeze({locations,inline_locations,paragraphs:bundle.paragraphs,
      finding_labels:Object.freeze(Object.fromEntries(bundle.report.findings.map(f=>[f.id,'意见“'+title(f)+'”']))),
      option_labels:Object.freeze(Object.fromEntries(bundle.report.options.map(option=>[option.id,option.id+' '+option.title]))),sources:bundle.sources});
    readableShared.set(bundle,shared);
  }
  return {...shared,current_finding_id:item?.id};
}
function readable(value,item){return window.ReaderText?.humanize(value,readableContext(item),{plain:true,shortLocations:true,hideUnknown:true})||String(value||'');}
function readableReason(item){return readable(item.reason,item);}
function renderReviewActions(card,item,draftText) {
  const bar=node('div','review-actionbar'),status=DeliveryUI.statusFor(item),usable=DeliveryUI.available(item);
  const discuss=button('讨论这条','discuss-entry',()=>DiscussionUI.open(bundle,item));
  discuss.dataset.unavailable=String(item.status==='处理失败');discuss.disabled=item.status==='处理失败';
  if(status==='不采用修改建议')bar.append(button('撤销不采用记录','secondary',()=>setIgnored(item,false)));
  if(status==='已修改'||status==='已批注')bar.append(button(DeliveryUI.statusLabel(item)==='已修改并批注'?'撤销修改和批注':status==='已修改'?'撤销本条修改':'撤销批注','ignore-action',()=>DeliveryUI.undoFinding(item)));
  else if(status!=='不采用修改建议')bar.append(button('不采用修改建议','ignore-action',()=>setIgnored(item,true)));
  const split=node('div','split-action');
  const blockedPlan=status!=='已修改'&&item.draft_plan&&item.draft_plan.status!=='ready';
  const needsPreparation=draftText&&status!=='已修改'&&DeliveryUI.needsPreparation(item,draftText);
  const newer=status==='已修改'&&DeliveryUI.hasNewerDraft?.(item);
  const primary=button(newer?'换用新版改稿':status==='已修改'?'编辑改稿':blockedPlan?'仅添加批注':needsPreparation?(DeliveryUI.hasCandidate?.(item,draftText)?'查看已整理改稿':'整理改稿'):draftText?'修改并批注':'添加批注','primary',
    ()=>DeliveryUI.openChange(item,draftText,newer?'edit':status==='已修改'?'custom':blockedPlan?'comment':draftText?'both':'comment'));
  primary.dataset.unavailable=String(!usable);primary.disabled=!usable;
  const menu=node('div','action-menu');menu.hidden=true;
  const toggle=button('▾','action-toggle',()=>{menu.hidden=!menu.hidden;toggle.setAttribute('aria-expanded',String(!menu.hidden));});
  toggle.setAttribute('aria-label','其他处理动作');toggle.setAttribute('aria-expanded','false');
  for(const [label,preset] of [['仅修改','edit'],['仅批注','comment'],['编辑改稿','custom']]){
    if(blockedPlan&&preset!=='comment')continue;
    const entry=button(label,'',()=>{menu.hidden=true;toggle.setAttribute('aria-expanded','false');return DeliveryUI.openChange(item,draftText,preset);});
    entry.disabled=!usable||(!draftText&&preset!=='comment');entry.dataset.unavailable=String(entry.disabled);menu.append(entry);
  }
  split.append(primary,toggle,menu);bar.append(split);card.append(bar);
  card.append(discuss);
}
async function setIgnored(item,ignore) {
  if(busy)return;setBusy(true);showError('');
  try {
    const response=await request('/api/reports/'+bundle.id+'/actions',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:ignore?'ignore':'restore',finding_id:item.id,expected_review_revision:state().revision,expected_delivery_revision:DeliveryUI.revision()})});
    bundle.state=response.review_state;bundle.actions=response.actions;renderAll(false);notify(ignore?'已记录不采用修改建议，风险判断保留':'已撤销不采用记录');
  } catch(error){showError(error.message);}
  finally{setBusy(false);}
}
function renderBasic(card,item) {
  const body=node('div','basic-content');
  if(item.missing_fields?.length) {
    body.append(node('p','','需要填写或确认：')); const list=node('ul');
    item.missing_fields.forEach(field=>list.append(node('li','',field))); body.append(list);
  } else body.append(node('p','',item.status==='已找到'?'已定位到相关文字。':item.note));
  if(item.refs?.length) {
    body.append(node('div','section-label','点击查看原文位置'));
    for(const ref of item.refs)body.append(button(locationFor([ref])+' ↗','basic-ref',()=>scrollParagraph(ref.id)));
  }
  card.append(body);
}
function renderOption(card,option) {
  const head=node('div','review-header');head.append(node('h2','',optionLabel(option)));
  if(option.need_review)head.append(node('p','human-note','相关意见已更新或撤回，这个方案还没有随之调整，请重新核对。'));
  const links=node('div','finding-links');
  [...option.finding_ids].sort((a,b)=>findingNumber(a)-findingNumber(b)).forEach(id=>{
    const f=finding(id);if(!f){links.append(node('p','human-note','相关意见不在本次报告中。'));return;}const link=button('查看意见：'+readable(f.title,f),'finding-chip',()=>navigate('findings',id));links.append(link);
  });
  head.append(links);card.append(head);const body=node('div','plan-body');
  function section(title,values,ordered=false){if(!values.length)return;const part=node('section','plan-section'),list=node(ordered?'ol':'ul');part.append(node('h3','',title));values.forEach(value=>{const line=node('li');appendOptionReferences(line,value);list.append(line);});part.append(list);body.append(part);}
  section('适用条件',option.preconditions||[]);section('怎样处理',option.steps||[],true);
  for(const [title,text] of [['需要付出的成本',option.cost],['仍需考虑的问题',option.remaining_risk],['与其他办法的关系',option.relationship]])if(text){const part=node('section','plan-section'),line=node('p');part.append(node('h3','',title));appendOptionReferences(line,text);part.append(line);body.append(part);}
  if(option.clause_draft){const part=node('section','plan-section');part.append(node('h3','','可参考的条款'));
    const resolved=window.ReaderText?.resolveDrafts(option,bundle.report.findings,state().reviews)||{drafts:[{text:option.clause_draft}]};
    for(const draft of resolved.drafts){if(draft.title)part.append(node('h4','',readable(draft.title,finding(draft.id))));part.append(node('div','suggestion-text',readable(draft.text,finding(draft.id))));}
    if(resolved.referenceOnly&&(!resolved.drafts.length||resolved.missing?.length))part.append(node('p','human-note','这里只引用了相关意见，没有完整的条款改稿。请打开对应意见查看。'));
    body.append(part);
  }
  if(option.source_ids?.length){const part=node('section','plan-section legal-links');part.append(node('h3','','依据'));addSources(part,option.source_ids,option.evidence);body.append(part);}
  card.append(body);
}
function appendOptionReferences(target,text){
  const raw=String(text||''),pattern=/(?<![A-Za-z0-9_])O\d+(?![A-Za-z0-9_])/g;let cursor=0;
  for(const match of raw.matchAll(pattern)){target.append(document.createTextNode(readable(raw.slice(cursor,match.index))));const option=bundle.report.options.find(item=>/^O\d+$/.test(item.id)&&Number(item.id.slice(1))===Number(match[0].slice(1)));
    target.append(option?button(optionLabel(option),'option-reference',()=>navigate('options',option.id)):node('span','unresolved-reference',match[0]+'（本报告无对应方案）'));cursor=match.index+match[0].length;
  }
  target.append(document.createTextNode(readable(raw.slice(cursor))));
}


function editForm(item,decision) {
  if(busy)return;
  const card=$('review-card'); card.querySelector('form')?.remove();
  const previous=state().reviews[item.id]||{},editing=decision==='修改',form=node('form','review-form');
  form.append(node('h3','',editing?'调整风险判断':'不采用修改建议'));
  if(editing)form.append(node('p','form-help','调整风险等级并说明理由，不改动合同正文。'));
  const rank=node('select'); rank.id='review-risk';
  ['高','中','低','待判断'].forEach(value=>rank.add(new Option(value==='待判断'?value:value+'风险',value)));
  rank.value=previous.risk||item.risk||'待判断';
  const note=node('textarea'); note.id='review-note'; note.maxLength=4000; note.value=previous.note||'';
  note.placeholder=editing?'说明你为什么调整这条意见。':'说明为什么不采用修改建议。';
  const draft=node('textarea'); draft.id='review-draft'; draft.rows=6; draft.maxLength=20000;
  draft.value=previous.decision==='修改'?previous.clause_draft:item.clause_draft||'';
  function field(text,input) { const label=node('label','',text); label.htmlFor=input.id; form.append(label,input); }
  if(editing)field('本条风险等级',rank);
  field(editing?'复核说明':'不采用的理由',note);
  const actions=node('div','form-actions'),save=node('button','primary',editing?'保存风险判断':'确认不采用修改建议');
  save.type='submit'; actions.append(save,button('取消编辑','secondary',()=>form.remove())); form.append(actions);
  form.onsubmit=e=>{
    e.preventDefault();
    if(!note.value.trim()) { note.focus(); notify('请填写'+(editing?'复核说明':'不采用的理由')); return; }
    saveDecision(item.id,decision,rank.value,note.value,draft.value);
  };
  card.querySelector('.review-actionbar').after(form); form.scrollIntoView({block:'nearest'});
  note.focus({preventScroll:true});
}
function saveDecision(id,decision,risk,note,draft) {
  const reviews=clone(state().reviews); reviews[id]={decision,risk,note,clause_draft:draft,opinion_revision:finding(id)?.opinion_revision||0};
  const verb={接受:'已采纳',修改:'已保存你的复核意见',驳回:'已记为不采用'}[decision];
  return persist(reviews,[...state().selected_options],'第'+findingNumber(id)+'条意见：'+verb);
}
$('prev').onclick=()=>{const i=items().findIndex(item=>item.id===selected);if(i>0)navigate(mode,items()[i-1].id);};
$('next').onclick=()=>{const i=items().findIndex(item=>item.id===selected);if(i<items().length-1)navigate(mode,items()[i+1].id);};
for(const name of ['findings','checklists','facts','options','basics'])$('tab-'+name).onclick=()=>navigate(name,null);
window.ContractFactsUI?.configure({onDiscuss:id=>{const item=finding(id);if(!item)return;navigate('findings',id);DiscussionUI.open(bundle,item);}});
window.InformationGateUI?.configure({onReady:async id=>{if(!await loadReport(id,{historyMode:'replace',clearIssue:true}))throw new Error('信息处理完成，但报告未能打开。请重试打开报告。');await refreshReportList();}});
$('list-toggle').onclick=()=>{$('issue-list').hidden=!$('issue-list').hidden;$('list-toggle').setAttribute('aria-expanded',String(!$('issue-list').hidden));};
$('outline-toggle').onclick=()=>{$('outline').hidden=!$('outline').hidden;$('outline-toggle').setAttribute('aria-expanded',String(!$('outline').hidden));};
$('undo').onclick=async()=>{
  if(busy)return;
  const description=undoDescription(state().undo_action);setBusy(true);showError('');
  try {
    const result=await request('/api/reports/'+bundle.id+'/undo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:state().revision})});
    bundle.state=result.state;renderAll(false);notify('已撤销'+description);
  } catch(e){showError(e.message);} finally{setBusy(false);}
};
$('export').onclick=()=>DeliveryUI.openPanel();
let resizeTimer;
window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(bundle&&!WorkbenchUI.isHome())scrollToActive();},100);});
window.addEventListener('reader:resize',()=>{if(bundle)scrollToActive();});
setBusy(false);
async function refreshReportList() {
  return WorkbenchUI.refresh();
}
WorkbenchUI.configure({openReport:loadReport,openJob:id=>ReviewJobsUI.openJob(id),isBusy:()=>busy});
ReviewJobsUI.configure({onComplete:async(id,completion={})=>{
  const previousBundle=bundle,previousUrl=location.href;
  await refreshReportList();
  if(completion.automatic&&(!completion.canNavigate?.()||bundle!==previousBundle||location.href!==previousUrl)){
    const notice=$('background-review-complete');notice.replaceChildren();
    notice.append(node('strong','','审查已完成'),node('span','',completion.documentName||'另一份合同的审查结果已生成。'));
    notice.append(button('查看结果','primary',async()=>{if(await loadReport(id,{clearIssue:true}))notice.hidden=true;}),button('稍后查看','secondary',()=>{notice.hidden=true;}));notice.hidden=false;
    return false;
  }
  // A partially validated report lives under pending_jobs, not allReviews.
  // Its report endpoint decides whether the saved result can be opened.
  if(!await loadReport(id,{clearIssue:true}))throw new Error('结果已生成，阅读台暂时未能打开；请返回工作台打开记录。');
  return true;
}});
OpinionRevisions.configure({onAccepted:async({reportId,findingId})=>{
  if(!bundle||bundle.id!==reportId||mode!=='findings'||selected!==findingId)return;
  const latest=await request('/api/reports/'+encodeURIComponent(reportId));
  if(bundle.id!==reportId||mode!=='findings'||selected!==findingId)return;
  bundle=latest;
  await DeliveryUI.load(bundle);
  renderAll(false);
  await DiscussionUI.refresh();
}});
(async()=>{
  const requested=new URLSearchParams(location.search).get('report');
  try {
    if(requested){
      await loadReport(requested,{historyMode:'replace'});
      refreshReportList().catch(()=>{});
    }else{
      await refreshReportList();
      await WorkbenchUI.showHome({push:false,refreshList:false});
    }
  }catch(e){showError(e.message);}
})();
window.addEventListener('popstate',async()=>{
  if(busy){notify('正在处理当前操作，请稍后切换。');return;}
  const requested=new URLSearchParams(location.search).get('report');
  if(requested)await loadReport(requested,{historyMode:'none'});
  else await WorkbenchUI.showHome({push:false});
});
