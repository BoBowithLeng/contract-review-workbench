"""Disposable jobs and injected replies only: retries and saved-response completion."""
import copy
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from model_client import ModelClientError
from review_jobs import JobConflict, JobError, _atomic_json, _sha256
from tests import test_review_preparation as fixtures
from tests import test_review_jobs as http_fixtures


class ReviewRetryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PreparationJobTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.manager = self.f.manager
        self.manager.engines['injected']['model'] = 'fixture'
        self.original_runner = self.manager.runner

    def ready(self, *, answered=False):
        if answered:
            self.f.prep_output = fixtures.output(questions=[fixtures.question()])
        jid = self.f.create(fixtures.checklist())
        self.f.prepare(jid, 'needs_info' if answered else 'prepared')
        if answered:
            prep = self.manager.get(jid)['preparation']
            self.manager.answer_preparation(jid, {'expected_revision': prep['revision'], 'answers': [
                {'id': 'Q01', 'answer': '设备用于内部展示。', 'status': 'answered'}]})
        return jid

    def fail_review(self, jid, exc=None):
        def failure(*_):
            raise exc or ModelClientError('模型服务返回 HTTP 503。', code='http_error', http_status=503)
        self.manager.runner = failure
        self.manager.start(jid, self.f.payload)
        self.f.wait(jid, 'failed')
        return self.manager.get(jid)

    def retry(self, attempt=1):
        return {**self.f.payload, 'retry': True, 'expected_attempt': attempt}

    def test_safe_specific_failure_and_unknown_exception_never_echo_credentials(self):
        jid = self.ready()
        failed = self.fail_review(jid)
        self.assertTrue(failed['retryable'])
        self.assertEqual(failed['attempt'], 1)
        self.assertEqual(failed['failure_diagnostics']['http_status'], 503)
        self.assertEqual(failed['failure_diagnostics']['stage'], 'model_execution')
        self.assertGreater(failed['failure_diagnostics']['message_bytes'], 0)
        self.assertIn('503', failed['error'])
        error, details = self.manager._failure_details(RuntimeError('secret-password raw provider body'),
            {'api_key': 'secret-password'}, stage='model_execution')
        self.assertNotIn('secret-password', error + json.dumps(details))
        self.assertNotIn('RuntimeError', error)
        self.assertEqual(details['error_type'], 'RuntimeError')
        _, details = self.manager._failure_details(ModelClientError('返回内容未完成。', code='output_limit',
            finish_reason='length', usage={'completion_tokens': 8192, 'unexpected_secret': 'private'}),
            {'engine_key': 'compatible_api', 'max_tokens': 8192}, stage='model_execution')
        self.assertEqual(details['max_tokens'], 8192)
        self.assertEqual(details['finish_reason'], 'length')
        self.assertEqual(details['usage'], {'completion_tokens': 8192})

    def test_explicit_retry_archives_and_keeps_contract_checklist_plan_and_answers(self):
        jid = self.ready(answered=True)
        failed = self.fail_review(jid)
        directory = self.manager._directory(jid)
        evidence = {name: (directory / name).read_bytes() for name in
            ('status.json', 'request.json', 'prompt-snapshot.json', 'final-sources/research.json')}
        inputs = {name: (directory / name).read_bytes() for name in
            ('source.docx', 'contract.json', 'sources.json', 'preparation.json')}
        self.manager.runner = self.original_runner
        with self.assertRaises(JobConflict):
            self.manager.start(jid, self.f.payload)
        self.assertTrue(self.manager.start(jid, self.retry())[1])
        self.f.wait(jid, 'completed')
        for name, data in evidence.items():
            self.assertEqual((directory / 'attempt-history' / '1' / name).read_bytes(), data)
        for name, data in inputs.items():
            self.assertEqual((directory / name).read_bytes(), data)
        complete = self.manager.get(jid)
        self.assertEqual(complete['attempt'], 2)
        self.assertFalse(complete['retryable'])
        self.assertEqual(complete['preparation'], failed['preparation'])
        self.assertEqual(self.manager.load_report(jid)['id'], jid)

    def test_running_duplicate_does_not_call_again_and_failed_stale_attempt_rejects(self):
        jid = self.ready()
        self.fail_review(jid)
        entered, release, calls = threading.Event(), threading.Event(), []
        self.addCleanup(release.set)
        def failure(*_):
            calls.append(1)
            entered.set()
            release.wait(3)
            raise ModelClientError('模型服务连接超时。', code='timeout')
        self.manager.runner = failure
        self.assertTrue(self.manager.start(jid, self.retry())[1])
        self.assertTrue(entered.wait(2))
        self.assertFalse(self.manager.start(jid, self.retry())[1])
        self.assertEqual(calls, [1])
        release.set()
        self.f.wait(jid, 'failed')
        with self.assertRaises(JobConflict):
            self.manager.start(jid, self.retry())
        self.assertEqual(calls, [1])

    def test_orphaned_review_is_retryable_without_repreparing(self):
        jid = self.ready()
        status = self.manager._status(jid)
        status.update(status='running', attempt=1, owner_pid=99999999)
        _atomic_json(self.manager._directory(jid) / 'status.json', status)
        recovered = self.manager.get(jid)
        self.assertEqual(recovered['status'], 'failed')
        self.assertTrue(recovered['retryable'])
        self.manager.start(jid, self.retry())
        self.f.wait(jid, 'completed')

    def test_no_overwrite_when_evidence_archive_fails(self):
        jid = self.ready()
        self.fail_review(jid)
        directory = self.manager._directory(jid)
        status, request = (directory / 'status.json').read_bytes(), (directory / 'request.json').read_bytes()
        with patch.object(self.manager, '_archive_attempt', side_effect=JobError('存储暂不可用')):
            with self.assertRaises(JobError):
                self.manager.start(jid, self.retry())
        self.assertEqual((directory / 'status.json').read_bytes(), status)
        self.assertEqual((directory / 'request.json').read_bytes(), request)

    def saved_response(self, jid):
        request = json.loads((self.manager._directory(jid) / 'request.json').read_text(encoding='utf-8'))
        raw = self.original_runner(request['messages'], {'review_phase': 'review'})
        path = self.f.root / 'actual-response-fixture.json'
        path.write_text(json.dumps({'model': 'fixture', 'created': 123456789,
            'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}]}), encoding='utf-8')
        return raw, request['request_digest'], {'response_path': str(path.resolve()), 'response_sha256': _sha256(path.read_bytes())}

    def test_saved_response_completion_uses_frozen_request_and_is_readback_compatible(self):
        jid = self.ready(answered=True)
        self.fail_review(jid)
        directory = self.manager._directory(jid)
        original_status, original_request = (directory / 'status.json').read_bytes(), (directory / 'request.json').read_bytes()
        raw, digest, provenance = self.saved_response(jid)
        self.manager.runner = lambda *_: self.fail('completion called model')
        self.manager.research_completion = lambda *_: self.fail('completion called search')
        complete = self.manager.complete_saved_response(jid, raw, expected_request_digest=digest, provenance=provenance)
        self.assertEqual(complete['status'], 'completed')
        self.assertEqual(complete['attempt'], 2)
        archive = directory / 'attempt-history' / '1'
        self.assertEqual((archive / 'status.json').read_bytes(), original_status)
        self.assertEqual((archive / 'request.json').read_bytes(), original_request)
        loaded = self.manager.load_report(jid)
        self.assertEqual(loaded['report']['metadata']['response_recovery']['original_attempt'], 1)
        self.assertEqual(loaded['report']['metadata']['response_recovery']['response_sha256'], provenance['response_sha256'])
        files = {p.name: p.read_bytes() for p in directory.glob('*.json')}
        self.manager.complete_saved_response(jid, raw, expected_request_digest=digest, provenance=provenance)
        self.assertEqual({p.name: p.read_bytes() for p in directory.glob('*.json')}, files)

    def test_saved_response_wrong_digest_or_provenance_does_not_change_task(self):
        jid = self.ready()
        self.fail_review(jid)
        raw, digest, provenance = self.saved_response(jid)
        directory = self.manager._directory(jid)
        before = {p.name: p.read_bytes() for p in directory.glob('*.json')}
        for bad_digest, bad_provenance in [('0' * 64, provenance), (digest, {**provenance, 'response_sha256': '0' * 64})]:
            with self.assertRaises(JobError):
                self.manager.complete_saved_response(jid, raw, expected_request_digest=bad_digest, provenance=bad_provenance)
        self.assertEqual({p.name: p.read_bytes() for p in directory.glob('*.json')}, before)
        self.assertFalse((directory / 'attempt-history').exists())


class ReviewRetryHTTPTests(unittest.TestCase):
    def test_start_retry_contract_over_real_local_http(self):
        f = http_fixtures.ReviewJobTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        _, _, created = f.create_general(legacy=True)
        jid = created['job']['job_id']
        def failure(*_):
            raise ModelClientError('模型服务返回 HTTP 503。', code='http_error', http_status=503)
        f.server.jobs.runner = failure
        body = {'engine_key': 'stub', 'consent': True, 'config': {'model': 'fixture'}}
        self.assertEqual(f.json_request('POST', f'/api/jobs/{jid}/start', body)[0], 202)
        f.wait_status(jid, 'failed')
        code, _, current = f.json_request('GET', f'/api/jobs/{jid}')
        self.assertEqual(code, 200)
        self.assertTrue(current['job']['retryable'])
        f.server.jobs.runner = lambda messages, _: http_fixtures.general_result(messages)
        retry = {**body, 'retry': True, 'expected_attempt': current['job']['attempt']}
        self.assertEqual(f.json_request('POST', f'/api/jobs/{jid}/start', retry)[0], 202)
        f.wait_status(jid, 'completed')
        self.assertEqual(f.server.jobs.get(jid)['attempt'], 2)
