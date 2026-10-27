const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
let reads=0,events=0;
let settings={revision:0,active_provider:null,profiles:[],storage:'windows-user-encrypted'};
const providers=[
 {id:'DeepSeek',label:'DeepSeek',base_url:'https://api.deepseek.com',model:'deepseek-flash'},
 {id:'硅基流动 SiliconFlow',label:'硅基流动 SiliconFlow',base_url:'https://api.siliconflow.cn/v1',model:''},
 {id:'OpenRouter',label:'OpenRouter',base_url:'https://openrouter.ai/api/v1',model:''},
 {id:'自定义兼容接口',label:'自定义兼容接口',base_url:'',model:''}
];
const ctx={window:{dispatchEvent(){events++;}},Event:class {constructor(type){this.type=type;}},
 fetch:async(url,options)=>{
   reads++;
   if(url==='/api/model-presets')return {ok:true,json:async()=>({providers})};
   if(options?.method==='POST'){
     const body=JSON.parse(options.body);assert.equal(body.expected_revision,settings.revision);
     settings.profiles=settings.profiles.filter(p=>p.provider!==body.provider);
     if(body.action==='save'){settings.profiles.push({provider:body.provider,...body.config});settings.active_provider=body.provider;}
     else if(settings.active_provider===body.provider)settings.active_provider=null;
     settings.revision++;
   }
   return {ok:true,json:async()=>JSON.parse(JSON.stringify(settings))};
 }};
