"""Client-to-server exercise of program-owned quotations and source records."""
import json
import unittest
import uuid

import test_checklist_workspace_integration as fixtures


class ProgramEvidenceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ChecklistWorkspaceHTTPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.seen_schema = []
        # General jobs start with no sources. Explicitly simulate a search
        # result instead of depending on the retired service-law table.
        self.fixture.server.jobs.engines['compatible_api'] = {
            'key': 'compatible_api', 'available': True,
            'config_fields': ['base_url', 'api_key', 'model']}
        self.fixture.payload = {'engine_key': 'compatible_api', 'consent': True,
            'web_search': True, 'config': {'base_url': 'https://api.deepseek.com/v1',
                                        'api_key': 'synthetic-test-key', 'model': 'synthetic'}}
        self.fixture.server.jobs.research_completion = lambda *_: {
            'text': '合成检索候选，仅验证冻结资料记录。',
            'search': {'status': 'searched', 'queries': ['合成检查'], 'request_count': 1,
                       'sources': [{'title': '合成测试资料', 'url': 'https://example.test/synthetic'}]}}

        def runner(messages, config):
            self.fixture.calls.append(config.copy())
            if config.get('review_phase') == 'review_preparation':
                return json.dumps({'questions': [], 'requirements': [], 'additional_checks': []})
            packet = json.loads(messages[1]['content'])
            self.seen_schema.append(packet['required_schema'])
            finding = {
                'id': 'F01', 'rule_id': 'G01', 'theme': '付款与交付', 'title': '合成流程检查意见',
                'issue_kind': '法律', 'status': '发现风险', 'risk': '中',
                'refs': [{'id': 'P002'}], 'source_ids': ['WEB001'], 'location_note': '',
                'reason': '注入回复仅验证程序流程，不代表真实法律判断。',
                'suggestion': '核对原文及适用条件。', 'questions': [],
                'draft_plan': {'status': 'not_needed', 'edits': [], 'insertions': [], 'questions': [],
                    'comment': '本合成意见只验证法源记录，不涉及合同文字修改。',
                    'paragraph_actions': [{'piece_id': 'P002', 'action': 'retain',
                                           'reason': '仅核对引用资料，不改变这段合同文字。'}]}}
            rows = []
            for check in packet['review_preparation']['checks']:
                rows.append({'check_id': check['id'], 'title': check['title'], 'status': '发现风险',
                    'reason': '注入回复。', 'finding_ids': ['F01'], 'groups': [
                        {'group_id': group['group_id'], 'title': group['title'], 'status': '发现风险',
                         'reason': '注入回复。', 'finding_ids': ['F01']} for group in check['groups']]})
            return json.dumps({'findings': [finding], 'checks': [
                {'rule_id': 'G01', 'title': '付款与交付', 'status': '发现风险', 'reason': '注入回复。'}],
                'options': [], 'uncovered': [], 'limitations': [], 'checklist_results': rows})

        self.fixture.server.jobs.runner = runner

    def check_report(self, report):
        finding = report['report']['findings'][0]
        self.assertEqual(finding['status'], '发现风险')
        self.assertEqual(finding['refs'], [{
            'id': 'P002', 'quote': report['contract']['pieces'][2]['text'],
            'quote_source': 'original_paragraph'}])
        self.assertEqual(finding['evidence']['origin'], 'frozen_source_records')
        self.assertEqual(finding['evidence']['legal_applicability'], 'not_assessed')
        self.assertEqual(finding['evidence']['sources'][0]['id'], 'WEB001')
        self.assertFalse(finding['evidence']['sources'][0]['snapshot_verified'])
        self.assertIn('checked_at', finding['evidence']['sources'][0])

    def test_initial_and_rereview_do_not_ask_model_to_copy_or_self_verify(self):
        f = self.fixture
        jid, report = f.run_initial()
        self.check_report(report)
        before = (f.base / 'jobs' / jid / 'response.json').read_bytes()
        preflight = f.request('/api/reports/' + jid + '/rereview')
        child = f.request('/api/reports/' + jid + '/rereview', {
            **f.payload, 'input_revision': preflight['input_revision'], 'request_id': str(uuid.uuid4())})['job']['job_id']
        f.wait_status(child, 'completed')
        self.check_report(f.request('/api/reports/' + child))
        self.assertEqual(before, (f.base / 'jobs' / jid / 'response.json').read_bytes())
        self.assertEqual(len(f.calls), 3)
        for schema in self.seen_schema:
            finding = schema['properties']['findings']['items']
            self.assertNotIn('evidence_status', finding['properties'])
            self.assertEqual(set(finding['properties']['refs']['items']['properties']), {'id'})


if __name__ == '__main__':
    unittest.main()
