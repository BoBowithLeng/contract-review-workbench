'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
class Element{
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.textContent='';this.value='';this.listeners={};this.open=false;this.hidden=false;this.disabled=false;this.classList={toggle(){}};}
  append(...nodes){this.children.push(...nodes);}
  add(node){this.append(node);}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  setAttribute(){}
  addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);}
  removeEventListener(type,fn){this.listeners[type]=(this.listeners[type]||[]).filter(f=>f!==fn);}
  focus(){}
  showModal(){this.open=true;}
  close(){this.open=false;}
  get selectedOptions(){return this.children.filter(n=>n.value===this.value);}
}
const all=n=>[n,...n.children.flatMap(all)],elements={};
for(const id of ['model-settings-dialog','model-settings-form','settings-provider','settings-base-url','settings-api-key','settings-model-id','settings-save','settings-remove','settings-error','settings-model-field','workbench-settings','settings-close'])elements[id]=new Element('div');
const document={createElement:tag=>new Element(tag),getElementById:id=>elements[id]};
const providers=[{id:'DeepSeek',label:'DeepSeek'},{id:'OpenRouter',label:'OpenRouter'}],models=[{id:'deepseek-flash',name:'DeepSeek-V4.1-Flash'},{id:'raw-second-id',name:'Distinct Friendly Model'},...Array.from({length:15},(_,i)=>({id:'raw-model-'+i,name:'Catalog model '+i}))];
let saved=true,config={base_url:'https://fixture.test/v1',api_key:'fixture-key',model:'deepseek-flash'},sent=null,reloads=0,removed=null;
const api={async load(options){if(options?.reload)reloads++;return providers;},get:()=>({...config}),hasSaved:id=>saved&&id==='DeepSeek',active:()=>({provider:'DeepSeek',config:{...config}}),async catalog(){return models;},async save(provider,values){sent={provider,config:values};config={...values};},async remove(provider){removed=provider;saved=false;}};
const ctx={window:{addEventListener(){},dispatchEvent(){}},document,Event:class{},setTimeout,clearTimeout,confirm:()=>true,Option:function(text,value){const n=new Element('option');n.textContent=text;n.value=value;return n;}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/api_profiles.js'),'utf8'),ctx);ctx.window.APIProfiles=api;ctx.APIProfiles=api;
vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/model_settings.js'),'utf8'),ctx);
(async()=>{
  await ctx.window.ModelSettings.open();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(reloads,1);assert.equal(elements['settings-provider'].children[0].textContent,'DeepSeek');assert.ok(elements['settings-provider'].children.every(p=>!p.textContent.includes('已保存')));
  const picker=elements['settings-model-field'],select=all(picker).find(n=>n.tagName==='SELECT'),search=all(picker).find(n=>n.type==='search');
  assert.equal(select.value,'deepseek-flash');assert.equal(select.children.find(o=>o.value==='deepseek-flash').textContent,'deepseek-flash');assert.ok(select.children.every(o=>!o.textContent.includes('DeepSeek-V4.1-Flash')));
  assert.equal(search.hidden,false);search.value='distinct friendly';search.oninput();assert.ok(select.children.some(o=>o.value==='raw-second-id'),'display names still participate in search');assert.ok(select.children.every(o=>!o.textContent.includes('Friendly')));
  select.value='raw-second-id';select.onchange();await elements['model-settings-form'].onsubmit({preventDefault(){}});assert.equal(sent.provider,'DeepSeek');assert.equal(sent.config.model,'raw-second-id');assert.equal(sent.config.api_key,'fixture-key');assert.equal(elements['model-settings-dialog'].open,false);
  await ctx.window.ModelSettings.open();await elements['settings-remove'].onclick();assert.equal(removed,'DeepSeek');assert.equal(elements['settings-remove'].hidden,true);assert.equal(elements['settings-provider'].children[0].textContent,'DeepSeek');
  console.log('model_settings_ui: plain provider names and raw model IDs, friendly-name search, saved config ID and remove flow passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
