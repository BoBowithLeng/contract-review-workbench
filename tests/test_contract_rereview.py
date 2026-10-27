"""No model network calls: continuation, actual DOCX projection and version checks."""
import copy
import json
import threading
import unittest
import uuid

import test_review_preparation as fixture
from contract_core import parse_docx
from contract_rereview import RereviewManager
from reader_delivery import DeliveryManager
from review_jobs import JobConflict
from docx_delivery import inspect_source


class FactsDouble:
    def __init__(self):
        self.lock = threading.RLock()
        self.revision = 1
    def get(self, loaded):
        return {'revision': self.revision, 'facts': [
            {'id': 'fact-'+'a'*32, 'text': '设备只作内部展示。', 'source': {'kind': 'discussion', 'report_id':loaded['id'], 'quote': '设备只作内部展示。'}, 'active': True},
            {'id': 'fact-'+'b'*32, 'text': '旧说法不要带入', 'source': {}, 'active': False}]}


class RereviewTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.PreparationJobTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.parent = self.f.create(fixture.checklist(), '核对付款条件。')
        self.f.prepare(self.parent)
        self.f.manager.start(self.parent, self.f.payload)
        self.f.wait(self.parent, 'completed')
        self.loaded = self.f.manager.load_report(self.parent)
        self.delivery = DeliveryManager(self.f.root / 'state', {self.parent: self.loaded['source_path']})
        self.facts = FactsDouble()
        self.manager = RereviewManager(self.f.root / 'rereviews', self.f.manager, self.delivery, self.facts)
        self.state = {'revision': 0, 'reviews': {}, 'selected_options': []}

    def payload(self):
        preflight = self.manager.preflight(self.loaded, self.state)
        self.assertTrue(preflight['available'])
        return {**self.f.payload, 'request_id': str(uuid.uuid4()), 'input_revision': preflight['input_revision']}

    def test_current_confirmed_docx_facts_checklist_parent_and_idempotence(self):
        raw = self.loaded['source_path'].read_bytes()
        source = inspect_source(raw)
        piece = source['pieces'][0]
        self.delivery.save(self.loaded, {
            'expected_revision': 0, 'source_sha256': source['source_sha256'], 'author': '工程验证',
            'edits': [{'id': 'edit-proof', 'piece_id': piece['id'], 'paragraph_sha256': piece['paragraph_sha256'],
                       'start': 0, 'end': len(piece['text']), 'old_text': piece['text'],
                       'new_text': '甲方采购设备。乙方交付并验收后支付货款。', 'confirmed': True}], 'comments': []})
        payload = self.payload()
        child, started = self.manager.start(self.loaded, self.state, payload)
        self.assertTrue(started)
        self.f.wait(child['job_id'], 'completed')
        self.assertEqual(len(self.f.calls), 3)  # parent prepare + review, child review only
        result = self.f.manager.load_report(child['job_id'])
        self.assertIn('交付并验收后', result['contract']['pieces'][0]['text'])
        self.assertEqual(self.loaded['source_path'].read_bytes(), raw)
        self.assertEqual(result['report']['metadata']['parent_report_id'], self.parent)
        self.assertEqual(len(result['report']['confirmed_facts']['facts']), 1)
        from contract_facts import ContractFactsManager
        inherited = ContractFactsManager(self.f.root/'child-facts').get(result)
        self.assertEqual(len(inherited['facts']), 1)
        self.assertEqual(inherited['inherited_revision'], 1)
        self.assertNotIn('旧说法不要带入', json.dumps(result['report'], ensure_ascii=False))
        self.assertEqual(result['report']['preparation']['checks'][0]['id'], 'payment')
        self.assertEqual(result['report']['context']['business_goal'], '核对付款条件。')
        repeat, again = self.manager.start(self.loaded, self.state, payload)
        self.assertEqual(repeat['job_id'], child['job_id'])
        self.assertFalse(again)
        self.assertEqual(len(self.f.calls), 3)
        self.assertEqual(self.f.manager.load_report(self.parent)['request_digest'], self.loaded['request_digest'])
        with self.assertRaises(JobConflict):
            self.f.manager.answer_preparation(child['job_id'], {'expected_revision': 1, 'answers': []})

    def test_unresolved_and_answered_questions_survive_round_without_reseeding_obsolete_facts(self):
        from contract_facts import ContractFactsManager, effective_facts
        self.loaded["report"]["context"]["business_facts"] = "已撤回的旧事实"
        self.loaded["report"]["preparation"]["questions"] = [
            {"id": "Q01", "question": "哪日交付？", "required": True, "reason": "交易尚未定", "answer": "", "status": "unknown"}]
        facts = ContractFactsManager(self.f.root / "real-facts")
        self.manager.facts = facts
        initial = facts.get(self.loaded)
        facts.withdraw(self.loaded, {"expected_revision": 0, "fact_id": initial["facts"][0]["id"]})
        qid = initial["questions"][0]["id"]
        facts.answer(self.loaded, {"expected_revision": 1, "question_id": qid, "answer": "10月8日", "status": "answered"})
        child, started = self.manager.start(self.loaded, self.state, self.payload())
        self.assertTrue(started)
        self.f.wait(child["job_id"], "completed")
        current = self.f.manager.load_report(child["job_id"])
        inherited = facts.get(current)
        self.assertEqual(len(inherited["questions"]), 1)
        self.assertEqual(inherited["questions"][0]["status"], "answered")
        self.assertEqual(inherited["questions"][0]["answer"], "10月8日")
        self.assertNotIn("已撤回的旧事实", json.dumps(current["report"]["preparation"], ensure_ascii=False))
        self.assertEqual(current["report"]["context"]["business_facts"], "")
        self.assertEqual(len(effective_facts(inherited)), 1)
        facts.withdraw(current, {"expected_revision": 0, "fact_id": effective_facts(inherited)[0]["id"]})
        self.delivery.register_source(current["id"], current["source_path"])
        preflight = self.manager.preflight(current, self.state)
        grandchild, _ = self.manager.start(current, self.state, {**self.f.payload, "request_id": str(uuid.uuid4()), "input_revision": preflight["input_revision"]})
        self.f.wait(grandchild["job_id"], "completed")
        latest = self.f.manager.load_report(grandchild["job_id"])
        final = facts.get(latest)
        self.assertEqual(final["questions"][0]["status"], "unknown")
        self.assertEqual(final["questions"][0]["answer"], "")
        self.assertEqual(effective_facts(final), [])
        self.assertEqual(latest["report"]["preparation"]["questions"][0]["id"], qid)

    def test_shared_fact_local_answer_keeps_other_question_valid_for_rereview(self):
        from contract_facts import ContractFactsManager, effective_facts
        for status, answer in (("answered", "十二日"), ("unknown", "")):
            with self.subTest(status=status):
                loaded = copy.deepcopy(self.loaded)
                loaded["report"]["context"]["business_facts"] = ""
                loaded["report"]["preparation"]["questions"] = [
                    {"id": "Q01", "question": "甲系统测试需几日？", "required": False, "reason": "测试安排", "answer": "", "status": "unknown"},
                    {"id": "Q02", "question": "乙系统测试需几日？", "required": False, "reason": "测试安排", "answer": "", "status": "unknown"},
                ]
                facts = ContractFactsManager(self.f.root / ("shared-facts-" + status))
                self.manager.facts = facts
                initial = facts.get(loaded)
                first_id, second_id = [q["id"] for q in initial["questions"]]
                first = facts.answer(loaded, {"expected_revision": 0, "question_id": first_id, "status": "answered", "answer": "十日"})
                second = facts.answer(loaded, {"expected_revision": 1, "question_id": second_id, "status": "answered", "answer": "十日"})
                shared_id = first["questions"][0]["fact_id"]
                shared_text = next(f["text"] for f in second["facts"] if f["id"] == shared_id)
                shared = facts.save(loaded, {"expected_revision": 2, "replace_id": second["questions"][1]["fact_id"], "text": shared_text})
                self.assertEqual([q["fact_id"] for q in shared["questions"]], [shared_id, shared_id])
                other_before = copy.deepcopy(shared["questions"][1])
                changed = facts.answer(loaded, {"expected_revision": 3, "question_id": first_id, "status": status, "answer": answer})
                self.assertEqual(changed["questions"][1], other_before)
                self.assertEqual(changed["questions"][0]["status"], status)
                old = next(f for f in changed["facts"] if f["id"] == shared_id)
                self.assertTrue(old["active"])
                self.assertIsNone(old["superseded_by"])
                self.assertEqual(len(effective_facts(changed)), 2 if status == "answered" else 1)
                preflight = self.manager.preflight(loaded, self.state)
                self.assertTrue(preflight["available"], preflight)
                child, _ = self.manager.start(loaded, self.state, {**self.f.payload, "request_id": str(uuid.uuid4()), "input_revision": preflight["input_revision"]})
                self.f.wait(child["job_id"], "completed")
                rereview = self.f.manager.load_report(child["job_id"])
                inherited = facts.get(rereview)
                self.assertEqual(len(inherited["questions"]), 2)
                self.assertEqual(inherited["questions"][0]["status"], status)
                self.assertEqual(inherited["questions"][1]["status"], "answered")
                self.assertEqual(inherited["questions"][1]["fact_id"], shared_id)
                self.assertTrue(any(f["id"] == shared_id for f in effective_facts(inherited)))

    def test_changed_facts_or_action_snapshot_rejects_start(self):
        payload = self.payload()
        self.facts.revision += 1
        with self.assertRaises(JobConflict):
            self.manager.start(self.loaded, self.state, payload)
        payload = self.payload()
        self.state['revision'] += 1
        with self.assertRaises(JobConflict):
            self.manager.start(self.loaded, self.state, payload)
        self.assertEqual(len(self.f.calls), 2)

    def test_parent_high_risk_and_nonadoption_are_data_not_deleted(self):
        self.loaded['report']['findings'] = [{'id': 'F01', 'title': '仍有高风险', 'status': '发现风险',
                                               'risk': '高', 'reason': '注入桩', 'refs': []}]
        self.state['reviews']['F01'] = {'decision': '驳回', 'note': '决定保留，未声称风险不存在。'}
        child, _ = self.manager.start(self.loaded, self.state, self.payload())
        self.f.wait(child['job_id'], 'completed')
        messages = self.f.calls[-1][0]
        sent = json.loads(messages[1]['content'])
        prior = sent['review_preparation']['rereview_context']['previous_findings'][0]
        self.assertEqual(prior['risk'], '高')
        self.assertTrue(prior['action']['ignored'])
        self.assertIn('仍成立的风险保留', messages[0]['content'])

    def test_inherited_trace_tamper_and_no_consent_are_blocked(self):
        p = self.payload()
        with self.assertRaises(ValueError):
            self.manager.start(self.loaded, self.state, {**p, 'consent': False})
        job, _ = self.manager.start(self.loaded, self.state, p)
        self.f.wait(job['job_id'], 'completed')
        path = self.f.root / 'jobs' / job['job_id'] / 'rereview-input.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['origin']['parent_report_id'] = 'minimal'
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(JobConflict):
            self.f.manager.load_report(job['job_id'])

    def test_legacy_context_without_preparation_can_rereview_without_new_questions(self):
        self.loaded['report'].pop('preparation')
        self.loaded['report'].pop('checklist_results')
        child, _ = self.manager.start(self.loaded, self.state, self.payload())
        self.f.wait(child['job_id'], 'completed')
        preparation = self.f.manager.load_report(child['job_id'])['report']['preparation']
        self.assertEqual(preparation['questions'], [])
        self.assertEqual(preparation['checklist_group_protocol_version'], 1)
        self.assertEqual(preparation['checks'][0]['id'], 'payment')
        self.assertEqual(len(preparation['checks'][0]['groups']), 1)

    def test_legacy_preparation_upgrades_only_new_round(self):
        parent_plan = self.loaded['report']['preparation']
        parent_plan.pop('checklist_group_protocol_version')
        for check in parent_plan['checks']:
            check.pop('groups')
        unchanged = copy.deepcopy(parent_plan)
        child, _ = self.manager.start(self.loaded, self.state, self.payload())
        self.f.wait(child['job_id'], 'completed')
        report = self.f.manager.load_report(child['job_id'])['report']
        self.assertEqual(report['preparation']['checklist_group_protocol_version'], 1)
        self.assertEqual(len(report['checklist_results'][0]['groups']), 1)
        self.assertEqual(parent_plan, unchanged)


if __name__ == '__main__':
    unittest.main()
