"""Explicit whole-document rereview from confirmed edits and a frozen context."""
from __future__ import annotations

import base64
import copy
import json
import re
import threading
from pathlib import Path
from typing import Any

from contract_core import parse_docx
from contract_facts import sanitize_fact_history
from reader_delivery import DeliveryUnavailable, DeliveryStateCorrupt
from review_jobs import JobConflict, JobError, _atomic_json, _read_json
from review_preparation import digest, pending
from checklist_groups import freeze_groups
from native_web_search import validate_deepseek_address


class RereviewManager:
    def __init__(self, root: Path, jobs, delivery, facts):
        self.root, self.jobs, self.delivery, self.facts = Path(root), jobs, delivery, facts
        self.lock = threading.RLock()
        self.deleted_request_provider = None

    def latest_completed_child(self, parent_id: str, deleted: set[str]) -> str | None:
        """The newest re-review started from this report whose result can be opened."""
        folder = self.root / parent_id
        if not folder.is_dir():
            return None
        found = []
        for path in folder.glob('*.json'):
            try:
                record = _read_json(path, '复审提交记录')
                job_id = record.get('job_id') if isinstance(record, dict) else None
                if record.get('parent_report_id') != parent_id or not isinstance(job_id, str) or job_id in deleted:
                    continue
                status = self.jobs.get(job_id)
            except (JobError, KeyError, ValueError):
                continue  # A damaged or vanished child must not hide the parent report.
            if status.get('status') == 'completed' and status.get('report_id') == job_id:
                found.append((str(status.get('created_at') or ''), job_id))
        return max(found)[1] if found else None

    def _snapshot(self, loaded: dict, review_state: dict) -> dict:
        ctx = copy.deepcopy(loaded['report']['context'])
        if not ctx.get('our_party'):
            raise DeliveryUnavailable('这份旧记录没有保存我方称谓，请通过新建审查补齐立场。')
        document = self.delivery.rereview_snapshot(loaded)
        facts = self.facts.get(loaded)
        active = [copy.deepcopy(f) for f in facts['facts'] if f.get('active', True)]
        confirmed = {'revision': facts['revision'], 'facts': active, 'questions': copy.deepcopy(facts.get('questions', []))}
        ctx.update(contract_type='general', business_facts='')
        ctx.setdefault('business_goal', '')
        ctx.setdefault('contract_type_label', loaded['report'].get('contract_type_label') or '合同')
        parent_preparation = loaded['report'].get('preparation')
        preparation = copy.deepcopy(parent_preparation) if parent_preparation else pending()
        preparation['confirmed_facts'] = confirmed
        preparation['questions'] = copy.deepcopy(facts.get('questions', []))
        preparation['answers_saved'] = True
        preparation['information_policy_version'] = 2
        preparation.setdefault('common_rules_text', loaded['report'].get('metadata', {}).get('common_rules_text', ''))
        preparation['original_requirements'] = ctx['business_goal']
        checklist = None
        if re.fullmatch(r'job-[0-9a-f]{32}', loaded['id']):
            checklist = self.jobs.get(loaded['id']).get('checklist_snapshot')
        if not parent_preparation and checklist:
            preparation['checks'] = [{**copy.deepcopy(item), 'source': 'checklist', 'source_texts': []}
                                     for item in checklist.get('items', []) if item.get('enabled')]
        # This is a new round's input, not a mutation of the historical parent.
        freeze_groups(preparation)
        actions = self.delivery.action_context(loaded, review_state, strict=True)
        previous = []
        for finding in loaded['report'].get('findings', []):
            fid = finding['id']
            previous.append({k: copy.deepcopy(finding.get(k)) for k in (
                'id', 'title', 'status', 'risk', 'reason', 'suggestion', 'refs', 'location_note',
                'opinion_revision', 'revision_action')})
            previous[-1]['action'] = actions['actions'].get(fid, {})
            previous[-1]['human_note'] = review_state.get('reviews', {}).get(fid, {}).get('note', '')
        origin = {
            'parent_report_id': loaded['id'], 'parent_request_digest': loaded['request_digest'],
            'delivery_revision': document['delivery_revision'], 'review_revision': review_state['revision'],
            'facts_revision': facts['revision'], 'previous_findings': sanitize_fact_history(previous, facts),
            'round_number': loaded['report'].get('metadata', {}).get('rereview', {}).get('round_number', 1) + 1,
        }
        binding = {'origin': origin, 'current_contract_fingerprint': parse_docx(document['data']).fingerprint,
                   'original_source_sha256': document['source_sha256'],
                   'edits': document['edits'], 'comments': document['comments'],
                   'context': ctx, 'checklist_snapshot': checklist, 'preparation': preparation}
        token = digest(binding)
        origin['input_revision'] = token
        return {'input_revision': token, 'data': document['data'], 'context': ctx,
                'checklist_snapshot': checklist, 'preparation': preparation, 'origin': origin}

    def preflight(self, loaded: dict, review_state: dict) -> dict:
        with self.lock, self.delivery.lock, self.facts.lock:
            try:
                snap = self._snapshot(loaded, review_state)
            except (DeliveryUnavailable, DeliveryStateCorrupt) as exc:
                return {'available': False, 'reason': str(exc)}
            return {'available': True, 'input_revision': snap['input_revision'],
                    'document_name': loaded['document_name'],
                    'round_number': snap['origin']['round_number'],
                    'facts_count': len(snap['preparation']['confirmed_facts']['facts']),
                    'checks_count': len(snap['preparation']['checks']),
                    'delivery_revision': snap['origin']['delivery_revision'],
                    'parent_report_id': loaded['id']}

    def start(self, loaded: dict, review_state: dict, payload: dict) -> tuple[dict, bool]:
        rid = payload.get('request_id')
        if not isinstance(rid, str) or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', rid):
            raise ValueError('复审 request_id 必须是标准UUID。')
        if self.deleted_request_provider and self.deleted_request_provider(loaded['id'], rid.lower()):
            raise JobConflict('先前这次复审任务已永久删除，请重新发起复审。')
        if not isinstance(payload.get('input_revision'), str):
            raise ValueError('请先读取本次复审准备信息。')
        self.jobs._runtime_config(payload)  # Validate explicit execution before creating a child.
        if type(payload.get('web_search', False)) is not bool:
            raise ValueError('web_search 必须是布尔值。')
        if payload.get('web_search'):
            if payload.get('engine_key') != 'compatible_api':
                raise ValueError('联网查证只支持兼容API的DeepSeek接口。')
            validate_deepseek_address(payload.get('config', {}).get('base_url'))
        runtime = {k: payload[k] for k in ('engine_key', 'config', 'consent', 'web_search') if k in payload}
        with self.lock, self.delivery.lock, self.facts.lock:
            path = self.root / loaded['id'] / (rid.lower() + '.json')
            if path.exists():
                record = _read_json(path, '复审提交记录')
                if (record.get('parent_report_id') != loaded['id']
                        or record.get('input_revision') != payload['input_revision']):
                    raise JobConflict('这次提交标识已用于不同的复审输入，请重新确认。')
                # Even after failure/restart, a repeated submission only retrieves its prior task.
                job = self.jobs.get(record['job_id'])
                job.setdefault('review_kind', 'rereview')
                job.setdefault('parent_report_id', loaded['id'])
                return job, False
            snap = self._snapshot(loaded, review_state)
            if payload['input_revision'] != snap['input_revision']:
                raise JobConflict('合同修改、补充信息或意见处理已有变化，请重新确认复审。')
            name = loaded['document_name']
            source = {'document_name': name,
                      'docx_base64': base64.b64encode(snap['data']).decode('ascii'),
                      'context': snap['context'], 'checklist_snapshot': snap['checklist_snapshot']}
            job = self.jobs.create_rereview(source, snap['preparation'], snap['origin'])
            record = {'parent_report_id': loaded['id'], 'input_revision': snap['input_revision'],
                      'job_id': job['job_id'], 'request_id': rid}
            _atomic_json(path, record)
            return self.jobs.start(job['job_id'], runtime)
