"""Search failure details survive research/discussion without changing old reports."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from contract_core import parse_text
from model_client import ModelClientError
from native_web_search import sanitize_search_diagnostics
from review_jobs import ReviewJobManager, _research_metadata
from clause_discussion import ClauseDiscussionManager, _validate_search
from tests import test_clause_discussion as discussion_fixtures
from tests.test_general_review import context


def diagnostic():
    return {'version': 1, 'continuation_count': 0, 'continuation_mode': 'none', 'answer_complete': True,
            'completion_error': None, 'error_codes': ['max_uses_exceeded'], 'partial_reasons': ['tool_error'],
            'stages': [{'phase': 'search', 'outcome': 'response', 'stop_reason': 'end_turn',
                       'has_final_text': True, 'tool_call_count': 4, 'matched_result_count': 4,
                       'pending_tool_count': 0, 'request_count': 4, 'request_count_origin': 'usage',
                       'error_codes': ['max_uses_exceeded'], 'partial_reasons': ['tool_error']}]}


class ResearchDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = ReviewJobManager(self.root, None, None, [])
        self.config = {'base_url': 'https://api.deepseek.com', 'api_key': 'synthetic-secret',
                       'model': 'synthetic', 'web_search': True}
        self.result = {'text': '两次搜索成功，两次到达次数上限。', 'answer_complete': True,
                      'search': {'status': 'partial', 'queries': ['测试'], 'request_count': 4,
                                 'sources': [{'title': '测试资料', 'url': 'https://example.test/one'}]},
                      'diagnostics': diagnostic()}

    def run_research(self):
        return self.jobs._research(parse_text('合同测试。'), context(), [], self.config, '合成规则')

    def test_details_kept_in_packet_and_report_metadata(self):
        self.result['search']['queries'] = ['问题一', '问题二', '问题三', '问题四']
        self.jobs.research_completion = lambda *_: copy.deepcopy(self.result)
        packet = self.run_research()
        self.assertEqual(packet['search']['queries'], self.result['search']['queries'])
        self.assertEqual(packet['diagnostics'], diagnostic())
        self.assertEqual(_research_metadata(packet)['diagnostics']['error_codes'], ['max_uses_exceeded'])
        self.assertEqual(_research_metadata(packet)['source_count'], 1)
        old = {k: v for k, v in packet.items() if k != 'diagnostics'}
        self.assertEqual(set(_research_metadata(old)), {'status', 'request_count', 'source_count', 'error'})

    def test_initial_transport_failure_keeps_safe_diagnostics(self):
        def fail(*_):
            error = ModelClientError('连接未完成', code='connection_error')
            error.search_diagnostics = {'version': 1, 'continuation_count': 0, 'continuation_mode': 'none',
                'answer_complete': False, 'completion_error': '连接未完成', 'error_codes': [],
                'partial_reasons': ['request_failed'], 'stages': [{'phase': 'search', 'outcome': 'failed',
                    'stop_reason': None, 'error': '连接未完成'}]}
            raise error
        self.jobs.research_completion = fail
        packet = self.run_research()
        self.assertEqual(packet['status'], 'error')
        self.assertEqual(packet['diagnostics']['partial_reasons'], ['request_failed'])

    def test_local_memo_truncation_has_own_reason(self):
        self.result.update(text='长' * 30001)
        self.result['search']['status'] = 'searched'
        self.jobs.research_completion = lambda *_: self.result
        packet = self.run_research()
        self.assertTrue(packet['memo_truncated'])
        self.assertTrue(_research_metadata(packet)['memo_truncated'])

    def test_later_search_sources_are_not_silently_cut_at_twenty(self):
        sources = [{'title': '合成资料'+str(i), 'url': f'https://example.test/source/{i}'} for i in range(30)]
        self.result['search']['sources'] = sources
        self.jobs.research_completion = lambda *_: copy.deepcopy(self.result)
        packet = self.run_research()
        self.assertEqual(len(packet['sources']), 30)
        self.assertEqual(packet['sources'][-1]['url'], sources[-1]['url'])
        self.assertEqual(_research_metadata(packet)['source_count'], 30)
        self.assertEqual(len(_validate_search(self.result['search'])['sources']), 30)

    def test_discussion_accepts_and_persists_new_native_metadata(self):
        f = discussion_fixtures.ClauseDiscussionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        manager = ClauseDiscussionManager(self.root / 'chat', f.delivery,
            search_completion=lambda *_: copy.deepcopy(self.result))
        request = discussion_fixtures.request_body('请搜索合成问题')
        request.update(web_search=True)
        request['config'].update(base_url='https://api.deepseek.com', model='synthetic')
        manager.start(f.loaded, 'F01', request)
        state = discussion_fixtures.wait_done(manager, f.loaded)
        turn = state['turns'][0]
        self.assertEqual(turn['status'], 'completed')
        self.assertEqual(turn['search']['diagnostics'], diagnostic())
        self.assertEqual(manager.get(f.loaded, 'F01')['turns'][0]['search']['diagnostics'], diagnostic())
        for malformed in (None, {'version': 999}):
            with self.assertRaises(ValueError):
                _validate_search({**self.result['search'], 'diagnostics': malformed})


if __name__ == '__main__':
    unittest.main()
