'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

class Element{
  constructor(tag,document){
    this.tagName=tag.toUpperCase();this.ownerDocument=document;this.children=[];this.parentNode=null;
    this.listeners={};this.className='';this.textContent='';this.name='';this.value='';this.type='';
    this.hidden=false;this.open=false;this.required=false;this.readOnly=false;this.disabled=false;this.checked=false;
    this.files=[];this.attributes={};this.validityReports=0;
  }
  append(...items){for(const item of items){if(item===null||item===undefined)continue;this.children.push(item);if(item instanceof Element)item.parentNode=this;}}
  prepend(...items){for(const item of [...items].reverse()){if(item===null||item===undefined)continue;this.children.unshift(item);if(item instanceof Element)item.parentNode=this;}}
  replaceChildren(...items){for(const item of this.children)if(item instanceof Element)item.parentNode=null;this.children=[];this.append(...items);}
  addEventListener(type,handler){(this.listeners[type]||(this.listeners[type]=[])).push(handler);}
  dispatchEvent(event){for(const handler of this.listeners[event.type]||[])handler({preventDefault(){},target:this});}
  async fire(type){for(const handler of this.listeners[type]||[])await handler({preventDefault(){},target:this});}
  setAttribute(key,value){this.attributes[key]=String(value);if(key==='open')this.open=true;}
  remove(){if(!this.parentNode)return;this.parentNode.children=this.parentNode.children.filter(item=>item!==this);this.parentNode=null;}
  focus(){this.ownerDocument.activeElement=this;}
  reportValidity(){this.validityReports+=1;return this.checkValidity();}
  checkValidity(){
    if(!this.required)return true;
    if(this.type==='file')return Boolean(this.files&&this.files.length);
    if(this.type==='checkbox')return Boolean(this.checked);
    return String(this.value||'').trim()!=='';
  }
  closest(selector){let current=this;while(current){if(selector==='details'&&current.tagName==='DETAILS')return current;current=current.parentNode;}return null;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  querySelectorAll(selector){
    const selectors=selector.split(',').map(item=>item.trim());
    const result=[];
    const matches=(element,raw)=>{
      if(!(element instanceof Element))return false;
      if(raw.startsWith('.'))return element.className.split(/\s+/).includes(raw.slice(1));
      const found=raw.match(/^([a-z]+)(?:\[([a-z]+)\])?$/i);
      if(!found)return false;
      if(element.tagName!==found[1].toUpperCase())return false;
      if(!found[2])return true;
      if(found[2]==='required')return element.required===true;
      if(found[2]==='name')return Boolean(element.name);
      return Object.hasOwn(element.attributes,found[2]);
    };
    const visit=element=>{for(const child of element.children){if(!(child instanceof Element))continue;if(selectors.some(raw=>matches(child,raw)))result.push(child);visit(child);}};
    visit(this);return result;
  }
}

class Document{
  constructor(){this.activeElement=null;this.elements={};}
  createElement(tag){return new Element(tag,this);}
  getElementById(id){return this.elements[id]||null;}
}

function all(root){const values=[];const visit=value=>{if(!(value instanceof Element))return;values.push(value);value.children.forEach(visit);};visit(root);return values;}
function byName(root,name){return all(root).find(item=>item.name===name);}
function byText(root,text){return all(root).find(item=>item.textContent===text);}
function fullText(root){return all(root).map(item=>item.textContent).filter(Boolean).join('\n');}

const capabilities={
  limits:{docx_bytes:12*1024*1024},
  default_context:{region:'上海',review_date:'2026-09-22',input_kind:'模板审阅',payday:10},
  profiles:[
    {key:'labor',label:'劳动合同（上海）',roles:[],default_context:{region:'上海',review_date:'2026-09-20',input_kind:'模板审阅',payday:10},scope_note:'仅检查劳动合同三项重点。'},
    {key:'software_service',label:'小程序开发服务合同',roles:[{key:'client',label:'客户'},{key:'provider',label:'服务商'}],default_context:{region:'上海',review_date:'2026-09-22',input_kind:'拟签审阅'},scope_note:'仅检查服务范围、验收付款和终止交付。'},
  ],
  engines:[{key:'codex_cli',label:'本机 Codex · Sol',available:true,config_fields:[]}]
};

const document=new Document();
const dialog=document.createElement('dialog');dialog.showModal=()=>{dialog.open=true;};dialog.close=()=>{dialog.open=false;};
const body=document.createElement('div');
const openButton=document.createElement('button');
document.elements['new-review-dialog']=dialog;document.elements['new-review-body']=body;document.elements['new-review']=openButton;
document.elements['new-review-title']=document.createElement('h2');
const storage=new Map([['contract-review-role:software_service','provider'],['contract-review-party:software_service','不应恢复的公司']]);
const storageWrites=[];
const storageRemovals=[];
const localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>{storage.set(key,String(value));storageWrites.push([key,String(value)]);},removeItem:key=>{storage.delete(key);storageRemovals.push(key);}};
const sessionStorage={getItem:()=>null,setItem(){},removeItem(){}};
const requests=[];
let readyJob={job_id:'job-'+'a'.repeat(32),status:'ready',document_name:'fixture.docx',context:{contract_type:'software_service',our_role:'client',our_party:'甲方'}};
let startConflict=false,startResultStatus='running';
async function fetch(url,options={}){
  requests.push({url,options});
  if(url==='/api/review-capabilities')return {ok:true,status:200,json:async()=>capabilities};
  if(url==='/api/jobs'&&options.method==='POST')return {ok:true,status:201,json:async()=>({job:readyJob,preview:{context:readyJob.context,pieces_count:1,blank_pieces:0,head:[{text:'合同原文'}],pieces:[{text:'合同原文'}],warnings:['已忽略 word/footer1.xml 中仅用于显示页码的 PAGE/NUMPAGES 字段。','正文附件无法读取，存在影响审查的缺失。']}})};
  if(url==='/api/jobs/'+readyJob.job_id)return {ok:true,status:200,json:async()=>({job:readyJob,preview:{context:readyJob.context,pieces_count:1,blank_pieces:0,head:[],pieces:[]}})};
  if(url==='/api/jobs/'+readyJob.job_id+'/prepare'){
    readyJob={...readyJob,status:'needs_info',preparation:{...readyJob.preparation,status:'needs_info',revision:1,questions:[{id:'q1',question:'实际付款安排是什么？',reason:'影响解除后的结算。',required:true,answer:'',status:'unanswered'}],checks:[{id:'c1',title:'结算',requirement:'核对解除后结算'}],requirements:[{source_text:'优先检查结算',related_check_ids:['c1'],disposition:'merged',reason:'同一检查事项。'}]}};
    return {ok:true,status:200,json:async()=>({job:readyJob})};
  }
  if(url==='/api/jobs/'+readyJob.job_id+'/answers'){
    const payload=JSON.parse(options.body);assert.equal(payload.expected_revision,readyJob.preparation.revision);
    const answer=payload.answers[0];readyJob={...readyJob,status:'prepared',preparation:{...readyJob.preparation,revision:readyJob.preparation.revision+1,status:'prepared',questions:[{...readyJob.preparation.questions[0],...answer}]}};
    return {ok:true,status:200,json:async()=>({job:readyJob})};
  }
  if(url==='/api/jobs/'+readyJob.job_id+'/start'){
    if(startConflict)return {ok:false,status:409,json:async()=>({error:'任务已在另一处重新尝试'})};
    const payload=JSON.parse(options.body);
    if(payload.retry)readyJob={...readyJob,status:startResultStatus,attempt:readyJob.attempt+1};
    return {ok:true,status:200,json:async()=>({job:{...readyJob,status:startResultStatus}})};
  }
  throw new Error('unexpected fetch '+url);
}
class FileReader{
  readAsDataURL(){this.result='data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,ZmFrZQ==';queueMicrotask(()=>this.onload());}
}

