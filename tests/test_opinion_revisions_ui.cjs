'use strict';
// Local API fixture only. It does not generate or validate a legal opinion.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this.className='';this.textContent='';this.open=false;this.disabled=false;this.events={};this.classList={toggle(){}};this.dataset={};}
  append(...nodes){this.children.push(...nodes);}
  // Only the selector the panel uses: open/closed version sections kept across re-renders.
  querySelectorAll(selector){
    assert.equal(selector,'details[data-key]');
    const found=[];const walk=node=>{for(const child of node.children||[]){if(child.tag==='details'&&child.dataset?.key)found.push(child);walk(child);}};
    walk(this);return found;
  }
  replaceChildren(...nodes){this.children=[...nodes];}
  setAttribute(){}
  focus(){}
  showModal(){this.open=true;}
  close(){this.open=false;this.events.close?.({target:this});}
  addEventListener(name,fn){this.events[name]=fn;}
}
const elements=new Map();
for(const id of ['opinion-revision-dialog','opinion-revision-title','opinion-revision-close','opinion-revision-body'])
  elements.set(id,new Element());
const document={createElement:tag=>new Element(tag),getElementById:id=>elements.get(id)||null};
const find=(root,predicate)=>{if(predicate(root))return root;for(const child of root.children){const result=find(child,predicate);if(result)return result;}return null;};
let state={
  revision:0,opinion_revision:0,original_finding:{id:'F01',title:'原意见',risk:'中',status:'发现问题',reason:'旧理由'},
  current_finding:{id:'F01',title:'原意见',risk:'中',status:'发现问题',reason:'旧理由'},
  candidates:[],accepted_candidate_id:null,pending_candidate_id:null,
  context:{discussion_revision:2,delivery_revision:4,locations:{P001:'第一条 / 1.1'},sources:[{id:'SRC1',title:'示例法规',url:'https://example.test/source',verified:false}]}
};
const calls=[];let accepted=0,polls=0;
const clone=value=>JSON.parse(JSON.stringify(value));
const ctx={document,URL,window:{APIProfiles:{active:()=>({provider:'DeepSeek',config:{base_url:'https://api.deepseek.com',model:'fixture-model',api_key:'fixture-key'}}),hasSaved:()=>true},
  DiscussionUI:{modelConfig:()=>({provider:'DeepSeek',config:{base_url:'https://api.deepseek.com',model:'selected-model',api_key:'fixture-key'}})}},
  crypto:{randomUUID:()=> 'fixture-request-id'},setTimeout:()=>{polls++;return 1;},clearTimeout(){},
  fetch:async(url,options={})=>{
    const input=options.body?JSON.parse(options.body):null;calls.push({url,method:options.method||'GET',input});
    if(options.method==='POST'&&url.endsWith('/accept')){
      assert.equal(input.candidate_id,'candidate-1');
      assert.equal(input.session_id,'session-1');
      assert.equal(input.expected_revision,state.revision);
      assert.equal(input.expected_discussion_revision,2);
      assert.equal(input.expected_delivery_revision,4);
      state.accepted_candidate_id='candidate-1';state.opinion_revision++;state.revision++;
      state.current_finding=clone(state.candidates[0].finding);
    }else if(options.method==='POST'){
      assert.equal(input.expected_revision,0);
      assert.equal(input.expected_discussion_revision,2);
      assert.equal(input.expected_delivery_revision,4);
      assert.equal(input.expected_opinion_revision,0);
      assert.equal(input.request_id,'fixture-request-id');
      assert.equal(input.session_id,'session-1');
      assert.equal(input.consent,true);
      assert.equal(input.config.model,'selected-model');
      state.pending_candidate_id='candidate-1';state.revision++;
      state.candidates=[{id:'candidate-1',request_id:input.request_id,status:'running',base_opinion_revision:0}];
    }
    const result=clone(state);
    if(url.includes('session_id=session-2')){
      result.candidates=[];result.pending_candidate_id=null;
      result.accepted_candidate_session_id='session-1';
      result.global_pending_candidate_id='candidate-in-other-session';
      result.pending_candidate_session_id='session-1';
    }
    return {ok:true,json:async()=>result};
  }};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/readable_text.js'),'utf8'),ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/opinion_revisions.js'),'utf8'),ctx);
