"""Persistent, source-bound DOCX delivery state for the loopback reader."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any, Mapping

from contract_core import parse_docx
from delivery_bundle import FILENAMES, build_bundle
from docx_delivery import ValidationError, inspect_source, validate_plan


# Every report request re-checks the source against the report fingerprint; parsing the whole DOCX for
# that took ~25 ms per call and a single page load makes several. The fingerprint depends only on the bytes.
_FINGERPRINTS: dict[str, str] = {}
_FINGERPRINT_LOCK = threading.Lock()


def _parsed_fingerprint(data: bytes) -> str:
    key = hashlib.sha256(data).hexdigest()
    with _FINGERPRINT_LOCK:
        cached = _FINGERPRINTS.get(key)
    if cached is None:
        cached = parse_docx(data).fingerprint
        with _FINGERPRINT_LOCK:
            _FINGERPRINTS[key] = cached
            while len(_FINGERPRINTS) > 32:
                _FINGERPRINTS.pop(next(iter(_FINGERPRINTS)))
    return cached


IGNORE_NOTE = "操作记录：不采用本条修改建议；不表示风险已解决，未填写复核理由。"

# Client-facing names carry the contract's own name; the numbered FILENAMES stay
# for the engineering CLI bundle, which is not handed to anyone.
CLIENT_LABELS = {
    "clean": "洁净版",
    "tracked": "修订版",
    "comments": "原稿批注版",
    "tracked_comments": "修订与批注版",
    "report": "审查报告",
}


def client_stem(document_name: str | None) -> str:
    """A Windows-safe base name taken from the uploaded contract's file name."""
    name = Path(str(document_name or "")).name
    stem = name[:-5] if name.lower().endswith(".docx") else Path(name).stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", stem).strip().rstrip(". ")
    return stem[:60].rstrip(". ") or "合同"


def client_zip_name(document_name: str | None) -> str:
    return client_stem(document_name) + "-交付文件.zip"


class DeliveryUnavailable(RuntimeError):
    pass


class DeliveryStateCorrupt(RuntimeError):
    pass


class DeliveryConflict(RuntimeError):
    def __init__(self, current_revision: int, message: str = "文档修改状态已被其他操作更新，请刷新后重试。"):
        super().__init__(message)
        self.current_revision = current_revision


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: Any) -> str:
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _edit_signature(edit: Mapping[str, Any]) -> dict[str, Any]:
    """Compare actual changes, including the UI's unchanged-edge trimming."""
    kind = edit.get("kind", "replace")
    old, new = edit["old_text"], edit["new_text"]
    start, end = edit["start"], edit["end"]
    if kind == "replace":
        prefix = 0
        while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
            prefix += 1
        suffix = 0
        while suffix < min(len(old), len(new)) - prefix and old[-1-suffix] == new[-1-suffix]:
            suffix += 1
        start += prefix
        end -= suffix
        old = old[prefix:len(old)-suffix if suffix else None]
        new = new[prefix:len(new)-suffix if suffix else None]
    return {"kind": kind, "piece_id": edit["piece_id"],
            "paragraph_sha256": edit["paragraph_sha256"], "start": start, "end": end,
            "old_text": old, "new_text": new}


def _edit_binding(edits: list[dict[str, Any]]) -> str:
    signatures = [_edit_signature(edit) for edit in edits]
    return _digest(sorted(signatures, key=lambda value: json.dumps(value, sort_keys=True)))


def _snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "revision": state["revision"],
        "author": state["author"],
        "created_at": state.get("created_at"),
        "edits": state["edits"],
        "comments": state["comments"],
        "draft_confirmations": state.get("draft_confirmations", {}),
    }