const context={window:{},Event,document,localStorage,sessionStorage,fetch,FileReader,console,setTimeout,clearTimeout,queueMicrotask};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/review_jobs.js'),'utf8').replace('window.ReviewJobsUI={configure','window.ReviewJobsUI={_test:{checkJob},configure'),context);

(async()=>{
  const ui=context.window.ReviewJobsUI;
  await ui.open();
  assert.equal(document.elements['new-review-title'].textContent,'新建审查');
  let form=body.querySelector('form');
  assert.equal(form.tagName,'FORM');
  assert.equal(form.noValidate,true);
  assert.ok(!fullText(form).includes('读取只解析并保存在本机'));
  assert.ok(!fullText(form).includes('不会启动模型审核'));
  assert.equal(byText(form,'本次检查项目'),undefined);
  assert.ok(byText(form,'我方立场'));
  assert.equal(all(form).find(item=>item.readOnly)?.value,'用人单位');
  assert.equal(byName(form,'payday'),undefined);
  assert.equal(byText(form,'工资结算信息'),undefined);

  // Missing file is surfaced at the actual control, rather than silently doing nothing.
  await form.fire('submit');
  assert.equal(document.activeElement,byName(form,'document'));
  assert.ok(fullText(form).includes('请选择一个 DOCX 文件。'));

  // Labor now reads without a payday field or a silently supplied default.
  const file=byName(form,'document');file.files=[{name:'labor.docx',size:100}];
  await form.fire('submit');
  await new Promise(resolve=>setTimeout(resolve,0));
  const laborPost=requests.find(item=>item.url==='/api/jobs'&&item.options.method==='POST');
  assert.ok(laborPost);
  assert.equal(Object.hasOwn(JSON.parse(laborPost.options.body).context,'payday'),false);
  requests.length=0;
  await ui.open();form=body.querySelector('form');
  byName(form,'document').files=[{name:'service.docx',size:100}];

  // Service role restores only an allowed role. Party/company data is never restored.
  const profile=byName(form,'contract_type');profile.value='software_service';await profile.fire('change');
  const role=byName(form,'our_role');
  assert.deepEqual(role.children.map(item=>item.value).filter(Boolean),['client','provider']);
  assert.equal(role.value,'provider');
  assert.equal(byName(form,'our_party').value,'');
  const remember=byName(form,'remember_role');assert.equal(Boolean(remember.checked),true);
  assert.ok(remember.parentNode.parentNode.className.includes('review-job-stance-group'));
  assert.equal(byName(form,'payday'),undefined);

  // Explicitly unchecking forgets the saved default after a successful read.
  role.value='client';byName(form,'our_party').value='甲方';remember.checked=false;
  await form.fire('submit');
  await new Promise(resolve=>setTimeout(resolve,0));
  let post=requests.find(item=>item.url==='/api/jobs'&&item.options.method==='POST');
  assert.ok(post);
  const payload=JSON.parse(post.options.body);
  assert.equal(payload.context.our_role,'client');
  assert.equal(payload.context.our_party,'甲方');
  assert.equal(Object.hasOwn(payload.context,'payday'),false);
  assert.deepEqual(storageWrites,[]);
  assert.deepEqual(storageRemovals,['contract-review-role:software_service']);
  assert.equal(storage.has('contract-review-role:software_service'),false);
  assert.equal(storage.get('contract-review-party:software_service'),'不应恢复的公司');

  // Scope moves to the parsed preview; transmission target and explicit consent remain.
  assert.equal(byText(body,'本次检查项目'),undefined);assert.equal(fullText(body).includes('个文本段'),false);assert.equal(byText(body,'核对合同全文'),undefined);assert.equal(fullText(body).includes('PAGE/NUMPAGES'),false);assert.ok(fullText(body).includes('正文附件无法读取'));
  assert.ok(fullText(body).includes('合同文字将发送到下面的模型服务'));assert.ok(byText(body,'Sol'));assert.equal(byName(body,'engine_key'),undefined);
  assert.ok(fullText(body).includes('我同意将本次审查材料发送给所选模型'));

  // With no saved role the checkbox is genuinely opt-in; checking it stores
  // only the role key and still never stores the party/company value.
  await ui.open();form=body.querySelector('form');
  const freshProfile=byName(form,'contract_type');freshProfile.value='software_service';await freshProfile.fire('change');
  const freshRole=byName(form,'our_role');const freshRemember=byName(form,'remember_role');
  assert.equal(freshRole.value,'');assert.equal(Boolean(freshRemember.checked),false);
  byName(form,'document').files=[{name:'service.docx',size:100}];
  freshRole.value='client';byName(form,'our_party').value='甲方';freshRemember.checked=true;
  await form.fire('submit');await new Promise(resolve=>setTimeout(resolve,0));
  assert.deepEqual(storageWrites,[['contract-review-role:software_service','client']]);
  assert.equal(storage.get('contract-review-role:software_service'),'client');
  assert.equal(storageWrites.some(([key])=>key.includes('party')),false);

  // A saved ready job still resumes at preview + explicit model consent, without starting it.
  const postsBefore=requests.filter(item=>item.options.method==='POST').length;
  await ui.openJob(readyJob.job_id);
  assert.ok(fullText(body).includes('开始审核'));
  assert.ok(fullText(body).includes('我同意将本次审查材料发送给所选模型'));
  assert.equal(requests.filter(item=>item.options.method==='POST').length,postsBefore);
  // New workflow is a separate, explicit preparation call, never an automatic review.
  capabilities.profiles.unshift({key:'general',label:'合同全文审查',default_context:{region:'中国大陆',review_date:'2026-09-30'},scope_note:'合同全文通用审核'});
  let checklistOptions;
  context.window.ChecklistsUI={mountPicker:(host,options)=>(checklistOptions=options,({ready:Promise.resolve(),snapshot:()=>({id:'list-1',name:'结算清单',version:1,builtin:false,contract_type:'',stance:'',items:[{id:'c1',title:'结算',requirement:'核对结算',enabled:true}]})})),renderChecks:(host,checks)=>{checks.forEach(check=>{const n=document.createElement('p');n.textContent=check.title;host.append(n);});}};
  readyJob={...readyJob,workflow_version:2,status:'ready',context:{contract_type:'general',our_party:'甲方'},preparation:{revision:0,status:'pending',questions:[],checks:[],requirements:[]}};
  await ui.open();form=body.querySelector('form');
  assert.ok(byName(form,'business_goal'));assert.ok(byName(form,'business_facts'));
  byName(form,'document').files=[{name:'general.docx',size:100}];byName(form,'our_party').value='甲方';
  byName(form,'business_goal').value='优先检查结算';byName(form,'business_facts').value='尚未付款';
  dialog.scrollTop=410;const intakeFile=byName(form,'document').files[0];
  checklistOptions.openStep(host=>host.append(document.createElement('h3')));assert.equal(dialog.scrollTop,0);assert.ok(body.querySelector('.review-checklist-step'));
  checklistOptions.openStep(null);assert.equal(body.querySelector('form'),form);assert.equal(dialog.scrollTop,410);assert.equal(byName(form,'document').files[0],intakeFile);assert.equal(byName(form,'business_facts').value,'尚未付款');
  await form.fire('submit');
  post=requests.filter(item=>item.url==='/api/jobs'&&item.options.method==='POST').at(-1);
  const generalPayload=JSON.parse(post.options.body);assert.equal(generalPayload.context.business_goal,'优先检查结算');assert.equal(generalPayload.context.business_facts,'尚未付款');assert.equal(generalPayload.checklist_snapshot.name,'结算清单');
  assert.ok(fullText(body).includes('检查准备情况'));assert.equal(byText(body,'开始审核'),undefined);
  const originalForm=form,originalFile=byName(form,'document').files[0];
  const creates=()=>requests.filter(item=>item.url==='/api/jobs'&&item.options.method==='POST').length;
  const created=creates();await byText(body,'返回上一步').fire('click');assert.equal(body.querySelector('form'),originalForm);assert.equal(byName(originalForm,'document').files[0],originalFile);assert.equal(byName(originalForm,'business_facts').value,'尚未付款');
  await originalForm.fire('submit');assert.equal(creates(),created);
  await byText(body,'返回上一步').fire('click');byName(originalForm,'business_goal').value='增加验收期限检查';await originalForm.fire('submit');assert.equal(creates(),created+1);assert.equal(generalPayload.context.business_goal,'优先检查结算');
  const preparationPosts=()=>requests.filter(item=>item.url.endsWith('/prepare')).length;
  const beforePrep=preparationPosts();await ui.openJob(readyJob.job_id);assert.equal(preparationPosts(),beforePrep);
  form=body.querySelector('form');await form.fire('submit');assert.equal(preparationPosts(),beforePrep); // Consent required.
  byName(form,'consent').checked=true;await form.fire('submit');assert.equal(preparationPosts(),beforePrep+1);
  assert.ok(fullText(body).includes('需要补充的信息'));assert.equal(body.querySelector('.review-job-preview'),null);assert.equal(byName(body,'engine_key'),undefined);assert.ok(fullText(body).includes('影响解除后的结算。'));assert.equal(byText(body,'开始审核'),undefined);
  form=body.querySelector('form');const beforeAnswers=requests.filter(item=>item.url.endsWith('/answers')).length;
  await form.fire('submit');assert.equal(requests.filter(item=>item.url.endsWith('/answers')).length,beforeAnswers+1);assert.equal(readyJob.status,'prepared');assert.ok(byText(body,'开始审核'));
  const unknownPost=JSON.parse(requests.filter(item=>item.url.endsWith('/answers')).at(-1).options.body);assert.equal(unknownPost.answers[0].status,'unknown');
  await byText(body,'修改补充信息').fire('click');form=body.querySelector('form');byName(form,'unknown_q1').checked=false;await byName(form,'unknown_q1').fire('change');byName(form,'answer_q1').value='验收后一次性付款';await form.fire('submit');
  assert.equal(readyJob.preparation.revision,3);assert.equal(readyJob.status,'prepared');assert.ok(byText(body,'开始审核'));assert.equal(fullText(body).includes('准备完成：本次要检查的内容'),false);
  const preparedPosts=preparationPosts();await ui.openJob(readyJob.job_id);assert.equal(preparationPosts(),preparedPosts);assert.ok(byText(body,'开始审核'));
  // Before review, an already-prepared answer can be corrected without another model call.
  await byText(body,'修改补充信息').fire('click');form=body.querySelector('form');assert.equal(byName(form,'answer_q1').value,'验收后一次性付款');
  byName(form,'answer_q1').value='签约先付30%，验收后付余款';await form.fire('submit');assert.equal(readyJob.preparation.revision,4);assert.equal(readyJob.status,'prepared');assert.ok(byText(body,'开始审核'));assert.equal(preparationPosts(),preparedPosts);assert.equal(JSON.parse(requests.filter(item=>item.url.endsWith('/answers')).at(-1).options.body).expected_revision,3);
  // Explicit skip preserves answers already typed and marks only blanks unknown.
  await byText(body,'修改补充信息').fire('click');form=body.querySelector('form');byName(form,'answer_q1').value='保留已经填好的回答';await byText(form,'暂不补充，继续').fire('click');await new Promise(resolve=>setImmediate(resolve));assert.equal(readyJob.preparation.questions[0].status,'answered');assert.equal(readyJob.preparation.questions[0].answer,'保留已经填好的回答');
  const savedQuestions=readyJob.preparation.questions;readyJob={...readyJob,preparation:{...readyJob.preparation,questions:[]}};await ui.openJob(readyJob.job_id);assert.equal(byText(body,'修改补充信息'),undefined);readyJob={...readyJob,preparation:{...readyJob.preparation,questions:savedQuestions}};await ui.openJob(readyJob.job_id);
  form=body.querySelector('form');const beforeStart=requests.filter(item=>item.url.endsWith('/start')).length;await form.fire('submit');assert.equal(requests.filter(item=>item.url.endsWith('/start')).length,beforeStart);byName(form,'consent').checked=true;await form.fire('submit');assert.equal(requests.filter(item=>item.url.endsWith('/start')).length,beforeStart+1);assert.ok(fullText(body).includes('模型正在审核'));
  readyJob={...readyJob,status:'preparing'};await ui.openJob(readyJob.job_id);assert.ok(fullText(body).includes('模型正在阅读全文'));assert.ok(body.querySelector('.preparation-breath'));assert.equal(body.querySelector('form'),null);
  readyJob={...readyJob,status:'ready',preparation:{...readyJob.preparation,status:'failed',error:'模型超时'}};await ui.openJob(readyJob.job_id);assert.ok(byText(body,'重试检查准备情况'));assert.ok(fullText(body).includes('模型超时'));
  form=body.querySelector('form');byName(form,'consent').checked=true;await form.fire('submit');assert.equal(JSON.parse(requests.filter(item=>item.url.endsWith('/prepare')).at(-1).options.body).retry,true);
  readyJob={...readyJob,status:'failed',report_id:'report-partial',preparation:{...readyJob.preparation,status:'prepared'},error:'模型返回未通过结果校验，部分检查未完成：evidence_status invalid enum'};let opened='';ui.configure({onComplete:async id=>{opened=id;}});await ui.openJob(readyJob.job_id);assert.ok(byText(body,'部分检查尚未完成'));assert.ok(fullText(body).includes('报告已生成，但仍有事项没检查完'));assert.equal(body.querySelector('.review-job-error'),null);assert.ok(!fullText(body).includes('evidence_status'));assert.equal(byText(body,'重新选择合同'),undefined);await byText(body,'查看报告').fire('click');assert.equal(opened,'report-partial');
  readyJob={...readyJob,report_id:null,error:'模型超时'};await ui.openJob(readyJob.job_id);assert.ok(byText(body,'审核没有完成'));assert.equal(byText(body,'查看报告'),undefined);
  readyJob={...readyJob,report_id:null,error:'quote_validation_version: schema invalid'};await ui.openJob(readyJob.job_id);assert.ok(fullText(body).includes('模型的回复还不能整理成完整报告'));assert.ok(!/quote_validation_version|schema invalid/.test(fullText(body)));
  // Late directory renders do not revoke consent for the same transmission configuration.
  let modelRender;const selectedModel={engine_key:'compatible_api',provider:'DeepSeek',config:{base_url:'https://api.deepseek.com',api_key:'fixture-key',model:'deepseek-flash'}};
  context.window.ReviewModelSelector={mount(_host,_engines,onChange){modelRender=onChange;return {ready:Promise.resolve(),value:()=>selectedModel,refresh:()=>Promise.resolve()};}};
  capabilities.engines.push({key:'compatible_api',available:true});readyJob={...readyJob,status:'ready',preparation:{status:'pending'}};await ui.openJob(readyJob.job_id);form=body.querySelector('form');const transmissionConsent=byName(form,'consent');transmissionConsent.checked=true;
  modelRender();assert.equal(transmissionConsent.checked,true);selectedModel.config.api_key='changed-fixture-key';modelRender();assert.equal(transmissionConsent.checked,false);
  transmissionConsent.checked=true;selectedModel.provider='Another provider';modelRender();assert.equal(transmissionConsent.checked,false);
  // Failed rereview resumes the same stored task and requires fresh transmission consent.
  readyJob={...readyJob,status:'failed',review_kind:'rereview',parent_report_id:'parent-report',report_id:null,attempt:2,retryable:true,failure_type:'ModelClientError',failure_diagnostics:null,
    context:{...readyJob.context,business_facts:'价款分三次支付'},preparation:{status:'prepared',revision:4,checks:[{id:'C1',title:'付款',requirement:'核对付款安排'}],questions:[{id:'Q1',question:'付款安排？',status:'answered',answer:'签约先付30%，验收后付余款'}]},checklist_snapshot:{name:'保存的付款清单',items:[{id:'C1',title:'付款'}]}};
  const retryInputs=JSON.stringify([readyJob.context,readyJob.preparation,readyJob.checklist_snapshot]),postsBeforeRetry=requests.filter(r=>r.options.method==='POST').length,starts=()=>requests.filter(r=>r.url.endsWith('/start'));
  await ui.openJob(readyJob.job_id);assert.equal(document.elements['new-review-title'].textContent,'合同复审');assert.ok(fullText(body).includes('具体原因未记录'));assert.ok(byText(body,'重新尝试'));assert.equal(requests.filter(r=>r.options.method==='POST').length,postsBeforeRetry,'opening failure does not restart or upload');
  await byText(body,'重新尝试').fire('click');form=body.querySelector('form');assert.ok(form);assert.equal(byName(form,'consent').checked,false);assert.ok(fullText(form).includes('不需要重新上传'));assert.ok(fullText(form).includes('签约先付30%，验收后付余款'));assert.equal(byText(form,'修改补充信息'),undefined,'started task does not offer an unsupported answer edit');assert.equal(byName(form,'document'),undefined);assert.equal(requests.filter(r=>r.options.method==='POST').length,postsBeforeRetry);
  await form.fire('submit');assert.equal(requests.filter(r=>r.options.method==='POST').length,postsBeforeRetry,'retry must wait for the model and consent');
  const retryConsent=byName(form,'consent');retryConsent.checked=true;selectedModel.config.model='chosen-retry-model';modelRender();assert.equal(retryConsent.checked,false);assert.equal(requests.filter(r=>r.options.method==='POST').length,postsBeforeRetry);
  retryConsent.checked=true;byName(form,'web_search').checked=false;const startsBeforeRetry=starts().length;await Promise.all([form.fire('submit'),form.fire('submit')]);assert.equal(starts().length,startsBeforeRetry+1,'double submit starts one attempt');
  const retryPayload=JSON.parse(starts().at(-1).options.body);assert.equal(retryPayload.retry,true);assert.equal(retryPayload.expected_attempt,2);assert.equal(retryPayload.consent,true);assert.equal(retryPayload.engine_key,'compatible_api');assert.equal(retryPayload.config.model,'chosen-retry-model');assert.equal(retryPayload.web_search,false);assert.equal(readyJob.attempt,3);assert.equal(JSON.stringify([readyJob.context,readyJob.preparation,readyJob.checklist_snapshot]),retryInputs);assert.equal(document.elements['new-review-title'].textContent,'合同复审');assert.equal(body.querySelector('form'),null);
  // Stale failed attempt cannot be resubmitted until current state is read again.
  readyJob={...readyJob,status:'failed',attempt:3,error:'模型超时'};await ui.openJob(readyJob.job_id);await byText(body,'重新尝试').fire('click');form=body.querySelector('form');byName(form,'consent').checked=true;startConflict=true;await form.fire('submit');assert.equal(byName(form,'consent').checked,false);assert.equal(byText(form,'重新尝试').disabled,true);assert.ok(byText(form,'重新读取任务'));const staleStarts=starts().length;modelRender();assert.equal(byText(form,'重新尝试').disabled,true);byName(form,'consent').checked=true;await form.fire('submit');assert.equal(starts().length,staleStarts);
  startConflict=false;readyJob={...readyJob,status:'failed',attempt:4};delete readyJob.review_kind;await byText(form,'重新读取任务').fire('click');assert.equal(document.elements['new-review-title'].textContent,'合同复审','parent report also identifies rereview');await byText(body,'重新尝试').fire('click');form=body.querySelector('form');assert.equal(byName(form,'consent').checked,false);byName(form,'consent').checked=true;startResultStatus='completed';readyJob.report_id='retry-report';await form.fire('submit');assert.equal(JSON.parse(starts().at(-1).options.body).expected_attempt,4);assert.equal(opened,'retry-report');assert.equal(body.querySelector('form'),null,'completed response opens the report, never another start form');assert.equal(dialog.open,false);startResultStatus='running';
  const completedPosts=starts().length;await ui.openJob(readyJob.job_id);assert.equal(starts().length,completedPosts,'opening a completed task does not restart');
  readyJob={...readyJob,status:'failed',report_id:null,retryable:false};await ui.openJob(readyJob.job_id);assert.equal(byText(body,'重新尝试'),undefined,'nonretryable failure does not promise another attempt');
  assert.equal(fs.readFileSync(path.join(__dirname,'../reader_ui/workbench.js'),'utf8').includes('必要信息答齐后再审核'),false);
  let backgroundCompletion=null;ui.configure({onComplete:async(id,options)=>{backgroundCompletion={id,automatic:options.automatic,canNavigate:options.canNavigate()};return false;}});
  readyJob={...readyJob,job_id:'job-'+'c'.repeat(32),status:'running',report_id:'background-report'};await ui.openJob(readyJob.job_id);dialog.close();readyJob.status='completed';await ui._test.checkJob(readyJob.job_id);
  assert.deepEqual(backgroundCompletion,{id:'background-report',automatic:true,canNavigate:false});assert.equal(dialog.open,false);
  // Report ordering keeps established risks visible even when the finding has open questions.
  const appSource=fs.readFileSync(path.join(__dirname,'../reader_ui/app.js'),'utf8');
  // Exercise the real app callback: failed tasks with saved reports are not in allReviews.
  let completionHandler,loadedPartial=null;
  const completionContext={bundle:null,location:{href:'http://localhost/'},ReviewJobsUI:{configure(value){completionHandler=value.onComplete;}},refreshReportList:async()=>[],loadReport:async id=>{loadedPartial=id;return true;}};
  vm.createContext(completionContext);
  vm.runInContext(appSource.slice(appSource.indexOf('ReviewJobsUI.configure({onComplete:'),appSource.indexOf('OpinionRevisions.configure(')),completionContext);
  await completionHandler('report-partial');assert.equal(loadedPartial,'report-partial');
  completionContext.loadReport=async()=>false;
  await assert.rejects(completionHandler('missing-report'),/阅读台暂时未能打开/);
  const reportContext={document:{getElementById(){return null;}},fixture:{state:{reviews:{}},report:{findings:[{id:'info',risk:null,status:'信息不足'},{id:'high-with-question',risk:'高',status:'信息不足',questions:['何时交付？']},{id:'low',risk:'低'}],options:[]},completeness:[]}};
  vm.createContext(reportContext);vm.runInContext(appSource.slice(0,appSource.indexOf('function node(')),reportContext);vm.runInContext('bundle=fixture;',reportContext);
  assert.equal(vm.runInContext('items().map(item=>item.id).join(",")',reportContext),'high-with-question,low,info');
  assert.equal(vm.runInContext('isInformation(bundle.report.findings[1])',reportContext),false);
  vm.runInContext('mode="facts";',reportContext);assert.equal(vm.runInContext('items().length',reportContext),0);
  console.log('review_jobs_ui: legacy intake and resume; new snapshot/facts, explicit consent/preparation, unknown answers continue, revision, refresh/retry, failed report access passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
