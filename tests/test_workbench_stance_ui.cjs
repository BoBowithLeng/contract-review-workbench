'use strict';
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const elements=new Map();
const ctx={window:{},location:{search:''},URLSearchParams,document:{getElementById:id=>{
  if(!elements.has(id))elements.set(id,{addEventListener(){}});return elements.get(id);
}}};
vm.createContext(ctx);
const code=fs.readFileSync(path.join(__dirname,'../reader_ui/workbench.js'),'utf8')
  .replace('return {configure,','return {_test:{stanceFromContext,roundLabel,statusForEntry,contractName,openEntry,setData(value){data=value;}},configure,');
vm.runInContext(code,ctx);
const label=ctx.window.WorkbenchUI._test.stanceFromContext;
assert.equal(label({our_party:'甲方',our_role:'client'}),'甲方 · 客户');
assert.equal(label({our_party:'丙方',our_role:'保证人'}),'丙方 · 保证人');
assert.equal(label({our_party:'某某公司',our_role:'受托人'}),'某某公司 · 受托人');
assert.equal(label({our_party:'保证人',our_role:'保证人'}),'保证人');
assert.equal(label({our_party:'第三方',our_role:'constructor'}),'第三方 · constructor');
assert.equal(label({}),'未记录');
const round=ctx.window.WorkbenchUI._test.roundLabel;assert.equal(round({round_number:3,parent_report_id:'old'}),'第3轮复审');assert.equal(round({}),'第1轮初审');
const html=fs.readFileSync(path.join(__dirname,'../reader_ui/index.html'),'utf8');
assert.ok(html.includes('id="review-stance"'));
assert.ok(!html.includes('id="report-select"'));
console.log('workbench_stance: exact supplied party/role including third parties, read-only header passed');

const workbench=ctx.window.WorkbenchUI,api=workbench._test;
const partialJob={id:'job-1',report_id:'job-1',status:'failed',document_name:'付款合同.docx'};
api.setData({documents:[],pending_jobs:[partialJob]});
assert.equal(api.statusForEntry({job:partialJob}),'有事项未完成');
assert.equal(api.statusForEntry({job:{status:'failed'}}),'出错，可重试');
assert.equal(api.statusForEntry({job:{status:'prepared'}}),'待开始审查');
assert.equal(api.statusForEntry({job:{status:'needs_info'}}),'待回答问题');
assert.equal(api.statusForEntry({job:{status:'running'}}),'审查中');
assert.equal(api.contractName({id:'job-1'}),'付款合同.docx');
assert.equal(api.contractName({id:'other',document_name:'报告自带名.docx'}),'报告自带名.docx');
assert.equal(api.contractName({id:'before-catalog',report:{document_name:'直接打开的合同.docx'}}),'直接打开的合同.docx');
let openedReport,openedJob;workbench.configure({openReport:id=>openedReport=id,openJob:id=>openedJob=id,isBusy:()=>false});
api.openEntry({job:partialJob});assert.equal(openedReport,'job-1');assert.equal(openedJob,undefined);
api.openEntry({job:{id:'job-empty',status:'failed'}});assert.equal(openedJob,'job-empty');
console.log('workbench_stance: partial reports open directly, retain filename and unfinished scope; no-output task retains failure passed');
