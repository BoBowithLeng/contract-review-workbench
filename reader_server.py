"""Loopback-only reader for the three saved, real review runs.

The server intentionally has no arbitrary file API.  It serves three explicit
static assets and three explicit report IDs, then stores only human review
state in a digest-bound JSON file.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import functools
import json
import os
import re
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from completeness import check_completeness
from clause_discussion import (
    ClauseDiscussionConflict,
    ClauseDiscussionCorrupt,
    ClauseDiscussionManager,
    ClauseDiscussionUnavailable,
)
from checklist_store import ChecklistStore, ChecklistError, ChecklistConflict, ChecklistCorrupt
from checklist_excel import make_template, parse_workbook
from contract_facts import ContractFactsManager, ContractFactsError, ContractFactsConflict, ContractFactsCorrupt
from post_review_information import PostReviewInformationManager, InformationGateRequired
from contract_rereview import RereviewManager
from report_archive import ReportArchive, ArchiveError
from document_view import docx_navigation, index_document, sort_findings_by_document
from docx_delivery import ValidationError as DeliveryValidationError
from draft_preparation import (
    DraftPreparationConflict,
    DraftPreparationCorrupt,
    DraftPreparationInput,
    DraftPreparationManager,
    DraftPreparationUnavailable,
)
from model_client import ModelClientError, list_models
from opinion_revisions import (
    OpinionRevisionConflict,
    OpinionRevisionCorrupt,
    OpinionRevisionManager,
    OpinionRevisionUnavailable,
)
from model_settings import (
    ModelSettings,
    ModelSettingsConflict,
    ModelSettingsCorrupt,
    ModelSettingsUnavailable,
)
from reader_delivery import (
    DeliveryConflict,
    DeliveryManager,
    DeliveryStateCorrupt,
    DeliveryUnavailable,
    IGNORE_NOTE,
    client_stem,
    client_zip_name,
)
from report_export import export_docx
from reader_workbench import build_workbench
from report_deletion import ReportDeletion, DeletionError, DeletionConflict, DeletionIncomplete, operation_lock
from workbench_groups import (
    WorkbenchGroupConflict,
    WorkbenchGroupCorrupt,
    WorkbenchGroupManager,
)
from review_jobs import (
    JOB_ID_RE,
    MAX_UPLOAD_ENVELOPE_BYTES,
    JobConflict,
    JobError,
    ReviewJobManager,
)
from tool_review import load_review


ROOT = Path(__file__).resolve().parent
MAX_BODY_BYTES = 128 * 1024
MAX_CHECKLIST_BODY_BYTES = 3 * 1024 * 1024
MAX_NOTE_CHARS = 4_000
MAX_DRAFT_CHARS = 20_000
REPORT_ID_PATTERN = r"(?:detailed|minimal|table|job-[0-9a-f]{32})"

REPORTS = {
    "detailed": {
        "title": "完整模板 · 详细审查",
        "document_name": "劳动合同模板",
        "directory": "sol-detailed-run-20260920",
    },
    "minimal": {
        "title": "短文本 · 基础缺项",
        "document_name": "短合同样例",
        "directory": "sol-minimal-run-20260920",
    },
    "table": {
        "title": "表格合同 · 阅读与定位",
        "document_name": "表格合同样例",
        "directory": "sol-table-run-20260920",
    },
}

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/workspace.js": ("workspace.js", "text/javascript; charset=utf-8"),
    "/delivery.js": ("delivery.js", "text/javascript; charset=utf-8"),
    "/right-panel.css": ("right-panel.css", "text/css; charset=utf-8"),
    "/review_jobs.js": ("review_jobs.js", "text/javascript; charset=utf-8"),
    "/upload.css": ("upload.css", "text/css; charset=utf-8"),
    "/workbench.js": ("workbench.js", "text/javascript; charset=utf-8"),
    "/workbench.css": ("workbench.css", "text/css; charset=utf-8"),
    "/discussion.js": ("discussion.js", "text/javascript; charset=utf-8"),
    "/readable_text.js": ("readable_text.js", "text/javascript; charset=utf-8"),
    "/marked.umd.js": ("vendor/marked.umd.js", "text/javascript; charset=utf-8"),
    "/purify.min.js": ("vendor/purify.min.js", "text/javascript; charset=utf-8"),
    "/opinion_revisions.js": ("opinion_revisions.js", "text/javascript; charset=utf-8"),
    "/discussion.css": ("discussion.css", "text/css; charset=utf-8"),
    "/api_profiles.js": ("api_profiles.js", "text/javascript; charset=utf-8"),
    "/model_settings.js": ("model_settings.js", "text/javascript; charset=utf-8"),
    "/desktop_ui.js": ("desktop_ui.js", "text/javascript; charset=utf-8"),
    "/desktop_ui.css": ("desktop_ui.css", "text/css; charset=utf-8"),
    "/checklists.js": ("checklists.js", "text/javascript; charset=utf-8"),
    "/checklists.html": ("checklists.html", "text/html; charset=utf-8"),
    "/checklists.css": ("checklists.css", "text/css; charset=utf-8"),
    "/information_gate.js": ("information_gate.js", "text/javascript; charset=utf-8"),
    "/information_gate.css": ("information_gate.css", "text/css; charset=utf-8"),
    "/contract_facts.js": ("contract_facts.js", "text/javascript; charset=utf-8"),
    "/rereview.js": ("rereview.js", "text/javascript; charset=utf-8"),
}

DECISIONS = {"接受", "修改", "驳回", "未复核"}
RISKS = {"高", "中", "低", "待判断"}


class StateCorruptError(RuntimeError):
    pass


class ReportDataError(RuntimeError):
    pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportDataError("报告文件无法读取或校验。") from exc


def _load_report(project_root: Path, report_id: str) -> dict[str, Any]:
    spec = REPORTS.get(report_id)
    if spec is None:
        raise KeyError(report_id)
    run_dir = project_root / "runtime" / spec["directory"]
    request_path = run_dir / "request.json"
    response_path = run_dir / "response.json"
    request = _read_json(request_path)
    response = _read_json(response_path)
    digest = request.get("request_digest")
    model = response.get("model")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ReportDataError("报告请求编号无效。")
    if not isinstance(model, str) or not model:
        raise ReportDataError("报告模型记录无效。")
    try:
        bundle = load_review(request_path, response_path, model=model)
    except Exception as exc:  # tool details never cross the HTTP boundary
        raise ReportDataError("报告未通过本地完整性校验。") from exc

    report = dict(bundle["report"])
    contract = bundle["contract"]
    report["findings"] = sort_findings_by_document(report.get("findings", []), contract)
    snapshot_sources = [dict(item) for item in bundle["sources"]]

    report["document_name"] = spec["document_name"]
    completeness = check_completeness(contract, report.get("context", {}))
    return {
        "id": report_id,
        "title": spec["title"],
        "document_name": spec["document_name"],
        "request_digest": digest,
        "report": report,
        "contract": contract,
        "sources": snapshot_sources,
        "paragraphs": index_document(contract),
        "completeness": completeness,
    }


def _state_path(state_dir: Path, report_id: str, digest: str) -> Path:
    if (
        report_id not in REPORTS
        and not JOB_ID_RE.fullmatch(report_id)
    ) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("复核状态标识无效。")
    return state_dir / f"{report_id}-{digest}.json"


def _default_reviews(report: dict[str, Any]) -> dict[str, dict[str, str]]:
    reviews: dict[str, dict[str, str]] = {}
    for finding in report.get("findings", []):
        finding_id = finding.get("id")
        if not isinstance(finding_id, str):
            raise ReportDataError("报告 finding id 无效。")
        risk = finding.get("risk") if finding.get("risk") in {"高", "中", "低"} else "待判断"
        reviews[finding_id] = {
            "decision": "未复核",
            "risk": risk,
            "note": "",
            "clause_draft": finding.get("clause_draft", "") if isinstance(finding.get("clause_draft", ""), str) else "",
        }
    return reviews


def _default_state(report: dict[str, Any]) -> dict[str, Any]:
    return {"revision": 0, "reviews": _default_reviews(report), "selected_options": [], "history": []}


def _validate_reviews(
    raw: Any,
    findings: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or set(raw) != set(findings):
        raise ValueError("reviews 必须完整包含本报告全部 finding，且不能有未知 id。")
    clean: dict[str, dict[str, Any]] = {}
    for finding_id, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError(f"{finding_id} 的人工复核不是对象。")
        decision, risk = value.get("decision"), value.get("risk")
        note, draft = value.get("note"), value.get("clause_draft")
        if decision not in DECISIONS:
            raise ValueError(f"{finding_id} 的处理决定无效。")
        if risk not in RISKS:
            raise ValueError(f"{finding_id} 的人工风险等级无效。")
        if not isinstance(note, str) or len(note) > MAX_NOTE_CHARS:
            raise ValueError(f"{finding_id} 的说明必须是不超过 {MAX_NOTE_CHARS} 字的文本。")
        if not isinstance(draft, str) or len(draft) > MAX_DRAFT_CHARS:
            raise ValueError(f"{finding_id} 的条款草案必须是不超过 {MAX_DRAFT_CHARS} 字的文本。")
        if decision in {"修改", "驳回"} and not note.strip():
            raise ValueError(f"{finding_id} 选择修改或驳回时必须填写说明。")
        if findings[finding_id].get("status") == "处理失败" and decision == "接受":
            raise ValueError(f"{finding_id} 未通过结果校验，不能直接接受。")
        clean[finding_id] = {
            "decision": decision,
            "risk": risk,
            "note": note,
            "clause_draft": draft,
        }
        if "opinion_revision" in value:
            version = value["opinion_revision"]
            if type(version) is not int or version < 0:
                raise ValueError(f"{finding_id} 的复核意见版本无效。")
            clean[finding_id]["opinion_revision"] = version
    return clean


def _validate_options(raw: Any, option_ids: set[str]) -> list[str]:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError("selected_options 必须是方案 id 列表。")
    if len(raw) != len(set(raw)):
        raise ValueError("selected_options 不能重复。")
    unknown = [item for item in raw if item not in option_ids]
    if unknown:
        raise ValueError("selected_options 含未知方案 id。")
    return list(raw)


def _validate_snapshot(raw: Any, report: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("历史状态不是对象。")
    revision = raw.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("历史 revision 无效。")
    findings = {item["id"]: item for item in report.get("findings", [])}
    option_ids = {item["id"] for item in report.get("options", [])}
    return {
        "revision": revision,
        "reviews": _validate_reviews(raw.get("reviews"), findings),
        "selected_options": _validate_options(raw.get("selected_options"), option_ids),
    }


def _load_state(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return _default_state(report)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        current = _validate_snapshot(raw, report)
        history_raw = raw.get("history")
        if not isinstance(history_raw, list):
            raise ValueError("history 不是列表。")
        current["history"] = [_validate_snapshot(item, report) for item in history_raw]
        return current
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise StateCorruptError("人工复核状态文件损坏，未作重置。") from exc


def _state_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "revision": state["revision"],
        "reviews": state["reviews"],
        "selected_options": state["selected_options"],
    }


def _undo_action(state: dict[str, Any]) -> dict[str, Any] | None:
    history = state.get("history")
    if not history:
        return None
    previous = history[-1]
    review_ids = sorted(
        finding_id
        for finding_id in set(state["reviews"]) | set(previous["reviews"])
        if state["reviews"].get(finding_id) != previous["reviews"].get(finding_id)
    )
    current_options = set(state["selected_options"])
    previous_options = set(previous["selected_options"])
    option_ids = sorted(current_options ^ previous_options)
    if len(review_ids) == 1 and not option_ids:
        finding_id = review_ids[0]
        decision = state["reviews"][finding_id]["decision"]
        return {
            "kind": "review",
            "finding_id": finding_id,
            "decision": decision,
            "label": f"撤回 {finding_id} 的本条复核（当前：{decision}）",
        }
    if len(option_ids) == 1 and not review_ids:
        option_id = option_ids[0]
        selected = option_id in current_options
        return {
            "kind": "option",
            "option_id": option_id,
            "selected": selected,
            "label": f"撤回{'选中' if selected else '取消'}方案 {option_id}",
        }
    change_count = len(review_ids) + len(option_ids)
    return {
        "kind": "multiple",
        "label": f"撤回上一次保存的 {change_count} 项变化" if change_count else "撤回上一次保存",
    }


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        **_state_snapshot(state),
        "can_undo": bool(state.get("history")),
        "undo_action": _undo_action(state),
    }


def _atomic_write(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
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


class ReaderServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        project_root: Path,
        state_dir: Path,
        source_paths: dict[str, Path | None],
        jobs: ReviewJobManager,
        draft_runner: Any = None,
        discussion_completion: Any = None,
        search_completion: Any = None,
        revision_completion: Any = None,
        draft_completion: Any = None,
        resource_root: Path | None = None,
        include_legacy_reports: bool = True,
    ):
        self.project_root = project_root
        self.resource_root = resource_root or project_root
        self.legacy_reports = REPORTS if include_legacy_reports else {}
        self.state_dir = state_dir
        self.state_lock = threading.RLock()
        self.operation_lock = operation_lock(project_root, state_dir, jobs.root)
        self.delivery = DeliveryManager(state_dir, source_paths)
        self.jobs = jobs
        self.drafts = DraftPreparationManager(state_dir / "drafts", self.delivery, draft_runner,
                                              completion=draft_completion)
        self.delivery.draft_candidate_resolver = self.drafts.resolve_confirmation
        self.delivery.draft_plan_candidate_resolver = self.drafts.resolve_plan_confirmation
        self.facts = ContractFactsManager(state_dir / "contract-facts")
        self.drafts.facts_provider = self.facts.get
        self.delivery.draft_facts_provider = self.facts.get
        self.information_gate = PostReviewInformationManager(state_dir / "post-review", self.facts, jobs)
        self.facts.mutation_guard = self.information_gate.guard_mutation
        self.discussions = ClauseDiscussionManager(
            state_dir / "discussions", self.delivery, discussion_completion, search_completion,
            facts_provider=self.facts.get,
            review_state_provider=lambda loaded: _load_state(
                _state_path(self.state_dir, loaded["id"], loaded["request_digest"]), loaded["report"]),
        )
        self.drafts.source_context_provider = self.discussions._source_context
        self.revisions = OpinionRevisionManager(
            state_dir / "opinion-revisions", self.discussions, revision_completion
        )
        self.archive = ReportArchive(state_dir / "report-archive.json")
        self.workbench_groups = WorkbenchGroupManager(
            state_dir / "workbench-groups.json",
            self.workbench_catalog,
        )
        self.model_settings = ModelSettings(state_dir)
        self.checklists = ChecklistStore(state_dir / "checklists.json",
                                        self.resource_root / "builtin_checklists.json" if resource_root else None)
        self.rereviews = RereviewManager(state_dir / "rereviews", jobs, self.delivery, self.facts)
        self.deletions = ReportDeletion(self, self.legacy_reports)
        self.rereviews.deleted_request_provider = self.deletions.deleted_rereview_request
        super().__init__(address, ReaderHandler)

    def workbench_catalog(self, *, include_archived: bool = False) -> dict[str, Any]:
        with self.operation_lock:
            loaded_reports = {}
            excluded = set(self.deletions.deleted_ids())
            if not include_archived:
                excluded.update(row["id"] for row in self.archive.read()["entries"])
            catalog = build_workbench(
                legacy_ids=[rid for rid in self.legacy_reports if rid not in self.deletions.deleted_ids()],
                legacy_specs=self.legacy_reports,
                load_legacy=lambda report_id: _load_report(self.project_root, report_id),
                jobs=self.jobs,
                state_dir=self.state_dir,
                source_paths=self.delivery.source_paths,
                delivery=self.delivery,
                loaded_reports=loaded_reports,
                excluded_ids=excluded,
                load_review_state=lambda loaded: _load_state(
                    _state_path(self.state_dir, loaded["id"], loaded["request_digest"]),
                    loaded["report"],
                ),
            )
            for item in [review for doc in catalog.get("documents", []) for review in doc.get("reviews", [])] + catalog.get("pending_jobs", []):
                rid = item.get("id")
                if not isinstance(rid, str) or not JOB_ID_RE.fullmatch(rid):
                    continue
                try:
                    status = self.jobs._status(rid)
                    if status.get("post_review_information_version") != 1 or status.get("report_id") != rid:
                        continue
                    loaded = loaded_reports.get(rid)
                    if loaded is None:
                        loaded = self.jobs.load_report(rid)
                    gate = self.information_gate.get(loaded)
                    item["information_gate_status"] = gate["status"]
                    if gate["status"] != "ready":
                        item["findings_count"] = 0
                        item["status"] = {"needs_response": "待确认审查信息", "running": "调整相关意见中", "failed": "意见调整未完成"}[gate["status"]]
                except (JobError, ContractFactsError, ValueError, OSError, TypeError, KeyError):
                    # A damaged round must not hide every other contract in the catalog.
                    item.update(available=False, findings_count=0, status="结果待核对",
                                error="这份报告或集中回答记录暂时无法校验，未绕过信息确认。")
            return self.deletions.visible(catalog if include_archived else self.archive.visible(catalog))


def _workspace_request(method):
    """Keep loading and writing within the same deletion exclusion window."""
    @functools.wraps(method)
    def wrapped(self):
        path = urllib.parse.urlsplit(self.path).path
        if not any(path == prefix or path.startswith(prefix + "/")
                   for prefix in ("/api/reports", "/api/jobs", "/api/workbench")):
            return method(self)
        if not self._valid_host() or (self.command == "POST" and not self._same_origin()):
            return method(self)  # Preserve Host/Origin validation before ID disclosure.
        with self.server.operation_lock:
            try:
                match = re.match(r"/api/(?:reports|jobs)/([^/]+)(?:/|$)", path)
                if match and match.group(1) in self.server.deletions.deleted_ids():
                    self.close_connection = True  # POST body has not been consumed.
                    self._error(404, "这份合同记录已永久删除。")
                    return
                return method(self)
            except InformationGateRequired as exc:
                self._error(409, str(exc), code="information_gate_required", report_id=exc.report_id, information_gate=exc.gate)
            except DeletionError as exc:
                self.close_connection = True
                self._error(500, str(exc))
    return wrapped

class ReaderHandler(BaseHTTPRequestHandler):
    server: ReaderServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid writing request material or local paths to a console log.
        return

    def _send_bytes(self, status: int, body: bytes, content_type: str, *, disposition: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        self._send_bytes(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str, **extra: Any) -> None:
        self._json(status, {"error": message, **extra})

    def _valid_host(self) -> bool:
        raw = self.headers.get("Host", "")
        try:
            parsed = urllib.parse.urlsplit("//" + raw)
            port = parsed.port
        except ValueError:
            return False
        return parsed.hostname == "127.0.0.1" and port == self.server.server_port

    def _same_origin(self) -> bool:
        return self.headers.get("Origin") == f"http://127.0.0.1:{self.server.server_port}"

    def _read_body(self, max_bytes: int = MAX_BODY_BYTES) -> dict[str, Any]:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("请求 Content-Type 必须是 application/json。")
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块请求正文。")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ValueError("请求缺少有效 Content-Length。") from None
        if length < 0 or length > max_bytes:
            raise ValueError(f"请求正文超过 {max_bytes} 字节限制。")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求正文不是有效 JSON。") from None
        if not isinstance(value, dict):
            raise ValueError("请求 JSON 必须是对象。")
        return value

    def _raw_report(self, report_id: str) -> dict[str, Any]:
        if report_id in self.server.deletions.deleted_ids():
            raise KeyError(report_id)
        if report_id in self.server.legacy_reports:
            loaded = _load_report(self.server.project_root, report_id)
        elif JOB_ID_RE.fullmatch(report_id):
            loaded = self.server.jobs.load_report(report_id)
            self.server.delivery.register_source(report_id, loaded["source_path"])
        else:
            raise KeyError(report_id)
        return loaded

    def _report(self, report_id: str) -> dict[str, Any]:
        loaded = self.server.information_gate.apply(self._raw_report(report_id))
        # Presentation metadata only: never rewrite the saved request/contract.
        if self.server.delivery.has_configured_source(report_id):
            try:
                source_data, _ = self.server.delivery._inspect_current(loaded)
                navigation = docx_navigation(source_data)
            except (DeliveryUnavailable, OSError, ValueError, KeyError):
                navigation = None
            if navigation is not None:
                loaded = {**loaded, "contract": {**loaded["contract"], **navigation}}
                loaded["paragraphs"] = index_document(loaded["contract"])
        loaded = self.server.revisions.apply(loaded)
        loaded["report"]["findings"] = sort_findings_by_document(
            loaded["report"].get("findings", []), loaded["contract"]
        )
        return loaded

    def _state_for(self, loaded: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        path = _state_path(self.server.state_dir, loaded["id"], loaded["request_digest"])
        return path, _load_state(path, loaded["report"])

    def _discussion_session(self) -> str | None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        values = query.get("session_id", [])
        if set(query) - {"session_id"} or len(values) > 1:
            raise ValueError("讨论查询只允许一个 session_id。")
        if not values:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", values[0]):
            raise ValueError("讨论标识无效。")
        return values[0]

    @_workspace_request
    def do_GET(self) -> None:
        if not self._valid_host():
            self._error(421, "Host 必须是当前 127.0.0.1 服务地址。")
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/reports":
                reports = []
                for report_id in self.server.legacy_reports:
                    if report_id in self.server.deletions.deleted_ids():
                        continue
                    loaded = self._report(report_id)
                    reports.append({
                        "id": report_id,
                        "title": loaded["title"],
                        "document_name": loaded["document_name"],
                        "findings_count": len(loaded["report"].get("findings", [])),
                    })
                reports.extend(r for r in self.server.jobs.list_reports()
                               if r["id"] not in self.server.deletions.deleted_ids())
                self._json(200, {"reports": reports})
                return

            if path == "/api/workbench":
                self._json(200, self.server.workbench_catalog())
                return

            if path == "/api/workbench/groups":
                self._json(200, self.server.workbench_groups.get())
                return

            if path == "/api/review-capabilities":
                self._json(200, self.server.jobs.capabilities())
                return

            if path == "/api/desktop-instance":
                instance = getattr(self.server, "desktop_instance", None)
                if not instance or self.headers.get("X-Desktop-Token") != instance["token"]:
                    self._error(404, "资源不存在。")
                    return
                self._json(200, instance)
                return

            if path == "/api/model-presets":
                from model_presets import providers

                self._json(200, {"providers": providers()})
                return

            if path == "/api/model-settings":
                if self.headers.get("Origin") and not self._same_origin():
                    self._error(403, "拒绝跨来源读取本机模型设置。")
                    return
                self._json(200, self.server.model_settings.get())
                return

            if path in {"/api/checklists", "/api/checklists/template.xlsx"}:
                if self.headers.get("Origin") and not self._same_origin():
                    self._error(403, "拒绝跨来源读取本机清单。")
                    return
                try:
                    if path == "/api/checklists":
                        self._json(200, self.server.checklists.list())
                    else:
                        filename = urllib.parse.quote("审核清单模板.xlsx")
                        self._send_bytes(200, make_template(),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            disposition=f"attachment; filename*=UTF-8''{filename}")
                except ChecklistCorrupt as exc:
                    self._error(500, str(exc))
                except (ChecklistError, ValueError) as exc:
                    self._error(400, str(exc))
                return

            job_match = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})", path)
            if job_match:
                try:
                    job = self.server.jobs.get(job_match.group(1))
                except KeyError:
                    self._error(404, "审核任务不存在。")
                    return
                if job.get("report_id") and self.server.jobs._status(job_match.group(1)).get("post_review_information_version") == 1:
                    job["information_gate_status"] = self.server.information_gate.get(self.server.jobs.load_report(job["report_id"]))["status"]
                self._json(200, {"job": job})
                return

            gate_match = re.fullmatch(rf"/api/reports/({REPORT_ID_PATTERN})/information-gate", path)
            if gate_match:
                if self.headers.get("Origin") and not self._same_origin():
                    self._error(403, "拒绝跨来源读取本机合同信息。")
                    return
                loaded = self._raw_report(gate_match.group(1))
                self._json(200, {"information_gate": self.server.information_gate.get(loaded)})
                return

            extra_match = re.fullmatch(rf"/api/reports/({REPORT_ID_PATTERN})/(facts|rereview)", path)
            if extra_match:
                if self.headers.get("Origin") and not self._same_origin():
                    self._error(403, "拒绝跨来源读取本机合同信息。")
                    return
                with self.server.state_lock:
                    loaded = self._report(extra_match.group(1))
                    if extra_match.group(2) == "facts":
                        self._json(200, {"facts": self.server.facts.get(loaded)})
                    else:
                        _, state = self._state_for(loaded)
                        self._json(200, self.server.rereviews.preflight(loaded, state))
                return

            draft_match = re.fullmatch(
                rf"/api/reports/({REPORT_ID_PATTERN})/drafts/([A-Za-z0-9_-]{{1,100}})", path
            )
            if draft_match:
                query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query, keep_blank_values=True
                )
                unknown = set(query) - {"candidate_id"}
                candidates = query.get("candidate_id", [])
                if unknown or len(candidates) > 1:
                    raise ValueError("候选改稿查询参数无效。")
                candidate_id = candidates[0] if candidates else None
                loaded = self._report(draft_match.group(1))
                self._json(200, self.server.drafts.get(loaded, draft_match.group(2), candidate_id))
                return

            revision_match = re.fullmatch(
                rf"/api/reports/({REPORT_ID_PATTERN})/discussions/([A-Za-z0-9_-]{{1,100}})/revisions", path
            )
            if revision_match:
                loaded = self._report(revision_match.group(1))
                self._json(200, self.server.revisions.get(loaded, revision_match.group(2), session_id=self._discussion_session()))
                return

            discussion_match = re.fullmatch(
                rf"/api/reports/({REPORT_ID_PATTERN})/discussions/([A-Za-z0-9_-]{{1,100}})", path
            )
            if discussion_match:
                loaded = self._report(discussion_match.group(1))
                self._json(200, self.server.discussions.get(loaded, discussion_match.group(2), session_id=self._discussion_session()))
                return

            match = re.fullmatch(rf"/api/reports/({REPORT_ID_PATTERN})/delivery", path)
            if match:
                loaded = self._report(match.group(1))
                self._json(200, self.server.delivery.describe(loaded))
                return

            match = re.fullmatch(rf"/api/reports/({REPORT_ID_PATTERN})", path)
            if match:
                loaded = self._report(match.group(1))
                with self.server.state_lock:
                    _, state = self._state_for(loaded)
                action_context = self.server.delivery.action_context(loaded, state, strict=False)
                payload = {key: loaded[key] for key in (
                    "id", "report", "contract", "sources", "paragraphs", "completeness"
                )}
                payload["state"] = _public_state(state)
                payload["actions"] = action_context["actions"]
                parent = loaded["report"].get("metadata", {}).get("rereview", {}).get("parent_report_id")
                if parent:
                    payload["parent_report_deleted"] = parent in self.server.deletions.deleted_ids()
                child = self.server.rereviews.latest_completed_child(loaded["id"], self.server.deletions.deleted_ids())
                if child:
                    payload["next_report_id"] = child
                self._json(200, payload)
                return

            match = re.fullmatch(rf"/api/reports/({REPORT_ID_PATTERN})/export\.docx", path)
            if match:
                loaded = self._report(match.group(1))
                with self.server.state_lock:
                    _, state = self._state_for(loaded)
                action_context = self.server.delivery.action_context(loaded, state, strict=False)
                report = dict(loaded["report"])
                report["completeness"] = loaded["completeness"]
                report["document_name"] = loaded["document_name"]
                report["confirmed_facts"] = self.server.facts.get(loaded)
                source_docx, confirmed_plan = self.server.delivery.report_snapshot(loaded)
                data = export_docx(
                    report,
                    loaded["sources"],
                    loaded["contract"],
                    reviews=state["reviews"],
                    selected_options=state["selected_options"],
                    reader_mode=True,
                    reader_actions=action_context["actions"],
                    source_docx=source_docx,
                    confirmed_plan=confirmed_plan,
                )
                self._send_bytes(
                    200,
                    data,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    disposition="attachment; filename=\"review-report.docx\"; filename*=UTF-8''"
                    + urllib.parse.quote(client_stem(loaded.get("document_name")) + "-审查报告.docx"),
                )
                return

            static = STATIC_FILES.get(path)
            if static:
                name, content_type = static
                target = self.server.resource_root / "reader_ui" / name
                try:
                    data = target.read_bytes()
                except OSError:
                    self._error(404, "页面文件不存在。")
                    return
                self._send_bytes(200, data, content_type)
                return
            self._error(404, "资源不存在。")
        except ArchiveError as exc:
            self._error(500, str(exc))
        except InformationGateRequired:
            raise
        except ContractFactsCorrupt as exc:
            self._error(500, str(exc))
        except ContractFactsError as exc:
            self._error(400, str(exc))
        except StateCorruptError as exc:
            self._error(500, str(exc))
        except ValueError as exc:
            self._error(400, str(exc))
        except DraftPreparationConflict as exc:
            self._error(409, str(exc))
        except DraftPreparationUnavailable as exc:
            self._error(409, str(exc))
        except DraftPreparationCorrupt as exc:
            self._error(500, str(exc))
        except WorkbenchGroupCorrupt as exc:
            self._error(500, str(exc))
        except ModelSettingsCorrupt as exc:
            self._error(500, str(exc))
        except ModelSettingsUnavailable as exc:
            self._error(503, str(exc))
        except ClauseDiscussionConflict as exc:
            extra = {}
            if exc.current_revision is not None:
                extra["current_revision"] = exc.current_revision
            if exc.current_delivery_revision is not None:
                extra["current_delivery_revision"] = exc.current_delivery_revision
            self._error(409, str(exc), **extra)
        except ClauseDiscussionUnavailable as exc:
            self._error(409, str(exc))
        except ClauseDiscussionCorrupt as exc:
            self._error(500, str(exc))
        except OpinionRevisionConflict as exc:
            self._error(409, str(exc))
        except OpinionRevisionUnavailable as exc:
            self._error(409, str(exc))
        except OpinionRevisionCorrupt as exc:
            self._error(500, str(exc))
        except KeyError:
            self._error(404, "报告不存在。")
        except (ReportDataError, JobError):
            self._error(500, "报告数据校验失败，未展示。")
        except InformationGateRequired:
            raise
        except Exception:
            self._error(500, "本地阅读服务处理失败。")

    @_workspace_request
    def do_POST(self) -> None:
        if not self._valid_host():
            self._error(421, "Host 必须是当前 127.0.0.1 服务地址。")
            return
        if not self._same_origin():
            self._error(403, "拒绝跨来源修改本地复核状态。")
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/workbench/delete":
            try:
                self._json(200, self.server.deletions.delete(self._read_body()))
            except DeletionConflict as exc:
                self._error(409, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except KeyError:
                self._error(404, "这项合同不在当前工作台中。")
            except DeletionIncomplete as exc:
                self._error(500, str(exc), pending_deletion=True, item_id=exc.item_id)
            except (DeletionError, WorkbenchGroupCorrupt, ArchiveError) as exc:
                self._error(500, str(exc))
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "永久删除未完成，请刷新后核对并重试。")
            return
        gate_match = re.fullmatch(rf"/api/reports/({REPORT_ID_PATTERN})/information-gate", path)
        if gate_match:
            try:
                body = self._read_body()
                loaded = self._raw_report(gate_match.group(1))
                gate, started = self.server.information_gate.submit(loaded, body)
                self._json(202 if started else 200, {"information_gate": gate, "started": started})
            except InformationGateRequired:
                raise
            except ContractFactsConflict as exc:
                self._error(409, str(exc), current_revision=exc.current_revision)
            except JobConflict as exc:
                self._error(409, str(exc))
            except (ContractFactsCorrupt, JobError) as exc:
                self._error(500, str(exc))
            except (ValueError, ContractFactsError) as exc:
                self._error(400, str(exc))
            except KeyError:
                self._error(404, "审核报告不存在。")
            return
        continuation = re.fullmatch(rf"/api/reports/({REPORT_ID_PATTERN})/(facts/(?:confirm|save|withdraw|answer)|rereview)", path)
        if continuation:
            try:
                body = self._read_body()
                with self.server.state_lock:
                    loaded = self._report(continuation.group(1))
                    if continuation.group(2) == "rereview":
                        _, state = self._state_for(loaded)
                        job, started = self.server.rereviews.start(loaded, state, body)
                        self._json(202 if started else 200, {"job": job, "started": started})
                    elif continuation.group(2) != "facts/confirm":
                        operation = continuation.group(2).split("/")[1]
                        self._json(200, {"facts": getattr(self.server.facts, operation)(loaded, body)})
                    else:
                        discussion = self.server.discussions.get(loaded, body.get("finding_id"), body.get("session_id"))
                        turns = discussion.get("turns", [])
                        turn = next((t for t in turns if t.get("id") == body.get("turn_id") and t.get("status") == "completed"), None)
                        candidate = next((c for c in (turn or {}).get("fact_candidates", [])
                                          if c.get("id") == body.get("candidate_id")), None)
                        if candidate is None:
                            raise ValueError("这条事实候选不在已完成的讨论中，请刷新后重试。")
                        payload = {key: body[key] for key in ("expected_revision", "candidate_id", "replace_id") if key in body}
                        self._json(200, {"facts": self.server.facts.confirm(loaded, payload, candidate)})
            except InformationGateRequired:
                raise
            except ContractFactsConflict as exc:
                self._error(409, str(exc), current_revision=exc.current_revision)
            except (ContractFactsCorrupt, ClauseDiscussionCorrupt, StateCorruptError) as exc:
                self._error(500, str(exc))
            except (JobConflict, DeliveryUnavailable, DeliveryStateCorrupt, ClauseDiscussionConflict) as exc:
                self._error(409, str(exc))
            except (ValueError, ContractFactsError) as exc:
                self._error(400, str(exc))
            except KeyError:
                self._error(404, "报告或讨论不存在。")
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "本次操作未完成，请刷新任务状态后重试。")
            return
        if path in {"/api/checklists/save", "/api/checklists/delete", "/api/checklists/import"}:
            try:
                body = self._read_body(MAX_CHECKLIST_BODY_BYTES)
                if path.endswith("/save"):
                    self._json(200, self.server.checklists.save(body))
                elif path.endswith("/delete"):
                    self._json(200, self.server.checklists.delete(body.get("id"), body.get("expected_revision")))
                else:
                    filename = body.get("filename")
                    encoded = body.get("xlsx_base64")
                    if not isinstance(filename, str) or not filename.lower().endswith(".xlsx"):
                        raise ValueError("请选择xlsx格式的审核清单。")
                    if not isinstance(encoded, str):
                        raise ValueError("缺少Excel文件内容。")
                    try:
                        data = base64.b64decode(encoded, validate=True)
                    except (ValueError, binascii.Error):
                        raise ValueError("Excel文件内容编码无效。") from None
                    result = parse_workbook(data)
                    if not result.get("ok"):
                        issues = result.get("errors") or []
                        result.setdefault("error", issues[0].get("message", "Excel清单无法导入。") if issues else "Excel清单无法导入。")
                    self._json(200 if result.get("ok") else 400, result)
            except ChecklistConflict as exc:
                self._error(409, str(exc), current_revision=exc.current_revision)
            except ChecklistCorrupt as exc:
                self._error(500, str(exc))
            except KeyError:
                self._error(404, "清单不存在。")
            except (ChecklistError, ValueError) as exc:
                self._error(400, str(exc))
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "清单操作未完成，原清单没有被替换。")
            return
        if path == "/api/model-settings":
            try:
                self._json(200, self.server.model_settings.update(self._read_body()))
            except ModelSettingsConflict as exc:
                self._error(409, str(exc), current_revision=exc.current_revision)
            except ValueError as exc:
                self._error(400, str(exc))
            except ModelSettingsCorrupt as exc:
                self._error(500, str(exc))
            except ModelSettingsUnavailable as exc:
                self._error(503, str(exc))
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "模型设置处理失败，原设置未更改。")
            return
        if path == "/api/models":
            try:
                body = self._read_body()
                config = body.get("config")
                self._json(200, {"models": list_models(config)})
            except ValueError as exc:
                self._error(400, str(exc))
            except ModelClientError as exc:
                self._error(502, str(exc))
            except InformationGateRequired:
                raise
            except Exception:
                self._error(502, "模型列表获取失败；可手填模型 ID。")
            return
        if path == "/api/workbench/groups":
            try:
                body = self._read_body()
                self._json(200, self.server.workbench_groups.update(body))
            except WorkbenchGroupConflict as exc:
                self._error(409, str(exc), current_revision=exc.current_revision)
            except ValueError as exc:
                self._error(400, str(exc))
            except WorkbenchGroupCorrupt as exc:
                self._error(500, str(exc))
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "合同分组保存失败。")
            return
        revision_match = re.fullmatch(
            rf"/api/reports/({REPORT_ID_PATTERN})/discussions/([A-Za-z0-9_-]{{1,100}})/revisions(?:/(accept))?", path
        )
        if revision_match:
            try:
                body = self._read_body()
                loaded = self._report(revision_match.group(1))
                if revision_match.group(3) == "accept":
                    result = self.server.revisions.accept(loaded, revision_match.group(2), body)
                    self._json(200, result)
                else:
                    result, started = self.server.revisions.start(loaded, revision_match.group(2), body)
                    self._json(202 if started or result.get("pending_candidate_id") else 200, result)
            except (OpinionRevisionConflict, ClauseDiscussionConflict) as exc:
                extra = {name: getattr(exc, name) for name in (
                    "current_revision", "current_opinion_revision", "current_delivery_revision"
                ) if getattr(exc, name, None) is not None}
                self._error(409, str(exc), **extra)
            except (OpinionRevisionUnavailable, ClauseDiscussionUnavailable) as exc:
                self._error(409, str(exc))
            except (OpinionRevisionCorrupt, ClauseDiscussionCorrupt) as exc:
                self._error(500, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except (KeyError, ReportDataError, JobError):
                self._error(404, "报告或审查意见不存在。")
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "新版意见操作未完成，原意见未被覆盖。")
            return
        session_match = re.fullmatch(
            rf"/api/reports/({REPORT_ID_PATTERN})/discussions/([A-Za-z0-9_-]{{1,100}})/sessions", path
        )
        if session_match:
            try:
                body = self._read_body()
                loaded = self._report(session_match.group(1))
                result, created = self.server.discussions.new_session(loaded, session_match.group(2), body)
                self._json(201 if created else 200, result)
            except ClauseDiscussionConflict as exc:
                extra = {name: getattr(exc, name) for name in (
                    "current_revision", "current_sessions_revision", "current_delivery_revision"
                ) if getattr(exc, name, None) is not None}
                self._error(409, str(exc), **extra)
            except ClauseDiscussionUnavailable as exc:
                self._error(409, str(exc))
            except ClauseDiscussionCorrupt as exc:
                self._error(500, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except (KeyError, ReportDataError, JobError):
                self._error(404, "报告或审查意见不存在。")
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "新讨论未能创建，原记录保留。")
            return
        stop_match = re.fullmatch(
            rf"/api/reports/({REPORT_ID_PATTERN})/discussions/([A-Za-z0-9_-]{{1,100}})/stop", path
        )
        if stop_match:
            try:
                body = self._read_body()
                loaded = self._report(stop_match.group(1))
                result, _ = self.server.discussions.stop(loaded, stop_match.group(2), body)
                self._json(200, result)
            except ClauseDiscussionConflict as exc:
                extra = {"current_revision": exc.current_revision} if exc.current_revision is not None else {}
                self._error(409, str(exc), **extra)
            except ClauseDiscussionUnavailable as exc:
                self._error(409, str(exc))
            except ClauseDiscussionCorrupt as exc:
                self._error(500, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except (KeyError, ReportDataError, JobError):
                self._error(404, "报告或审查意见不存在。")
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "停止未成功，回答可能仍在生成。")
            return
        discussion_match = re.fullmatch(
            rf"/api/reports/({REPORT_ID_PATTERN})/discussions/([A-Za-z0-9_-]{{1,100}})", path
        )
        if discussion_match:
            try:
                body = self._read_body()
                loaded = self._report(discussion_match.group(1))
                result, started = self.server.discussions.start(loaded, discussion_match.group(2), body)
                self._json(202 if started or result["pending_turn_id"] else 200, result)
            except ClauseDiscussionConflict as exc:
                extra = {}
                if exc.current_revision is not None:
                    extra["current_revision"] = exc.current_revision
                if exc.current_delivery_revision is not None:
                    extra["current_delivery_revision"] = exc.current_delivery_revision
                self._error(409, str(exc), **extra)
            except ClauseDiscussionUnavailable as exc:
                self._error(409, str(exc))
            except ClauseDiscussionCorrupt as exc:
                self._error(500, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except (KeyError, ReportDataError, JobError):
                self._error(404, "报告或审查意见不存在。")
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "条款讨论未能启动。")
            return
        if path == "/api/jobs":
            try:
                body = self._read_body(MAX_UPLOAD_ENVELOPE_BYTES)
                self._json(201, self.server.jobs.create(body))
            except ValueError as exc:
                self._error(400, str(exc))
            except JobError as exc:
                self._error(500, str(exc))
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "创建审核任务失败。")
            return

        job_prepare = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/prepare", path)
        if job_prepare:
            try:
                job, started = self.server.jobs.prepare(job_prepare.group(1), self._read_body())
                self._json(202 if started or job["status"] == "preparing" else 200, {"job": job})
            except KeyError:
                self._error(404, "审核任务不存在。")
            except JobConflict as exc:
                self._error(409, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except JobError as exc:
                self._error(500, str(exc))
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "审前信息核对未能启动。")
            return

        job_answers = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/answers", path)
        if job_answers:
            try:
                job = self.server.jobs.answer_preparation(job_answers.group(1), self._read_body())
                self._json(200, {"job": job})
            except KeyError:
                self._error(404, "审核任务不存在。")
            except JobConflict as exc:
                self._error(409, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except JobError as exc:
                self._error(500, str(exc))
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "补充信息未能保存。")
            return

        job_start = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/start", path)
        if job_start:
            try:
                body = self._read_body()
                job, started = self.server.jobs.start(job_start.group(1), body)
                self._json(202 if started or job["status"] == "running" else 200, {"job": job})
            except KeyError:
                self._error(404, "审核任务不存在。")
            except JobConflict as exc:
                self._json(409, {"error": str(exc), "code": "review_job_conflict",
                                 "job": self.server.jobs.get(job_start.group(1))})
            except ValueError as exc:
                self._error(400, str(exc))
            except JobError as exc:
                self._error(500, str(exc))
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "启动审核任务失败。")
            return


        draft_match = re.fullmatch(
            rf"/api/reports/({REPORT_ID_PATTERN})/drafts/([A-Za-z0-9_-]{{1,100}})", path
        )
        if draft_match:
            try:
                body = self._read_body()
                loaded = self._report(draft_match.group(1))
                result, started = self.server.drafts.start(loaded, draft_match.group(2), body)
                self._json(202 if started or result["status"] == "running" else 200, result)
            except (DraftPreparationInput, ValueError) as exc:
                self._error(400, str(exc))
            except DraftPreparationConflict as exc:
                self._error(409, str(exc))
            except DraftPreparationUnavailable as exc:
                self._error(409, str(exc))
            except DraftPreparationCorrupt as exc:
                self._error(500, str(exc))
            except (KeyError, ReportDataError, JobError):
                self._error(404, "报告或审查意见不存在。")
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "本地候选改稿整理失败。")
            return

        action_match = re.fullmatch(rf"/api/reports/({REPORT_ID_PATTERN})/actions", path)
        if action_match:
            try:
                body = self._read_body()
                action = body.get("action")
                finding_id = body.get("finding_id")
                review_revision = body.get("expected_review_revision")
                delivery_revision = body.get("expected_delivery_revision")
                if action not in {"ignore", "restore"}:
                    raise ValueError("action 只能是 ignore 或 restore。")
                if not isinstance(finding_id, str):
                    raise ValueError("finding_id 必须是字符串。")
                if not isinstance(review_revision, int) or isinstance(review_revision, bool) or review_revision < 0:
                    raise ValueError("expected_review_revision 必须是非负整数。")
                loaded = self._report(action_match.group(1))
                report = loaded["report"]
                if finding_id not in {item["id"] for item in report.get("findings", [])}:
                    raise ValueError("finding_id 不属于本报告。")
                state_path = _state_path(self.server.state_dir, loaded["id"], loaded["request_digest"])
                with self.server.state_lock, self.server.delivery.lock:
                    current = _load_state(state_path, report)
                    if current["revision"] != review_revision:
                        self._error(
                            409,
                            "人工复核状态已变化，请刷新后重试。",
                            current_review_revision=current["revision"],
                        )
                        return
                    if self.server.delivery.has_configured_source(loaded["id"]):
                        if not isinstance(delivery_revision, int) or isinstance(delivery_revision, bool) or delivery_revision < 0:
                            raise ValueError("有原稿 DOCX 时 expected_delivery_revision 必须是非负整数。")
                        context = self.server.delivery.action_context(loaded, current, strict=True)
                        if context["delivery_revision"] != delivery_revision:
                            self._error(
                                409,
                                "文档修改状态已变化，请刷新后重试。",
                                current_delivery_revision=context["delivery_revision"],
                            )
                            return
                    else:
                        if delivery_revision is not None:
                            raise ValueError("无原稿 DOCX 时 expected_delivery_revision 必须是 null。")
                        context = self.server.delivery.action_context(loaded, current, strict=False)
                    current_action = context["actions"][finding_id]
                    if current_action["modified"] or current_action["commented"]:
                        self._error(409, "本条已有确认的正文修改或批注，请先撤销实际文档操作。")
                        return

                    reviews = {key: dict(value) for key, value in current["reviews"].items()}
                    if action == "ignore":
                        reviews[finding_id]["decision"] = "驳回"
                        reviews[finding_id]["note"] = IGNORE_NOTE
                    else:
                        reviews[finding_id] = _default_reviews(report)[finding_id]
                    new_state = {
                        "revision": current["revision"] + 1,
                        "reviews": reviews,
                        "selected_options": current["selected_options"],
                        "history": [*current["history"], _state_snapshot(current)],
                    }
                    _atomic_write(state_path, new_state)
                    actions = self.server.delivery.action_context(loaded, new_state, strict=False)["actions"]
                self._json(200, {"review_state": _public_state(new_state), "actions": actions})
            except (DeliveryUnavailable, DeliveryStateCorrupt) as exc:
                self._error(409, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except StateCorruptError as exc:
                self._error(500, str(exc))
            except (KeyError, ReportDataError):
                self._error(500, "报告数据校验失败，未记录操作。")
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "本地操作记录失败。")
            return
        delivery_match = re.fullmatch(
            rf"/api/reports/({REPORT_ID_PATTERN})/delivery(?:/(undo|export))?", path
        )
        if delivery_match:
            try:
                body = self._read_body()
                expected = body.get("expected_revision")
                source_sha = body.get("source_sha256")
                if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                    raise ValueError("expected_revision 必须是非负整数。")
                if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha):
                    raise ValueError("source_sha256 必须是64位小写十六进制摘要。")
                loaded = self._report(delivery_match.group(1))
                action = delivery_match.group(2)
                if action == "undo":
                    state = self.server.delivery.undo(loaded, body)
                    self._json(200, {"state": state})
                    return
                if action == "export":
                    review_revision = body.get("review_revision")
                    if not isinstance(review_revision, int) or isinstance(review_revision, bool) or review_revision < 0:
                        raise ValueError("review_revision 必须是非负整数。")
                    with self.server.state_lock:
                        _, review_state = self._state_for(loaded)
                    if review_state["revision"] != review_revision:
                        self._error(
                            409,
                            "人工复核状态已变化，请刷新后重新导出。",
                            current_review_revision=review_state["revision"],
                        )
                        return
                    action_context = self.server.delivery.action_context(loaded, review_state, strict=True)
                    loaded = {**loaded, "report": {**loaded["report"], "confirmed_facts": self.server.facts.get(loaded)}}
                    data = self.server.delivery.export_zip(
                        loaded,
                        body,
                        review_state,
                        reader_actions=action_context["actions"],
                    )
                    self._send_bytes(
                        200,
                        data,
                        "application/zip",
                        disposition="attachment; filename=\"delivery.zip\"; filename*=UTF-8''"
                        + urllib.parse.quote(client_zip_name(loaded.get("document_name"))),
                    )
                    return
                state = self.server.delivery.save(loaded, body)
                self._json(200, {"state": state})
                return
            except DeliveryConflict as exc:
                self._error(409, str(exc), current_revision=exc.current_revision)
            except DeliveryUnavailable as exc:
                self._error(409, str(exc))
            except DeliveryStateCorrupt as exc:
                self._error(500, str(exc))
            except (DeliveryValidationError, ValueError) as exc:
                self._error(400, str(exc))
            except (KeyError, ReportDataError):
                self._error(500, "报告数据校验失败，未处理文档修改。")
            except InformationGateRequired:
                raise
            except Exception:
                self._error(500, "文档修改处理失败。")
            return
        match = re.fullmatch(rf"/api/reports/({REPORT_ID_PATTERN})/(state|undo)", path)
        if not match:
            self._error(404, "资源不存在。")
            return
        try:
            body = self._read_body()
            expected = body.get("expected_revision")
            if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                raise ValueError("expected_revision 必须是非负整数。")
            loaded = self._report(match.group(1))
            report = loaded["report"]
            state_path = _state_path(self.server.state_dir, loaded["id"], loaded["request_digest"])
            with self.server.state_lock:
                current = _load_state(state_path, report)
                if current["revision"] != expected:
                    self._error(
                        409,
                        "复核状态已被其他操作更新，请刷新后重试。",
                        current_revision=current["revision"],
                    )
                    return
                if match.group(2) == "state":
                    findings = {item["id"]: item for item in report.get("findings", [])}
                    option_ids = {item["id"] for item in report.get("options", [])}
                    reviews = _validate_reviews(body.get("reviews"), findings)
                    selected = _validate_options(body.get("selected_options"), option_ids)
                    snapshot = _state_snapshot(current)
                    new_state = {
                        "revision": current["revision"] + 1,
                        "reviews": reviews,
                        "selected_options": selected,
                        "history": [*current["history"], snapshot],
                    }
                else:
                    if not current["history"]:
                        self._error(409, "没有可撤回的上一步复核状态。", current_revision=current["revision"])
                        return
                    previous = current["history"][-1]
                    new_state = {
                        "revision": current["revision"] + 1,
                        "reviews": previous["reviews"],
                        "selected_options": previous["selected_options"],
                        "history": current["history"][:-1],
                    }
                _atomic_write(state_path, new_state)
            self._json(200, {"state": _public_state(new_state)})
        except StateCorruptError as exc:
            self._error(500, str(exc))
        except ValueError as exc:
            self._error(400, str(exc))
        except (KeyError, ReportDataError):
            self._error(500, "报告数据校验失败，未保存。")
        except InformationGateRequired:
            raise
        except Exception:
            self._error(500, "本地阅读服务处理失败，未保存。")


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    root: str | os.PathLike[str] | None = None,
    resource_root: str | os.PathLike[str] | None = None,
    include_legacy_reports: bool = True,
    state_dir: str | os.PathLike[str] | None = None,
    source_paths: dict[str, str | os.PathLike[str] | None] | None = None,
    review_runner=None,
    review_engines: list[dict[str, Any]] | None = None,
    jobs_dir: str | os.PathLike[str] | None = None,
    discussion_completion=None,
    search_completion=None,
    revision_completion=None,
    draft_runner=None,
    draft_completion=None,
) -> ReaderServer:
    if host != "127.0.0.1":
        raise ValueError("阅读服务只允许绑定 127.0.0.1。")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("端口无效。")
    project_root = Path(root).resolve() if root is not None else ROOT
    resources = Path(resource_root).resolve() if resource_root is not None else project_root
    states = Path(state_dir).resolve() if state_dir is not None else project_root / "runtime" / "reader-state"
    defaults: dict[str, Path | None] = {
        "detailed": None,
        "minimal": None,
        "table": None,
    }
    if source_paths is not None:
        unknown = set(source_paths) - set(REPORTS)
        if unknown:
            raise ValueError("source_paths 含未知报告 id。")
        for key, value in source_paths.items():
            defaults[key] = Path(value).resolve() if value is not None else None
    if review_runner is None:
        try:
            from review_runner import capabilities as runner_capabilities, run_review

            review_runner = run_review
            if review_engines is None:
                raw_capabilities = runner_capabilities()
                review_engines = (
                    raw_capabilities.get("engines")
                    if isinstance(raw_capabilities, dict)
                    else raw_capabilities
                )
        except (ImportError, AttributeError, TypeError, OSError, RuntimeError, ValueError):
            review_runner = None
    jobs_root = Path(jobs_dir).resolve() if jobs_dir is not None else project_root / "runtime" / "review-jobs"
    jobs = ReviewJobManager(
        jobs_root,
        None,
        review_runner,
        review_engines,
        prompt_path=resources / "prompts" / "initial_review.md",
    )
    return ReaderServer(
        (host, port), project_root, states, defaults, jobs, draft_runner, discussion_completion, search_completion,
        revision_completion, draft_completion,
        resources if resource_root is not None else None,
        include_legacy_reports,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="劳动合同审查阅读面（仅本机）")
    parser.add_argument("--port", type=int, default=8519)
    args = parser.parse_args()
    server = create_server(port=args.port)
    try:
        print(f"Reader: http://127.0.0.1:{server.server_port}")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
