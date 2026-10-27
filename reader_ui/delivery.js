'use strict';

// Document actions are independent of report opinions and selected strategies.
window.DeliveryUI = (() => {
  let reportBundle=null, data=null, view='original';
  let preparationTicket=0;
  const preparedCandidates=new Map();
  const candidateReads=new Map(),candidateIssues=new Map();
  const $=id=>document.getElementById(id);
  const copy=value=>JSON.parse(JSON.stringify(value));
  const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;};
  const btn=(text,cls,action)=>{const b=el('button',cls,text);b.type='button';b.onclick=action;return b;};
  const endpoint=suffix=>'/api/reports/'+encodeURIComponent(reportBundle.id)+'/delivery'+suffix;
  const state=()=>data?.state;
  const targetKey=(fid,t)=>fid+'-'+t.piece_id+'-'+t.start+'-'+t.end;
  const editId=(fid,t)=>'edit-'+targetKey(fid,t);
  const commentId=(fid,t)=>'note-'+targetKey(fid,t);
  const position=id=>reportBundle.paragraphs.find(p=>p.id===id)?.location.replaceAll(' / ',' · ')||id;
  // Comments are written into the contract file. Paragraph IDs become clause numbers;
  // opinion or question IDs are left for the reviewer to reword, with a visible warning.
  // A loaded bundle is replaced, never edited in place: build these once per bundle. Frozen parts let
  // ReaderText reuse its label tables instead of rebuilding them for every opinion in the dialog.
  const placeCache=new WeakMap(),findingLabelCache=new WeakMap();
  function placeContext(){
    let places=placeCache.get(reportBundle);
    if(!places){
      const paragraphs=reportBundle.paragraphs||[];
      const locations=Object.freeze(Object.fromEntries(paragraphs.map(p=>[p.id,(p.location||p.text||'对应原文').replaceAll(' / ',' · ')])));
      const inline_locations=Object.freeze(Object.fromEntries(paragraphs.map(p=>[p.id,window.ReaderText?.shortReference(p)||locations[p.id]])));
      places=Object.freeze({locations,inline_locations,paragraphs});
      placeCache.set(reportBundle,places);
    }
    return places;
  }
  function commentDraft(text){
    return window.ReaderText?.humanize(String(text||''),placeContext(),{plain:true,shortLocations:true})||String(text||'');
  }
  // Model prose shown in the dialog reads like the opinion card: clause numbers and opinion titles, never raw IDs.
  function readable(text,item){
    const places=placeContext(),options={plain:true,shortLocations:true,hideUnknown:true},human=value=>window.ReaderText?.humanize(String(value||''),places,options)||String(value||'');
    let finding_labels=findingLabelCache.get(reportBundle);
    if(!finding_labels){
      finding_labels=Object.freeze(Object.fromEntries((reportBundle.report?.findings||[]).map(f=>[f.id,'意见“'+human(f.title||f.id)+'”'])));
      findingLabelCache.set(reportBundle,finding_labels);
    }
    return window.ReaderText?.humanize(String(text||''),{...places,finding_labels,current_finding_id:item?.id},options)||String(text||'');
  }
  const internalIds=text=>[...new Set(String(text||'').match(/(?<![A-Za-z0-9_])(?:[FPQOCG]\d{2,}|T\d+R\d+C\d+P\d+)(?![A-Za-z0-9_])/g)||[])];
  function watchCommentIds(form,wrapper,input){
    const warn=el('p','dialog-help comment-id-warning');form.append(warn);
    const update=()=>{const ids=internalIds(input.value);warn.hidden=wrapper.hidden||!ids.length;warn.textContent=ids.length?'批注会写进合同文件，里面还有内部编号 '+ids.join('、')+'，请改成条款号或事项名称。':'';};
    input.addEventListener('input',update);update();return update;
  }
  // Show a confirmed range inside its own sentence, not as a detached fragment.
  function editInContext(edit){
    const line=el('p','diff-preview change-context'),text=(data?.source?.pieces||[]).find(p=>p.id===edit.piece_id)?.text;
    if(edit.kind==='insert_paragraph_after'){line.append(el('ins','',edit.new_text||''));return line;}
    if(typeof text!=='string'||text.slice(edit.start,edit.end)!==edit.old_text){line.textContent=edit.old_text+' → '+(edit.new_text||'（删除）');return line;}
    line.append(document.createTextNode(text.slice(0,edit.start)));
    if(edit.old_text)line.append(el('del','',edit.old_text));
    if(edit.new_text)line.append(el('ins','',edit.new_text));
    line.append(document.createTextNode(text.slice(edit.end)));return line;
  }
  const editLocation=row=>row.kind==='insert_paragraph_after'?'在'+position(row.piece_id)+'之后新增':row.location||position(row.piece_id);

  async function load(bundle) {
    preparationTicket++;preparedCandidates.clear();candidateReads.clear();candidateIssues.clear();
    reportBundle=bundle; data=null; view='original';
    for(const id of ['change-dialog','delivery-dialog'])if($(id)?.open)$(id).close();
    try {data=await request(endpoint(''));}
    catch(error){data={available:false,reason:error.message};}
  }
  function field(form,label,input) {
    const wrapper=el('label','delivery-field'); wrapper.append(el('span','',label),input);form.append(wrapper);return wrapper;
  }
  function authorInput() {
    const input=el('input');input.type='text';input.name='author';input.maxLength=100;
    input.value=state()?.author||'法务复核';input.required=true;return input;
  }
  function draftWithLabel(original,draft) {
    const label=original.match(/^(\s*(?:[（(][一二三四五六七八九十百\d]+[）)]|\d+(?:[.．]\d+)+(?=\s)|\d+(?:[.．]\d+)*[、.．])\s*)/u)?.[0];
    return label&&draft&&!draft.trimStart().startsWith(label.trim())?label+draft:draft;
  }
  // Only guard unmistakable editing instructions. The original suggestion stays
  // visible for a human to turn into a complete replacement; we do not infer a
  // smaller range or rewrite the model output here.
  function hasEditInstructionPrefix(text) {
    const value=String(text||'').trimStart();
    const withoutLabel=value.replace(/^(?:[（(][一二三四五六七八九十百\d]+[）)]|\d+(?:[.．]\d+)+(?=\s)|\d+(?:[.．]\d+)*[、.．])\s*/u,'');
    const article='第[\\d一二三四五六七八九十百千万〇零两]+(?:[.．][\\d一二三四五六七八九十百千万〇零两]+)*条?';
    const change=new RegExp('^(?:将|把)\\s*'+article+'(?:\\s*[、,，及和]\\s*'+article+')*(?:[^：:\\n]{0,24})?(?:修改|调整|改写|替换)为\\s*[：:]','u');
    const insertion=new RegExp('^在\\s*'+article+'\\s*(?:后|之后)\\s*(?:增加|补充|新增)\\s*[：:]','u');
    const supplement=/^(?:补充约定|建议\s*补充(?:约定|条款|如下内容|以下内容)?)\s*[：:]/u;
    return [value,withoutLabel].some(candidate=>change.test(candidate)||insertion.test(candidate)||supplement.test(candidate));
  }
  // Strip unchanged edges before creating a native edit, preserving their runs.
  function minimalEdit(target,newText) {
    if(target.kind==='insert_paragraph_after')return {kind:target.kind,piece_id:target.piece_id,paragraph_sha256:target.paragraph_sha256,start:target.start,end:target.end,old_text:'',new_text:newText,confirmed:true};
    const before=Array.from(target.old_text),after=Array.from(newText);
    let prefix=0,suffix=0;
    while(prefix<before.length&&prefix<after.length&&before[prefix]===after[prefix])prefix++;
    while(suffix<before.length-prefix&&suffix<after.length-prefix&&before[before.length-1-suffix]===after[after.length-1-suffix])suffix++;
    return {piece_id:target.piece_id,paragraph_sha256:target.paragraph_sha256,
      start:target.start+prefix,end:target.end-suffix,old_text:before.slice(prefix,before.length-suffix).join(''),
      new_text:after.slice(prefix,after.length-suffix).join(''),confirmed:true};
  }
  function fullDraft(target,edit) {
    if(!edit)return '';
    const chars=Array.from(target.old_text);
    return chars.slice(0,edit.start-target.start).join('')+edit.new_text+chars.slice(edit.end-target.start).join('');
  }
  // Character-level preview. Very large replacements use one honest delete/add.
  function diffSegments(before,after) {
    const a=Array.from(before),b=Array.from(after);let p=0,s=0;
    while(p<a.length&&p<b.length&&a[p]===b[p])p++;
    while(s<a.length-p&&s<b.length-p&&a[a.length-1-s]===b[b.length-1-s])s++;
    const x=a.slice(p,a.length-s),y=b.slice(p,b.length-s),segments=[];
    function push(type,text){if(!text)return;const last=segments.at(-1);if(last?.type===type)last.text+=text;else segments.push({type,text});}
    push('same',a.slice(0,p).join(''));
    if(x.length*y.length>350000){push('delete',x.join(''));push('insert',y.join(''));}
    else {
      const width=y.length+1,table=new Uint32Array((x.length+1)*width);
      for(let i=x.length-1;i>=0;i--)for(let j=y.length-1;j>=0;j--)
        table[i*width+j]=x[i]===y[j]?table[(i+1)*width+j+1]+1:Math.max(table[(i+1)*width+j],table[i*width+j+1]);
      let i=0,j=0;
      while(i<x.length||j<y.length){
        if(i<x.length&&j<y.length&&x[i]===y[j]){push('same',x[i++]);j++;}
        else if(j<y.length&&(i===x.length||table[i*width+j+1]>table[(i+1)*width+j]))push('insert',y[j++]);
        else push('delete',x[i++]);
      }
    }
    push('same',s?a.slice(a.length-s).join(''):'');return segments;
  }
  function renderDiff(container,before,after) {
    container.replaceChildren();
    const raw=diffSegments(before,after),readable=[];let group=[];
    function flush(){
      if(!group.length)return;
      const removed=group.filter(s=>s.type!=='insert').map(s=>s.text).join('');
      const added=group.filter(s=>s.type!=='delete').map(s=>s.text).join('');
      if(removed)readable.push({type:'delete',text:removed});if(added)readable.push({type:'insert',text:added});group=[];
    }
    raw.forEach((seg,index)=>{
      if(seg.type==='same'&&(index===0||index===raw.length-1||Array.from(seg.text).length>=4)){flush();readable.push(seg);}
      else group.push(seg);
    });flush();
    for(const seg of readable)container.append(seg.type==='same'?document.createTextNode(seg.text):el(seg.type==='delete'?'del':'ins','',seg.text));
  }
  async function save(edits,comments,author,errorBox,message,previewMode=null,draftConfirmation=null) {
    errorBox.hidden=true;setBusy(true);
    const controls=[...document.querySelectorAll('.work-dialog button,.work-dialog input,.work-dialog select,.work-dialog textarea')];
    controls.forEach(e=>e.disabled=true);
    try {
      const result=await request(endpoint(''),{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({expected_revision:state().revision,source_sha256:data.source.source_sha256,author,edits,comments,...(draftConfirmation?{draft_confirmation:draftConfirmation}:{})})});
      data.state=result.state;
      if(previewMode)view=previewMode;
      if(!data.state.edits.length)view='original';
      renderAll(false);notify(message);return true;
    } catch(error){errorBox.textContent=error.status===409?'原稿或记录已变化，请刷新页面后重新确认；本次没有覆盖已有修改。':error.message;errorBox.hidden=false;return false;}
    finally{controls.forEach(e=>e.disabled=false);setBusy(false);}
  }
  function operations(item) {
    const edits=(state()?.edits||[]).filter(e=>(e.finding_ids||[]).includes(item.id));
    const ids=new Set(edits.map(e=>e.id)),targets=data?.targets?.[item.id]||[];
    const comments=(state()?.comments||[]).filter(c=>ids.has(c.edit_id)||c.id.startsWith('note-'+item.id+'-')||
      (!c.edit_id&&targets.some(t=>t.piece_id===c.piece_id&&c.start<t.end&&t.start<c.end)));
    return {edits,comments};
  }
  function statusFor(item) {
    const op=operations(item);
    if(op.edits.length)return '已修改';
    if(op.comments.length)return '已批注';
    return reportBundle?.state.reviews[item.id]?.decision==='驳回'?'不采用修改建议':'待处理';
  }
  function statusLabel(item) {const op=operations(item);return op.edits.length&&op.comments.length?'已修改并批注':statusFor(item);}
  function suggestions(item,draftText) {
    const existing=savedDrafts(item);
    if(existing.length)return existing.map(t=>({location:t.location,before:t.old_text,after:t.new_text}));
    const prepared=preparedCandidates.get(item.id);
    if(hasCandidate(item,draftText))return [...prepared.value.edits,...(prepared.value.insertions||[])].map(t=>({location:editLocation(t),before:t.old_text,after:t.new_text}));
    if(item.draft_plan){
      if(item.draft_plan.status!=='ready')return [];
      return [...(item.draft_plan.edits||[]).map(edit=>({location:position(edit.piece_id),before:reportBundle.paragraphs.find(p=>p.id===edit.piece_id)?.text||'',after:edit.new_text})),
        ...(item.draft_plan.insertions||[]).map(edit=>({location:'在'+position(edit.after_piece_id)+'之后新增',before:'',after:edit.new_text}))];
    }
    if(needsPreparation(item,draftText))return [];
    const targets=data?.targets?.[item.id]||[];
    const saved=targets.filter(t=>state()?.edits.some(e=>e.id===editId(item.id,t)));
    const chosen=saved.length?saved:targets.length===1?targets:[];
    return chosen.map(t=>{
      const edit=state()?.edits.find(e=>e.id===editId(item.id,t));
      return {location:t.location||position(t.piece_id),before:t.old_text,after:edit?fullDraft(t,edit):draftWithLabel(t.old_text,draftText||t.old_text)};
    });
  }
  async function ensureCandidate(item,draftText,{reload=false}={}){
    if(!data?.available||(!draftText&&!item.draft_plan)||(!item.draft_plan&&!needsPreparation(item,draftText))||(savedDrafts(item).length&&!hasNewerDraft(item)))return false;
    if(item.draft_plan&&item.draft_plan.status!=='ready')return false;
    const key=readKey(item,draftText);if(candidateReads.has(key)&&!reload)return false;
    const loaded=reportBundle,currentRevision=state().revision,ticket=preparationTicket;
    const pending=(async()=>{try{
      const value=await request('/api/reports/'+encodeURIComponent(loaded.id)+'/drafts/'+encodeURIComponent(item.id));
      if(ticket!==preparationTicket||reportBundle!==loaded||state().revision!==currentRevision||candidateReads.get(key)!==pending)return false;
      if(value.status!=='ready'||value.source_sha256!==data.source.source_sha256||value.based_on_revision!==currentRevision||!value.draft_sha256){
        if(item.draft_plan)throw new Error('这份改稿与当前原稿或记录不一致。合同未改，也没有额外调用模型，请重新打开报告核对。');return false;
      }
      const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(draftText))),byte=>byte.toString(16).padStart(2,'0')).join('');
      if(ticket!==preparationTicket||reportBundle!==loaded||state().revision!==currentRevision||candidateReads.get(key)!==pending)return false;
      if(digest!==value.draft_sha256){if(item.draft_plan)throw new Error('改稿文字版本已变化，请重新打开报告核对；本次没有应用修改。');return false;}
      candidateIssues.delete(item.id);
      preparedCandidates.set(item.id,{draftText,value});return true;
    }catch(error){if(item.draft_plan&&ticket===preparationTicket&&reportBundle===loaded&&candidateReads.get(key)===pending){preparedCandidates.delete(item.id);candidateIssues.set(item.id,error.message);return true;}return false;}})();
    candidateReads.set(key,pending);return pending;
  }
  const readKey=(item,draftText)=>state().revision+'\n'+item.id+'\n'+(item.opinion_revision||0)+'\n'+draftText;
  const candidateIssue=item=>candidateIssues.get(item.id)||'';
  function scope(item,draftText,candidate){
    const saved=candidate?[]:savedDrafts(item),prepared=candidate||(saved.length?state()?.draft_confirmations?.[item.id]:null)||(hasCandidate(item,draftText)?preparedCandidates.get(item.id).value:null)||item.draft_plan;
    if(!prepared&&!saved.length)return [];
    const edits=saved.length?saved:[...(prepared?.edits||[]),...(prepared?.insertions||[]).map(edit=>edit.after_piece_id?{...edit,piece_id:edit.after_piece_id,kind:'insert_paragraph_after'}:edit)],ids=[...new Set([...(item.refs||[]).map(ref=>ref.id),...(prepared?.paragraph_actions||[]).map(row=>row.piece_id),...edits.filter(row=>row.kind!=='insert_paragraph_after').map(row=>row.piece_id)])];
    if(!ids.length)return [];
    const rows=ids.map(id=>{
      const changed=edits.some(edit=>edit.piece_id===id&&edit.kind!=='insert_paragraph_after'),explicit=prepared?.paragraph_actions?.find(row=>row.piece_id===id);
      const action=changed?'modify':explicit?.action||'unexplained';
      const label=changed?(saved.length?'已修改':'拟修改'):action==='retain'?'保留原文':action==='needs_confirmation'?'待确认':'尚未说明';
      const reason=explicit?.reason||(changed?(saved.length?'此处已有你确认的修改。':'下面是这段的改稿，确认后才会写进合同。'):saved.length?'这段还没改，请确认要保留原文，还是一起调整。':prepared?'这份改稿没有说明为什么保留这段，可以在讨论中确认。':'还没确定具体改哪些文字，整理改稿后会列出每处改动和保留的理由。');
      return {piece_id:id,location:explicit?.location||position(id),action,label,reason};
    });
    for(const entry of edits.filter(edit=>edit.kind==='insert_paragraph_after'))rows.push({piece_id:entry.piece_id,location:editLocation(entry),action:'modify',label:saved.length?'已新增':'拟新增',reason:entry.new_text});
    return rows;
  }
  function suggestionScope(item,draftText,candidate){const value=candidate||state()?.draft_confirmations?.[item.id]||(hasCandidate(item,draftText)?preparedCandidates.get(item.id).value:null);return value?.suggestion_actions||[];}
  function renderSuggestionScope(target,item,draftText,candidate){
    if(item.draft_plan)return;
    const rows=suggestionScope(item,draftText,candidate);if(!rows.length)return;
    const section=el('section','draft-scope');section.append(el('h4','','建议如何写进合同'));
    for(const row of rows){const entry=el('div','draft-scope-row');entry.append(el('strong','',row.text||row.unit_text||row.unit_id),el('span','draft-scope-status '+(row.action==='implemented'?'modify':'unexplained'),row.action==='implemented'?'已纳入改稿':row.action==='needs_confirmation'?'待确认':'尚未核对'),el('p','',readable(row.reason,item)));section.append(entry);}
    target.append(section);
  }
  function renderScope(target,item,candidate){
    const section=el('section','draft-scope');section.append(el('h4','','相关条款怎么处理'));
    for(const row of scope(item,'',candidate)){const entry=el('div','draft-scope-row');entry.append(el('strong','',readable(row.location,item)),el('span','draft-scope-status '+row.action,row.label),el('p','',readable(row.reason,item)));section.append(entry);}
    target.append(section);
    renderSuggestionScope(target,item,'',candidate);
  }
  function available(item) {return !!data?.available&&item.status!=='处理失败'&&((data.targets?.[item.id]||[]).some(t=>t.supported!==false)||item.draft_plan?.status==='ready');}
  function hasCandidate(item,draftText){const cached=preparedCandidates.get(item.id);return cached?.draftText===draftText&&cached.value.based_on_revision===state()?.revision;}
  function revision() {return data?.available?state().revision:null;}
  async function undoFinding(item) {
    if(busy||!data?.available)return;
    const op=operations(item),editIds=new Set(op.edits.map(e=>e.id)),commentIds=new Set(op.comments.map(c=>c.id));
    await save(state().edits.filter(e=>!editIds.has(e.id)),state().comments.filter(c=>!commentIds.has(c.id)&&!editIds.has(c.edit_id)),
      state().author,$('error'),'已撤销本条修改和批注');
  }
  function needsPreparation(item,draftText) {
    if(item.draft_plan)return false;
    const targets=data?.targets?.[item.id]||[],text=String(draftText||''),label=value=>/^\s*(\d+(?:\.\d+)+)\b/.exec(value||'')?.[1];
    return hasEditInstructionPrefix(text)||targets.length>1||text.split(/\r?\n/).filter(line=>line.trim()).length>1||/新增|增加.{0,12}条款/.test(item.suggestion||'')||Boolean(targets.length===1&&label(text)&&label(targets[0].old_text)&&label(text)!==label(targets[0].old_text));
  }
  function savedDrafts(item) {
    const own=operations(item).edits;
    const pieces=new Map((data?.source?.pieces||[]).map(p=>[p.id,p]));
    const result=[];
    for(const id of new Set(own.filter(e=>e.kind!=='insert_paragraph_after').map(e=>e.piece_id))){
      const piece=pieces.get(id);if(!piece)continue;
      const changes=own.filter(e=>e.piece_id===id&&e.kind!=='insert_paragraph_after').sort((a,b)=>a.start-b.start);
      const chars=Array.from(piece.text);let cursor=0,newText='';
      for(const e of changes){newText+=chars.slice(cursor,e.start).join('')+e.new_text;cursor=e.end;}
      newText+=chars.slice(cursor).join('');
      result.push({piece_id:id,paragraph_sha256:piece.paragraph_sha256,start:0,end:chars.length,
        old_text:piece.text,new_text:newText,location:position(id)});
    }
    for(const change of own.filter(e=>e.kind==='insert_paragraph_after'))result.push({...change,location:'在'+position(change.piece_id)+'之后新增'});
    return result;
  }
  function overlaps(a,b) {
    if(a.piece_id!==b.piece_id)return false;
    if(a.kind==='insert_paragraph_after'||b.kind==='insert_paragraph_after')return a.kind===b.kind;
    if(a.start===a.end&&b.start===b.end)return a.start===b.start;
    if(a.start===a.end)return a.start>=b.start&&a.start<=b.end;
    if(b.start===b.end)return b.start>=a.start&&b.start<=a.end;
    return a.start<b.end&&b.start<a.end;
  }
  function openPreparedChange(item,candidate,preset,baseRevision,sourceDraftText=item.clause_draft) {
    const rows=[...(candidate.edits||[]),...(candidate.insertions||[])],body=$('change-body');body.replaceChildren();
    $('change-title').textContent=rows.length>1?'确认这 '+rows.length+' 处改稿':'确认合同改稿';
    const form=el('form','delivery-form'),error=el('div','dialog-error');
    error.hidden=true;error.setAttribute('role','alert');
    if(candidate.candidate_id&&state().edits.some(e=>(e.finding_ids||[]).includes(item.id)))
      form.append(el('p','change-fact-notice','这是新版意见的改稿，确认后会替换本条之前确认的修改。'));
    if(candidate.contract_notice)form.append(el('p','change-fact-notice',candidate.contract_notice));
    if(candidate.fact_notice?.length){
      // Information added later does not lock the draft; the person checks it here.
      const note=el('div','change-fact-notice'),list=el('ul');
      note.append(el('p','','这条改稿写于你补充下面这些信息之前，确认前请核对是否仍然适用：'));
      for(const line of candidate.fact_notice)list.append(el('li','',line));
      note.append(list);form.append(note);
    }
    renderScope(form,item,candidate);
    const incomplete=Boolean(candidate.candidate_id&&candidate.coverage_complete===false);
    if(incomplete){form.append(el('p','dialog-help','这份旧改稿还没核对原建议中的所有修改，请先重新整理后再确认。'));
      if(!savedDrafts(item).length)form.append(btn('重新整理完整改稿','secondary',()=>prepareChange(item,sourceDraftText,preset)));
    }
    const author=authorInput();author.id='change-author';field(form,'署名',author);
    const inputs=[];
    for(const [index,t] of rows.entries()){
      const section=el('section','change-preview prepared-change');
      section.append(el('h3','',editLocation(t)));
      const diff=el('div','diff-preview suggestion-diff');diff.id='prepared-diff-'+index;
      const edit=el('textarea');edit.id='prepared-text-'+index;edit.rows=6;edit.maxLength=20000;edit.value=t.new_text;
      const wrapper=field(section,'改后条款',edit);wrapper.hidden=preset!=='custom';
      const toggle=btn('调整文字','link-button',()=>{wrapper.hidden=!wrapper.hidden;toggle.textContent=wrapper.hidden?'调整文字':'收起编辑';if(!wrapper.hidden)edit.focus();});
      edit.oninput=()=>renderDiff(diff,t.old_text,edit.value);
      section.append(diff,toggle);form.append(section);inputs.push({target:t,input:edit});renderDiff(diff,t.old_text,edit.value);
    }
    const note=el('textarea');note.id='change-comment';note.rows=3;note.maxLength=4000;note.placeholder='写给协作者的说明';
    const ownedEditIds=new Set(operations(item).edits.map(e=>e.id));
    const ownedNotes=(state().comments||[]).filter(c=>ownedEditIds.has(c.edit_id)||c.id.startsWith('note-'+item.id+'-'));
    const existing=ownedNotes[0];
    note.value=existing?.text||(preset==='edit'?'':commentDraft(candidate.comment));
    const noteField=field(form,'批注内容',note);noteField.hidden=preset==='edit'&&!existing;watchCommentIds(form,noteField,note);
    note.required=preset==='both';
    for(const extra of ownedNotes.slice(1))form.append(el('p','dialog-help','保留已有批注：'+extra.text));
    form.append(error);
    const submit=el('button','primary',preset==='both'?'确认修改并批注':'确认修改');submit.type='submit';submit.disabled=incomplete;
    const footer=el('div','dialog-actions');footer.append(btn('返回阅读','secondary',()=>$('change-dialog').close()),submit);form.append(footer);body.append(form);
    form.onsubmit=async event=>{
      event.preventDefault();error.hidden=true;
      if(incomplete)return;
      if(state().revision!==baseRevision){error.textContent='合同修改记录已变化，请重新打开这条意见后确认。';error.hidden=false;return;}
      const previous=state().edits.filter(e=>(e.finding_ids||[]).includes(item.id));
      const previousIds=new Set(previous.map(e=>e.id));
      const others=state().edits.filter(e=>!previousIds.has(e.id));
      const changes=[];
      for(const {target:t,input} of inputs){
        if(!input.value.trim()||hasEditInstructionPrefix(input.value)){
          error.textContent='改稿缺少完整条款，或还含有操作说明。请重新整理，或点击“调整文字”修改后再确认。';error.hidden=false;return;
        }
        const change=minimalEdit(t,input.value);
        if(change.old_text===change.new_text)continue;
        if(others.some(e=>overlaps(e,change))){
          error.textContent='另一条意见已经改过这段原文，这次不会覆盖。请先到“交付文件”查看并处理已有修改。';error.hidden=false;return;
        }
        changes.push({id:'edit-'+item.id+'-prepared-'+(t.kind==='insert_paragraph_after'?'insert-after-':'')+t.piece_id,...change,finding_ids:[item.id]});
      }
      if(!changes.length){error.textContent='没有需要修改的文字。';error.hidden=false;return;}
      const comments=copy(state().comments).filter(c=>!previousIds.has(c.edit_id)&&!c.id.startsWith('note-'+item.id+'-'));
      for(const extra of ownedNotes.slice(1)){
        const previousEdit=previous.find(e=>e.id===extra.edit_id);
        const changed=changes.find(e=>e.piece_id===(previousEdit?.piece_id||extra.piece_id));
        if(!changed){error.textContent='新的改稿没有包含这处已有批注的修改。请先到“交付文件”处理这条批注，再保存改稿。';error.hidden=false;return;}
        comments.push({id:extra.id,edit_id:changed.id,text:extra.text,confirmed:true});
      }
      if(note.value.trim())comments.push({id:'note-'+item.id+'-prepared',edit_id:changes[0].id,text:note.value,confirmed:true});
      if(await save([...copy(others),...changes],comments,author.value.trim(),error,'已应用本条修订','tracked',candidate.candidate_id?{finding_id:item.id,candidate_id:candidate.candidate_id}:null))$('change-dialog').close();
    };
    if(!$('change-dialog').open)$('change-dialog').showModal();$('change-dialog').scrollTop=0;
  }
  async function prepareChange(item,draftText,preset,clarification='') {
    if(busy||!data?.available)return;
    const dialog=$('change-dialog'),body=$('change-body'),loaded=reportBundle,ticket=++preparationTicket;
    body.replaceChildren();$('change-title').textContent='整理改稿';
    body.append(el('p','dialog-help','这条旧建议还没确定具体改哪些文字。下面的模型会把建议整理成改稿，先预览、再确认修改合同。'));
    const host=el('div','draft-model-selector'),error=el('p','dialog-error');error.hidden=true;error.setAttribute('role','alert');
    const consent=el('label','draft-model-consent'),checkbox=el('input');checkbox.type='checkbox';
    const description=el('span','','正在读取模型设置，读完后请选择并确认调用');consent.append(checkbox,description);
    const submit=btn('确认调用并整理','primary',async()=>{
      if(!checkbox.checked||submit.disabled)return;
      submit.disabled=true;error.hidden=true;
      try{
        const previous=selector.value();await APIProfiles.load({reload:true});
        if(ticket!==preparationTicket||reportBundle!==loaded||!dialog.open)return;
        const choice=selector.value();
        if(!choice?.config?.base_url||!choice.config.api_key||!choice.config.model)throw new Error('请先在模型设置里保存服务地址、密钥和模型。');
        if(JSON.stringify(previous?.config)!==JSON.stringify(choice.config)){checkbox.checked=false;updateConsent();throw new Error('模型设置刚有变化，请核对当前服务后再确认调用。');}
        await runPreparation(item,draftText,preset,clarification,choice.config,choice.provider);
      }catch(reason){error.textContent=reason.message;error.hidden=false;}
      finally{submit.disabled=false;}
    });
    function updateConsent(){const choice=selector?.value();description.textContent=choice?.config?.model?'我确认调用 '+choice.provider+' · '+choice.config.model+' 整理这条建议':'请先在模型设置中保存可用的服务';checkbox.checked=false;submit.disabled=!choice?.config?.api_key;}
    body.append(host,consent,error);const actions=el('div','dialog-actions');actions.append(btn('返回阅读','secondary',()=>dialog.close()),submit);body.append(actions);
    let selector=null;submit.disabled=true;if(!dialog.open)dialog.showModal();
    try{
      await APIProfiles.load({reload:true});if(ticket!==preparationTicket||reportBundle!==loaded||!dialog.open)return;
      selector=window.ReviewModelSelector.mount(host,[{key:'compatible_api',available:true}],()=>{if(selector)updateConsent();});
      await selector.ready;if(ticket===preparationTicket&&dialog.open)updateConsent();
    }catch(reason){error.textContent=reason.message;error.hidden=false;}
  }
  async function runPreparation(item,draftText,preset,clarification,config,provider) {
    if(busy||!data?.available)return;
    const ticket=++preparationTicket,reportId=reportBundle.id,baseRevision=state().revision;
    const path='/api/reports/'+encodeURIComponent(reportId)+'/drafts/'+encodeURIComponent(item.id);
    const dialog=$('change-dialog'),body=$('change-body');body.replaceChildren();
    $('change-title').textContent='正在整理改稿';
    body.append(el('p','','正在把这条建议整理成对应原文的合同条款…'),
      el('p','dialog-help',provider+' · '+config.model+' 正在处理。完成后先展示删改，确认前不会修改合同。'));
    body.append(btn('先返回阅读','secondary',()=>dialog.close()));
    const cancel=()=>{if(ticket===preparationTicket){preparationTicket++;setBusy(false);}};
    dialog.addEventListener('close',cancel,{once:true});if(!dialog.open)dialog.showModal();setBusy(true);
    try {
      const facts=await request('/api/reports/'+encodeURIComponent(reportId)+'/facts');
      if(ticket!==preparationTicket||!dialog.open||reportBundle.id!==reportId)return;
      let value=await request(path,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({expected_revision:baseRevision,source_sha256:data.source.source_sha256,draft_text:draftText,clarification,
          engine_key:'compatible_api',config,consent:true,expected_opinion_revision:reportBundle.opinion_revisions?.[item.id]??item.opinion_revision??reportBundle.opinion_revision??0,expected_facts_revision:facts.facts.revision})});
      const candidateId=value.candidate_id||value.cache_key;
      while(value.status==='running'){
        await new Promise(resolve=>setTimeout(resolve,1200));
        if(ticket!==preparationTicket||!dialog.open||reportBundle.id!==reportId)return;
        value=await request(path+(candidateId?'?candidate_id='+encodeURIComponent(candidateId):''));
      }
      if(ticket!==preparationTicket||!dialog.open||reportBundle.id!==reportId)return;
      dialog.removeEventListener('close',cancel);setBusy(false);
      if(value.status==='ready'){
        if(value.source_sha256&&value.source_sha256!==data.source.source_sha256)throw new Error('原稿已变化，请重新打开报告。');
        preparedCandidates.set(item.id,{draftText,value});
        renderAll(false);
        openPreparedChange(item,value,preset,baseRevision,draftText);
      } else if(value.status==='needs_input'){
        $('change-title').textContent='还需要确认的信息';body.replaceChildren();
        renderScope(body,item,value);
        for(const question of value.questions||[])body.append(el('p','',readable(question,item)));
        const answer=el('textarea');answer.rows=4;answer.maxLength=4000;answer.value=clarification;
        field(body,'补充信息',answer);
        const again=btn('继续整理','primary',()=>{if(answer.value.trim())prepareChange(item,draftText,preset,answer.value.trim());else answer.focus();});
        body.append(again,btn('返回阅读','secondary',()=>dialog.close()));
      } else {
        throw new Error(value.error||'这次没有生成可用改稿，合同尚未修改。');
      }
    } catch(error) {
      if(ticket!==preparationTicket||!dialog.open)return;
      $('change-title').textContent='改稿未完成';body.replaceChildren();
      body.append(el('p','dialog-error',error.message),el('p','dialog-help','合同尚未修改。可以先返回阅读或仅添加批注。'),
        btn('仅添加批注','secondary',()=>{setBusy(false);openDirectChange(item,draftText,'comment');}),
        btn('返回阅读','secondary',()=>dialog.close()));
    } finally {
      dialog.removeEventListener('close',cancel);
      if(ticket===preparationTicket)setBusy(false);
    }
  }
  // A newer accepted opinion version brings its own ready draft; confirming it
  // replaces this opinion's earlier edits instead of reopening the old ones.
  function hasNewerDraft(item){
    if(!savedDrafts(item).length||item.draft_plan?.status!=='ready')return false;
    // Confirmations saved without a version are left as they are.
    const confirmed=state()?.draft_confirmations?.[item.id]?.opinion_revision;
    return typeof confirmed==='number'&&(item.opinion_revision||0)>confirmed;
  }
  async function openChange(item,draftText,preset='both') {
    if(busy||!data?.available)return;
    const saved=savedDrafts(item);
    if(preset!=='comment'&&saved.length&&!hasNewerDraft(item)){
      openPreparedChange(item,{edits:saved,...state()?.draft_confirmations?.[item.id],candidate_id:null},preset,state().revision);return;
    }
    if(preset!=='comment'&&item.draft_plan){
      if(item.draft_plan.status!=='ready')return;
      const loaded=reportBundle,currentRevision=state().revision;
      await ensureCandidate(item,draftText,{reload:true});
      await candidateReads.get(readKey(item,draftText));
      if(reportBundle!==loaded||state().revision!==currentRevision||busy)return;
      if(hasCandidate(item,draftText)){openPreparedChange(item,preparedCandidates.get(item.id).value,preset,currentRevision,draftText);return;}
      const body=$('change-body');body.replaceChildren(el('p','dialog-error',candidateIssue(item)||'当前改稿未能与原稿核对，请重新打开报告。'),btn('返回阅读','secondary',()=>$('change-dialog').close()));
      $('change-title').textContent='改稿版本需要核对';if(!$('change-dialog').open)$('change-dialog').showModal();return;
    }
    if(preset!=='comment'&&draftText&&needsPreparation(item,draftText)){
      const cached=preparedCandidates.get(item.id);if(cached?.draftText===draftText&&cached.value.based_on_revision===state().revision){openPreparedChange(item,cached.value,preset,state().revision,draftText);return;}
      return prepareChange(item,draftText,preset);
    }
    openDirectChange(item,draftText,preset);
  }
  function openDirectChange(item,draftText,preset='both') {
    if(busy||!data?.available)return;
    if(!draftText&&preset!=='comment')preset='comment';
    const needsFullReplacement=preset!=='comment'&&hasEditInstructionPrefix(draftText);
    const targets=data.targets?.[item.id]||[],body=$('change-body');body.replaceChildren();
    const titles={both:'修改并批注',edit:'仅修改',comment:'仅批注',custom:'编辑改稿'};
    $('change-title').textContent=titles[preset];
    const form=el('form','delivery-form'),error=el('div','dialog-error');error.hidden=true;error.setAttribute('role','alert');
    const targetSelect=el('select');targetSelect.id='change-target';targetSelect.required=true;
    targetSelect.add(new Option('选择要处理的原文位置',''));
    targets.forEach((t,i)=>{const option=new Option((t.location||position(t.piece_id))+(t.supported===false?'（暂不支持）':''),String(i));option.disabled=t.supported===false;targetSelect.add(option);});
    if(targets.length===1&&targets[0].supported!==false) {
      targetSelect.value='0';targetSelect.hidden=true;form.append(targetSelect,el('p','dialog-help',targets[0].location||position(targets[0].piece_id)));
    } else field(form,'要处理哪处文字',targetSelect);
    const author=authorInput();author.id='change-author';field(form,'署名',author);
    const original=el('textarea');original.id='change-original';original.rows=3;original.readOnly=true;
    const originalField=field(form,'本次替换的原文（仅这段文字）',original);originalField.hidden=preset==='comment';
    const replacement=el('textarea');replacement.id='change-text';replacement.rows=5;replacement.maxLength=20000;
    const draftField=field(form,'替换为（前后未选中的原文会保留）',replacement);draftField.hidden=preset!=='custom'&&!needsFullReplacement;
    const instructionHelp=el('p','dialog-help','这条建议含修改说明，请改成上方所选文字的替换内容，不要重复前后未选中的原文。');instructionHelp.hidden=!needsFullReplacement;form.append(instructionHelp);
    const preview=el('div','diff-preview suggestion-diff');preview.id='change-diff';
    const previewBlock=el('div','change-preview');previewBlock.append(el('h3','',preset==='comment'?'批注位置':'本次修订'),preview);form.append(previewBlock);
    const comment=el('textarea');comment.id='change-comment';comment.rows=4;comment.maxLength=4000;comment.placeholder='写给协作者的说明';
    const commentField=field(form,'批注内容',comment);const checkCommentIds=watchCommentIds(form,commentField,comment);
    form.append(error);
    const footer=el('div','dialog-actions'),submit=el('button','primary',titles[preset]);submit.type='submit';
    footer.append(btn('取消','secondary',()=>$('change-dialog').close()),submit);form.append(footer);body.append(form);
    function target(){return targetSelect.value===''?null:targets[Number(targetSelect.value)];}
    function refresh(fill) {
      const t=target(),edit=t?state().edits.find(e=>e.id===editId(item.id,t)):null;
      const existing=t?state().comments.find(c=>c.id===commentId(item.id,t)||(edit&&c.edit_id===edit.id)):null;
      original.value=t?.old_text||'请先选择原文位置';
      if(fill){replacement.value=edit?fullDraft(t,edit):t?(needsFullReplacement?draftText:draftWithLabel(t.old_text,draftText||t.old_text)):'';comment.value=existing?.text||'';checkCommentIds();}
      commentField.hidden=preset==='edit'&&!existing;
      comment.required=preset==='comment'||preset==='both';
      submit.textContent=preset==='custom'?'保存改稿':titles[preset];submit.disabled=!t;
      if(!t)preview.textContent='请先选择原文位置';
      else if(preset==='comment')preview.textContent=edit?fullDraft(t,edit):t.old_text;
      else renderDiff(preview,t.old_text,replacement.value);
    }
    targetSelect.onchange=()=>refresh(true);replacement.oninput=()=>refresh(false);
    form.onsubmit=async event=>{
      event.preventDefault();const t=target();if(!t)return;
      const eid=editId(item.id,t),cid=commentId(item.id,t),onlyComment=preset==='comment';
      let edits=copy(state().edits),comments=copy(state().comments);
      const existingEdit=edits.find(e=>e.id===eid);
      comments=comments.filter(c=>c.id!==cid&&c.edit_id!==eid);
      if(!onlyComment){
        if(hasEditInstructionPrefix(replacement.value)){
          error.textContent='请删除操作说明，只填写所选原文的替换内容；前后原文会保留。';error.hidden=false;replacement.focus();return;
        }
        const change=minimalEdit(t,replacement.value);
        if(change.old_text===change.new_text){error.textContent='这段文字没有变化；如需沟通，可以选择“仅批注”。';error.hidden=false;return;}
        edits=edits.filter(e=>e.id!==eid);edits.push({id:eid,...change,finding_ids:[item.id]});
      }
      if(comment.value.trim())comments.push(onlyComment&&!existingEdit?{id:cid,text:comment.value,confirmed:true,
        piece_id:t.piece_id,paragraph_sha256:t.paragraph_sha256,start:t.start,end:t.end,old_text:t.old_text}:
        {id:cid,text:comment.value,confirmed:true,edit_id:eid});
      if(await save(edits,comments,author.value.trim(),error,onlyComment?'已添加批注':'已应用本条修订',onlyComment?null:'tracked'))$('change-dialog').close();
    };
    refresh(true);$('change-dialog').showModal();$('change-dialog').scrollTop=0;
  }
  function count() {
    if(data?.available)$('save-status').textContent='已修改 '+state().edits.length+' 处 · 批注 '+state().comments.length+' 条';
  }


  function decorateDocument() {
    const holder=$('document-mode-label');if(!holder)return;
    holder.replaceChildren();const select=el('select');select.id='document-preview-mode';select.setAttribute('aria-label','选择原稿或改稿预览');
    [['original','原稿'],['tracked','修订预览'],['clean','改后预览']].forEach(([v,t])=>select.add(new Option(t,v)));
    select.value=view;select.disabled=!data?.available;select.onchange=()=>{view=select.value;renderDocument();};holder.append(select);
    const legend=document.querySelector('.risk-legend');if(legend)legend.hidden=view!=='original';
    if(view==='original'||!data?.available)return;
    for(const paragraph of reportBundle.paragraphs){
      const block=$('p-'+paragraph.id),content=block?.querySelector('.clause,.chapter,.subheading,.body-title');if(!content)continue;
      const edits=state().edits.filter(e=>e.piece_id===paragraph.id&&e.kind!=='insert_paragraph_after').sort((a,b)=>a.start-b.start),chars=Array.from(paragraph.text);
      content.replaceChildren();content.classList.toggle('diff-preview',view==='tracked');let cursor=0;
      // Word's automatic numbering is not part of the paragraph text; keep it in both previews.
      if(paragraph.numbering_label)content.append(el('span','word-numbering-label',paragraph.numbering_label+' '));
      for(const edit of edits){content.append(document.createTextNode(chars.slice(cursor,edit.start).join('')));
        if(view==='tracked'&&edit.old_text)content.append(el('del','',edit.old_text));
        if(edit.new_text)content.append(view==='tracked'?el('ins','',edit.new_text):document.createTextNode(edit.new_text));cursor=edit.end;}
      content.append(document.createTextNode(chars.slice(cursor).join('')));
      for(const insertion of state().edits.filter(e=>e.piece_id===paragraph.id&&e.kind==='insert_paragraph_after')){const inserted=el('div','paragraph-block inserted-paragraph');for(const text of insertion.new_text.split(/\r?\n/)){const p=el('p','clause');p.append(view==='tracked'?el('ins','',text):document.createTextNode(text));inserted.append(p);}block.after(inserted);}
    }
  }
  function openPanel() {
    if(busy||!reportBundle)return;
    const body=$('delivery-body');body.replaceChildren();const form=el('form','delivery-form'),error=el('div','dialog-error');error.hidden=true;error.setAttribute('role','alert');
    if(!data?.available){form.append(el('p','dialog-help',data?.reason||'缺少原始DOCX。'),btn('下载审查报告','primary',()=>{location.href='/api/reports/'+reportBundle.id+'/export.docx';}));body.append(form);$('delivery-dialog').showModal();return;}
    form.append(el('p','delivery-count','已确认 '+state().edits.length+' 处改稿、'+state().comments.length+' 条批注'));
    if(!state().edits.length)form.append(el('p','dialog-help','还没有确认正文修改，下载的合同将保留原文。'));
    const list=el('ul','change-list');
    for(const edit of state().edits){
      const row=el('li','confirmed-change'),text=el('div');text.append(el('strong','',editLocation(edit)),editInContext(edit));
      row.append(text,btn('移除这处改稿','secondary',async()=>{
        if(await save(state().edits.filter(e=>e.id!==edit.id),state().comments.filter(c=>c.edit_id!==edit.id),state().author,error,'已移除改稿及其关联批注'))openPanel();
      }));list.append(row);
    }
    for(const comment of state().comments){const row=el('li','confirmed-change');row.append(el('p','','批注：'+comment.text),btn('移除批注','secondary',async()=>{
      if(await save(state().edits,state().comments.filter(c=>c.id!==comment.id),state().author,error,'已移除该批注'))openPanel();}));list.append(row);}
    if(list.children.length){const details=el('details','delivery-confirmed-details'),summary=el('summary','delivery-confirmed-toggle','查看已确认的修改（'+state().edits.length+'）和批注（'+state().comments.length+'）');
      summary.setAttribute('aria-expanded','false');details.addEventListener('toggle',()=>summary.setAttribute('aria-expanded',String(details.open)));details.append(summary,list);form.append(details);}
    const undoAction=state().undo_action;
    const undoText=undoAction?.kind==='edit'&&undoAction.piece_id?'撤回修改：'+position(undoAction.piece_id):undoAction?.kind==='comment'&&undoAction.text?'撤回批注：'+undoAction.text:undoAction?.label||'撤回上次文档操作';
    if(state().can_undo)form.append(btn(undoText,'secondary',async()=>{
      setBusy(true);try{const r=await request(endpoint('/undo'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:state().revision,source_sha256:data.source.source_sha256})});data.state=r.state;renderAll(false);openAfterUndo();}catch(e){error.textContent=e.message;error.hidden=false;}finally{setBusy(false);}
    }));
    function openAfterUndo(){setBusy(false);openPanel();notify('已撤销文档改稿／批注操作');}
    form.append(el('h3','','这次需要哪些文件？'));
    // Preselect the file that carries what was actually confirmed; no empty comment variants.
    const preselected=state().edits.length?(state().comments.length?'tracked_comments':'tracked'):state().comments.length?'comments':'report';
    const choices=[['clean','洁净版','直接使用改后正文'],['tracked','修订版','保留可接受／拒绝的修订'],['comments','原稿批注版','原文不改，只附批注'],['tracked_comments','修订＋批注版','同时交付删改和沟通说明'],['report','审查报告','意见概览、对应条款、修改建议及处理记录']];
    for(const [value,title,help] of choices){const label=el('label','variant-choice'),checkbox=el('input');checkbox.type='checkbox';checkbox.name='variant';checkbox.value=value;checkbox.checked=value===preselected;
      const text=el('span');text.append(el('strong','',title),el('small','',help));label.append(checkbox,text);form.append(label);}
    form.append(error);const footer=el('div','dialog-actions'),download=el('button','primary','下载所选文件');download.type='submit';footer.append(btn('返回阅读','secondary',()=>$('delivery-dialog').close()),download);form.append(footer);body.append(form);
    form.onsubmit=async event=>{
      event.preventDefault();const variants=[...form.querySelectorAll('input[name=variant]:checked')].map(e=>e.value);
      if(!variants.length){error.textContent='请至少选择一种文件。';error.hidden=false;return;}
      download.disabled=true;download.textContent='正在生成…';error.hidden=true;setBusy(true);
      try{
        const response=await fetch(endpoint('/export'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:state().revision,source_sha256:data.source.source_sha256,review_revision:reportBundle.state.revision,variants})});
        if(!response.ok){const value=await response.json();throw new Error(value.error||'导出失败');}
        const blob=await response.blob();
        if(response.status!==200||!blob.size||!response.headers.get('Content-Type')?.includes('application/zip'))throw new Error('没有收到完整交付文件，请重新下载。');
        const url=URL.createObjectURL(blob),link=el('a');link.href=url;const named=/filename\*=UTF-8''([^;]+)/i.exec(response.headers.get('Content-Disposition')||'');
        link.download=named?decodeURIComponent(named[1]):'合同交付文件.zip';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);notify('所选文件已生成：'+link.download);
      }catch(e){error.textContent=e.message;error.hidden=false;}
      finally{setBusy(false);download.disabled=false;download.textContent='下载所选文件';}
    };
    if(!$('delivery-dialog').open)$('delivery-dialog').showModal();$('delivery-dialog').scrollTop=0;
  }
  return {load,decorateDocument,count,openPanel,statusFor,statusLabel,suggestions,renderDiff,available,revision,undoFinding,openChange,needsPreparation,ensureCandidate,scope,hasCandidate,hasNewerDraft,candidateIssue,renderSuggestionScope};
})();
