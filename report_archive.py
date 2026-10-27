"""Reversible workbench cleanup: archive listing entries, never contract files."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import tempfile
import threading
from pathlib import Path

_ID = re.compile(r'(?:detailed|minimal|table|job-[0-9a-f]{32})\Z')


class ArchiveError(RuntimeError):
    pass


class ReportArchive:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.exists():
                return {'schema_version': 1, 'revision': 0, 'entries': []}
            try:
                if self.path.stat().st_size > 1024 * 1024:
                    raise ValueError('size')
                state = json.loads(self.path.read_text(encoding='utf-8'))
                if (set(state) != {'schema_version', 'revision', 'entries'} or state['schema_version'] != 1
                        or type(state['revision']) is not int or state['revision'] < 0
                        or not isinstance(state['entries'], list)):
                    raise ValueError('state')
                ids = set()
                for row in state['entries']:
                    if (not isinstance(row, dict) or set(row) != {'id', 'document_name', 'archived_at'}
                            or not isinstance(row['id'], str) or not _ID.fullmatch(row['id'])
                            or row['id'] in ids or not isinstance(row['document_name'], str)
                            or not isinstance(row['archived_at'], str)):
                        raise ValueError('entry')
                    ids.add(row['id'])
                return state
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise ArchiveError('归档目录无法读取，未修改合同或重置归档记录。') from exc

    def update(self, entries: list[dict], *, archived: bool, expected_revision: int):
        with self.lock:
            state = self.read()
            if type(expected_revision) is not int or state['revision'] != expected_revision:
                raise ArchiveError('归档目录已更新，请重新读取后操作。')
            known = {row['id']: copy.deepcopy(row) for row in state['entries']}
            for row in entries:
                if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                        or not _ID.fullmatch(row['id']) or not isinstance(row.get('document_name'), str)):
                    raise ValueError('必须提供核实过的报告编号与名称。')
                if archived:
                    known.setdefault(row['id'], {'id': row['id'], 'document_name': row['document_name'],
                                                'archived_at': dt.datetime.now(dt.timezone.utc).isoformat()})
                else:
                    known.pop(row['id'], None)
            rows = list(known.values())
            if rows == state['entries']:
                return state
            updated = {'schema_version': 1, 'revision': state['revision'] + 1, 'entries': rows}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.report-archive-', suffix='.tmp', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(updated, stream, ensure_ascii=False, indent=2)
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            return updated

    def visible(self, catalog: dict):
        hidden = {row['id'] for row in self.read()['entries']}
        result = copy.deepcopy(catalog)
        documents = []
        for doc in result.get('documents', []):
            doc['reviews'] = [r for r in doc['reviews'] if r['id'] not in hidden]
            if doc['reviews']:
                doc['review_count'] = len(doc['reviews'])
                documents.append(doc)
        result['documents'] = documents
        result['pending_jobs'] = [job for job in result.get('pending_jobs', []) if job['id'] not in hidden]
        return result


def main():
    parser = argparse.ArgumentParser(description='查看或恢复工作台归档，不删除合同文件。')
    parser.add_argument('action', choices=['list', 'restore'])
    parser.add_argument('ids', nargs='*', help='restore可指定编号；--all恢复全部。')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--state-dir', type=Path, default=Path(__file__).resolve().parent/'runtime'/'reader-state')
    args = parser.parse_args()
    manager = ReportArchive(args.state_dir/'report-archive.json')
    state = manager.read()
    if args.action == 'restore':
        ids = {row['id'] for row in state['entries']} if args.all else set(args.ids)
        if not ids or not ids <= {row['id'] for row in state['entries']}:
            parser.error('请指定目录中已有的归档编号，或 --all。')
        state = manager.update([row for row in state['entries'] if row['id'] in ids],
                               archived=False, expected_revision=state['revision'])
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
