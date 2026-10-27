'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
class Element{
  constructor(tag,doc){this.tagName=tag.toUpperCase();this.doc=doc;this.children=[];this.parentNode=null;this.listeners={};this.attributes={};this.textContent='';this.className='';this.value='';this.open=false;this.disabled=false;this.checked=false;this.files=[];}
  append(...nodes){for(const n of nodes){n.remove?.();n.parentNode=this;this.children.push(n);}}
  prepend(...nodes){for(const n of nodes.reverse()){n.remove?.();n.parentNode=this;this.children.unshift(n);}}
  replaceChildren(...nodes){for(const child of this.children)child.parentNode=null;this.children=[];this.append(...nodes);}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(n=>n!==this);this.parentNode=null;}
  addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);}
  async fire(type){for(const fn of this.listeners[type]||[])await fn({preventDefault(){},target:this});}
  dispatchEvent(event){for(const fn of this.listeners[event.type]||[])fn({preventDefault(){},target:this});}
  setAttribute(key,value){this.attributes[key]=String(value);}
  focus(){this.doc.activeElement=this;}
  showModal(){this.open=true;}
  close(){this.open=false;this.dispatchEvent({type:'close'});}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  querySelectorAll(selector){return all(this).slice(1).filter(n=>selector.startsWith('.')?n.className.split(' ').includes(selector.slice(1)):n.tagName===selector.toUpperCase());}
  reportValidity(){return all(this).every(n=>n.disabled||!n.required||String(n.value||'').trim());}
  click(){return this.fire('click');}
}
function all(root){return [root,...root.children.flatMap(all)];}
const byText=(root,text)=>all(root).find(n=>n.tagName==='BUTTON'&&n.textContent===text)||all(root).find(n=>n.textContent===text),byName=(root,name)=>all(root).find(n=>n.name===name),text=root=>all(root).map(n=>n.textContent).join('\n');

