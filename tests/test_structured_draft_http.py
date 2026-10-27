"""New report -> deterministic draft preview -> native Word, through real HTTP."""
import copy
import http.client
import io
import json
import sys
import time
import unittest
import zipfile

from docx import Document
from contract_core import parse_docx
from tests import test_checklist_workspace_integration as fixtures


class StructuredDraftHTTPTests(unittest.TestCase):
    def setUp(self):
        # The injected post-review reply uses the scope-1 shape that earlier records replay.
        from unittest.mock import patch
        scope = patch("post_review_information.SCOPE_VERSION", 1)
        scope.start()
        self.addCleanup(scope.stop)
        self.f = fixtures.ChecklistWorkspaceHTTPTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        document = Document()
        for text in ['1.1 甲方验收后支付全部货款。', '1.2 乙方按约定交付设备。', '8.1 未尽事项双方协商。']:
            document.add_paragraph(text)
        stream = io.BytesIO()
        document.save(stream)
        self.f.source = stream.getvalue()
        self.new_text = '1.1 甲方验收合格并收到合规发票后支付全部货款。'
        self.new_clause = '8.2 双方通过指定邮箱发出履行通知。'
        self.needs_input = False
        self.malformed = False
        self.phases = []
        self.affected_groups = []

        def runner(messages, config):
            phase = config.get('review_phase', 'review')
            self.phases.append(phase)
            if phase == 'review_preparation':
                return json.dumps({'questions': [], 'requirements': [], 'additional_checks': []})
            packet = json.loads(messages[1]['content'])
            if phase == 'post_review_information':
                self.assertIn('收到发票再付款', json.dumps(packet, ensure_ascii=False))
                return json.dumps({'findings': [self.finding(False)], 'group_results': [
                    {'group_id': gid, 'status': '发现风险', 'reason': '已结合付款顺序补充了完整改稿。',
                     'finding_ids': ['F01']} for gid in self.affected_groups]}, ensure_ascii=False)
            plan = self.finding(self.needs_input)
            checks = packet['review_preparation']['checks']
            self.affected_groups = [g['group_id'] for c in checks for g in c['groups']]
            rows = [{'check_id': c['id'], 'title': c['title'], 'status': plan['status'],
                     'reason': '合成工程检查。', 'finding_ids': ['F01'], 'groups': [
                         {'group_id': g['group_id'], 'title': g['title'], 'status': plan['status'],
                          'reason': '合成工程检查。', 'finding_ids': ['F01']} for g in c['groups']]}
                    for c in checks]
            return json.dumps({'findings': [plan], 'checks': [{'rule_id': 'G01', 'title': '付款和通知',
                'status': plan['status'], 'reason': '合成工程检查。'}], 'options': [], 'uncovered': [],
                'limitations': [], 'checklist_results': rows}, ensure_ascii=False)
        self.f.server.jobs.runner = runner
        self.f.server.drafts.runner = None

    def finding(self, pending=False):
        plan = {'status': 'needs_input' if pending else 'ready',
                'edits': [] if pending else [{'piece_id': 'P000', 'new_text': self.new_text}],
                'insertions': [] if pending else [{'after_piece_id': 'P002', 'new_text': self.new_clause}],
                'paragraph_actions': [
                    {'piece_id': 'P000', 'action': 'needs_confirmation' if pending else 'modify',
                     'reason': '请确认发票和付款的先后顺序。' if pending else '明确收到发票后才支付货款。'},
                    {'piece_id': 'P001', 'action': 'retain', 'reason': '交付义务不受本次付款与通知约定影响。'}],
                'questions': ['是否约定收到发票后才付款？'] if pending else [],
                'comment': '请确认付款条件和通知方式。'}
        if self.malformed:
            plan['edits'][0]['piece_id'] = 'DOES_NOT_EXIST'
        return {'id': 'F01', 'rule_id': 'G01', 'theme': '付款和通知', 'title': '明确付款前提及通知方式',
                'issue_kind': '信息' if pending else '商业', 'status': '信息不足' if pending else '发现风险',
                'risk': None if pending else '中', 'refs': [{'id': 'P000'}, {'id': 'P001'}],
                'location_note': '', 'source_ids': [], 'reason': '付款条件与送达规则需要明确。',
                'suggestion': '明确付款前提，补充通知条款。', 'questions': plan['questions'],
                'draft_plan': plan}

    def raw(self, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.f.server.server_port, timeout=15)
        conn.request('GET' if body is None else 'POST', path,
                     None if body is None else json.dumps(body).encode(),
                     {'Origin': f'http://127.0.0.1:{self.f.server.server_port}', 'Content-Type': 'application/json'})
        response = conn.getresponse()
        status, data = response.status, response.read()
        conn.close()
        return status, data

    def initial(self, expected='completed'):
        jid = self.f.create()
        self.f.request('/api/jobs/' + jid + '/prepare', self.f.payload)
        self.f.wait_status(jid, 'prepared')
        self.f.request('/api/jobs/' + jid + '/start', self.f.payload)
        self.f.wait_status(jid, expected)
        return jid

    def gate(self, jid):
        return self.f.request('/api/reports/' + jid + '/information-gate')['information_gate']

    def answer(self, jid, text=''):
        gate = self.gate(jid)
        payload = {'expected_revision': gate['revision'], 'expected_facts_revision': gate['facts_revision'],
                   'answers': [{'question_id': q['id'], 'status': 'answered' if text else 'unknown',
                                'answer': text} for q in gate['questions']]}
        if text:
            payload.update(self.f.payload)
        self.f.request('/api/reports/' + jid + '/information-gate', payload)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and self.gate(jid)['status'] == 'running':
            time.sleep(.02)
        self.assertEqual(self.gate(jid)['status'], 'ready', self.gate(jid))

    def confirm_and_export(self, jid):
        endpoint = '/api/reports/' + jid
        report = self.f.request(endpoint)
        self.assertEqual(report['report']['findings'][0]['draft_plan']['status'], 'ready')
        calls_before = list(self.phases)
        candidate = self.f.request(endpoint + '/drafts/F01')
        self.assertEqual(candidate['status'], 'ready', candidate)
        self.assertTrue(candidate['coverage_complete'])
        self.assertEqual(len(candidate['edits']), 1)
        self.assertEqual(len(candidate['insertions']), 1)
        self.assertEqual([r['action'] for r in candidate['paragraph_actions']], ['modify', 'retain'])
        self.assertEqual(self.f.request(endpoint + '/drafts/F01')['candidate_id'], candidate['candidate_id'])
        delivery = self.f.request(endpoint + '/delivery')
        edits = [{k: v for k, v in dict(row, id=f'edit-http-{i}', confirmed=True, finding_ids=['F01']).items()
                  if k != 'location'} for i, row in enumerate(candidate['edits'] + candidate['insertions'])]
        saved = self.f.request(endpoint + '/delivery', {
            'expected_revision': delivery['state']['revision'], 'source_sha256': delivery['source']['source_sha256'],
            'author': '合成验收', 'edits': edits, 'comments': [],
            'draft_confirmation': {'finding_id': 'F01', 'candidate_id': candidate['candidate_id']}})['state']
        self.assertEqual(saved['draft_confirmations']['F01']['paragraph_actions'][1]['action'], 'retain')
        status, raw = self.raw(endpoint + '/delivery/export', {
            'expected_revision': saved['revision'], 'source_sha256': delivery['source']['source_sha256'],
            'review_revision': report['state']['revision'], 'variants': ['clean', 'tracked']})
        self.assertEqual(status, 200, raw[:1000])
        with zipfile.ZipFile(io.BytesIO(raw)) as files:
            clean = files.read(next(name for name in files.namelist() if '洁净版' in name))
        self.assertEqual([p.text for p in parse_docx(clean).pieces],
                         [self.new_text, '1.2 乙方按约定交付设备。', '8.1 未尽事项双方协商。', self.new_clause])
        self.assertEqual(self.phases, calls_before, 'Preview, confirmation and export must not call a model')

    def test_new_report_direct_preview_confirm_and_word_without_codex(self):
        jid = self.initial()
        self.confirm_and_export(jid)
        self.assertEqual(self.phases, ['review_preparation', 'review'])

    def test_unknown_information_stays_pending_in_report_not_a_ready_draft(self):
        self.needs_input = True
        jid = self.initial()
        self.assertEqual(self.gate(jid)['status'], 'needs_response')
        self.answer(jid)
        report = self.f.request('/api/reports/' + jid)
        finding = report['report']['findings'][0]
        self.assertEqual(finding['draft_plan']['status'], 'needs_input')
        self.assertFalse(finding['clause_draft'])
        candidate = self.f.request('/api/reports/' + jid + '/drafts/F01')
        self.assertEqual(candidate['status'], 'needs_input')
        self.assertFalse(candidate['edits'])
        self.assertTrue(candidate['questions'])
        self.assertEqual(self.phases, ['review_preparation', 'review'])

    def test_postreview_answer_produces_direct_draft_before_report_delivery(self):
        self.needs_input = True
        jid = self.initial()
        self.answer(jid, '收到发票再付款，通知使用双方指定邮箱。')
        self.confirm_and_export(jid)
        self.assertEqual(self.phases, ['review_preparation', 'review', 'post_review_information'])

    def test_invalid_plan_never_published_as_ready(self):
        self.malformed = True
        jid = self.initial('failed')
        report = self.f.request('/api/reports/' + jid)
        finding = report['report']['findings'][0]
        self.assertEqual(finding['status'], '处理失败')
        self.assertFalse(finding.get('draft_plan', {}).get('status') == 'ready')
        self.assertFalse(finding['clause_draft'])
        self.assertEqual(self.phases, ['review_preparation', 'review'])


if __name__ == '__main__':
    if '--serve' in sys.argv:
        fixture = StructuredDraftHTTPTests()
        fixture.setUp()
        try:
            ready = fixture.initial()
            fixture.needs_input = True
            pending = fixture.initial()
            fixture.answer(pending)
            fixture.needs_input = False
            print(json.dumps({'port': fixture.f.server.server_port, 'ready': ready, 'pending': pending,
                              'root': str(fixture.f.base)}), flush=True)
            input()
        finally:
            fixture.doCleanups()
    else:
        unittest.main()
