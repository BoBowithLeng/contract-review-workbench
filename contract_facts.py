"""Confirmed, report-bound business facts; immutable reports are never rewritten.

Rereview reports inherit ``report.confirmed_facts = {revision, facts}``, containing
only the parent's active facts. An inherited snapshot replaces all legacy seeds.
The child's sidecar revision starts at zero; inherited_revision records its origin.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping


MAX_FACTS = 500
MAX_TEXT = 24_000  # legacy business_facts/answers allow 12k; Q + answer needs more
MAX_STATE_BYTES = 4 * 1024 * 1024
FACTS_DIGEST_VERSION = 2
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FACT_ID = re.compile(r"fact-[0-9a-f]{32}\Z")
_CANDIDATE_ID = re.compile(r"fact-candidate-[0-9a-f]{32}\Z")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_LOCK = threading.Lock()


class ContractFactsError(RuntimeError):
    pass


class ContractFactsConflict(ContractFactsError):
    def __init__(self, message: str, *, current_revision: int | None = None):
        super().__init__(message)
        self.current_revision = current_revision


class ContractFactsCorrupt(ContractFactsError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def facts_digest(state: Mapping[str, Any], *, version: int = FACTS_DIGEST_VERSION) -> str:
    """Changes when the effective facts change, not when merely reading a report."""
    if version not in {1, FACTS_DIGEST_VERSION}:
        raise ValueError("合同信息摘要版本无效。")
    payload = effective_facts(state)
    if version == FACTS_DIGEST_VERSION:
        payload = {"facts": payload, "questions": state.get("questions", [])}
    return hashlib.sha256(_canonical(payload)).hexdigest()


def effective_facts(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [copy.deepcopy(item) for item in state.get("facts", []) if isinstance(item, dict) and item.get("active") is True]


def sanitize_fact_history(value: Any, state: Mapping[str, Any]) -> Any:
    """Omit explicitly superseded human assertions from later discussion inputs.

    Report/source storage stays intact. Only the model's discussion history and old
    opinion narrative are filtered; contract quotations themselves must remain exact.
    """
    obsolete = set()
    for fact in state.get("facts", []):
        if isinstance(fact, dict) and fact.get("active") is False:
            obsolete.add(fact.get("text", ""))
            obsolete.add(fact.get("source", {}).get("quote", ""))
    if isinstance(value, str):
        for text in sorted((text for text in obsolete if isinstance(text, str) and text), key=len, reverse=True):
            if len(text) >= 4 or value.strip() == text:
                value = value.replace(text, "【此前信息已更正，以本合同有效事实为准】")
        return value
    if isinstance(value, list):
        return [sanitize_fact_history(item, state) for item in value]
    if isinstance(value, dict):
        # Original contract quotes and stable identities retain their exact values.
        resolved = {_question_key(q["question"]) for q in state.get("questions", []) if q.get("status") == "answered"}
        return {key: ([q for q in item if not isinstance(q, str) or _question_key(q) not in resolved]
                      if key == "questions" and isinstance(item, list) else item)
                if key in {"refs", "id", "rule_id", "source_ids", "questions"} else sanitize_fact_history(item, state)
                for key, item in value.items()}
    return value


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_TEXT


def _source_valid(source: Any) -> bool:
    return (isinstance(source, dict) and isinstance(source.get("kind"), str)
            and source["kind"] in {"business_facts", "preparation", "discussion", "manual", "answer"}
            and _text(source.get("report_id")) and _text(source.get("quote"))
            and all(isinstance(value, str) and len(value) <= MAX_TEXT for value in source.values()))


def _fact_valid(fact: Any) -> bool:
    return (isinstance(fact, dict) and isinstance(fact.get("id"), str) and bool(_FACT_ID.fullmatch(fact["id"]))
            and _text(fact.get("text")) and type(fact.get("active")) is bool and _source_valid(fact.get("source"))
            and (fact.get("superseded_by") is None or isinstance(fact["superseded_by"], str)))


def _seed(loaded: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    report = loaded.get("report", {})
    if "confirmed_facts" in report:
        snapshot = report["confirmed_facts"]
        if (not isinstance(snapshot, dict) or type(snapshot.get("revision")) is not int or snapshot["revision"] < 0
                or not isinstance(snapshot.get("facts"), list) or len(snapshot["facts"]) > MAX_FACTS
                or any(not _fact_valid(item) or item["active"] is not True for item in snapshot["facts"])
                or len({item["id"] for item in snapshot["facts"]}) != len(snapshot["facts"])):
            raise ContractFactsCorrupt("父轮合同信息快照损坏，未退回已被更正的旧信息。")
        return copy.deepcopy(snapshot["facts"]), snapshot["revision"]
    seeds = []

    def add(text: str, source: dict[str, str]) -> None:
        identity = hashlib.sha256(_canonical(source)).hexdigest()[:32]
        seeds.append({"id": "fact-" + identity, "text": text, "source": source, "active": True,
                      "confirmed_at": None, "superseded_by": None, "seed": True})

    context = report.get("context", {})
    raw = context.get("business_facts") if isinstance(context, dict) else None
    if _text(raw):
        add(raw.strip(), {"kind": "business_facts", "report_id": loaded["id"], "quote": raw.strip()})
    preparation = report.get("preparation", {})
    if isinstance(preparation, dict):
        for question in preparation.get("questions", []):
            if (isinstance(question, dict) and question.get("status") == "answered" and _text(question.get("answer"))):
                answer = question["answer"].strip()
                qtext = question.get("question")
                # Keep the question: a bare "是" is not meaningful in another discussion.
                text = f"{qtext}\n答：{answer}" if _text(qtext) else answer
                add(text, {"kind": "preparation", "report_id": loaded["id"], "quote": answer,
                           "question_id": str(question.get("id", "")), "question": qtext if _text(qtext) else ""})
    return seeds, None


def _answer_part(question: str, text: str) -> str:
    """Answered questions are stored as "question\\n答：answer"; return the answer alone."""
    prefix = f"{question}\n答："
    return text[len(prefix):].strip() if text.startswith(prefix) else text


def _question_key(text: str) -> str:
    return re.sub(r"[\s？?。！!，,：:]", "", text).casefold()


def _questions(loaded: Mapping[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One question identity across preparation, findings and inherited rounds."""
    report = loaded.get("report", {})
    inherited = report.get("confirmed_facts", {}).get("questions", [])
    if not isinstance(inherited, list):
        raise ContractFactsCorrupt("父轮问题快照损坏，未重新生成旧问题。")
    result = copy.deepcopy(inherited)
    if any(not isinstance(q, dict) or not _text(q.get("question")) or not _text(q.get("id")) for q in result):
        raise ContractFactsCorrupt("父轮问题快照损坏，未重新生成旧问题。")
    # Finding IDs are local to each report. Preserve provenance, but rebuild the
    # clickable discussion target only from findings in this child report.
    for question in result:
        if question.get("finding_id") and isinstance(question.get("source"), dict):
            question["source"].setdefault("finding_id", question["finding_id"])
        question["finding_id"] = None
        question["finding_ids"] = []
    known = {_question_key(q["question"]): q for q in result}

    def add(text: Any, *, source: str, source_id: str = "", finding_id: str | None = None,
            reason: str = "", required: bool = False, status: str = "unknown", answer: str = "") -> None:
        if not _text(text):
            return
        key = _question_key(text)
        if key in known:
            if finding_id and not known[key].get("finding_id"):
                known[key]["finding_id"] = finding_id
                known[key]["source"].setdefault("finding_id", finding_id)
            if finding_id:
                links = known[key].setdefault("finding_ids", [])
                if finding_id not in links:
                    links.append(finding_id)
            return
        identity = "question-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        fact = next((f for f in facts if f["source"].get("question_id") == source_id
                     and f["source"].get("kind") == "preparation"), None) if source == "preparation" else None
        visited = set()
        while fact and not fact["active"]:
            if fact["id"] in visited:
                raise ContractFactsCorrupt("合同信息更正链损坏。")
            visited.add(fact["id"])
            fact = next((f for f in facts if f["id"] == fact.get("superseded_by")), None)
        current_answer = (answer if fact["source"]["kind"] == "preparation"
                          else _answer_part(text, fact["text"])) if fact else ""
        question = {"id": identity, "question": text, "reason": reason, "required": required,
                    "status": "answered" if fact else status, "answer": current_answer,
                    "fact_id": fact["id"] if fact else None, "finding_id": finding_id,
                    "finding_ids": [finding_id] if finding_id else [],
                    "source": {"kind": source, "report_id": loaded["id"], "question_id": source_id}}
        if finding_id:
            question["source"]["finding_id"] = finding_id
        result.append(question)
        known[key] = question

    for q in report.get("preparation", {}).get("questions", []):
        if isinstance(q, dict):
            add(q.get("question"), source="preparation", source_id=q.get("id", ""),
                reason=q.get("reason", ""), required=q.get("required", False),
                answer=q.get("answer", ""))
    for f in report.get("findings", []):
        if not isinstance(f, dict):
            continue
        texts = f.get("questions", [])
        if not texts and f.get("issue_kind") == "信息" and f.get("status") == "信息不足":
            texts = [f.get("title")]
        for text in texts:
            add(text, source="finding", finding_id=f.get("id"), reason=f.get("reason", ""))
    return result


