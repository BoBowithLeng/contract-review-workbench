"""Deterministic, client-readable Word reports from existing review results.

Export never reviews a contract or confirms a suggested edit. Source numbering
is presentation metadata, not newly invented contract text.
"""
import io
import re

from docx import Document
from document_view import docx_navigation, index_document, sort_findings_by_document
from report_presentation import ReportWriter, chinese_number, source_locations

# Same wording as the checklist workspace (resultStatus in reader_ui/checklists.js),
# so the delivered report never names a state differently from the screen.
CHECK_STATUS_LABELS = {
    '未检出': '未发现问题', '发现风险': '发现问题', '发现问题': '发现问题',
    '信息不足': '待确认', '需要法务判断': '待确认', '待确认': '待确认', '待核实': '待确认',
    '不适用': '不适用', '处理失败': '尚未完成检查', '未完成': '尚未完成检查', '未检查': '尚未完成检查',
}


def check_status_label(status):
    status = status or '未完成'
    return CHECK_STATUS_LABELS.get(status, status)


def whole_paragraph(text, edits):
    """Apply confirmed ranges to the source paragraph, or None if they no longer match it."""
    if text is None:
        return None
    ranges = sorted(edits, key=lambda e: e.get('start', -1), reverse=True)
    result, limit = text, len(text)
    for edit in ranges:
        start, end = edit.get('start'), edit.get('end')
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= limit:
            return None
        if text[start:end] != (edit.get('old_text') or ''):
            return None
        result = result[:start] + (edit.get('new_text') or '') + result[end:]
        limit = start
    return result


