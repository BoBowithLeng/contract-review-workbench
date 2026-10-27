"""Read-only homepage metadata for the local contract review workbench."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

from contract_core import parse_docx
from review_jobs import JOB_ID_RE
from service_review import PROFILE_KEY as SERVICE_PROFILE_KEY, ROLE_LABELS


_SHA256 = re.compile(r"[0-9a-f]{64}")
_PENDING = {"ready", "preparing", "needs_info", "prepared", "running", "failed"}
_SAFE_JOB_ERRORS = {
    "审核执行因服务重启或线程中断而停止；未自动重试。",
    "无法启动审核执行线程；未自动重试。",
    "模型返回未通过结果校验。",
    "模型执行或结果校验失败。",
}
_JOB_ERROR_LABELS = {
    "审核执行因服务重启或线程中断而停止；未自动重试。": "上次审查中断了，需要重新运行。",
    "无法启动审核执行线程；未自动重试。": "这次审查没有启动，可以重试。",
    "模型返回未通过结果校验。": "返回的审查结果暂时不能使用，需要重新运行。",
    "模型执行或结果校验失败。": "这次审查没有完成，可以重新运行。",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return value


def _latest(*values: str | None) -> str | None:
    candidates: list[tuple[dt.datetime, str]] = []
    for value in values:
        clean = _iso(value)
        if clean is None:
            continue
        parsed = dt.datetime.fromisoformat(clean.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        candidates.append((parsed, clean))
    return max(candidates, default=(None, None), key=lambda item: item[0])[1]


def _context_metadata(context: Any) -> tuple[str, str | None, str | None, str]:
    clean = context if isinstance(context, dict) else {}
    if clean.get("contract_type") == "general":
        role = clean.get("our_role") if isinstance(clean.get("our_role"), str) else ""
        party = clean.get("our_party") if isinstance(clean.get("our_party"), str) else ""
        return "general", role or None, party or None, " · ".join(value for value in (role, party) if value) or "未指定"
    if clean.get("contract_type") == SERVICE_PROFILE_KEY:
        role = clean.get("our_role") if clean.get("our_role") in ROLE_LABELS else None
        party = clean.get("our_party") if isinstance(clean.get("our_party"), str) and clean.get("our_party").strip() else None
        role_label = ROLE_LABELS.get(role, "未知")
        party_label = f"{role_label} · {party.strip()}" if party else role_label
        return SERVICE_PROFILE_KEY, role, party.strip() if party else None, party_label
    role = clean.get("our_role") if clean.get("our_role") in {"employer", "employee"} else None
    party = clean.get("our_party") if isinstance(clean.get("our_party"), str) and clean.get("our_party").strip() else None
    labels = {"employer": "用人单位", "employee": "劳动者"}
    role_label = labels.get(role, "未知")
    party_label = f"{role_label} · {party.strip()}" if party else role_label
    return "labor", role, party.strip() if party else None, party_label


def _review_title(loaded: Mapping[str, Any], *, legacy: bool) -> str:
    context = loaded.get("report", {}).get("context", {})
    contract_type, role, _, _ = _context_metadata(context)
    if contract_type == "general":
        return (context.get("contract_type_label") or "合同") + "审查"
    if contract_type == SERVICE_PROFILE_KEY and role in ROLE_LABELS:
        return f"{ROLE_LABELS[role]}视角审查"
    title = loaded.get("title")
    if legacy and isinstance(title, str) and title:
        return title
    return "劳动合同审查"


def _safe_status(path: Path, job_id: str) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("job_id") != job_id
        or value.get("status") not in _PENDING | {"completed"}
    ):
        return None
    return value


def _pending_item(status: Mapping[str, Any]) -> dict[str, Any]:
    contract_type, role, party, party_label = _context_metadata(status.get("context"))
    item = {
        "id": status["job_id"],
        "document_name": status.get("document_name") if isinstance(status.get("document_name"), str) else "未命名合同",
        "status": status["status"],
        "created_at": _iso(status.get("created_at")),
        "updated_at": _iso(status.get("updated_at")),
        "party_label": party_label,
        "contract_type": contract_type, "our_role": role, "our_party": party,
        "report_id": status.get("report_id"),
    }
    if status.get("workflow_version") == 2:
        item["workflow_version"] = 2
        item["preparation"] = {"status": status.get("preparation", {}).get("status")}
    error = status.get("error")
    if status["status"] == "failed" and isinstance(error, str) and error:
        item["error"] = (_JOB_ERROR_LABELS.get(error, error) if error in _SAFE_JOB_ERRORS
                         else "这次审查没有完成，可以重新运行。")
    return item


def _source_bytes(path: Path | None) -> bytes | None:
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


@lru_cache(maxsize=4)
def _source_fingerprint(data: bytes) -> str:
    return parse_docx(data).fingerprint


def _validate_source(loaded: Mapping[str, Any], data: bytes) -> str:
    expected = loaded.get("contract", {}).get("fingerprint")
    if not isinstance(expected, str) or _source_fingerprint(data) != expected or loaded.get("report", {}).get("fingerprint") != expected:
        raise ValueError("source mismatch")
    return _sha256(data)


def _finding_links(
    findings: list[dict[str, Any]], edits: list[dict[str, Any]], comments: list[dict[str, Any]]
) -> tuple[set[str], set[str]]:
    finding_ids = {item.get("id") for item in findings if isinstance(item.get("id"), str)}
    edit_links: dict[str, set[str]] = {}
    modified: set[str] = set()
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        linked = {
            item for item in edit.get("finding_ids", [])
            if isinstance(item, str) and item in finding_ids
        }
        edit_id = edit.get("id")
        if isinstance(edit_id, str):
            linked.update(fid for fid in finding_ids if edit_id.startswith(f"edit-{fid}-"))
            edit_links[edit_id] = linked
        modified.update(linked)
    commented: set[str] = set()
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        commented.update(
            item for item in comment.get("finding_ids", [])
            if isinstance(item, str) and item in finding_ids
        )
        edit_id = comment.get("edit_id")
        if isinstance(edit_id, str):
            commented.update(edit_links.get(edit_id, set()))
        comment_id = comment.get("id")
        if isinstance(comment_id, str):
            commented.update(
                fid for fid in finding_ids
                if comment_id.startswith(f"comment-{fid}") or comment_id.startswith(f"note-{fid}")
            )
    return modified, commented


def _progress(
    loaded: Mapping[str, Any], delivery_state: Mapping[str, Any], review_state: Mapping[str, Any]
) -> tuple[int, int, int, str]:
    findings = [item for item in loaded.get("report", {}).get("findings", []) if isinstance(item, dict)]
    edits = delivery_state.get("edits", [])
    comments = delivery_state.get("comments", [])
    modified, commented = _finding_links(findings, edits, comments)
    rejected = {
        finding_id for finding_id, review in review_state.get("reviews", {}).items()
        if isinstance(review, dict) and review.get("decision") == "驳回"
    }
    known = {item.get("id") for item in findings}
    handled = (modified | commented | rejected) & known
    count = len(findings)
    if count == 0:
        status = "结果已生成"
    elif len(handled) >= count:
        status = "已复核"
    elif handled:
        status = "复核中"
    else:
        status = "待复核"
    return len(handled), len(edits), len(comments), status


def _document_id(group_key: str) -> str:
    return "doc-" + _sha256(group_key.encode("utf-8"))[:24]


def build_workbench(
    *,
    legacy_ids: list[str],
    legacy_specs: Mapping[str, Mapping[str, Any]],
    load_legacy: Callable[[str], dict[str, Any]],
    jobs: Any,
    state_dir: Path,
    source_paths: Mapping[str, Path | None],
    delivery: Any,
    load_review_state: Callable[[dict[str, Any]], dict[str, Any]],
    loaded_reports: dict[str, dict[str, Any]] | None = None,
    excluded_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Build homepage metadata without changing any persisted project state."""
    del state_dir  # The validated state callbacks own their exact paths.
    records: list[tuple[str, dict[str, Any]]] = []
    pending: list[dict[str, Any]] = []
    excluded = excluded_ids or set()

    for report_id in legacy_ids:
        if report_id in excluded:
            continue
        spec = legacy_specs[report_id]
        loaded: dict[str, Any] | None = None
        error: str | None = None
        try:
            loaded = load_legacy(report_id)
            if loaded_reports is not None:
                loaded_reports[report_id] = loaded
        except Exception:
            error = "这份审查结果暂时无法校验。"
        configured = source_paths.get(report_id)
        data = _source_bytes(configured)
        source_sha: str | None = None
        if loaded is not None and configured is not None:
            if data is None:
                error = "原稿文件不可用。"
            else:
                try:
                    source_sha = _validate_source(loaded, data)
                except (OSError, ValueError):
                    error = "原稿文字已变化，不能当作这份审查的原文。"
        if source_sha is not None:
            group_key = "source:" + source_sha
        elif report_id == "minimal" and loaded is not None:
            fingerprint = loaded.get("contract", {}).get("fingerprint")
            group_key = "text:" + fingerprint if isinstance(fingerprint, str) else "legacy:" + report_id
        else:
            group_key = "legacy:" + report_id
        if loaded is None:
            review = {
                "id": report_id,
                "title": spec.get("title", "审查结果"),
                "party_label": "未知", "our_role": None, "our_party": None,
                "created_at": None, "updated_at": None, "status": "结果已生成",
                "findings_count": 0, "handled_count": 0, "modified_count": 0,
                "comment_count": 0, "available": False, "error": error,
            }
            records.append((group_key, {"document_name": spec.get("document_name", "未命名合同"), "review": review}))
            continue
        review = _review_record(
            loaded, report_id, legacy=True, created_at=None, completed_at=None,
            source_data=data, source_sha=source_sha, delivery=delivery,
            load_review_state=load_review_state, existing_error=error,
        )
        records.append((group_key, {"document_name": loaded.get("document_name", spec.get("document_name", "未命名合同")), "review": review}))

    jobs_root = Path(jobs.root)
    if jobs_root.is_dir():
        for directory in sorted(jobs_root.iterdir(), key=lambda item: item.name):
            if not directory.is_dir() or not JOB_ID_RE.fullmatch(directory.name):
                continue
            if directory.name in excluded:
                continue
            status = _safe_status(directory / "status.json", directory.name)
            if status is None:
                continue
            if status["status"] in _PENDING:
                pending.append(_pending_item(status))
                continue
            if status.get("report_id") != directory.name:
                continue
            expected_sha = status.get("source_sha256")
            group_key = "source:" + expected_sha if isinstance(expected_sha, str) and _SHA256.fullmatch(expected_sha) else "job:" + directory.name
            loaded = None
            error = None
            try:
                loaded = jobs.load_report(directory.name)
                if loaded_reports is not None:
                    loaded_reports[directory.name] = loaded
            except Exception:
                error = "这份审查结果或原稿暂时无法校验。"
            if loaded is None:
                contract_type, role, party, party_label = _context_metadata(status.get("context"))
                review = {
                    "id": directory.name,
                    "title": ((status.get("context", {}).get("contract_type_label") or "合同") + "审查") if contract_type == "general" else (f"{ROLE_LABELS[role]}视角审查" if contract_type == SERVICE_PROFILE_KEY and role in ROLE_LABELS else "劳动合同审查"),
                    "party_label": party_label, "our_role": role, "our_party": party,
                    "contract_type": contract_type,
                    "created_at": _iso(status.get("created_at")), "updated_at": _iso(status.get("updated_at")),
                    "status": "结果已生成", "findings_count": status.get("findings_count") if isinstance(status.get("findings_count"), int) else 0,
                    "handled_count": 0, "modified_count": 0, "comment_count": 0,
                    "available": False, "error": error,
                }
            else:
                source_data = _source_bytes(directory / "source.docx")
                review = _review_record(
                    loaded, directory.name, legacy=False,
                    created_at=_iso(status.get("created_at")), completed_at=_iso(status.get("updated_at")),
                    source_data=source_data, source_sha=expected_sha if isinstance(expected_sha, str) else None,
                    delivery=delivery, load_review_state=load_review_state, existing_error=error,
                )
            records.append((group_key, {"document_name": status.get("document_name", "未命名合同"), "review": review}))

    documents: list[dict[str, Any]] = []
    for group_key, record in records:
        review = record["review"]
        report_id = review["id"]
        contract_type = review.pop("contract_type", "labor")
        documents.append({
            "id": _document_id("report:" + report_id),
            "legacy_document_id": _document_id(group_key),
            "document_name": record["document_name"],
            "contract_type": contract_type,
            "review_count": 1,
            "updated_at": review.get("updated_at"),
            "reviews": [review],
        })
    documents.sort(key=lambda item: (item.get("updated_at") is not None, item.get("updated_at") or "", item["id"]), reverse=True)
    pending.sort(key=lambda item: (item.get("updated_at") or "", item["id"]), reverse=True)
    return {"documents": documents, "pending_jobs": pending}


