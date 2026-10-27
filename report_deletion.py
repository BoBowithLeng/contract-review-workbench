"""Permanent removal of one workbench round and its managed local artifacts.

The ID-only journal is not an archive: it contains no contract or review data.
It prevents hardcoded legacy reports (and a partially removed task) reappearing
after restart, and permits retrying an interrupted filesystem deletion.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import stat
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from reader_workbench import _document_id
from review_jobs import JOB_ID_RE, _ACTIVE_RUNS, _ACTIVE_RUNS_LOCK, _atomic_json
from workbench_groups import _atomic_json as _write_groups, _validate_state

_REPORT = re.compile(r"(?:detailed|minimal|table|job-[0-9a-f]{32})\Z")
_ITEM = re.compile(r"(?:doc-[0-9a-f]{24}|job-[0-9a-f]{32})\Z")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_LOCKS: dict[tuple[str, str, str], Any] = {}
_LOCKS_LOCK = threading.Lock()


def operation_lock(project: Path, states: Path, jobs: Path):
    key = tuple(str(p.resolve()).casefold() for p in (project, states, jobs))
    with _LOCKS_LOCK:
        return _LOCKS.setdefault(key, threading.RLock())


class DeletionError(RuntimeError):
    pass


class DeletionConflict(DeletionError):
    pass


class DeletionIncomplete(DeletionError):
    def __init__(self, item_id: str):
        super().__init__("永久删除尚未完成，部分本机文件可能已移除；请再次删除同一项以继续清理。")
        self.item_id = item_id


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("object")
        return value
    except (OSError, ValueError, UnicodeError) as exc:
        raise DeletionError("删除所需的本地记录无法读取，未继续删除。") from exc


class ReportDeletion:
    def __init__(self, server, legacy_specs: dict):
        self.server = server
        self.legacy_specs = legacy_specs
        self.path = server.state_dir / "report-deletions.json"

    def read(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 1, "entries": []}
        value = _json(self.path)
        try:
            if (set(value) != {"schema_version", "entries"} or value["schema_version"] != 1
                    or not isinstance(value["entries"], list)):
                raise ValueError("schema")
            ids = set()
            for row in value["entries"]:
                if (set(row) != {"report_id", "item_id", "group_keys", "draft_candidates", "rereview_requests", "status"}
                        or not _REPORT.fullmatch(row["report_id"])
                        or not _ITEM.fullmatch(row["item_id"])
                        or row["status"] not in {"pending", "deleted"}
                        or row["report_id"] in ids or not isinstance(row["group_keys"], list)
                        or any(not isinstance(k, str) or not _ITEM.fullmatch(k) for k in row["group_keys"])):
                    raise ValueError("entry")
                if (not isinstance(row["draft_candidates"], list)
                        or any(not isinstance(k, str) or not re.fullmatch(r"[0-9a-f]{64}", k) for k in row["draft_candidates"])):
                    raise ValueError("draft candidates")
                if not isinstance(row["rereview_requests"], list):
                    raise ValueError("requests")
                for request in row["rereview_requests"]:
                    if (set(request) != {"parent_report_id", "request_id", "job_id"}
                            or not _REPORT.fullmatch(request["parent_report_id"])
                            or not _UUID.fullmatch(request["request_id"])
                            or request["job_id"] != row["report_id"]):
                        raise ValueError("request")
                ids.add(row["report_id"])
        except (ValueError, TypeError, KeyError) as exc:
            raise DeletionError("永久删除目录损坏，未重置或继续删除。") from exc
        return value

    def deleted_ids(self) -> set[str]:
        return {row["report_id"] for row in self.read()["entries"]}

    def deleted_rereview_request(self, parent_report_id: str, request_id: str) -> bool:
        return any(request["parent_report_id"] == parent_report_id and request["request_id"] == request_id
                   for row in self.read()["entries"] for request in row["rereview_requests"])

    def visible(self, catalog: dict) -> dict:
        journal = self.read()
        hidden = {row["report_id"] for row in journal["entries"]}
        result = {
            "documents": [d for d in catalog.get("documents", [])
                          if not any(r["id"] in hidden for r in d["reviews"])],
            "pending_jobs": [j for j in catalog.get("pending_jobs", []) if j["id"] not in hidden],
        }
        pending = [{"item_id": row["item_id"], "report_id": row["report_id"]}
                   for row in journal["entries"] if row["status"] == "pending"]
        if pending:
            result["pending_deletions"] = pending
        return result

    @staticmethod
    def _checked(path: Path, root: Path, *, recursive: bool = False) -> Path:
        """Reject links/junctions and resolve every deletion target before writes."""
        absolute, base = path.absolute(), root.absolute()
        try:
            relative = absolute.relative_to(base)
            if not relative.parts or absolute.resolve() == base.resolve():
                raise ValueError("root")
            absolute.resolve().relative_to(base.resolve())
        except ValueError as exc:
            raise DeletionError("删除目标超出已核实的工作台目录。") from exc
        current = base
        for part in ("", *relative.parts):
            if part:
                current /= part
            try:
                attributes = getattr(current.lstat(), "st_file_attributes", 0)
            except FileNotFoundError:
                attributes = 0
            if (current.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    or (hasattr(current, "is_junction") and current.is_junction())):
                raise DeletionError("删除目标含链接或目录联接，未继续删除。")
        if recursive and absolute.is_dir():
            for child in absolute.rglob("*"):
                ReportDeletion._checked(child, base)
        return absolute

    def _artifacts(self, rid: str, known_candidates: list[str]) -> list[tuple[Path, Path, bool]]:
        s = self.server
        result: list[tuple[Path, Path, bool]] = []
        if JOB_ID_RE.fullmatch(rid):
            result.append((s.jobs.root / rid, s.jobs.root, True))
        else:
            directory = s.project_root / "runtime" / self.legacy_specs[rid]["directory"]
            # These are report snapshots, not a configured original DOCX or a
            # fixture input. Unknown files in a legacy run are kept intact.
            for name in ("request.json", "response.json", "result.json", "validated-result.json"):
                result.append((directory / name, s.project_root / "runtime", False))
                result.extend((p, s.project_root / "runtime", False)
                              for p in directory.glob("." + name + ".*.tmp") if p.is_file())
        for root in (s.state_dir, s.delivery.root, s.discussions.root, s.revisions.root, s.facts.root):
            if root.is_dir():
                result.extend((p, root, False) for p in root.glob(rid + "-*") if p.is_file())
                result.extend((p, root, False) for p in root.glob("." + rid + "-*.tmp") if p.is_file())
        gate = getattr(s, "information_gate", None)
        if gate and gate.root.is_dir():
            result.extend((p, gate.root, True) for p in gate.root.glob(rid + "-*") if p.is_dir())
        if s.drafts.root.is_dir():
            result.extend((s.drafts.root / cid, s.drafts.root, True) for cid in known_candidates)
            for directory in s.drafts.root.iterdir():
                if directory.is_dir() and re.fullmatch(r"[0-9a-f]{64}", directory.name):
                    if directory.name in known_candidates:
                        continue  # A partial rmtree may have removed its binding JSON.
                    operation = directory / "operation.json"
                    request = directory / "request.json"
                    record = _json(operation) if operation.exists() else (
                        _json(request).get("identity", {}) if request.exists() else {})
                    if record.get("report_id") == rid:
                        result.append((directory, s.drafts.root, True))
            result.extend((p, s.drafts.root, False) for p in s.drafts.root.glob("latest-" + rid + "-*.json"))
            result.extend((p, s.drafts.root, False) for p in s.drafts.root.glob(".latest-" + rid + "-*.tmp"))
        result.append((s.rereviews.root / rid, s.rereviews.root, True))
        if s.rereviews.root.is_dir():
            for directory in s.rereviews.root.iterdir():
                if directory.is_dir() and _REPORT.fullmatch(directory.name) and directory.name != rid:
                    for path in directory.glob("*.json"):
                        if _json(path).get("job_id") == rid:
                            result.append((path, s.rereviews.root, False))
        return result

    def _assert_idle(self, rid: str, artifacts: list[tuple[Path, Path, bool]]) -> None:
        s = self.server
        gate = getattr(s, "information_gate", None)
        if gate:
            with gate.lock:
                if any(str(p) in gate._active or ((p / "publication.json").exists() and _json(p / "publication.json").get("status") == "running")
                       for p, root, recursive in artifacts if root == gate.root):
                    raise DeletionConflict("这份合同仍在根据补充信息更新意见，请等结束后删除。")
        if JOB_ID_RE.fullmatch(rid):
            key = str((s.jobs.root / rid).resolve())
            with _ACTIVE_RUNS_LOCK:
                active = any(key + suffix in _ACTIVE_RUNS for suffix in (":preparation", ":review"))
            status_path = s.jobs.root / rid / "status.json"
            status = _json(status_path) if status_path.exists() else {}
            if active or status.get("status") in {"running", "preparing"}:
                raise DeletionConflict("这份合同仍在检查准备情况或审核，请等结束后删除。")
        for path, root, recursive in artifacts:
            if root == s.drafts.root and recursive:
                op = _json(path / "operation.json") if (path / "operation.json").exists() else {}
                if path.name in s.drafts._active or op.get("status") == "running":
                    raise DeletionConflict("这份合同仍在整理候选改稿，请等结束后删除。")
            elif root in (s.discussions.root, s.revisions.root) and path.exists() and path.suffix == ".json":
                record = _json(path)
                entries = record.get("turns", []) if root == s.discussions.root else record.get("candidates", [])
                active = s.discussions._active if root == s.discussions.root else s.revisions._active
                if any(row.get("status") == "running" or row.get("id") in active for row in entries):
                    raise DeletionConflict("这份合同仍在讨论或生成新版意见，请等结束后删除。")

    def delete(self, payload: dict) -> dict:
        if set(payload) != {"item_id", "confirm"} or payload.get("confirm") is not True:
            raise ValueError("请明确确认永久删除，并提供一个工作台 item_id。")
        item_id = payload.get("item_id")
        if not isinstance(item_id, str) or not _ITEM.fullmatch(item_id):
            raise ValueError("工作台 item_id 无效。")
        s = self.server
        with s.operation_lock, ExitStack() as stack:
            for lock in (s.jobs.lock, s.rereviews.lock, s.state_lock, s.workbench_groups.lock,
                         s.discussions.lock, s.delivery.lock, s.facts.lock, s.revisions.lock, s.drafts.lock, s.archive.lock):
                stack.enter_context(lock)
            journal = self.read()
            prior = next((r for r in journal["entries"] if r["item_id"] == item_id), None)
            catalog = s.workbench_catalog()
            document = next((d for d in catalog["documents"] if d["id"] == item_id), None)
            job = next((j for j in catalog["pending_jobs"] if j["id"] == item_id), None)
            if prior is not None:
                rid = prior["report_id"]
            elif document is not None:
                rid = document["reviews"][0]["id"]
            elif job is not None:
                rid = job["id"]
            else:
                raise KeyError(item_id)
            artifacts = self._artifacts(rid, prior["draft_candidates"] if prior else [])
            for path, root, recursive in artifacts:
                self._checked(path, root, recursive=recursive)
            self._assert_idle(rid, artifacts)
            groups = copy.deepcopy(s.workbench_groups._load())
            group_keys = {item_id, _document_id("report:" + rid)}
            if JOB_ID_RE.fullmatch(rid):
                group_keys.add(rid)
            if prior:
                group_keys.update(prior["group_keys"])
            old_key = document.get("legacy_document_id") if document else None
            full_catalog = s.workbench_catalog(include_archived=True)
            if old_key and not any(d["id"] != item_id and d.get("legacy_document_id") == old_key for d in full_catalog["documents"]):
                group_keys.add(old_key)
            changed = False
            for snapshot in [groups, *groups["history"]]:
                for key in group_keys:
                    changed |= snapshot["assignments"].pop(key, None) is not None
            if changed:
                groups["revision"] += 1
                groups = _validate_state(groups)
            archive = s.archive.read()
            archive_rows = [r for r in archive["entries"] if r["id"] != rid]
            if prior is None:
                requests = []
                for path, root, recursive in artifacts:
                    if root == s.rereviews.root and not recursive and path.is_file():
                        record = _json(path)
                        request_id = record.get("request_id", path.stem)
                        if isinstance(request_id, str) and _UUID.fullmatch(request_id.lower()):
                            requests.append({"parent_report_id": path.parent.name,
                                             "request_id": request_id.lower(), "job_id": rid})
                prior = {"report_id": rid, "item_id": item_id, "group_keys": sorted(group_keys),
                         "draft_candidates": [path.name for path, root, recursive in artifacts if root == s.drafts.root and recursive],
                         "rereview_requests": requests, "status": "pending"}
                journal["entries"].append(prior)
            else:
                prior["status"] = "pending"
            # First durable mutation: hide/block this ID even if an OS deletion
            # fails midway. No original content is copied into the journal.
            _atomic_json(self.path, journal)
            try:
                if changed:
                    _write_groups(s.workbench_groups.path, groups)
                if archive_rows != archive["entries"]:
                    s.archive.update([{"id": rid, "document_name": ""}], archived=False,
                                     expected_revision=archive["revision"])
                for path, root, recursive in artifacts:
                    path = self._checked(path, root, recursive=recursive)
                    if path.exists():
                        if recursive:
                            shutil.rmtree(path)
                        else:
                            path.unlink()
                s.delivery.source_paths.pop(rid, None)
                prior["status"] = "deleted"
                _atomic_json(self.path, journal)
            except (OSError, DeletionError) as exc:
                raise DeletionIncomplete(item_id) from exc
            return {"deleted": True, "item_id": item_id, "deleted_report_ids": [rid],
                    "groups": s.workbench_groups.get()}
