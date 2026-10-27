"""Per-section truth and frozen protocol tests. All completions are local doubles."""
import copy
import json
import unittest

import test_review_preparation as fixture
from checklist_groups import VERSION_FIELD, freeze_groups, split_groups, uses_groups
from contract_core import parse_text
from general_review import build_messages, parse_response, response_schema
from review_jobs import JobConflict, _atomic_json, _read_json
from review_preparation import digest, parse_response as prepare, plan_digest


class GroupReviewTests(unittest.TestCase):
    def setUp(self):
        self.selected = fixture.checklist()
        self.selected['items'][0]['requirement'] = '### 付款节点\n核对节点。\n\n### 退费\n核对退费。'
        self.plan = prepare(json.dumps(fixture.output()), fixture.context(), self.selected, 1,
                            group_protocol_version=1)
        self.groups = self.plan['checks'][0]['groups']
        self.parsed = parse_text('甲方支付货款。')

    def answer(self):
        return {'findings': [], 'checks': [], 'options': [], 'uncovered': [], 'limitations': [],
                'checklist_results': [{'check_id': 'payment', 'title': '付款与结算', 'status': '未检出',
                                      'reason': '按全文检查。', 'finding_ids': [], 'groups': [
                    {'group_id': g['group_id'], 'title': g['title'], 'status': '未检出',
                     'reason': '本组已独立核查。', 'finding_ids': []} for g in self.groups]}]}

    def parse(self, answer):
        return parse_response(json.dumps(answer), self.parsed, fixture.context(), [], preparation=self.plan)

    def test_split_plain_preamble_duplicate_heading_and_fenced_heading(self):
        check = {'id': 'C01', 'title': '综合', 'requirement': '概述\n### 同名\nA\n### 同名\nB\n```md\n### 不拆\n```\n'}
        groups = split_groups(check)
        self.assertEqual([g['title'] for g in groups], ['其他检查内容', '同名', '同名'])
        self.assertEqual(len({g['group_id'] for g in groups}), 3)
        self.assertEqual(''.join(g['requirement'] for g in groups), check['requirement'])
        self.assertEqual(split_groups({**check, 'requirement': '普通文字'})[0]['title'], '综合')
        padded = {**check, 'requirement': '\n\n### 内容\r\n原文字。\r\n'}
        self.assertEqual(''.join(g['requirement'] for g in split_groups(padded)), padded['requirement'])

    def test_all_groups_required_in_schema_and_model_input(self):
        schema = response_schema(self.plan)['properties']['checklist_results']['items']
        self.assertIn('groups', schema['required'])
        self.assertEqual(schema['properties']['groups']['items']['properties']['group_id']['enum'],
                         [g['group_id'] for g in self.groups])
        messages = build_messages(self.parsed, fixture.context(), [], preparation=self.plan)
        self.assertEqual(json.loads(messages[1]['content'])['review_preparation'], self.plan)
        self.assertIn('不能把父项未检出', messages[0]['content'])

    def test_legacy_no_groups_no_changed_plan_digest_or_model_schema(self):
        legacy = prepare(json.dumps(fixture.output()), fixture.context(), self.selected, 1)
        expected = digest({k: legacy[k] for k in ('plan_revision', 'checks', 'requirements', 'original_requirements')} |
                          {'questions': []})
        self.assertEqual(plan_digest(legacy), expected)
        self.assertFalse(uses_groups(legacy))
        self.assertNotIn('groups', legacy['checks'][0])
        self.assertNotIn('groups', response_schema(legacy)['properties']['checklist_results']['items']['properties'])
        answer = self.answer()
        del answer['checklist_results'][0]['groups']
        parsed = parse_response(json.dumps(answer), self.parsed, fixture.context(), [], preparation=legacy)
        self.assertEqual(parsed['checklist_results'], answer['checklist_results'])

    def test_neutral_inapplicable_and_no_parent_propagation(self):
        answer = self.answer()
        answer['checklist_results'][0]['groups'][1]['status'] = '不适用'
        parsed = self.parse(answer)
        self.assertEqual([g['status'] for g in parsed['checklist_results'][0]['groups']], ['未检出', '不适用'])
        self.assertEqual(parsed['checks'], [])
        answer['checklist_results'][0]['status'] = '信息不足'
        parsed = self.parse(answer)
        self.assertEqual(parsed['checklist_results'][0]['groups'][0]['status'], '未检出')
        answer['checklist_results'][0]['status'] = '不适用'
        self.assertEqual(self.parse(answer)['checklist_results'][0]['status'], '处理失败')
        for group in answer['checklist_results'][0]['groups']:
            group['status'] = '不适用'
        self.assertEqual(self.parse(answer)['checklist_results'][0]['status'], '不适用')

    def test_missing_duplicate_unknown_bad_links_and_empty_reason_fail_visibly(self):
        valid = self.answer()['checklist_results'][0]['groups']
        cases = [None, [], valid[:1], [valid[0], valid[0], valid[1]],
                 [*valid, {**valid[0], 'group_id': 'unknown'}],
                 [{**valid[0], 'finding_ids': ['F99']}, valid[1]],
                 [{**valid[0], 'reason': ''}, valid[1]]]
        for groups in cases:
            with self.subTest(groups=groups):
                answer = self.answer()
                answer['checklist_results'][0]['groups'] = groups
                result = self.parse(answer)
                self.assertEqual(result['checklist_results'][0]['status'], '处理失败')
                self.assertTrue(result['limitations'])
                self.assertEqual(result['findings'], [])  # A backend failure is not a contract risk.

    def test_independent_pending_or_risk_cannot_hide_under_green_parent(self):
        for status in ('信息不足', '需要法务判断', '发现风险'):
            answer = self.answer()
            answer['checklist_results'][0]['groups'][1]['status'] = status
            result = self.parse(answer)
            self.assertEqual(result['checklist_results'][0]['status'], '处理失败')
            self.assertEqual(result['checklist_results'][0]['groups'][1]['status'], status)
            self.assertEqual(result['checklist_results'][0]['groups'][0]['status'], '未检出')

    def test_pass_with_risk_link_and_unverified_finding_fail_only_linked_group(self):
        finding = {'id': 'F01', 'rule_id': 'G01', 'theme': '付款', 'title': '付款问题', 'issue_kind': '商业',
                   'status': '发现风险', 'risk': '中', 'evidence_status': '原文可核',
                   'refs': [{'id': 'P000', 'quote': '甲方支付货款'}], 'location_note': '', 'source_ids': [],
                   'reason': '本条风险。', 'suggestion': '协商节点。', 'clause_draft': '', 'questions': []}
        for quote in ('甲方支付货款', '不存在的引文'):
            answer = self.answer()
            answer['findings'] = [{**finding, 'refs': [{'id': 'P000', 'quote': quote}]}]
            answer['checks'] = [{'rule_id': 'G01', 'title': '付款', 'status': '发现风险', 'reason': '核查。'}]
            answer['checklist_results'][0]['groups'][0]['finding_ids'] = ['F01']
            result = self.parse(answer)
            self.assertEqual(result['checklist_results'][0]['groups'][0]['status'], '处理失败')
            self.assertEqual(result['checklist_results'][0]['groups'][1]['status'], '未检出')

    def test_group_snapshot_tamper_and_unsupported_version_rejected(self):
        for mutate in (lambda p: p['checks'][0]['groups'][0].update(title='篡改'),
                       lambda p: p.update(checklist_group_protocol_version=2)):
            plan = copy.deepcopy(self.plan)
            mutate(plan)
            with self.assertRaises(ValueError):
                response_schema(plan)
        newer = copy.deepcopy(self.plan)
        newer['checks'][0]['requirement'] += '\n### 新检查\n新增内容'
        freeze_groups(newer)
        self.assertNotEqual(plan_digest(newer), plan_digest(self.plan))

    def test_parent_pass_with_pending_link_fails_only_new_protocol(self):
        answer = self.answer()
        answer['findings'] = [{
            'id': 'F01', 'rule_id': 'G01', 'theme': '付款', 'title': '付款信息待确认',
            'issue_kind': '信息', 'status': '信息不足', 'risk': None, 'evidence_status': '资料不足',
            'refs': [], 'location_note': '付款安排', 'source_ids': [], 'reason': '付款日期未确定。',
            'suggestion': '确认付款日期。', 'clause_draft': '', 'questions': ['何时付款？'],
        }]
        answer['checks'] = [{'rule_id': 'G01', 'title': '付款', 'status': '信息不足', 'reason': '尚待确认。'}]
        answer['checklist_results'][0]['finding_ids'] = ['F01']
        legacy = copy.deepcopy(self.plan)
        legacy.pop(VERSION_FIELD)
        for check in legacy['checks']:
            check.pop('groups')
        for status in ('未检出', '不适用'):
            with self.subTest(status=status):
                answer['checklist_results'][0]['status'] = status
                for group in answer['checklist_results'][0]['groups']:
                    group['status'] = status
                current = self.parse(answer)
                self.assertEqual(current['checklist_results'][0]['status'], '处理失败')
                self.assertEqual([g['status'] for g in current['checklist_results'][0]['groups']], [status, status])
                self.assertTrue(current['limitations'])
                old = parse_response(json.dumps(answer), self.parsed, fixture.context(), [], preparation=legacy)
                self.assertEqual(old['checklist_results'][0]['status'], status)
                self.assertEqual(old['limitations'], [])
                self.assertNotIn('groups', old['checklist_results'][0])


