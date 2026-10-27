"""Offline seed, empty-workbench HTTP and first-start archive checks."""
from __future__ import annotations

import base64
import http.client
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from desktop.build_seed import PROJECT, build_seed, pack_runtime
from desktop.synthetic_fixture import SYNTHETIC_CONTRACT, contract_bytes
from reader_server import create_server


class DesktopSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        with patch('socket.create_connection', side_effect=AssertionError('Seed build must be offline')):
            self.manifest = build_seed(self.base / 'seed')
        self.root = self.base / 'seed'

    def test_reproducible_manifest_only_seed(self):
        with patch('socket.create_connection', side_effect=AssertionError('Seed build must be offline')):
            manifest = build_seed(self.base / 'second')
        def files(directory):
            return {p.relative_to(directory).as_posix(): p.read_bytes()
                    for p in directory.rglob('*') if p.is_file()}
        self.assertEqual(files(self.root), files(self.base / 'second'))
        self.assertEqual(manifest, self.manifest)
        self.assertEqual(set(files(self.root)), {'runtime/demo-seed.json'})
        self.assertEqual(manifest['samples'], [])
        self.assertEqual(manifest['files'], {})
        self.assertEqual(json.loads((self.root / 'runtime' / 'demo-seed.json').read_text(encoding='utf-8')), manifest)

    def test_nonempty_destination_never_overwritten(self):
        marker = self.root / 'keep.txt'
        marker.write_text('Existing data', encoding='utf-8')
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ValueError, 'empty'):
            build_seed(self.root)
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        with self.assertRaisesRegex(ValueError, 'empty'):
            build_seed(marker)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'Existing data')

    def test_real_http_empty_workbench_and_new_upload_without_model_call(self):
        calls = []
        def api_guard(messages, config):
            calls.append(config)
            raise AssertionError('Listing and upload must not invoke a model')
        server = create_server(root=self.root, resource_root=PROJECT, include_legacy_reports=False,
            source_paths={'detailed': None, 'minimal': None, 'table': None},
            review_runner=api_guard,
            review_engines=[{'key': 'compatible_api', 'label': 'Configured API', 'available': True,
                             'config_fields': ['model', 'base_url', 'api_key']}])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def request(path, body=None):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=30)
                try:
                    headers = {'Origin': f'http://127.0.0.1:{server.server_port}', 'Content-Type': 'application/json'}
                    connection.request('GET' if body is None else 'POST', path,
                        None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8'), headers)
                    response = connection.getresponse()
                    data = response.read()
                    self.assertLess(response.status, 400, data.decode('utf-8', errors='replace')[:1500])
                    return json.loads(data)
                finally:
                    connection.close()
            self.assertEqual(request('/api/workbench')['documents'], [])
            self.assertNotIn('prebuilt_demo', json.dumps(request('/api/review-capabilities')))
            new = request('/api/jobs', {'document_name': SYNTHETIC_CONTRACT['name'],
                'docx_base64': base64.b64encode(contract_bytes()).decode(),
                'context': {'contract_type': 'general', 'our_party': '示例客户公司', 'our_role': '客户方'}})['job']
            self.assertEqual(new['status'], 'ready')
            self.assertIsNone(new['report_id'])
            self.assertIsNone(new['engine_key'])
            self.assertEqual(calls, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


class SyntheticFixtureTests(unittest.TestCase):
    def test_fixture_completes_a_real_job_pipeline(self):
        import time
        from desktop.synthetic_fixture import fixture_runner
        from review_jobs import ReviewJobManager
        with tempfile.TemporaryDirectory() as temp:
            catalog = json.loads((PROJECT / 'builtin_checklists.json').read_text(encoding='utf-8-sig'))
            checklist = next(c for c in catalog['checklists'] if c['id'] == SYNTHETIC_CONTRACT['checklist_id'])
            manager = ReviewJobManager(Path(temp) / 'review-jobs', runner=fixture_runner(),
                engines=[{'key': 'fixture', 'label': 'fixture', 'available': True, 'model': 'fixture', 'config_fields': []}],
                prompt_path=PROJECT / 'prompts' / 'initial_review.md')
            job = manager.create({'document_name': SYNTHETIC_CONTRACT['name'],
                'docx_base64': base64.b64encode(contract_bytes()).decode('ascii'),
                'context': {'contract_type': 'general', 'our_party': '示例客户公司', 'our_role': '客户方'},
                'checklist_snapshot': checklist})['job']
            payload = {'engine_key': 'fixture', 'config': {}, 'consent': True}

            def wait(expected):
                for _ in range(300):
                    status = manager.get(job['job_id'])['status']
                    if status == expected:
                        return
                    self.assertNotEqual(status, 'failed', manager.get(job['job_id']))
                    time.sleep(.02)
                self.fail('timed out waiting for ' + expected)

            manager.prepare(job['job_id'], payload)
            wait('prepared')
            manager.start(job['job_id'], payload)
            wait('completed')
            report = manager.load_report(job['job_id'])['report']
            self.assertEqual(len(report['findings']), len(SYNTHETIC_CONTRACT['opinions']))


class SeedArchiveRoundTripTests(unittest.TestCase):
    def test_reproducible_archive_initializes_once_and_preserves_existing_data(self):
        from app_paths import DesktopPaths, ensure_writable_directory
        from desktop_runtime import initialize_data
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            build_seed(base / 'tree')
            seed = base / 'res' / 'desktop_seed' / 'runtime.zip'
            seed.parent.mkdir(parents=True)
            pack_runtime(base / 'tree' / 'runtime', seed)
            again = base / 'again.zip'
            pack_runtime(base / 'tree' / 'runtime', again)
            self.assertEqual(again.read_bytes(), seed.read_bytes())
            with zipfile.ZipFile(seed) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.namelist(), ['demo-seed.json'])
                self.assertEqual(json.loads(archive.read('demo-seed.json'))['samples'], [])
            before = seed.read_bytes()
            with self.assertRaises(FileExistsError):
                pack_runtime(base / 'tree' / 'runtime', seed)
            self.assertEqual(seed.read_bytes(), before)
            paths = DesktopPaths(base / 'res', base / 'data')
            ensure_writable_directory(paths.data)
            self.assertTrue(initialize_data(paths))
            expected = {p.relative_to(base / 'tree' / 'runtime').as_posix(): p.read_bytes()
                        for p in (base / 'tree' / 'runtime').rglob('*') if p.is_file()}
            actual = {p.relative_to(paths.runtime).as_posix(): p.read_bytes()
                      for p in paths.runtime.rglob('*') if p.is_file()}
            self.assertEqual(actual, expected)
            marker = paths.runtime / 'user-contract.txt'
            marker.write_text('Keep my contract', encoding='utf-8')
            (paths.runtime / 'demo-seed.json').unlink()
            self.assertFalse(initialize_data(paths))
            self.assertEqual(marker.read_text(encoding='utf-8'), 'Keep my contract')
            self.assertFalse((paths.runtime / 'demo-seed.json').exists())


if __name__ == '__main__':
    unittest.main()
