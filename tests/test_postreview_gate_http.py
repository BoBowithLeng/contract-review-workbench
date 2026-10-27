"""Publication gate exercised over HTTP, with no paid model or production data."""
import copy
import http.client
import io
import json
import time
import unittest
import zipfile
from unittest.mock import patch

import test_checklist_workspace_integration as fixtures


class PostreviewGateHTTPTests(unittest.TestCase):
    def setUp(self):
        # These walk the scope-1 protocol that earlier records replay; scope 2 has its own test below.
        scope = patch("post_review_information.SCOPE_VERSION", 1)
        scope.start()
        self.addCleanup(scope.stop)
        self.f = fixtures.ChecklistWorkspaceHTTPTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.questions = True
        self.phases = []
        self.affected_groups = []
        self.group_still_pending = False
        self.first = {'id': 'F01', 'rule_id': 'G01', 'theme': '使用安排', 'title': '设备用途需要确认',
            'issue_kind': '信息', 'status': '信息不足', 'risk': None, 'refs': [{'id': 'P002'}],
            'location_note': '', 'source_ids': [], 'reason': '用途影响本项判断。', 'suggestion': '确认使用场景。',
            'clause_draft': '', 'questions': ['设备是否仅在内部展示使用？']}
        self.other = {'id': 'F02', 'rule_id': 'G02', 'theme': '交付', 'title': '交付期限需要明确',
            'issue_kind': '商业', 'status': '发现风险', 'risk': '低', 'refs': [{'id': 'P002'}],
            'location_note': '', 'source_ids': [], 'reason': '注入回复验证无关意见保留。', 'suggestion': '明确交付期限。',
            'clause_draft': '', 'questions': []}

        def runner(messages, config):
            phase = config.get('review_phase', 'review')
            self.phases.append(phase)
            if phase == 'review_preparation':
                return json.dumps({'questions': [], 'requirements': [], 'additional_checks': []})
            def structured(row):
                value = copy.deepcopy(row)
                if config.get('draft_plan_version') == 1:
                    value.pop('clause_draft', None)
                    pending = bool(value['questions'])
                    value['draft_plan'] = {'status': 'needs_input' if pending else 'not_needed',
                        'edits': [], 'insertions': [], 'questions': value['questions'],
                        'comment': '需先明确使用安排。' if pending else '本轮更新内部判断，原条款保留。',
                        'paragraph_actions': [{'piece_id': ref['id'], 'action': 'needs_confirmation' if pending else 'retain',
                            'reason': '使用安排缺失，暂不能定稿。' if pending else '本轮只调整内部判断，保留相关原文。'} for ref in value['refs']]}
                return value
            if phase == 'post_review_information':
                self.assertIn('内部展示', json.dumps(messages, ensure_ascii=False))
                corrected = {**self.first, 'status': '未检出', 'risk': None, 'questions': [],
                             'reason': '已说明只在内部展示，本项无需修改。', 'suggestion': ''}
                packet = json.loads(messages[1]['content'])
                if 'must_update_finding_ids' in packet:
                    also = {**self.other, 'reason': '内部展示无需对外交付，交付期限风险降低。', 'risk': '低'}
                    returned = {'F01', 'F02'}
                    groups = [g for check in packet['checklist_requirements'] for g in check['groups']
                              if returned.intersection(g['current_result'].get('finding_ids', []))]
                    question = packet['answered_question_ids'][0]
                    return json.dumps({'findings': [structured(corrected), structured(also)], 'group_results': [
                        {'group_id': g['group_id'], 'status': g['current_result']['status'] if 'F02' in g['current_result']['finding_ids'] else '未检出',
                         'reason': '已结合新答案核对本组要求。', 'finding_ids': g['current_result']['finding_ids']} for g in groups],
                        'update_basis': [
                            {'finding_id': 'F01', 'question_ids': [question], 'reason': '确认仅内部展示，用途问题解决。'},
                            {'finding_id': 'F02', 'question_ids': [question], 'reason': '仅内部展示，交付期限风险随之降低。'}]},
                        ensure_ascii=False)
                self.assertTrue(packet['affected_checklist_requirements'])
                return json.dumps({'findings': [structured(corrected)], 'group_results': [
                    {'group_id': gid, 'status': '信息不足' if self.group_still_pending else '未检出',
                     'reason': '另一个检查条件仍需确认。' if self.group_still_pending else '已结合新答案核对本组要求。',
                     'finding_ids': ['F01']} for gid in self.affected_groups]})
            packet = json.loads(messages[1]['content'])
            first = copy.deepcopy(self.first)
            if not self.questions:
                first['questions'] = []
                first['issue_kind'] = '商业'
                first['status'] = '未检出'
            rows = []
            self.affected_groups = [check['groups'][0]['group_id']
                                    for check in packet['review_preparation']['checks'] if check['groups']]
            for check in packet['review_preparation']['checks']:
                rows.append({'check_id': check['id'], 'title': check['title'], 'status': '发现风险',
                    'reason': '注入回复。', 'finding_ids': ['F01', 'F02'], 'groups': [
                        {'group_id': group['group_id'], 'title': group['title'],
                         'status': first['status'] if n == 0 else '发现风险', 'reason': '注入回复。',
                         'finding_ids': ['F01'] if n == 0 else ['F02']}
                        for n, group in enumerate(check['groups'])]})
            return json.dumps({'findings': [structured(first), structured(self.other)], 'checks': [
                {'rule_id': 'G01', 'title': '使用安排', 'status': first['status'], 'reason': '注入回复。'},
                {'rule_id': 'G02', 'title': '交付', 'status': '发现风险', 'reason': '注入回复。'}],
                'options': [], 'uncovered': [], 'limitations': [], 'checklist_results': rows})

        self.f.server.jobs.runner = runner

    def raw_request(self, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.f.server.server_port, timeout=10)
        conn.request('GET' if body is None else 'POST', path,
            None if body is None else json.dumps(body).encode(),
            {'Origin': f'http://127.0.0.1:{self.f.server.server_port}', 'Content-Type': 'application/json'})
        response = conn.getresponse()
        data = response.read()
        status = response.status
        conn.close()
        return status, json.loads(data)

    def initial(self):
        jid = self.f.create()
        self.f.request('/api/jobs/' + jid + '/prepare', self.f.payload)
        self.f.wait_status(jid, 'prepared')
        self.f.request('/api/jobs/' + jid + '/start', self.f.payload)
        self.f.wait_status(jid, 'completed')
        return jid

    def gate(self, jid):
        return self.f.request('/api/reports/' + jid + '/information-gate')['information_gate']

    def submit(self, jid, answer=''):
        gate = self.gate(jid)
        payload = {'expected_revision': gate['revision'], 'expected_facts_revision': gate['facts_revision'],
            'answers': [{'question_id': q['id'], 'status': 'answered' if answer else 'unknown',
                         'answer': answer} for q in gate['questions']]}
        if answer:
            payload.update(self.f.payload)
        return payload, self.raw_request('/api/reports/' + jid + '/information-gate', payload)

    def test_required_round_all_unknown_then_direct_report_without_extra_model(self):
        jid = self.initial()
        g = self.gate(jid)
        self.assertEqual(g['status'], 'needs_response')
        self.assertTrue(g['questions'])
        self.assertTrue(all(q['status'] == 'unanswered' for q in g['questions']))
        for suffix in ('', '/facts', '/export.docx', '/rereview'):
            code, data = self.raw_request('/api/reports/' + jid + suffix)
            self.assertEqual(code, 409, data)
            self.assertEqual(data['code'], 'information_gate_required')
        _, (code, data) = self.submit(jid)
        self.assertEqual(code, 200, data)
        self.assertEqual(self.gate(jid)['status'], 'ready')
        self.f.request('/api/reports/' + jid)
        self.assertEqual(self.phases, ['review_preparation', 'review'])

    def test_answer_updates_related_opinion_only_and_preserves_initial_trace(self):
        jid = self.initial()
        original = self.f.server.jobs.load_report(jid)
        original_second = next(f for f in original['report']['findings'] if f['id'] == 'F02')
        original_bytes = (self.f.base / 'jobs' / jid / 'result.json').read_bytes()
        body, (code, data) = self.submit(jid, '仅供内部展示')
        self.assertIn(code, (200, 202), data)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and self.gate(jid)['status'] == 'running':
            time.sleep(.02)
        self.assertEqual(self.gate(jid)['status'], 'ready', self.gate(jid))
        report = self.f.request('/api/reports/' + jid)
        self.assertEqual(next(f for f in report['report']['findings'] if f['id'] == 'F01')['status'], '未检出')
        self.assertEqual(next(f for f in report['report']['findings'] if f['id'] == 'F02'), original_second)
        self.assertEqual(report['report']['checklist_results'][0]['status'], '发现风险')
        self.assertEqual(original_bytes, (self.f.base / 'jobs' / jid / 'result.json').read_bytes())
        facts = self.f.request('/api/reports/' + jid + '/facts')['facts']
        self.assertTrue(any('内部展示' in f['text'] for f in facts['facts'] if f['active']))
        self.raw_request('/api/reports/' + jid + '/information-gate', body)
        self.assertEqual(self.phases.count('post_review_information'), 1)

    def test_scope_two_answer_updates_another_affected_opinion_end_to_end(self):
        with patch("post_review_information.SCOPE_VERSION", 2):
            jid = self.initial()
            _, (code, data) = self.submit(jid, '仅供内部展示')
            self.assertIn(code, (200, 202), data)
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and self.gate(jid)['status'] == 'running':
                time.sleep(.02)
            self.assertEqual(self.gate(jid)['status'], 'ready', self.gate(jid))
            report = self.f.request('/api/reports/' + jid)['report']
            second = next(f for f in report['findings'] if f['id'] == 'F02')
            self.assertEqual(second['reason'], '内部展示无需对外交付，交付期限风险降低。')
            self.assertIn('交付期限风险随之降低', second['post_review_update']['reason'])
            self.assertEqual(report['metadata']['post_review_information']['affected_finding_ids'], ['F01', 'F02'])
            conn = http.client.HTTPConnection('127.0.0.1', self.f.server.server_port, timeout=10)
            conn.request('GET', '/api/reports/' + jid + '/export.docx',
                         headers={'Origin': f'http://127.0.0.1:{self.f.server.server_port}'})
            docx = conn.getresponse().read()
            conn.close()
            with zipfile.ZipFile(io.BytesIO(docx)) as archive:
                self.assertIn('补答后更新', archive.read('word/document.xml').decode('utf-8'))
            self.assertEqual(self.phases.count('post_review_information'), 1)

    def test_no_questions_does_not_insert_a_response_step(self):
        self.questions = False
        jid = self.initial()
        self.assertEqual(self.gate(jid)['status'], 'ready')
        self.f.request('/api/reports/' + jid)
        self.assertEqual(self.phases, ['review_preparation', 'review'])

    def test_resolved_finding_does_not_automatically_pass_its_checklist_group(self):
        self.group_still_pending = True
        jid = self.initial()
        self.submit(jid, '仅供内部展示')
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and self.gate(jid)['status'] == 'running':
            time.sleep(.02)
        self.assertEqual(self.gate(jid)['status'], 'ready')
        report = self.f.request('/api/reports/' + jid)['report']
        self.assertEqual(next(f for f in report['findings'] if f['id'] == 'F01')['status'], '未检出')
        self.assertEqual(report['checklist_results'][0]['groups'][0]['status'], '信息不足')


if __name__ == '__main__':
    unittest.main()