Element.prototype.removeAttribute=function(key){delete this.attributes[key];};
const document={activeElement:null,elements:{},createElement(tag){return new Element(tag,this);},getElementById(id){return this.elements[id]||null;}};document.body=document.createElement('body');
for(const id of ['rereview','previous-round','review-contract-facts','review-round','rereview-notes'])document.elements[id]=document.createElement(id==='previous-round'?'a':'div');
const window={listeners:{},addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);},APIProfiles:{load:async()=>{},active:()=>({provider:'deepseek',config:{base_url:'https://api.deepseek.com',model:'flash',api_key:'local-key'}})},ModelSettings:{open(){}},ReviewJobsUI:{openJob:async id=>{openedJob=id;}}};
let modelUpdate,selectorMounts=0;const selectedModel={engine_key:'compatible_api',provider:'deepseek',config:{base_url:'https://api.deepseek.com',model:'flash',api_key:'local-key'}};
window.ReviewModelSelector={mount(host,engines,onChange){selectorMounts++;assert.ok(engines.some(e=>e.key==='codex_cli'));modelUpdate=onChange;const select=document.createElement('select');select.name='review_model';const group=document.createElement('optgroup');group.label='DeepSeek';const option=document.createElement('option');option.textContent=selectedModel.config.model;group.append(option);select.append(group);host.append(select);return {ready:Promise.resolve(),value:()=>selectedModel,refresh:async()=>onChange()};}};
let openedJob='',sequence=0,available=true,inputRevision='input-1',postError=0,factsRevision=3;
let factState={revision:factsRevision,facts:[{id:'fact-1',text:'尚未付款',active:true,source:{quote:'我还没付款'}},{id:'old',text:'已付款',active:false}]};
const calls=[];
async function fetch(url,options={}){
  const payload=options.body?JSON.parse(options.body):undefined;calls.push({url,payload});let status=200,value;
  if(url.endsWith('/facts/confirm')){
    if(payload.candidate_id==='conflict'){status=409;value={error:'changed'};factState.revision++;}
    else {factState={revision:++factsRevision,facts:factState.facts.map(f=>f.id===payload.replace_id?{...f,active:false}:f).concat({id:'fact-'+factsRevision,text:payload.candidate_id==='correction'?'已支付30%':'无需接口开发',active:true})};value={facts:factState};}
  }else if(url.endsWith('/facts/save')){
    if(postError===409){status=409;value={error:'changed'};factState.revision++;postError=0;}
    else {factState={...factState,revision:factState.revision+1,has_updates_since_review:true,facts:factState.facts.map(f=>f.id===payload.replace_id?{...f,active:false}:f).concat({id:'manual-'+factState.revision,text:payload.text,active:true,source:{quote:payload.text}}),questions:(factState.questions||[]).map(q=>q.fact_id===payload.replace_id?{...q,answer:payload.text,fact_id:'manual-'+factState.revision}:q)};value={facts:factState};}
  }else if(url.endsWith('/facts/withdraw')){factState={...factState,revision:factState.revision+1,has_updates_since_review:true,facts:factState.facts.map(f=>f.id===payload.fact_id?{...f,active:false}:f),questions:(factState.questions||[]).map(q=>q.fact_id===payload.fact_id?{...q,status:'unknown',answer:'',fact_id:null}:q)};value={facts:factState};
  }else if(url.endsWith('/facts/answer')){const answeredId='answer-'+factState.revision;const previous= factState.questions.find(q=>q.id===payload.question_id)?.fact_id;factState={...factState,revision:factState.revision+1,has_updates_since_review:true,facts:factState.facts.map(f=>f.id===previous?{...f,active:false}:f).concat(payload.status==='answered'?[{id:answeredId,text:payload.answer,active:true}]:[]),questions:factState.questions.map(q=>q.id===payload.question_id?{...q,answer:payload.answer,status:payload.status,fact_id:payload.status==='answered'?answeredId:null}:q)};value={facts:factState};
  }else if(url.endsWith('/facts'))value={facts:factState};
  else if(url==='/api/review-capabilities')value={engines:[{key:'compatible_api',label:'兼容API',available:true},{key:'codex_cli',label:'Codex',available:true}]};
  else if(url.endsWith('/rereview')){
    if(!payload)value={available,reason:'没有原始DOCX',input_revision:inputRevision,document_name:'改后合同.docx'};
    else if(postError){status=postError;value={error:'failed'};if(postError===409)inputRevision='input-2';postError=0;}
    else value={job:{job_id:'job-'+ 'a'.repeat(32)},started:true};
  }else throw Error('unexpected '+url);
  return {ok:status===200,status,json:async()=>JSON.parse(JSON.stringify(value))};
}
const context={window,document,fetch,URL,console,crypto:{randomUUID:()=>String(++sequence)}};vm.createContext(context);
for(const file of ['readable_text.js','contract_facts.js','rereview.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/'+file),'utf8'),context);
const factsUI=window.ContractFactsUI,rereviewUI=window.RereviewUI;
const bundle={id:'report-1',report:{metadata:{parent_report_id:'report-parent',rereview:{round_number:2}},limitations:['已解决：付款周期已明确。','仍需处理：交付期限缺失。'],context:{business_goal:'不可自动续约'}}};
const dialogs=()=>all(document.body).filter(n=>n.tagName==='DIALOG'&&n.open),tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await factsUI.load(bundle);assert.equal(factsUI.activeFacts().length,1);assert.equal(text(document.elements['review-contract-facts']).includes('已付款'),false);
  const host=document.createElement('div'),turn={id:'turn-1',fact_candidates:[{id:'normal',text:'无需接口开发',source_quote:'项目不需要接口'}]},active={reportId:bundle.id,findingId:'F1',sessionId:'session-1'};
  factsUI.renderCandidates(host,turn,active);assert.ok(text(host).includes('原话：项目不需要接口'));assert.equal(host.querySelectorAll('option').length,2);await byText(host,'确认保存信息').fire('click');
  let payload=calls.filter(c=>c.url.endsWith('/facts/confirm')).at(-1).payload;assert.deepEqual(payload,{expected_revision:3,finding_id:'F1',session_id:'session-1',turn_id:'turn-1',candidate_id:'normal'});assert.equal(byText(host,'已保存到本合同信息').disabled,true);
  const correction=document.createElement('div');factsUI.renderCandidates(correction,{id:'turn-2',fact_candidates:[{id:'correction',kind:'correction',text:'已支付30%',source_quote:'刚才说错了，付了30%'}]},active);assert.equal(byText(correction,'确认保存信息').disabled,true);const select=correction.querySelector('select');select.value='fact-1';await select.fire('change');await byText(correction,'确认保存信息').fire('click');
  payload=calls.filter(c=>c.url.endsWith('/facts/confirm')).at(-1).payload;assert.equal(payload.replace_id,'fact-1');assert.equal(factsUI.activeFacts().some(f=>f.id==='fact-1'),false);assert.equal(factState.facts.find(f=>f.id==='fact-1').active,false);
  const conflict=document.createElement('div');factsUI.renderCandidates(conflict,{id:'turn-3',fact_candidates:[{id:'conflict',text:'测试并发',source_quote:'一句原话'}]},active);await byText(conflict,'确认保存信息').fire('click');assert.ok(text(conflict).includes('没有覆盖'));assert.equal(byText(conflict,'确认保存信息').disabled,false);
  const noCandidates=document.createElement('div');factsUI.renderCandidates(noCandidates,{id:'turn-4'},active);assert.equal(noCandidates.children.length,0);
  // Manual facts and pending questions share the same revisioned record; a conflict keeps the draft.
  factState={...factState,questions:[{id:'q1',question:'用途尚未明确？',reason:'核对实际场景',status:'unknown',finding_id:'F1',source:{quote:'合同中未写具体用途'}}]};await factsUI.load(bundle);
  const pane=document.elements['review-contract-facts'];assert.ok(text(pane).includes('已提供的信息'));assert.ok(text(pane).includes('待补充的问题'));assert.ok(text(pane).includes('暂不能提供'));assert.ok(text(pane).includes('合同中未写具体用途'));
  let discussed='';factsUI.configure({onDiscuss:id=>{discussed=id;}});await byText(pane,'讨论').fire('click');assert.equal(discussed,'F1');
  await byText(pane,'＋ 新增信息').fire('click');let editor=pane.querySelector('.fact-editor');editor.querySelector('textarea').value='项目尚未开工';postError=409;await editor.fire('submit');assert.equal(editor.querySelector('textarea').value,'项目尚未开工');assert.ok(text(editor).includes('没有覆盖'));await editor.fire('submit');assert.ok(text(pane).includes('项目尚未开工'));assert.ok(text(pane).includes('信息已保存，后续讨论会用到；还没有按这些信息复审全文。'));
  const manual=all(pane).find(n=>n.className==='contract-fact'&&text(n).includes('项目尚未开工'));await byText(manual,'编辑更正').fire('click');editor=manual.querySelector('.fact-editor');editor.querySelector('textarea').value='项目已开工一周';await editor.fire('submit');assert.equal(factsUI.activeFacts().some(f=>f.text==='项目尚未开工'),false);assert.equal(factsUI.activeFacts().some(f=>f.text==='项目已开工一周'),true);
  const corrected=all(pane).find(n=>n.className==='contract-fact'&&text(n).includes('项目已开工一周'));await byText(corrected,'撤回').fire('click');assert.equal(factsUI.activeFacts().some(f=>f.text==='项目已开工一周'),false);
  let question=pane.querySelector('.fact-answer-form');const unknown=question.querySelector('input');assert.equal(unknown.checked,false);question.querySelector('textarea').value='内部员工使用';await question.fire('submit');assert.equal(factState.questions[0].status,'answered');assert.equal(factState.questions[0].answer,'内部员工使用');assert.ok(text(pane).includes('内部员工使用'));assert.equal(pane.querySelectorAll('.fact-answer-form').length,0);assert.equal(pane.querySelectorAll('.contract-question').length,0);assert.ok(text(pane).includes('当前没有待补充的问题。'));const providedAnswer=all(pane).find(n=>n.className==='contract-fact'&&text(n).includes('内部员工使用'));assert.ok(byText(providedAnswer,'编辑更正'));assert.ok(byText(providedAnswer,'讨论'));
  await byText(providedAnswer,'编辑更正').fire('click');editor=providedAnswer.querySelector('.fact-editor');editor.querySelector('textarea').value='内部员工隔离使用';await editor.fire('submit');assert.equal(factState.questions[0].answer,'内部员工隔离使用');assert.equal(pane.querySelectorAll('.fact-answer-form').length,0);const updatedAnswer=all(pane).find(n=>n.className==='contract-fact'&&text(n).includes('内部员工隔离使用'));
  await byText(updatedAnswer,'撤回').fire('click');assert.equal(factState.questions[0].status,'unknown');assert.equal(factState.questions[0].answer,'');question=pane.querySelector('.fact-answer-form');assert.ok(question);assert.equal(question.querySelector('input').checked,false);question.querySelector('input').checked=true;await question.fire('submit');assert.equal(factState.questions[0].status,'unknown');
  rereviewUI.load(bundle);assert.equal(document.elements['review-round'].textContent,'第2轮复审');assert.ok(text(document.elements['rereview-notes']).includes('本轮复审说明'));assert.ok(text(document.elements['rereview-notes']).includes('仍需处理'));assert.equal(document.elements['rereview-notes'].querySelector('details').open,false);assert.equal(document.elements['previous-round'].href,'/?report=report-parent');assert.equal(document.elements['previous-round'].hidden,false);
  rereviewUI.load({id:'report-ids',paragraphs:[{id:'P017',location:'1. 开发软件 / 1.3.',text:'开发软件上线运行所需的硬件'}],report:{metadata:{parent_report_id:'report-parent',rereview:{round_number:2}},findings:[{id:'F09'},{id:'F02'}],limitations:['P017已落实；仍需处理验收（F02）。']}});const notesText=text(document.elements['rereview-notes']);assert.ok(!/P017|F02/.test(notesText),notesText);assert.ok(notesText.includes('意见2'),notesText);
  rereviewUI.load(bundle);
  available=false;await rereviewUI.open();assert.ok(text(dialogs().at(-1)).includes('没有原始DOCX'));await byText(dialogs().at(-1),'关闭').fire('click');
  available=true;await rereviewUI.open();let box=dialogs().at(-1),form=box.querySelector('form');assert.ok(text(box).includes('原报告和修改记录保留'));assert.equal(text(box).includes('换清单'),false);assert.equal(text(box).includes('不可自动续约'),true);
  assert.ok(text(box).includes('讨论和修改完成后，可以让 AI 再检查一遍全文。按这次清单和审查要求，结合确认保存的信息检查改后合同。'));
  assert.ok(text(box).includes('讨论中要用于复审的信息，请先保存到“审查信息”。'));assert.ok(text(box).includes('选择过“不采用修改建议”的问题也会重新检查。'));
  assert.equal(selectorMounts,1);assert.equal(byName(form,'engine_key'),undefined);assert.ok(byName(form,'review_model'));assert.ok(text(form).includes('合同文字将发送到下面的模型服务'));assert.equal(text(form).includes('https://api.deepseek.com'),false);assert.equal(text(form).includes('全文复审方式'),false);
  let checkbox=all(form).filter(n=>n.type==='checkbox').at(-1);await form.fire('submit');assert.equal(calls.filter(c=>c.url.endsWith('/rereview')&&c.payload).length,0);
  checkbox.checked=true;modelUpdate();assert.equal(checkbox.checked,true);selectedModel.config.model='raw-rereview-model';modelUpdate();assert.equal(checkbox.checked,false);checkbox.checked=true;selectedModel.config.api_key='changed-local-key';modelUpdate();assert.equal(checkbox.checked,false);
  checkbox.checked=true;postError=409;await form.fire('submit');assert.equal(checkbox.checked,false);assert.ok(text(box).includes('复审材料已更新'));assert.equal(openedJob,'');
  checkbox.checked=true;postError=500;await form.fire('submit');let failed=calls.filter(c=>c.url.endsWith('/rereview')&&c.payload).at(-1).payload;assert.equal(failed.input_revision,'input-2');assert.equal(checkbox.checked,true);
  checkbox.checked=true;const one=form.fire('submit'),two=form.fire('submit');await Promise.all([one,two]);let sent=calls.filter(c=>c.url.endsWith('/rereview')&&c.payload).at(-1).payload;assert.equal(sent.request_id,failed.request_id);assert.equal(sent.engine_key,'compatible_api');assert.equal(sent.config.model,'raw-rereview-model');assert.equal(sent.config.api_key,'changed-local-key');assert.equal(sent.consent,true);assert.equal(sent.web_search,true);assert.equal(openedJob,'job-'+ 'a'.repeat(32));assert.equal(dialogs().length,0);assert.equal(calls.filter(c=>c.url.endsWith('/rereview')&&c.payload).length,3);
  rereviewUI.load({id:'no-parent',report:{}});assert.equal(document.elements['previous-round'].hidden,true);assert.equal(document.elements['review-round'].textContent,'第1轮初审');assert.equal(document.elements['rereview-notes'].hidden,true);
  console.log('reconstruction_ui: active facts, ID-only confirmation/correction/conflict, explicit rereview consent, revision refresh, idempotent retry, pending-job handoff, parent navigation passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
