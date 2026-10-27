"""Discussion receives whole current document, not other sessions' chat logs."""
import copy
import json
import time
import unittest
import uuid

from contract_core import parse_docx
from reader_server import REPORTS, _load_report
from tests import test_delivery_reliability_http as fixtures


class DiscussionLinkedHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DeliveryReliabilityHTTPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.request = self.fixture.request
        project = self.fixture.root / 'project'
        # Add a second opinion before the report is first read. This is a fake
        # historical response in a temporary project, never a production edit.
        for spec in REPORTS.values():
            path = project / 'runtime' / spec['directory'] / 'response.json'
            saved = json.loads(path.read_text(encoding='utf-8'))
            other = copy.deepcopy(saved['result']['findings'][0])
            other.update(id='F02', title='其他条款的结算建议',
                refs=[{'id': self.fixture.ids[3], 'quote': '8.1 未尽事项由双方协商补充。'}],
                clause_draft='未采用的建议：不要把此句当成合同正文。')
            saved['result']['findings'].append(other)
            path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        self.calls = []
        def completion(config, messages):
            self.calls.append(copy.deepcopy(messages))
            return '合成测试答复，未请求任何外部服务。'
        self.fixture.server.discussions.completion = completion

    def discuss(self, finding_id, message):
        path = f'/api/reports/detailed/discussions/{finding_id}'
        current = self.request(path)
        state = current.get('state', current)
        result = self.request(path, {'session_id': state['session_id'],
            'expected_revision': state['revision'], 'expected_delivery_revision': state['context']['delivery_revision'],
            'expected_opinion_revision': state['opinion_revision'], 'message': message,
            'request_id': str(uuid.uuid4()), 'config': {'base_url': 'http://127.0.0.1:9999/v1',
            'api_key': 'synthetic-key', 'model': 'synthetic'}, 'consent': True, 'web_search': False})
        for _ in range(100):
            state = result.get('state', result)
            if not state.get('pending_turn_id'):
                break
            time.sleep(.02)
            result = self.request(path)
        self.assertFalse(state.get('pending_turn_id'))
        self.assertEqual(state['turns'][-1]['status'], 'completed', state['turns'][-1])

    def test_other_confirmed_changes_and_status_are_visible_but_other_chat_is_not(self):
        delivery = self.request('/api/reports/detailed/delivery')
        source = delivery['source']
        anchor = next(p for p in source['pieces'] if p['id'] == self.fixture.ids[3])
        edits = [dict(id='edit-F02-confirmed', piece_id=anchor['id'], paragraph_sha256=anchor['paragraph_sha256'],
            start=0, end=len(anchor['text']), old_text=anchor['text'], new_text='8.1 结算期限已经确认为十五日。',
            confirmed=True, finding_ids=['F02']),
            dict(id='edit-F02-insertion', kind='insert_paragraph_after', piece_id=anchor['id'],
                paragraph_sha256=anchor['paragraph_sha256'], start=len(anchor['text']), end=len(anchor['text']),
                old_text='', new_text='8.2 通知由指定邮箱发送。', confirmed=True, finding_ids=['F02'])]
        self.request('/api/reports/detailed/delivery', {'expected_revision': 0,
            'source_sha256': source['source_sha256'], 'author': '合成测试', 'edits': edits, 'comments': []})
        report = self.request('/api/reports/detailed')
        reviews = report['state']['reviews']
        reviews['F02'].update(decision='驳回', note='这条建议不采用，但已确认的部分修改保留。')
        self.request('/api/reports/detailed/state', {'expected_revision': report['state']['revision'],
            'reviews': reviews, 'selected_options': []})
        self.request('/api/reports/detailed/facts/save', {'expected_revision': 0, 'text': '合成业务事实：项目必须在活动前可用。'})
        self.discuss('F02', 'OTHER_SESSION_PRIVATE_SENTINEL')
        self.discuss('F01', 'CURRENT_SESSION_HISTORY_SENTINEL')
        self.discuss('F01', '请一起看其他条款现在的安排。')
        messages = self.calls[-1]
        packet = json.loads(messages[1]['content'].split('\n', 1)[1])
        current_texts = [p['text'] for p in packet['CURRENT_CONTRACT']]
        original_texts = [p['text'] for p in packet['ORIGINAL_CONTRACT']]
        self.assertIn('8.1 结算期限已经确认为十五日。', current_texts)
        self.assertIn('8.2 通知由指定邮箱发送。', current_texts)
        self.assertIn('8.1 未尽事项由双方协商补充。', original_texts)
        self.assertNotIn('未采用的建议', '\n'.join(current_texts))
        loaded = _load_report(self.fixture.root/'project', 'detailed')
        actual = self.fixture.server.delivery.rereview_snapshot(loaded)
        self.assertEqual(current_texts, [p.text for p in parse_docx(actual['data']).pieces])
        other = next(row for row in packet['REPORT_OPINIONS'] if row['finding_id'] == 'F02')
        self.assertEqual(other['current_opinion']['clause_draft'], '未采用的建议：不要把此句当成合同正文。')
        self.assertEqual(other['human_review']['decision'], '驳回')
        self.assertTrue(other['document_action']['modified'])
        self.assertIn('合成业务事实', json.dumps(packet['CONFIRMED_FACTS'], ensure_ascii=False))
        flattened = json.dumps(messages, ensure_ascii=False)
        self.assertIn('CURRENT_SESSION_HISTORY_SENTINEL', flattened)
        self.assertNotIn('OTHER_SESSION_PRIVATE_SENTINEL', flattened)
        self.assertEqual(len(self.calls), 3)


if __name__ == '__main__':
    unittest.main()