def _review_record(
    loaded: dict[str, Any], report_id: str, *, legacy: bool,
    created_at: str | None, completed_at: str | None,
    source_data: bytes | None, source_sha: str | None,
    delivery: Any, load_review_state: Callable[[dict[str, Any]], dict[str, Any]],
    existing_error: str | None,
) -> dict[str, Any]:
    context = loaded.get("report", {}).get("context", {})
    contract_type, role, party, party_label = _context_metadata(context)
    error = existing_error
    delivery_state: dict[str, Any] = {"edits": [], "comments": [], "created_at": None}
    review_state: dict[str, Any] = {"reviews": {}}
    if error is None and source_data is not None and source_sha is not None:
        try:
            with delivery.lock:
                delivery_state = delivery._load_state(loaded, source_data, source_sha)
        except Exception:
            error = "文档复核状态损坏或与原稿不一致。"
    try:
        review_state = load_review_state(loaded)
    except Exception:
        error = error or "人工复核状态损坏，未展示处理进度。"
    handled, modified, comments, status = _progress(loaded, delivery_state, review_state)
    updated_at = _latest(completed_at, delivery_state.get("created_at"))
    available = error is None
    if not available:
        status = "结果已生成"
    result = {
        "id": report_id,
        "title": _review_title(loaded, legacy=legacy),
        "party_label": party_label,
        "our_role": role,
        "our_party": party,
        "contract_type": contract_type,
        "created_at": created_at,
        "updated_at": updated_at,
        "status": status,
        "findings_count": len(loaded.get("report", {}).get("findings", [])),
        "handled_count": handled,
        "modified_count": modified,
        "comment_count": comments,
        "available": available,
    }
    if error:
        result["error"] = error
    rereview = loaded.get("report", {}).get("metadata", {}).get("rereview")
    if rereview:
        result["parent_report_id"] = rereview["parent_report_id"]
        result["round_number"] = rereview["round_number"]
        result["title"] = f"第{rereview['round_number']}轮复审"
    return result


__all__ = ["build_workbench"]
