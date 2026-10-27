"""Old report drafting through configured API, without a Codex dependency."""
import json
import sys
import time
import unittest

from tests import test_delivery_reliability_http as fixtures


class LegacyDraftAPIHTTPTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DeliveryReliabilityHTTPTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.messages = []
        self.config = {'base_url': 'http://127.0.0.1:9999/v1',
                       'api_key': 'synthetic-draft-secret-http', 'model': 'synthetic-api-model'}
        self.f.server.drafts.runner = None
        def completion(config, messages):
            self.assertEqual(config, self.config)
            self.messages.append(messages)
            return self.f.server.jobs.runner(messages, {'engine_key': 'injected'})
        self.f.server.drafts.completion = completion
        self.f.request('/api/model-settings', {'expected_revision': 0, 'action': 'save',
            'provider': '自定义兼容接口', 'config': self.config})

    def test_explicit_api_draft_call_cache_and_no_secret_in_records(self):
        draft = self.f.request('/api/reports/detailed/drafts/F01')
        self.assertEqual(draft['status'], 'absent')
        self.assertEqual(self.messages, [])
        delivery = self.f.request('/api/reports/detailed/delivery')
        facts = self.f.request('/api/reports/detailed/facts')['facts']
        payload = {'expected_revision': delivery['state']['revision'],
            'source_sha256': delivery['source']['source_sha256'], 'draft_text': self.f.draft,
            'clarification': '', 'config': self.config, 'consent': True,
            'expected_opinion_revision': 0, 'expected_facts_revision': facts['revision']}
        candidate = self.f.request('/api/reports/detailed/drafts/F01', payload)
        deadline = time.monotonic() + 6
        while candidate['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.02)
            candidate = self.f.request('/api/reports/detailed/drafts/F01')
        self.assertEqual(candidate['status'], 'ready', candidate)
        self.assertIn('synthetic-api-model', candidate['engine_label'])
        packet = json.loads(self.messages[0][1]['content'].split('\n', 1)[1])
        self.assertIn('required_schema', packet)
        self.assertEqual(set(packet['required_schema']['required']),
                         {'status', 'edits', 'insertions', 'questions', 'comment', 'paragraph_actions', 'suggestion_actions'})
        self.assertEqual(len(packet['contract_context']), 4)
        self.assertTrue(all(p['id'].startswith('current:') for p in packet['contract_context']))
        self.assertEqual(self.f.request('/api/reports/detailed/drafts/F01', payload)['candidate_id'], candidate['candidate_id'])
        self.assertEqual(len(self.messages), 1)
        for path in (self.f.root / 'state' / 'drafts').rglob('*'):
            if path.is_file():
                self.assertNotIn(self.config['api_key'], path.read_text(encoding='utf-8'), str(path))


if __name__ == '__main__':
    if '--serve' in sys.argv:
        fixture = LegacyDraftAPIHTTPTests()
        fixture.setUp()
        try:
            print(json.dumps({'port': fixture.f.port, 'root': str(fixture.f.root)}), flush=True)
            input()
        finally:
            fixture.doCleanups()
    else:
        unittest.main()
