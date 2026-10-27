"""Real HTTP and Word ZIP integration using an injected, non-network model."""
from tests.source_fixtures import labor_sources
import copy
import http.client
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docx import Document
from reader_server import REPORTS, create_server
from tool_review import prepare


class DeliveryReliabilityHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project = self.root / 'project'
        project.mkdir()
        shutil.copytree(Path(__file__).resolve().parents[1] / 'reader_ui', project / 'reader_ui')
        source = project / '合成验收合同.docx'
        document = Document()
        for line in ['1.3 双方各指定联系人，重要事项在项目群确认。', '甲方联系人甲，邮箱甲@example.com。',
                     '乙方联系人乙，邮箱乙@example.com。', '8.1 未尽事项由双方协商补充。']:
            document.add_paragraph(line)
        document.save(source)
        self.new1 = '1.3 双方各指定联系人，重大变更由授权代表通过指定邮箱书面确认。'
        self.new8 = '8.2 通知以指定邮箱发出，自发送成功之日视为送达。'
        self.draft = self.new1 + '\n' + self.new8
        for spec in REPORTS.values():
            directory = project / 'runtime' / spec['directory']
            directory.mkdir(parents=True)
            packet = prepare(source, directory / 'request.json', detailed=True, sources=labor_sources())
            pieces = packet['contract']['pieces']
            self.ids = [p['id'] for p in pieces]
            result = {'findings': [{'id': 'F01', 'rule_id': 'LC-02', 'title': '合成验收 联系人与通知安排',
                'status': '发现风险', 'risk': '中', 'evidence_status': '依据已核',
                'refs': [{'id': p['id'], 'quote': p['text']} for p in pieces[:3]],
                'source_ids': ['LCL30_50'], 'reason': '仅用作工程流程验收。',
                'suggestion': '修改1.3并新增8.2通知条款。', 'clause_draft': self.draft, 'questions': []}],
                'checks': [{'rule_id': rid, 'status': '发现风险' if rid == 'LC-02' else '未检出', 'reason': '合成测试。'}
                           for rid in ['LC-01', 'LC-02', 'LC-03']], 'options': [], 'uncovered': []}
            (directory / 'response.json').write_text(json.dumps({'request_digest': packet['request_digest'],
                'model': 'injected-fixture', 'result': result}, ensure_ascii=False), encoding='utf-8')
        self.calls = 0
        def runner(messages, config):
            self.calls += 1
            p = json.loads(messages[1]['content'].split('UNTRUSTED DATA:\n', 1)[1])
            return json.dumps({'status': 'ready', 'edits': [{'piece_id': self.ids[0], 'new_text': self.new1}],
                'insertions': [{'after_piece_id': self.ids[3], 'new_text': self.new8}], 'questions': [],
                'comment': '请确认联系人授权及通知安排。',
                'paragraph_actions': [{'piece_id': pid, 'action': 'modify' if i == 0 else 'retain',
                    'reason': '明确联系人授权。' if i == 0 else '联系人邮箱已经明确，保留供通知条款引用。'}
                    for i, pid in enumerate(self.ids[:3])],
                'suggestion_actions': [{'unit_id': p['suggestion_units'][0]['unit_id'], 'action': 'implemented',
                    'edit_piece_ids': [self.ids[0]], 'insertion_indices': [], 'reason': '已经修改1.3的重大事项确认方式。'},
                    {'unit_id': p['suggestion_units'][1]['unit_id'], 'action': 'implemented',
                     'edit_piece_ids': [], 'insertion_indices': [0], 'reason': '在8.1之后新增8.2通知送达约定。'}]}, ensure_ascii=False)
        self.server = create_server(root=project, state_dir=self.root/'state', jobs_dir=self.root/'jobs',
            source_paths={key: source for key in REPORTS}, review_runner=runner, draft_runner=runner,
            review_engines=[{'key': 'codex_cli', 'label': 'Injected test runner', 'available': True}])
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join(timeout=3)))
        self.port = self.server.server_port

    def request(self, path, body=None, raw=False):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('POST' if body is not None else 'GET', path,
                     json.dumps(body).encode() if body is not None else None,
                     {'Origin': f'http://127.0.0.1:{self.port}', 'Content-Type': 'application/json'})
        response = conn.getresponse()
        data = response.read()
        status = response.status
        conn.close()
        self.assertLess(status, 300, data.decode(errors='replace'))
        return data if raw else json.loads(data)

    def test_new_clause_and_retained_reasons_survive_confirmation_export_and_undo(self):
        delivery = self.request('/api/reports/detailed/delivery')
        candidate = self.request('/api/reports/detailed/drafts/F01', {'expected_revision': 0,
            'source_sha256': delivery['source']['source_sha256'], 'draft_text': self.draft, 'clarification': ''})
        for _ in range(100):
            if candidate['status'] != 'running': break
            time.sleep(.03)
            candidate = self.request('/api/reports/detailed/drafts/F01')
        self.assertEqual(candidate['status'], 'ready', candidate)
        self.assertTrue(candidate['coverage_complete'])
        edits = []
        for i, item in enumerate(candidate['edits'] + candidate['insertions']):
            edits.append({k: v for k, v in dict(item, id=f'edit-test-{i}', confirmed=True, finding_ids=['F01']).items()
                          if k not in {'location'}})
        payload = {'expected_revision': 0, 'source_sha256': delivery['source']['source_sha256'], 'author': '工程验收',
                   'edits': edits, 'comments': [{'id': 'note-test', 'edit_id': edits[-1]['id'], 'text': '新增通知条款。', 'confirmed': True}],
                   'draft_confirmation': {'finding_id': 'F01', 'candidate_id': candidate['candidate_id']}}
        state = self.request('/api/reports/detailed/delivery', payload)['state']
        current = self.request('/api/reports/detailed/delivery')['state']
        scope = current['draft_confirmations']['F01']
        self.assertEqual([x['action'] for x in scope['paragraph_actions']], ['modify', 'retain', 'retain'])
        self.assertEqual(len(scope['suggestion_actions']), 2)
        zipped = self.request('/api/reports/detailed/delivery/export', {'expected_revision': current['revision'],
            'source_sha256': delivery['source']['source_sha256'], 'review_revision': 0,
            'variants': ['clean', 'tracked_comments', 'report']}, raw=True)
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        with zipfile.ZipFile(io.BytesIO(zipped)) as package:
            clean_name = next(n for n in package.namelist() if '洁净版' in n)
            tracked_name = next(n for n in package.namelist() if '修订与批注' in n)
            for name in [clean_name, tracked_name]:
                with zipfile.ZipFile(io.BytesIO(package.read(name))) as doc:
                    root = ET.fromstring(doc.read('word/document.xml'))
                    paragraphs = [''.join(t.text or '' for t in p.findall('.//w:t', ns)) for p in root.findall('.//w:p', ns)]
                    self.assertIn(self.new8, paragraphs)
                    self.assertEqual(sum(self.new8 == p for p in paragraphs), 1)
        # An unrelated comment change preserves both retain explanations.
        update = {**payload, 'expected_revision': state['revision'], 'comments': []}
        update.pop('draft_confirmation')
        updated = self.request('/api/reports/detailed/delivery', update)['state']
        self.assertEqual(updated['draft_confirmations']['F01']['paragraph_actions'], scope['paragraph_actions'])
        removed = self.request('/api/reports/detailed/delivery', {**update, 'expected_revision': updated['revision'], 'edits': []})['state']
        self.assertNotIn('F01', removed['draft_confirmations'])
        restored = self.request('/api/reports/detailed/delivery/undo', {'expected_revision': removed['revision'],
            'source_sha256': delivery['source']['source_sha256']})['state']
        self.assertEqual(restored['draft_confirmations']['F01']['paragraph_actions'], scope['paragraph_actions'])
        self.assertEqual(self.calls, 1)


if __name__ == '__main__':
    if '--serve' in sys.argv:
        fixture = DeliveryReliabilityHTTPTests()
        fixture.setUp()
        print(json.dumps({'port': fixture.port, 'root': str(fixture.root)}), flush=True)
        try: input()
        finally: fixture.doCleanups()
    else:
        unittest.main()
