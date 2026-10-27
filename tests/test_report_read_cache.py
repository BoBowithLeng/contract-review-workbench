"""Bounded, content-keyed read caches. Synthetic sources, no provider calls."""
import copy
import io
import json
import os
import unittest
from unittest.mock import patch

from docx import Document
from docx_delivery import inspect_source, validate_plan, render_docx, _validation_material, _load_source
from draft_plan import validate_docx_plan
from review_jobs import JobError, JobConflict, ReviewJobManager
from tests import test_review_preparation as fixtures


class SourceMaterialCacheTests(unittest.TestCase):
    def setUp(self):
        _validation_material.cache_clear()
        self.addCleanup(_validation_material.cache_clear)
        document = Document()
        document.add_paragraph('1.1 原合同正文。')
        stream = io.BytesIO()
        document.save(stream)
        self.data = stream.getvalue()
        self.draft = {'status': 'ready', 'edits': [{'piece_id': 'P000', 'new_text': '1.1 明确后的合同正文。'}], 'insertions': []}

    def test_many_plans_reuse_one_exact_source_without_exposing_cached_metadata(self):
        with patch('docx_delivery._load_source', wraps=_load_source) as parse:
            for _ in range(19):
                validate_docx_plan(self.draft, self.data)
            self.assertEqual(parse.call_count, 1)
        original = inspect_source(self.data)
        damaged = inspect_source(self.data)
        damaged['pieces'][0]['text'] = '调用方改坏的对象'
        self.assertEqual(inspect_source(self.data), original)
        self.assertEqual(_validation_material.cache_info().maxsize, 4)

    def test_rendering_cannot_mutate_cached_original_xml(self):
        original = inspect_source(self.data)
        piece = original['pieces'][0]
        plan = {'schema_version': 1, 'source_sha256': original['source_sha256'], 'revision': 1,
            'author': 'Synthetic test', 'created_at': '2026-10-06T00:00:00Z', 'comments': [],
            'edits': [{'id': 'edit-test', 'piece_id': piece['id'], 'paragraph_sha256': piece['paragraph_sha256'],
                'start': 0, 'end': len(piece['text']), 'old_text': piece['text'], 'new_text': '1.1 改后正文。', 'confirmed': True}]}
        clean = render_docx(self.data, plan, 'clean')
        render_docx(self.data, plan, 'tracked')
        self.assertEqual(inspect_source(self.data), original)
        self.assertEqual(inspect_source(clean)['pieces'][0]['text'], '1.1 改后正文。')
        self.assertEqual(validate_plan(self.data, plan)['edits'][0]['old_text'], piece['text'])


class ReportReadCacheTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PreparationJobTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.manager = self.f.manager
        self.jid = self.f.create(fixtures.checklist())
        self.f.prepare(self.jid)
        self.manager.start(self.jid, self.f.payload)
        self.f.wait(self.jid, 'completed')
        self.directory = self.manager._directory(self.jid)

    def test_warm_read_isolated_from_overlay_mutation_and_still_hashes_content(self):
        with patch.object(self.manager, '_load_report_uncached', wraps=self.manager._load_report_uncached) as validate:
            first = self.manager.load_report(self.jid)
            first['report']['limitations'].append('污染')
            first['contract']['pieces'][0]['text'] = '污染原稿'
            second = self.manager.load_report(self.jid)
            self.assertEqual(validate.call_count, 1)
            self.assertNotIn('污染', second['report']['limitations'])
            self.assertNotEqual(second['contract']['pieces'][0]['text'], '污染原稿')

    def test_same_timestamp_tampering_all_signed_current_evidence_misses_and_rejects(self):
        cases = {
            'status.json': lambda v: v['context'].update(our_party='冒充另一方'),
            'contract.json': lambda v: v['pieces'][0].update(text='改变原文'),
            'sources.json': lambda v: v.append({'id': 'fake-source'}),
            'request.json': lambda v: v['messages'][0].update(content='改变任务要求'),
            'response.json': lambda v: v.update(raw_text=v['raw_text'] + ' '),
            'result.json': lambda v: v['report']['limitations'].append('伪造结果'),
            'preparation.json': lambda v: v['preparation'].update(original_requirements='改变要求'),
            'prompt-snapshot.json': lambda v: v.update(text=v['text'] + '改变提示'),
            'final-sources/research.json': lambda v: v.update(memo='改变研究资料'),
            'preparation-attempts/1/request.json': lambda v: v.update(prompt_text=v['prompt_text'] + '改变审前提示'),
            'preparation-attempts/1/response.json': lambda v: v.update(raw_text=v['raw_text'] + ' '),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                self.manager.load_report(self.jid)
                path = self.directory / name
                original, stat = path.read_bytes(), path.stat()
                value = json.loads(original)
                mutate(value)
                path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                try:
                    with self.assertRaises((JobError, JobConflict)):
                        self.manager.load_report(self.jid)
                finally:
                    path.write_bytes(original)
                    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.manager.load_report(self.jid)
        path = self.directory / 'source.docx'
        original, stat = path.read_bytes(), path.stat()
        path.write_bytes(original + b' ')
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaises(JobError):
            self.manager.load_report(self.jid)

    def test_missing_dependency_does_not_use_a_previous_success(self):
        self.manager.load_report(self.jid)
        (self.directory / 'response.json').unlink()
        with self.assertRaises(JobError):
            self.manager.load_report(self.jid)

    def test_cache_is_bounded_not_one_per_contract_forever(self):
        manager = ReviewJobManager(self.f.root / 'unused')
        with patch.object(manager, '_status', side_effect=lambda jid: {'status': 'completed', 'report_id': jid}), \
             patch.object(manager, '_report_content_signature', side_effect=lambda jid, status: (jid,)), \
             patch.object(manager, '_load_report_uncached', side_effect=lambda jid: {'id': jid}):
            for index in range(20):
                manager.load_report(f'job-{index:032x}')
        self.assertEqual(len(manager._report_cache), 16)
        self.assertNotIn('job-' + '0' * 32, manager._report_cache)

    def test_rereview_inherited_input_is_also_a_content_dependency(self):
        from tests.test_contract_rereview import RereviewTests
        fixture = RereviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        child, started = fixture.manager.start(fixture.loaded, fixture.state, fixture.payload())
        self.assertTrue(started)
        jid = child['job_id']
        fixture.f.wait(jid, 'completed')
        jobs = fixture.f.manager
        jobs.load_report(jid)
        path = jobs._directory(jid) / 'rereview-input.json'
        stat = path.stat()
        value = json.loads(path.read_text(encoding='utf-8'))
        value['origin']['facts_revision'] += 1
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaises(JobConflict):
            jobs.load_report(jid)

    def test_workbench_reuses_same_round_loaded_report_for_information_gate(self):
        from tests.test_checklist_workspace_integration import ChecklistWorkspaceHTTPTests
        fixture = ChecklistWorkspaceHTTPTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        jid = fixture.create()
        jobs = fixture.server.jobs
        jobs.prepare(jid, fixture.payload)
        fixture.wait_status(jid, 'prepared')
        jobs.start(jid, fixture.payload)
        fixture.wait_status(jid, 'completed')
        with patch('reader_server.REPORTS', {}), patch.object(jobs, 'load_report', wraps=jobs.load_report) as reads:
            catalog = fixture.server.workbench_catalog()
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(catalog['documents'][0]['reviews'][0]['information_gate_status'], 'ready')
