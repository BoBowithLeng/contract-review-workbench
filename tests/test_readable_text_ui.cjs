'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const ctx={window:{}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../reader_ui/readable_text.js'),'utf8'),ctx);
const humanize=ctx.window.ReaderText.humanize;
const locationParts=ctx.window.ReaderText.locationParts;
assert.deepEqual(JSON.parse(JSON.stringify(locationParts({location:'2. 开发费用 / 2.3.',text:'尾款在验收后支付。'}))),{title:'第2.3条',context:'2. 开发费用',excerpt:'尾款在验收后支付。'});
assert.equal(locationParts({location:'附件：项目确认书 / 六、付款进度',text:'首笔款'}).context,'附件：项目确认书');
assert.equal(humanize('与O2和O04衔接',{option_labels:{O2:'O2 顺延方案',O04:'O04 付款结构'}},{plain:true}),'与O2 顺延方案和O04 付款结构衔接');
const context={locations:{P048:'第二条 · （二）',P049:'第二条 · （三）'},sources:[
  {id:'LCL14',title:'中华人民共和国劳动合同法',article:'第十四条'},
  {id:'SPC10_11',title:'最高人民法院关于审理劳动争议案件适用法律问题的解释（二）',article:'第十、十一条'}]};
const result=humanize('P048 和 P49 的 current_text：LCL14 + SPC解释二第十、十一条。',context);
assert.equal(result,'第二条 · （二） 和 第二条 · （三） 的 当前条款文字：中华人民共和国劳动合同法 · 第十四条 + 最高人民法院关于审理劳动争议案件适用法律问题的解释（二）第十、十一条。');
assert.equal(humanize('XP048X / https://example.com/P048 / `P048`',context),'XP048X / https://example.com/P048 / `P048`');
assert.equal(humanize('[LCL14](https://example.com/LCL14)',context),'[中华人民共和国劳动合同法 · 第十四条](https://example.com/LCL14)');
assert.equal(humanize('```json\n{"current_text":"P048"}\n```',context),'```json\n{"current_text":"P048"}\n```');
assert.ok(humanize('P048', {locations:{P048:'x|[fake](https://evil.example)'}}).includes('\\|\\['));
assert.equal(humanize('LCL99 P999',context),'LCL99 P999');
const oldReplyContext={...context,current_finding_id:'F02',finding_labels:{F02:'第1条意见',F01:'第2条意见'},
  locations:{P048:'二、劳动合同期限 / （二） · “（二）劳动合同期满甲乙双方未提出终止劳动合同或重新续订劳动合…”',
             P049:'二、劳动合同期限 / （二） · “乙方满足《劳动合同法》及相关法律法规规定的应当签署无固定期限…”'}};
const oldReply=humanize('P048 的 current_text 仍是原文、confirmed_edit_ids 为空。把 F02 更新，并与 P049、F01 联动。',oldReplyContext);
assert.ok(!/confirmed_edit_ids|\bF0[12]\b|P048|P049/.test(oldReply));
assert.ok(oldReply.includes('尚未确认修改'));
assert.ok(oldReply.includes('把 本条意见 更新'));
assert.ok(oldReply.includes('第2条意见'));
assert.ok(!oldReply.includes('二、劳动合同期限 / （二）'));
assert.ok(oldReply.includes('劳动合同期满')&&oldReply.includes('乙方满足'));
assert.equal(humanize('confirmed_edit_ids 不为空',oldReplyContext),'已确认的合同修改 不为空');
assert.equal(humanize('confirmed_edit_ids = []',oldReplyContext),'尚未确认修改');
assert.equal(humanize('```json\n{"confirmed_edit_ids":[],"id":"F02"}\n```',oldReplyContext),'```json\n{"confirmed_edit_ids":[],"id":"F02"}\n```');
console.log('readable_text: real paragraph/source labels, aliases, literal code and URL preservation passed');

