"""Local-only workbench. Network calls occur only on explicit button presses."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import streamlit as st

from contract_core import ParsedContract, parse_docx, parse_text
from review_engine import review_detailed
from demo_contracts import MINIMAL_TEXT, table_docx
from model_client import chat_completion
from model_presets import PRESETS
from tool_review import load_review
from report_view import render_report, _reader
from document_view import reader_markup, docx_headings

ROOT = Path(__file__).resolve().parent
SOURCES = []
RUNS = {"detailed": ("模板 · 逐条审核与方案", ROOT / "runtime" / "sol-detailed-run-20260920"), "minimal": ("简约合同 · 真实试跑", ROOT / "runtime" / "sol-minimal-run-20260920"), "table": ("表格合同 · 真实试跑", ROOT / "runtime" / "sol-table-run-20260920"), "goal": ("早期：加入公司目标", ROOT / "runtime" / "sol-goal-run-20260920"), "first": ("早期：首次试跑", ROOT / "runtime" / "sol-tool-run-20260920")}
SAMPLE = """劳动合同节选（合成样例，不是真实雇员资料）
乙方应在合同期满前一个月书面申请订立无固定期限劳动合同，未申请视为放弃。
乙方离职时未完成工作交接的，甲方不支付工资，且不构成拖欠。
交接清单包括工作资料、客户记录、账号权限和办公设备，由甲方指定接收人。
经济补偿在办结工作交接时支付，不包括工资。
乙方离职后不得与甲方原客户开展同类业务。乙方负有保密义务。
另行启动离职竞业限制时，甲方按月支付竞业限制经济补偿。
"""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def clear_input():
    for key in ("contract", "loaded_signature", "report", "reviews", "report_signature", "report_id"):
        st.session_state.pop(key, None)


def save_report(report, signature, contract=None):
    st.session_state.report = report
    if contract is not None:
        st.session_state.report_contract = contract.model_dump()
        st.session_state.report_contract['headings'] = st.session_state.get('document_headings', {})
    st.session_state.report_signature = signature
    st.session_state.reviews = {}
    st.session_state.report_id = st.session_state.get("report_id", 0) + 1
    st.session_state.report_stale = False


def clear_credentials():
    st.session_state.pop("model_config", None)
    st.session_state.pop("profiles", None)
    st.session_state.config_epoch = st.session_state.get("config_epoch", 0) + 1


st.set_page_config(page_title="劳动合同 · 审查台", page_icon="▤", layout="wide")
st.markdown("""<style>
:root {--ink:#263344;--accent:#5850a2;--paper:#f7f8fc;}
.stApp {background:var(--paper);color:var(--ink);}
h1,h2,h3 {font-family:'DengXian','Microsoft YaHei',sans-serif;letter-spacing:.025em;}
h1 {font-weight:650!important;font-size:2.2rem!important;}
[data-testid="stSidebar"] {background:#edf0f7;border-right:1px solid #dce1ed;}
[data-testid="stMainBlockContainer"] {max-width:1600px;padding-top:4rem;}
[data-testid="stExpander"] {background:white;}
[data-testid="stAppDeployButton"] {display:none;}
[data-testid="stText"] {font-family:'Microsoft YaHei',sans-serif;line-height:1.8;}
.desk-label {color:#5850a2;font:600 .8rem 'Microsoft YaHei',sans-serif;letter-spacing:.12em;}
.scope {border-left:4px solid #7770b8;padding:10px 16px;background:#f0eef9;line-height:1.8;}
button:focus-visible,input:focus-visible,textarea:focus-visible {outline:3px solid #8f86d9!important;}
</style>""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("### 劳动合同 · 审查台")
    st.caption("企业签约前审核 / 本地原型")
    page = st.radio("工作区", ["本次报告", "审核合同", "模型设置"], label_visibility="collapsed", key="nav_page")
    st.divider()
    active = st.session_state.get("model_config")
    if active:
        st.caption("当前模型")
        st.text(active["provider"] + " / " + active["model"])
        st.caption("已填写配置 ≠ 已验证审核质量")
    st.caption("先看意见，再决定怎么改。")

if page == "本次报告":
    available = {key: value for key,value in RUNS.items() if (value[1] / "response.json").is_file()}
    labels = {key:value[0] for key,value in available.items()}
    if st.session_state.get("report"):
        labels = {"session":"本次材料的审核结果", **labels}
    if not labels:
        st.title("还没有审查报告")
        st.write("从左边「审核合同」读取材料，完成审核后再来这里看结果。")
        st.stop()
    preferred = st.query_params.get("run")
    names=list(labels)
    choice=st.selectbox("查看报告", names, index=names.index(preferred) if preferred in names else 0, format_func=lambda x:labels[x])
    if choice == 'session':
        report=st.session_state.report
        contract=st.session_state.get('report_contract')
        sources=SOURCES
        key='reviews'
        stale=st.session_state.get('report_stale',False)
    else:
        folder=available[choice][1]
        try:
            bundle=load_review(folder/'request.json',folder/'response.json',model='gpt-5.6-sol')
        except (ValueError,KeyError,OSError,TypeError) as exc:
            st.error('这份报告暂时无法载入：'+str(exc));st.stop()
        report=bundle['report'];contract=bundle['contract']
        sources=bundle['sources']
        key='host_reviews_'+report['metadata']['request_digest']
        stale=False
    render_report(report,sources,contract,review_key=key,stale=stale)
    st.stop()

if page == "模型设置":
    st.markdown('<div class="desk-label">连接你要用的模型</div>', unsafe_allow_html=True)
    st.title("模型提供方")
    st.write("换模型，不换审核清单。密钥只放在当前页面会话里，不写进文件或导出报告。")
    left, right = st.columns([1, 2.7], gap="large")
    with left:
        provider = st.radio("服务商", list(PRESETS), key="provider_choice")
        st.caption("本版仅接 Chat Completions 兼容协议。原生 Claude / Gemini 协议尚未接入。")
    with right:
        st.subheader(provider)
        profiles = st.session_state.setdefault("profiles", {})
        previous = profiles.get(provider, {})
        default_url, default_model = PRESETS[provider]
        with st.form("provider_form_" + provider + str(st.session_state.get("config_epoch", 0))):
            endpoint = st.text_input("接口地址（Base URL）", value=previous.get("base_url", default_url), placeholder="https://你的服务地址/v1")
            api_key = st.text_input("API 密钥", value=previous.get("api_key", ""), type="password")
            model = st.text_input("模型 ID", value=previous.get("model", default_model), placeholder="填写服务商给出的完整模型 ID")
            st.caption("模型 ID 请按服务商提供的名称填写。连接测试会发送一句公开测试文字，可能产生少量调用费用，不会发送合同。")
            c1, c2 = st.columns(2)
            save = c1.form_submit_button("保存为当前模型", type="primary")
            test = c2.form_submit_button("测试连接（发一次请求）")
        if save or test:
            candidate = {"provider": provider, "base_url": endpoint.strip(), "api_key": api_key.strip(), "model": model.strip()}
            if not all(candidate[k] for k in ("base_url", "api_key", "model")):
                st.error("请填好接口地址、API 密钥和模型 ID。")
            elif save:
                profiles[provider] = candidate
                st.session_state.model_config = candidate.copy()
                st.success("配置已保存到当前会话；可以回到「审核合同」。尚未发出请求。")
            else:
                try:
                    with st.spinner("正在测试一次连接，不发送合同……"):
                        response = chat_completion(candidate, [{"role": "user", "content": "连接测试，请只回复 OK。"}])
                    st.success("接口已返回文本。此测试不代表合同审核质量已经验收。")
                    st.text(response[:300])
                except (ValueError, RuntimeError) as exc:
                    st.error(str(exc))
        st.button("清除会话内所有模型密钥", on_click=clear_credentials)
    st.stop()

st.title("审核已有合同")
st.write("读取全文，检查必备内容与选定的三个主题；给局部修改建议，不从空白生成整份合同。")
st.caption("这个旧版劳动合同入口没有当前法源资料；规则预检会提示资料不足。通用合同审核请使用阅读台，历史报告仍读取各次冻结资料。")
choices=['简约合同样例','表格合同样例','上传 DOCX','粘贴文本']
saved_source=st.session_state.get('saved_input_source',choices[0])
input_mode=st.radio('合同来源',choices,index=choices.index(saved_source) if saved_source in choices else 0,horizontal=True)
st.session_state.saved_input_source=input_mode
raw=None;doc_name=input_mode
if input_mode=='上传 DOCX':
    uploaded=st.file_uploader('选择合同（最大5MB）',type=['docx'])
    if uploaded: raw=uploaded.getvalue();doc_name=uploaded.name
elif input_mode=='粘贴文本':
    raw=st.text_area('合同正文',height=180,max_chars=100000)
elif input_mode=='简约合同样例':raw=MINIMAL_TEXT
elif input_mode=='表格合同样例':
    if 'table_sample_bytes' not in st.session_state:st.session_state.table_sample_bytes=table_docx()
    raw=st.session_state.table_sample_bytes
input_signature=digest({'source':input_mode,'content':hashlib.sha256(raw).hexdigest() if isinstance(raw,bytes) else raw})
if st.button('读取合同',type='primary'):
    clear_input()
    try:
        if not raw:raise ValueError('请先选择文件或填写正文。')
        st.session_state.contract=parse_docx(raw) if isinstance(raw,bytes) else parse_text(raw)
        st.session_state.document_headings=docx_headings(raw) if isinstance(raw,bytes) else {}
        st.session_state.loaded_signature=input_signature
    except (ValueError,RuntimeError) as exc:st.error(str(exc))
if 'contract' not in st.session_state:st.stop()
if st.session_state.loaded_signature!=input_signature:
    st.session_state.report_stale=True
    st.warning('材料已变化，请重新读取合同。');st.stop()
contract=st.session_state.contract
with st.expander('替换敏感文字（可选）'):
    replacements=st.text_area('每行填写：原文字=>替换文字',height=80)
pieces=[p.model_dump() for p in contract.pieces]
for line in replacements.splitlines():
    if '=>' in line:
        old,new=line.split('=>',1)
        if old.strip():
            for p in pieces:p['text']=p['text'].replace(old.strip(),new.strip())
effective=ParsedContract(pieces=pieces,warnings=contract.warnings,fingerprint=digest(pieces))
left,right=st.columns([1.2,1],gap='large')
with left:
    st.subheader('确认送审文字')
    _reader(**reader_markup(effective,[],None),key='preview_'+input_signature,default=None)
    for warning in effective.warnings:st.warning(warning)
with right:
    st.subheader('这次审核的背景')
    saved=st.session_state.get('saved_background',{})
    regions=['上海','其他 / 尚未确认'];kinds=['模板审阅','拟签文本审阅']
    region=st.selectbox('适用地区',regions,index=regions.index(saved.get('region','上海')))
    review_date=st.date_input('审核基准日',value=date.fromisoformat(saved.get('review_date','2026-09-20')))
    input_kind=st.selectbox('材料状态',kinds,index=kinds.index(saved.get('input_kind','模板审阅')))
    payday=st.number_input('每月发薪日（0表示未确认）',min_value=0,max_value=31,value=saved.get('payday',0))
    goal=st.text_area('公司希望这次解决什么（可选）',value=saved.get('business_goal',''),max_chars=4000,height=100)
    st.session_state.saved_background={'region':region,'review_date':review_date.isoformat(),'input_kind':input_kind,'payday':payday,'business_goal':goal}
    context={'region':region,'review_date':review_date.isoformat(),'input_kind':input_kind,'payday':int(payday) or None}
    if goal.strip():context['business_goal']=goal
    config=st.session_state.get('model_config')
    signature=digest({'contract':effective.fingerprint,'context':context,'sources':SOURCES,'model':{k:config[k] for k in ('provider','base_url','model')} if config else None})
    if st.button('本地规则预检（不调用 AI）'):
        try:
            result=review_detailed(effective,context,SOURCES);result['document_name']=doc_name
            save_report(result,signature,effective)
        except (ValueError,RuntimeError) as exc:st.error(str(exc))
    if config:
        st.caption('发送至：'+config['provider']+' / '+config['model'])
        consent=st.checkbox('我已检查送审文字，同意发送给以上模型服务商',key='consent_'+signature)
        if st.button('开始 AI 审核',type='primary',disabled=not consent):
            st.session_state.pop('report',None)
            try:
                with st.spinner('正在审核并校验引用……'):
                    result=review_detailed(effective,context,SOURCES,config);result['document_name']=doc_name
                    save_report(result,signature,effective)
            except (ValueError,RuntimeError) as exc:st.error(str(exc))
    else:st.caption('需要独立调用模型时，到「模型设置」填写接口。已有的真实Sol报告可从「本次报告」查看。')
if st.session_state.get('report'):
    if st.session_state.report_signature!=signature:
        st.session_state.report_stale=True;st.warning('材料或设置已改变，原报告已过期，请重新审核。')
    else:
        st.success('结果已生成。')
        st.button('查看审查报告',type='primary',on_click=lambda:st.session_state.update(nav_page='本次报告'))