def export_docx(report, sources, contract, reviews=None, selected_options=None, reader_mode=False,
                reader_actions=None, supplemental_cases=None, source_docx: bytes = None,
                confirmed_plan=None):
    """Keep the legacy arguments; optionally recover numbering from original DOCX."""
    reviews, reader_actions = reviews or {}, reader_actions or {}
    selected_options, supplemental_cases = selected_options or [], supplemental_cases or {}
    # Do not alter the archived model input, source text, or its fingerprint.
    view = dict(contract.model_dump() if hasattr(contract, 'model_dump') else contract)
    if source_docx is not None:
        view.update(docx_navigation(source_docx))
    indexed = index_document(view)
    locations = source_locations(indexed)
    findings = (sort_findings_by_document(report.get('findings', []), view)
                if reader_mode else list(report.get('findings', [])))
    finding_labels = {f.get('id', f.get('rule_id', '')): f'意见{chinese_number(i)}'
                      for i, f in enumerate(findings, 1)}
    rule_labels = {}
    for f in findings:
        rule_labels.setdefault(f.get('rule_id', ''), f.get('theme') or f.get('title') or '相关审查项')
    for i, check in enumerate(report.get('checklist_results') or [], 1):
        if check.get('check_id'):
            rule_labels[check['check_id']] = '清单第' + chinese_number(i) + '项'
    for pid, location in locations.items():
        table = re.match(r'^(T\d+)R', pid)
        if table:
            rule_labels.setdefault(table[1], re.sub(r'第\d+行第\d+列第\d+段$', '', location))
    writer = ReportWriter(Document(), locations, finding_labels, rule_labels)
    doc = writer.doc
    source_map = {s['id']: s for s in sources or []}
    ctx = report.get('context') or {}
    kind = ctx.get('contract_type')
    default_name = (ctx.get('contract_type_label') or report.get('contract_type_label') or '合同')
    if kind == 'software_service':
        default_name = '小程序开发服务合同'
    elif kind not in {'general', 'software_service'}:
        default_name = '劳动合同'
    # A known contract title takes precedence over a generic classifier label.
    first = next((p['text'].strip() for p in indexed if p['text'].strip()), '')
    title_name = first if len(first) <= 48 and first.endswith(('合同', '协议', '协议书', '合同书')) else default_name
    writer.title(title_name + '审查报告')
    role = {'client': '客户', 'provider': '服务商'}.get(ctx.get('our_role'), ctx.get('our_role', ''))
    party = ctx.get('our_party', '')
    writer.meta('审查立场  ' + (party + ('（' + role + '）' if role else '') or '待确认')
                + '    审查日期  ' + ctx.get('review_date', '待确认'))
    if report.get('document_name') or ctx.get('document_name'):
        writer.meta('材料：' + (report.get('document_name') or ctx['document_name']))
    if ctx.get('region'):
        writer.meta('地区：' + ctx['region'] + ('    我方角色：' + role if role else ''))
    if ctx.get('business_goal'):
        writer.body('本次审查要求：' + ctx['business_goal'])
    writer.body(f'本报告列示{len(findings)}项审查意见，具体风险、修改建议及处理状态见下文。')
    if reader_mode:
        writer.body('建议文字供核对，合同以已确认的修改为准。不采用修改建议不表示原风险已解决。')
        confirmed = confirmed_plan or {}
        if any(e.get('confirmed') is True and e.get('new_text') != e.get('old_text') for e in confirmed.get('edits') or []) or any(c.get('confirmed') is True for c in confirmed.get('comments') or []):
            writer.body('已确认的修改与批注见报告末尾。')
    rereview = (report.get('metadata') or {}).get('rereview')
    if rereview:
        writer.body(f"第{rereview.get('round_number', '')}轮审查：基于前次已确认修改后的完整合同及本轮有效信息复审。")

    writer.section('审查意见概览')
    processing = {}
    for finding in findings:
        fid = finding.get('id', finding.get('rule_id', ''))
        if reader_mode:
            action = reader_actions.get(fid) or {}
            processing[fid] = action.get('label', action.get('status', '待处理'))
        else:
            processing[fid] = (reviews.get(fid) or {}).get('decision', '未复核')
    writer.overview(findings, processing)
    writer.section('详细审查意见')
    if not findings:
        writer.body('本次未返回具体问题；请结合基本要素、未完成检查和检查范围阅读。')
    for i, finding in enumerate(findings, 1):
        fid = finding.get('id', finding.get('rule_id', ''))
        human = reviews.get(fid) or {}
        writer.subheading(f'（{chinese_number(i)}）' + finding.get('title', '审查意见'))
        risk = finding.get('risk')
        risk_line = '风险等级：' + (str(risk) + ('' if str(risk).endswith('风险') else '风险') if risk else '待判断')
        if finding.get('status') and finding['status'] != '发现风险':
            risk_line += '；' + finding['status']
        writer.meta(risk_line).paragraph_format.keep_with_next = True
        update = finding.get('post_review_update')
        if isinstance(update, dict) and update.get('reason'):
            # The questions and answers themselves are listed under 本次合同的补充信息.
            writer.body('补答后更新：' + update['reason'])
        if finding.get('opinion_revision'):
            label = '讨论后撤回' if finding.get('revision_action') == 'withdraw' else '讨论更新'
            writer.body(f"意见版本：{label}（第{finding['opinion_revision']}次确认更新）")
            writer.optional('本次调整：', finding.get('revision_summary'))
        writer.detail_heading('1. 合同原文')
        refs = finding.get('refs') or []
        if not refs:
            writer.body(finding.get('location_note') or '本项未定位到相关文字。')
        for ref in refs:
            writer.located(ref.get('id'), ref.get('quote', ''), quote=True)
        writer.detail_heading('2. 为什么需要修改')
        writer.body(finding.get('reason') or '本项未提供进一步说明。')
        writer.detail_heading('3. 建议怎么改')
        writer.body(finding.get('suggestion') or '本项未提供具体修改办法。')
        plan = finding.get('draft_plan') or {}
        edits = plan.get('edits') or []
        insertions = plan.get('insertions') or []
        if edits or insertions or (not plan and finding.get('clause_draft')):
            writer.detail_heading('4. 建议修改文字')
            if edits or insertions:
                for edit in edits:
                    writer.located(edit.get('piece_id'), edit.get('new_text', ''))
                for insertion in insertions:
                    writer.located(insertion.get('after_piece_id'), insertion.get('new_text', ''), insertion=True)
            else:
                writer.body(finding['clause_draft'], raw=True)
        if plan:
            plan_status = {'needs_input': '待补充信息',
                           'not_needed': '无需修改正文'}.get(plan.get('status'))
            if plan_status:
                writer.meta('修改建议状态：' + plan_status)
            writer.optional('修改说明：', plan.get('comment'))
            for row in plan.get('paragraph_actions') or []:
                if row.get('action') == 'retain':
                    writer.body('保留' + writer.location(row.get('piece_id')) + '：' + row.get('reason', ''))
            for question in plan.get('questions') or []:
                writer.body('待确认：' + str(question))
        for question in finding.get('questions') or []:
            if question not in (plan.get('questions') or []):
                writer.body('待确认：' + (question.get('question', '') if isinstance(question, dict) else str(question)))
        if finding.get('evidence_status') in {'资料不足', '适用待判断'}:
            writer.body('资料核对状态：' + finding['evidence_status'])
        writer.sources(finding.get('source_ids') or [], source_map)
        cases = supplemental_cases.get(fid) or []
        if cases:
            writer.detail_heading('补充案例')
            writer.meta('以下案例为审查后补充，未参与原审查。')
            for case in cases:
                source_kind = case.get('source_kind_label') or ('官方案例摘要' if case.get('source_kind') == 'official_case_summary' else '裁判文书')
                decision_kind = case.get('decision_kind_label') or {'court': '法院裁判', 'arbitration': '仲裁裁决'}.get(case.get('decision_kind'), '决定类型未披露')
                writer.body(case.get('title', '未命名案例') + '（' + source_kind + '；' + decision_kind + '）')
                for key, label in [('facts', '案情：'), ('holding', '裁判要点：'), ('relevance', '与本意见的关系：'), ('limits', '局限：'), ('url', '原文链接：')]:
                    writer.optional(label, case.get(key))
        if human and human.get('decision') not in {None, '', '未复核'}:
            label = '历史复核记录（不等于正文操作）：' if reader_mode else '人工复核记录：'
            writer.body(label + human['decision'] + '；风险等级：' + human.get('risk', '待判断') + '；说明：' + human.get('note', ''))
            if human.get('clause_draft'):
                writer.body(('历史复核建议文字：' if reader_mode else '复核建议文字：') + human['clause_draft'], raw=True)

    options = report.get('options') or []
    if options:
        writer.section('可选择的处理办法')
        for i, option in enumerate(options, 1):
            status = ('（历史曾选择）' if reader_mode else '（已选）') if option.get('id') in selected_options else '（模型备选）' if reader_mode else ''
            writer.subheading(f'（{chinese_number(i)}）' + (str(option['id']) + ' ' if option.get('id') else '') + option.get('title', '处理办法') + status)
            if option.get('need_review'):
                writer.body('关联意见已更新，本方案需重新核对。')
            writer.links(option.get('finding_ids'))
            writer.optional('适用条件：', '；'.join(option.get('preconditions') or []))
            for j, step in enumerate(option.get('steps') or [], 1):
                writer.body(f'{j}. ' + step)
            for key, label in [('cost', '成本：'), ('remaining_risk', '剩余风险：'), ('relationship', '与其他方案的关系：')]:
                writer.optional(label, option.get(key))
            if option.get('clause_draft'):
                value = option['clause_draft']
                if re.match(r'^(?:以|参见|见)\s*F\d+.*(?:项下|修改稿|修改文本|新增段落)', value):
                    # Explicit pointers in old options are explanatory prose,
                    # unlike real contract wording that may contain product IDs.
                    value = writer.clean(value)
                    value = re.sub(r'项下\s*(?:draft_plan|建议改稿)\s*的修改稿', '中的建议修改文字', value)
                writer.body('建议条款：' + value, raw=True)
            writer.sources(option.get('source_ids') or [], source_map)

    current_facts = report.get('confirmed_facts')
    facts = [f.get('text', '') for f in (current_facts or {}).get('facts', []) if f.get('active', True) and f.get('text')]
    preparation = report.get('preparation') or {}
    answered = [q for q in preparation.get('questions', []) if q.get('status') == 'answered' and q.get('answer')] if current_facts is None else []
    business_facts = ctx.get('business_facts') if current_facts is None else ''
    if facts or answered or business_facts:
        writer.section('本次合同的补充信息')
        writer.optional('已提供的交易信息：', business_facts)
        for fact in facts:
            writer.body(fact)
        for question in answered:
            writer.body(question.get('question', '补充问题'))
            writer.body('回答：' + str(question['answer']))
    pending = [q for q in (current_facts or {}).get('questions', []) if q.get('status') != 'answered' and q.get('question')]
    limitations, uncovered = report.get('limitations') or [], report.get('uncovered') or []
    research = (report.get('metadata') or {}).get('research') or {}
    if pending or limitations or uncovered or research.get('status') in {'error', 'partial', 'no_results'}:
        writer.section('尚待确认的信息与检查范围')
        if research.get('status') in {'error', 'partial', 'no_results'}:
            writer.body('联网查证未完整取得新资料，待核资料在引用处标明。')
        for question in pending:
            writer.body('待确认：' + question['question'])
        for text in limitations:
            writer.body(text)
        if uncovered:
            writer.subheading('未展开的检查范围')
            for text in uncovered:
                writer.body(text)

    completeness = report.get('completeness') or []
    basic = report.get('basic_info') or {}
    # General and software reviews carry only a placeholder description here;
    # the reader hides this tab for them unless real basics were checked.
    if completeness or (basic.get('description') and kind not in {'general', 'software_service'}):
        writer.section('附录 基本要素检查')
        if not completeness:
            writer.body(basic.get('description'))
        writer.sources(basic.get('source_ids') or [], source_map)
        for item in completeness:
            writer.body(item.get('title', '基本要素') + '：' + item.get('status', '未完成') + '。' + item.get('note', ''))
            for ref in item.get('refs') or []:
                writer.located(ref.get('id'), ref.get('quote', ''), quote=True)
    results, checks = report.get('checklist_results') or [], report.get('checks') or []
    if results or checks:
        writer.section('附录 清单逐项检查结果')
        name = report.get('checklist_name')
        writer.body(('以下按本次选用的清单「' + name + '」' if name else '以下按本次审查所用清单')
                    + '逐项列出检查结果；标为“发现问题”的项目，具体意见见正文对应编号。')
        sources_by_check = {c.get('id'): c.get('source') for c in preparation.get('checks', [])}
        for i, check in enumerate(results, 1):
            prefix = '本次审查要求 ' if sources_by_check.get(check.get('check_id')) == 'request' else ''
            writer.subheading(f'（{chinese_number(i)}）' + prefix + (check.get('title') or '检查项') + '：' + check_status_label(check.get('status')))
            group_reasons = '\n'.join(g.get('reason', '') for g in check.get('groups') or [])
            # Only remove a demonstrably duplicate top-level explanation. No
            # model summarization and no loss of a unique business conclusion.
            if check.get('reason') and check['reason'] not in group_reasons:
                writer.optional('检查结果：', check['reason'])
            writer.links(check.get('finding_ids'))
            for j, group in enumerate(check.get('groups') or [], 1):
                writer.body(f"{j}. {group.get('title') or '检查内容'}：{check_status_label(group.get('status'))}。{group.get('reason', '')}")
                writer.links(group.get('finding_ids'))
        represented = '\n'.join(str(row.get(key, '')) for row in results for key in ('title', 'reason'))
        represented += '\n' + '\n'.join(str(group.get(key, '')) for row in results for group in row.get('groups') or [] for key in ('title', 'reason'))
        independent_checks = [check for check in checks if not results or check.get('status') == '处理失败'
                              or not ((check.get('title') or rule_labels.get(check.get('rule_id')) or '') in represented
                                      and (check.get('reason') or '') in represented)]
        if independent_checks:
            writer.subheading('各审查主题的检查状态')
            for check in independent_checks:
                label = check.get('title') or rule_labels.get(check.get('rule_id')) or '审查主题'
                writer.body(label + '：' + check_status_label(check.get('status')) + '。' + check.get('reason', ''))

    confirmed = confirmed_plan or {}
    actual_edits = [e for e in confirmed.get('edits') or []
                    if e.get('confirmed') is True and e.get('new_text') != e.get('old_text')]
    comments = [c for c in confirmed.get('comments') or [] if c.get('confirmed') is True]
    if actual_edits or comments:
        writer.section('已确认的修改与批注')
        edits_by_id = {e.get('id'): e for e in confirmed.get('edits') or []}
        texts = {p['id']: p['text'] for p in view['pieces']}
        # One entry per changed paragraph, read as whole sentences before and
        # after; several confirmed ranges in one paragraph are applied together.
        entries = []
        for edit in actual_edits:
            pid = edit.get('piece_id')
            group = next((e for e in entries if e[0] == pid and edit.get('kind') != 'insert_paragraph_after'
                          and e[1][0].get('kind') != 'insert_paragraph_after'), None)
            if group:
                group[1].append(edit)
            else:
                entries.append((pid, [edit]))
        for i, (pid, edits) in enumerate(entries, 1):
            writer.subheading(f'（{chinese_number(i)}）' + writer.location(pid))
            if edits[0].get('kind') == 'insert_paragraph_after':
                writer.located(pid, edits[0].get('new_text', ''), insertion=True, prefix='已确认文字：')
            else:
                after = whole_paragraph(texts.get(pid), edits)
                if after is None:
                    for edit in edits:
                        if edit.get('old_text'):
                            writer.body('原文：' + edit['old_text'], raw=True)
                        writer.located(pid, edit.get('new_text') or '（删除）', prefix='已确认文字：')
                else:
                    writer.labeled('修改前：', '“' + texts[pid] + '”')
                    writer.labeled('修改后：', '“' + after + '”' if after.strip() else '（整段删除）')
            writer.links(list(dict.fromkeys(fid for edit in edits for fid in edit.get('finding_ids') or [])))
        if comments:
            writer.subheading('已确认批注')
            for comment in comments:
                edit = edits_by_id.get(comment.get('edit_id')) or {}
                writer.located(comment.get('piece_id') or edit.get('piece_id'), comment.get('text', ''), prefix='批注：')
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