const presentation={plain:true,fullLocations:true,hideUnknown:true};
assert.equal(humanize('P48、F99、finding、clause_draft',{locations:{P048:'正文 · 第八条 · 付款'}},presentation),'正文 · 第八条 · 付款、相关意见、审阅意见、建议改稿');
assert.equal(humanize('`P048` / https://example.com/P048',context,presentation),'第二条 · （二） / https://example.com/P048');
const resolve=ctx.window.ReaderText.resolveDrafts;
const draftFindings=[{id:'F01',title:'付款约定',clause_draft:'甲方应在验收后付款。'},{id:'F02',title:'验收约定',status:'处理失败',clause_draft:'不可使用的失败内容'}];
let resolved=resolve({clause_draft:'见各finding的clause_draft，合并后形成完整条款。',finding_ids:['F01','F02','F99']},draftFindings);
assert.equal(resolved.referenceOnly,true);assert.equal(resolved.drafts.length,1);assert.equal(resolved.drafts[0].text,'甲方应在验收后付款。');assert.deepEqual(Array.from(resolved.missing),['F02','F99']);
resolved=resolve({clause_draft:'甲方应按F01安排付款。',finding_ids:['F01']},draftFindings);assert.equal(resolved.referenceOnly,false);assert.equal(resolved.drafts[0].text,'甲方应按F01安排付款。');
resolved=resolve({clause_draft:'见F01的clause_draft。\n双方应另行签署验收确认书。',finding_ids:['F01']},draftFindings);assert.equal(resolved.referenceOnly,false);assert.ok(resolved.drafts[0].text.includes('双方应另行签署验收确认书'));
resolved=resolve({clause_draft:'见F01的clause_draft。',finding_ids:['F01']},draftFindings,{F01:{decision:'修改',clause_draft:'人工修改条款'}});assert.equal(resolved.drafts[0].text,'人工修改条款');
console.log('readable_text: presentation prose, unknown referents, usable option-draft resolution, failed/missing drafts and preserved real clauses passed');

const namedFindings=[{id:'F01',title:'付款',clause_draft:'付款条款'},{id:'F02',title:'验收',clause_draft:'验收条款'},{id:'F03',title:'交付',clause_draft:'未关联的交付条款'}];
resolved=resolve({clause_draft:'见F02的clause_draft',finding_ids:['F01','F02']},namedFindings);assert.equal(resolved.drafts.length,1);assert.equal(resolved.drafts[0].id,'F02');assert.equal(resolved.drafts[0].text,'验收条款');
resolved=resolve({clause_draft:'见F2的clause_draft',finding_ids:['F01','F02']},namedFindings);assert.equal(resolved.drafts.length,1);assert.equal(resolved.drafts[0].id,'F02');
resolved=resolve({clause_draft:'见F02和F2的clause_draft',finding_ids:['F01','F02']},namedFindings);assert.equal(resolved.drafts.length,1);
resolved=resolve({clause_draft:'见F03和F99的clause_draft',finding_ids:['F01','F02']},namedFindings);assert.equal(resolved.drafts.length,0);assert.deepEqual(Array.from(resolved.missing),['F03','F99']);
resolved=resolve({clause_draft:'见F02、F03的clause_draft',finding_ids:['F01','F02']},namedFindings);assert.equal(resolved.drafts.length,1);assert.equal(resolved.drafts[0].id,'F02');assert.deepEqual(Array.from(resolved.missing),['F03']);
resolved=resolve({clause_draft:'见各finding的clause_draft',finding_ids:['F01','F02']},namedFindings);assert.deepEqual(Array.from(resolved.drafts,d=>d.id),['F01','F02']);
console.log('readable_text: explicitly named linked draft only, padded aliases, deduplication, unknown/unlinked missing and generic all-linked references passed');

const sourceNote=ctx.window.ReaderText.sourceNote,findingTitle=ctx.window.ReaderText.findingTitle;
assert.equal(sourceNote({id:'LCL14',snapshot_verified:true,verification_state:'verified_snapshot',checked_at:'2026-09-20'},true),'（已核对原文 · 核对日期 2026-09-20）');
assert.equal(sourceNote({id:'WEB001',snapshot_verified:false,verification_state:'candidate'}),'（联网检索结果，原文待核对）');
assert.equal(sourceNote({id:'SRC1',verified:false}),'（原文核对情况未记录）');
assert.equal(sourceNote({id:'SRC1',verified:true,checked_at:'2024-06-01T10:00:00'}),'（原文核对情况未记录）');
assert.ok(!sourceNote({snapshot_verified:false,verification_state:'unverified',checked_at:'not a date'}).includes('not a date'));
assert.equal(findingTitle({id:'F1',status:'处理失败',title:'结算约定'}),'结算约定');
assert.equal(findingTitle({id:'F1',rule_id:'C1',status:'处理失败',title:'模型结果校验失败'}, {preparation:{checks:[{id:'C1',title:'解除后的结算'}]}}),'解除后的结算');
assert.equal(findingTitle({id:'F1',status:'处理失败',title:'模型结果校验失败'}, {preparation:{checks:[{id:'C1',title:'交付范围'}]},checklist_results:[{check_id:'C1',finding_ids:['F1']}]}),'交付范围');
assert.equal(findingTitle({id:'F1',status:'处理失败',title:'模型结果校验失败'}),'本项检查尚未完成');
console.log('readable_text: frozen-source status/date and business failure titles passed');

