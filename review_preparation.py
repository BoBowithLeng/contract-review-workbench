"""Semantic, separately billed preparation; model input and output stay data."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from contract_core import ParsedContract, _strip_json_fence
from checklist_groups import VERSION_FIELD, freeze_groups


PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "review_preparation.md"
COMMON_RULES_PATH = PROMPT_PATH.parent / "common_review_rules.md"


def common_rules_text() -> str:
    value = COMMON_RULES_PATH.read_text(encoding="utf-8-sig")
    if not value.strip():
        raise ValueError("通用审查规则正文为空。")
    return value


MAX_QUESTIONS = 20
MAX_REQUIREMENTS = 100
MAX_CHECKS = 500
_QUESTION_ID = re.compile(r"Q[0-9]{1,3}\Z")
_EXTRA_ID = re.compile(r"REQ-[0-9]{1,3}\Z")


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def response_schema() -> dict[str, Any]:
    text = {"type": "string"}
    return _object({
        "questions": {"type": "array", "maxItems": MAX_QUESTIONS, "items": _object({
            "id": {"type": "string", "pattern": r"^Q[0-9]{1,3}$"}, "question": text, "reason": text, "required": {"type": "boolean"},
        })},
        "requirements": {"type": "array", "maxItems": MAX_REQUIREMENTS, "items": _object({
            "source_text": text, "related_check_ids": {"type": "array", "items": text},
            "disposition": {"type": "string", "enum": ["merged", "added", "context"]}, "reason": text,
        })},
        "additional_checks": {"type": "array", "maxItems": MAX_REQUIREMENTS, "items": _object({
            "id": {"type": "string", "pattern": r"^REQ-[0-9]{1,3}$"}, "title": text,
        })},
    })


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def pending() -> dict[str, Any]:
    return {"revision": 0, "plan_revision": 0, "status": "pending", "questions": [],
            "checks": [], "requirements": [], "original_requirements": "", "error": None}


def build_messages(parsed: ParsedContract, context: dict[str, Any], checklist: dict[str, Any] | None,
                   *, prompt_text: str | None = None) -> list[dict[str, str]]:
    prompt = PROMPT_PATH.read_text(encoding="utf-8-sig") + "\n\n" + common_rules_text() if prompt_text is None else prompt_text
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("审前准备指令为空。")
    return [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps({
        "phase": "review_preparation", "context": context,
        "contract": [piece.model_dump() for piece in parsed.pieces],
        "selected_checklist": checklist, "required_schema": response_schema(),
    }, ensure_ascii=False)}]


def _text(value: Any, name: str, *, limit: int = 6000, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f"审前准备 {name} 必须是有效文本。")
    return value


def parse_response(raw: str, context: dict[str, Any], checklist: dict[str, Any] | None,
                   revision: int, *, group_protocol_version: int | None = None) -> dict[str, Any]:
    try:
        data = json.loads(_strip_json_fence(raw))
    except (TypeError, json.JSONDecodeError):
        raise ValueError("审前准备未返回有效 JSON。") from None
    if not isinstance(data, dict):
        raise ValueError("审前准备结果不是对象。")
    for field, limit in (("questions", MAX_QUESTIONS), ("requirements", MAX_REQUIREMENTS),
                         ("additional_checks", MAX_REQUIREMENTS)):
        if not isinstance(data.get(field), list) or len(data[field]) > limit:
            raise ValueError(f"审前准备 {field} 缺失或超过容量。")
    questions = []
    seen: set[str] = set()
    for item in data["questions"]:
        if not isinstance(item, dict):
            raise ValueError("审前问题不是对象。")
        qid = item.get("id")
        if not isinstance(qid, str) or not _QUESTION_ID.fullmatch(qid) or qid in seen:
            raise ValueError("审前问题 id 无效或重复。")
        seen.add(qid)
        if type(item.get("required")) is not bool:
            raise ValueError("审前问题缺少必要性状态。")
        questions.append({"id": qid, "question": _text(item.get("question"), "question"),
                          "reason": _text(item.get("reason"), "reason"), "required": item["required"],
                          "answer": "", "status": "unanswered"})
    checks = []
    for item in (checklist or {}).get("items", []):
        if item["enabled"]:
            checks.append({**copy.deepcopy(item), "source": "checklist", "source_texts": []})
    original_ids = {item["id"] for item in checks}
    extra_ids: set[str] = set()
    for item in data["additional_checks"]:
        if not isinstance(item, dict):
            raise ValueError("本次补充检查项不是对象。")
        cid = item.get("id")
        if not isinstance(cid, str) or not _EXTRA_ID.fullmatch(cid) or cid in original_ids | extra_ids:
            raise ValueError("本次补充检查 id 无效或重复。")
        extra_ids.add(cid)
        checks.append({"id": cid, "title": _text(item.get("title"), "title", limit=500),
                       "requirement": "", "when": "", "facts": "", "guidance": "", "notes": "",
                       "enabled": True, "source": "request", "source_texts": []})
    if len(checks) > MAX_CHECKS:
        raise ValueError("本次检查项超过容量。")
    known = {item["id"]: item for item in checks}
    goal = context.get("business_goal", "")
    requirements = []
    seen_requirements: set[str] = set()
    for item in data["requirements"]:
        if not isinstance(item, dict):
            raise ValueError("本次要求映射不是对象。")
        quote = _text(item.get("source_text"), "source_text")
        if quote not in goal or quote in seen_requirements:
            raise ValueError("本次要求摘句不在原要求中或重复。")
        seen_requirements.add(quote)
        ids = item.get("related_check_ids")
        disposition = item.get("disposition")
        if (not isinstance(ids, list) or any(not isinstance(cid, str) or cid not in known for cid in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("本次要求引用不存在或重复的检查项。")
        if disposition == "context":
            valid = not ids
        elif disposition == "merged":
            valid = bool(ids) and all(cid in original_ids for cid in ids)
        elif disposition == "added":
            valid = bool(ids) and all(cid in extra_ids for cid in ids)
        else:
            valid = False
        if not valid:
            raise ValueError("本次要求合并/新增去向无效。")
        requirements.append({"source_text": quote, "related_check_ids": ids,
                             "disposition": disposition, "reason": _text(item.get("reason"), "reason")})
        for cid in ids:
            known[cid]["source_texts"].append(quote)
    for check in checks:
        if check["source"] == "request":
            if not check["source_texts"]:
                raise ValueError("新增检查项缺少原审查要求。")
            check["requirement"] = "\n".join(check["source_texts"])
        elif check["source_texts"]:
            check["requirement"] += "\n本次补充要求：\n" + "\n".join(check["source_texts"])
    result = {"revision": revision, "plan_revision": revision, "status": "prepared", "questions": questions,
              "checks": checks, "requirements": requirements, "original_requirements": goal, "error": None}
    result["information_policy_version"] = 2
    if group_protocol_version is not None:
        freeze_groups(result, group_protocol_version)
    result["status"] = readiness(result)
    return result


def readiness(preparation: dict[str, Any]) -> str:
    # Saving is an explicit decision to review what can be judged, not a model gate.
    if preparation.get("answers_saved") or preparation.get("rereview_context"):
        return "prepared"
    return "needs_info" if any(q["required"] and (q["status"] != "answered" or not q["answer"].strip())
                               for q in preparation["questions"]) else "prepared"


def plan_digest(preparation: dict[str, Any]) -> str:
    plan = {"plan_revision": preparation["plan_revision"],
                   "questions": [{key: q[key] for key in ("id", "question", "reason", "required")}
                                 for q in preparation["questions"]],
                   "checks": preparation["checks"], "requirements": preparation["requirements"],
                   "original_requirements": preparation["original_requirements"]}
    if VERSION_FIELD in preparation:
        plan[VERSION_FIELD] = preparation[VERSION_FIELD]
    return digest(plan)
