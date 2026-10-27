'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
class Element{
  constructor(tag,doc){this.tagName=tag.toUpperCase();this.doc=doc;this.children=[];this.parentNode=null;this.listeners={};this.attributes={};this.textContent='';this.className='';this.value='';this.open=false;this.disabled=false;this.checked=false;this.files=[];}
  append(...nodes){for(const n of nodes){n.remove?.();n.parentNode=this;this.children.push(n);}}
  prepend(...nodes){for(const n of nodes.reverse()){n.remove?.();n.parentNode=this;this.children.unshift(n);}}
  replaceChildren(...nodes){for(const child of this.children)child.parentNode=null;this.children=[];this.append(...nodes);}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(n=>n!==this);this.parentNode=null;}
  addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);}
  async fire(type,extra={}){let prevented=false;for(const fn of this.listeners[type]||[])await fn({preventDefault(){prevented=true;},stopPropagation(){},target:this,...extra});return prevented;}
  dispatchEvent(event){for(const fn of this.listeners[event.type]||[])fn({preventDefault(){},target:this});}
  setAttribute(key,value){this.attributes[key]=String(value);}
  getAttribute(key){return this.attributes[key]??null;}
  contains(node){return all(this).includes(node);}
  focus(){this.doc.activeElement=this;}
  showModal(){this.open=true;}
  close(){this.open=false;this.dispatchEvent({type:'close'});}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  querySelectorAll(selector){const selectors=selector.split(',');return all(this).slice(1).filter(n=>selectors.some(value=>value.startsWith('.')?n.className.split(' ').includes(value.slice(1)):n.tagName===value.toUpperCase()));}
  reportValidity(){return all(this).every(n=>n.disabled||!n.required||String(n.value||'').trim());}
  click(){return this.fire('click');}
}
function all(root){return [root,...root.children.flatMap(all)];}
const byText=(root,text)=>all(root).find(n=>n.tagName==='BUTTON'&&n.textContent===text)||all(root).find(n=>n.textContent===text),byName=(root,name)=>all(root).find(n=>n.name===name),text=root=>all(root).map(n=>n.textContent).join('\n');
let pageHost=null;
const document={activeElement:null,createElement(tag){return new Element(tag,this);},createTextNode(text){const n=new Element('text',this);n.textContent=text;n.nodeType=3;return n;},getElementById(id){return id==='checklist-page-host'?pageHost:null;}};document.body=document.createElement('body');
const builtin={scope:'适用于服务合同全文。',id:'builtin-1',name:'客户清单',contract_type:'服务合同',stance:'客户',version:'0.2',builtin:true,items:[{id:'c1',title:'结算',requirement:'核对解除后的结算',when:'解除合同时',facts:'履行到哪一步',guidance:'已交付：按实际成果结算；未交付：确认已发生成本',notes:'',enabled:true}]};
let catalog={revision:0,checklists:[builtin]},importData={ok:true,rows:[],errors:[],warnings:[]},conflict=false;
const calls=[];
let navigated='';const window={dispatchEvent(){},addEventListener(){},location:{assign(url){navigated=url;}}};
async function fetch(url,options={}){
  const payload=options.body?JSON.parse(options.body):null;calls.push({url,payload});let status=200,value;
  if(url==='/api/checklists')value=catalog;
  else if(url==='/api/checklists/save'){
    if(conflict||payload.expected_revision!==catalog.revision){status=409;value={error:'conflict'};}
    else {const checklist={...payload.checklist,id:payload.checklist.id||'custom-'+(catalog.revision+1),version:catalog.revision+1};catalog={revision:catalog.revision+1,checklists:catalog.checklists.filter(item=>item.id!==checklist.id).concat(checklist)};value={revision:catalog.revision,checklist};}
  }else if(url==='/api/checklists/delete'){const deleted=payload.id;catalog={revision:catalog.revision+1,checklists:catalog.checklists.filter(c=>c.id!==deleted)};value={revision:catalog.revision,deleted_id:deleted};}
  else if(url==='/api/checklists/import')value=importData;
  else throw new Error('unexpected '+url);
  return {ok:status===200,status,json:async()=>JSON.parse(JSON.stringify(value))};
}
class FileReader{readAsDataURL(){this.result='data:application/octet-stream;base64,ZmFrZQ==';queueMicrotask(()=>this.onload());}}
const context={window,document,fetch,FileReader,Event,console,queueMicrotask,setTimeout,clearTimeout};vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/checklists.js'),'utf8'),context);
const ui=window.ChecklistsUI;
const dialogs=()=>all(document.body).filter(n=>n.tagName==='DIALOG'&&n.open),lastDialog=()=>dialogs().at(-1),tick=()=>new Promise(resolve=>setImmediate(resolve));
const watchdog=setTimeout(()=>{console.error('checklists_ui did not complete');process.exitCode=1;},10000);
(async()=>{
  const normalized=ui.normalizeList({...builtin,source_pages:'A PDF445',items:[{...builtin.items[0],general:'hidden',sources:'PDF445'}]});
  assert.equal(normalized.scope,builtin.scope);assert.equal(Object.hasOwn(ui.normalizeList({id:'legacy'}),'scope'),false);assert.equal(normalized.version,'0.2');assert.equal(Object.hasOwn(normalized,'source_pages'),false);assert.equal(Object.hasOwn(normalized.items[0],'sources'),false);assert.equal(Object.hasOwn(normalized.items[0],'general'),false);
  assert.equal(ui.legacyContent(builtin.items[0]),'核对解除后的结算\n\n### 原可能需要的信息\n\n履行到哪一步\n\n### 原处理建议\n\n已交付：按实际成果结算；未交付：确认已发生成本');
  assert.throws(()=>ui.migrateItem({title:'长文',requirement:'a'.repeat(8000),facts:'b'}),/8000/);
  const markdownHost=document.createElement('div');ui.renderContent(markdownHost,'### 条件\n\n- 第一条\n- 第二条\n\n完整说明。\n<script>bad()</script>');assert.equal(markdownHost.querySelectorAll('h4').length,1);assert.equal(markdownHost.querySelectorAll('li').length,2);assert.equal(markdownHost.querySelectorAll('script').length,0);assert.ok(text(markdownHost).includes('<script>bad()</script>'));
  const structures=document.createElement('div');ui.renderContent(structures,'## 保留二级标题\n\n- 第一项\n  - 第二层\n\n完整段落');assert.equal(ui.serializeRich(structures.children[0]),'## 保留二级标题\n\n- 第一项\n  - 第二层\n\n完整段落');structures.querySelector('h4').textContent='更改标题';assert.ok(ui.serializeRich(structures.children[0]).includes('## 更改标题'));const unsafe=document.createElement('script');unsafe.textContent='bad()';structures.children[0].append(unsafe);assert.equal(ui.serializeRich(structures.children[0]).includes('bad()'),false);
  assert.equal(ui.repairable({field:'requirement',code:'required'}),true);assert.equal(ui.repairable({field:'requirement',code:'formula'}),false);
  const host=document.createElement('div');document.body.append(host);let session=document.createElement('section');const picker=ui.mountPicker(host,{openStep:render=>{if(render)render(session);}});await picker.ready;
  assert.equal(picker.snapshot().id,'builtin-1');assert.equal(all(host).some(n=>n.tagName==='OPTION'&&n.value===''),false);byName(host,'checklist_id').value='builtin-1';await byName(host,'checklist_id').fire('change');const snap=picker.snapshot();assert.equal(snap.version,'0.2');snap.items[0].title='cannot mutate';assert.equal(picker.snapshot().items[0].title,'结算');
  await byText(host,'查看与调整检查项').fire('click');assert.equal(dialogs().length,0);assert.ok(text(session).includes('只用于这份合同'));assert.equal(session.children[2].className,'checklist-actions checklist-step-actions');assert.ok(byText(session.children[2],'保存并返回'));assert.equal(session.children[3].querySelectorAll('.checklist-table').length,1);
  await byText(session,'编辑').fire('click');let itemDialog=session.querySelector('.is-editing');
  assert.ok(itemDialog);assert.equal(session.querySelector('.checklist-edit-row'),null);assert.equal(session.querySelector('.checklist-inline-editor'),null);
  assert.ok(byName(itemDialog,'title'));assert.ok(byName(itemDialog,'requirement'));assert.ok(text(byName(itemDialog,'requirement')).includes('原可能需要的信息'));assert.equal(text(byName(itemDialog,'requirement')).includes('###'),false);assert.equal(byName(itemDialog,'facts'),undefined);assert.equal(byName(itemDialog,'guidance'),undefined);
  assert.equal(byText(session,'保存并返回').disabled,true);byName(itemDialog,'title').textContent='本次结算';await byText(itemDialog,'保存').fire('click');assert.equal(session.querySelector('.is-editing'),null);
  await byText(session,'保存并返回').fire('click');assert.equal(picker.snapshot().items[0].title,'本次结算');assert.equal(builtin.items[0].title,'结算');assert.equal(calls.some(c=>c.url.endsWith('/save')),false);

  await ui.open();assert.equal(navigated,'/checklists.html');
  pageHost=document.createElement('main');document.body.append(pageHost);
  await ui.open();let library=pageHost;assert.ok(text(library).includes('内置清单只读'));assert.equal(dialogs().length,0);assert.equal(library.querySelector('h1').textContent,'客户清单');assert.equal(byName(library,'name'),undefined);assert.ok(library.querySelector('table'));
  assert.equal(library.querySelector('.checklist-edit-row'),null);assert.ok(text(library).includes('适用情形'));assert.ok(text(library).includes('解除合同时'));
  await byText(library,'复制为我的清单').fire('click');assert.equal(byName(library,'scope').value,builtin.scope);assert.equal(byName(library,'stance').tagName,'TEXTAREA');byName(library,'scope').value='我自己的适用范围';await byName(library,'scope').fire('input');byName(library,'name').value='我的结算清单';await byName(library,'name').fire('input');
  await byText(library,'编辑').fire('click');itemDialog=library.querySelector('.is-editing');assert.ok(itemDialog);assert.equal(itemDialog.parentNode.tagName,'TBODY');assert.equal(itemDialog.querySelector('form'),null);assert.equal(itemDialog.querySelector('textarea'),null);
  assert.equal(byText(library,'保存清单').disabled,true);assert.equal(byText(library,'重新读取清单库').disabled,true);assert.equal(byText(library,'复制为我的清单').disabled,true);assert.equal(byName(library,'name').disabled,true);
  byName(itemDialog,'title').textContent='修改后结算';conflict=true;await byText(itemDialog,'保存').fire('click');assert.ok(text(itemDialog).includes('这次没有覆盖'));assert.equal(byName(itemDialog,'title').textContent,'修改后结算');assert.equal(byName(itemDialog,'requirement').contentEditable,'true');assert.equal(byText(library,'保存清单').disabled,true);
  conflict=false;await byText(itemDialog,'保存').fire('click');assert.equal(library.querySelector('.is-editing'),null);const firstSave=calls.filter(c=>c.url.endsWith('/save')).at(-1).payload;assert.equal(firstSave.checklist.scope,'我自己的适用范围');assert.equal(firstSave.checklist.id,'');assert.equal(firstSave.expected_revision,0);assert.equal(catalog.revision,1);assert.equal(catalog.checklists[1].items[0].title,'修改后结算');assert.equal(catalog.checklists[1].items[0].facts,'');assert.equal(catalog.checklists[1].items[0].guidance,'');assert.ok(catalog.checklists[1].items[0].requirement.includes('履行到哪一步'));assert.equal(byText(library,'保存清单').disabled,false);
  await byText(library,'编辑').fire('click');itemDialog=library.querySelector('.is-editing');byName(itemDialog,'title').textContent='取消不应保存';const requirementEditor=byName(itemDialog,'requirement');const prevented=await requirementEditor.fire('paste',{clipboardData:{getData:type=>type==='text/plain'?'<img src=x onerror=bad()>':''}});assert.equal(prevented,true);assert.equal(requirementEditor.querySelector('img'),null);assert.ok(ui.serializeRich(requirementEditor).includes('<img src=x onerror=bad()>'));const beforeCancel=calls.filter(c=>c.url.endsWith('/save')).length;await byText(itemDialog,'取消').fire('click');assert.ok(text(library).includes('修改后结算'));assert.equal(calls.filter(c=>c.url.endsWith('/save')).length,beforeCancel);
  await byText(library,'＋ 新增检查项').fire('click');itemDialog=library.querySelector('.is-editing');assert.equal(dialogs().length,0);byName(itemDialog,'title').textContent='验收';byName(itemDialog,'requirement').replaceChildren(document.createElement('p'));byName(itemDialog,'requirement').children[0].textContent='核对验收标准';await byText(itemDialog,'保存').fire('click');assert.equal(catalog.revision,2);
  const down=all(library).find(n=>n.attributes['aria-label']==='修改后结算下移');await down.fire('click');const toggle=all(library).find(n=>n.attributes['aria-label']==='验收：启用');toggle.checked=false;await toggle.fire('change');
  await byText(library,'保存清单').fire('click');const saved=calls.filter(c=>c.url.endsWith('/save')).at(-1).payload;assert.equal(saved.expected_revision,2);assert.equal(saved.checklist.id,'custom-1');assert.equal(saved.checklist.builtin,false);assert.equal(saved.checklist.items[0].title,'验收');assert.equal(saved.checklist.items[0].enabled,false);assert.equal(saved.checklist.items[1].title,'修改后结算');assert.equal(saved.checklist.items[1].facts,'');assert.equal(saved.checklist.items[1].guidance,'');assert.ok(saved.checklist.items[1].requirement.includes('履行到哪一步'));
  assert.ok(text(library).includes('已保存到本机清单库'));
  byName(library,'name').value='发生冲突的草稿';await byName(library,'name').fire('input');conflict=true;await byText(library,'保存清单').fire('click');assert.ok(text(library).includes('本次没有覆盖'));assert.equal(byName(library,'name').value,'发生冲突的草稿');
  conflict=false;await byText(library,'另存为新清单').fire('click');assert.equal(catalog.revision,4);assert.ok(catalog.checklists.find(c=>c.name==='发生冲突的草稿（副本）'));
  const deleting=byText(library,'删除清单').fire('click');await tick();const confirm=lastDialog();assert.ok(text(confirm).includes('已经送审的合同仍保留'));await byText(confirm,'删除清单').fire('click');await deleting;assert.equal(calls.filter(c=>c.url.endsWith('/delete')).at(-1).payload.expected_revision,4);

  // Missing required cells are repaired in the preview; old unmapped values are visible and acknowledged.
  importData={ok:true,rows:[{row_number:2,title:'',requirement:'核对付款',notes:'客户自有经验'}],errors:[{row_number:2,field:'title',code:'required',message:'必填内容为空。'}],warnings:[{row_number:2,field:'通用目的',code:'unmapped_content',message:'未自动导入，请确认残余内容。',value:'不能默默丢掉的原文字'}]};
  let upload=all(library).find(n=>n.type==='file');upload.files=[{name:'rules.xlsx',size:100}];await upload.fire('change');await tick();let preview=lastDialog();assert.ok(text(preview).includes('不能默默丢掉的原文字'));assert.ok(text(preview).includes('Excel 第 2 行'));
  assert.equal(byText(preview,'确认保存为新清单').disabled,true);byName(preview,'title').value='付款';await byName(preview,'title').fire('input');const ack=all(preview).find(n=>n.type==='checkbox');ack.checked=true;await ack.fire('change');assert.equal(byText(preview,'确认保存为新清单').disabled,false);await preview.querySelector('form').fire('submit');await tick();
  const imported=calls.filter(c=>c.url.endsWith('/save')).at(-1).payload.checklist;assert.equal(imported.items[0].title,'付款');assert.equal(imported.items[0].notes,'客户自有经验');assert.equal(Object.hasOwn(imported.items[0],'row_number'),false);assert.equal(calls.find(c=>c.url.endsWith('/import')).payload.xlsx_base64,'ZmFrZQ==');
  importData={ok:true,rows:[{row_number:4,title:'付款',requirement:''}],errors:[{row_number:4,field:'requirement',code:'formula',message:'公式不能作为审核要求。'}],warnings:[]};upload=all(library).find(n=>n.type==='file');upload.files=[{name:'formula.xlsx',size:100}];await upload.fire('change');await tick();preview=lastDialog();assert.ok(text(preview).includes('公式不能作为审核要求'));byName(preview,'requirement').value='现在填了也不可忽略原公式错误';await byName(preview,'requirement').fire('input');assert.equal(byText(preview,'确认保存为新清单').disabled,true);await byText(preview,'取消').fire('click');

  const resultHost=document.createElement('div');const report={preparation:{checks:[{id:'c1',title:'付款',requirement:'核对付款',enabled:true},{id:'c2',title:'验收',requirement:'核对验收',enabled:true},{id:'c3',title:'停用',enabled:false}]},findings:[{id:'f1',title:'期限不明'}],checklist_results:[{check_id:'c1',title:'付款',status:'未检出',reason:'已核对付款安排',finding_ids:['f1']}]};let selected='';ui.renderResults(resultHost,report,id=>{selected=id;});assert.ok(text(resultHost).includes('尚无分组结果'));assert.ok(text(resultHost).includes('这一组是否通过仍待核对'));assert.equal(ui.resultRows(report).length,2);await byText(resultHost,'查看意见：期限不明').fire('click');assert.equal(selected,'f1');assert.equal(resultHost.querySelector('details').open,false);const missingCheck=all(resultHost).find(n=>n.className==='check-results-item'&&text(n).includes('验收'));await missingCheck.fire('click');assert.ok(text(resultHost).includes('本次报告缺少这项检查的回应'));assert.ok(text(resultHost).includes('尚未完成检查'));
  const requestReport={preparation:{checks:[{id:'REQ-1',title:'不自动续约',source:'request',requirement:'不得自动续约'}]},checklist_results:[{check_id:'REQ-1',title:'不自动续约',status:'未检出',reason:'已排查'}]};ui.renderResults(resultHost,requestReport);assert.ok(text(resultHost).includes('本次审查要求的检查结果'));assert.equal(text(resultHost).includes('清单检查回应'),false);
  ui.renderResults(resultHost,{findings:[]});assert.ok(text(resultHost).includes('历史报告没有保存逐项清单结果')); // Missing legacy results stay explicit, not green.
  // A row conflict can recover without throwing away new keystrokes or overwriting the external version.
  await byText(library,'编辑').fire('click');itemDialog=library.querySelector('.is-editing');byName(itemDialog,'title').textContent='冲突时的输入';conflict=true;await byText(itemDialog,'保存').fire('click');assert.ok(byText(itemDialog,'另存为新清单'));assert.equal(byText(library,'重新读取清单库').disabled,true);
  const sourceList=catalog.checklists.find(item=>!item.builtin&&item.name===library.querySelector('h1').textContent),sourceId=sourceList.id;sourceList.items[0].title='外部更新原清单';catalog.revision++;const externalVersion=JSON.stringify(sourceList),externalRevision=catalog.revision;
  byName(itemDialog,'title').textContent='冲突后继续改的输入';byName(itemDialog,'notes').textContent='恢复时最新备注';conflict=false;await byText(itemDialog,'另存为新清单').fire('click');assert.equal(library.querySelector('.is-editing'),null);const recoveryPayload=calls.filter(call=>call.url.endsWith('/save')).at(-1).payload;assert.equal(recoveryPayload.checklist.id,'');assert.equal(recoveryPayload.expected_revision,externalRevision);assert.equal(recoveryPayload.checklist.items[0].title,'冲突后继续改的输入');assert.equal(recoveryPayload.checklist.items[0].notes,'恢复时最新备注');assert.equal(JSON.stringify(catalog.checklists.find(item=>item.id===sourceId)),externalVersion);assert.ok(catalog.checklists.some(item=>item.id!==sourceId&&!item.builtin&&item.items[0].title==='冲突后继续改的输入'));assert.equal(byText(library,'保存清单').disabled,false);
  console.log('checklists_ui: same-row rich editing, real personal row persistence/new copy creation, local snapshot isolation, locks/cancel/conflict preservation, heading/list roundtrip, plain-text paste and whitelist, import/results regression passed');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>clearTimeout(watchdog));
