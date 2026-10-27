'use strict';
// Local state fixture; no provider calls or production records.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
class Element{
  constructor(tag='div'){this.tag=tag;this.tagName=tag.toUpperCase();this.children=[];this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.style={};this.attributes={};this.inert=false;
    this.scrollHeight=200;this.scrollTop=0;this.clientHeight=100;
    const classes=new Set();this.classList={add:name=>classes.add(name),remove:name=>classes.delete(name),toggle:(name,on)=>on?classes.add(name):classes.delete(name),contains:name=>classes.has(name)};}
  append(...nodes){this.children.push(...nodes);for(const node of nodes)node.parentElement=this;}
  replaceChildren(...nodes){this.children=[...nodes];}
  add(node){this.children.push(node);}
  querySelectorAll(){return [];}
  setAttribute(name,value){this.attributes[name]=String(value);}
  removeAttribute(name){delete this.attributes[name];}
  focus(){}
  addEventListener(){}
  get selectedOptions(){return this.children.filter(child=>child.value===this.value);}
}
const ids=['discussion-pane','rail','discussion-input','discussion-topic','discussion-context-note','discussion-error',
  'discussion-log','discussion-send','discussion-stop','discussion-model','discussion-search-control','discussion-web-search',
  'discussion-generate-opinion','discussion-opinion-history','discussion-session','discussion-new-session',
  'discussion-back','discussion-settings','discussion-form','about-product','about-dialog','about-close',
  'reader-workspace','document-area','discussion-expand','discussion-model-picker','discussion-model-search','discussion-model-list',
  'discussion-model-status','discussion-model-close'];
const elements=new Map(ids.map(id=>[id,new Element()]));
const body=new Element('body'),background=new Element('header'),settingsDialog=new Element('dialog');body.append(background,elements.get('reader-workspace'),settingsDialog);body.style.overflow='auto';
let nativeModal=false;const windowHandlers={};
const document={body,getElementById:id=>elements.get(id),createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text}),querySelector:()=>nativeModal?settingsDialog:null};
const $=id=>elements.get(id);
const sessions=[
  {id:'s1',created_at:'2026-09-24T00:00:00Z',turn_count:1,last_at:'2026-09-24T00:00:00Z'},
  {id:'s2',created_at:'2026-09-25T00:00:00Z',turn_count:0,last_at:'2026-09-25T00:00:00Z'}
];
const turns={s1:[{id:'t1',status:'completed',user:'旧问题',assistant:'旧回答'}],s2:[]};
let revision=2,sessionRevision=2,requests=[],holdOld=null,newCount=0;
const saved=new Map();
const localStorage={getItem:key=>saved.get(key)||null,setItem:(key,value)=>saved.set(key,String(value))};
const providers=[{id:'DeepSeek',label:'DeepSeek'},{id:'Other',label:'Other'}];
const configs={DeepSeek:{base_url:'https://api.deepseek.com',api_key:'fixture',model:'fixture'},Other:{base_url:'https://api.example.test/v1',api_key:'fixture-B',model:'other-default'}};
let sanitized=0;
const snapshot=id=>({session_id:id,active_session_id:sessions.at(-1).id,sessions_revision:sessionRevision,sessions:sessions.map(x=>({...x})),
  revision,opinion_revision:1,pending_turn_id:null,turns:turns[id].map(x=>({...x})),
  context:{delivery_revision:3,opinion_revision:1,document_name:'测试合同',location:'第一条'}});
const request=async(url,options)=>{
  const method=options?.method||'GET',body=options?.body?JSON.parse(options.body):null;
  requests.push({url,method,body});
  if(method==='GET'){
    const id=new URL(url,'http://local').searchParams.get('session_id')||sessions.at(-1).id;
    return snapshot(id);
  }
  if(url.endsWith('/sessions')){
    assert.equal(body.expected_sessions_revision,sessionRevision);
    assert.equal(body.expected_delivery_revision,3);
    assert.equal(body.expected_opinion_revision,1);
    assert.ok(body.request_id);
    const id='s'+(++newCount+2);sessions.push({id,created_at:'2026-09-26T00:00:00Z',turn_count:0,last_at:'2026-09-26T00:00:00Z'});turns[id]=[];sessionRevision++;
    return snapshot(id);
  }
  assert.equal(body.session_id,new URL(url,'http://local').searchParams.get('session_id')||body.session_id);
  if(body.session_id==='s1'&&body.message==='延迟问题'){
    return new Promise(resolve=>{holdOld=()=>{turns.s1.push({id:'late',request_id:body.request_id,status:'completed',user:body.message,assistant:'延迟回答'});resolve(snapshot('s1'));};});
  }
  turns[body.session_id].push({id:'new',request_id:body.request_id,status:'completed',user:body.message,assistant:'夹具回答'});
  revision++;return snapshot(body.session_id);
};
const ctx={document,window:{addEventListener(name,handler){windowHandlers[name]=handler;},scrollY:70,scrollTo(){},OpinionRevisions:{close(){},open(){}},ModelSettings:{open(){}},
  marked:require('../reader_ui/vendor/marked.umd.js'),DOMPurify:{sanitize:(html,options)=>{sanitized++;assert.ok(options.ALLOWED_TAGS.includes('table'));return html;}},
  ReaderText:{humanize:text=>text.replace('P048','第四条')}},
  APIProfiles:{active:()=>({provider:'DeepSeek',config:configs.DeepSeek}),hasSaved:()=>true,get:id=>configs[id],load:async()=>providers,
    catalog:async config=>config.provider==='Other'?[{id:'other-default',name:'Other Default'},{id:'other-new',name:'Other New'}]:[{id:'fixture',name:'Fixture'}]},
  URL,localStorage,crypto:{randomUUID:()=> 'request-'+requests.length},setTimeout:()=>1,clearTimeout(){},request};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/discussion.js'),'utf8'),ctx);
