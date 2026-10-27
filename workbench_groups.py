"""Persistent user-defined workbench groups for contract metadata only."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from review_jobs import JOB_ID_RE


SCHEMA_VERSION = 1
MAX_NAME_CHARS = 40
MAX_GROUPS = 100
MAX_ASSIGN_ITEMS = 100
MAX_ASSIGNMENTS = 5_000
MAX_HISTORY = 20
MAX_STATE_BYTES = 1024 * 1024
GROUP_ID_RE = re.compile(r"g-[0-9a-f]{32}")
ITEM_ID_RE = re.compile(r"(?:doc-[0-9a-f]{24}|job-[0-9a-f]{32})")


class WorkbenchGroupError(RuntimeError):
    pass


class WorkbenchGroupConflict(WorkbenchGroupError):
    def __init__(self, message: str, current_revision: int):
        super().__init__(message)
        self.current_revision = current_revision


class WorkbenchGroupCorrupt(WorkbenchGroupError):
    pass


def _empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "revision": 0, "groups": [], "assignments": {}, "history": []}


def _snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "revision": state["revision"],
        "groups": [dict(item) for item in state["groups"]],
        "assignments": dict(state["assignments"]),
    }


def _public(state: Mapping[str, Any], *, created_group_id: str | None = None) -> dict[str, Any]:
    value = _snapshot(state)
    if created_group_id is not None:
        value["created_group_id"] = created_group_id
    return value


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def _clean_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("分组名称必须是文本。")
    name = value.strip()
    if not name or len(name) > MAX_NAME_CHARS:
        raise ValueError(f"分组名称必须是1–{MAX_NAME_CHARS}个字符。")
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise ValueError("分组名称不能含控制字符。")
    if _name_key(name) in {"全部合同", "未分组"}:
        raise ValueError("这个名称用于系统入口，请换一个分组名称。")
    return name


def _validate_groups(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > MAX_GROUPS:
        raise ValueError("groups")
    groups: list[dict[str, str]] = []
    ids: set[str] = set()
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"id", "name"}:
            raise ValueError("group")
        group_id = item.get("id")
        name = _clean_name(item.get("name"))
        if not isinstance(group_id, str) or not GROUP_ID_RE.fullmatch(group_id):
            raise ValueError("group id")
        key = _name_key(name)
        if group_id in ids or key in names:
            raise ValueError("duplicate group")
        ids.add(group_id)
        names.add(key)
        groups.append({"id": group_id, "name": name})
    return groups


def _validate_assignments(raw: Any, group_ids: set[str]) -> dict[str, str]:
    if not isinstance(raw, dict) or len(raw) > MAX_ASSIGNMENTS:
        raise ValueError("assignments")
    assignments: dict[str, str] = {}
    for item_id, group_id in raw.items():
        if (
            not isinstance(item_id, str)
            or not ITEM_ID_RE.fullmatch(item_id)
            or not isinstance(group_id, str)
            or group_id not in group_ids
        ):
            raise ValueError("assignment")
        assignments[item_id] = group_id
    return assignments


def _validate_snapshot(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"revision", "groups", "assignments"}:
        raise ValueError("snapshot")
    revision = raw.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("revision")
    groups = _validate_groups(raw.get("groups"))
    assignments = _validate_assignments(raw.get("assignments"), {item["id"] for item in groups})
    return {"revision": revision, "groups": groups, "assignments": assignments}


def _validate_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "revision", "groups", "assignments", "history"}:
        raise ValueError("state")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema")
    current = _validate_snapshot({key: raw.get(key) for key in ("revision", "groups", "assignments")})
    history_raw = raw.get("history")
    if not isinstance(history_raw, list) or len(history_raw) > MAX_HISTORY:
        raise ValueError("history")
    history = [_validate_snapshot(item) for item in history_raw]
    if any(item["revision"] >= current["revision"] for item in history):
        raise ValueError("history revision")
    return {"schema_version": SCHEMA_VERSION, **current, "history": history}


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    if len(payload) > MAX_STATE_BYTES:
        raise ValueError("分组记录超过当前容量，本次未保存。")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class WorkbenchGroupManager:
    def __init__(self, path: Path, catalog_provider: Callable[[], Mapping[str, Any]]):
        self.path = Path(path)
        self.catalog_provider = catalog_provider
        self.lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_state()
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError("size")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return _validate_state(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise WorkbenchGroupCorrupt("合同分组状态损坏，未自动重置。") from exc

    def get(self) -> dict[str, Any]:
        with self.lock:
            return _public(self._load())

    def migrate_legacy_assignments(self, expected_revision: int) -> dict[str, Any]:
        """Once, fan out old source-group assignments to reports present now.

        The old key is removed in the same atomic state update, so a future
        review of the same source cannot inherit that earlier委托's group.
        This is deliberately explicit; GET and ordinary updates never migrate.
        """
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision 必须是非负整数。")
        with self.lock:
            current = self._load()
            if current["revision"] != expected_revision:
                raise WorkbenchGroupConflict("合同分组已被其他操作更新，请刷新后重试。", current["revision"])
            catalog = self.catalog_provider()
            legacy_to_reports: dict[str, dict[str, list[str]]] = {}
            for document in catalog.get("documents", []):
                if not isinstance(document, dict):
                    continue
                new_id, old_id = document.get("id"), document.get("legacy_document_id")
                if (isinstance(new_id, str) and ITEM_ID_RE.fullmatch(new_id)
                        and isinstance(old_id, str) and ITEM_ID_RE.fullmatch(old_id) and old_id != new_id):
                    review_ids = [review.get("id") for review in document.get("reviews", [])
                                  if isinstance(review, dict) and isinstance(review.get("id"), str)]
                    legacy_to_reports.setdefault(old_id, {})[new_id] = review_ids
            assignments = dict(current["assignments"])
            removed = backfilled = pending_preferred = existing_preserved = 0
            for old_id, targets in legacy_to_reports.items():
                group_id = assignments.get(old_id)
                if group_id is None:
                    continue
                for target, review_ids in targets.items():
                    if target in assignments:
                        existing_preserved += 1
                        continue
                    own_pending = next((assignments[rid] for rid in review_ids
                                        if JOB_ID_RE.fullmatch(rid) and rid in assignments), None)
                    assignments[target] = own_pending or group_id
                    backfilled += 1
                    if own_pending is not None:
                        pending_preferred += 1
                assignments.pop(old_id)
                removed += 1
            present_ids = {doc_id for targets in legacy_to_reports.values() for doc_id in targets}
            unmapped = sum(1 for item_id in assignments
                           if item_id.startswith("doc-") and item_id not in present_ids)
            summary = {"changed": bool(removed), "legacy_keys_removed": removed,
                       "documents_backfilled": backfilled, "pending_group_preserved": pending_preferred,
                       "existing_preserved": existing_preserved,
                       "unmapped_document_keys_preserved": unmapped}
            if not removed:
                return {**_public(current), "migration": summary}
            new_state = _validate_state({
                "schema_version": SCHEMA_VERSION, "revision": current["revision"] + 1,
                "groups": [dict(item) for item in current["groups"]], "assignments": assignments,
                "history": [*current["history"], _snapshot(current)][-MAX_HISTORY:],
            })
            _atomic_json(self.path, new_state)
            return {**_public(new_state), "migration": summary}

    def update(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("请求 JSON 必须是对象。")
        action = payload.get("action")
        expected = payload.get("expected_revision")
        if action not in {"create", "rename", "delete", "assign"}:
            raise ValueError("action 必须是 create、rename、delete 或 assign。")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ValueError("expected_revision 必须是非负整数。")
        allowed = {
            "create": {"expected_revision", "action", "name"},
            "rename": {"expected_revision", "action", "group_id", "name"},
            "delete": {"expected_revision", "action", "group_id"},
            "assign": {"expected_revision", "action", "group_id", "item_ids"},
        }[action]
        if set(payload) != allowed:
            raise ValueError("分组请求字段与 action 不匹配。")

        with self.lock:
            current = self._load()
            if current["revision"] != expected:
                raise WorkbenchGroupConflict("合同分组已被其他操作更新，请刷新后重试。", current["revision"])
            groups = [dict(item) for item in current["groups"]]
            assignments = dict(current["assignments"])
            created_group_id = None
            changed = False

            if action == "create":
                if len(groups) >= MAX_GROUPS:
                    raise ValueError(f"分组数量不能超过 {MAX_GROUPS} 个。")
                name = _clean_name(payload.get("name"))
                self._ensure_unique_name(groups, name)
                created_group_id = "g-" + uuid.uuid4().hex
                groups.append({"id": created_group_id, "name": name})
                changed = True
            else:
                group_id = payload.get("group_id")
                if action == "assign" and group_id is None:
                    pass
                elif not isinstance(group_id, str) or not GROUP_ID_RE.fullmatch(group_id):
                    raise ValueError("group_id 无效。")
                if group_id is not None and group_id not in {item["id"] for item in groups}:
                    raise ValueError("group_id 不存在。")

                if action == "rename":
                    name = _clean_name(payload.get("name"))
                    self._ensure_unique_name(groups, name, exclude=group_id)
                    target = next(item for item in groups if item["id"] == group_id)
                    if target["name"] != name:
                        target["name"] = name
                        changed = True
                elif action == "delete":
                    groups = [item for item in groups if item["id"] != group_id]
                    revised = {item_id: assigned for item_id, assigned in assignments.items() if assigned != group_id}
                    changed = len(groups) != len(current["groups"]) or revised != assignments
                    assignments = revised
                elif action == "assign":
                    item_ids = payload.get("item_ids")
                    if (
                        not isinstance(item_ids, list)
                        or not 1 <= len(item_ids) <= MAX_ASSIGN_ITEMS
                        or any(not isinstance(item, str) for item in item_ids)
                        or len(item_ids) != len(set(item_ids))
                    ):
                        raise ValueError(f"item_ids 必须是1–{MAX_ASSIGN_ITEMS}个不重复字符串。")
                    catalog = self.catalog_provider()
                    documents = {
                        item.get("id"): item
                        for item in catalog.get("documents", [])
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    }
                    pending_ids = {
                        item.get("id") for item in catalog.get("pending_jobs", [])
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    }
                    valid = set(documents) | pending_ids
                    unknown = [item for item in item_ids if item not in valid]
                    if unknown:
                        raise ValueError("item_ids 含有不属于当前工作台的项目。")
                    for item_id in item_ids:
                        document = documents.get(item_id)
                        if document is not None:
                            for review in document.get("reviews", []):
                                review_id = review.get("id") if isinstance(review, dict) else None
                                if isinstance(review_id, str) and JOB_ID_RE.fullmatch(review_id):
                                    if review_id in assignments:
                                        assignments.pop(review_id)
                                        changed = True
                        if group_id is None:
                            if item_id in assignments:
                                assignments.pop(item_id)
                                changed = True
                        elif assignments.get(item_id) != group_id:
                            assignments[item_id] = group_id
                            changed = True

            if not changed:
                return _public(current)
            new_state = {
                "schema_version": SCHEMA_VERSION,
                "revision": current["revision"] + 1,
                "groups": groups,
                "assignments": assignments,
                "history": [*current["history"], _snapshot(current)][-MAX_HISTORY:],
            }
            # Validate our own output before the single atomic replacement.
            new_state = _validate_state(new_state)
            _atomic_json(self.path, new_state)
            return _public(new_state, created_group_id=created_group_id)

    @staticmethod
    def _ensure_unique_name(groups: list[dict[str, str]], name: str, *, exclude: str | None = None) -> None:
        key = _name_key(name)
        if any(item["id"] != exclude and _name_key(item["name"]) == key for item in groups):
            raise ValueError("已有同名分组。")


__all__ = [
    "WorkbenchGroupManager", "WorkbenchGroupError", "WorkbenchGroupConflict", "WorkbenchGroupCorrupt",
]
