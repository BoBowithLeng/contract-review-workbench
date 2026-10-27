'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
class Element{
  constructor(tag,doc){this.tagName=tag.toUpperCase();this.doc=doc;this.children=[];this.parentNode=null;this.listeners={};this.attributes={};this.textContent='';this.className='';this.value='';this.disabled=false;this.checked=false;this.hidden=false;this.dataset={};this.style={};this.classList={toggle(){},add(){},remove(){}};}
  append(...nodes){for(const n of nodes){n.parentNode=this;this.children.push(n);}}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);}
  async fire(type){for(const fn of this.listeners[type]||[])await fn({preventDefault(){},target:this});}
  setAttribute(k,v){this.attributes[k]=v;}
  focus(){this.doc.activeElement=this;}
  querySelector(s){return this.querySelectorAll(s)[0]||null;}
  querySelectorAll(s){return all(this).slice(1).filter(n=>s.startsWith('.')?n.className.split(' ').includes(s.slice(1)):n.tagName===s.toUpperCase());}
}
const all=n=>[n,...n.children.flatMap(all)],text=n=>all(n).map(x=>x.textContent).join('\n');
const byName=(n,name)=>all(n).find(x=>x.name===name),byText=(n,value)=>all(n).find(x=>x.textContent===value);
const document={elements:{},createElement(tag){return new Element(tag,this);},getElementById(id){return this.elements[id]||null;},querySelectorAll(){return[];}};document.body=document.createElement('body');
const host=document.elements['information-gate']=document.createElement('section');
let currentGate,fail=false,conflict=false,choice={engine_key:'compatible_api',provider:'chosen',config:{model:'chosen-model',base_url:'https://chosen.example',api_key:'fixture'}};
const calls=[],published=[],timers=[];
const response=(status,data)=>({ok:status<400,status,json:async()=>data});
const context={window:{ReviewModelSelector:{mount(){return{ready:Promise.resolve(),value:()=>choice};}}},document,console,setTimeout(fn){timers.push(fn);return timers.length;},clearTimeout(){},fetch:async(url,options={})=>{
  calls.push({url,...options});
  if(url==='/api/review-capabilities')return response(200,{engines:[{key:'compatible_api',available:true}]});
  if(options.method==='POST'){
    if(fail)throw new Error('fixture unavailable');
    if(conflict)return response(409,{error:'revision conflict'});
    currentGate={...currentGate,status:JSON.parse(options.body).consent?'running':'ready',revision:currentGate.revision+1};return response(200,{information_gate:currentGate});
  }
  return response(200,{information_gate:currentGate});
}};vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/information_gate.js'),'utf8'),context);
const ui=context.window.InformationGateUI;ui.configure({onReady:async id=>published.push(id)});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const gate=(status='needs_response')=>({enabled:true,status,revision:0,facts_revision:4,requires_model:true,questions:[{id:'q1',question:'实际付款安排？',reason:'影响结算',status:'unanswered',answer:'',finding_ids:['F1','F2']},{id:'q2',question:'交付时间？',status:'unknown',answer:''}]});
const postCalls=()=>calls.filter(c=>c.method==='POST');
(async()=>{
 currentGate=gate();ui.open('all-unknown',currentGate);await tick();
 assert.equal(byName(host,'gate_unknown_q2').checked,false,'no prechecked unknown on first response');
 await host.querySelector('form').fire('submit');assert.equal(postCalls().length,0);assert.ok(text(host).includes('请逐条回答'));
 await byText(host,'本次全部暂不能提供').fire('click');assert.equal(postCalls().length,0,'bulk is explicit choice, not implicit submit');
 await host.querySelector('form').fire('submit');assert.deepEqual(published,['all-unknown']);let sent=JSON.parse(postCalls().at(-1).body);assert.equal(sent.answers.every(a=>a.status==='unknown'&&a.answer===''),true);assert.equal('consent'in sent,false);assert.equal('config'in sent,false);
 currentGate=gate();ui.open('answered',currentGate);await tick();byName(host,'gate_answer_q1').value='分三次支付';await byName(host,'gate_answer_q1').fire('input');byName(host,'gate_unknown_q2').checked=true;await byName(host,'gate_unknown_q2').fire('change');
 const prior=postCalls().length;await host.querySelector('form').fire('submit');assert.equal(postCalls().length,prior);assert.ok(text(host).includes('勾选同意'));
 byName(host,'gate_consent').checked=true;fail=true;await host.querySelector('form').fire('submit');assert.equal(byName(host,'gate_answer_q1').value,'分三次支付');assert.ok(text(host).includes('fixture unavailable'));
 fail=false;await host.querySelector('form').fire('submit');sent=JSON.parse(postCalls().at(-1).body);assert.equal(sent.config.model,'chosen-model');assert.equal(sent.engine_key,'compatible_api');assert.equal(sent.answers[0].answer,'分三次支付');assert.equal(text(host).includes('完成后可以打开报告'),true);assert.equal(host.querySelector('form'),null);
 currentGate={...currentGate,status:'ready'};await timers.at(-1)();assert.equal(published.at(-1),'answered');
 currentGate=gate();ui.open('hidden-answer',currentGate);await tick();byName(host,'gate_answer_q1').value='不应计费的隐藏答案';await byName(host,'gate_answer_q1').fire('input');await byText(host,'本次全部暂不能提供').fire('click');await host.querySelector('form').fire('submit');sent=JSON.parse(postCalls().at(-1).body);assert.equal(sent.answers.every(a=>!a.answer),true);assert.equal('config'in sent,false);
 currentGate={...gate('failed'),revision:1,error:'一次调用失败',questions:[{...gate().questions[0],status:'answered',answer:'已保存答案'},{...gate().questions[1],status:'unknown'}]};ui.open('retry',currentGate);await tick();assert.equal(byName(host,'gate_answer_q1').disabled,true);assert.ok(text(host).includes('调整未完成'));await host.querySelector('form').fire('submit');assert.ok(text(host).includes('勾选同意'));byName(host,'gate_consent').checked=true;await host.querySelector('form').fire('submit');sent=JSON.parse(postCalls().at(-1).body);assert.equal(sent.retry,true);assert.equal('answers'in sent,false);assert.equal(sent.config.model,'chosen-model');assert.ok(text(host).includes('完成后可以打开报告'));
 currentGate=gate();ui.open('conflict',currentGate);await tick();byName(host,'gate_answer_q1').value='保留冲突输入';await byName(host,'gate_answer_q1').fire('input');byName(host,'gate_unknown_q2').checked=true;await byName(host,'gate_unknown_q2').fire('change');byName(host,'gate_consent').checked=true;conflict=true;await host.querySelector('form').fire('submit');assert.equal(byName(host,'gate_answer_q1').value,'保留冲突输入');conflict=false;currentGate.revision=3;await byText(host,'重新读取，保留当前输入').fire('click');assert.equal(byName(host,'gate_answer_q1').value,'保留冲突输入');
 const count=calls.length;ui.open('legacy',{enabled:false,status:'ready'});await tick();assert.equal(published.at(-1),'legacy');assert.equal(calls.length,count);ui.close();
 console.log('information_gate_ui: required explicit choices, conscious all unknown/no call, consent+selected model, hidden text/no billing, draft errors/conflict, running polling, frozen retry and legacy passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