const ui=ctx.window.DiscussionUI;
(async()=>{
  await ui.open({id:'report-1'},{id:'F01',title:'当前意见'});
  assert.equal($('discussion-model').textContent,'fixture');assert.ok(!$('discussion-model').textContent.includes('deepseek.com'));
  assert.equal(ui.current().sessionId,'s2','reopening selects latest session without creating one');
  assert.equal(requests.filter(x=>x.method==='POST').length,0);
  $('discussion-input').value='新轮未发草稿';$('discussion-input').oninput();
  $('discussion-session').value='s1';await $('discussion-session').onchange({target:$('discussion-session')});
  assert.equal(ui.current().sessionId,'s1');
  $('discussion-input').value='旧轮未发草稿';$('discussion-input').oninput();
  $('discussion-session').value='s2';await $('discussion-session').onchange({target:$('discussion-session')});
  assert.equal($('discussion-input').value,'新轮未发草稿');
  $('discussion-session').value='s1';await $('discussion-session').onchange({target:$('discussion-session')});
  assert.equal($('discussion-input').value,'旧轮未发草稿');
  $('discussion-input').value='延迟问题';
  const pending=$('discussion-form').onsubmit({preventDefault(){}});
  await Promise.resolve();assert.ok(holdOld);
  $('discussion-session').value='s2';await $('discussion-session').onchange({target:$('discussion-session')});
  holdOld();await pending;
  assert.equal(ui.current().sessionId,'s2');
  assert.equal($('discussion-input').value,'新轮未发草稿','late old reply cannot clear new draft');
  await $('discussion-new-session').onclick();
  assert.equal(ui.current().sessionId,'s3');
  assert.equal($('discussion-input').value,'');
  assert.equal(requests.filter(x=>x.url.endsWith('/sessions')).length,1);
  assert.equal(requests.filter(x=>x.method==='POST'&&!x.url.endsWith('/sessions'))[0].body.session_id,'s1');
  $('discussion-web-search').checked=true;$('discussion-web-search').onchange();
  assert.equal(saved.get('contract-review-web-search-v1'),'true');
  await $('discussion-model').onclick();
  const find=(root,text)=>root.textContent.includes(text)?root:root.children.map(child=>find(child,text)).find(Boolean);
  const other=find($('discussion-model-list'),'other-new');assert.ok(other);assert.equal(other.textContent,'other-new');other.onclick();
  assert.ok(find($('discussion-model-list'),'api.deepseek.com'),'provider address is available inside model menu');
  assert.equal(ui.modelConfig().provider,'Other');assert.equal(ui.modelConfig().config.model,'other-new');
  assert.equal($('discussion-web-search').checked,true,'unsupported provider must not clear preference');
  assert.equal(saved.get('contract-review-discussion-model-v1'),JSON.stringify({provider:'Other',model:'other-new'}));
  $('discussion-input').value='全屏切换仍保留';$('document-area').scrollTop=317;$('discussion-log').scrollTop=42;
  const beforeFullscreen=requests.length;$('discussion-expand').onclick();assert.equal($('reader-workspace').classList.contains('discussion-expanded'),true);
  assert.equal($('discussion-expand').textContent,'退出全屏');assert.equal(body.style.overflow,'hidden');assert.equal(background.inert,true);assert.equal(settingsDialog.inert,false);assert.equal($('reader-workspace').attributes['aria-modal'],'true');
  nativeModal=true;windowHandlers.keydown({key:'Escape',preventDefault(){}});assert.equal($('reader-workspace').classList.contains('discussion-expanded'),true,'Esc should first belong to open native settings');nativeModal=false;
  windowHandlers.keydown({key:'Escape',preventDefault(){}});assert.equal($('reader-workspace').classList.contains('discussion-expanded'),false);assert.equal(background.inert,false);assert.equal(body.style.overflow,'auto');assert.equal($('document-area').scrollTop,317);assert.equal($('discussion-input').value,'全屏切换仍保留');assert.equal($('discussion-log').scrollTop,42);assert.equal(requests.length,beforeFullscreen);
  $('discussion-expand').onclick();
  turns.s3.push({id:'markdown',status:'completed',user:'原问题',assistant:'## 小标题 P048\n\n- [x] 已办\n- [ ] 未办\n\n|事项|结论|\n|---|---|\n|A|B|\n\n<script>alert(1)</script>'});
  await ui.refresh();
  const rendered=[];const walk=node=>{if(node.innerHTML)rendered.push(node.innerHTML);for(const child of node.children)walk(child);};walk($('discussion-log'));
  assert.ok(rendered.some(html=>html.includes('<h2>小标题 第四条</h2>')&&html.includes('<table>')&&html.includes('type="checkbox"')));
  assert.ok(rendered.every(html=>!html.includes('<script>')));assert.ok(sanitized>0);
  turns.s3[0].assistant='### 三级标题\n\n**粗体**与*斜体*、~~删线~~、`行内代码`。\n\n> 引用\n\n1. 一层\n   - 二层\n\n```js\nconst x = 1;\n```\n\n[官方](https://example.test)';
  await ui.refresh();rendered.length=0;walk($('discussion-log'));
  assert.ok(rendered.some(html=>['<h3>','<strong>','<em>','<del>','<code>','<blockquote>','<ol>','<ul>','<pre>','<a href="https://example.test"'].every(tag=>html.includes(tag))));
  ui.close();assert.equal($('reader-workspace').classList.contains('discussion-expanded'),false);
  console.log('discussion_sessions_ui: latest read, per-session drafts, stale reply isolation, explicit no-model new session passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
