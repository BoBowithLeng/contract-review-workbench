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
Element.prototype.classList={};
const document={elements:{},createElement(tag){const n=new Element(tag,this);n.dataset={};n.style={setProperty(){}};n.scrollTop=0;n.clientHeight=600;n.classList={add(...v){n.className=[...new Set(n.className.split(' ').concat(v))].join(' ');},remove(...v){n.className=n.className.split(' ').filter(x=>!v.includes(x)).join(' ');},toggle(v,on){if(on===undefined)on=!n.className.split(' ').includes(v);on?this.add(v):this.remove(v);return on;}};return n;},createTextNode(text){return this.createElement('text',text);},getElementById(id){return this.elements[id]||null;},querySelectorAll(){return [];}};
document.createTextNode=text=>{const n=document.createElement('text');n.textContent=text;return n;};document.body=document.createElement('body');
Element.prototype.getBoundingClientRect=()=>({top:0,left:0,right:1200,width:1200,height:200});Element.prototype.scrollIntoView=function(){};
const html=fs.readFileSync(path.join(__dirname,'../reader_ui/index.html'),'utf8');
for(const match of html.matchAll(/id="([^"]+)"/g))document.elements[match[1]]=document.createElement('div');
const window={dispatchEvent(){},addEventListener(){},matchMedia:()=>({matches:false}),location:{assign(){}},setTimeout,clearTimeout};
const DeliveryUI={load:async()=>{},statusFor:()=> '待处理',statusLabel:()=> '待处理',count(){},decorateDocument(){},available:item=>item.status!=='处理失败',needsPreparation:()=>false,suggestions:()=>[],revision:()=>0};
const noop=()=>{};const WorkbenchUI={configure:noop,refresh:async()=>{},showHome:async()=>{},isHome:()=>false,enter:noop};
const context={window,document,console,URL,URLSearchParams,Event,location:new URL('http://localhost/'),history:{replaceState(){},pushState(){}},fetch:async()=>({ok:true,json:async()=>({results:[]})}),setTimeout,clearTimeout,requestAnimationFrame:fn=>fn(),DeliveryUI,WorkbenchUI,DiscussionUI:{close(){},open(){},refresh:async()=>{}},ReviewJobsUI:{configure(){}},OpinionRevisions:{configure(){}}};vm.createContext(context);
for(const file of ['readable_text.js','checklists.js','app.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/'+file),'utf8'),context);
const ui=window.ChecklistsUI,host=document.elements['review-checklist-results'];
const builtin=JSON.parse(fs.readFileSync(path.join(__dirname,'../builtin_checklists.json'),'utf8')).checklists.find(c=>c.items.length===31);
const statuses=['未检出','发现风险','信息不足','不适用','处理失败'];
const checks=builtin.items.map((item,index)=>({...item,groups:[{group_id:item.id+':G01',title:'签约身份',requirement:'### 签约身份\n核对身份与职责。'},{group_id:item.id+':G02',title:'交接权限',requirement:'### 交接权限\n核对交接权限。'}]}));
const report={context:{contract_type:'general'},preparation:{checklist_group_protocol_version:1,checks},findings:[{id:'F01',title:'账号交接安排',status:'发现风险',risk:'中',reason:'P001与F01的finding没有约定交接。',suggestion:'改P001',clause_draft:'乙方应在验收后移交账号。',refs:[{id:'P001',quote:'账号'}]}],options:[{id:'O1',title:'参考F01方案',finding_ids:['F01'],steps:['见F01的clause_draft'],clause_draft:'见各finding的clause_draft，合并后形成完整条款。'}],checklist_results:checks.map((c,i)=>({check_id:c.id,title:c.title,status:statuses[i%5],reason:'整项检查说明',finding_ids:i===16?['F01']:[],groups:c.groups.map((g,n)=>({group_id:g.group_id,title:g.title,status:n===0?statuses[i%5]:'信息不足',reason:'具体分组说明 '+i+'-'+n,finding_ids:i===16?['F01']:[]}))}))};
const bundle={id:'fresh',report,state:{reviews:{},selected_options:[],revision:0},sources:[],paragraphs:[{id:'P001',text:'账号由乙方保管',display_text:'1. 账号由乙方保管',numbering_label:'1.',level:2,heading_parent:null,location:'第一条 · 账号管理'}],completeness:[]};context.testBundle=bundle;
const run=code=>vm.runInContext(code,context);
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));run("bundle=testBundle;mode='checklists';renderAll(false);");
 assert.equal(document.elements['reader-workspace'].hidden,true);assert.equal(host.hidden,false);assert.equal(document.elements['tab-basics'].hidden,true);
 assert.equal(host.querySelectorAll('.check-results-item').length,10);assert.ok(text(host).includes('C01'));assert.equal(host.querySelectorAll('.check-requirement-group').every(d=>!d.open),true);
 let summary=host.querySelector('.check-requirement-group').querySelector('summary');await summary.fire('click');assert.ok(text(host.querySelector('.check-group-result')).includes('未发现问题'));
 assert.equal(host.querySelectorAll('.check-overall-result').length,1);assert.ok(host.querySelector('.check-panel-head').contains(host.querySelector('.check-overall-result')));assert.equal(host.querySelector('.check-group-result').querySelector('.check-overall-result'),null);
 await byText(host,'下一页').fire('click');assert.ok(text(host).includes('C11'));assert.equal(host.querySelectorAll('.check-results-item').length,10);
 let selectedRow=host.querySelectorAll('.check-results-item').find(n=>text(n).includes('C17'));await selectedRow.fire('click');let groups=host.querySelectorAll('.check-requirement-group');
 assert.ok(text(groups[0]).includes('发现问题'));assert.ok(text(groups[1]).includes('待确认'));assert.equal(text(groups[0]).split('签约身份').length-1,1,'heading must not repeat in requirement body');
 groups[1].open=true;await groups[1].fire('toggle');await groups[1].querySelector('summary').fire('click');host.scrollTop=155;host.querySelector('.check-results-items').scrollTop=123;
 assert.equal(host.querySelectorAll('.check-overall-result').length,1,'switching child never repeats the whole-item explanation');assert.equal(host.querySelector('.check-group-result').querySelector('.check-overall-result'),null);
 await byText(host.querySelector('.check-group-result'),'查看意见：账号交接安排').fire('click');
 assert.equal(document.elements['reader-workspace'].hidden,false);assert.equal(host.hidden,true);assert.ok(text(document.elements['paper']).includes('1. '));assert.ok(text(document.elements['outline']).includes('1. 账号由乙方保管'));assert.equal(document.elements['outline'].children[0].style.paddingLeft,'12px','root heading must not be indented merely because styles skip level1');
 const card=document.elements['review-card'];assert.ok(text(card).includes('相关原文'));assert.ok(text(card).includes('账号管理'));assert.ok(text(card).includes('第一条'));assert.ok(!/P001|F01|finding|clause_draft/.test(text(card)),text(card));assert.equal(card.querySelector('.case-section'),null,'retired local case library leaves no empty panel');
 await byText(card,'← 返回刚才的检查项').onclick();assert.equal(host.hidden,false);assert.equal(host.scrollTop,155);assert.equal(host.querySelector('.check-results-items').scrollTop,123);assert.ok(text(host).includes('2 / 4 页'));assert.ok(text(host.querySelector('.check-panel-head')).includes('C17'));assert.equal(host.querySelectorAll('.check-requirement-group')[1].open,true);assert.ok(text(host.querySelector('.check-group-result')).includes('交接权限'));
 run("navigate('options','O1',false)");assert.ok(text(card).includes('乙方应在验收后移交账号。'));assert.ok(!/finding|clause_draft|F01|P001/.test(text(card)));
 const snapshot=JSON.stringify(report);ui.renderResults(host,report,()=>{},{reportId:'fresh'});await byText(host,'只看需关注').fire('click');assert.equal(host.querySelectorAll('.check-requirement-group').some(n=>text(n).includes('未发现问题')),false);assert.equal(JSON.stringify(report),snapshot);
 await byText(host,'显示全部').fire('click');await byText(host,'下一页').fire('click');await byText(host,'下一页').fire('click');await byText(host,'下一页').fire('click');assert.equal(host.querySelectorAll('.check-results-item').length,1);assert.ok(text(host).includes('C31'));assert.equal(byText(host,'下一页').disabled,true);
 // A failed or unresolved whole-item validation remains attention-worthy even when every independent child passed.
 for(const parentStatus of ['处理失败','信息不足','需要法务判断']){
   const parentReport={preparation:{checks:[checks[0]]},findings:[],checklist_results:[{check_id:'C01',title:checks[0].title,status:parentStatus,reason:'整项关联信息未形成可靠回应',finding_ids:[],groups:checks[0].groups.map(g=>({group_id:g.group_id,title:g.title,status:'未检出',reason:'本组已经独立核对',finding_ids:[]}))}]};
   ui.renderResults(host,parentReport,()=>{},{reportId:'parent-attention-'+parentStatus});await byText(host,'只看需关注').fire('click');
   assert.equal(host.querySelectorAll('.check-results-item').length,1,'parent '+parentStatus+' must remain in attention filter');assert.ok(text(host.querySelector('.check-panel-head')).includes('C01'));
   assert.equal(host.querySelectorAll('.check-requirement-group').filter(d=>d.getAttribute('data-group')).length,0,'passed children stay filtered, not relabelled failed');assert.ok(text(host.querySelector('.check-original')).includes('请查看标题区的整项检查说明'));
   assert.ok(text(host.querySelector('.check-panel-head')).includes(parentStatus==='处理失败'?'这项检查还没完成':'整项关联信息未形成可靠回应'));assert.ok(text(host.querySelector('.check-panel-head')).includes(parentStatus==='处理失败'?'尚未完成检查':'待确认'));
   await byText(host,'显示全部').fire('click');assert.equal(host.querySelectorAll('.check-requirement-group').filter(d=>d.getAttribute('data-group')).length,2);assert.ok(host.querySelectorAll('.check-requirement-group').filter(d=>d.getAttribute('data-group')).every(d=>text(d).includes('未发现问题')),'parent validation failure must not become child failure');
 }
 const legacy={preparation:{checks:[{id:'C01',title:'旧检查',requirement:'### 身份\n原身份要求\n### 权限\n原权限要求'}]},findings:[],checklist_results:[{check_id:'C01',title:'旧检查',status:'未检出',reason:'旧整项通过',finding_ids:[]}]};ui.renderResults(host,legacy);assert.equal(host.querySelectorAll('.check-requirement-group').filter(d=>d.getAttribute('data-group')).every(d=>text(d).includes('尚无分组结果')),true);assert.equal(host.querySelectorAll('.check-requirement-group').some(d=>text(d).includes('未发现问题')),false);
 assert.ok(text(host.querySelector('.check-panel-head')).includes('旧整项通过'),'legacy report still shows its only whole-item result');
 const longReport=JSON.parse(JSON.stringify(report));longReport.checklist_results[0].reason='完整的整项检查解释。'.repeat(35);ui.renderResults(host,longReport);assert.equal(host.querySelector('.check-overall-result').tagName,'DETAILS');assert.equal(host.querySelector('.check-overall-result').open,false,'long grouped result folds in title area');assert.ok(text(host.querySelector('.check-overall-result')).includes('完整的整项检查解释。'));
 const longLegacy=JSON.parse(JSON.stringify(legacy));longLegacy.checklist_results[0].reason='只有这个旧整项结论。'.repeat(35);ui.renderResults(host,longLegacy);assert.equal(host.querySelector('.check-overall-result').open,true,'legacy only result is not hidden by default');
 legacy.checklist_results[0].status='处理失败';ui.renderResults(host,legacy);assert.ok(text(host).includes('尚未完成检查'));assert.ok(!text(host).includes('未发现问题'));
 const saved=path.join(__dirname,'../runtime/review-jobs/job-0dc02e08748b469fab3e71fe9163f4d3/result.json');
 if(fs.existsSync(saved)){const actual=JSON.parse(fs.readFileSync(saved,'utf8')).report;ui.renderResults(host,actual,()=>{},{reportId:'actual-failed'});assert.equal(ui.resultRows(actual).length,31);const row=host.querySelectorAll('.check-results-item').find(n=>text(n).includes('C02'));await row.fire('click');assert.ok(text(host.querySelector('.check-panel-head')).includes('尚未完成检查'));assert.equal(host.querySelectorAll('.check-requirement-group').filter(d=>d.getAttribute('data-group')).every(d=>text(d).includes('尚无分组结果')),true);const raw=JSON.stringify(actual);context.testBundle={...bundle,id:'actual-failed',report:actual};run("bundle=testBundle;mode='findings';selected=bundle.report.findings.find(f=>f.status==='处理失败').id;renderAll(false)");assert.ok(text(card).includes('尚未完成检查'));for(const f of actual.findings){context.testFinding=f.id;run("navigate('findings',testFinding,false)");assert.ok(!/(?<![A-Za-z0-9_])(?:P\d+|F\d+|finding|clause_draft)(?![A-Za-z0-9_])/.test(text(card)),f.id+' still exposes an internal reference');}for(const o of actual.options){context.testOption=o.id;run("navigate('options',testOption,false)");assert.ok(!/(?<![A-Za-z0-9_])(?:P\d+|F\d+|finding|clause_draft)(?![A-Za-z0-9_])/.test(text(card)),o.id+' still exposes an internal reference');}assert.equal(JSON.stringify(actual),raw);console.log('actual saved failed report: 31 frozen legacy checks and failed C02 not green passed');}
 // IDs-only model refs are materialized by the program before display: a complete paragraph still locates and highlights.
 const completeFinding={...report.findings[0],issue_kind:'法律',evidence_status:'资料不足',refs:[{id:'P001',quote:'账号由乙方保管',quote_source:'original_paragraph'}],source_ids:['SRC1'],evidence:{origin:'frozen_source_records',source_state:'verified_snapshot',legal_applicability:'not_assessed',sources:[{id:'SRC1',snapshot_verified:true,verification_state:'verified_snapshot',checked_at:'2026-09-20'}]}};
 const completeBundle={...bundle,id:'program-owned',sources:[{id:'SRC1',title:'示例依据',url:'https://example.test/law',verified:false,checked_at:'2026-10-04'}],report:{...report,findings:[completeFinding]}};
 context.testBundle=completeBundle;const frozen=JSON.stringify(completeBundle);run("bundle=testBundle;mode='findings';selected='F01';renderAll(false)");
 const locationLink=card.querySelector('.source-location-item');assert.ok(locationLink);assert.ok(!text(locationLink).includes('↗'));locationLink.onclick();
 assert.ok(document.elements['paper'].querySelectorAll('mark').some(n=>n.textContent==='账号由乙方保管'&&n.className.includes('focused')));
 const sourceLink=card.querySelectorAll('a').find(n=>n.href==='https://example.test/law');assert.ok(sourceLink);assert.ok(sourceLink.textContent.includes('已核对原文 · 核对日期 2026-09-20'));assert.ok(!sourceLink.textContent.includes('2026-10-04'));
 assert.ok(!text(card).includes('依据待核验'));assert.equal(JSON.stringify(completeBundle),frozen);
 // Options use their program-derived evidence, never the raw verified flag.
 for(const [kind,raw] of [
   ['missing-text',{verified:true,checked_at:'2026-09-20'}],
   ['missing-date',{verified:true,text:'资料原文'}],
   ['future-date',{verified:true,text:'资料原文',checked_at:'2099-01-01'}]
 ]){
   const source={id:'SRC1',title:'示例依据',url:'https://example.test/law',...raw};
   const option={...report.options[0],source_ids:['SRC1'],evidence:{origin:'frozen_source_records',source_state:'unverified',legal_applicability:'not_assessed',sources:[{id:'SRC1',snapshot_verified:false,verification_state:'unverified',checked_at:raw.checked_at}]}};
   context.testBundle={...completeBundle,id:'option-'+kind,sources:[source],report:{...completeBundle.report,options:[option]}};
   run("bundle=testBundle;mode='options';selected='O1';renderAll(false)");
   let link=card.querySelectorAll('a').find(n=>n.href==='https://example.test/law');assert.ok(link);assert.ok(link.textContent.includes('原文待核对'));assert.ok(!/已核对原文|核对日期|2099/.test(link.textContent),kind);
   delete option.evidence;run("renderAll(false)");link=card.querySelectorAll('a').find(n=>n.href==='https://example.test/law');assert.ok(link.textContent.includes('原文核对情况未记录'));assert.ok(!/已核对原文|核对日期|2099/.test(link.textContent),kind+' legacy');
 }
 const verifiedOption={...report.options[0],source_ids:['SRC1'],evidence:completeFinding.evidence};context.testBundle={...completeBundle,id:'verified-option',report:{...completeBundle.report,options:[verifiedOption]}};
 run("bundle=testBundle;mode='options';selected='O1';renderAll(false)");assert.ok(card.querySelectorAll('a').some(n=>n.textContent.includes('已核对原文 · 核对日期 2026-09-20')));
 console.log('checklist_workspace_ui: option-derived provenance, missing text/date and future-date raw flags stay unverified passed');
 const failedFinding={...completeFinding,status:'处理失败',risk:null,title:'模型结果校验失败',rule_id:'C01',reason:'evidence_status invalid enum',model_reason:'模型原理由',validation_errors:['quote id invalid P999'],suggestion:'schema debug',clause_draft:'invalid internal draft'};
 const failedReport={...report,findings:[failedFinding],checklist_results:[{check_id:'C01',title:checks[0].title,status:'处理失败',reason:'source_ids invalid reference',finding_ids:['F01'],groups:checks[0].groups.map(g=>({group_id:g.group_id,status:'处理失败',reason:'quote validation failed',finding_ids:['F01']}))}]};
 context.testBundle={...bundle,id:'diagnostic-failure',report:failedReport};run("bundle=testBundle;mode='findings';selected='F01';renderAll(false)");
 assert.ok(text(card).includes(checks[0].title));assert.ok(text(card).includes('这项检查尚未完成'));assert.equal(card.querySelector('.review-actionbar'),null);
 assert.ok(!/evidence_status|invalid|schema|validation|模型结果校验失败|模型原理由/.test(text(card)));
 ui.renderResults(host,failedReport,()=>{},{reportId:'diagnostic-failure'});assert.ok(text(host).includes('尚未完成检查'));assert.ok(!/source_ids|quote validation|invalid|模型结果校验失败/.test(text(host)));
 const paired={...bundle,id:'same-label',paragraphs:[...bundle.paragraphs,{...bundle.paragraphs[0],id:'P002',text:'乙方负责权限配置'}],report:{...report,findings:[{...report.findings[0],refs:[...report.findings[0].refs,{id:'P002',quote:'权限'}]}]}};
 context.testBundle=paired;run("bundle=testBundle;mode='findings';selected='F01';renderAll(false)");assert.equal(card.querySelectorAll('.source-location-item').length,2);assert.equal(card.querySelectorAll('.source-location-excerpt').length,2);assert.ok(text(card).includes('乙方负责权限配置'));assert.ok(!text(card.querySelector('.source-location-links')).includes('↗'));
 paired.report.options=[{id:'O1',title:'验收安排',finding_ids:[],relationship:'与O2、O99一并考虑。'},{id:'O2',title:'付款安排',finding_ids:[],steps:[]}];
 run("navigate('options','O1',false)");assert.equal(card.querySelector('h2').textContent,'O1 验收安排');assert.ok(text(document.elements['issue-list']).includes('O2 付款安排'));assert.ok(text(card).includes('O99（本报告无对应方案）'));const optionRef=card.querySelector('.option-reference');assert.equal(optionRef.textContent,'O2 付款安排');optionRef.onclick();assert.equal(card.querySelector('h2').textContent,'O2 付款安排');
 const inlineParagraphs=[{id:'P001',text:'尾款应在验收后支付。',location:'2. 开发费用 / 2.3.',numbering_label:'2.3.'},{id:'P002',text:'本合同包含软件确认书附件。',location:'14. 附则 / 14.3.',numbering_label:'14.3.'},{id:'P003',text:'其他付款条件。',location:'2. 开发费用 / 2.3.',numbering_label:'2.3.'}];
 const inlineReport={...report,findings:[{...report.findings[0],refs:[{id:'P001',quote:inlineParagraphs[0].text},{id:'P002',quote:inlineParagraphs[1].text}]}],checklist_results:[{...report.checklist_results[0],reason:'账号未约定交接（F01）。',finding_ids:['F01','F01'],groups:[{...report.checklist_results[0].groups[0],reason:'P001引用附件，但P002只列一份附件。',finding_ids:['F01']}]}]};
 const beforeInline=JSON.stringify(inlineReport),inlineFindings=[],inlineSources=[];
 const inlineOptions={reportId:'inline-links',textContext:{paragraphs:inlineParagraphs,locations:Object.fromEntries(inlineParagraphs.map(p=>[p.id,p.location]))},onParagraph:(id,ids)=>inlineSources.push([id,ids])};
 ui.renderResults(host,inlineReport,id=>inlineFindings.push(id),inlineOptions);
 const overallInline=host.querySelector('.check-overall-result');
 assert.equal(overallInline.querySelectorAll('.check-inline-reference').length,1);assert.equal(overallInline.querySelectorAll('.checklist-finding-link').length,0,'opinion already linked inline is not listed twice');
 await overallInline.querySelector('.check-inline-reference').click();assert.deepEqual(inlineFindings,['F01']);
 let inlineGroup=host.querySelector('.check-group-result');
 assert.deepEqual(inlineGroup.querySelectorAll('.check-inline-reference').map(b=>b.textContent),['第2.3条','第14.3条']);assert.ok(!text(inlineGroup).includes('原文“'));assert.ok(!text(inlineGroup).includes(' · '));
 await inlineGroup.querySelectorAll('.check-inline-reference')[1].click();assert.equal(inlineSources[0][0],'P002');assert.deepEqual(Array.from(inlineSources[0][1]),['F01']);
 assert.equal(JSON.stringify(inlineReport),beforeInline,'rendering never rewrites saved model text');
 inlineReport.checklist_results[0].groups[0].reason='P001-P003另有说明，`P002`和https://example.test/P002保持原样。';
 ui.renderResults(host,inlineReport,()=>{},inlineOptions);inlineGroup=host.querySelector('.check-group-result');
 assert.equal(inlineGroup.querySelectorAll('.check-inline-reference').length,1,'same-clause range is one link; code and URLs are literal');
 assert.ok(text(inlineGroup).includes('`P002`'));assert.ok(text(inlineGroup).includes('https://example.test/P002'));
 inlineReport.checklist_results[0].groups[0].reason='P001-P999暂无法完整定位。';ui.renderResults(host,inlineReport,()=>{},inlineOptions);assert.equal(host.querySelector('.check-group-result').querySelectorAll('.check-inline-reference').length,0,'unknown endpoint does not get a misleading partial link');
 inlineReport.checklist_results[0].groups[0].reason='P002只列一份附件。';context.testBundle={...bundle,id:'inline-app-route',paragraphs:inlineParagraphs,report:inlineReport};context.testScroll=[];
 run("bundle=testBundle;mode='checklists';selected=null;var oldScrollParagraph=scrollParagraph;scrollParagraph=id=>testScroll.push(id);renderAll(false)");
 await host.querySelector('.check-group-result').querySelector('.check-inline-reference').click();
 assert.equal(run('mode'),'findings');assert.equal(context.testScroll[0],'P002');assert.ok(card.querySelector('.checklist-return'));card.querySelector('.checklist-return').onclick();assert.equal(run('mode'),'checklists');run('scrollParagraph=oldScrollParagraph');
 console.log('checklist_workspace_ui: inline opinion deduplication, clause-only source links, protected literals and return navigation passed');
 console.log('checklist_workspace_ui: structured duplicate locations and O-number title/list/reference navigation passed');
 console.log('checklist_workspace_ui: program-derived full-paragraph anchors, frozen registry date and hidden internal diagnostics passed');
 console.log('checklist_workspace_ui: real 31 rows, 10/10/10/1 pagination, independent modes, grouped truthful lights, attention, collapsed headings, finding/reentry state, numbered document links, real option drafts, legacy absence/failure passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
