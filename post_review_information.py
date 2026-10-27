"""One central response round before a newly-created review is published.

The original response/result are immutable. A separately bound, validated partial
projection changes only question-linked findings, before opinion-revision overlays.
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Mapping

from contract_facts import (ContractFactsConflict, ContractFactsCorrupt, _question_key, _questions, effective_facts,
                            facts_digest)
from draft_plan import DRAFT_PLAN_VERSION, DRAFT_PLAN_VERSION_FIELD, prompt_rules as draft_plan_rules
from general_review import (_validate_finding, source_gate,
                            QUOTE_VALIDATION_VERSION_FIELD, STATUS_VALUES)
from review_jobs import _atomic_json, _canonical, _read_json, _sha256, MAX_RAW_RESPONSE_BYTES


_ACTIVE: set[str] = set()

# Scope 2 (recorded on each submission): the one update call may revise any
# opinion an answer affects, not only the opinion that asked, and must name the
# answers behind every revision. Records without the field replay as scope 1.
SCOPE_VERSION = 2
_BASIS_REASON_LIMIT = 300


class InformationGateRequired(RuntimeError):
    def __init__(self, report_id: str, gate: dict):
        super().__init__("请先回答本轮问题；暂时答不了的可以选“暂无法提供”，再查看报告。")
        self.report_id, self.gate = report_id, gate


def _rollup(findings: list[dict], fallback: str) -> str:
    statuses = {f.get("status") for f in findings}
    for status in ("处理失败", "发现风险", "需要法务判断", "信息不足"):
        if status in statuses:
            return status
    if statuses and statuses <= {"未检出", "不适用"}:
        return "不适用" if statuses == {"不适用"} else "未检出"
    return fallback


def project_findings(loaded: Mapping[str, Any], updates: list[dict], group_results: list[dict] | None = None,
                     basis: list[dict] | None = None) -> dict:
    result = copy.deepcopy(dict(loaded))
    report = result["report"]
    affected = {f["id"] for f in updates}
    update_map = {f["id"]: f for f in updates}
    for row in basis or []:
        update_map[row["finding_id"]] = {**update_map[row["finding_id"]],
                                         "post_review_update": {"questions": row["questions"], "reason": row["reason"]}}
    report["findings"] = [copy.deepcopy(update_map.get(f["id"], f)) for f in report["findings"]]
    known = {f["id"]: f for f in report["findings"]}
    rules = {f["rule_id"] for f in updates}
    for check in report.get("checks", []):
        if check.get("rule_id") in rules:
            check["status"] = _rollup([f for f in known.values() if f["rule_id"] == check["rule_id"]], check["status"])
            check["reason"] = "；".join(f["reason"] for f in known.values() if f["rule_id"] == check["rule_id"])
    group_map = {g["group_id"]: g for g in (group_results or [])}
    for row in report.get("checklist_results", []):
        touched = bool(affected.intersection(row.get("finding_ids", [])))
        for group in row.get("groups", []):
            if affected.intersection(group.get("finding_ids", [])):
                if group["group_id"] not in group_map:
                    raise ValueError("受影响的独立检查内容缺少显式更新结果，不能推定通过。")
                group.update(copy.deepcopy(group_map[group["group_id"]]))
                touched = True
        if touched:
            groups = row.get("groups", [])
            if groups:
                # A finding clearing does not clear the rest of its group's
                # requirements. Only explicit group outcomes determine the parent.
                row["status"] = _rollup(groups, row["status"])
                row["reason"] = "；".join(g["reason"] for g in groups)
                row["finding_ids"] = list(dict.fromkeys(fid for g in groups for fid in g.get("finding_ids", [])))
            else:
                row["reason"] = "关联意见已根据补充信息更新；本检查项其余内容尚未重新确认。"
    # Existing proposals were drafted against the pre-answer opinions. They are
    # retained in the immutable result, but not delivered as actionable fresh plans.
    report["options"] = [option for option in report.get("options", [])
                         if not affected.intersection(option.get("finding_ids", []))]
    return result


class PostReviewInformationManager:
    def __init__(self, root: Path, facts, jobs):
        self.root, self.facts, self.jobs = Path(root), facts, jobs
        self.lock = facts.lock
        self._active = _ACTIVE

    @staticmethod
    def enabled(loaded):
        return loaded.get("post_review_information_version") == 1

    def _directory(self, loaded):
        binding = self.facts._binding(loaded)
        digest = _sha256(_canonical(binding))
        return self.root / (binding["report_id"] + "-" + digest)

    def _read(self, loaded):
        path = self._directory(loaded) / "publication.json"
        if not path.exists():
            facts = self.facts.get(loaded)
            # Preparation and inherited questions have already had their response
            # opportunity; do not force identical unknown questions again.
            previous = {_question_key(q.get("question", "")) for q in loaded["report"].get("preparation", {}).get("questions", [])}
            previous |= {_question_key(q.get("question", "")) for q in loaded["report"].get("confirmed_facts", {}).get("questions", [])}
            explicit = {_question_key(text) for f in loaded["report"].get("findings", [])
                        for text in f.get("questions", []) if isinstance(text, str) and text.strip()}
            ids = [q["id"] for q in facts["questions"] if q.get("source", {}).get("kind") == "finding"
                   and _question_key(q["question"]) in explicit
                   and _question_key(q["question"]) not in previous and q["status"] != "answered"]
            return {"schema_version": 1, **self.facts._binding(loaded), "revision": 0,
                    "question_ids": ids, "status": "needs_response" if ids else "ready",
                    "facts_revision": facts["revision"], "attempt": 0, "error": None,
                    "requires_model": False, "submission_digest": None, "affected_ids": [],
                    "frozen_facts": None, "response_sha256": None, "overlay_sha256": None,
                    "scope_version": SCOPE_VERSION}
        state = _read_json(path, "集中回答记录")
        if (not isinstance(state, dict) or state.get("schema_version") != 1
                or any(state.get(k) != v for k, v in self.facts._binding(loaded).items())
                or state.get("status") not in {"needs_response", "running", "failed", "ready"}
                or type(state.get("revision")) is not int or type(state.get("attempt")) is not int
                or not isinstance(state.get("question_ids"), list)
                or any(not isinstance(qid, str) for qid in state["question_ids"])
                or len(set(state["question_ids"])) != len(state["question_ids"])):
            raise ContractFactsCorrupt("集中回答记录损坏，暂时无法打开报告。")
        if (state["revision"] not in {0, 1} or state["attempt"] < 0
                or state.get("scope_version", 1) not in {1, 2}
                or type(state.get("requires_model")) is not bool
                or type(state.get("facts_revision")) is not int
                or not isinstance(state.get("affected_ids"), list)
                or (state["status"] == "ready" and state["question_ids"] and state["revision"] == 0)
                or (state["revision"] == 1 and not isinstance(state.get("frozen_facts"), dict))
                or (state["status"] in {"running", "failed"} and (state["revision"] != 1 or not state["requires_model"]))
                or (state["status"] == "ready" and state["requires_model"]
                    and (not state.get("overlay_sha256") or not state.get("response_sha256")))):
            raise ContractFactsCorrupt("集中回答的完成状态与记录不一致，暂时无法打开报告。")
        if state["status"] == "running" and str(path.parent) not in self._active:
            state.update(status="failed", error="上次更新相关意见时中断了，回答已保留，可以重试更新。")
            self._write(loaded, state)
        return state

    def _write(self, loaded, state):
        _atomic_json(self._directory(loaded) / "publication.json", state)

    def _public(self, loaded, state):
        facts = self.facts.get(loaded)
        indexed = {q["id"]: q for q in facts["questions"]}
        questions = []
        for qid in state["question_ids"]:
            if qid not in indexed:
                raise ContractFactsCorrupt("集中回答的问题记录缺失。")
            q = indexed[qid]
            links = sorted({f["id"] for f in loaded["report"].get("findings", [])
                            if any(_question_key(t) == _question_key(q["question"]) for t in f.get("questions", []))}
                         | set(q.get("finding_ids", [])) | ({q["finding_id"]} if q.get("finding_id") else set()))
            titles = [f["title"] for f in loaded["report"].get("findings", []) if f["id"] in links]
            questions.append({"id": qid, "question": q["question"], "reason": "用于核对相关判断。", "finding_titles": titles,
                "status": q["status"] if state["revision"] > 0 or q.get("response_recorded") else "unanswered",
                "answer": q["answer"], "finding_ids": links})
        return {"enabled": True, **{k: state[k] for k in ("status", "revision", "attempt", "error", "requires_model")},
                "facts_revision": facts["revision"], "questions": questions}

    def get(self, loaded):
        with self.lock:
            if not self.enabled(loaded):
                return {"enabled": False, "status": "ready", "revision": 0, "facts_revision": self.facts.get(loaded)["revision"],
                        "questions": [], "error": None, "requires_model": False, "attempt": 0}
            state = self._read(loaded)
            if state["status"] == "ready" and state["requires_model"]:
                self.apply(loaded)  # Recover any interrupted new-question merge without a model call.
            return self._public(loaded, state)

    def guard_mutation(self, loaded):
        if self.enabled(loaded):
            state = self._read(loaded)
            if state["status"] in {"running", "failed"}:
                raise ContractFactsConflict("这轮更新的回答已提交，请完成或重试更新后再编辑合同信息。",
                                            current_revision=self.facts.get(loaded)["revision"])

    def submit(self, loaded, payload):
        with self.lock:
            if not self.enabled(loaded):
                raise ValueError("这份历史报告不需要审后集中回答。")
            state = self._read(loaded)
            answers = payload.get("answers")
            digest = _sha256(_canonical(answers)) if isinstance(answers, list) else None
            if state["status"] in {"running", "ready"}:
                if digest is not None and state["submission_digest"] == digest:
                    return self._public(loaded, state), False
                if not state["question_ids"] and state["status"] == "ready":
                    return self._public(loaded, state), False
                raise ContractFactsConflict("本轮回答已提交，不能重复启动或改写。")
            if type(payload.get("expected_revision")) is not int or payload["expected_revision"] != state["revision"]:
                raise ContractFactsConflict("集中回答状态已变化，请刷新。")
            config = None
            if state["status"] == "failed":
                if payload.get("retry") is not True or answers is not None:
                    raise ContractFactsConflict("这轮回答已经保存，请重试更新相关意见，无需重新提交回答。")
                config = self.jobs._runtime_config(payload)
            else:
                ids = [a.get("question_id") for a in answers if isinstance(a, dict)] if isinstance(answers, list) else []
                if any(not isinstance(qid, str) for qid in ids) or len(ids) != len(state["question_ids"]) or set(ids) != set(state["question_ids"]):
                    raise ValueError("请回答每个问题；暂时答不了的可以选“暂无法提供”。")
                if any(a.get("status") == "answered" for a in answers):
                    config = self.jobs._runtime_config(payload)
                # Persist the obligation before the atomic fact commit. If the
                # following publication write fails, answered facts cannot make
                # the default gate disappear and silently bypass correction.
                self._write(loaded, state)
                facts = self.facts.answer_batch(loaded, payload.get("expected_facts_revision"), answers)
                state.update(revision=state["revision"] + 1, submission_digest=digest,
                             facts_revision=facts["revision"], frozen_facts=copy.deepcopy(facts), requires_model=config is not None)
                public = self._public(loaded, state)
                answered_ids = {a["question_id"] for a in answers if a["status"] == "answered"}
                state["affected_ids"] = sorted({fid for q in public["questions"] if q["id"] in answered_ids for fid in q["finding_ids"]})
                if config is None:
                    state.update(status="ready", error=None)
                    self._write(loaded, state)
                    return self._public(loaded, state), False
            if self.facts.get(loaded)["revision"] != state["facts_revision"]:
                raise ContractFactsConflict("这轮更新使用的合同信息已变化，请核对已提交的回答。")
            state.update(status="running", error=None, attempt=state["attempt"] + 1)
            directory = self._directory(loaded)
            self._write(loaded, state)
            self._active.add(str(directory))
            thread = threading.Thread(target=self._run, args=(copy.deepcopy(loaded), copy.deepcopy(state), config), daemon=True)
            try:
                thread.start()
            except Exception:
                self._active.discard(str(directory))
                state.update(status="failed", error="相关意见的更新没有启动，回答已保留，可以重试。")
                self._write(loaded, state)
                raise
            return self._public(loaded, state), True

    def _group_scope(self, loaded, state, ids=None):
        affected = set(state["affected_ids"] if ids is None else ids)
        plan = {check["id"]: check for check in loaded["report"].get("preparation", {}).get("checks", [])}
        scope = []
        for row in loaded["report"].get("checklist_results", []):
            groups = [g for g in row.get("groups", []) if affected.intersection(g.get("finding_ids", []))]
            if not groups:
                continue
            check = plan.get(row["check_id"])
            if not check or not isinstance(check.get("requirement"), str):
                raise ValueError("找不到本轮检查项的原始要求，这项检查尚未完成。")
            planned = {g["group_id"]: g for g in check.get("groups", [])}
            entries = []
            for group in groups:
                original = planned.get(group["group_id"])
                if not original or not isinstance(original.get("requirement"), str):
                    raise ValueError("找不到本轮检查内容的原始要求，这项检查尚未完成。")
                entries.append({"group_id": group["group_id"], "title": original["title"],
                                "requirement": original["requirement"], "current_result": copy.deepcopy(group)})
            scope.append({"check_id": check["id"], "title": check["title"], "requirement": check["requirement"], "groups": entries})
        return scope

    def _schema(self, loaded, state=None):
        from review_runner import output_schema
        version = loaded["report"].get("metadata", {}).get(QUOTE_VALIDATION_VERSION_FIELD, 1)
        schema = output_schema("general", phase="post_review_information", quote_validation_version=version,
                               draft_plan_version=loaded["report"]["context"].get(DRAFT_PLAN_VERSION_FIELD, 0))
        if state is not None and state.get("scope_version", 1) == 2:
            schema["properties"]["update_basis"] = {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["finding_id", "question_ids", "reason"],
                "properties": {"finding_id": {"type": "string"}, "question_ids": {"type": "array", "items": {"type": "string"}},
                               "reason": {"type": "string"}}}}
            schema["required"] = [*schema["required"], "update_basis"]
        return schema

    @staticmethod
    def _answered(state):
        ids = set(state["question_ids"])
        return {q["id"]: q for q in state["frozen_facts"]["questions"] if q["id"] in ids and q.get("status") == "answered"}

    def _validate(self, loaded, state, raw):
        try:
            data = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
        except (ValueError, AttributeError) as exc:
            raise ValueError("返回的更新内容无法解析。") from exc
        rows = data.get("findings") if isinstance(data, dict) else None
        widened = state.get("scope_version", 1) == 2
        keys = {"findings", "group_results", "update_basis"} if widened else {"findings", "group_results"}
        if not isinstance(data, dict) or set(data) != keys or not isinstance(rows, list):
            raise ValueError("返回的更新内容缺少意见或逐项检查结果。")
        ids = [f.get("id") for f in rows if isinstance(f, dict)]
        if widened:
            existing = {f["id"] for f in loaded["report"]["findings"]}
            if (len(ids) != len(rows) or len(set(ids)) != len(ids) or not set(ids) <= existing
                    or not set(state["affected_ids"]) <= set(ids)):
                raise ValueError("本轮更新漏掉了提出问题的意见、有重复，或包含不存在的意见。")
            basis = self._basis(state, data["update_basis"], ids)
        elif len(ids) != len(rows) or len(set(ids)) != len(ids) or set(ids) != set(state["affected_ids"]):
            raise ValueError("本轮相关意见的更新有遗漏、重复，或包含了其他意见。")
        originals = {f["id"]: f for f in loaded["report"]["findings"]}
        version = loaded["report"].get("metadata", {}).get(QUOTE_VALIDATION_VERSION_FIELD, 1)
        clean, sources, problems = source_gate(loaded["report"]["context"], loaded["sources"], quote_validation_version=version)
        if problems:
            raise ValueError("暂时无法核对更新所用的依据来源。")
        pieces = {p["id"]: p["text"] for p in loaded["contract"]["pieces"]}
        validated = []
        for row in rows:
            if row.get("rule_id") != originals[row["id"]]["rule_id"]:
                raise ValueError("更新返回的意见改动了原规则编号，暂时不能使用。")
            source_bytes = (Path(loaded["source_path"]).read_bytes() if loaded.get("source_path")
                            and clean.get(DRAFT_PLAN_VERSION_FIELD) == DRAFT_PLAN_VERSION
                            and isinstance(row.get("draft_plan"), dict) and row["draft_plan"].get("status") == "ready" else None)
            finding = _validate_finding(row, row["id"], pieces, {s["id"]: s for s in sources}, clean, version,
                                        draft_plan_source_bytes=source_bytes)
            if finding["status"] == "处理失败":
                raise ValueError("更新返回的意见与原文对应不上，或内容不完整：" +
                                 "；".join(finding.get("validation_errors", [])))
            if finding.get(DRAFT_PLAN_VERSION_FIELD) == DRAFT_PLAN_VERSION:
                finding["draft_plan_basis"] = {
                    "facts_revision": state["frozen_facts"]["revision"],
                    "facts_digest": facts_digest(state["frozen_facts"], version=1),
                    "facts_digest_version": 1, "delivery_revision": 0}
            validated.append(finding)
        scope = self._group_scope(loaded, state, ids if widened else None)
        expected = {g["group_id"]: g["current_result"] for check in scope for g in check["groups"]}
        groups = data["group_results"]
        if not isinstance(groups, list) or len(groups) != len(expected):
            raise ValueError("本轮相关检查内容的更新有遗漏、重复，或包含了其他内容。")
        known = {f["id"]: f for f in loaded["report"]["findings"]}
        known.update({f["id"]: f for f in validated})
        result_groups, seen = [], set()
        for group in groups:
            if not isinstance(group, dict) or set(group) != {"group_id", "status", "reason", "finding_ids"}:
                raise ValueError("更新返回的检查结果不完整。")
            gid, status, links = group["group_id"], group["status"], group["finding_ids"]
            if (not isinstance(gid, str) or gid not in expected or gid in seen
                    or not isinstance(status, str) or status not in STATUS_VALUES - {"处理失败"}
                    or not isinstance(group["reason"], str) or not group["reason"].strip() or len(group["reason"]) > 24000
                    or not isinstance(links, list) or any(not isinstance(fid, str) or fid not in expected[gid].get("finding_ids", []) for fid in links)
                    or len(set(links)) != len(links)):
                raise ValueError("更新返回的检查结果不完整，或涉及本轮以外的内容。")
            if status in {"未检出", "不适用"} and any(known[fid]["status"] in {"发现风险", "需要法务判断", "信息不足", "处理失败"} for fid in expected[gid].get("finding_ids", [])):
                raise ValueError("独立检查内容结论与关联待处理意见冲突。")
            seen.add(gid)
            result_groups.append(copy.deepcopy(group))
        result = {"findings": validated, "group_results": result_groups}
        if widened:
            result["update_basis"] = basis
        return result

    def _basis(self, state, rows, ids):
        """Every revised opinion names the answers that changed it, in the opinions' order."""
        answered = self._answered(state)
        if not isinstance(rows, list) or len(rows) != len(ids):
            raise ValueError("每条更新的意见都要写明依据了哪些回答。")
        clean = {}
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"finding_id", "question_ids", "reason"}
                    or row["finding_id"] not in ids or row["finding_id"] in clean
                    or not isinstance(row["question_ids"], list) or not row["question_ids"]
                    or len(set(row["question_ids"])) != len(row["question_ids"])
                    or any(qid not in answered for qid in row["question_ids"])
                    or not isinstance(row["reason"], str) or not row["reason"].strip()
                    or len(row["reason"]) > _BASIS_REASON_LIMIT):
                raise ValueError("更新理由缺失，或引用了本轮没有回答的问题。")
            clean[row["finding_id"]] = {"finding_id": row["finding_id"], "question_ids": list(row["question_ids"]),
                                        "questions": [answered[qid]["question"] for qid in row["question_ids"]],
                                        "reason": row["reason"].strip()}
        return [clean[fid] for fid in ids]

    def _messages(self, loaded, state):
        if state.get("scope_version", 1) == 2:
            return self._messages_widened(loaded, state)
        schema = self._schema(loaded)
        inputs = {"contract": loaded["contract"], "context": loaded["report"]["context"], "sources": loaded["sources"],
                      "facts": effective_facts(state["frozen_facts"]), "questions": state["frozen_facts"]["questions"],
                      "affected_finding_ids": state["affected_ids"], "findings": loaded["report"]["findings"],
                      "affected_checklist_requirements": self._group_scope(loaded, state), "required_schema": schema}
        messages = [{"role": "system", "content": "只根据本轮集中补充的事实更新affected_finding_ids中的意见。这不是全文重审；其他意见仅作交叉引用上下文，不新增、不删除、不改ID或rule_id。受影响意见逐项返回完整finding（结论不变也须返回）；不确定仍可保留信息不足。未回答/暂无法提供不能猜测。同时依据affected_checklist_requirements中的完整冻结要求，逐项独立重新判断本轮受影响的group，显式返回group_results（group_id/status/reason/finding_ids），没有受影响group时返回空数组。解决一条意见不等于该组其余要求已满足；其余事实仍不足就明确保留信息不足，不能从finding或父项通过推定group通过。group不得新增、遗漏，finding_ids只引用该组原有的关联意见。仅返回{findings:[...],group_results:[...]}。新发现的必要问题可以留在questions供以后补充，不开展第二轮提问。"},
                        {"role": "user", "content": json.dumps(inputs, ensure_ascii=False)}]
        if loaded["report"]["context"].get(DRAFT_PLAN_VERSION_FIELD) == DRAFT_PLAN_VERSION:
            messages[0]["content"] += draft_plan_rules(loaded["report"]["context"])
        return messages

    def _messages_widened(self, loaded, state):
        all_ids = [f["id"] for f in loaded["report"]["findings"]]
        inputs = {"contract": loaded["contract"], "context": loaded["report"]["context"], "sources": loaded["sources"],
                  "facts": effective_facts(state["frozen_facts"]), "questions": state["frozen_facts"]["questions"],
                  "answered_question_ids": sorted(self._answered(state)),
                  "must_update_finding_ids": state["affected_ids"], "findings": loaded["report"]["findings"],
                  "checklist_requirements": self._group_scope(loaded, state, all_ids),
                  "required_schema": self._schema(loaded, state)}
        system = ("根据本轮集中补充的回答，通看全部findings，更新所有确实受这些回答影响的意见。"
                  "must_update_finding_ids是提出这些问题的意见，必须返回，结论不变也须返回；其他意见只有当某个回答"
                  "改变了它的事实前提、结论、建议或改稿时才返回，不受影响的不要返回。这不是全文重审，不新增、不删除意见，"
                  "不改ID或rule_id；返回的每条意见都是完整finding。"
                  "update_basis为每条返回的意见各写一项：finding_id、所依据的question_ids（只能用answered_question_ids中的ID）、"
                  "reason（一句话说明哪个回答改变了什么，不超过100字）。未回答或暂无法提供的问题不能猜测。"
                  "checklist_requirements给出全部检查内容的冻结要求：对你返回的每条意见，其关联的每个group都要依据完整要求"
                  "独立重新判断，在group_results中显式返回（group_id/status/reason/finding_ids）；与返回意见无关的group不要返回。"
                  "解决一条意见不等于该组其余要求已满足；其余事实仍不足就明确保留信息不足，不能从finding或父项通过推定group通过。"
                  "finding_ids只引用该组原有的关联意见。仅返回{findings:[...],group_results:[...],update_basis:[...]}。"
                  "新发现的必要问题可以留在questions供以后补充，不开展第二轮提问。")
        if loaded["report"]["context"].get(DRAFT_PLAN_VERSION_FIELD) == DRAFT_PLAN_VERSION:
            system += draft_plan_rules(loaded["report"]["context"])
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(inputs, ensure_ascii=False)}]

    def _run(self, loaded, state, config):
        directory = self._directory(loaded)
        trace = directory / ("attempt-" + str(state["attempt"]))
        try:
            schema = self._schema(loaded, state)
            messages = self._messages(loaded, state)
            digest = _sha256(_canonical(messages))
            _atomic_json(trace / "request.json", {"request_digest": digest, "messages": messages, "engine": self.jobs._safe_config(config),
                                                 "facts_revision": state["facts_revision"]})
            config.update(profile_key="general", review_phase="post_review_information", output_schema=schema,
                          draft_plan_version=loaded["report"]["context"].get(DRAFT_PLAN_VERSION_FIELD, 0),
                          quote_validation_version=loaded["report"].get("metadata", {}).get(QUOTE_VALIDATION_VERSION_FIELD, 1))
            raw = self.jobs.runner(messages, config)
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RAW_RESPONSE_BYTES:
                raise ValueError("更新返回的意见无法读取，或内容过长。")
            secret = config.get("api_key")
            if secret:
                raw = raw.replace(secret, "[已隐藏 API Key]")
            _atomic_json(trace / "response.json", {"request_digest": digest, "raw_text": raw, "raw_sha256": _sha256(raw.encode("utf-8"))})
            updates = self._validate(loaded, state, raw)
            _atomic_json(trace / "overlay.json", updates)
            with self.lock:
                if self.facts.get(loaded)["revision"] != state["facts_revision"]:
                    raise ContractFactsConflict("更新期间合同信息版本改变，未交付旧版本结论。")
                projected = project_findings(loaded, updates["findings"], updates["group_results"])
                prior_questions = {_question_key(q["question"]) for q in state["frozen_facts"]["questions"]}
                added_questions = any(_question_key(q["question"]) not in prior_questions
                                      for q in _questions(projected, state["frozen_facts"]["facts"]))
                state["published_facts_revision"] = state["facts_revision"] + int(added_questions)
                state.update(status="ready", error=None, response_sha256=_sha256(raw.encode("utf-8")),
                             overlay_sha256=_sha256(_canonical(updates)))
                self._write(loaded, state)
        except Exception:
            with self.lock:
                state.update(status="failed", error="相关意见的更新没有完成，回答已保留，可以重试更新。")
                self._write(loaded, state)
        finally:
            with self.lock:
                self._active.discard(str(directory))

    def apply(self, loaded):
        with self.lock:
            if not self.enabled(loaded):
                return loaded
            state = self._read(loaded)
            if state["status"] != "ready":
                raise InformationGateRequired(loaded["id"], self._public(loaded, state))
            if not state["question_ids"] and not (self._directory(loaded) / "publication.json").exists():
                self._write(loaded, state)
            if not state["overlay_sha256"]:
                if state["revision"] == 0:
                    return loaded
                result = copy.deepcopy(loaded)
                result["report"].setdefault("metadata", {})["post_review_information"] = {
                    "version": 1, "facts_revision": state["facts_revision"], "input_facts_revision": state["facts_revision"],
                    "affected_finding_ids": [], "attempt": 0}
                if loaded["report"]["context"].get(DRAFT_PLAN_VERSION_FIELD) == DRAFT_PLAN_VERSION:
                    result["report"]["metadata"]["draft_plan_fact_basis"] = {
                        "source": "post_review_information_publication", "facts_revision": state["facts_revision"],
                        "facts": effective_facts(state["frozen_facts"])}
                return result
            trace = self._directory(loaded) / ("attempt-" + str(state["attempt"]))
            request = _read_json(trace / "request.json", "本轮意见更新的请求记录")
            response = _read_json(trace / "response.json", "本轮意见更新的原始回复")
            overlay = _read_json(trace / "overlay.json", "本轮意见更新的结果记录")
            raw = response.get("raw_text")
            if (not isinstance(raw, str) or _sha256(raw.encode("utf-8")) != state["response_sha256"]
                    or response.get("raw_sha256") != state["response_sha256"]
                    or request.get("messages") != self._messages(loaded, state)
                    or request.get("facts_revision") != state["facts_revision"]
                    or _sha256(_canonical(request.get("messages"))) != request.get("request_digest")
                    or response.get("request_digest") != request.get("request_digest")
                    or _sha256(_canonical(overlay)) != state["overlay_sha256"]
                    or self._validate(loaded, state, raw) != overlay):
                raise ContractFactsCorrupt("更新后的意见与原始记录不一致，暂时无法打开报告。")
            result = project_findings(loaded, overlay["findings"], overlay["group_results"], overlay.get("update_basis"))
            # Publication is already durable. Interrupted merges can be retried
            # on read without invalidating the frozen correction facts revision.
            self.facts.merge_questions(result)
            result["report"]["metadata"]["post_review_information"] = {
                "version": 1, "facts_revision": state.get("published_facts_revision", state["facts_revision"]),
                "input_facts_revision": state["facts_revision"], "attempt": state["attempt"],
                "affected_finding_ids": ([f["id"] for f in overlay["findings"]] if state.get("scope_version", 1) == 2
                                         else state["affected_ids"])}
            if loaded["report"]["context"].get(DRAFT_PLAN_VERSION_FIELD) == DRAFT_PLAN_VERSION:
                result["report"]["metadata"]["draft_plan_fact_basis"] = {
                    "source": "post_review_information_publication", "facts_revision": state["facts_revision"],
                    "facts": effective_facts(state["frozen_facts"])}
            return result
