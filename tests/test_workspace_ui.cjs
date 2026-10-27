'use strict';
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../reader_ui/workspace.js'),'utf8');
function setup(width,saved=null,hidden=false){
  const handlers={},windowHandlers={},values={},storage=new Map(saved===null?[]:[['reader-workspace-review-width',String(saved)]]);
  const workspace={hidden,getBoundingClientRect:()=>({width,right:width}),style:{setProperty:(key,value)=>values[key]=value}};
  const splitter={getBoundingClientRect:()=>({width:8}),setAttribute:(key,value)=>values[key]=value,addEventListener:(key,handler)=>handlers[key]=handler};
  const ctx={document:{querySelector:()=>workspace,getElementById:id=>id==='workspace-splitter'?splitter:null},
    window:{matchMedia:()=>({get matches(){return width>=921;}}),addEventListener:(key,handler)=>windowHandlers[key]=handler,dispatchEvent(){}},
    localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},Event:class{}};
  vm.runInNewContext(source,ctx);
  return {values,storage,workspace,resize(next){width=next;windowHandlers.resize();},reset(){handlers.dblclick();},key(key){handlers.keydown({key,preventDefault(){}});}};
}
for(const [viewport,expected] of [[1080,420],[1280,420],[1707,634],[1920,740],[2560,1060],[3200,1100]]){
  const ui=setup(viewport);
  assert.equal(ui.values['--review-width'],`${expected}px`);
  assert.equal(ui.values['aria-valuenow'],String(expected));
  assert.equal(ui.storage.size,0,'automatic sizing must not persist a preference');
}
const automatic=setup(1080);automatic.resize(2560);assert.equal(automatic.values['--review-width'],'1060px');
const saved=setup(2560,900);assert.equal(saved.values['--review-width'],'900px');
saved.resize(1080);assert.equal(saved.values['--review-width'],'772px');
saved.resize(2560);assert.equal(saved.values['--review-width'],'900px','temporary shrink must preserve preferred width');
saved.key('ArrowLeft');assert.equal(saved.values['--review-width'],'916px');assert.equal(saved.storage.get('reader-workspace-review-width'),'916');
saved.reset();assert.equal(saved.values['--review-width'],'1060px');assert.equal(saved.storage.size,0);
for(const invalid of [0,-1,100,'broken'])assert.equal(setup(2560,invalid).values['--review-width'],'1060px');
const home=setup(2560,null,true);assert.equal(home.values['--review-width'],undefined);home.workspace.hidden=false;home.resize(2560);assert.equal(home.values['--review-width'],'1060px');
const narrow=setup(650);narrow.resize(650);assert.equal(narrow.values['--review-width'],'342px');
const desk=fs.readFileSync(path.join(__dirname,'../reader_ui/workbench.css'),'utf8');
const check=fs.readFileSync(path.join(__dirname,'../reader_ui/checklists.css'),'utf8');
assert.ok(!/\.workbench-view\{[^}]*max-width/.test(desk));
assert.ok(!/#checklist-page-host\{[^}]*max-width/.test(check));
assert.ok(!/\.check-results-intro,\.check-results-layout\{[^}]*max-width/.test(check));
const css=fs.readFileSync(path.join(__dirname,'../reader_ui/style.css'),'utf8');
assert.ok(css.includes('.paper{max-width:730px'));
assert.ok(css.includes('.paper{max-width:790px}'));
console.log('workspace: adaptive defaults, explicit preferences, resize/reopen/reset, ARIA and readable paper passed');