def _undo_action(state: Mapping[str, Any]) -> dict[str, Any] | None:
    history = state.get("history")
    if not history:
        return None
    previous = history[-1]
    current_edits = {item["id"]: item for item in state["edits"]}
    previous_edits = {item["id"]: item for item in previous["edits"]}
    current_comments = {item["id"]: item for item in state["comments"]}
    previous_comments = {item["id"]: item for item in previous["comments"]}
    edit_ids = sorted(
        key for key in set(current_edits) | set(previous_edits)
        if current_edits.get(key) != previous_edits.get(key)
    )
    comment_ids = sorted(
        key for key in set(current_comments) | set(previous_comments)
        if current_comments.get(key) != previous_comments.get(key)
    )
    author_changed = state["author"] != previous["author"]
    if len(edit_ids) == 1 and not comment_ids and not author_changed:
        edit = current_edits.get(edit_ids[0]) or previous_edits[edit_ids[0]]
        return {"kind": "edit", "edit_id": edit_ids[0], "piece_id": edit["piece_id"], "label": "撤回这处正文修改"}
    if len(comment_ids) == 1 and not edit_ids and not author_changed:
        comment = current_comments.get(comment_ids[0]) or previous_comments[comment_ids[0]]
        return {"kind": "comment", "comment_id": comment_ids[0], "text": comment["text"][:32], "label": "撤回这条协作批注"}
    count = len(edit_ids) + len(comment_ids) + int(author_changed)
    changes = ([f"{len(edit_ids)}处改稿"] if edit_ids else []) + ([f"{len(comment_ids)}条批注"] if comment_ids else [])
    if author_changed:
        changes.append("署名设置")
    return {"kind": "multiple", "label": "撤回上次保存的" + "、".join(changes) if count else "撤回上一次保存"}


def _public_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_snapshot(state),
        "draft_confirmations": {
            key: {field: value[field] for field in (
                "candidate_id", "status", "paragraph_actions", "suggestion_actions", "coverage_complete", "opinion_revision"
            ) if field in value}
            for key, value in state.get("draft_confirmations", {}).items()
        },
        "can_undo": bool(state.get("history")),
        "undo_action": _undo_action(state),
    }