for(const raw of [{verified:true,checked_at:'2026-09-20'},{verified:true,text:'原文'},{verified:true,text:'原文',checked_at:'2099-01-01'}]){
 assert.equal(sourceNote(raw),'（原文核对情况未记录）');
 assert.ok(!sourceNote(raw).includes('已核对原文'));
}
assert.equal(sourceNote({snapshot_verified:false,verification_state:'unverified',checked_at:'2099-01-01'},true),'（原文待核对）');
console.log('readable_text: raw registry flags never imply verification, unchecked future date is not a verification label passed');

const short=ctx.window.ReaderText.shortReference;
const directory='正文 / 1. 系统需求与功能说明 / 1.3 项目人员、权限、账号与操作约定';
assert.equal(short({numbering_label:'1.3.',location:directory,text:'用户权限的设置与交接。'}),'第1.3条');
assert.equal(short({location:'正文 / 第八条 价款与支付 / 第八条',text:'双方另行约定价款。'}),'第八条');
assert.equal(short({location:'附件 / 2.1 接口与交付清单',text:'交付时间需要另行填写'}),'附件第2.1条');
const inlineContext={locations:{P017:directory,P163:'附件 / 2.1 接口与交付清单',P047:'正文 / 4.5 验收和支付'},inline_locations:{P017:'第1.3条',P163:'附件第2.1条',P047:'第4.5条'}};
const reason='P017约定的交付义务，与附件P163及P047中的结算安排不一致；解除后已经履行的价款仍需结算。';
const original=JSON.stringify(inlineContext);
assert.equal(humanize(reason,inlineContext,{plain:true,shortLocations:true}), '第1.3条约定的交付义务，与附件第2.1条及第4.5条中的结算安排不一致；解除后已经履行的价款仍需结算。');
assert.equal(JSON.stringify(inlineContext),original);assert.equal(reason.includes('P017'),true);
assert.equal(humanize('P017',inlineContext,{plain:true,fullLocations:true}),directory);
console.log('readable_text: compact numbered inline references, no duplicated attachment, unchanged substantive prose and exact separate location passed');
const rangeContext={locations:{P010:'正文 / 1 开发软件 / 1.1',P013:'正文 / 1 开发软件 / 1.1',P047:'正文 / 4 验收 / 4.5',P163:'附件一：功能清单 / 1.1',P180:'附件二：交付清单 / 1.1'},paragraphs:[
  {id:'P010',location:'正文 / 1 开发软件 / 1.1',text:'开发软件应满足双方确认的需求。'},
  {id:'P013',location:'正文 / 1 开发软件 / 1.1',text:'开发软件的技术方案由乙方确认。'}],finding_labels:{F01:'付款约定'},inline_locations:{P047:'第4.5条'}};
