'use strict';
window.WorkbenchUI=(()=>{
  let data={documents:[],pending_jobs:[],pending_deletions:[]},openReport=async()=>false,openJob=()=>{},isBusy=()=>false,loading=false;
  const $=id=>document.getElementById(id);
  const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;};
  const button=(text,cls,action)=>{const e=el('button',cls,text);e.type='button';e.onclick=action;return e;};
  const cleanName=value=>String(value||'未命名合同').replace(/\.(docx?|txt)$/i,'');
  const home=()=>!$('workbench-view').hidden;
  const allReviews=()=>data.documents.flatMap(d=>d.reviews||[]);
  const groupFor=id=>data.documents.find(d=>(d.reviews||[]).some(r=>r.id===id));
  const reportJob=id=>data.pending_jobs.find(job=>job.report_id===id||job.id===id);
  const contractName=current=>groupFor(current.id)?.document_name||reportJob(current.id)?.document_name||current.document_name||current.report?.document_name||'合同审阅';
  const openEntry=entry=>entry.job?.report_id?goReview({id:entry.job.report_id}):entry.job?openJob(entry.job.id):goReview(entry.doc?.reviews?.[0]);
  function formatDate(value){
    if(!value)return null;
    const date=new Date(value);if(!Number.isFinite(date.getTime()))return null;
    return {date:date.toLocaleDateString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit'}).replaceAll('/','-'),
      time:date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false})};
  }
  function stage(text){
    const e=el('span','review-stage',text||'结果已生成');
    if(['复核中','审查中'].includes(text))e.classList.add('working');
    if(['已复核','已处理'].includes(text))e.classList.add('done');
    if(['无法读取','未完成','出错，可重试','记录异常'].includes(text))e.classList.add('failed');
    return e;
  }
  function reviewLabel(review){
    return review.party_label&&review.party_label!=='未知'?review.party_label:review.title||'审查记录';
  }
  function roundLabel(review){const round=Number(review?.round_number);return Number.isInteger(round)&&round>0?'第'+round+'轮'+(review.parent_report_id?'复审':'初审'):review?.parent_report_id?'复审':'第1轮初审';}
  function stanceFromContext(context={}){
    const party=String(context.our_party||'').trim();
    const roles=new Map([['client','客户'],['provider','服务商'],['employer','用人单位'],['employee','劳动者']]);
    const role=String(roles.get(context.our_role)||context.our_role||'').trim();
    return [...new Set([party,role].filter(Boolean))].join(' · ')||'未记录';
  }
  function dateNode(review){
    const box=el('div','review-date'),d=formatDate(review.updated_at||review.created_at);
    if(d){box.append(el('span','',d.date),el('small','',d.time));box.title='最近活动：'+d.date+' '+d.time;}
    else box.append(el('span','','时间未记录'));
    return box;
  }
  let grouping={revision:0,groups:[],assignments:{}},groupsValid=false;
  let selectedGroup=new URLSearchParams(location.search).get('group')||'all';
  const entryRows=()=>[...data.documents.map(doc=>({doc,date:doc.updated_at,name:doc.document_name})),
    ...data.pending_jobs.map(job=>({job,date:job.updated_at||job.created_at,name:job.document_name}))];
  const entryKey=entry=>entry.doc?.id||entry.job.id;
  function assignedGroup(entry){
    const allowed=new Set(grouping.groups.map(g=>g.id)),direct=grouping.assignments[entryKey(entry)];
    if(allowed.has(direct))return direct;
    if(entry.doc){
      const inherited=[...new Set((entry.doc.reviews||[]).map(r=>grouping.assignments[r.id]).filter(id=>allowed.has(id)))];
      if(inherited.length===1)return inherited[0];
    }
    return null;
  }
  async function readGroups(){
    const response=await fetch('/api/workbench/groups',{cache:'no-store'});
    const value=await response.json();if(!response.ok)throw new Error(value.error||'分组暂时无法读取。');
    grouping=value.state||value;groupsValid=true;
    if(!['all','ungrouped'].includes(selectedGroup)&&!grouping.groups.some(g=>g.id===selectedGroup))selectedGroup='all';
    return grouping;
  }
  async function saveGroup(action,payload,revision){
    const response=await fetch('/api/workbench/groups',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({expected_revision:revision,action,...payload})});
    const value=await response.json();
    if(!response.ok){
      if(response.status===409){await readGroups();render();throw new Error('分组已被其他操作更新，请重新打开这个窗口。');}
      throw new Error(value.error||'分组没有保存，请重试。');
    }
    grouping=value.state||value;groupsValid=true;render();return value;
  }
  function selectGroup(id){
    selectedGroup=id;
    const url=new URL(location);if(id==='all')url.searchParams.delete('group');else url.searchParams.set('group',id);
    history.replaceState(null,'',url);render();
  }
  function renderGroups(){
    const nav=$('group-list');nav.replaceChildren();const entries=entryRows();
    const add=(id,name,count,custom=false)=>{
      const row=el('div','group-nav-row'),select=button('','group-nav-item',()=>selectGroup(id));
      select.append(el('span','',name),el('small','',String(count)));select.title=name;select.setAttribute('aria-pressed',String(selectedGroup===id));
      select.classList.toggle('current',selectedGroup===id);row.append(select);
      if(custom){const edit=button('⋯','group-settings',()=>groupDialog(id));edit.setAttribute('aria-label','管理分组：'+name);row.append(edit);}
      nav.append(row);
    };
    add('all','全部合同',entries.length);
    $('create-group').disabled=!groupsValid;
    if(!groupsValid){nav.append(el('p','group-load-error','分组暂时无法读取'));return;}
    add('ungrouped','未分组',entries.filter(e=>!assignedGroup(e)).length);
    nav.append(el('div','group-nav-divider'));
    for(const group of grouping.groups)add(group.id,group.name,entries.filter(e=>assignedGroup(e)===group.id).length,true);
  }
  function groupDialog(id=null,entry=null){
    if(!groupsValid||isBusy())return;
    const current=grouping.groups.find(g=>g.id===id),revision=grouping.revision;
    const dialog=$('group-dialog'),body=$('group-body');body.replaceChildren();
    $('group-title').textContent=entry?'移动到分组':current?'管理分组':'新建分组';
    const form=el('form','group-form'),error=el('p','dialog-error');error.hidden=true;error.setAttribute('role','alert');
    const control=el(entry?'select':'input');control.id='group-value';
    if(entry){
      body.append(el('p','group-item-name',cleanName(entry.name)));
      control.add(new Option('未分组',''));
      for(const group of grouping.groups)control.add(new Option(group.name,group.id));
      control.value=assignedGroup(entry)||'';
    }else{control.type='text';control.maxLength=40;control.required=true;control.value=current?.name||'';control.placeholder='例如：劳动用工、客户A、项目B';}
    const field=el('label','',entry?'分组':'分组名称');field.htmlFor=control.id;form.append(field,control,error);
    const footer=el('div','group-dialog-actions'),submit=el('button','primary',entry?'移动':'保存');submit.type='submit';
    footer.append(button('取消','secondary',()=>dialog.close()),submit);
    if(current&&!entry){
      const warning=el('p','group-delete-warning');warning.hidden=true;form.append(warning);
      const remove=button('删除分组','group-delete',async()=>{
        if(warning.hidden){
          warning.textContent='组内 '+entryRows().filter(e=>assignedGroup(e)===id).length+' 项将移至“未分组”，合同文件和审查记录不会删除。';
          warning.hidden=false;remove.textContent='确认删除分组';return;
        }
        remove.disabled=true;submit.disabled=true;
        try{await saveGroup('delete',{group_id:id},revision);if(selectedGroup===id)selectGroup('ungrouped');dialog.close();notify('分组已删除，合同仍保留');}
        catch(e){error.textContent=e.message;error.hidden=false;}
        finally{remove.disabled=false;submit.disabled=false;}
      });
      form.append(remove);
    }
    form.append(footer);body.append(form);
    form.onsubmit=async event=>{
      event.preventDefault();error.hidden=true;submit.disabled=true;
      try{
        if(entry)await saveGroup('assign',{item_ids:[entryKey(entry)],group_id:control.value||null},revision);
        else await saveGroup(current?'rename':'create',{...(current?{group_id:id}:{}),name:control.value.trim()},revision);
        dialog.close();notify(entry?'已更新合同分组':'分组已保存');
      }catch(e){error.textContent=e.message;error.hidden=false;}
      finally{submit.disabled=false;}
    };
    if(!dialog.open)dialog.showModal();control.focus();
  }


  function deleteDialog(entry){
    if(isBusy()||document.querySelector('.contract-delete-dialog'))return;
    const itemId=entry.item_id||entryKey(entry),retry=!!entry.item_id;
    const dialog=el('dialog','work-dialog group-dialog contract-delete-dialog');
    dialog.setAttribute('aria-labelledby','contract-delete-title');
    const head=el('header','dialog-head'),body=el('div','dialog-body');
    head.append(el('h2','','删除这条合同记录？'));head.firstChild.id='contract-delete-title';
    const close=button('×','dialog-close',()=>dialog.close());close.setAttribute('aria-label','关闭删除窗口');head.append(close);
    body.append(el('p','delete-record-name',retry?'继续上次未完成的删除':cleanName(entry.name)));
    if(!retry&&entry.doc?.reviews?.[0])body.append(el('p','delete-record-round',roundLabel(entry.doc.reviews[0])));
    body.append(el('p','delete-record-help','这条记录的上传副本、报告、讨论和修改会永久删除，无法恢复。'),
      el('p','delete-record-help','其他审查轮次和电脑里的原始合同文件保留。'));
    const error=el('p','dialog-error');error.hidden=true;error.setAttribute('role','alert');
    const actions=el('div','group-dialog-actions'),cancel=button('取消','secondary',()=>dialog.close());
    let submitting=false;
    const submit=button(retry?'重试删除':'永久删除','contract-delete-confirm',async()=>{
      if(submitting)return;submitting=true;submit.disabled=true;cancel.disabled=true;close.disabled=true;error.hidden=true;submit.textContent='正在删除…';
      try{
        const response=await fetch('/api/workbench/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_id:itemId,confirm:true})});
        const value=await response.json();if(!response.ok)throw new Error(value.error||'删除没有完成，请重试。');
        dialog.close();notify('这条合同记录已永久删除');await refresh();
      }catch(reason){error.textContent=reason.message;error.hidden=false;}
      finally{submitting=false;submit.disabled=false;cancel.disabled=false;close.disabled=false;submit.textContent='重试删除';}
    });
    actions.append(cancel,submit);body.append(error,actions);dialog.append(head,body);document.body.append(dialog);
    dialog.addEventListener('cancel',event=>{if(submitting)event.preventDefault();});
    dialog.addEventListener('close',()=>{dialog.remove();$('refresh-workbench').focus();});dialog.showModal();cancel.focus();
  }

  const expanded=new Set();
  // Rows say what can be done next; the filter groups those rows into five plain buckets.
  const STATUS_BUCKETS={'审查中':['审查中'],'等你操作':['待开始审查','待回答问题','有事项未完成'],'待复核':['待复核','复核中'],
    '已复核':['已复核','结果已生成'],'出错':['出错，可重试','记录异常','无法读取','未完成']};
  function stance(review){const text=stanceFromContext(review||{});return text==='未记录'?'—':text;}
  function statusForEntry(entry){
    const gateStatus=entry.job?.information_gate_status||entry.doc?.reviews?.[0]?.information_gate_status;
    const gateLabel={needs_response:'待回答问题',running:'审查中',failed:'出错，可重试'}[gateStatus];if(gateLabel)return gateLabel;
    if(entry.job?.report_id)return '有事项未完成';
    if(entry.job)return entry.job.preparation?.status==='failed'?'出错，可重试':({ready:'待开始审查',preparing:'审查中',needs_info:'待回答问题',prepared:'待开始审查',running:'审查中',failed:'出错，可重试'})[entry.job.status]||'未完成';
    const latest=entry.doc.reviews?.[0];
    return latest?.available===false?'记录异常':latest?.status||'结果已生成';
  }
  function cell(row,label,className){
    const td=el('td',className);td.dataset.column=label;row.append(td);return td;
  }
  function goReview(review){
    if(!isBusy()&&review.available!==false)openReport(review.id,{clearIssue:true});
  }
  function renderHistory(doc,container){
    const head=el('div','history-heading','审查记录');
    const rows=el('div','review-history-list');
    for(const review of doc.reviews||[]){
      const row=el('div','history-review');
      const created=formatDate(review.created_at);
      const title=el('div','history-review-name',created?created.date+' '+created.time:'创建时间未记录');
      title.append(el('small','review-round-label',roundLabel(review)));
      const party=el('div','history-party','我方：'+stance(review));
      const progress=el('div','review-progress');progress.append(stage(review.available===false?'无法读取':review.status));
      if(review.findings_count>0)progress.append(el('small','',review.handled_count+' / '+review.findings_count+' 条已处理'));
      const action=button('打开','open-review',()=>goReview(review));action.disabled=review.available===false;
      action.setAttribute('aria-label','打开审查记录：'+reviewLabel(review));
      row.append(title,party,progress,action);rows.append(row);
    }
    container.append(head,rows);
  }
  function render(){
    const list=$('contract-list'),query=$('contract-search').value.trim().toLowerCase(),filter=$('contract-status').value;
    list.replaceChildren();
    for(const pending of data.pending_deletions||[]){
      const notice=el('div','pending-deletion');notice.setAttribute('role','status');
      notice.append(el('span','','有一条记录尚未删完，请重试完成清理。'),button('重试删除','delete-record',()=>deleteDialog(pending)));list.append(notice);
    }
    const entries=entryRows();renderGroups();
    entries.sort((a,b)=>(Date.parse(b.date)||0)-(Date.parse(a.date)||0));
    const visible=entries.filter(e=>(selectedGroup==='all'||(selectedGroup==='ungrouped'?!assignedGroup(e):assignedGroup(e)===selectedGroup))&&(!query||(e.name||'').toLowerCase().includes(query))&&(!filter||(STATUS_BUCKETS[filter]||[filter]).includes(statusForEntry(e))));
    $('workbench-title').textContent=selectedGroup==='all'?'全部合同':selectedGroup==='ungrouped'?'未分组':grouping.groups.find(g=>g.id===selectedGroup)?.name||'全部合同';
    const drafts=data.pending_jobs.filter(j=>['ready','needs_info','prepared'].includes(j.status)).length;
    $('workbench-count').textContent=query||filter||selectedGroup!=='all'?'显示 '+visible.length+' 项':data.documents.length+' 份已审合同'+(drafts?' · '+drafts+' 份草稿':'');
    if(!visible.length){
      const box=el('div','workbench-empty');
      box.append(el('strong','',query||filter?'没有匹配的合同':selectedGroup==='ungrouped'?'没有待归类的合同':selectedGroup!=='all'?'这个分组还没有合同':'还没有合同'),
        el('span','',query||filter?'换个名称或状态试试。':selectedGroup==='ungrouped'?'新上传且尚未归类的合同会显示在这里。':selectedGroup!=='all'?'在“全部合同”中点“归类”，将合同放到这里。':'点击右上角“新建审查”，上传第一份合同。'));
      list.append(box);return;
    }
    const table=el('table','contracts-table'),thead=el('thead'),head=el('tr');
    for(const name of ['合同名称','审查立场','状态','最近更新','操作']){const th=el('th','',name);th.scope='col';head.append(th);}
    thead.append(head);table.append(thead);const body=el('tbody');table.append(body);list.append(table);
    for(const entry of visible){
      const doc=entry.doc,job=entry.job,review=doc?.reviews?.[0],id=doc?.id||job.id;
      const row=el('tr','contract-row');row.dataset.documentId=id;
      const name=cell(row,'合同名称','contract-name-cell'),title=el('div','contract-name-line');
      const icon=el('span','table-file-icon','▤');icon.setAttribute('aria-hidden','true');
      const open=button(cleanName(entry.name),'contract-name-button',()=>openEntry(entry));
      open.title=entry.name||'';open.disabled=!job&&review?.available===false;
      title.append(icon,open);if(review)title.append(el('small','review-round-label',roundLabel(review)));else if(job?.parent_report_id)title.append(el('small','review-round-label','全文复审'));name.append(title);
      if(doc?.reviews?.length>1){
        const toggle=button(doc.reviews.length+' 次审查记录 '+(expanded.has(id)?'▴':'▾'),'history-toggle',()=>{
          if(expanded.has(id))expanded.delete(id);else expanded.add(id);render();
          [...list.querySelectorAll('.contract-row')].find(r=>r.dataset.documentId===id)?.querySelector('.history-toggle')?.focus({preventScroll:true});
        });
        toggle.setAttribute('aria-expanded',String(expanded.has(id)));
        toggle.setAttribute('aria-label','查看 '+cleanName(entry.name)+' 的审查记录');
        name.append(toggle);
      }
      cell(row,'审查立场','contract-party-cell').append(el('span','',stance(review||job)));
      const progress=cell(row,'状态','contract-status-cell');progress.append(stage(statusForEntry(entry)));
      if(review?.findings_count>0)progress.append(el('small','',review.handled_count+' / '+review.findings_count+' 条已处理'));
      if(job){const note=({ready:job.preparation?.status==='failed'?'检查准备失败，可重试':'已上传，尚未开始',preparing:'仅检查准备情况，尚未审核',needs_info:'可先补充，或暂不补充继续审核',prepared:'等待确认开始审核'})[job.status];if(note)progress.append(el('small','',note));}
      const date=cell(row,'最近更新','contract-date-cell');date.append(dateNode(review||job));
      const action=cell(row,'操作','contract-action-cell');
      const gateStatus=job?.information_gate_status||review?.information_gate_status;
      const actionLabel=gateStatus==='needs_response'?'确认信息':gateStatus==='running'?'查看调整进度':gateStatus==='failed'?'重试调整':job?.report_id?'查看报告':job?(['ready','needs_info','prepared'].includes(job.status)?'继续':job.status==='failed'?'查看原因':'查看进度'):'打开';
      const actionButton=button(actionLabel,'open-review',()=>openEntry(entry));
      actionButton.disabled=!job&&review?.available===false;
      actionButton.setAttribute('aria-label',actionLabel+'：'+cleanName(entry.name));action.append(actionButton);
      const move=button('归类','move-group',()=>groupDialog(null,entry));move.disabled=!groupsValid;
      move.setAttribute('aria-label','归类：'+cleanName(entry.name));action.append(move);
      const remove=button('删除','delete-record',()=>deleteDialog(entry));
      remove.disabled=!!job&&['running','preparing'].includes(job.status);
      remove.title=remove.disabled?'检查或审核结束后可以删除':'';
      remove.setAttribute('aria-label','删除：'+cleanName(entry.name));action.append(remove);
      if(review?.available===false)name.append(el('small','record-error',review.error||'记录暂时无法读取'));
      body.append(row);
      if(doc&&expanded.has(id)){
        const historyRow=el('tr','history-row'),content=el('td');content.colSpan=5;
        renderHistory(doc,content);historyRow.append(content);body.append(historyRow);
      }
    }
  }


  async function refresh(){
    if(loading)return allReviews();
    loading=true;$('refresh-workbench').disabled=true;$('workbench-error').hidden=true;
    try{
      const response=await fetch('/api/workbench',{cache:'no-store'});
      const value=await response.json();if(!response.ok)throw new Error(value.error||'合同列表暂时无法读取。');
      data={documents:Array.isArray(value.documents)?value.documents:[],pending_jobs:Array.isArray(value.pending_jobs)?value.pending_jobs:[],pending_deletions:Array.isArray(value.pending_deletions)?value.pending_deletions:[]};
      try{await readGroups();}catch(error){groupsValid=false;$('workbench-error').textContent=error.message;$('workbench-error').hidden=false;selectedGroup='all';}
      render();return allReviews();
    }catch(error){
      $('workbench-error').textContent=error.message;$('workbench-error').hidden=false;
      if(!data.documents.length)$('contract-list').replaceChildren(el('p','workbench-empty','列表暂时无法读取，请点击“刷新列表”重试。'));
      throw error;
    }finally{loading=false;$('refresh-workbench').disabled=false;}
  }
  function setMode(reader){
    window.InformationGateUI?.close();
    if($('information-gate'))$('information-gate').hidden=true;
    if($('review-contract-facts'))$('review-contract-facts').hidden=true;
    document.body.classList.toggle('reader-mode',reader);document.body.classList.toggle('home-mode',!reader);
    $('workbench-view').hidden=reader;$('reader-workspace').hidden=!reader;
    if($('review-view-tabs'))$('review-view-tabs').hidden=!reader;
    if(!reader&&$('review-checklist-results'))$('review-checklist-results').hidden=true;
    $('reader-heading').hidden=!reader;$('review-toolbar').hidden=!reader;
    $('back-workbench').hidden=!reader;$('home-location').hidden=reader;$('export').hidden=!reader;const rereview=$('rereview');if(rereview)rereview.hidden=!reader;const previous=$('previous-round');if(previous&&!reader)previous.hidden=true;
  }
  function enter(current){
    setMode(true);
    const name=contractName(current);
    $('current-file-name').textContent=cleanName(name);$('current-file-name').title=name;
    document.title=cleanName(name)+' · 合同审阅';
    const stanceText=stanceFromContext(current.report?.context||{});
    $('review-stance').textContent=stanceText;$('review-stance').title=stanceText;
    window.dispatchEvent(new Event('resize'));
  }
  function enterInformationGate(current){
    setMode(false);$('workbench-view').hidden=true;$('home-location').hidden=true;$('back-workbench').hidden=false;
    $('reader-heading').hidden=false;const name=contractName(current);$('current-file-name').textContent=cleanName(name);
    document.title=cleanName(name)+' · 审查信息';
  }
  async function showHome({push=true,refreshList=true}={}){
    if(isBusy())return;
    window.DiscussionUI?.close();
    setMode(false);document.title='合同审查工作台';
    const url=new URL(location);url.searchParams.delete('report');url.searchParams.delete('issue');
    if(push&&location.href!==url.href)history.pushState(null,'',url);
    window.scrollTo({top:0,behavior:'instant'});
    render();if(refreshList)try{await refresh();}catch{}
  }
  function configure(options){
    openReport=options.openReport||openReport;openJob=options.openJob||openJob;isBusy=options.isBusy||isBusy;
  }
  $('contract-search').addEventListener('input',render);
  $('contract-status').addEventListener('change',render);
  $('create-group').onclick=()=>groupDialog();
  $('group-close').onclick=()=>$('group-dialog').close();
  $('group-dialog').addEventListener('click',event=>{if(event.target===$('group-dialog'))$('group-dialog').close();});
  $('refresh-workbench').onclick=()=>refresh().catch(()=>{});
  $('back-workbench').onclick=()=>showHome();
  $('workbench-brand').onclick=event=>{event.preventDefault();showHome();};
  return {configure,refresh,enter,enterInformationGate,showHome,allReviews,groupFor,isHome:home};
})();
