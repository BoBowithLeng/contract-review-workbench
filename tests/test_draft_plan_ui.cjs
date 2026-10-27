'use strict';
// Synthetic local responses only: no model, source document, or runtime data.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
class Element{
  constructor(tag,doc){this.tagName=tag.toUpperCase();this.doc=doc;this.children=[];this.listeners={};this.attributes={};this.textContent='';this.className='';this.value='';this.checked=false;this.hidden=false;this.disabled=false;this.open=false;this.dataset={};this.style={};this.classList={toggle(){},add(){},remove(){}};}
  append(...nodes){for(const n of nodes){n.parentNode=this;this.children.push(n);}}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  add(n){this.append(n);}
  addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);}
  removeEventListener(type,fn){this.listeners[type]=(this.listeners[type]||[]).filter(f=>f!==fn);}
  async fire(type){for(const fn of [...this.listeners[type]||[]])await fn({preventDefault(){},target:this});}
  setAttribute(k,v){this.attributes[k]=v;}
  focus(){this.doc.activeElement=this;}
  scrollIntoView(){}
  showModal(){this.open=true;}
  close(){this.open=false;this.fire('close');}
  querySelector(s){return this.querySelectorAll(s)[0]||null;}
  querySelectorAll(s){return all(this).slice(1).filter(n=>s.startsWith('.')?n.className.split(' ').includes(s.slice(1)):n.tagName===s.toUpperCase());}
}
const all=n=>[n,...n.children.flatMap(all)],text=n=>all(n).map(x=>x.textContent).join('\n'),byText=(n,t)=>all(n).find(x=>x.textContent===t);
const document={elements:{},createElement(tag){return new Element(tag,this);},createTextNode(text){const n=this.createElement('text');n.textContent=text;return n;},getElementById(id){return this.elements[id]||Object.values(this.elements).flatMap(all).find(n=>n.id===id)||null;},querySelectorAll(){return[];},querySelector(){return null;}};
document.body=document.createElement('body');
for(const match of fs.readFileSync(path.join(__dirname,'../reader_ui/index.html'),'utf8').matchAll(/id="([^"]+)"/g))document.elements[match[1]]=document.createElement('div');
const paragraphs=[{id:'P001',text:'1. 应在7日内验收。',location:'第一条 / 1.1'},{id:'P002',text:'2. 交付账号。',location:'第二条 / 2.1'}];
const target=p=>({piece_id:p.id,paragraph_sha256:p.id+'sha',start:0,end:Array.from(p.text).length,old_text:p.text,location:p.location});
const plan={status:'ready',edits:[{piece_id:'P001',new_text:'1. 应在10日内验收。'}],insertions:[{after_piece_id:'P002',new_text:'3. 通知采用书面形式。'}],paragraph_actions:[{piece_id:'P001',action:'modify',reason:'调整期限，与P002的交付衔接'},{piece_id:'P002',action:'retain',reason:'账号约定明确，无需联动修改'}],questions:[],comment:'供协作者参考，与P001和F03衔接'};
const item={id:'F01',title:'验收期限',risk:'中',reason:'期限需要调整',status:'发现风险',suggestion:'调整期限并新增通知约定',refs:paragraphs.map(p=>({id:p.id,quote:p.text})),draft_plan:plan,clause_draft:[...plan.edits,...plan.insertions].map(e=>e.new_text).join('\n')};
const fixture={id:'ui-test',paragraphs,report:{context:{contract_type:'general'},findings:[item],options:[]},state:{revision:0,reviews:{},selected_options:[]},sources:[],completeness:[]};
const delivery={available:true,source:{source_sha256:'source',pieces:paragraphs.map(p=>({...p,paragraph_sha256:p.id+'sha'}))},state:{revision:0,edits:[],comments:[]},targets:{F01:paragraphs.map(target)}};
const digest=t=>require('node:crypto').createHash('sha256').update(t).digest('hex');
const candidate={status:'ready',candidate_id:'plan-candidate',coverage_complete:true,based_on_revision:0,source_sha256:'source',draft_sha256:digest(item.clause_draft),edits:[{...target(paragraphs[0]),new_text:plan.edits[0].new_text}],insertions:[{...target(paragraphs[1]),kind:'insert_paragraph_after',start:paragraphs[1].text.length,end:paragraphs[1].text.length,old_text:'',new_text:plan.insertions[0].new_text}],paragraph_actions:plan.paragraph_actions,comment:plan.comment,suggestion_actions:[{unit_id:'S1',text:plan.edits[0].new_text,action:'implemented',reason:'初审已定位到对应合同段落的结构化改稿。'}]};
let getFailure=null,profile={base_url:'https://selected.test/v1',api_key:'fixture-key',model:'selected-model'},changeConfigOnReload=false,reloads=0;
const calls=[],no=()=>{},window={addEventListener:no,ContractFactsUI:{questionsForFinding:()=>[]}};
window.APIProfiles={async load(options){if(options?.reload){reloads++;if(changeConfigOnReload){profile={...profile,api_key:'new-fixture-key'};changeConfigOnReload=false;}}return[];}};
window.ReviewModelSelector={mount(host,engines){assert.deepEqual(JSON.parse(JSON.stringify(engines)),[{key:'compatible_api',available:true}]);return{ready:Promise.resolve(),value:()=>({provider:'所选服务',config:{...profile}})};}};
const context={window,document,console,TextEncoder,crypto:require('node:crypto').webcrypto,APIProfiles:window.APIProfiles,Option:function(t,v){const o=document.createElement('option');o.textContent=t;o.value=v;return o;},setTimeout:()=>1,clearTimeout:no,URL,URLSearchParams,location:new URL('http://fixture/'),fetch:async(url,options={})=>{
  const payload=options.body?JSON.parse(options.body):null;calls.push({url,method:options.method||'GET',payload});
  if(url.endsWith('/facts'))return{ok:true,json:async()=>({facts:{revision:6}})};
  if(url.includes('/drafts/')){if(getFailure&&options.method!=='POST')return{ok:false,status:409,json:async()=>({error:getFailure})};return{ok:true,json:async()=>options.method==='POST'?{...candidate,candidate_id:'legacy-candidate'}:candidate};}
  if(options.method==='POST'){delivery.state={...delivery.state,revision:delivery.state.revision+1,edits:payload.edits,comments:payload.comments,draft_confirmations:{F01:{paragraph_actions:plan.paragraph_actions}}};return{ok:true,json:async()=>({state:delivery.state})};}
  return{ok:true,json:async()=>delivery};
}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/readable_text.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/delivery.js'),'utf8'),context);context.DeliveryUI=window.DeliveryUI;
const appSource=fs.readFileSync(path.join(__dirname,'../reader_ui/app.js'),'utf8');vm.runInContext(appSource.slice(0,appSource.indexOf("$('prev').onclick")),context);
const run=code=>vm.runInContext(code,context),card=document.elements['review-card'],changeBody=document.elements['change-body'];
const posts=()=>calls.filter(c=>c.method==='POST');
async function render(){run('bundle=fixture;selected="F01";mode="findings";renderCard()');await new Promise(resolve=>setImmediate(resolve));}
context.fixture=fixture;
(async()=>{
  const ui=window.DeliveryUI;await ui.load(fixture);await render();
  assert.ok(text(card).includes('账号约定明确，无需联动修改'));assert.ok(text(card).includes('拟新增'));assert.equal(card.querySelectorAll('.suggestion-diff').length,2);
  assert.ok(!text(card).includes('尚未说明'));assert.equal(posts().length,0,'opening structured report is read-only');
  await ui.openChange(item,item.clause_draft,'both');{const note=all(changeBody).find(n=>n.id==='change-comment'),warn=all(changeBody).find(n=>String(n.className).includes('comment-id-warning'));assert.ok(!note.value.includes('P001'),note.value);assert.ok(note.value.includes('F03'),'opinion IDs are left for the reviewer: '+note.value);assert.equal(warn.hidden,false);assert.ok(warn.textContent.includes('F03'));note.value='供协作者参考';note.fire('input');assert.equal(warn.hidden,true);assert.ok(!text(changeBody).includes('P002'),'paragraph reasons in the dialog show clause numbers');assert.ok(text(changeBody).includes('与第2条的交付衔接'),text(changeBody).slice(0,400));document.elements['change-dialog'].close();}
  await byText(card,'仅修改').onclick();assert.equal(posts().length,0,'only modify performs GET projection, never model POST');
  assert.equal(document.elements['change-dialog'].open,true);assert.equal(document.elements['change-title'].textContent,'确认这 2 处改稿');assert.ok(text(changeBody).includes('保留原文'));
  assert.ok(!text(changeBody).includes('建议如何写进合同'),'structured single-source plans do not repeat legacy audit prose');
  run('renderAll=()=>{}');await changeBody.querySelector('form').onsubmit({preventDefault(){}});
  const saved=posts().at(-1).payload;assert.equal(saved.edits.length,2);assert.equal(saved.edits[1].kind,'insert_paragraph_after');assert.equal(saved.comments.length,0,'only modify must not silently add the hidden suggested comment');assert.deepEqual(saved.draft_confirmation,{finding_id:'F01',candidate_id:'plan-candidate'});
  ui.openPanel();const details=document.elements['delivery-body'].querySelector('details'),summary=details.querySelector('summary');assert.equal(summary.textContent,'查看已确认的修改（2）和批注（0）');assert.ok(summary.className.includes('delivery-confirmed-toggle'));details.open=true;await details.fire('toggle');assert.equal(summary.attributes['aria-expanded'],'true');details.open=false;await details.fire('toggle');assert.equal(summary.attributes['aria-expanded'],'false');
  await ui.openChange(item,item.clause_draft,'custom');assert.ok(text(changeBody).includes('账号约定明确，无需联动修改'),'editing confirmed changes keeps scope reasons');
  delivery.state={revision:0,edits:[],comments:[]};await ui.load(fixture);getFailure='合同事实已变化，请先更新本条意见。';await ui.openChange(item,item.clause_draft,'edit');assert.ok(text(changeBody).includes(getFailure));assert.equal(posts().length,1,'plan conflicts never trigger a model or overwrite');getFailure=null;
  await ui.load(fixture);item.draft_plan={...plan,status:'needs_input',edits:[],insertions:[],questions:['实际验收负责人是谁？','通知采用哪个地址？'],paragraph_actions:[{piece_id:'P001',action:'needs_confirmation',reason:'缺少业务安排'}]};item.clause_draft='';window.ContractFactsUI.questionsForFinding=()=>[{id:'Q1',question:'实际验收负责人是谁？'}];await render();assert.ok(text(card).includes('实际验收负责人是谁？'));assert.ok(text(card).includes('通知采用哪个地址？'));assert.ok(byText(card,'去补充信息'));assert.equal(byText(card,'仅修改'),undefined);assert.equal(byText(card,'编辑改稿'),undefined);
  assert.equal(all(card).filter(n=>n.textContent==='实际验收负责人是谁？').length,1,'legacy information reminder does not repeat the plan question');assert.ok(text(card.querySelector('.finding-information-notice')).includes('有1条信息待确认'));assert.equal(card.querySelector('.information-question-preview'),null);assert.ok(byText(card,'查看并回答'));assert.equal(byText(card,'讨论这条信息'),undefined);assert.equal(byText(card,'讨论待确认的位置'),undefined);assert.ok(byText(card,'讨论这条'));assert.ok(text(card).includes('缺少业务安排'));assert.ok(text(card).includes('补充信息后，可在讨论中生成新版意见，再查看改稿。'));window.ContractFactsUI.questionsForFinding=()=>[];
  item.draft_plan={...plan,status:'not_needed',edits:[],insertions:[],comment:'原约定已经完整',paragraph_actions:paragraphs.map(p=>({piece_id:p.id,action:'retain',reason:'原约定已明确'}))};await render();assert.ok(text(card).includes('正文无需修改'));assert.equal(byText(card,'仅修改'),undefined);assert.equal(byText(card,'编辑改稿'),undefined);
  delete item.draft_plan;
  const originalTargets=delivery.targets.F01,originalRefs=item.refs,originalSuggestion=item.suggestion;
  delivery.targets.F01=[target(paragraphs[0])];item.refs=[item.refs[0]];item.suggestion='延长验收期限';item.clause_draft='1. 应在10日内验收。';await ui.load(fixture);await render();
  assert.equal(ui.needsPreparation(item,item.clause_draft),false);assert.ok(byText(card,'修改并批注'));assert.equal(byText(card,'整理改稿'),undefined);const directBaseline=posts().length,readsBefore=calls.length;
  await byText(card,'仅修改').onclick();assert.equal(document.elements['change-dialog'].open,true);assert.equal(document.elements['change-title'].textContent,'仅修改');assert.equal(document.getElementById('change-text').value,item.clause_draft);assert.equal(posts().length,directBaseline,'complete single-location legacy replacement previews without a model or save POST');assert.equal(calls.length,readsBefore,'direct preview requires no draft API call');document.elements['change-dialog'].close();
  delivery.targets.F01=originalTargets;item.refs=originalRefs;item.suggestion=originalSuggestion;item.clause_draft='将第1条修改为：1. 应在10日内验收。';await ui.load(fixture);await render();assert.ok(byText(card,'整理改稿'));assert.equal(card.querySelector('.draft-scope'),null,'unprepared legacy reports are not painted as unresolved business discussions');assert.ok(!text(card).includes('讨论这些位置怎么处理'));
  const baseline=posts().length;await byText(card,'整理改稿').onclick();assert.equal(posts().length,baseline,'explicit consent screen does not call a model');assert.ok(text(changeBody).includes('所选服务 · selected-model'));const checkbox=changeBody.querySelector('input');assert.equal(checkbox.checked,false);checkbox.checked=true;changeConfigOnReload=true;await byText(changeBody,'确认调用并整理').onclick();assert.equal(posts().length,baseline,'updated config requires reconfirmation');assert.equal(checkbox.checked,false);assert.ok(text(changeBody).includes('模型设置刚有变化'));
  checkbox.checked=true;await byText(changeBody,'确认调用并整理').onclick();const prepared=posts().at(-1).payload;assert.equal(prepared.config.model,'selected-model');assert.equal(prepared.config.api_key,'new-fixture-key');assert.equal(prepared.consent,true);assert.equal(prepared.engine_key,'compatible_api');assert.equal(prepared.expected_facts_revision,6);assert.equal(prepared.expected_opinion_revision,0);assert.equal(reloads,3);assert.ok(!text(changeBody).includes('本机 Codex'));
  delivery.state={revision:0,edits:[],comments:[]};ui.openPanel();assert.equal(document.elements['delivery-body'].querySelector('details'),null);assert.ok(text(document.elements['delivery-body']).includes('还没有确认正文修改'));
  console.log('draft_plan_ui: read-only structured plans, exact multi-edit/insertion confirmation, scope retention, conflict without model, visible input questions, no-change actions, explicit legacy model consent with fresh config, and accessible delivery disclosure passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