const rangeRaw='范围(P010-P013)，另见P047和F01。',rangeSnapshot=JSON.stringify(rangeContext);
assert.equal(humanize(rangeRaw,rangeContext,{plain:true,shortLocations:true}),'范围(第1.1条，原文“开发软件应满足双方确认的需求。”至“开发软件的技术方案由乙方确认。”)，另见第4.5条和付款约定。');
assert.equal(humanize('P10—P13',rangeContext,{plain:true,shortLocations:true}),'第1.1条，原文“开发软件应满足双方确认的需求。”至“开发软件的技术方案由乙方确认。”');
assert.ok(humanize('P010-P047',rangeContext,{plain:true,shortLocations:true}).includes('第4.5条'));assert.ok(humanize('P163-P180',rangeContext,{plain:true,shortLocations:true}).includes('附件一：功能清单·第1.1条—附件二：交付清单·第1.1条'));
assert.equal(humanize('金额A-A，期间1.1—1.1，https://example.test/P010-P013',rangeContext,{plain:true,shortLocations:true}),'金额A-A，期间1.1—1.1，https://example.test/P010-P013');
assert.equal(humanize('```json\n{"range":"P010-P013"}\n```',rangeContext,{plain:true,shortLocations:true}),'```json\n{"range":"P010-P013"}\n```');assert.equal(humanize('`P010-P013`',rangeContext,{shortLocations:true}),'`P010-P013`');
assert.equal(JSON.stringify(rangeContext),rangeSnapshot);assert.equal(rangeRaw,'范围(P010-P013)，另见P047和F01。');
const itemRange={paragraphs:[{id:'P093',location:'9. 保密 / 9.2.',text:'个人信息与数据处理'},{id:'P103',location:'9. 保密 / （10） / 9.2.',text:'（10）甲方业务数据归甲方所有。'}]};
itemRange.locations=Object.fromEntries(itemRange.paragraphs.map(p=>[p.id,p.location]));
assert.equal(humanize('P093—P103',itemRange,{plain:true,shortLocations:true}),'第9.2条，原文“个人信息与数据处理”至“（10）甲方业务数据归甲方所有。”','numbered sub-items do not split one clause into two places');
const dottedItems={paragraphs:[{id:'P092',location:'9. 保密 / 9.2.',text:'保密信息是指信息披露方提供的信息。'},{id:'P093',location:'9. 保密 / 9.2.',text:'个人信息与数据保护'},{id:'P100',location:'9. 保密 / 7. / 9.2.',text:'7. 乙方违反本条约定处理个人信息的，应赔偿甲方。'}]};
dottedItems.locations=Object.fromEntries(dottedItems.paragraphs.map(p=>[p.id,p.location]));
assert.equal(humanize('已通过P093-P100落地',dottedItems,{plain:true,shortLocations:true}),'已通过第9.2条，原文“个人信息与数据保护”至“7. 乙方违反本条约定处理个人信息的，应赔偿甲方…”落地','“7.” items under a clause stay inside that clause');
assert.equal(humanize('按draft_plan修改P093：',itemRange,{plain:true,shortLocations:true}),'按建议改稿修改第9.2条 · 原文“个人信息与数据处理”：');
assert.equal(humanize('research_memo 仅为检索候选',itemRange,{plain:true}),'检索记录 仅为检索候选');
// A frozen context reuses its label tables across calls; the current finding is still decided per call
// and the result matches an unfrozen (uncached) context word for word.
const cacheLabels={F02:'第1条意见',F01:'第2条意见'},cacheText='F02 与 F01、F2 和 P048 联动，O1 待定';
const cacheBase={locations:{P048:'第二条 · （二）'},finding_labels:cacheLabels,option_labels:{O01:'O01 顺延方案'}};
const cacheFrozen={...cacheBase,locations:Object.freeze({...cacheBase.locations})};
for(const current of [undefined,'F02','F01']){
  const options={plain:true,shortLocations:true,hideUnknown:true};
  assert.equal(humanize(cacheText,{...cacheFrozen,current_finding_id:current},options),humanize(cacheText,{...cacheBase,current_finding_id:current},options),'cached labels match uncached for '+current);
}
assert.equal(humanize(cacheText,{...cacheFrozen,current_finding_id:'F02'},{plain:true}),'本条意见 与 第2条意见、本条意见 和 第二条 · （二） 联动，O01 顺延方案 待定');
assert.equal(humanize(cacheText,{...cacheFrozen,current_finding_id:'F01'},{plain:true}),'第1条意见 与 本条意见、第1条意见 和 第二条 · （二） 联动，O01 顺延方案 待定');
assert.equal(humanize('F01',{...cacheFrozen,finding_labels:{F01:'第9条意见'}},{plain:true}),'第9条意见','a new finding label table is not served from the old cache');
console.log('readable_text: machine ranges retain real endpoints and same-clause excerpts, attachment context, aliases, literal business ranges, URLs/code and raw input passed');
