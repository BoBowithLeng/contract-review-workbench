'use strict';
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
class Element{
  constructor(tag='div'){this.tagName=tag;this.children=[];this.listeners={};this.attributes={};this.className='';this.textContent='';this.hidden=false;this.disabled=false;this.open=false;}
  append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node);}}
  get firstChild(){return this.children[0];}
  setAttribute(k,v){this.attributes[k]=v;}
  removeAttribute(k){delete this.attributes[k];delete this[k];}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  addEventListener(k,fn){this.listeners[k]=fn;}
  remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
  focus(){focused=this;}
  showModal(){this.open=true;}
  close(){this.open=false;this.listeners.close?.();}
}
let focused,requests=[],responses=[],refreshes=0,notices=[];
const elements=new Map(),body=new Element('body');
const document={body,createElement:tag=>new Element(tag),getElementById:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},querySelector:()=>body.children.find(n=>n.className.includes('contract-delete-dialog'))};
const ctx={window:{addEventListener(){}},document,location:{search:''},URLSearchParams,notify:s=>notices.push(s),fetch:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return responses.shift();}};
vm.createContext(ctx);
const code=fs.readFileSync(path.join(__dirname,'../reader_ui/workbench.js'),'utf8').replace('return {configure,','return {_test:{deleteDialog,setRefresh(fn){refresh=fn;}},configure,');
vm.runInContext(code,ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/rereview.js'),'utf8'),ctx);
const child={report:{metadata:{rereview:{parent_report_id:'job-parent',round_number:2}}},parent_report_deleted:true};
ctx.window.RereviewUI.load(child);
assert.equal(elements.get('previous-round').textContent,'前一轮已删除');
assert.equal(elements.get('previous-round').href,undefined);
assert.equal(elements.get('review-round').textContent,'第2轮复审');
ctx.window.RereviewUI.load({...child,parent_report_deleted:false});
assert.equal(elements.get('previous-round').href,'/?report=job-parent');
const api=ctx.window.WorkbenchUI._test;
api.setRefresh(async()=>{refreshes++;});
const descend=node=>[node,...node.children.flatMap(descend)];
const find=text=>descend(body).find(n=>n.textContent===text);
const entry={name:'合成合同.docx',doc:{id:'doc-abc',reviews:[{id:'job-abc',round_number:2,parent_report_id:'job-old'}]}};
(async()=>{
  api.deleteDialog(entry);
  assert.equal(requests.length,0,'opening never deletes');
  assert.equal(focused.textContent,'取消','safe initial focus');
  assert.ok(find('第2轮复审'));
  assert.ok(find('其他审查轮次和电脑里的原始合同文件保留。'));
  find('取消').onclick();assert.equal(body.children.length,0);assert.equal(requests.length,0);
  api.deleteDialog(entry);
  responses.push({ok:false,json:async()=>({error:'这份合同仍在审核，请等结束后删除。'})});
  await find('永久删除').onclick();
  assert.ok(find('这份合同仍在审核，请等结束后删除。'));assert.equal(refreshes,0);assert.equal(body.children.length,1);
  assert.deepEqual(requests[0],{url:'/api/workbench/delete',body:{item_id:'doc-abc',confirm:true}});
  responses.push({ok:true,json:async()=>({deleted:true})});
  await find('重试删除').onclick();assert.equal(body.children.length,0);assert.equal(refreshes,1);assert.equal(notices.length,1);
  api.deleteDialog({item_id:'job-retry'});
  assert.ok(find('继续上次未完成的删除'));
  responses.push({ok:true,json:async()=>({deleted:true})});
  await find('重试删除').onclick();assert.equal(requests.at(-1).body.item_id,'job-retry');
  console.log('workbench_delete: cancel, confirmed single-row request, error/retry, pending cleanup passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