class DeliveryManager:
    def __init__(self, state_root: Path, source_paths: Mapping[str, Path | None]):
        self.root = Path(state_root) / "delivery"
        self.source_paths = {key: (Path(value).resolve() if value is not None else None) for key, value in source_paths.items()}
        self.lock = threading.RLock()
        # Injected by the server after the drafts manager exists. The resolver
        # must not call describe(), acquire delivery through drafts, or write.
        self.draft_candidate_resolver = None
        self.draft_plan_candidate_resolver = None
        self.draft_facts_provider = None

    @staticmethod
    def _scope_edits(edits: list[dict[str, Any]], finding_id: str, piece_ids: list[str]) -> list[dict[str, Any]]:
        return [edit for edit in edits if edit["piece_id"] in piece_ids
                or finding_id in edit.get("finding_ids", [])
                or edit["id"].startswith(f"edit-{finding_id}-")]

    def _valid_confirmations(self, values: Any, loaded: Mapping[str, Any],
                             edits: list[dict[str, Any]], source_sha: str) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("draft_confirmations")
        findings = {item["id"]: item for item in loaded["report"].get("findings", [])}
        valid = {}
        for finding_id, value in values.items():
            if (not isinstance(value, dict) or not isinstance(value.get("scope_piece_ids"), list)
                    or value.get("status") not in {"confirmed", "changed"}
                    or not isinstance(value.get("candidate_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value["candidate_id"])
                    or not isinstance(value.get("paragraph_actions"), list)
                    or not isinstance(value.get("suggestion_actions"), list)
                    or not isinstance(value.get("coverage_complete"), bool)):
                raise ValueError("draft_confirmation")
            if (value.get("source_sha256") != source_sha or finding_id not in findings
                    or value.get("finding_sha256") != _digest(findings[finding_id])):
                continue
            if "facts_digest" in value and self.draft_facts_provider:
                from contract_facts import facts_digest
                facts = self.draft_facts_provider(loaded)
                if value.get("facts_revision") != facts["revision"] or value["facts_digest"] != facts_digest(facts):
                    continue
            relevant = self._scope_edits(edits, finding_id, value["scope_piece_ids"])
            if relevant and value.get("edit_binding") == _edit_binding(relevant):
                valid[finding_id] = value
        return valid

    def _resolve_candidate(self, loaded: Mapping[str, Any], finding_id: str,
                           candidate_id: str | None, source_sha: str, source_snapshot=None) -> Mapping[str, Any]:
        if self.draft_candidate_resolver is None:
            raise ValidationError("暂时无法确认改稿，请稍后重试。")
        try:
            finding = next((f for f in loaded["report"]["findings"] if f["id"] == finding_id), {})
            if ("draft_plan" in finding or finding.get("draft_plan_version") == 1
                    or loaded.get("report", {}).get("context", {}).get("draft_plan_version") == 1):
                if self.draft_plan_candidate_resolver is None or source_snapshot is None:
                    raise ValidationError("暂时无法核对这份改稿对应的文档版本。")
                candidate = self.draft_plan_candidate_resolver(
                    loaded, finding_id, candidate_id, *source_snapshot)
            else:
                candidate = self.draft_candidate_resolver(loaded, finding_id, candidate_id, source_sha)
        except Exception as exc:
            raise ValidationError(str(exc) or "候选改稿无法对应当前原稿及意见，请重新查看。") from exc
        if not isinstance(candidate, dict) or candidate.get("status") != "ready":
            raise ValidationError("这份改稿还没有完成，暂时不能确认。")
        return candidate

    def _make_confirmation(self, loaded: Mapping[str, Any], finding_id: str,
                           candidate: Mapping[str, Any], edits: list[dict[str, Any]],
                           source_sha: str, *, exact_only: bool = False) -> dict[str, Any]:
        finding = next((item for item in loaded["report"].get("findings", []) if item["id"] == finding_id), None)
        if finding is None:
            raise ValidationError("这份改稿对应的意见不在当前报告中。")
        rows = [*candidate.get("edits", []), *candidate.get("insertions", [])]
        actions = candidate.get("paragraph_actions", [])
        piece_ids = sorted({row["piece_id"] for row in rows} | {row["piece_id"] for row in actions})
        relevant = self._scope_edits(edits, finding_id, piece_ids)
        exact = bool(rows) and _edit_binding(rows) == _edit_binding(relevant)
        if not exact:
            # A manually edited preview may differ in words, but must still be
            # the same set of bound candidate paragraphs/insertion anchors.
            remaining = list(rows)
            for edit in relevant:
                matches = [row for row in remaining if row["piece_id"] == edit["piece_id"]
                           and row.get("kind", "replace") == edit.get("kind", "replace")
                           and row["paragraph_sha256"] == edit["paragraph_sha256"]
                           and row["start"] <= edit["start"] <= edit["end"] <= row["end"]]
                if len(matches) != 1:
                    raise ValidationError("确认的修改范围与所选候选改稿不一致。")
                remaining.remove(matches[0])
            if exact_only or remaining or not relevant:
                raise ValidationError("确认的修改与所选候选改稿不一致。")
        paragraphs = json.loads(json.dumps(actions, ensure_ascii=False))
        suggestions = json.loads(json.dumps(candidate.get("suggestion_actions", []), ensure_ascii=False))
        if not exact:
            for action in paragraphs:
                changed = any(edit["piece_id"] == action["piece_id"] for edit in relevant)
                action.update(action="modify" if changed else "unexplained", reason=(
                    "这处改稿已手动调整，请重新核对原处理说明。" if changed else
                    "改稿已手动调整，请重新核对这处原文的保留理由。"))
            for action in suggestions:
                action.update(action="unexplained", reason="改稿已手动调整，请重新核对这条建议是否已落实。")
        return {"candidate_id": candidate["candidate_id"], "status": "confirmed" if exact else "changed",
                "paragraph_actions": paragraphs, "suggestion_actions": suggestions,
                "coverage_complete": exact and bool(candidate.get("coverage_complete")),
                "source_sha256": source_sha, "finding_sha256": _digest(finding),
                "scope_piece_ids": piece_ids, "edit_binding": _edit_binding(relevant),
                **{key: candidate[key] for key in ("facts_revision", "facts_digest", "opinion_revision") if key in candidate}}

    def _recover_confirmations(self, loaded: Mapping[str, Any], edits: list[dict[str, Any]],
                               source_sha: str) -> dict[str, Any]:
        """Read-only old-state projection: never infer retain from missing edits."""
        if not edits or self.draft_candidate_resolver is None:
            return {}
        recovered = {}
        for finding in loaded["report"].get("findings", []):
            finding_id = finding["id"]
            if not any(finding_id in edit.get("finding_ids", [])
                       or edit["id"].startswith(f"edit-{finding_id}-") for edit in edits):
                continue
            try:
                candidate = self._resolve_candidate(loaded, finding_id, None, source_sha)
                # The legacy candidate must also match the finding's original
                # drafting text, not a later manually entered draft.
                if candidate.get("draft_sha256") != _sha256(finding.get("clause_draft", "").encode("utf-8")):
                    continue
                recovered[finding_id] = self._make_confirmation(
                    loaded, finding_id, candidate, edits, source_sha, exact_only=True)
            except ValidationError:
                continue
        return recovered

    def register_source(self, report_id: str, path: Path) -> None:
        """Register a server-resolved dynamic source; HTTP never supplies it."""
        if not re.fullmatch(r"job-[0-9a-f]{32}", report_id):
            raise ValueError("报告编号有误，请重新打开报告。")
        with self.lock:
            resolved = Path(path).resolve()
            existing = self.source_paths.get(report_id)
            if existing is not None and existing != resolved:
                raise DeliveryUnavailable("这份报告对应了不同的原稿文件，暂时无法继续。")
            self.source_paths[report_id] = resolved

    def _prefix(self, loaded: Mapping[str, Any]) -> str:
        return f"{loaded['id']}-{loaded['request_digest']}"

    def _binding_path(self, loaded: Mapping[str, Any]) -> Path:
        return self.root / f"{self._prefix(loaded)}-source.json"

    def _state_path(self, loaded: Mapping[str, Any]) -> Path:
        return self.root / f"{self._prefix(loaded)}-state.json"

    def _source_reason(self, report_id: str) -> str:
        if report_id == "minimal":
            return "本报告来自纯文本，没有可保留原格式的原稿 DOCX；旧审查报告仍可继续使用。"
        return "未找到该报告对应的固定原稿 DOCX；旧审查报告仍可继续使用。"

    def _inspect_current(self, loaded: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        path = self.source_paths.get(loaded["id"])
        if path is None or not path.is_file():
            raise DeliveryUnavailable(self._source_reason(loaded["id"]))
        try:
            data = path.read_bytes()
            fingerprint = _parsed_fingerprint(data)
            inspected = inspect_source(data)
        except (OSError, ValueError) as exc:
            raise DeliveryUnavailable("暂时无法读取 Word 原稿，审查报告仍可查看。") from exc
        expected = loaded["contract"].get("fingerprint")
        if fingerprint != expected or loaded["report"].get("fingerprint") != expected:
            raise DeliveryUnavailable("Word 原稿与这份报告中的合同文字不一致，请核对所选文件。")
        return data, inspected

    def _binding(self, loaded: Mapping[str, Any], inspected: Mapping[str, Any]) -> dict[str, Any]:
        path = self._binding_path(loaded)
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DeliveryUnavailable("原稿对应记录损坏，暂时无法继续修改或导出。") from exc
            required = {
                "schema_version", "report_id", "request_digest", "contract_fingerprint",
                "source_sha256", "parser_version", "pieces",
            }
            if not isinstance(value, dict) or set(value) != required:
                raise DeliveryUnavailable("原稿对应记录格式不正确，暂时无法继续修改或导出。")
            if (
                value["report_id"] != loaded["id"]
                or value["request_digest"] != loaded["request_digest"]
                or value["contract_fingerprint"] != loaded["contract"].get("fingerprint")
            ):
                raise DeliveryUnavailable("保存的原稿对应记录与这份报告不一致。")
            if value["source_sha256"] != inspected["source_sha256"]:
                raise DeliveryUnavailable("原稿文件已变化，请先核对文件，再修改或导出。")
            return value
        value = {
            "schema_version": 1,
            "report_id": loaded["id"],
            "request_digest": loaded["request_digest"],
            "contract_fingerprint": loaded["contract"].get("fingerprint"),
            "source_sha256": inspected["source_sha256"],
            "parser_version": inspected["parser_version"],
            "pieces": inspected["pieces"],
        }
        _atomic_json(path, value)
        return value

    def _plan(
        self,
        source_sha256: str,
        revision: int,
        author: str,
        created_at: str,
        edits: Any,
        comments: Any,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source_sha256": source_sha256,
            "revision": max(1, revision),
            "author": author or "未填写确认人（空修改快照）",
            "created_at": created_at,
            "edits": edits,
            "comments": comments,
        }

    def _validate_relations(self, plan: Mapping[str, Any], loaded: Mapping[str, Any]) -> None:
        finding_ids = {item["id"] for item in loaded["report"].get("findings", [])}
        option_ids = {item["id"] for item in loaded["report"].get("options", [])}
        for edit in plan["edits"]:
            if any(item not in finding_ids for item in edit.get("finding_ids", [])):
                raise ValidationError(f"修改 {edit['id']} 关联的意见不在当前报告中。")
            if any(item not in option_ids for item in edit.get("option_ids", [])):
                raise ValidationError(f"修改 {edit['id']} 关联的方案不在当前报告中。")

    def _normalize_state(self, raw: Any, loaded: Mapping[str, Any], data: bytes, source_sha256: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise DeliveryStateCorrupt("保存的文档修改记录损坏，暂时无法读取。")
        try:
            revision = raw["revision"]
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise ValueError("revision")
            if raw.get("source_sha256") != source_sha256:
                raise ValueError("source")
            stored_author = raw["author"]
            if not isinstance(stored_author, str) or len(stored_author) > 200:
                raise ValueError("author")
            if not stored_author and (raw["edits"] or raw["comments"]):
                raise ValueError("author")
            # Old pre-timestamp states may be read, but their historical time is
            # deliberately left unknown.  The fixed value exists only to run
            # structural validation and is never returned, persisted or used
            # for export hashes.
            stored_created_at = raw.get("created_at")
            validation_created_at = stored_created_at or "1970-01-01T00:00:00Z"
            plan = validate_plan(
                data,
                self._plan(
                    source_sha256, revision, raw["author"], validation_created_at,
                    raw["edits"], raw["comments"],
                ),
            )
            self._validate_relations(plan, loaded)
            history = raw["history"]
            if not isinstance(history, list):
                raise ValueError("history")
            normalized_history = []
            for snapshot in history:
                if not isinstance(snapshot, dict) or snapshot.get("revision") == 0:
                    if (
                        isinstance(snapshot, dict)
                        and snapshot.get("revision") == 0
                        and snapshot.get("author") == ""
                        and snapshot.get("edits") == []
                        and snapshot.get("comments") == []
                    ):
                        normalized_history.append({**snapshot, "created_at": snapshot.get("created_at")})
                        continue
                    raise ValueError("history")
                historical_author = snapshot["author"]
                if not isinstance(historical_author, str) or len(historical_author) > 200:
                    raise ValueError("history")
                if not historical_author and (snapshot["edits"] or snapshot["comments"]):
                    raise ValueError("history")
                historical_created_at = snapshot.get("created_at")
                historical = validate_plan(
                    data,
                    self._plan(
                        source_sha256,
                        snapshot["revision"],
                        snapshot["author"],
                        historical_created_at or "1970-01-01T00:00:00Z",
                        snapshot["edits"],
                        snapshot["comments"],
                    ),
                )
                self._validate_relations(historical, loaded)
                normalized_history.append({
                    "revision": snapshot["revision"], "author": historical_author,
                    "created_at": historical_created_at,
                    "edits": historical["edits"], "comments": historical["comments"],
                    "draft_confirmations": self._valid_confirmations(
                        snapshot.get("draft_confirmations", {}), loaded, historical["edits"], source_sha256),
                })
            return {
                "revision": revision,
                "source_sha256": source_sha256,
                "author": stored_author,
                "created_at": stored_created_at,
                "edits": plan["edits"],
                "comments": plan["comments"],
                "draft_confirmations": self._valid_confirmations(
                    raw.get("draft_confirmations", {}), loaded, plan["edits"], source_sha256)
                    if "draft_confirmations" in raw else self._recover_confirmations(loaded, plan["edits"], source_sha256),
                "history": normalized_history,
            }
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise DeliveryStateCorrupt("保存的文档修改记录损坏，暂时无法读取。") from exc

    def _load_state(self, loaded: Mapping[str, Any], data: bytes, source_sha256: str) -> dict[str, Any]:
        path = self._state_path(loaded)
        if not path.exists():
            return {
                "revision": 0,
                "source_sha256": source_sha256,
                "author": "",
                "created_at": None,
                "edits": [],
                "comments": [],
                "draft_confirmations": {},
                "history": [],
            }
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeliveryStateCorrupt("保存的文档修改记录损坏，暂时无法读取。") from exc
        return self._normalize_state(raw, loaded, data, source_sha256)

    def _source(self, loaded: Mapping[str, Any]) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
        data, inspected = self._inspect_current(loaded)
        binding = self._binding(loaded, inspected)
        return data, inspected, binding

    def _targets(self, loaded: Mapping[str, Any], data: bytes, inspected: Mapping[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
        pieces = {item["id"]: item for item in inspected["pieces"]}
        locations = {item["id"]: item["location"] for item in loaded["paragraphs"]}
        targets: dict[str, list[dict[str, Any]]] = {}
        unsupported: list[dict[str, str]] = []
        for finding in loaded["report"].get("findings", []):
            finding_id = finding["id"]
            values: list[dict[str, Any]] = []
            for ref in finding.get("refs", []):
                piece = pieces.get(ref.get("id"))
                quote = ref.get("quote")
                target = {
                    "piece_id": ref.get("id"),
                    "paragraph_sha256": piece.get("paragraph_sha256", "") if piece else "",
                    "start": 0,
                    "end": 0,
                    "old_text": quote if isinstance(quote, str) else "",
                    "location": locations.get(ref.get("id"), ref.get("id", "未知位置")),
                    "supported": False,
                }
                reason = None
                if piece is None or not isinstance(quote, str) or not quote:
                    reason = "在原稿中找不到这条引文，暂时无法确定修改位置。"
                else:
                    count = piece["text"].count(quote)
                    if count != 1:
                        reason = "同一段里多次出现这条引文，请先确定要改哪一处。"
                    else:
                        start = piece["text"].index(quote)
                        target.update({"start": start, "end": start + len(quote)})
                        probe = {
                            "schema_version": 1,
                            "source_sha256": inspected["source_sha256"],
                            "revision": 1,
                            "author": "target-inspection",
                            "created_at": _utc_now(),
                            "edits": [{
                                "id": f"probe-{finding_id}-{piece['id']}-{start}",
                                "piece_id": piece["id"],
                                "paragraph_sha256": piece["paragraph_sha256"],
                                "start": start,
                                "end": start + len(quote),
                                "old_text": quote,
                                "new_text": quote + "（探针）",
                                "confirmed": True,
                            }],
                            "comments": [],
                        }
                        try:
                            validate_plan(data, probe)
                            target["supported"] = True
                        except ValidationError as exc:
                            reason = str(exc)
                if reason:
                    target["reason"] = reason
                    unsupported.append({"finding_id": finding_id, "piece_id": str(ref.get("id", "")), "reason": reason})
                values.append(target)
            targets[finding_id] = values
        return targets, unsupported

    def describe(self, loaded: Mapping[str, Any]) -> dict[str, Any]:
        with self.lock:
            try:
                data, inspected, _ = self._source(loaded)
                state = self._load_state(loaded, data, inspected["source_sha256"])
                targets, unsupported = self._targets(loaded, data, inspected)
                return {
                    "available": True,
                    "reason": "",
                    "source": {"source_sha256": inspected["source_sha256"], "pieces": inspected["pieces"]},
                    "state": _public_state(state),
                    "targets": targets,
                    "unsupported": unsupported,
                }
            except (DeliveryUnavailable, DeliveryStateCorrupt) as exc:
                return {"available": False, "reason": str(exc)}

    def has_configured_source(self, report_id: str) -> bool:
        return self.source_paths.get(report_id) is not None

    def action_context(
        self,
        loaded: Mapping[str, Any],
        review_state: Mapping[str, Any],
        *,
        strict: bool = False,
        source_snapshot: tuple[bytes, Mapping[str, Any], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Describe actual document actions without treating old reviews as edits.

        source_snapshot is an internal, already validated (data, inspection,
        delivery state) read. Reusing it keeps discussion inputs coherent and
        does not create or rewrite the persistent source binding on reads.
        """
        with self.lock:
            data = None
            inspected = None
            delivery_state = None
            reason = ""
            if source_snapshot is not None:
                data, inspected, delivery_state = source_snapshot
            elif self.has_configured_source(loaded["id"]):
                try:
                    data, inspected, _ = self._source(loaded)
                    delivery_state = self._load_state(loaded, data, inspected["source_sha256"])
                except (DeliveryUnavailable, DeliveryStateCorrupt) as exc:
                    if strict:
                        raise
                    reason = str(exc)

            edits = delivery_state["edits"] if delivery_state else []
            comments = delivery_state["comments"] if delivery_state else []
            edits_by_id = {item["id"]: item for item in edits}
            target_ranges: dict[str, list[tuple[str, int, int]]] = {}
            if data is not None and inspected is not None:
                targets, _ = self._targets(loaded, data, inspected)
                for finding_id, values in targets.items():
                    target_ranges[finding_id] = [
                        (item["piece_id"], item["start"], item["end"])
                        for item in values if item.get("supported")
                    ]

            actions: dict[str, dict[str, Any]] = {}
            for finding in loaded["report"].get("findings", []):
                finding_id = finding["id"]
                related_edit_ids = set()
                for edit in edits:
                    related = (
                        finding_id in edit.get("finding_ids", [])
                        or edit["id"].startswith(f"edit-{finding_id}-")
                    )
                    if not related:
                        for piece_id, start, end in target_ranges.get(finding_id, []):
                            if edit["piece_id"] != piece_id:
                                continue
                            if edit["start"] == edit["end"]:
                                related = start <= edit["start"] <= end
                            else:
                                related = max(edit["start"], start) < min(edit["end"], end)
                            if related:
                                break
                    if related:
                        related_edit_ids.add(edit["id"])
                commented = False
                for comment in comments:
                    if comment["id"].startswith(f"comment-{finding_id}"):
                        commented = True
                        break
                    if comment.get("edit_id") in related_edit_ids:
                        commented = True
                        break
                    if "piece_id" in comment:
                        for piece_id, start, end in target_ranges.get(finding_id, []):
                            if comment["piece_id"] == piece_id and max(comment["start"], start) < min(comment["end"], end):
                                commented = True
                                break
                modified = bool(related_edit_ids)
                ignored = review_state.get("reviews", {}).get(finding_id, {}).get("decision") == "驳回"
                if modified:
                    status = "已修改"
                    label = "已修改并批注" if commented else "已修改"
                elif commented:
                    status = "已批注"
                    label = "已批注"
                elif ignored:
                    status = "已忽略"
                    label = "不采用修改建议"
                else:
                    status = "待处理"
                    label = "待处理"
                actions[finding_id] = {
                    "status": status,
                    "label": label,
                    "modified": modified,
                    "commented": commented,
                    "ignored": ignored,
                }
            return {
                "actions": actions,
                "delivery_available": delivery_state is not None,
                "delivery_revision": delivery_state["revision"] if delivery_state else None,
                "delivery_reason": reason,
            }

    def report_snapshot(self, loaded: Mapping[str, Any]) -> tuple[bytes | None, dict[str, Any] | None]:
        """Read source and confirmed changes for a report without changing state."""
        with self.lock:
            if not self.has_configured_source(loaded["id"]):
                return None, None
            try:
                data, inspected = self._inspect_current(loaded)
                state = self._load_state(loaded, data, inspected["source_sha256"])
            except (DeliveryUnavailable, DeliveryStateCorrupt):
                return None, None
            return data, {"edits": state["edits"], "comments": state["comments"]}

    def rereview_snapshot(self, loaded: Mapping[str, Any]) -> dict[str, Any]:
        """Return the current confirmed clean DOCX without changing any delivery state."""
        with self.lock:
            data, inspected, _ = self._source(loaded)
            state = self._load_state(loaded, data, inspected["source_sha256"])
            current = data
            if state["edits"]:
                plan = self._plan(inspected["source_sha256"], max(1, state["revision"]),
                                  state["author"], state.get("created_at") or _utc_now(),
                                  state["edits"], [])
                current = build_bundle(data, plan, variants=["clean"])["files"][FILENAMES["clean"]]
            return {"data": current, "source_sha256": inspected["source_sha256"],
                    "delivery_revision": state["revision"],
                    "edits": state["edits"], "comments": state["comments"]}

    def save(self, loaded: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
        with self.lock:
            data, inspected, _ = self._source(loaded)
            state = self._load_state(loaded, data, inspected["source_sha256"])
            expected = body.get("expected_revision")
            if expected != state["revision"]:
                raise DeliveryConflict(state["revision"])
            if body.get("source_sha256") != inspected["source_sha256"]:
                raise DeliveryUnavailable("原稿文件版本与保存的记录不一致，请重新打开报告核对。")
            author = body.get("author")
            if not isinstance(author, str) or not author.strip() or len(author) > 200:
                raise ValidationError("请填写确认人名称，最多200字。")
            revision = state["revision"] + 1
            created_at = _utc_now()
            plan = validate_plan(
                data,
                self._plan(
                    inspected["source_sha256"], revision, author, created_at,
                    body.get("edits"), body.get("comments"),
                ),
            )
            self._validate_relations(plan, loaded)
            confirmations = self._valid_confirmations(
                state.get("draft_confirmations", {}), loaded, plan["edits"], inspected["source_sha256"])
            confirmation = body.get("draft_confirmation")
            if confirmation is not None:
                if (not isinstance(confirmation, dict) or set(confirmation) != {"candidate_id", "finding_id"}
                        or not isinstance(confirmation["finding_id"], str)
                        or not isinstance(confirmation["candidate_id"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", confirmation["candidate_id"])):
                    raise ValidationError("确认请求缺少改稿或意见编号，请重新打开这条意见。")
                finding_id = confirmation["finding_id"]
                candidate = self._resolve_candidate(loaded, finding_id, confirmation["candidate_id"], inspected["source_sha256"],
                                                    source_snapshot=(inspected, state))
                if "based_on_revision" in candidate and candidate["based_on_revision"] != state["revision"]:
                    raise ValidationError("候选改稿依据的文档版本已变化，请重新查看。")
                confirmations[finding_id] = self._make_confirmation(
                    loaded, finding_id, candidate, plan["edits"], inspected["source_sha256"])
            new_state = {
                "revision": revision,
                "source_sha256": inspected["source_sha256"],
                "author": plan["author"],
                "created_at": plan["created_at"],
                "edits": plan["edits"],
                "comments": plan["comments"],
                "draft_confirmations": confirmations,
                "history": [*state["history"], _snapshot(state)],
            }
            _atomic_json(self._state_path(loaded), new_state)
            return _public_state(new_state)

    def undo(self, loaded: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
        with self.lock:
            data, inspected, _ = self._source(loaded)
            state = self._load_state(loaded, data, inspected["source_sha256"])
            if body.get("expected_revision") != state["revision"]:
                raise DeliveryConflict(state["revision"])
            if body.get("source_sha256") != inspected["source_sha256"]:
                raise DeliveryUnavailable("原稿文件版本与保存的记录不一致，请重新打开报告核对。")
            if not state["history"]:
                raise DeliveryConflict(state["revision"], "没有可撤回的上一步文档修改。")
            previous = state["history"][-1]
            new_state = {
                "revision": state["revision"] + 1,
                "source_sha256": inspected["source_sha256"],
                "author": previous["author"],
                "created_at": previous.get("created_at"),
                "edits": previous["edits"],
                "comments": previous["comments"],
                "draft_confirmations": self._valid_confirmations(
                    previous.get("draft_confirmations", {}), loaded, previous["edits"], inspected["source_sha256"]),
                "history": state["history"][:-1],
            }
            _atomic_json(self._state_path(loaded), new_state)
            return _public_state(new_state)

    def export_zip(
        self,
        loaded: Mapping[str, Any],
        body: Mapping[str, Any],
        review_state: Mapping[str, Any],
        *,
        reader_actions: Mapping[str, Any] | None = None,
        supplemental_cases: Mapping[str, Any] | None = None,
    ) -> bytes:
        with self.lock:
            data, inspected, _ = self._source(loaded)
            state = self._load_state(loaded, data, inspected["source_sha256"])
            if body.get("expected_revision") != state["revision"]:
                raise DeliveryConflict(state["revision"])
            if body.get("source_sha256") != inspected["source_sha256"]:
                raise DeliveryUnavailable("原稿文件版本与保存的记录不一致，请重新打开报告核对。")
            if body.get("review_revision") != review_state["revision"]:
                raise DeliveryConflict(state["revision"])
            variants = body.get("variants")
            if (
                not isinstance(variants, list)
                or not variants
                or any(not isinstance(item, str) or item not in FILENAMES for item in variants)
                or len(variants) != len(set(variants))
            ):
                raise ValidationError("导出版本选择有误，请重新选择要下载的版本。")
            if state["revision"] > 0 and state.get("created_at") is None:
                raise DeliveryStateCorrupt(
                    "旧版文档修改状态缺少确认时间；系统未伪造历史时间，请重新确认保存后再导出。"
                )
            plan = self._plan(
                inspected["source_sha256"],
                max(1, state["revision"]),
                state["author"],
                state.get("created_at") or _utc_now(),
                state["edits"],
                state["comments"],
            )
            review_report = dict(loaded["report"])
            review_report["completeness"] = loaded["completeness"]
            review_report["document_name"] = loaded["document_name"]
            review_bundle = {
                "contract": loaded["contract"],
                "sources": loaded["sources"],
                "report": review_report,
                "reviews": review_state["reviews"],
                "selected_options": review_state["selected_options"],
                "actions": dict(reader_actions or {}),
                "supplemental_cases": dict(supplemental_cases or {}),
            }
            built = build_bundle(data, plan, variants=variants, review_bundle=review_bundle)
            # The browser download is an outward-facing delivery, not the CLI's
            # internal working directory.  Include only explicitly selected
            # Word variants; do not leak manifests, plans, snapshots,
            # model responses, machine paths, or an unrequested raw original.
            stem = client_stem(loaded.get("document_name"))
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for item in built["manifest"]["files"]:
                    archive.writestr(f"{stem}-{CLIENT_LABELS[item['variant']]}.docx", built["files"][item["name"]])
            return output.getvalue()


__all__ = [
    "DeliveryConflict",
    "DeliveryManager",
    "DeliveryStateCorrupt",
    "DeliveryUnavailable",
]
