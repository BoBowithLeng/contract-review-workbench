"""Versioned, executable draft plans. No inference or model call happens here."""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Mapping

DRAFT_PLAN_VERSION = 1
DRAFT_PLAN_VERSION_FIELD = "draft_plan_version"
# Frozen per task. Version 1: a blank line inside proposed contract text is read
# as a paragraph break instead of failing the whole opinion. Version 2 also tells
# the model that comments go into the contract file, so no internal IDs there,
# and drops blank fields the plan has no place for (a stray "reason_note": "")
# instead of failing the opinion; extra fields with content still fail.
# Version 3 asks contract text that refers to another clause to use the
# clause number, not a heading path such as “开发费用—费用调整”.
# Tasks frozen without it, or with 1, keep their original prompt and reading, so
# saved reports re-derive as is. New tasks freeze the newest version.
DRAFT_TEXT_VERSION = 3
DRAFT_TEXT_VERSIONS = (1, 2, 3)
DRAFT_TEXT_VERSION_FIELD = "draft_text_version"
INSERTION_ANCHORS_FIELD = "draft_plan_insertion_anchors"
MAX_TARGETS = 8
MAX_PARAGRAPH_CHARS = 30_000
MAX_TOTAL_CHARS = 80_000
_LABEL = re.compile(r"^\s*(?P<label>(?:\d+(?:\.\d+)+|\d+[、.]|第[一二三四五六七八九十百千零〇0-9]+条))(?P<space>\s*)")
_INSTRUCTION = re.compile(r"^\s*(?:建议\s*)?(?:将|把|在)?(?:第[^\n：:]{0,40})?(?:条|款|段)?\s*(?:修改|改为|替换|删除|增加|补充|调整|完善)(?:为|成|如下)?\s*[：:]")

PROMPT_RULES = (
    "每条finding必须在本次回复中给出draft_plan，交付前明确哪里改、改后全文及引用但保留的原因。"
    "draft_plan不是讨论建议，edits仅替换refs中指定原稿段落的整段全文；相关refs不代表全都必须改，"
    "paragraph_actions必须按原稿piece_id逐一覆盖全部refs，modify恰好对应实际edits，其余说明retain原因。"
    "新增独立段落使用insertions及明确after_piece_id，只能选择draft_plan_insertion_anchors中的合法锚点；"
    "表格或不存在的位置不可冒充安全新增位置；同一锚点的多段新增合为一个new_text，以换行分段。"
    "保留原条号，不重复原文，不用操作说明代替合同正文；不得猜测金额、日期或业务事实。"
    "ready须至少一处真实修改或新增，questions=[]，不能有needs_confirmation；"
    "缺业务事实、选择或合法新增位置时用needs_input，edits/insertions必须为空，questions具体说明缺什么，"
    "不能把不完整的结构化输出解释成业务信息不足。无须改稿用not_needed、空修改、空问题并解释保留。"
    "不得输出finding.clause_draft或draft_plan_version；展示条款由程序从已验证的plan生成。"
)
COMMENT_RULE = "comment会作为Word批注写进合同文件，提到位置时写条款号和事项名称，不写F01、P017、Q01等内部编号。"
CROSS_REFERENCE_RULE = ("new_text会成为合同正文，引用本合同其他条款时写条款号（如“第2.2条”），条款号以合同段落位置中的编号为准；"
                        "不用“开发费用—费用调整”这类标题路径，也不写P017等内部编号。原条款没有编号时，用条款标题加书名号或引号指明。")


def draft_text_version(context: Mapping[str, Any] | None) -> int:
    """The frozen version, or 0 for tasks from before versioning (or an unknown value)."""
    value = (context or {}).get(DRAFT_TEXT_VERSION_FIELD)
    return value if type(value) is int and value in DRAFT_TEXT_VERSIONS else 0


def prompt_rules(context: Mapping[str, Any] | None) -> str:
    """Draft-plan rules exactly as the task's frozen version asked for them."""
    version = draft_text_version(context)
    return PROMPT_RULES + (COMMENT_RULE if version >= 2 else "") + (CROSS_REFERENCE_RULE if version >= 3 else "")