class GroupJobTests(unittest.TestCase):
    setUp = fixture.PreparationJobTests.setUp
    create = fixture.PreparationJobTests.create
    prepare = fixture.PreparationJobTests.prepare
    wait = fixture.PreparationJobTests.wait
    def test_version_frozen_at_create_and_bound_to_input_digest(self):
        jid = self.create(fixture.checklist())
        directory = self.manager._directory(jid)
        status = _read_json(directory / 'status.json', 'test')
        self.assertEqual(status[VERSION_FIELD], 1)
        status.pop(VERSION_FIELD)
        _atomic_json(directory / 'status.json', status)
        with self.assertRaises(JobConflict):
            self.manager.prepare(jid, self.payload)

    def test_legacy_pending_job_remains_legacy_when_preparing_now(self):
        jid = self.create(fixture.checklist())
        directory = self.manager._directory(jid)
        status = _read_json(directory / 'status.json', 'test')
        status.pop(VERSION_FIELD)
        from contract_core import ParsedContract
        parsed = ParsedContract.model_validate(_read_json(directory / 'contract.json', 'test'))
        sources = _read_json(directory / 'sources.json', 'test')
        status['preparation_input_digest'] = self.manager._input_digest(status, parsed, sources)
        _atomic_json(directory / 'status.json', status)
        self.prepare(jid)
        self.manager.start(jid, self.payload)
        self.wait(jid, 'completed')
        report = self.manager.load_report(jid)['report']
        self.assertNotIn(VERSION_FIELD, report['preparation'])
        self.assertNotIn('groups', report['checklist_results'][0])


if __name__ == '__main__':
    unittest.main()