const ui=ctx.window.OpinionRevisions;
ui.configure({onAccepted:async()=>{accepted++;}});
const body=elements.get('opinion-revision-body');
async function flush(){for(let i=0;i<8;i++)await Promise.resolve();}
(async()=>{
  await ui.open({reportId:'report-1',findingId:'F01',title:'原意见',sessionId:'session-1',sessionLabel:'讨论1'},null);
  assert.equal(elements.get('opinion-revision-dialog').open,true);
  const generate=find(body,n=>n.textContent==='生成新版意见'&&n.tag==='button');
  assert.ok(generate);
  await generate.onclick();await flush();
  assert.equal(state.pending_candidate_id,'candidate-1');
  assert.equal(calls.filter(c=>c.method==='POST').length,1);
  assert.equal(find(body,n=>n.textContent==='生成中…')?.disabled,true);
  state.pending_candidate_id=null;
  state.candidates[0]={...state.candidates[0],status:'completed',action:'revise',
    finding:{id:'F01',title:'新版意见',risk:'高',status:'发现问题',reason:'新理由',suggestion:'与对方重新约定期限',
      refs:[{id:'P001',quote:'示例原文'}],source_ids:['SRC1']},
    change_summary:'讨论后提高风险等级',related_finding_ids:['F02']};
  await ui.refresh();
  assert.ok(find(body,n=>n.textContent==='关联意见需复核'));
  assert.ok(find(body,n=>n.textContent==='与对方重新约定期限'));
  assert.ok(find(body,n=>n.textContent==='第一条 / 1.1\n示例原文'));
  // 模型写的理由里引用段落、意见与依据的内部编号，界面上要翻成条款号和名称
  state.candidates[0].finding.reason='P001 与 F02 衔接，见 SRC1';state.candidates[0].change_summary='按 P001 调整';await ui.refresh();
  const reason=find(body,n=>n.tag==='p'&&n.textContent.includes('衔接'));
  assert.ok(reason&&!/\bP001\b|\bF02\b|\bSRC1\b/.test(reason.textContent),reason?.textContent);
  assert.ok(reason.textContent.includes('第1.1条')&&reason.textContent.includes('示例法规'),reason.textContent);
  assert.ok(find(body,n=>n.tag==='p'&&n.textContent==='按 第1.1条 调整'));
  state.candidates[0].finding.reason='新理由';state.candidates[0].change_summary='讨论后提高风险等级';await ui.refresh();
  const source=find(body,n=>n.textContent==='示例法规（原文核对情况未记录）');assert.equal(source.href,'https://example.test/source');
  for(const raw of [{verified:true,checked_at:'2026-09-20'},{verified:true,text:'原文'},{verified:true,text:'原文',checked_at:'2099-01-01'}]){
    state.context.sources=[{id:'SRC1',title:'示例法规',url:'https://example.test/source',...raw}];await ui.refresh();
    const fallback=find(body,n=>n.tag==='a'&&n.href==='https://example.test/source');assert.ok(fallback.textContent.includes('原文核对情况未记录'));assert.ok(!/已核对原文|核对日期|2099/.test(fallback.textContent));
  }
  state.candidates[0].finding.evidence={origin:'frozen_source_records',sources:[{id:'SRC1',snapshot_verified:true,verification_state:'verified_snapshot',checked_at:'2026-09-20'}]};await ui.refresh();
  assert.ok(find(body,n=>n.tag==='a'&&n.textContent.includes('已核对原文 · 核对日期 2026-09-20')));
  const accept=find(body,n=>n.textContent==='采用这版意见'&&n.tag==='button');
  assert.ok(accept&&!accept.disabled);
  await accept.onclick();
  assert.equal(accepted,1);
  assert.equal(state.current_finding.title,'新版意见');
  assert.equal(calls.filter(c=>c.method==='POST'&&c.url.endsWith('/accept')).length,1);
  assert.equal(find(body,n=>n.textContent==='已采用')?.textContent,'已采用');
  assert.equal(find(body,n=>n.textContent==='已采用这版意见。合同改稿与批注没有自动改变。')?.textContent,'已采用这版意见。合同改稿与批注没有自动改变。');
  ui.close();assert.equal(elements.get('opinion-revision-dialog').open,false);
  await ui.open({reportId:'report-1',findingId:'F01',title:'原意见',sessionId:'session-2',sessionLabel:'讨论2'},null);
  assert.equal(find(body,n=>n.textContent==='生成新版意见')?.disabled,true,'other session pending blocks generation');
  assert.ok(polls>=2,'other session pending keeps polling until completion');
  assert.ok(find(body,n=>n.textContent==='当前意见（采用自其他讨论）'));
  assert.ok(calls.some(c=>c.url.includes('session_id=session-2')));
  ui.close();
  console.log('opinion_revisions_ui: saved profile payload, pending lock, compare, accept, preserved candidate passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