def response_schema() -> dict[str, Any]:
    def obj(properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    text = {"type": "string"}
    return obj({
        "status": {"type": "string", "enum": ["ready", "needs_input", "not_needed"]},
        "edits": {"type": "array", "maxItems": MAX_TARGETS, "items": obj({"piece_id": text, "new_text": text})},
        "insertions": {"type": "array", "maxItems": MAX_TARGETS, "items": obj({"after_piece_id": text, "new_text": text})},
        "paragraph_actions": {"type": "array", "maxItems": MAX_TARGETS, "items": obj({
            "piece_id": text, "action": {"type": "string", "enum": ["modify", "retain", "needs_confirmation"]}, "reason": text})},
        "questions": {"type": "array", "maxItems": 6, "items": text}, "comment": text,
    })


def insertion_capabilities(contract: Any, source_path: str | Path | bytes | None = None) -> list[dict[str, str]]:
    """Only real DOCX body/plain paragraph anchors; text-only contracts expose none."""
    if source_path is None:
        return []
    from docx_delivery import inspect_source
    parsed = contract.model_dump() if hasattr(contract, "model_dump") else contract
    pieces = {p["id"]: p["text"] for p in parsed["pieces"]}
    source = inspect_source(source_path if isinstance(source_path, bytes) else Path(source_path).read_bytes())
    if {p["id"]: p["text"] for p in source["pieces"]} != pieces:
        raise ValueError("新增段落能力与冻结原稿不一致。")
    return [{"piece_id": p["id"], "text": p["text"]} for p in source["pieces"]
            if p.get("supports_paragraph_insertion") is True]


_PLAN_KEYS = {"status", "edits", "insertions", "paragraph_actions", "questions", "comment"}
_ROW_KEYS = {"edits": {"piece_id", "new_text"}, "insertions": {"after_piece_id", "new_text"},
             "paragraph_actions": {"piece_id", "action", "reason"}}


def _blank(value: Any) -> bool:
    return value is None or value in ([], {}) or (isinstance(value, str) and not value.strip())


def drop_blank_extras(raw: Any) -> Any:
    """Remove blank fields the plan has no place for; every other value is untouched."""
    if not isinstance(raw, dict):
        return raw
    plan = {key: value for key, value in raw.items() if key in _PLAN_KEYS or not _blank(value)}
    for key, allowed in _ROW_KEYS.items():
        if isinstance(plan.get(key), list):
            plan[key] = [{k: v for k, v in row.items() if k in allowed or not _blank(v)} if isinstance(row, dict) else row
                         for row in plan[key]]
    return plan


def normalize_plan(raw: Any, version: int) -> Any:
    """What the frozen draft text version reads leniently before strict validation."""
    if version >= 1:
        raw = collapse_blank_lines(raw)
    if version >= 2:
        raw = drop_blank_extras(raw)
    return raw


def collapse_blank_lines(raw: Any) -> Any:
    """Drop empty lines from proposed edit and insertion text; everything else is untouched."""
    if not isinstance(raw, dict):
        return raw
    def rows(key):
        value = raw.get(key)
        if not isinstance(value, list):
            return value
        return [{**row, "new_text": "\n".join(line for line in row["new_text"].splitlines() if line.strip())}
                if isinstance(row, dict) and isinstance(row.get("new_text"), str) else row for row in value]
    return {**raw, "edits": rows("edits"), "insertions": rows("insertions")} if {"edits", "insertions"} <= set(raw) else raw


def validate_draft_plan(raw: Any, *, pieces: Mapping[str, str], refs: list[dict],
                        allowed_insertion_ids: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"status", "edits", "insertions", "paragraph_actions", "questions", "comment"}:
        raise ValueError("draft_plan 缺失或结构无效，不能当作可用改稿或业务资料不足")
    status = raw["status"]
    if not isinstance(status, str) or status not in {"ready", "needs_input", "not_needed"}:
        raise ValueError("draft_plan 状态无效")
    for key, limit in (("edits", MAX_TARGETS), ("insertions", MAX_TARGETS), ("paragraph_actions", MAX_TARGETS), ("questions", 6)):
        if not isinstance(raw[key], list) or len(raw[key]) > limit:
            raise ValueError(f"draft_plan.{key} 格式或数量无效")
    if not isinstance(raw["comment"], str) or not raw["comment"].strip() or len(raw["comment"]) > 600:
        raise ValueError("draft_plan 缺少简短具体说明或超过上限")
    if any(not isinstance(q, str) or not q.strip() or len(q) > 500 for q in raw["questions"]):
        raise ValueError("draft_plan.questions 必须是具体非空问题")
    target_ids = [r["id"] for r in refs]
    if len(target_ids) != len(set(target_ids)) or any(pid not in pieces or not pieces[pid].strip() for pid in target_ids):
        raise ValueError("改稿引用段落重复、不存在或为空")
    action_map = {}
    for row in raw["paragraph_actions"]:
        if not isinstance(row, dict) or set(row) != {"piece_id", "action", "reason"}:
            raise ValueError("改稿逐段说明结构无效")
        pid, action, reason = row["piece_id"], row["action"], row["reason"]
        if not isinstance(pid, str) or pid not in target_ids or pid in action_map:
            raise ValueError("改稿逐段说明重复或超出refs")
        if not isinstance(action, str) or action not in {"modify", "retain", "needs_confirmation"}:
            raise ValueError("改稿逐段动作无效")
        if not isinstance(reason, str) or len(reason.strip()) < 4 or len(reason) > 1200:
            raise ValueError("改稿逐段说明缺少具体理由或超过上限")
        action_map[pid] = {**row, "reason": reason.strip()}
    if set(action_map) != set(target_ids):
        raise ValueError("改稿逐段说明必须覆盖全部refs")
    modified = {pid for pid, row in action_map.items() if row["action"] == "modify"}
    pending = {pid for pid, row in action_map.items() if row["action"] == "needs_confirmation"}
    if status != "ready":
        if raw["edits"] or raw["insertions"] or modified:
            raise ValueError("未就绪改稿不能包含可确认修改")
        if status == "needs_input" and not raw["questions"]:
            raise ValueError("业务资料不足必须列出具体问题")
        if status == "not_needed" and (raw["questions"] or pending):
            raise ValueError("无需改稿不能包含待确认动作或问题")
    elif raw["questions"] or pending or not (raw["edits"] or raw["insertions"]):
        raise ValueError("ready必须有真实修改且没有待确认问题")
    seen_edits, seen_anchors = set(), set()
    original_labels = {m["label"] for text in pieces.values() for line in text.splitlines()
                       if (m := _LABEL.match(line))}
    inserted_labels = set()
    total = 0
    def body(text):
        nonlocal total
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_PARAGRAPH_CHARS:
            raise ValueError("改后全文为空或超过上限")
        if any(not line.strip() for line in text.splitlines()):
            raise ValueError("改后全文包含空段")
        for line in text.splitlines():
            label = _LABEL.match(line)
            if _INSTRUCTION.match(line) or _INSTRUCTION.match(line[label.end():] if label else line):
                raise ValueError("修改操作说明不能当作合同正文")
        total += len(text)
        if total > MAX_TOTAL_CHARS:
            raise ValueError("改稿总长度超过上限")
    for edit in raw["edits"]:
        if not isinstance(edit, dict) or set(edit) != {"piece_id", "new_text"}:
            raise ValueError("改稿edit结构无效")
        pid, text = edit["piece_id"], edit["new_text"]
        if not isinstance(pid, str) or pid not in target_ids or pid in seen_edits:
            raise ValueError("改稿edit重复或超出refs")
        body(text)
        old = pieces[pid]
        if text == old:
            raise ValueError("改稿没有实际文字变化")
        old_label, new_label = _LABEL.match(old), _LABEL.match(text)
        if old_label and (not new_label or new_label["label"] != old_label["label"]):
            raise ValueError("改稿必须保留原段落条号")
        if old_label and sum(1 for line in text.splitlines() if (m := _LABEL.match(line)) and m["label"] == old_label["label"]) > 1:
            raise ValueError("改稿重复原段落条号")
        # Historical quote-based revisions must not silently broaden a partial reference.
        quotes = [r.get("quote", old) for r in refs if r["id"] == pid]
        quote = quotes[0]
        if isinstance(quote, str) and quote and quote != old:
            if old.count(quote) != 1:
                raise ValueError("部分引文不能唯一确定可修改范围")
            start = old.index(quote)
            prefix, suffix = old[:start], old[start + len(quote):]
            if not text.startswith(prefix) or (suffix and not text.endswith(suffix)):
                raise ValueError("改稿改变了引用范围外的原文")
        seen_edits.add(pid)
    if seen_edits != modified:
        raise ValueError("modify逐段动作必须与真实edits完全对应")
    for insertion in raw["insertions"]:
        if not isinstance(insertion, dict) or set(insertion) != {"after_piece_id", "new_text"}:
            raise ValueError("新增段落结构无效")
        pid = insertion["after_piece_id"]
        if not isinstance(pid, str) or pid not in pieces or pid not in allowed_insertion_ids or pid in seen_anchors:
            raise ValueError("新增位置不在合法正文锚点中或重复")
        body(insertion["new_text"])
        for line in insertion["new_text"].splitlines():
            label = _LABEL.match(line)
            if label:
                if label["label"] in original_labels or label["label"] in inserted_labels:
                    raise ValueError("新增段落条号与原稿或其他新增段落重复")
                inserted_labels.add(label["label"])
        seen_anchors.add(pid)
    return {**copy.deepcopy(raw), "paragraph_actions": [action_map[pid] for pid in target_ids]}


def render_clause_draft(plan: Mapping[str, Any]) -> str:
    """Compatibility display is derived exclusively from a validated ready plan."""
    if plan.get("status") != "ready":
        return ""
    return "\n".join(row["new_text"] for row in [*plan["edits"], *plan["insertions"]])


def validate_docx_plan(plan: Mapping[str, Any], source_bytes: bytes) -> None:
    """Dry validation only: do not render, confirm, or persist document edits."""
    if plan.get("status") != "ready":
        return
    from docx_delivery import inspect_source, validate_plan
    source = inspect_source(source_bytes)
    pieces = {p["id"]: p for p in source["pieces"]}
    edits = []
    for index, row in enumerate([*plan["edits"], *plan["insertions"]]):
        insertion = "after_piece_id" in row
        piece = pieces[row["after_piece_id"] if insertion else row["piece_id"]]
        edits.append({"id": f"draft-validation-{index}", "piece_id": piece["id"],
            "paragraph_sha256": piece["paragraph_sha256"],
            "start": len(piece["text"]) if insertion else 0, "end": len(piece["text"]),
            "old_text": "" if insertion else piece["text"], "new_text": row["new_text"],
            "confirmed": True, **({"kind": "insert_paragraph_after"} if insertion else {})})
    validate_plan(source_bytes, {"schema_version": 1, "source_sha256": source["source_sha256"],
        "revision": 1, "author": "Draft validation", "created_at": "2000-01-01T00:00:00Z", "edits": edits, "comments": []})
