'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
class Element{
  constructor(){this.hidden=true;this.disabled=false;this.checked=false;this.open=false;this.textContent='';this.listeners={};this.children=[];}
  replaceChildren(...items){this.children=[];this.append(...items);}
  append(...items){for(const item of items){this.children.push(item);}this.textContent=this.children.map(c=>typeof c==='string'?c:c.textContent).join('');}
  addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);}
  setAttribute(){} focus(){this.focused=true;} showModal(){this.open=true;} close(){this.open=false;}
}
const names=['desktop-settings','desktop-remove-dialog','desktop-remove-open','desktop-remove-confirm','desktop-remove-data','desktop-remove-cancel','desktop-remove-close','desktop-remove-data-note','desktop-remove-data-path','desktop-settings-error','desktop-remove-error','model-settings-title','settings-close','desktop-data-location','desktop-remove-app-path','workbench-settings'];
const elements=Object.fromEntries(names.map(id=>[id,new Element()]));
const listeners={},window={addEventListener:(type,fn)=>{listeners[type]=fn;}};
const ctx={window,document:{getElementById:id=>elements[id],createElement:()=>new Element()}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/desktop_ui.js'),'utf8'),ctx);
(async()=>{
  await window.DesktopUI.refresh();assert.equal(elements['desktop-settings'].hidden,true);
  let calls=[],result={cancelled:true};
  window.pywebview={api:{get_desktop_info:async()=>({available:true,remove_allowed:true,app_dir:'C:/Demo',data_dir:'C:/Demo/我的数据',version:'0.1.1'}),request_remove:async value=>{calls.push(value);return result;}}};
  await listeners.pywebviewready();assert.equal(elements['desktop-settings'].hidden,false);
  assert.equal(elements['model-settings-title'].textContent,'设置');
  assert.equal(elements['desktop-data-location'].textContent,'审查记录保存在：C:/Demo/我的数据','without the folder bridge the path stays plain text');
  let opened=0;window.pywebview.api.open_data_folder=async()=>{opened+=1;return {ok:true};};
  await window.DesktopUI.refresh();const folderLink=elements['desktop-data-location'].children[1];
  assert.equal(folderLink.textContent,'C:/Demo/我的数据');await folderLink.onclick();assert.equal(opened,1);
  window.pywebview.api.open_data_folder=async()=>({ok:false,error:'数据文件夹不存在'});await folderLink.onclick();
  assert.match(elements['desktop-settings-error'].textContent,/不存在/);
  await window.DesktopUI.open();assert.equal(elements['desktop-remove-dialog'].open,true);
  assert.equal(elements['desktop-remove-data'].checked,false);assert.match(elements['desktop-remove-data-note'].textContent,/保留/);
  assert.equal(elements['desktop-remove-app-path'].textContent,'C:/Demo');
  await elements['desktop-remove-confirm'].onclick();assert.deepEqual(calls,[false]);assert.equal(elements['desktop-remove-confirm'].disabled,false);
  elements['desktop-remove-data'].checked=true;elements['desktop-remove-data'].onchange();assert.match(elements['desktop-remove-data-note'].textContent,/回收站/);
  result={ok:false,error:'还有审查任务正在运行'};await elements['desktop-remove-confirm'].onclick();assert.equal(calls[1],true);assert.match(elements['desktop-remove-error'].textContent,/正在运行/);
  elements['desktop-remove-cancel'].onclick();assert.equal(elements['desktop-remove-dialog'].open,false);
  await window.DesktopUI.open();assert.equal(elements['desktop-remove-data'].checked,false,'reopening must reset destructive choice');
  result={ok:true};await elements['desktop-remove-confirm'].onclick();assert.equal(elements['desktop-remove-confirm'].disabled,true);
  let prevented=false;elements['desktop-remove-dialog'].listeners.cancel[0]({preventDefault(){prevented=true;}});assert.equal(prevented,true);
  await elements['desktop-remove-confirm'].onclick();assert.equal(calls.length,3,'double clicks must not repeat removal');
  console.log('desktop_ui: browser hidden, native entry, default keep, explicit recycle, cancel/error/retry and duplicate prevention passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
