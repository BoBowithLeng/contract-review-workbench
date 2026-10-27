"""Local checklist catalog and validated, independent review snapshots."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

MAX_ITEMS = 200
MAX_FIELD_CHARS = 8000
MAX_HISTORY = 20
MAX_STATE_BYTES = 20 * 1024 * 1024
ITEM_FIELDS = {"id", "title", "requirement", "when", "facts", "guidance", "notes", "enabled"}
LEGACY_LIST_FIELDS = {"id", "name", "contract_type", "stance", "version", "builtin", "items"}
LIST_FIELDS = LEGACY_LIST_FIELDS | {"scope"}
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_LOCKS: dict[str, threading.RLock] = {}
_LOCK_GUARD = threading.Lock()


class ChecklistError(ValueError):
    pass


class ChecklistConflict(ChecklistError):
    def __init__(self, current_revision: int):
        super().__init__("清单库已更新，请刷新后重试。")
        self.current_revision = current_revision


class ChecklistCorrupt(ChecklistError):
    pass


def _text(value: Any, field: str, required: bool = False) -> str:
    field = {"title": "检查项名称", "requirement": "检查内容", "when": "适用条件",
             "facts": "需要的信息", "guidance": "处理建议", "notes": "备注"}.get(field, field)
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
        raise ChecklistError(f"{field} 必须是最多 {MAX_FIELD_CHARS} 字的文本。")
    if required and not value.strip():
        raise ChecklistError(f"{field} 不能为空。")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ChecklistError(f"{field}中有无法保存的特殊字符，请删除后重试。")
    return value.strip()


def _id(value: Any, field: str, allow_empty: bool = False) -> str:
    if allow_empty and value in (None, ""):
        return ""
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ChecklistError(f"{field.replace('id', '编号')}有误。")
    return value


def normalize_checklist_snapshot(raw: Any) -> dict | None:
    """Whitelist and deep-copy a per-review selection; never consult the catalog."""
    if raw is None or raw == "" or raw == {}:
        return None
    if not isinstance(raw, dict) or set(raw) - LIST_FIELDS:
        raise ChecklistError("清单中有不支持的字段，请检查清单内容。")
    items = raw.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ChecklistError(f"清单必须含 1–{MAX_ITEMS} 项。")
    result = {"id": _id(raw.get("id", ""), "清单id", True),
              "name": _text(raw.get("name"), "清单名称", True),
              "contract_type": _text(raw.get("contract_type", ""), "合同类型"),
              "stance": _text(raw.get("stance", ""), "审查立场")}
    # Historical request digests must remain identical. Only preserve scope when
    # it was explicitly present; do not silently add an empty field to old data.
    if "scope" in raw:
        result["scope"] = _text(raw["scope"], "适用范围")
    version = raw.get("version", 1)
    if not ((type(version) is int and version >= 1) or
            (isinstance(version, str) and 0 < len(version) <= 40)):
        raise ChecklistError("清单版本无效。")
    builtin = raw.get("builtin", False)
    if type(builtin) is not bool:
        raise ChecklistError("清单类型设置有误。")
    result.update(version=version, builtin=builtin, items=[])
    ids = set()
    for item in items:
        if not isinstance(item, dict) or set(item) - ITEM_FIELDS:
            raise ChecklistError("检查项中有不支持的字段，请检查内容。")
        item_id = _id(item.get("id", ""), "检查项id", True) or "i-" + uuid.uuid4().hex
        if item_id in ids:
            raise ChecklistError("检查项编号重复，请检查清单。")
        ids.add(item_id)
        clean = {"id": item_id}
        for field in ("title", "requirement", "when", "facts", "guidance", "notes"):
            clean[field] = _text(item.get(field, ""), field, field in {"title", "requirement"})
        enabled = item.get("enabled", True)
        if type(enabled) is not bool:
            raise ChecklistError("检查项的启用设置有误。")
        clean["enabled"] = enabled
        result["items"].append(clean)
    return copy.deepcopy(result)


def legacy_content(item: dict) -> str:
    """Carry old item fields into content on explicit edit/import only.

    This is a deterministic, lossless field merge, not a semantic rewrite or
    deduplication. Reading/normalizing a frozen snapshot must not call it.
    """
    parts = [_text(item.get("requirement", ""), "检查内容", True)]
    for field, heading in (("facts", "原可能需要的信息"), ("guidance", "原处理建议")):
        value = _text(item.get(field, ""), field)
        if value:
            parts.append(f"### {heading}\n\n{value}")
    return _text("\n\n".join(parts), "合并后的检查内容", True)


def convert_checklist_for_edit(raw: Any) -> dict | None:
    """Return an editable copy; leave the catalog and old snapshots untouched."""
    clean = normalize_checklist_snapshot(raw)
    if clean is not None:
        for item in clean["items"]:
            item["requirement"] = legacy_content(item)
            item["facts"] = item["guidance"] = ""
    return clean


class ChecklistStore:
    def __init__(self, path: Path, builtin_path: Path | None = None):
        self.path = Path(path).resolve()
        self.builtin_path = Path(builtin_path or Path(__file__).with_name("builtin_checklists.json")).resolve()
        with _LOCK_GUARD:
            self.lock = _LOCKS.setdefault(str(self.path), threading.RLock())

    def _builtins(self) -> list[dict]:
        try:
            raw = json.loads(self.builtin_path.read_text(encoding="utf-8-sig"))
            values = raw["checklists"] if isinstance(raw, dict) else raw
            if not isinstance(values, list):
                raise ChecklistError("内置清单格式无效。")
            clean = [normalize_checklist_snapshot(item) for item in values]
            if any(not item or not item["id"] or not item["builtin"] for item in clean):
                raise ChecklistError("内置清单标识无效。")
            if len({item["id"] for item in clean}) != len(clean):
                raise ChecklistError("内置清单编号重复，请检查清单文件。")
            return clean
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise ChecklistCorrupt("内置清单文件损坏，暂时无法读取。") from exc

    @staticmethod
    def _snapshot(state: dict) -> dict:
        return copy.deepcopy({"revision": state["revision"], "checklists": state["checklists"]})

    def _validate_snapshot(self, raw: Any) -> dict:
        if (not isinstance(raw, dict) or set(raw) != {"revision", "checklists"}
                or type(raw["revision"]) is not int or raw["revision"] < 0
                or not isinstance(raw["checklists"], list) or len(raw["checklists"]) > 100):
            raise ChecklistError("清单状态无效。")
        lists = [normalize_checklist_snapshot(item) for item in raw["checklists"]]
        if any(not LEGACY_LIST_FIELDS <= set(item) or set(item) - LIST_FIELDS
               or any(set(row) != ITEM_FIELDS or not row.get("id") for row in item["items"])
               for item in raw["checklists"]):
            raise ChecklistError("保存的清单字段缺失。")
        if any(not item or not item["id"] or item["builtin"] or type(item["version"]) is not int for item in lists):
            raise ChecklistError("个人清单状态无效。")
        if len({item["id"] for item in lists}) != len(lists):
            raise ChecklistError("清单编号重复，请检查清单。")
        return {"revision": raw["revision"], "checklists": lists}

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 1, "revision": 0, "checklists": [], "history": []}
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise ChecklistError("size")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"schema_version", "revision", "checklists", "history"} or raw["schema_version"] != 1:
                raise ChecklistError("schema")
            current = self._validate_snapshot({k: raw[k] for k in ("revision", "checklists")})
            if not isinstance(raw["history"], list) or len(raw["history"]) > MAX_HISTORY:
                raise ChecklistError("history")
            history = [self._validate_snapshot(s) for s in raw["history"]]
            if any(s["revision"] >= current["revision"] for s in history):
                raise ChecklistError("history revision")
            if {s["id"] for s in current["checklists"]} & {s["id"] for s in self._builtins()}:
                raise ChecklistError("builtin collision")
            return {"schema_version": 1, **current, "history": history}
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise ChecklistCorrupt("保存的清单库记录损坏，暂时无法读取。") from exc

    def list(self) -> dict:
        with self.lock:
            state = self._load()
            return {"revision": state["revision"], "checklists": self._builtins() + copy.deepcopy(state["checklists"])}

    def get(self, id: str) -> dict:
        _id(id, "清单id")
        for item in self.list()["checklists"]:
            if item["id"] == id:
                return item
        raise ChecklistError("清单不存在。")

    def _commit(self, current: dict, lists: list[dict]) -> int:
        state = {"schema_version": 1, "revision": current["revision"] + 1, "checklists": lists,
                 "history": [*current["history"], self._snapshot(current)][-MAX_HISTORY:]}
        payload = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        if len(payload) > MAX_STATE_BYTES:
            raise ChecklistError("清单库超过容量，本次未保存。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return state["revision"]

    @staticmethod
    def _expected(expected: Any, current: dict) -> None:
        if type(expected) is not int or expected < 0:
            raise ChecklistError("请求中的清单版本有误，请刷新后重试。")
        if expected != current["revision"]:
            raise ChecklistConflict(current["revision"])

    def save(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) != {"checklist", "expected_revision"}:
            raise ChecklistError("保存清单的请求格式不正确，请刷新后重试。")
        clean = normalize_checklist_snapshot(payload["checklist"])
        if not clean:
            raise ChecklistError("不能保存空清单。")
        with self.lock:
            state = self._load()
            self._expected(payload["expected_revision"], state)
            if clean["builtin"] or clean["id"] in {item["id"] for item in self._builtins()}:
                raise ChecklistError("内置清单不可覆盖，请复制为新清单。")
            lists = copy.deepcopy(state["checklists"])
            if not clean["id"]:
                if len(lists) >= 100:
                    raise ChecklistError("个人清单不能超过100份。")
                clean.update(id="cl-" + uuid.uuid4().hex, version=1)
                lists.append(clean)
            else:
                index = next((i for i, item in enumerate(lists) if item["id"] == clean["id"]), None)
                if index is None:
                    raise ChecklistError("清单不存在；新建时请留空id。")
                if clean["version"] != lists[index]["version"]:
                    raise ChecklistConflict(state["revision"])
                clean["version"] = lists[index]["version"] + 1
                lists[index] = clean
            revision = self._commit(state, lists)
            return {"revision": revision, "checklist": copy.deepcopy(clean)}

    def delete(self, id: str, expected_revision: int) -> dict:
        _id(id, "清单id")
        with self.lock:
            state = self._load()
            self._expected(expected_revision, state)
            if id in {item["id"] for item in self._builtins()}:
                raise ChecklistError("内置清单不可删除。")
            lists = [item for item in state["checklists"] if item["id"] != id]
            if len(lists) == len(state["checklists"]):
                raise ChecklistError("清单不存在。")
            return {"revision": self._commit(state, lists), "deleted_id": id}