class ContractFactsManager:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.mutation_guard = None
        with _LOCKS_LOCK:
            self.lock = _LOCKS.setdefault(str(self.root.resolve()).casefold(), threading.RLock())

    def _binding(self, loaded: Mapping[str, Any]) -> dict[str, str]:
        rid, digest = loaded.get("id"), loaded.get("request_digest")
        fingerprint = loaded.get("contract", {}).get("fingerprint")
        if (not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", rid)
                or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                or not isinstance(fingerprint, str) or not _DIGEST.fullmatch(fingerprint)):
            raise ValueError("暂时无法确认这份报告对应的合同信息。")
        return {"report_id": rid, "request_digest": digest, "contract_fingerprint": fingerprint}

    def _path(self, loaded: Mapping[str, Any]) -> Path:
        binding = self._binding(loaded)
        digest = hashlib.sha256(_canonical([binding["report_id"], binding["request_digest"]])).hexdigest()
        return self.root / f"{binding['report_id']}-{digest}.json"

    def _default(self, loaded: Mapping[str, Any]) -> dict[str, Any]:
        facts, inherited = _seed(loaded)
        state = {"schema_version": 2, **self._binding(loaded), "revision": 0, "facts": facts,
                 "confirmations": [], "inherited_revision": inherited, "questions": _questions(loaded, facts)}
        try:
            return self._validate(state)
        except ValueError as exc:
            raise ContractFactsCorrupt("报告的合同信息或问题快照损坏。") from exc

    @staticmethod
    def _validate(state: Any) -> dict[str, Any]:
        required = {"schema_version", "report_id", "request_digest", "contract_fingerprint", "revision",
                    "facts", "confirmations", "inherited_revision"}
        if isinstance(state, dict) and state.get("schema_version") == 2:
            required.add("questions")
        if (not isinstance(state, dict) or set(state) != required or state["schema_version"] not in {1, 2}
                or type(state["revision"]) is not int or state["revision"] < 0
                or (state["inherited_revision"] is not None and
                    (type(state["inherited_revision"]) is not int or state["inherited_revision"] < 0))
                or not isinstance(state["facts"], list) or len(state["facts"]) > MAX_FACTS
                or any(not _fact_valid(item) for item in state["facts"])
                or len({item["id"] for item in state["facts"]}) != len(state["facts"])):
            raise ValueError("facts state")
        confirmations = state["confirmations"]
        ids = {item["id"] for item in state["facts"]}
        if (not isinstance(confirmations, list) or len(confirmations) > 2_000
                or any(not isinstance(item, dict) or set(item) != {"key", "fact_id", "replace_id", "candidate", "confirmed_at"}
                       or not isinstance(item["key"], str) or not _DIGEST.fullmatch(item["key"])
                       or item["fact_id"] not in ids or (item["replace_id"] is not None and item["replace_id"] not in ids)
                       or not isinstance(item["candidate"], dict) or not isinstance(item["confirmed_at"], str)
                       for item in confirmations)
                or len({item["key"] for item in confirmations}) != len(confirmations)
                or any(item.get("superseded_by") is not None and item["superseded_by"] not in ids for item in state["facts"])):
            raise ValueError("confirmations")
        if "questions" in state:
            questions = state["questions"]
            if (not isinstance(questions, list) or len(questions) > MAX_FACTS
                    or any(not isinstance(q, dict) or not _text(q.get("id")) or not _text(q.get("question"))
                           or q.get("status") not in {"unknown", "answered"} or not isinstance(q.get("answer"), str)
                           or len(q["answer"]) > MAX_TEXT or (q.get("fact_id") is not None and q["fact_id"] not in ids)
                           or type(q.get("required")) is not bool or not isinstance(q.get("reason"), str)
                           or not isinstance(q.get("source"), dict)
                           for q in questions)
                    or len({q["id"] for q in questions}) != len(questions)):
                raise ValueError("questions")
        return state

    def _load(self, loaded: Mapping[str, Any]) -> dict[str, Any]:
        path = self._path(loaded)
        if not path.exists():
            return self._default(loaded)
        try:
            if path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError("size")
            state = self._validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ContractFactsCorrupt("保存的合同信息记录损坏，暂时无法读取。") from exc
        if any(state.get(key) != value for key, value in self._binding(loaded).items()):
            raise ContractFactsConflict("合同信息记录不属于当前报告或原稿。", current_revision=state["revision"])
        if state["schema_version"] == 1:
            state.update(schema_version=2, questions=_questions(loaded, state["facts"]))
        return state

    @staticmethod
    def _public(state: Mapping[str, Any]) -> dict[str, Any]:
        return copy.deepcopy({key: state[key] for key in ("revision", "facts", "inherited_revision", "confirmations", "questions")}
                             | {"has_updates_since_review": state["revision"] > 0})

    def get(self, loaded: Mapping[str, Any]) -> dict[str, Any]:
        with self.lock:
            return self._public(self._load(loaded))

    def _write(self, loaded: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        self._validate(state)
        serialized = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        if len(serialized) > MAX_STATE_BYTES:
            raise ValueError("合同信息记录超过容量上限。")
        path = self._path(loaded)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self._public(state)

    def _mutation(self, loaded: Mapping[str, Any], payload: Mapping[str, Any], required: set[str],
                  optional: set[str] = frozenset()) -> dict[str, Any]:
        if (not isinstance(payload, Mapping) or not required <= set(payload) or set(payload) - required - optional
                or type(payload.get("expected_revision")) is not int or payload["expected_revision"] < 0):
            raise ValueError("合同信息提交格式不正确，请检查后重试。")
        if self.mutation_guard:
            self.mutation_guard(loaded)
        state = copy.deepcopy(self._load(loaded))
        if payload["expected_revision"] != state["revision"]:
            raise ContractFactsConflict("合同信息已更新，请刷新后保存。", current_revision=state["revision"])
        return state

    @staticmethod
    def _active(state: dict[str, Any], fact_id: Any) -> dict[str, Any]:
        fact = next((f for f in state["facts"] if f["id"] == fact_id and f["active"]), None)
        if not fact:
            raise ContractFactsConflict("这条信息已更正或撤回，请刷新。", current_revision=state["revision"])
        return fact

    def save(self, loaded: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        with self.lock:
            state = self._mutation(loaded, payload, {"expected_revision", "text"}, {"replace_id"})
            if not _text(payload["text"]):
                raise ValueError("请输入不超过24000字的有效信息。")
            if payload.get("replace_id") is not None and (not isinstance(payload["replace_id"], str)
                                                          or not _FACT_ID.fullmatch(payload["replace_id"])):
                raise ValueError("找不到要更正的合同信息，请刷新后重新选择。")
            old = self._active(state, payload["replace_id"]) if payload.get("replace_id") else None
            text, now = payload["text"].strip(), _now()
            answer = text
            linked = next((q for q in state["questions"] if old and q.get("fact_id") == old["id"]), None)
            existing = any(f["active"] and f["text"].strip() == text for f in state["facts"])
            if linked and _text(linked.get("question")) and not existing:
                # The editor shows only the answer; keep the question beside it for later discussions.
                answer = _answer_part(linked["question"], text)
                text = f"{linked['question']}\n答：{answer}"
                if not answer or len(text) > MAX_TEXT:
                    raise ValueError("请输入不超过24000字的有效信息。")
            if old and old["text"] == text:
                return self._public(state)
            duplicate = next((f for f in state["facts"] if f["active"] and f["text"].strip() == text), None)
            if duplicate and old is None:
                return self._public(state)
            fact = duplicate or {"id": "fact-" + uuid.uuid4().hex, "text": text,
                                 "source": {"kind": "manual", "report_id": loaded["id"], "quote": answer},
                                 "active": True, "confirmed_at": now, "superseded_by": None, "seed": False}
            if duplicate is None:
                state["facts"].append(fact)
            if old:
                old.update(active=False, superseded_by=fact["id"], superseded_at=now)
                for q in state["questions"]:
                    if q.get("fact_id") == old["id"]:
                        q.update(fact_id=fact["id"], answer=_answer_part(q["question"], text), status="answered")
            state["revision"] += 1
            return self._write(loaded, state)

    def withdraw(self, loaded: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        with self.lock:
            state = self._mutation(loaded, payload, {"expected_revision", "fact_id"})
            fact = self._active(state, payload["fact_id"])
            fact.update(active=False, withdrawn_at=_now())
            for q in state["questions"]:
                if q.get("fact_id") == fact["id"]:
                    q.update(fact_id=None, answer="", status="unknown", response_recorded=False)
            state["revision"] += 1
            return self._write(loaded, state)

    def answer(self, loaded: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        with self.lock:
            state = self._mutation(loaded, payload, {"expected_revision", "question_id", "answer", "status"})
            q = next((q for q in state["questions"] if q["id"] == payload["question_id"]), None)
            text, status = payload["answer"], payload["status"]
            if (not q or not isinstance(status, str) or status not in {"answered", "unknown"} or not isinstance(text, str)
                    or len(text) > MAX_TEXT or (status == "answered" and (not text.strip() or len(q["question"] + "\n答：" + text.strip()) > MAX_TEXT))):
                raise ValueError("请检查所选问题；选择“已回答”时需要填写回答。")
            if q["status"] == status and q["answer"] == text and q.get("response_recorded") is True:
                return self._public(state)
            old = self._active(state, q["fact_id"]) if q.get("fact_id") else None
            fid = None
            if status == "answered":
                fid = "fact-" + uuid.uuid4().hex
                state["facts"].append({"id": fid, "text": q["question"] + "\n答：" + text.strip(),
                    "source": {"kind": "answer", "report_id": loaded["id"], "quote": text,
                               "question_id": q["id"], "question": q["question"], "finding_id": q.get("finding_id") or ""},
                    "active": True, "confirmed_at": _now(), "superseded_by": None, "seed": False})
            if old and not any(other["id"] != q["id"] and other.get("fact_id") == old["id"]
                               for other in state["questions"]):
                old.update(active=False, superseded_by=fid, superseded_at=_now())
            q.update(answer=text, status=status, fact_id=fid, response_recorded=True)
            state["revision"] += 1
            return self._write(loaded, state)

    def answer_batch(self, loaded: Mapping[str, Any], expected_revision: int, answers: list[dict[str, Any]]) -> dict[str, Any]:
        """Validate the whole response round, then commit facts once (no partial answers)."""
        with self.lock:
            state = copy.deepcopy(self._load(loaded))
            if type(expected_revision) is not int or state["revision"] != expected_revision:
                raise ContractFactsConflict("合同信息已更新，请刷新后保存。", current_revision=state["revision"])
            if not isinstance(answers, list) or not answers or len(answers) > MAX_FACTS:
                raise ValueError("请逐项回答或明确选择暂无法提供。")
            seen = set()
            for item in answers:
                if not isinstance(item, dict) or set(item) != {"question_id", "status", "answer"}:
                    raise ValueError("集中回答的提交格式不正确，请检查后重试。")
                q = next((q for q in state["questions"] if q["id"] == item["question_id"]), None)
                text, status = item["answer"], item["status"]
                if (not q or item["question_id"] in seen or not isinstance(status, str) or status not in {"answered", "unknown"}
                        or not isinstance(text, str) or len(text) > MAX_TEXT
                        or (status == "answered" and (not text.strip() or len(q["question"] + "\n答：" + text.strip()) > MAX_TEXT))
                        or (status == "unknown" and text.strip())):
                    raise ValueError("每个问题须填写有效回答或明确暂无法提供；不能重复提交问题。")
                seen.add(q["id"])
                old = self._active(state, q["fact_id"]) if q.get("fact_id") else None
                fid = None
                if status == "answered":
                    fid = "fact-" + uuid.uuid4().hex
                    state["facts"].append({"id": fid, "text": q["question"] + "\n答：" + text.strip(),
                        "source": {"kind": "answer", "report_id": loaded["id"], "quote": text,
                                   "question_id": q["id"], "question": q["question"], "finding_id": q.get("finding_id") or ""},
                        "active": True, "confirmed_at": _now(), "superseded_by": None, "seed": False})
                if old and not any(other["id"] != q["id"] and other.get("fact_id") == old["id"] for other in state["questions"]):
                    old.update(active=False, superseded_by=fid, superseded_at=_now())
                q.update(answer=text.strip(), status=status, fact_id=fid, response_recorded=True)
            state["revision"] += 1
            return self._write(loaded, state)

    def merge_questions(self, loaded: Mapping[str, Any]) -> None:
        """Add questions discovered by the one corrective call without a second gate."""
        with self.lock:
            state = copy.deepcopy(self._load(loaded))
            known = {_question_key(q["question"]) for q in state["questions"]}
            additions = [q for q in _questions(loaded, state["facts"]) if _question_key(q["question"]) not in known]
            if additions:
                state["questions"].extend(additions)
                state["revision"] += 1
                self._write(loaded, state)

    def confirm(self, loaded: Mapping[str, Any], payload: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
        """candidate must come from the server's persisted, source-checked discussion.

        The browser submits only IDs and revisions. No browser text is accepted here.
        """
        required = {"expected_revision", "candidate_id"}
        if (not isinstance(payload, Mapping) or not required <= set(payload) or set(payload) - required - {"replace_id"}
                or type(payload["expected_revision"]) is not int or payload["expected_revision"] < 0):
            raise ValueError("确认合同信息的请求格式不正确，请刷新后重试。")
        source = candidate.get("source") if isinstance(candidate, Mapping) else None
        if (not isinstance(candidate, Mapping) or not isinstance(candidate.get("id"), str)
                or not _CANDIDATE_ID.fullmatch(candidate["id"]) or payload["candidate_id"] != candidate["id"]
                or not isinstance(candidate.get("kind"), str) or candidate["kind"] not in {"fact", "correction"}
                or not _text(candidate.get("text")) or not _text(candidate.get("source_quote"))
                or not _source_valid(source) or source["kind"] != "discussion" or source["report_id"] != loaded["id"]
                or source["quote"] != candidate["source_quote"]
                or not _text(source.get("finding_id")) or not _text(source.get("session_id")) or not _text(source.get("turn_id"))):
            raise ValueError("找不到这条待确认信息，或无法核对它对应的原话。")
        replace = payload.get("replace_id")
        if replace is not None and (not isinstance(replace, str) or not _FACT_ID.fullmatch(replace)):
            raise ValueError("要替换的信息编号有误，请重新选择。")
        if candidate["kind"] == "correction" and replace is None:
            raise ValueError("更正事实时须明确选择要替换的已确认信息。")
        key = hashlib.sha256(_canonical([candidate["id"], source])).hexdigest()
        with self.lock:
            if self.mutation_guard:
                self.mutation_guard(loaded)
            state = self._load(loaded)
            prior = next((item for item in state["confirmations"] if item["key"] == key), None)
            if prior:
                if prior["replace_id"] != replace or prior["candidate"] != dict(candidate):
                    raise ContractFactsConflict("该候选已经确认，不能用同一候选改写另一项信息。", current_revision=state["revision"])
                return self._public(state)
            if state["revision"] != payload["expected_revision"]:
                raise ContractFactsConflict("合同信息已更新，请刷新后确认。", current_revision=state["revision"])
            state = copy.deepcopy(state)
            question_id = candidate.get("question_id")
            question = next((q for q in state["questions"] if q["id"] == question_id), None) if question_id else None
            if question_id and (not question or question.get("finding_id") != source.get("finding_id")):
                raise ValueError("事实候选引用的问题不属于本条意见。")
            if question and question.get("fact_id") and replace != question["fact_id"]:
                raise ContractFactsConflict("这条问题已有回答，请明确更正原回答。", current_revision=state["revision"])
            replaced = next((item for item in state["facts"] if item["id"] == replace), None) if replace else None
            if replace and (not replaced or not replaced["active"]):
                raise ContractFactsConflict("待更正的信息已不存在或已经更正，请刷新。", current_revision=state["revision"])
            text = candidate["text"].strip()
            duplicate = next((item for item in state["facts"] if item["active"] and item["text"].strip() == text), None)
            timestamp = _now()
            fact = duplicate or {"id": "fact-" + key[:32], "text": text, "source": copy.deepcopy(source),
                                 "active": True, "confirmed_at": timestamp, "superseded_by": None, "seed": False}
            if not duplicate:
                state["facts"].append(fact)
            if replaced and replaced["id"] != fact["id"]:
                replaced.update(active=False, superseded_by=fact["id"], superseded_at=timestamp)
            state["confirmations"].append({"key": key, "fact_id": fact["id"], "replace_id": replace,
                                           "candidate": copy.deepcopy(dict(candidate)), "confirmed_at": timestamp})
            state["revision"] += 1
            if replaced:
                for q in state["questions"]:
                    if q.get("fact_id") == replaced["id"]:
                        q.update(fact_id=fact["id"], answer=text, status="answered")
            if question:
                question.update(fact_id=fact["id"], answer=text, status="answered")
            return self._write(loaded, state)


__all__ = ["ContractFactsManager", "ContractFactsError", "ContractFactsConflict", "ContractFactsCorrupt",
           "effective_facts", "facts_digest", "sanitize_fact_history"]
