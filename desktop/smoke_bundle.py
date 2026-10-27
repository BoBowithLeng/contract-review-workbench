"""Check a running, freshly extracted public bundle using only synthetic input.

Never point this at a used installation. The initial empty-data checks run
before any writes. No paid provider or real contract is used.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop.synthetic_fixture import SYNTHETIC_CONTRACT, contract_bytes, fixture_runner
from docx import Document
from report_export import EXPERIMENTAL_TOOL_NOTICE, AI_REVIEW_NOTICE


def smoke(app: Path, output: Path) -> dict:
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use a new, empty output directory.')
    data = app / '我的数据'
    marker = json.loads((data / '.desktop-instance.json').read_text(encoding='utf-8'))
    origin = f'http://127.0.0.1:{marker["port"]}'

    def request(path, body=None, raw=False):
        req = urllib.request.Request(origin + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Origin': origin, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            value = response.read()
        return value if raw else json.loads(value)

    catalog = request('/api/workbench')
    assert catalog['documents'] == [] and not catalog.get('pending_jobs'), 'Use a fresh installation.'
    settings = request('/api/model-settings')
    assert settings['profiles'] == [], 'Never test over saved model connections.'
    assert [e['key'] for e in request('/api/review-capabilities')['engines']] == ['compatible_api']
    assert (app / 'LICENSE.txt').is_file() and (app / '先读我.txt').is_file()
    output.mkdir(parents=True, exist_ok=True)
    calls = []
    complete = fixture_runner()

    class MockModel(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, body):
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            assert self.path == '/v1/models'
            calls.append('models')
            self.respond({'data': [{'id': 'local-smoke-fixture'}]})

        def do_POST(self):
            assert self.path == '/v1/chat/completions'
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            phase = 'review_preparation' if 'preparation' not in calls else 'review'
            calls.append('preparation' if phase == 'review_preparation' else 'review')
            text = complete(body['messages'], {'review_phase': phase})
            self.respond({'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]})

    mock = ThreadingHTTPServer(('127.0.0.1', 0), MockModel)
    thread = threading.Thread(target=mock.serve_forever, daemon=True)
    thread.start()

    def wait_job(jid, status):
        for _ in range(120):
            job = request('/api/jobs/' + jid)['job']
            if job['status'] == status:
                return job
            if job['status'] == 'failed':
                raise AssertionError(job)
            time.sleep(.1)
        raise TimeoutError(status)

    try:
        config = {'base_url': f'http://127.0.0.1:{mock.server_port}/v1',
            'api_key': 'desktop-smoke-not-a-real-key', 'model': 'local-smoke-fixture'}
        saved = request('/api/model-settings', {'expected_revision': settings['revision'],
            'action': 'save', 'provider': '自定义兼容接口', 'config': config})
        assert request('/api/model-settings')['profiles'][0]['api_key'] == config['api_key']
        assert not any(config['api_key'].encode() in p.read_bytes()
            for p in (data / 'runtime' / 'reader-state').rglob('*.json'))
        assert request('/api/models', {'config': config})['models'][0]['id'] == config['model']
        checklist = next(c for c in request('/api/checklists')['checklists']
            if c['id'] == SYNTHETIC_CONTRACT['checklist_id'])
        job = request('/api/jobs', {'document_name': '合成测试.docx',
            'docx_base64': base64.b64encode(contract_bytes()).decode(),
            'context': {'contract_type': 'general', 'our_party': '示例客户公司', 'our_role': '客户方'},
            'checklist_snapshot': checklist})['job']
        jid = job['job_id']
        payload = {'engine_key': 'compatible_api', 'config': config, 'consent': True}
        request('/api/jobs/' + jid + '/prepare', payload)
        wait_job(jid, 'prepared')
        request('/api/jobs/' + jid + '/start', payload)
        wait_job(jid, 'completed')
        endpoint = '/api/reports/' + jid
        report = request(endpoint)
        assert len(report['report']['findings']) == len(SYNTHETIC_CONTRACT['opinions'])
        delivery = request(endpoint + '/delivery')
        raw = request(endpoint + '/delivery/export', {'expected_revision': delivery['state']['revision'],
            'source_sha256': delivery['source']['source_sha256'],
            'review_revision': report['state']['revision'], 'variants': ['clean', 'tracked', 'report']}, raw=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert len(archive.namelist()) == 3
            for name in archive.namelist():
                assert Path(name).name == name and name.endswith('.docx')
                value = archive.read(name)
                text = '\n'.join(p.text for p in Document(io.BytesIO(value)).paragraphs)
                if '审查报告' in name:
                    assert EXPERIMENTAL_TOOL_NOTICE in text and AI_REVIEW_NOTICE in text
                else:
                    assert EXPERIMENTAL_TOOL_NOTICE not in text and AI_REVIEW_NOTICE not in text
                (output / name).write_bytes(value)
        request('/api/model-settings', {'expected_revision': saved['revision'],
            'action': 'remove', 'provider': '自定义兼容接口'})
        assert request('/api/model-settings')['profiles'] == []
        assert calls == ['models', 'preparation', 'review']
        result = {'empty_first_start': True, 'no_bundled_credentials': True,
            'model_key_encrypted': True, 'mock_model_calls': calls, 'paid_model_calls': 0,
            'new_review_completed': True, 'word_exports': 3, 'report_notice_verified': True,
            'contract_body_notices_absent': True, 'job_id': jid}
        (output / 'smoke-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
    finally:
        mock.shutdown()
        mock.server_close()
        thread.join(3)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(smoke(args.app_dir.resolve(), args.output_dir.resolve()), ensure_ascii=False))
