"""Two-pane original-text review with exact anchors and readable export."""
import json
from pathlib import Path
import streamlit as st
import streamlit.components.v1 as components
from completeness import check_completeness
from document_view import index_document, reader_markup
from report_export import export_docx

ROOT = Path(__file__).resolve().parent
_reader = components.declare_component("contract_source_reader", path=str(ROOT / "source_reader"))


def render_report(report, sources, contract=None, *, review_key, stale=False):
    st.title("合同审查")
    ctx = report["context"]
    name = report.get("document_name", ctx.get("document_name", "劳动合同"))
    st.caption(f"{name} · {ctx.get('region','地区待确认')} · {ctx.get('review_date','')}")
    if stale:
        st.warning("材料或设置已变化，请重新审核后再处理这份报告。")
        return
    if contract is None:
        st.error("缺少本次合同原文，请重新读取材料。")
        return
    findings = [{**f, "id": f.get("id", f["rule_id"])} for f in report.get("findings", [])]
    basics = check_completeness(contract, ctx)
    report = {**report, "findings":findings, "completeness":basics}
    items = findings + basics
    by_id = {item["id"]:item for item in items}
    reviews = st.session_state.setdefault(review_key, {})
    source_map = {s["id"]:s for s in sources}
    mode = report["mode"]
    if mode == "local_rules":
        st.info("本地预检：基础内容查找与少量明确写法检查，未调用AI。")
    elif not report.get("metadata", {}).get("model_called", True):
        st.warning("本次条件或资料未满足，模型没有运行。")
    else:
        st.caption(f"{len(findings)} 条审查意见 · 点击原稿中的标色位置，或从右侧选择意见。基础项为本地文字检查。")
    if mode == 'host_ai':
        st.caption('当前展示已保存的 Sol 真实审核结果；查看和复核不会再次调用模型。')
    failed_checks=[c for c in report.get('checks',[]) if c['status']=='处理失败']
    if failed_checks:
        st.warning('有检查未完成：'+'；'.join(c.get('reason','结果校验失败') for c in failed_checks))
    if report.get("metadata", {}).get("source_snapshot_current") is False:
        st.caption("重点意见保留本次模型运行时使用的资料快照。")
    focus_key = "focus_" + review_key
    preferred = st.query_params.get("issue")
    initial = next((f['id'] for f in findings if preferred in (f['id'],f['rule_id'])), items[0]['id'] if items else None)
    if st.session_state.get(focus_key) not in by_id:
        st.session_state[focus_key] = initial
    selected = st.session_state[focus_key]
    if selected is None:
        st.write("没有可展示的意见。")
        return
    item = by_id[selected]
    is_basic = selected in {b['id'] for b in basics}
    left, right = st.columns([1.2, 1], gap="large")
    with left:
        st.subheader("合同原稿")
        markup = reader_markup(contract, findings + ([item] if is_basic else []), selected)
        event = _reader(**markup, key="reader_"+review_key, default=None)
        if isinstance(event, dict) and event.get('seq') != st.session_state.get('reader_event_'+review_key):
            st.session_state['reader_event_'+review_key] = event.get('seq')
            if event.get('id') in by_id and event['id'] != selected:
                st.session_state[focus_key] = event['id']
                st.rerun()
        st.caption("橙色是当前选中原句；点击标色段下的意见按钮可联动右侧。目录来自原稿，未识别标题时按段落定位。")
    with right:
        st.subheader("意见与处理办法")
        st.selectbox("查看意见或必备项", list(by_id), key=focus_key,
            format_func=lambda x: ("基本项 · " if x.startswith('B') else ('本项未检出' if by_id[x].get('status')=='未检出' else by_id[x].get('risk') or '待判断')+' · ') + by_id[x]['title'])
        if is_basic:
            label = {"已找到":"有相关文字", "未找到":"未找到相关内容"}.get(item['status'],item['status'])
            st.markdown("### " + item['title'])
            st.write("**" + label + "**")
            st.text(item.get('note',''))
            if item.get('missing_fields'):
                st.write("需补充或确认："+'、'.join(item['missing_fields']))
            if not item.get('refs'):
                st.caption("没有可定位的原句；先确认是否另有附件或尚未填写，不编造内容。")
            st.caption("找到相关文字不代表条款内容已经合法。非必备章节不会因缺少而自动补入。")
        else:
            st.markdown("### " + item['title'])
            grade = item.get('risk') or '待判断'
            previous = reviews.get(selected, {})
            st.write('**本项未检出问题**' if item['status']=='未检出' else f"**原稿风险：{grade}**　·　{item['status']}")
            check = next((c for c in report.get('checks',[]) if c['rule_id']==item['rule_id']), None)
            if check and check['status']=='处理失败':
                st.warning('本主题有未完成部分：'+check.get('reason','请查看运行信息。'))
            if previous:
                st.caption("你的处理："+previous['decision']+' / '+previous.get('risk','待判断'))
            st.text(item.get('reason',''))
            if item.get('clause_draft'):
                st.markdown("**局部改稿**")
                st.text(item['clause_draft'])
            options = [o for o in report.get('options',[]) if selected in o.get('finding_ids',[])]
            if options:
                st.markdown("#### 可以怎样处理")
                selected_options = st.session_state.setdefault('options_'+review_key, [])
                for option in options:
                    with st.expander(option['title']):
                        st.text('适用：'+'；'.join(option.get('preconditions',[])))
                        for step in option.get('steps',[]):st.text('• '+step)
                        st.text('成本：'+option.get('cost',''))
                        st.text('剩余风险：'+option.get('remaining_risk',''))
                        st.text(option.get('relationship',''))
                        if option.get('clause_draft'):st.text(option['clause_draft'])
                        use = st.checkbox('选择这个办法', value=option['id'] in selected_options, key='option_'+review_key+option['id'])
                        if use and option['id'] not in selected_options:selected_options.append(option['id'])
                        elif not use and option['id'] in selected_options:selected_options.remove(option['id'])
            elif item.get('suggestion'):
                with st.expander("办理建议与待确认事项"):
                    st.text(item['suggestion'])
                    for q in item.get('questions',[]):st.text('• '+q)
            if st.button('接受这条意见',type='primary',disabled=item['status']=='处理失败',key='accept_'+review_key+selected):
                reviews[selected]={'decision':'接受','risk':grade,'note':'','clause_draft':item.get('clause_draft','')}
                st.rerun()
            with st.expander("修改等级、改稿或驳回"):
                with st.form('edit_'+review_key+selected):
                    decisions=['未复核','接受','修改','驳回'];risks=['待判断','高','中','低']
                    decision=st.selectbox('你的处理',decisions,index=decisions.index(previous.get('decision','未复核')))
                    risk=st.selectbox('你的风险等级',risks,index=risks.index(previous.get('risk',grade)))
                    note=st.text_area('复核理由',value=previous.get('note',''))
                    draft=st.text_area('你的条款版本',value=previous.get('clause_draft',item.get('clause_draft','')),height=160)
                    if st.form_submit_button('保存这条复核'):
                        if decision in ('修改','驳回') and not note.strip():st.error('请留一句修改或驳回的理由。')
                        elif decision=='接受' and item['status']=='处理失败':st.error('本项未完成检查，不能直接接受。')
                        else:
                            reviews[selected]={'decision':decision,'risk':risk,'note':note,'clause_draft':draft};st.rerun()
        with st.expander("原句位置与法律依据"):
            locations={p['id']:p['location'] for p in index_document(contract)}
            for ref in item.get('refs',[]):
                st.caption(locations.get(ref['id'],ref['id']));st.text(ref['quote'])
            for sid in item.get('source_ids',[]):
                if sid in source_map:
                    s=source_map[sid];st.write(s['title']+' · '+s['article']);st.text(s['text']);st.link_button('官方原文',s['url'])
    st.divider()
    selected_options=st.session_state.get('options_'+review_key,[])
    word=export_docx(report,sources,contract,reviews,selected_options)
    st.download_button("下载Word审查报告",word,"劳动合同审查报告.docx",'application/vnd.openxmlformats-officedocument.wordprocessingml.document',key='word_'+review_key)
    with st.expander("导出数据与检查范围"):
        st.download_button('下载完整数据（JSON）',json.dumps({**report,'human_reviews':reviews,'selected_options':selected_options},ensure_ascii=False,indent=2),'内部审查报告.json','application/json',key='json_'+review_key)
        st.caption('重点审查：续签、工资交接、保密与离职限制。必备要素检查不等于全面法律审核。')
        for text in report.get('uncovered',[]):st.text(text)
        if ctx.get('business_goal'):st.text('本次公司目标：'+ctx['business_goal'])
