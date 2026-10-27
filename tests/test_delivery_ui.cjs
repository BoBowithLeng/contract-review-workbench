'use strict';
// No model calls: exercise source alignment and the read-only suggestion path.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../reader_ui/delivery.js'),'utf8')
  .replace('return {load,decorateDocument', 'return {_test:{minimalEdit,overlaps,savedDrafts,hasEditInstructionPrefix,invalidateReads(){preparationTicket++;}},load,decorateDocument');
const text='4.1 乙方提供测试版本供甲方验收。甲方应在7日内反馈；逾期视为通过。';
const quote='甲方应在7日内反馈；逾期视为通过。';
const target={piece_id:'P016',paragraph_sha256:'sha',start:Array.from(text.slice(0,text.indexOf(quote))).length,
  end:Array.from(text).length,old_text:quote,location:'第四条 / 4.1'};
const fixture={available:true,source:{source_sha256:'source',pieces:[{id:'P016',text,paragraph_sha256:'sha'}]},
  state:{revision:0,edits:[],comments:[]},targets:{F04:[target],F02:[target,{...target,piece_id:'P017'}]}};
let cached=null,reads=0;
const ctx={window:{},document:{getElementById:()=>({open:false})},TextEncoder,crypto:require('node:crypto').webcrypto,request:async url=>{if(url.includes('/drafts/')){reads++;return cached;}return fixture;}};
vm.createContext(ctx);vm.runInContext(source,ctx);
(async()=>{
  const ui=ctx.window.DeliveryUI;
  await ui.load({id:'job-fixture',paragraphs:[{id:'P016',text,location:'第四条 / 4.1'}],state:{reviews:{}}});
  assert.equal(ui.needsPreparation({id:'F04'},'甲方应在10日内反馈。'),false);
  assert.equal(ui.needsPreparation({id:'F04'},'将第4.1条修改为：甲方应在10日内反馈。'),true);
  assert.equal(ui.needsPreparation({id:'F02'},'甲方应在10日内反馈。'),true);
  assert.equal(ui.needsPreparation({id:'F04'},'4.1 原有条款。\n8.2 通知送达。'),true,'even one reference needs preparation for multiple suggested clauses');
  assert.equal(ui.suggestions({id:'F04'},'将第4.1条修改为：新文字。').length,0);
  const multi={id:'F02',refs:[{id:'P016'},{id:'P017'}]},prose='甲方应在10日内反馈。';
  assert.ok(ui.scope(multi,prose).every(row=>row.action==='unexplained'));
  cached={status:'ready',based_on_revision:0,source_sha256:'source',draft_sha256:require('node:crypto').createHash('sha256').update(prose).digest('hex'),edits:[{...target,new_text:prose}],paragraph_actions:[{piece_id:'P016',action:'modify',reason:'调整此处反馈期限',location:'第四条 / 4.1'},{piece_id:'P017',action:'retain',reason:'另一段已明确账号交付方式，无需联动调整',location:'第四条 / 4.2'}]};
  assert.equal(await ui.ensureCandidate(multi,prose),true);assert.equal(reads,1);assert.equal(await ui.ensureCandidate(multi,prose),false);assert.equal(reads,1,'read once, never POST on card rendering');
  assert.equal(ui.scope(multi,prose)[1].action,'retain');assert.equal(ui.suggestions(multi,prose).length,1);
  assert.equal(ui.scope(multi,prose,{edits:cached.edits})[1].action,'unexplained','legacy omitted reference is not retain');
  assert.equal(await ui.ensureCandidate(multi,prose+'改过'),false,'different draft hash cannot show stale candidate');
  const prior=cached,raceText='新确认的条款';let release;cached=new Promise(resolve=>release=resolve);
  const pending=ui.ensureCandidate(multi,raceText);ui._test.invalidateReads();release({...prior,draft_sha256:require('node:crypto').createHash('sha256').update(raceText).digest('hex')});
  assert.equal(await pending,false,'a late cache GET cannot replace a newer explicit preparation');cached=prior;
  fixture.state.revision=1;assert.equal(ui.suggestions(multi,prose).length,0,'stale candidate is not previewed after document changes');assert.ok(ui.scope(multi,prose).every(row=>row.action==='unexplained'));fixture.state.revision=0;
  const prefix=text.slice(0,text.indexOf(quote)),draft=prefix+'甲方应在10个工作日内反馈。';
  const full={...target,start:0,end:Array.from(text).length,old_text:text};
  const change=ui._test.minimalEdit(full,draft);
  assert.equal(Array.from(text).slice(0,change.start).join('')+change.new_text+Array.from(text).slice(change.end).join(''),draft);
  assert.ok(change.start>=Array.from(prefix).length);
  assert.equal(ui._test.overlaps({...change,piece_id:'P016'},{piece_id:'P016',start:change.start,end:change.end}),true);
  assert.equal(ui._test.overlaps({...change,piece_id:'P016'},{piece_id:'P017',start:change.start,end:change.end}),false);
  fixture.state.edits=[{id:'edit-F04-prepared-P016',...change,finding_ids:['F04']}];
  const saved=ui.suggestions({id:'F04'},'将第4.1条修改为：旧建议。');
  assert.equal(saved.length,1);assert.equal(saved[0].after,draft);assert.equal(saved[0].before,text);
  const unicode=ui._test.minimalEdit({piece_id:'P0',paragraph_sha256:'s',start:0,end:3,old_text:'一😀二'},'一😀三');
  assert.equal(unicode.start,2);assert.equal(unicode.end,3);
  const insertion={kind:'insert_paragraph_after',piece_id:'P016',paragraph_sha256:'sha',start:text.length,end:text.length,old_text:'',new_text:'8.2 通知送达。'};
  assert.equal(ui._test.minimalEdit(insertion,insertion.new_text).kind,'insert_paragraph_after');
  assert.equal(ui._test.overlaps(insertion,{...change,piece_id:'P016'}),false,'new paragraph does not overlap a replacement in its anchor');
  fixture.state.edits.push({id:'insert-F04',...insertion,finding_ids:['F04']});assert.equal(ui._test.savedDrafts({id:'F04'}).length,2,'standalone insertion is not fused into anchor text');
  fixture.state.draft_confirmations={F04:{paragraph_actions:[{piece_id:'P017',action:'retain',reason:'已确认保留的原文理由',location:'第四条 / 4.2'}]}};
  const retained=ui.scope({id:'F04',refs:[{id:'P016'},{id:'P017'}]},draft);assert.equal(retained.find(x=>x.piece_id==='P017').action,'retain');assert.equal(retained.find(x=>x.piece_id==='P017').reason,'已确认保留的原文理由');assert.equal(retained.at(-1).label,'已新增');
  console.log('delivery_ui: instruction/multi-target routing, saved full context, exact minimal edit, overlap and Unicode checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
