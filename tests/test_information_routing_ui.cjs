'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
class Element{
 constructor(tag,doc){this.tagName=tag.toUpperCase();this.doc=doc;this.children=[];this.listeners={};this.attributes={};this.textContent='';this.className='';this.value='';this.checked=false;this.hidden=false;this.disabled=false;this.dataset={};this.style={};this.classList={toggle(){},add(){},remove(){}};}
 append(...nodes){for(const n of nodes){n.parentNode=this;this.children.push(n);}}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);}
 async fire(type){for(const fn of this.listeners[type]||[])await fn({preventDefault(){},target:this});}
 setAttribute(k,v){this.attributes[k]=v;}
 focus(){this.doc.activeElement=this;}
 scrollIntoView(){}
 querySelector(s){return this.querySelectorAll(s)[0]||null;}
 querySelectorAll(s){return all(this).slice(1).filter(n=>s.startsWith('.')?n.className.split(' ').includes(s.slice(1)):n.tagName===s.toUpperCase());}
}
const all=n=>[n,...n.children.flatMap(all)],text=n=>all(n).map(x=>x.textContent).join('\n');
const document={elements:{},createElement(tag){return new Element(tag,this);},createTextNode(text){const n=this.createElement('text');n.textContent=text;return n;},getElementById(id){return this.elements[id]||Object.values(this.elements).flatMap(all).find(n=>n.id===id)||null;},querySelectorAll(){return[];}};document.body=document.createElement('body');
for(const match of fs.readFileSync(path.join(__dirname,'../reader_ui/index.html'),'utf8').matchAll(/id="([^"]+)"/g))document.elements[match[1]]=document.createElement('div');
const no=()=>{},opened=[],gateEntered=[],requests=[];let completedCallback,deliveryLoads=0,blocked=true;
const gate={enabled:true,status:'needs_response',revision:0,facts_revision:0,questions:[{id:'shared',question:'实际安排是什么？',status:'unanswered',finding_ids:['F1','F2']}]};
const full={id:'report-test',report:{context:{contract_type:'general'},findings:[{id:'F1',title:'付款',status:'发现风险',risk:'中',reason:'P001未约定结算。',questions:['同一问题'],refs:[{id:'P001',quote:'付款'}]},{id:'F2',title:'交付',status:'发现风险',risk:'低',reason:'需要核对',refs:[]}],options:[]},paragraphs:[{id:'P001',text:'付款另定。',location:'正文 / 第八条 付款',numbering_label:'8.',display_text:'8. 付款另定。'}],sources:[],completeness:[],state:{reviews:{},selected_options:[],revision:0}};
let facts={revision:0,facts:[],questions:[...gate.questions,{id:'unknown',question:'上线日期？',status:'unknown',finding_ids:['F1']},{id:'answered',question:'金额？',status:'answered',fact_id:'a',finding_ids:['F2']}],facts:[{id:'a',text:'金额？\n答：100元',active:true,source:{kind:'preparation',quote:'100元'}}]};let saveFails=false;
const response=(status,value)=>({ok:status<400,status,json:async()=>value});
const window={addEventListener:no,dispatchEvent:no,matchMedia:()=>({matches:false}),InformationGateUI:{open:(id,value)=>opened.push([id,value]),close:no,configure:no}};
const DeliveryUI={load:async()=>deliveryLoads++,statusFor:()=> '待处理',statusLabel:()=> '待处理',count:no,decorateDocument:no,available:()=>true,needsPreparation:()=>false,suggestions:()=>[]};
const WorkbenchUI={configure:no,refresh:async()=>{},showHome:async()=>{},isHome:()=>false,enter:no,enterInformationGate:value=>gateEntered.push(value)};
const context={window,document,console,URL,URLSearchParams,Event,location:new URL('http://localhost/'),history:{replaceState:no,pushState:no},requestAnimationFrame:fn=>fn(),setTimeout,clearTimeout,DeliveryUI,WorkbenchUI,DiscussionUI:{close:no,open:no,refresh:async()=>{}},ReviewJobsUI:{configure(options){completedCallback=options.onComplete;}},OpinionRevisions:{configure:no},fetch:async(url,options={})=>{
 requests.push(url);
 if(url.endsWith('/facts/answer'))return response(saveFails?500:200,saveFails?{error:'fixture save error'}:{facts});
 if(url.endsWith('/facts'))return response(200,{facts});
 if(url==='/api/reports/report-test')return blocked?response(409,{error:'confirm information',code:'information_gate_required',information_gate:gate}):response(200,full);
 throw new Error('unexpected '+url);
}};vm.createContext(context);for(const file of ['readable_text.js','contract_facts.js','app.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/'+file),'utf8'),context);
const run=code=>vm.runInContext(code,context),byText=(n,t)=>all(n).find(x=>x.textContent===t);
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(await run("loadReport('report-test',{historyMode:'replace'})"),true);assert.equal(opened.length,1);assert.equal(gateEntered.length,1);assert.equal(deliveryLoads,0);assert.equal(run('bundle'),null);
 await completedCallback('report-test');assert.equal(opened.length,2,'ordinary completion checks same gate');assert.equal(deliveryLoads,0,'no provisional report loaded');
 blocked=false;await run("loadReport('report-test',{historyMode:'replace'})");assert.equal(deliveryLoads,1);assert.equal(run('bundle.id'),'report-test');const card=document.elements['review-card'];assert.ok(text(card).includes('有2条信息待确认'));assert.equal(card.querySelector('.other-options'),null,'no duplicate full question tail');
 const beforeBackground=deliveryLoads;await completedCallback('new-report',{automatic:true,canNavigate:()=>false,documentName:'后台完成的合同'});assert.equal(deliveryLoads,beforeBackground);assert.equal(run('bundle.id'),'report-test');assert.ok(text(document.elements['background-review-complete']).includes('后台完成的合同'));assert.equal(document.elements['background-review-complete'].hidden,false);byText(document.elements['background-review-complete'],'稍后查看').onclick();assert.equal(document.elements['background-review-complete'].hidden,true);
 const jump=card.querySelector('.finding-information-link');jump.onclick();assert.equal(document.elements['reader-workspace'].hidden,true);assert.equal(document.elements['review-contract-facts'].hidden,false);assert.equal(document.activeElement.id,'fact-question-shared');
 assert.equal(jump.textContent,'查看并回答');assert.ok(text(card.querySelector('.finding-information-notice')).includes('实际安排是什么？'));
 const host=document.elements['review-contract-facts'];assert.ok(text(host).includes('已回答'));assert.ok(text(host).includes('尚未回答'));assert.ok(text(host).includes('暂不能提供'));assert.equal(all(host).filter(n=>n.textContent==='实际安排是什么？').length,1);assert.equal(window.ContractFactsUI.questionsForFinding('F2').length,1,'shared question links both findings, excludes answered');
 const shared=document.getElementById('fact-question-shared'),answer=shared.querySelector('textarea');answer.value='当前输入不能丢';saveFails=true;await shared.querySelector('form').fire('submit');assert.equal(answer.value,'当前输入不能丢');assert.ok(text(shared).includes('fixture save error'));
 assert.ok(byText(host,'＋ 新增信息'));assert.ok(byText(host,'编辑更正'));assert.ok(byText(host,'撤回'));
 const answered=document.getElementById('contract-fact-a');assert.equal(all(answered).filter(n=>n.textContent==='金额？').length,1,'question shown once');assert.equal(all(answered).filter(n=>n.textContent==='100元').length,1,'answer shown once');assert.ok(!text(answered).includes('答：'));assert.ok(!text(answered).includes('原话出处'),'quote equal to answer is not repeated');await byText(answered,'编辑更正').fire('click');assert.equal(answered.querySelector('.fact-editor').querySelector('textarea').value,'100元','editor holds the answer only');
 run("navigate('findings','F2',false)");assert.ok(text(card).includes('有1条信息待确认'));assert.ok(!text(card).includes('同一问题'));
 // A pre-publication localized update is already applied; only subsequent edits are pending.
 full.report.metadata={post_review_information:{version:1,facts_revision:0,affected_finding_ids:['F1'],attempt:1}};facts.has_updates_since_review=true;await window.ContractFactsUI.load(full);
 assert.ok(text(host).includes('这份报告的相关意见已根据这些补充信息调整。'));assert.equal(text(host).includes('尚未用于全文复审'),false);
 facts.revision=1;await window.ContractFactsUI.load(full);assert.ok(text(host).includes('信息已保存，后续讨论会用到；还没有按这些信息复审全文。'));assert.equal(text(host).includes('已用于本次报告的相关意见调整'),false);
 full.report.metadata.post_review_information={version:1,facts_revision:1,affected_finding_ids:[],attempt:0};await window.ContractFactsUI.load(full);assert.equal(host.querySelector('.fact-update-note'),null,'all unknown has no model adjustment claim, and no later-edit note at applied revision');
 const unknownQuestion=facts.questions.find(q=>q.id==='unknown');
 assert.equal(document.getElementById('fact-question-unknown').querySelector('input').checked,false,'legacy unknown without a response is not a human choice');
 unknownQuestion.response_recorded=true;full.report.findings[0].title='P001付款';await window.ContractFactsUI.load(full);
 assert.equal(document.getElementById('fact-question-unknown').querySelector('input').checked,true);
 assert.ok(!text(host).includes('P001'),'discussion titles use readable contract locations');
 assert.ok(run("researchSummary({status:'partial',source_count:20})").includes('具体原因未记录'));
 assert.ok(run("researchSummary({status:'partial',source_count:20,diagnostics:{error_codes:['max_uses_exceeded'],partial_reasons:['tool_error'],stages:[]}})").includes('次数上限'));
 assert.ok(run("researchSummary({status:'partial',source_count:2,diagnostics:{answer_complete:false,partial_reasons:['nonterminal_stop'],stages:[{stop_reason:'max_tokens'}]}})").includes('输出长度上限'));
 assert.ok(run("researchSummary({status:'partial',source_count:1,memo_truncated:true})").includes('只保留了前一部分'));
 assert.equal(run("researchSummary({status:'searched',source_count:3})"),'');
 // Independent auxiliary loads start together; a slow delivery request does not delay fetching facts.
 let releaseDelivery;const originalLoad=DeliveryUI.load;DeliveryUI.load=()=>new Promise(resolve=>{releaseDelivery=resolve;});
 requests.length=0;const loadingReport=run("loadReport('report-test')");await new Promise(resolve=>setImmediate(resolve));
 try{assert.ok(requests.some(url=>url.endsWith('/facts')),'facts request starts while delivery is still pending');}
 finally{releaseDelivery?.();DeliveryUI.load=originalLoad;}
 await loadingReport;
 console.log('information_routing_ui: parallel auxiliary loading, research reasons, direct URL/completion gate, central facts and saved input passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