const settingsFetch=ctx.fetch;
vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/api_profiles.js'),'utf8'),ctx);
(async()=>{
 const api=ctx.window.APIProfiles;
 await Promise.all([api.load(),api.load()]);assert.equal(reads,2);
 assert.equal(api.active(),null);assert.equal(api.get('DeepSeek').model,'deepseek-flash');
 api.use('DeepSeek',{base_url:providers[0].base_url,api_key:'test-A',model:'model-A'});
 assert.equal(api.get('OpenRouter').api_key,'');
 api.use('OpenRouter',{base_url:providers[2].base_url,api_key:'test-B',model:'model-B'});
 assert.equal(api.get('DeepSeek').api_key,'test-A');assert.equal(api.get('OpenRouter').api_key,'test-B');
 const copy=api.active();copy.config.api_key='modified outside';assert.equal(api.active().config.api_key,'test-B');
 assert.equal(events,3);
 await api.save('DeepSeek',{base_url:providers[0].base_url,model:'saved-model',api_key:'saved-test-key'});
 assert.equal(api.hasSaved('DeepSeek'),true);assert.equal(api.active().config.model,'saved-model');
 await api.load({reload:true});assert.equal(api.active().config.api_key,'saved-test-key');
 // Catalogs are tied to provider/address/key, coalesced in flight, and refreshable.
 const calls=[];let release;
 ctx.fetch=async(url,options)=>{
   calls.push({url,body:JSON.parse(options.body)});
   if(calls.length===1)await new Promise(resolve=>{release=resolve;});
   return {ok:true,json:async()=>({models:[{id:'model-A',name:'Model A'}]})};
 };
 const config={provider:'DeepSeek',base_url:'https://api.deepseek.com',api_key:'key-A'};
 const first=api.catalog(config),second=api.catalog(config);assert.equal(calls.length,1);release();
 const [list]=await Promise.all([first,second]);assert.equal(list[0].id,'model-A');
 list[0].id='modified';assert.equal((await api.catalog(config))[0].id,'model-A');assert.equal(calls.length,1);
 await api.catalog(config,{force:true});assert.equal(calls.length,2);
 await api.catalog({...config,api_key:'key-B'});assert.equal(calls.length,3);
 await api.catalog({...config,base_url:'https://other.example/v1'});assert.equal(calls.length,4);
 await api.catalog({...config,provider:'自定义兼容接口'});assert.equal(calls.length,5);
 assert.equal(calls[0].url,'/api/models');assert.equal(calls[0].body.config.api_key,'key-A');
 ctx.fetch=async()=>({ok:false,json:async()=>({error:'检查 API Key'})});
 await assert.rejects(api.catalog(config,{force:true}),/检查 API Key/);
 ctx.fetch=async()=>({ok:true,json:async()=>({models:[]})});
 assert.equal((await api.catalog(config,{force:true})).length,0);
 // The workflow selector shows saved/cache options before a real refresh, and keeps selection on failure.
 class Element{
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.textContent='';this.value='';this.listeners={};this.isConnected=true;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  setAttribute(){}
  addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);}
  querySelectorAll(tag){return this.children.flatMap(n=>[...(n.tagName===tag.toUpperCase()?[n]:[]),...n.querySelectorAll(tag)]);}
  fire(type){for(const fn of this.listeners[type]||[])fn({target:this});}
 }
 ctx.document={createElement:tag=>new Element(tag)};ctx.APIProfiles=api;ctx.window.addEventListener=()=>{};
 let finishCatalog;ctx.fetch=async()=>{await new Promise(resolve=>{finishCatalog=resolve;});return {ok:true,json:async()=>({models:[{id:'raw-model-id',name:'Friendly model name'}]})};};
 let choiceChanges=0,consentChecked=false;
 const workflowHost=new Element('div'),selector=ctx.window.ReviewModelSelector.mount(workflowHost,[{key:'compatible_api',available:true},{key:'codex_cli',available:true}],()=>{choiceChanges++;consentChecked=false;});
 await selector.ready;const select=workflowHost.children[0],status=workflowHost.children[1].children[0];assert.equal(selector.value().config.model,'saved-model');assert.equal(status.textContent,'正在刷新模型列表…');
 assert.equal(select.querySelectorAll('optgroup')[0].label,'DeepSeek');assert.equal(select.querySelectorAll('option')[0].textContent,'saved-model');assert.ok(select.querySelectorAll('option').some(n=>n.textContent==='Sol'));
 assert.equal(choiceChanges,1);consentChecked=true;
 finishCatalog();await new Promise(resolve=>setImmediate(resolve));assert.ok(select.querySelectorAll('option').some(n=>n.textContent==='raw-model-id'));assert.equal(select.querySelectorAll('option').some(n=>n.textContent.includes('Friendly')),false);
 assert.equal(choiceChanges,1);assert.equal(consentChecked,true);
 select.value=JSON.stringify(['DeepSeek','raw-model-id']);select.fire('change');assert.equal(selector.value().config.model,'raw-model-id');assert.equal(selector.value().engine_key,'compatible_api');
 assert.equal(choiceChanges,2);assert.equal(consentChecked,false);consentChecked=true;
 ctx.fetch=async()=>({ok:false,json:async()=>({error:'offline'})});await selector.refresh();await new Promise(resolve=>setImmediate(resolve));assert.equal(selector.value().config.model,'raw-model-id');assert.equal(status.textContent,'模型列表刷新失败，仍可使用当前模型。');
 assert.equal(choiceChanges,2);assert.equal(consentChecked,true);
 api.use('DeepSeek',{...api.get('DeepSeek'),api_key:'changed-secret'});await selector.refresh();await new Promise(resolve=>setImmediate(resolve));assert.equal(choiceChanges,3);assert.equal(consentChecked,false);
 consentChecked=true;api.use('OpenRouter',{...api.get('OpenRouter'),model:'other-model',api_key:'unrelated-secret'});await selector.refresh();await new Promise(resolve=>setImmediate(resolve));assert.equal(choiceChanges,3);assert.equal(consentChecked,true);
 select.value='codex_cli';select.fire('change');assert.equal(choiceChanges,4);assert.equal(consentChecked,false);
assert.equal(selector.value().engine_key,'codex_cli');
 // Fresh pages restore the local service's saved defaults, not browser storage.
 const fresh={...ctx,fetch:settingsFetch,window:{dispatchEvent(){}}};
 vm.createContext(fresh);vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/api_profiles.js'),'utf8'),fresh);
 assert.equal(fresh.window.APIProfiles.active(),null);
 await fresh.window.APIProfiles.load();assert.equal(fresh.window.APIProfiles.active().config.model,'saved-model');
 await fresh.window.APIProfiles.remove('DeepSeek');assert.equal(fresh.window.APIProfiles.active(),null);
 assert.equal(fresh.window.APIProfiles.hasSaved('DeepSeek'),false);
 console.log('api_profiles_ui: saved settings reload/remove, shared catalog cache/refresh/identity isolation/errors passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
