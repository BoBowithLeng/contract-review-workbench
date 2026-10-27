(()=>{
  'use strict';
  const fields=['title','requirement','when','facts','guidance','notes'];
  const editableFields=['title','requirement','when','notes'];
  const labels={title:'检查事项',requirement:'检查内容',when:'适用情形',facts:'原可能需要的信息',guidance:'原处理建议',notes:'备注'};
  const clone=value=>JSON.parse(JSON.stringify(value));
  const el=(tag,text='',cls='')=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
  const btn=(text,action,cls='')=>{const n=el('button',text,cls);n.type='button';n.addEventListener('click',action);return n;};
  const uid=()=>globalThis.crypto?.randomUUID?.()||'item-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
  let catalog={revision:0,checklists:[]};
  let library=null;
  function normalizeItem(raw={}){const item={id:String(raw.id||uid()),enabled:raw.enabled!==false};for(const field of fields)item[field]=String(raw[field]??'');return item;}
  function normalizeList(raw={}){return {id:String(raw.id||''),name:String(raw.name||''),contract_type:String(raw.contract_type||''),stance:String(raw.stance||''),...(Object.hasOwn(raw,'scope')?{scope:String(raw.scope||'')}:{}),version:raw.version??1,builtin:raw.builtin===true,items:(raw.items||[]).map(normalizeItem)};}
  function legacyContent(item={}){return [String(item.requirement||'').trim(),...['facts','guidance'].filter(key=>String(item[key]||'').trim()).map(key=>'### '+labels[key]+'\n\n'+String(item[key]).trim())].filter(Boolean).join('\n\n');}
  function migrateItem(item){const value=normalizeItem(item);value.requirement=legacyContent(value);if(value.requirement.length>8000)throw new Error('“'+value.title+'”合并后的检查内容超过 8000 字，请先缩短内容再保存；原文字未丢弃。');value.facts='';value.guidance='';return value;}
  // Build only text nodes: Markdown never executes HTML, scripts, or links.
  function renderContent(target,value,renderInline){
    const wrap=el('div','','checklist-markdown');let paragraph=[],list=null;
    const content=(tag,text)=>{const n=el(tag);if(renderInline)renderInline(n,text);else n.textContent=text;return n;};
    const flush=()=>{if(paragraph.length){wrap.append(content('p',paragraph.join('\n')));paragraph=[];}};
    for(const line of String(value||'').split(/\r?\n/)){
      const heading=/^\s{0,3}#{1,6}\s+(.+)$/.exec(line),bullet=/^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+)$/.exec(line);
      if(heading){flush();list=null;const title=content('h4',heading[1]);title.setAttribute('data-md-prefix',/^\s{0,3}#{1,6}\s+/.exec(line)[0]);wrap.append(title);}
      else if(bullet){flush();const tag=/^\s*\d/.test(line)?'ol':'ul';if(!list||list.tagName!==tag.toUpperCase()){list=el(tag);wrap.append(list);}const entry=content('li',bullet[1]);entry.setAttribute('data-md-prefix',/^\s*(?:[-*+]\s+|\d+[.)]\s+)/.exec(line)[0]);list.append(entry);}
      else if(!line.trim()){flush();list=null;}
      else {list=null;paragraph.push(line);}
    }
    flush();target.append(wrap);return wrap;
  }
  async function request(path,payload){
    let response;
    try{response=await fetch(path,payload===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});}catch{throw new Error('无法连接本机清单库，请确认审查工作台仍在运行。');}
    const data=await response.json();
    if(!response.ok){const error=new Error(response.status===409?'清单库已被另一处修改，本次没有覆盖。草稿仍在；请重新读取清单库，或将草稿另存为新清单。':data.error||'清单操作失败（'+response.status+'）');error.status=response.status;throw error;}
    return data;
  }
  async function load(){const data=await request('/api/checklists');if(!Array.isArray(data.checklists))throw new Error('清单库返回的格式无法识别。');catalog={revision:data.revision,checklists:data.checklists.map(normalizeList)};return catalog;}
  function message(target,text){target.querySelector('.checklist-error')?.remove();const n=el('p',text,'checklist-error');n.setAttribute('role','alert');target.prepend(n);}
  function modal(title,className=''){
    const dialog=el('dialog','','work-dialog checklist-dialog '+className),head=el('header','','dialog-head'),heading=el('h2',title),body=el('div','','dialog-body');
    const close=btn('×',()=>dialog.dispatchEvent(new Event('cancel',{cancelable:true})),'dialog-close');close.setAttribute('aria-label','关闭'+title);
    head.append(heading,close);dialog.append(head,body);document.body.append(dialog);
    let closeAction=()=>dialog.close();
    dialog.addEventListener('cancel',event=>{event.preventDefault();closeAction();});dialog.addEventListener('close',()=>dialog.remove());
    return {dialog,body,heading,onClose:action=>{closeAction=action;},show:()=>dialog.showModal()};
  }
  function confirmAction(title,text,action='确认'){
    const box=modal(title,'checklist-confirm');box.body.append(el('p',text));
    return new Promise(resolve=>{let done=false;const finish=value=>{if(done)return;done=true;resolve(value);box.dialog.close();};box.onClose(()=>finish(false));box.body.append(btn('取消',()=>finish(false)),btn(action,()=>finish(true),'checklist-primary'));box.show();});
  }
  function textField(name,value,required=false){
    const label=el('label',labels[name]||({name:'清单名称',contract_type:'合同类型（可选）',scope:'适用范围',stance:'审查立场'})[name],'checklist-field');
    const input=el(['requirement','when','facts','guidance','notes','scope','stance'].includes(name)?'textarea':'input');input.name=name;input.value=value||'';input.required=required;input.maxLength=name==='name'?200:8000;
    if(input.tagName==='TEXTAREA')input.rows=name==='requirement'?4:3;
    if(name==='guidance')input.placeholder='每种情况单独写一行，例如：\n已约定验收期限：核对期限是否合理。\n未约定期限：考虑补充明确期限。';
    label.append(input);return {label,input};
  }
  function renderChecks(target,checks){
    target.replaceChildren();
    if(!checks?.length){target.append(el('p','未附加清单检查项；仍会进行合同全文通用审核。','checklist-muted'));return;}
    checks.forEach((item,index)=>{const row=el('section','','checklist-read-row');row.append(el('strong',checkCode(item.id,index)+' · '+item.title));renderContent(row,legacyContent(item));if(item.enabled===false)row.append(el('small','本次未启用','checklist-muted'));appendDetails(row,item);target.append(row);});
  }
  function appendDetails(row,item){
    const extra=['when','notes'].filter(key=>item[key]);if(!extra.length)return;
    const details=el('details','','checklist-details');details.append(el('summary','补充说明'));
    for(const key of extra){const section=el('section');section.append(el('h4',labels[key]));renderContent(section,item[key]);details.append(section);}
    row.append(details);
  }
  const children=node=>Array.from(node.childNodes||node.children||[]);
  function plainText(node){
    if(['SCRIPT','STYLE','IFRAME','OBJECT','IMG','SVG'].includes(node.tagName))return '';
    if(node.tagName==='BR')return '\n';
    const nodes=children(node);if(!nodes.length)return String(node.textContent||'');
    return nodes.map(child=>plainText(child)+(['DIV','P'].includes(child.tagName)?'\n':'')).join('');
  }
  function serializeRich(root){
    function block(node,depth=0){
      if(['SCRIPT','STYLE','IFRAME','OBJECT','IMG','SVG'].includes(node.tagName))return '';
      const text=plainText(node).trim();
      if(/^H[1-6]$/.test(node.tagName||''))return (node.getAttribute?.('data-md-prefix')||'### ')+text;
      if(['UL','OL'].includes(node.tagName))return children(node).map((item,index)=>{
        if(item.tagName!=='LI')return block(item,depth);
        const parts=children(item),nested=parts.filter(part=>['UL','OL'].includes(part.tagName));
        const own=parts.length?parts.filter(part=>!nested.includes(part)).map(plainText).join('').trim():String(item.textContent||'').trim();
        return (item.getAttribute?.('data-md-prefix')||'  '.repeat(depth)+(node.tagName==='OL'?(index+1)+'. ':'- '))+own+(nested.length?'\n'+nested.map(part=>block(part,depth+1)).join('\n'):'');
      }).filter(Boolean).join('\n');
      if(node.tagName==='DIV'&&children(node).some(part=>['P','DIV','H1','H2','H3','H4','H5','H6','UL','OL'].includes(part.tagName)))return children(node).map(part=>block(part,depth)).filter(Boolean).join('\n\n');
      return text;
    }
    const nodes=children(root);return (nodes.length?nodes.map(node=>block(node)).filter(Boolean).join('\n\n'):String(root.textContent||'')).trim();
  }
  function insertPlain(root,text,multiline=true){
    text=String(text||'').replace(/\r\n?/g,'\n');if(!multiline)text=text.replace(/\n+/g,' ');
    const selection=window.getSelection?.();
    if(selection?.rangeCount&&root.contains(selection.anchorNode)&&root.contains(selection.focusNode)){
      const range=selection.getRangeAt(0);range.deleteContents();const content=document.createTextNode(text);range.insertNode(content);range.setStartAfter(content);range.collapse(true);selection.removeAllRanges();selection.addRange(range);
    }else root.append(document.createTextNode(text));
    root.dispatchEvent(new Event('input',{bubbles:true}));
  }
  function editable(element,label,{multiline=true}={}){
    element.contentEditable='true';element.setAttribute('contenteditable','true');element.setAttribute('role','textbox');element.setAttribute('aria-label',label);element.setAttribute('aria-multiline',String(multiline));element.className+=' checklist-editable';
    element.addEventListener('paste',event=>{event.preventDefault();insertPlain(element,event.clipboardData?.getData('text/plain')||'',multiline);});
    element.addEventListener('drop',event=>{event.preventDefault();insertPlain(element,event.dataTransfer?.getData('text/plain')||'',multiline);});
    element.addEventListener('beforeinput',event=>{if(/^format/.test(event.inputType||''))event.preventDefault();});
    if(!multiline)element.addEventListener('keydown',event=>{if(event.key==='Enter')event.preventDefault();});
    return element;
  }
  function richEditor(target,value,name){
    const original=String(value||''),root=renderContent(target,original);root.name=name;editable(root,labels[name]);const initial=serializeRich(root);
    return {element:root,value:()=>{const current=serializeRich(root);return current===initial?original:current;}};
  }
  function lockOutside(root,exception,locked,previous){
    if(!locked){for(const [control,disabled] of previous)control.disabled=disabled;previous.clear();return;}
    for(const control of root.querySelectorAll('button,input,textarea,select'))if(!exception?.contains(control)&&!previous.has(control)){previous.set(control,control.disabled);control.disabled=true;}
  }
  function newList(base=null){const list=normalizeList(base||{});list.id='';list.builtin=false;list.version=1;if(base){list.name+='（副本）';list.items=list.items.map(item=>({...item,id:uid()}));}return list;}
  async function save(list,revision){const data=await request('/api/checklists/save',{checklist:{...normalizeList(list),items:list.items.map(migrateItem)},expected_revision:revision});catalog.revision=data.revision;const value=normalizeList(data.checklist);catalog.checklists=catalog.checklists.filter(c=>c.id!==value.id).concat(value);window.dispatchEvent(new Event('checklists-changed'));return value;}
  function renderEditor(container,getDraft,{readonly=false,onChange=()=>{},onEditingChange=()=>{},onSave=null,onConflictSave=null,onSaved=()=>{}}={}){
    const draft=getDraft();container.replaceChildren();let activeIndex=null,newIndex=null,saving=false;const lockedControls=new Map();
    const heading=el('div','','checklist-heading');const title=el('h1',draft.name||'新建清单');heading.append(title);if(draft.contract_type)heading.append(el('p',draft.contract_type,'checklist-muted'));renderMetadata(heading,draft);container.append(heading);
    if(!readonly){const info=el('details','','checklist-list-info');info.open=!draft.id;info.append(el('summary','修改清单名称、类型与说明'));const grid=el('div','','checklist-info-grid');for(const key of ['name','contract_type','scope','stance']){const f=textField(key,draft[key],key==='name');f.input.addEventListener('input',()=>{getDraft()[key]=f.input.value;if(key==='name')title.textContent=f.input.value||'新建清单';onChange();});grid.append(f.label);}info.append(grid);container.append(info);}
    const wrap=el('div','','checklist-table-wrap'),table=el('table','','checklist-table'),thead=el('thead'),header=el('tr');
    for(const label of ['启用','检查事项','检查内容','操作'])header.append(el('th',label));thead.append(header);const rows=el('tbody');table.append(thead,rows);wrap.append(table);container.append(wrap);
    function finish(){activeIndex=null;newIndex=null;saving=false;lockOutside(container,null,false,lockedControls);onEditingChange(false,null);redraw();onChange();}
    function cancel(){if(saving)return;if(newIndex!==null)getDraft().items.splice(newIndex,1);finish();}
    function begin(index,isNew=false){if(activeIndex!==null)return;if(!getDraft().name.trim()){message(container,'请先填写清单名称，再编辑检查项。');container.querySelector('input')?.focus();return;}activeIndex=index;if(isNew)newIndex=index;redraw();const row=rows.children[index];lockOutside(container,row,true,lockedControls);onEditingChange(true,row);row.querySelector('.checklist-editable')?.focus();}
    function redraw(){rows.replaceChildren();const items=getDraft().items;items.forEach((item,index)=>{
      const editing=index===activeIndex,row=el('tr','','checklist-item-row'+(editing?' is-editing':'')),enabledCell=el('td'),titleCell=el('td'),content=el('td','','checklist-row-content'),actionCell=el('td'),toggle=el('input');toggle.type='checkbox';toggle.checked=item.enabled;toggle.disabled=readonly;toggle.setAttribute('aria-label',item.title+'：启用');toggle.addEventListener('change',()=>{if(!editing){item.enabled=toggle.checked;onChange();}});enabledCell.append(toggle);
      const titleLine=el('div','','checklist-title-line'),itemTitle=el('strong',item.title||(!editing?'未填写检查事项':''));titleLine.append(el('small',checkCode(item.id,index),'checklist-row-number'),itemTitle);titleCell.append(titleLine);
      const actions=el('div','','checklist-row-actions');
      if(editing){
        editable(itemTitle,labels.title,{multiline:false});itemTitle.name='title';
        const requirement=richEditor(content,legacyContent(item),'requirement');
        const extra={};for(const key of ['when','notes']){const section=el('section','','checklist-row-extra');section.append(el('h4',labels[key]));const root=el('div',item[key]||'','checklist-inline-text');root.name=key;root.setAttribute('data-placeholder',key==='when'?'无特殊适用情形可留空':'可留空');editable(root,labels[key]);section.append(root);content.append(section);extra[key]=root;}
        let recovery=null;const editors=[itemTitle,requirement.element,extra.when,extra.notes];
        const saveRow=btn('保存',()=>persistRow(false),'checklist-primary'),cancelRow=btn('取消',cancel);
        async function persistRow(asNew){
          if(saving)return;row.querySelector('.checklist-error')?.remove();const changed={id:item.id,enabled:toggle.checked,title:plainText(itemTitle).trim(),requirement:requirement.value().trim(),when:plainText(extra.when).trim(),notes:plainText(extra.notes).trim(),facts:'',guidance:''};
          const candidate=clone(getDraft());candidate.items[index]=changed;
          if(!validDraft(candidate,row))return;for(const key of editableFields)if(changed[key].length>8000){message(content,labels[key]+'超过 8000 字，请缩短后保存。');return;}
          saving=true;saveRow.disabled=true;cancelRow.disabled=true;toggle.disabled=true;if(recovery)recovery.disabled=true;for(const control of editors){control.contentEditable='false';control.setAttribute('contenteditable','false');}saveRow.textContent='保存中…';
          try{if(asNew)await onConflictSave(candidate);else if(onSave)await onSave(candidate);else getDraft().items[index]=changed;finish();onSaved();}
          catch(error){
            message(content,error.status===409&&onConflictSave?'清单已在另一处修改，这次没有覆盖。当前编辑可以另存为新清单。':error.message);saving=false;saveRow.disabled=false;cancelRow.disabled=false;toggle.disabled=false;for(const control of editors){control.contentEditable='true';control.setAttribute('contenteditable','true');}saveRow.textContent='保存';
            if(error.status===409&&onConflictSave&&!recovery){recovery=btn('另存为新清单',()=>persistRow(true),'checklist-conflict-save');actions.append(recovery);}if(recovery)recovery.disabled=false;
          }
        }
        actions.append(saveRow,cancelRow);row.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();cancel();}});
      }else {
        renderContent(content,legacyContent(item)||'未填写检查内容');for(const key of ['when','notes'])if(item[key]){const section=el('section','','checklist-row-extra');section.append(el('h4',labels[key]));renderContent(section,item[key]);content.append(section);}
        if(readonly)actions.append(el('small','只读','checklist-muted'));else {actions.append(btn('编辑',()=>begin(index)));for(const [label,step] of [['↑',-1],['↓',1]]){const move=btn(label,()=>{if(activeIndex!==null)return;const next=index+step;[items[index],items[next]]=[items[next],items[index]];redraw();onChange();});move.disabled=index+step<0||index+step>=items.length;move.setAttribute('aria-label',item.title+(step<0?'上移':'下移'));actions.append(move);}actions.append(btn('删除',async()=>{if(activeIndex!==null)return;if(await confirmAction('删除检查项','从当前草稿删除“'+item.title+'”？保存清单后才会生效。','删除检查项')){items.splice(index,1);redraw();onChange();}},'checklist-remove'));}
      }
      actionCell.append(actions);row.append(enabledCell,titleCell,content,actionCell);rows.append(row);
    });if(!items.length){const row=el('tr'),cell=el('td','还没有检查项，点击下方新增。','checklist-muted');cell.colSpan=4;row.append(cell);rows.append(row);}}
    redraw();if(!readonly){container.append(btn('＋ 新增检查项',()=>{if(activeIndex!==null)return;if(!getDraft().name.trim()){message(container,'请先填写清单名称，再新增检查项。');container.querySelector('input')?.focus();return;}getDraft().items.push(normalizeItem({title:'',requirement:''}));begin(getDraft().items.length-1,true);},'checklist-add'));}
    return {isEditing:()=>activeIndex!==null,cancel};
  }
  function validDraft(draft,target){if(!draft.name.trim()){message(target,'请填写清单名称。');return false;}const invalid=draft.items.find(item=>!item.title.trim()||!item.requirement.trim());if(invalid){message(target,'有检查项未填写检查事项或检查内容，请编辑补齐。');return false;}if(!draft.items.length){message(target,'请至少添加一项检查事项。');return false;}return true;}
  async function open(){
    const host=document.getElementById('checklist-page-host');
    if(!host){window.location.assign('/checklists.html');return;}
    if(library?.dialog.open){library.dialog.focus();return;}
    const box={body:host,dialog:host,show:()=>{},onClose:()=>{}};host.open=true;library=box;const tools=el('div','','checklist-actions checklist-library-tools'),layout=el('div','','checklist-library-layout'),nav=el('nav','','checklist-nav'),main=el('div','','checklist-library-main'),status=el('p','','checklist-muted');
    let draft=null,base='',revision=0,busy=false,rowEditing=false;const externalLocks=new Map();
    const dirty=()=>rowEditing||draft&&JSON.stringify(draft)!==base;
    const leave=async action=>{if(busy)return;if(rowEditing){message(main,'请先保存或取消正在编辑的这一行。');return;}if(!dirty()||await confirmAction('清单还未保存','离开会丢弃这个窗口里的修改。','放弃修改'))action();};
    for(const id of ['checklist-back','checklist-return'])document.getElementById(id)?.addEventListener('click',event=>{event.preventDefault();leave(()=>{base=JSON.stringify(draft);window.location.assign('/');});});
    window.addEventListener('beforeunload',event=>{if(dirty()){event.preventDefault();event.returnValue='';}});
    const refreshStatus=()=>{status.textContent=rowEditing?'这一行正在编辑，保存后会写入本机清单库':draft?.builtin?'内置清单只读，复制后可以修改。':dirty()?'有未保存的修改':'已保存到本机清单库';};
    function select(list){draft=clone(list);base=JSON.stringify(draft);revision=catalog.revision;render();}
    function render(){nav.replaceChildren();for(const builtin of [true,false]){nav.append(el('h3',builtin?'内置清单':'我的清单'));const lists=catalog.checklists.filter(c=>c.builtin===builtin);if(!lists.length)nav.append(el('p',builtin?'暂无内置清单':'还没有个人清单','checklist-muted'));for(const list of lists){const b=btn(list.name,()=>leave(()=>select(list)));b.setAttribute('aria-pressed',String(draft?.id===list.id));nav.append(b);}}
      main.replaceChildren();if(!draft){main.append(el('p','新建清单，或选择左边的清单。'));return;}const actions=el('div','','checklist-actions');actions.append(btn('复制为我的清单',()=>leave(()=>{draft=newList(draft);base='';revision=catalog.revision;render();})));
      if(!draft.builtin){const saveBtn=btn('保存清单',async()=>{if(busy||rowEditing||!validDraft(draft,main))return;busy=true;saveBtn.disabled=true;try{select(await save(draft,revision));}catch(error){message(main,error.message);}finally{busy=false;saveBtn.disabled=false;}},'checklist-primary');actions.append(saveBtn,btn('另存为新清单',async()=>{if(busy||rowEditing||!validDraft(draft,main))return;busy=true;try{await load();const copy=newList(draft);select(await save(copy,catalog.revision));}catch(error){message(main,error.message);}finally{busy=false;}}));if(draft.id)actions.append(btn('删除清单',async()=>{if(busy||rowEditing||!await confirmAction('删除清单','删除“'+draft.name+'”？已经送审的合同仍保留当时使用的检查项。','删除清单'))return;busy=true;try{const data=await request('/api/checklists/delete',{id:draft.id,expected_revision:revision});catalog.revision=data.revision;catalog.checklists=catalog.checklists.filter(c=>c.id!==data.deleted_id);draft=null;base='';render();window.dispatchEvent(new Event('checklists-changed'));}catch(error){message(main,error.message);}finally{busy=false;}},'checklist-remove'));}
      renderEditor(main,()=>draft,{readonly:draft.builtin,onChange:refreshStatus,onEditingChange:value=>{rowEditing=value;lockOutside(tools,null,value,externalLocks);lockOutside(nav,null,value,externalLocks);refreshStatus();},onSave:async candidate=>{busy=true;try{const saved=await save(candidate,revision);draft=saved;base=JSON.stringify(saved);revision=catalog.revision;}finally{busy=false;}},onConflictSave:async candidate=>{busy=true;try{await load();const saved=await save(newList(candidate),catalog.revision);draft=saved;base=JSON.stringify(saved);revision=catalog.revision;}finally{busy=false;}},onSaved:render});main.querySelector('.checklist-heading').append(status,actions);refreshStatus();
    }
    tools.append(btn('＋ 新建清单',()=>leave(()=>{draft=newList();base='';revision=catalog.revision;render();}),'checklist-primary'),btn('重新读取清单库',()=>leave(async()=>{try{await load();select(catalog.checklists.find(c=>c.id===draft?.id)||catalog.checklists[0]||newList());}catch(error){message(box.body,error.message);}})));
    const download=el('a','下载 Excel 模板','checklist-download');download.href='/api/checklists/template.xlsx';download.download='审核清单模板.xlsx';tools.append(download);
    const upload=el('input');upload.type='file';upload.accept='.xlsx';upload.hidden=true;tools.append(btn('导入 Excel',()=>upload.click()),upload);upload.addEventListener('change',async()=>{const file=upload.files?.[0];upload.value='';if(!file)return;await leave(async()=>{try{const imported=await importExcel(file);if(imported){select(imported);}}catch(error){message(box.body,error.message);}});});
    layout.append(nav,main);box.body.append(tools,layout);box.show();main.append(el('p','正在读取清单…'));try{await load();if(catalog.checklists.length)select(catalog.checklists[0]);else render();}catch(error){message(box.body,error.message);}
  }
  function base64(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new Error('无法读取 Excel 文件。'));reader.onload=()=>resolve(String(reader.result).split(',',2)[1]||'');reader.readAsDataURL(file);});}
  function repairable(error){return ['title','requirement'].includes(error.field)&&error.code==='required';}
  async function importExcel(file){
    if(!/\.xlsx$/i.test(file.name))throw new Error('请选择 .xlsx Excel 文件。');if(file.size>2*1024*1024)throw new Error('Excel 超过 2 MB，请缩小后重新上传。');
    const data=await request('/api/checklists/import',{filename:file.name,xlsx_base64:await base64(file)});
    if(!Array.isArray(data.rows))throw new Error('Excel 预览格式无法识别。');
    const box=modal('预览并保存导入清单','checklist-import'),form=el('form','','checklist-form'),name=textField('name',file.name.replace(/\.xlsx$/i,''),true),rows=data.rows.map(normalizeItem);
    form.append(el('p','先检查内容，再确认保存为新清单。检查事项与检查内容可以在这里补齐。'),name.label);
    for(const warning of data.warnings||[]){const text=typeof warning==='string'?warning:(warning.row_number?'Excel 第 '+warning.row_number+' 行 · ':'')+(labels[warning.field]||warning.field||'')+'：'+(warning.message||'')+(warning.value?'\n未导入的原内容：'+warning.value:'');form.append(el('p',text,'checklist-muted'));}
    const blocking=(data.errors||[]).filter(error=>!repairable(error));
    for(const error of blocking)form.append(el('p','Excel 第 '+(error.row_number||'?')+' 行 · '+(labels[error.field]||error.field||'文件')+'：'+error.message+'。请修正文件后重新上传。','checklist-error'));
    if(!rows.length)form.append(el('p','没有可导入的检查项。请填写清单工作表后重新上传。','checklist-error'));
    rows.forEach((row,index)=>{const section=el('section','','checklist-import-row');section.append(el('h3','Excel 第 '+(data.rows[index].row_number||index+2)+' 行'));for(const key of ['title','requirement']){const f=textField(key,row[key],true),error=el('small','','checklist-error');const validate=()=>{row[key]=f.input.value;error.textContent=row[key].trim()?'':'必填，请在这里补齐';};f.input.addEventListener('input',validate);f.label.append(error);validate();section.append(f.label);}appendDetails(section,row);form.append(section);});
    const omitted=(data.warnings||[]).some(warning=>warning.code==='unmapped_content');const acknowledge=el('input');acknowledge.type='checkbox';acknowledge.checked=!omitted;
    const saveBtn=el('button','确认保存为新清单','checklist-primary');saveBtn.type='submit';const updateSave=()=>{saveBtn.disabled=blocking.length>0||rows.length===0||!acknowledge.checked;};updateSave();
    if(omitted){const label=el('label','我已看过上面未导入的原内容，确认先保存这些可识别的检查项。','checklist-toggle');label.prepend(acknowledge);form.append(label);acknowledge.addEventListener('change',updateSave);}
    box.body.append(form);
    return new Promise(resolve=>{let done=false;const finish=value=>{if(done)return;done=true;resolve(value);box.dialog.close();};box.onClose(()=>finish(null));const actions=el('div','','checklist-actions');actions.append(btn('取消',()=>finish(null)),saveBtn);form.append(actions);form.addEventListener('submit',async event=>{event.preventDefault();if(saveBtn.disabled||!form.reportValidity())return;const draft=normalizeList({name:name.input.value.trim(),items:rows});if(!validDraft(draft,form))return;saveBtn.disabled=true;try{const value=await save(draft,catalog.revision);finish(value);}catch(error){message(form,error.message);}finally{updateSave();}});box.show();});
  }
  function renderMetadata(target,list){
    for(const [key,label] of [['scope','适用范围'],['stance','审查立场']]){
      if(!list[key])continue;
      if(list[key].length<180){const part=el('section','','checklist-metadata');part.append(el('strong',label));renderContent(part,list[key]);target.append(part);}
      else {const part=el('details','','checklist-metadata'),summary=el('summary',label+' · 展开原文');summary.append(el('span',list[key],'checklist-metadata-preview'));part.append(summary);renderContent(part,list[key]);target.append(part);}
    }
  }
  function renderSnapshotStep(host,snapshot,{onApply,onCancel}){
    let draft=clone(snapshot),rowEditing=false;const locks=new Map();host.replaceChildren();
    host.append(el('h3','查看与调整检查项'),el('p','这里的检查项调整只用于这份合同，常用清单原版不会变化。','checklist-muted'));
    const editor=el('div'),actions=el('div','','checklist-actions checklist-step-actions');host.append(actions,editor);
    renderEditor(editor,()=>draft,{onEditingChange:value=>{rowEditing=value;lockOutside(actions,null,value,locks);}});
    const canLeave=()=>{if(rowEditing){message(editor,'请先保存或取消正在编辑的这一行。');return false;}return true;};
    actions.append(btn('取消并返回',()=>{if(canLeave())onCancel();}),btn('另存为新清单',async()=>{if(!canLeave()||!validDraft(draft,host))return;try{await load();const saved=await save(newList(draft),catalog.revision);message(host,'已另存为“'+saved.name+'”。');}catch(error){message(host,error.message);}}),btn('保存并返回',()=>{if(!canLeave()||!validDraft(draft,host))return;try{onApply({...clone(draft),items:draft.items.map(migrateItem)});}catch(error){message(host,error.message);}},'checklist-primary'));
  }
  function mountPicker(container,{openStep}={}){
    const section=el('section','','checklist-picker'),label=el('label','检查清单','checklist-field'),select=el('select'),summary=el('div','','checklist-picker-summary'),errorBox=el('div');select.name='checklist_id';select.required=true;label.append(select);let snapshot=null;
    const render=()=>{summary.replaceChildren();if(!snapshot)return;summary.append(el('p',snapshot.name+' · '+snapshot.items.filter(item=>item.enabled).length+' 项启用'));renderMetadata(summary,snapshot);summary.append(btn('查看与调整检查项',()=>{if(openStep)openStep(host=>renderSnapshotStep(host,snapshot,{onApply:value=>{snapshot=value;render();openStep(null);},onCancel:()=>openStep(null)}));}));};
    select.addEventListener('change',()=>{const list=catalog.checklists.find(c=>c.id===select.value);snapshot=list?clone(list):null;render();});section.append(label,summary,errorBox);container.append(section);
    async function refresh(){try{await load();select.replaceChildren();for(const list of catalog.checklists){const option=el('option',list.name+(list.builtin?'（内置）':''));option.value=list.id;select.append(option);}if(!snapshot)snapshot=clone(catalog.checklists.find(c=>c.id==='builtin-general-v01')||catalog.checklists[0]||null);select.value=snapshot?.id||'';errorBox.replaceChildren();render();}catch(error){errorBox.replaceChildren();message(errorBox,error.message);errorBox.append(btn('重试读取清单',refresh));render();}}
    const ready=refresh();return {ready,snapshot:()=>snapshot?clone(snapshot):null};
  }
  const resultViews=new Map();
  const checkCode=(id,index)=>/^[CG]\d+$/.test(id||'')?id:'第'+(index+1)+'项';
  function resultRows(report){
    const results=(report.checklist_results||[]).map(item=>({...item,check_id:item.check_id||item.id}));
    const checks=(report.preparation?.checks||[]).filter(check=>check.enabled!==false),seen=new Set();
    const rows=checks.map(check=>{seen.add(check.id);return {...check,...(results.find(row=>row.check_id===check.id)||{status:'未完成',reason:'本次报告缺少这项检查的回应。',finding_ids:[]}),check_id:check.id,original:check};});
    return rows.concat(results.filter(row=>!seen.has(row.check_id)));
  }
  function resultStatus(status){
    if(status==='未检出')return {kind:'pass',label:'未发现问题'};
    if(['发现风险','发现问题'].includes(status))return {kind:'problem',label:'发现问题'};
    if(['信息不足','需要法务判断','待确认','待核实'].includes(status))return {kind:'pending',label:'待确认'};
    if(status==='不适用')return {kind:'na',label:'不适用'};
    if(['处理失败','未完成','未检查'].includes(status))return {kind:'broken',label:'尚未完成检查'};
    return {kind:'unknown',label:status||'尚无检查结果'};
  }
  function resultReason(row){
    if(resultStatus(row.status).kind==='broken')return row.reason==='本次报告缺少这项检查的回应。'?row.reason:'这项检查还没完成，暂时没有可用的风险结论。';
    return row.reason||'未提供检查说明。';
  }
  function statusLight(status){const n=el('span','','check-status-light '+status.kind);n.append(el('i'),el('span',status.label));return n;}
  function originalGroups(row){
    const check=row.original;
    if(check?.groups?.length)return check.groups.map(g=>({...g}));
    const value=check?legacyContent(check):'';if(!value)return [];
    const pieces=value.split(/(?=^\s{0,3}#{1,6}\s+)/m).filter(part=>part.trim());
    return pieces.map((part,index)=>{const heading=/^\s{0,3}#{1,6}\s+([^\n]+)(?:\n|$)/.exec(part);return {group_id:row.check_id+':legacy-'+index,title:heading?heading[1]:pieces.length===1?row.title:'其他检查内容',requirement:part};});
  }
  function renderResults(container,report,onFinding=()=>{},options={}){
    const rows=resultRows(report),key=options.reportId||report;
    let view=resultViews.get(key);if(!view){view={selected:rows[0]?.check_id,page:0,open:new Set(),groups:new Map(),attention:false,scroll:0,listScroll:0};resultViews.set(key,view);}
    const human=value=>window.ReaderText?.humanize(value,options.textContext||{}, {plain:true,shortLocations:true,hideUnknown:true})||String(value||'');
    const paragraphs=options.textContext?.paragraphs||[];
    const paragraphById=new Map(),findingById=new Map();
    for(const p of paragraphs){paragraphById.set(p.id,p);if(/^P\d+$/.test(p.id))paragraphById.set('P'+Number(p.id.slice(1)),p);}
    for(const f of report.findings||[]){findingById.set(f.id,f);if(/^F\d+$/.test(f.id))findingById.set('F'+Number(f.id.slice(1)),f);}
    const groupState=(row,g)=>{const result=row.groups?.find(item=>item.group_id===g.group_id);return result?resultStatus(result.status):{kind:'unknown',label:'尚无分组结果'};};
    const attention=row=>!['pass','na'].includes(resultStatus(row.status).kind)||originalGroups(row).some(g=>!['pass','na'].includes(groupState(row,g).kind));
    function draw(){
      const before=container.scrollTop||0;container.replaceChildren();container.hidden=false;
      if(!rows.length){container.append(el('h2','清单检查情况'),el('p','这份历史报告没有保存逐项清单结果。','checklist-muted'));return;}
      const visible=rows.filter(row=>!view.attention||attention(row));
      if(!visible.some(row=>row.check_id===view.selected))view.selected=visible[0]?.check_id;
      const total=Math.max(1,Math.ceil(visible.length/10));view.page=Math.min(view.page,total-1);
      const intro=el('div','','check-results-intro');intro.append(el('h1','清单检查情况'),el('p','左边是送审时的检查要求，右边是检查结果。具体意见、讨论和修改可到“审阅意见”查看。','checklist-muted'));container.append(intro);
      const layout=el('div','','check-results-layout'),menu=el('aside','','check-results-menu'),panel=el('section','','check-results-panel');layout.append(menu,panel);container.append(layout);
      const controls=el('div','','check-menu-controls');controls.append(el('div','共 '+rows.length+' 项 · 每页 10 项','checklist-muted'));
      const filter=btn(view.attention?'显示全部':'只看需关注',()=>{view.attention=!view.attention;view.page=0;view.selected=rows.find(row=>!view.attention||attention(row))?.check_id;view.listScroll=0;draw();},'check-attention');filter.setAttribute('aria-pressed',String(view.attention));controls.append(filter);menu.append(controls);
      const list=el('div','','check-results-items');list.setAttribute('aria-label','检查项列表');menu.append(list);
      for(const row of visible.slice(view.page*10,view.page*10+10)){
        const item=btn('',()=>{view.listScroll=list.scrollTop||0;view.selected=row.check_id;draw();},'check-results-item');item.setAttribute('aria-pressed',String(view.selected===row.check_id));
        const title=el('span','','check-item-title');title.append(el('span',checkCode(row.check_id,rows.indexOf(row)),'check-code'),el('span',human(row.title||'未命名检查项')));item.append(title,statusLight(resultStatus(row.status)));if(row.source==='request'||/^REQ-/.test(row.check_id))item.append(el('small','本次审查要求的检查结果','checklist-muted'));list.append(item);
      }
      list.scrollTop=view.listScroll;
      const pager=el('nav','','check-results-pager');pager.setAttribute('aria-label','检查项分页');
      for(const [label,step] of [['上一页',-1],['下一页',1]]){const b=btn(label,()=>{view.page+=step;view.selected=visible[view.page*10]?.check_id;view.listScroll=0;draw();});b.disabled=step<0?view.page===0:view.page>=total-1;if(step===1)pager.append(el('span',(view.page+1)+' / '+total+' 页'));pager.append(b);}menu.append(pager);
      const row=rows.find(item=>item.check_id===view.selected);
      if(!row){panel.append(el('p','没有需要关注的检查项。','checklist-muted'));return;}
      const head=el('header','','check-panel-head'),heading=el('div','','check-heading-row'),title=el('h2');title.append(el('span',checkCode(row.check_id,rows.indexOf(row)),'check-code'),el('span',human(row.title)));if(row.source==='request'||/^REQ-/.test(row.check_id))head.append(el('span','本次审查要求的检查结果','checklist-muted'));heading.append(title,statusLight(resultStatus(row.status)));head.append(heading);panel.append(head);
      const compare=el('div','','check-results-compare'),original=el('div','','check-original'),resultPane=el('div','','check-group-result');compare.append(original,resultPane);panel.append(compare);
      const groups=originalGroups(row),visibleGroups=groups.filter(g=>!view.attention||!['pass','na'].includes(groupState(row,g).kind));
      let chosen=view.groups.get(row.check_id);if(!visibleGroups.some(g=>g.group_id===chosen)){chosen=visibleGroups[0]?.group_id;view.groups.set(row.check_id,chosen);}
      const top=el('div','','check-column-top');top.append(el('h3','原来要求检查什么'),el('span','清单原文 · '+checkCode(row.check_id,rows.indexOf(row)),'checklist-muted'));
      const fold=btn('',()=>{const close=visibleGroups.every(g=>view.open.has(g.group_id));visibleGroups.forEach(g=>close?view.open.delete(g.group_id):view.open.add(g.group_id));draw();},'check-fold-all');
      const updateFold=()=>fold.textContent=visibleGroups.length&&visibleGroups.every(g=>view.open.has(g.group_id))?'全部收起':'全部展开';updateFold();fold.disabled=!visibleGroups.length;top.append(fold);original.append(top);
      function remember(){view.scroll=container.scrollTop||0;view.listScroll=list.scrollTop||0;}
      function links(target,ids,linked=new Set()){for(const id of new Set(ids||[])){const f=findingById.get(id);if(f&&!linked.has(f.id))target.append(btn('查看意见：'+human(window.ReaderText?.findingTitle(f,report)||f.title),()=>{remember();onFinding(f.id);},'checklist-finding-link'));else if(!f)target.append(el('p','相关审阅意见不在本次报告中。','checklist-error'));}}
      function resultText(target,result){
        const linked=new Set();
        const protectedParts=/(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]+`|\[[^\]\n]*\]\([^\n)]*\)|https?:\/\/[^\s<>\])]+)/g;
        const references=/(?<![A-Za-z0-9_])((?:P\d+|T\d+R\d+C\d+P\d+))(?:\s*[-—–~～至]\s*((?:P\d+|T\d+R\d+C\d+P\d+)))?(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])(F\d+)(?![A-Za-z0-9_])/g;
        const location=p=>{let label=window.ReaderText?.shortReference(p)||'对应原文';if(/原文[“"]/.test(label))label=label.startsWith('附件')?'附件原文':'对应原文';const attachment=String(p.location||'').split(/\s*\/\s*/).find(part=>/^附件[：:]/.test(part));if(attachment&&label.startsWith('附件'))label='附件《'+attachment.replace(/^附件[：:]\s*/,'')+'》'+label.slice(2);return label;};
        const jump=(parent,p)=>{const b=btn(location(p),()=>{remember();options.onParagraph?.(p.id,result.finding_ids||[]);},'check-inline-reference');b.title=[p.location,p.text].filter(Boolean).join('\n');b.setAttribute('aria-label','查看'+location(p)+'：'+String(p.text||'').slice(0,60));parent.append(b);};
        renderContent(target,resultReason(result),(parent,value)=>{
          for(const [index,part] of value.split(protectedParts).entries()){
            if(index%2){parent.append(document.createTextNode(part));continue;}
            let start=0;
            for(const match of part.matchAll(references)){
              parent.append(document.createTextNode(human(part.slice(start,match.index))));
              const p=paragraphById.get(match[1]),end=paragraphById.get(match[2]),f=findingById.get(match[3]);
              if(f){const label='意见'+((report.findings||[]).findIndex(item=>item.id===f.id)+1);const b=btn(label,()=>{remember();onFinding(f.id);},'check-inline-reference');b.title=human(window.ReaderText?.findingTitle(f,report)||f.title);b.setAttribute('aria-label','查看意见：'+b.title);parent.append(b);linked.add(f.id);}
              else if(p&&(!match[2]||end)&&options.onParagraph){jump(parent,p);if(end&&end.id!==p.id&&location(end)!==location(p)){parent.append(document.createTextNode('至'));jump(parent,end);}}
              else parent.append(document.createTextNode(human(match[0])));
              start=match.index+match[0].length;
            }
            parent.append(document.createTextNode(human(part.slice(start))));
          }
        });
        links(target,result.finding_ids,linked);
      }
      const overallReason=human(resultReason(row)),longOverall=overallReason.length>220||overallReason.split('\n').length>3;
      const overall=el(longOverall?'details':'section','','check-overall-result');overall.append(el(longOverall?'summary':'h3','整项检查说明'));if(longOverall)overall.open=!row.groups?.length;
      resultText(overall,row);head.append(overall);
      const detailsById=new Map();
      function result(){
        resultPane.replaceChildren();resultPane.append(el('h3','这次检查得怎样','check-column-label'));
        const g=groups.find(item=>item.group_id===view.groups.get(row.check_id)),r=row.groups?.find(item=>item.group_id===g?.group_id);
        if(g){resultPane.append(el('h4',g.title),statusLight(groupState(row,g)));if(r){resultText(resultPane,r);}else resultPane.append(el('p','这份报告没有单独检查这一组的结果，请查看标题区的整项检查说明；这一组是否通过仍待核对。','checklist-muted'));}
        else resultPane.append(el('p','这份报告只有整项检查结果，请查看标题区的说明。','checklist-muted'));
        for(const [id,d] of detailsById)d.setAttribute('data-current',String(id===view.groups.get(row.check_id)));
      }
      for(const g of visibleGroups){
        const d=el('details','','check-requirement-group'),summary=el('summary');d.open=view.open.has(g.group_id);d.setAttribute('data-group',g.group_id);detailsById.set(g.group_id,d);
        summary.append(el('span',g.title,'check-group-title'),statusLight(groupState(row,g)));summary.addEventListener('click',()=>{view.groups.set(row.check_id,g.group_id);result();});
        const body=el('div','','check-requirement-body');renderContent(body,g.requirement.replace(/^\s{0,3}#{1,6}\s+[^\n]+\n?/,''));d.append(summary,body);
        d.addEventListener('toggle',()=>{d.open?view.open.add(g.group_id):view.open.delete(g.group_id);updateFold();});original.append(d);
      }
      if(!groups.length)original.append(el('p','这份报告没有保存送审时的这项检查要求。','checklist-muted'));
      else if(!visibleGroups.length)original.append(el('p','各组未发现问题或已确认不适用，请查看标题区的整项检查说明。想核对各组要求，可以切换到“显示全部”。','checklist-muted'));
      for(const field of ['when','notes'])if(row.original?.[field]){const d=el('details','','check-requirement-group check-original-note');const id=row.check_id+':'+field;d.open=view.open.has(id);d.append(el('summary',labels[field]));const body=el('div','','check-requirement-body');renderContent(body,row.original[field]);d.append(body);d.addEventListener('toggle',()=>d.open?view.open.add(id):view.open.delete(id));original.append(d);}
      result();container.scrollTop=before;
    }
    draw();container.scrollTop=view.scroll;
  }
  document.getElementById('open-checklists')?.addEventListener('click',open);
  window.ChecklistsUI={open,mountPicker,renderChecks,renderResults,normalizeItem,normalizeList,resultRows,repairable,legacyContent,migrateItem,renderContent,serializeRich,plainText,insertPlain};
  if(document.getElementById('checklist-page-host'))open();
})();
