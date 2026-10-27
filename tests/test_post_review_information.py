from __future__ import annotations
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contract_facts import ContractFactsManager, ContractFactsConflict, ContractFactsCorrupt
from post_review_information import PostReviewInformationManager, InformationGateRequired, project_findings
from tests.test_opinion_revisions import full_loaded


class Jobs:
    def __init__(self, runner):
        self.runner = runner
    def _runtime_config(self, payload):
        if payload.get("consent") is not True:
            raise ValueError("consent")
        return dict(payload.get("config", {}))
    def _safe_config(self, config):
        return {"model": config.get("model")}


class InformationGateTests(unittest.TestCase):
    """Scope 1: records submitted before 2026-10-08 replay exactly as they were produced."""
    def setUp(self):
        scope = patch("post_review_information.SCOPE_VERSION", 1)
        scope.start()
        self.addCleanup(scope.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.loaded = full_loaded()
        self.loaded["post_review_information_version"] = 1
        self.loaded["report"]["metadata"] = {"quote_validation_version": 1}
        self.loaded["report"]["findings"][0]["questions"] = ["实际需要几日验收？"]
        self.calls = []
        def runner(messages, config):
            self.calls.append((messages, config))
            return self.response(messages)
        self.jobs = Jobs(runner)
        self.facts = ContractFactsManager(Path(self.temp.name) / "facts")
        self.gate = PostReviewInformationManager(Path(self.temp.name) / "post-review", self.facts, self.jobs)
        self.facts.mutation_guard = self.gate.guard_mutation

    def response(self, messages):
        data = json.loads(messages[1]["content"])
        findings = [copy.deepcopy(f) for f in data["findings"] if f["id"] in data["affected_finding_ids"]]
        for f in findings:
            f.update(status="未检出", risk=None, reason="补充事实已明确验收安排。", questions=[])
        return json.dumps({"findings": findings, "group_results": []}, ensure_ascii=False)

    def body(self, status="answered", answer="十日"):
        gate = self.gate.get(self.loaded)
        return {"expected_revision": gate["revision"], "expected_facts_revision": gate["facts_revision"],
                "answers": [{"question_id": q["id"], "status": status, "answer": answer} for q in gate["questions"]],
                "consent": True, "engine_key": "stub", "config": {"model": "stub", "api_key": "test-secret-never-save"}}

    def done(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.gate.get(self.loaded)
            if state["status"] != "running":
                return state
            time.sleep(.01)
        self.fail("correction did not finish")

    def test_legacy_and_no_question_passthrough(self):
        legacy = copy.deepcopy(self.loaded)
        legacy.pop("post_review_information_version")
        self.assertFalse(self.gate.get(legacy)["enabled"])
        self.assertIs(self.gate.apply(legacy), legacy)
        self.loaded["report"]["findings"][0]["questions"] = []
        self.assertEqual(self.gate.get(self.loaded)["status"], "ready")
        self.assertEqual(self.gate.apply(self.loaded), self.loaded)
        self.assertEqual(self.calls, [])
        self.loaded["report"]["findings"][0].update(status="信息不足", issue_kind="信息")
        self.assertEqual(self.gate.get(self.loaded)["questions"], [])

    def test_unanswered_is_not_unknown_and_all_unknown_no_call(self):
        state = self.gate.get(self.loaded)
        self.assertEqual(state["questions"][0]["status"], "unanswered")
        with self.assertRaises(InformationGateRequired):
            self.gate.apply(self.loaded)
        payload = self.body("unknown", "")
        payload.pop("consent")
        ready, started = self.gate.submit(self.loaded, payload)
        self.assertFalse(started)
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(self.facts.get(self.loaded)["questions"][0]["response_recorded"])
        result = self.gate.apply(self.loaded)
        metadata = result["report"]["metadata"].pop("post_review_information")
        self.assertEqual(metadata["affected_finding_ids"], [])
        self.assertEqual(metadata["attempt"], 0)
        self.assertEqual(result, self.loaded)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.gate.submit(self.loaded, payload)[1])

    def test_answer_updates_only_linked_findings_and_keeps_original(self):
        before = copy.deepcopy(self.loaded)
        payload = self.body()
        self.assertTrue(self.gate.submit(self.loaded, payload)[1])
        self.assertEqual(self.done()["status"], "ready")
        result = self.gate.apply(self.loaded)
        self.assertEqual(result["report"]["findings"][1], before["report"]["findings"][1])
        self.assertEqual(result["report"]["findings"][0]["status"], "未检出")
        self.assertEqual(result["report"]["options"], [])
        self.assertEqual(before, self.loaded)
        self.assertFalse(self.gate.submit(self.loaded, payload)[1])
        self.assertEqual(len(self.calls), 1)
        self.assertIn("十日", json.dumps(self.facts.get(self.loaded), ensure_ascii=False))
        for path in Path(self.temp.name).rglob("*.json"):
            self.assertNotIn("test-secret-never-save", path.read_text(encoding="utf-8"))

    def test_dedup_maps_every_link_and_prep_unknown_not_reasked(self):
        self.loaded["report"]["findings"][1]["questions"] = ["实际需要几日验收? "]
        state = self.gate.get(self.loaded)
        self.assertEqual(len(state["questions"]), 1)
        self.assertEqual(state["questions"][0]["finding_ids"], ["F01", "F02"])
        self.loaded["report"]["preparation"] = {"questions": [{"id": "Q1", "question": "实际需要几日验收？", "status": "unknown", "answer": ""}]}
        self.assertEqual(self.gate.get(self.loaded)["questions"], [])

    def test_batch_invalid_never_commits_partial_facts(self):
        self.loaded["report"]["findings"][1]["questions"] = ["付款时间？"]
        payload = self.body()
        payload["answers"][1]["answer"] = ""
        with self.assertRaises(ValueError):
            self.gate.submit(self.loaded, payload)
        self.assertEqual(self.facts.get(self.loaded)["revision"], 0)
        self.assertEqual(self.gate.get(self.loaded)["status"], "needs_response")
        self.assertEqual(self.calls, [])
        payload = self.body()
        payload.pop("consent")
        with self.assertRaises(ValueError):
            self.gate.submit(self.loaded, payload)
        self.assertEqual(self.facts.get(self.loaded)["revision"], 0)

    def test_running_freezes_facts_duplicate_submit_and_deletion_state(self):
        entered, release = threading.Event(), threading.Event()
        def slow(messages, config):
            self.calls.append((messages, config))
            entered.set()
            release.wait(3)
            return self.response(messages)
        self.jobs.runner = slow
        payload = self.body()
        self.gate.submit(self.loaded, payload)
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(ContractFactsConflict):
                self.facts.save(self.loaded, {"expected_revision": 1, "text": "并发编辑"})
            self.assertFalse(self.gate.submit(self.loaded, payload)[1])
            with self.assertRaises(InformationGateRequired):
                self.gate.apply(self.loaded)
        finally:
            release.set()
        self.assertEqual(self.done()["status"], "ready")
        self.assertEqual(len(self.calls), 1)

    def test_out_of_scope_failure_persists_then_explicit_retry(self):
        self.jobs.runner = lambda messages, config: json.dumps({"findings": self.loaded["report"]["findings"]})
        self.gate.submit(self.loaded, self.body())
        state = self.done()
        self.assertEqual(state["status"], "failed")
        with self.assertRaises(ContractFactsConflict):
            self.facts.save(self.loaded, {"expected_revision": state["facts_revision"], "text": "冻结"})
        self.jobs.runner = lambda messages, config: self.response(messages)
        retry = {"expected_revision": state["revision"], "expected_facts_revision": state["facts_revision"],
                 "retry": True, "consent": True, "config": {"model": "stub"}}
        self.assertTrue(self.gate.submit(self.loaded, retry)[1])
        self.assertEqual(self.done()["status"], "ready")
        self.assertEqual(self.gate.get(self.loaded)["attempt"], 2)
        self.assertEqual(len(list(Path(self.temp.name).rglob("response.json"))), 2)

    def test_new_questions_go_central_without_second_gate(self):
        def runner(messages, config):
            data = json.loads(self.response(messages))
            data["findings"][0]["questions"] = ["后续能提供验收记录吗？"]
            return json.dumps(data, ensure_ascii=False)
        self.jobs.runner = runner
        self.gate.submit(self.loaded, self.body())
        self.assertEqual(self.done()["status"], "ready")
        self.assertEqual(len(self.facts.get(self.loaded)["questions"]), 2)
        self.assertEqual(self.gate.get(self.loaded)["questions"][0]["question"], "实际需要几日验收？")
        self.assertEqual(self.gate.apply(self.loaded)["report"]["findings"][0]["questions"], ["后续能提供验收记录吗？"])

    def test_original_projection_remains_fixed_after_later_fact_edit(self):
        self.gate.submit(self.loaded, self.body())
        self.assertEqual(self.done()["status"], "ready")
        before = self.gate.apply(self.loaded)
        state = self.facts.get(self.loaded)
        self.facts.save(self.loaded, {"expected_revision": state["revision"], "text": "以后又改变了"})
        self.assertEqual(before, self.gate.apply(self.loaded))
        self.assertEqual(len(self.calls), 1)

    def test_tampered_trace_or_overlay_is_not_delivered(self):
        self.gate.submit(self.loaded, self.body())
        self.assertEqual(self.done()["status"], "ready")
        path = self.gate._directory(self.loaded) / "attempt-1" / "overlay.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["findings"][0]["reason"] = "未经原始输出的改动"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ContractFactsCorrupt):
            self.gate.apply(self.loaded)

    def test_linked_group_parent_rollup_keeps_unknown_sibling(self):
        self.loaded["report"]["checklist_results"] = [{"check_id": "C1", "title": "检查", "status": "发现风险", "reason": "原始",
            "finding_ids": ["F01"], "groups": [
                {"group_id": "C1:G1", "status": "发现风险", "reason": "待处理", "finding_ids": ["F01"]},
                {"group_id": "C1:G2", "status": "信息不足", "reason": "其他信息未知", "finding_ids": []}]}]
        update = copy.deepcopy(self.loaded["report"]["findings"][0])
        update.update(status="未检出", reason="已解决")
        result = project_findings(self.loaded, [update], [{"group_id": "C1:G1", "status": "未检出", "reason": "已逐项确认", "finding_ids": ["F01"]}])
        row = result["report"]["checklist_results"][0]
        self.assertEqual(row["groups"][0]["status"], "未检出")
        self.assertEqual(row["groups"][1], self.loaded["report"]["checklist_results"][0]["groups"][1])
        self.assertEqual(row["status"], "信息不足")

    def test_batch_preserves_fact_shared_by_other_question(self):
        self.loaded["report"]["findings"][1]["questions"] = ["付款日期？"]
        state = self.facts._load(self.loaded)
        q1, q2 = state["questions"]
        first = self.facts.answer_batch(self.loaded, 0, [{"question_id": q1["id"], "status": "answered", "answer": "共享"}])
        state = self.facts._load(self.loaded)
        state["questions"][1].update(fact_id=first["questions"][0]["fact_id"], status="answered", answer="共享")
        self.facts._write(self.loaded, state)
        result = self.facts.answer_batch(self.loaded, 1, [{"question_id": q1["id"], "status": "answered", "answer": "新回答"}])
        old = next(f for f in result["facts"] if f["id"] == result["questions"][1]["fact_id"])
        self.assertTrue(old["active"])

    def test_failed_ready_write_does_not_advance_facts_and_retry_recovers(self):
        def with_question(messages, config):
            data = json.loads(self.response(messages))
            data["findings"][0]["questions"] = ["后续验收记录？"]
            return json.dumps(data, ensure_ascii=False)
        self.jobs.runner = with_question
        original_write = self.gate._write
        failed = []
        def fail_ready_once(loaded, state):
            if state["status"] == "ready" and not failed:
                failed.append(True)
                raise OSError("injected publication failure")
            return original_write(loaded, state)
        self.gate._write = fail_ready_once
        self.gate.submit(self.loaded, self.body())
        state = self.done()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(self.facts.get(self.loaded)["revision"], 1)
        self.assertEqual(len(self.facts.get(self.loaded)["questions"]), 1)
        self.gate.submit(self.loaded, {"expected_revision": state["revision"], "expected_facts_revision": 1,
                                     "retry": True, "consent": True, "config": {"model": "stub"}})
        self.assertEqual(self.done()["status"], "ready")
        result = self.gate.apply(self.loaded)
        self.assertEqual(len(self.facts.get(self.loaded)["questions"]), 2)
        self.assertEqual(result["report"]["metadata"]["post_review_information"]["facts_revision"], 2)
        self.facts.save(self.loaded, {"expected_revision": 2, "text": "后来修改的信息"})
        self.assertEqual(self.gate.apply(self.loaded)["report"]["metadata"]["post_review_information"]["facts_revision"], 2)

    def test_interrupted_question_merge_recovers_on_read_without_model(self):
        def with_question(messages, config):
            self.calls.append((messages, config))
            data = json.loads(self.response(messages))
            data["findings"][0]["questions"] = ["后续验收记录？"]
            return json.dumps(data, ensure_ascii=False)
        self.jobs.runner = with_question
        original_merge = self.facts.merge_questions
        failed = []
        def fail_merge_once(loaded):
            if not failed:
                failed.append(True)
                raise OSError("injected question merge failure")
            return original_merge(loaded)
        self.facts.merge_questions = fail_merge_once
        self.gate.submit(self.loaded, self.body())
        deadline = time.monotonic() + 3
        while self.gate._active and time.monotonic() < deadline:
            time.sleep(.01)
        with self.assertRaises(OSError):
            self.gate.get(self.loaded)
        self.assertEqual(self.gate.get(self.loaded)["status"], "ready")
        self.assertEqual(len(self.facts.get(self.loaded)["questions"]), 2)
        self.assertEqual(len(self.calls), 1)

    def test_catalog_isolates_one_damaged_flagged_report(self):
        from reader_server import ReaderServer
        rid = "job-" + "a" * 32
        catalog = {"documents": [{"id": "doc-one", "reviews": [{"id": rid, "available": False}]}], "pending_jobs": []}
        class CorruptJobs:
            root = Path("jobs")
            def _status(self, value):
                return {"post_review_information_version": 1, "report_id": rid}
            def load_report(self, value):
                from review_jobs import JobError
                raise JobError("bad original response digest")
        server = SimpleNamespace(operation_lock=threading.RLock(),
            deletions=SimpleNamespace(deleted_ids=lambda: set(), visible=lambda c: c),
            archive=SimpleNamespace(visible=lambda c: c, read=lambda: {"entries": []}), legacy_reports={}, project_root=Path(self.temp.name), state_dir=Path(self.temp.name),
            jobs=CorruptJobs(), delivery=SimpleNamespace(source_paths={}), information_gate=self.gate)
        with patch("reader_server.build_workbench", return_value=catalog):
            result = ReaderServer.workbench_catalog(server)
        row = result["documents"][0]["reviews"][0]
        self.assertFalse(row["available"])
        self.assertEqual(row["status"], "结果待核对")
        self.assertEqual(row["findings_count"], 0)

    def independent_groups(self):
        self.loaded["report"]["preparation"] = {"checks": [{"id": "C1", "title": "履行要求", "requirement": "验收安排及质保期限分别确认。",
            "groups": [{"group_id": "C1:G1", "title": "验收和质保", "requirement": "核对验收安排；另外确认质保期限。"},
                       {"group_id": "C1:G2", "title": "交付", "requirement": "核对交付期限。"}]}]}
        self.loaded["report"]["checklist_results"] = [{"check_id": "C1", "title": "履行要求", "status": "信息不足", "reason": "多个要求待确认。",
            "finding_ids": ["F01", "F02"], "groups": [
                {"group_id": "C1:G1", "title": "验收和质保", "status": "信息不足", "reason": "验收和质保待确认。", "finding_ids": ["F01"]},
                {"group_id": "C1:G2", "title": "交付", "status": "发现风险", "reason": "交付独立风险。", "finding_ids": ["F02"]}]}]

    def group_response(self, messages, *, status="信息不足", missing=False):
        data = json.loads(self.response(messages))
        data["group_results"] = [] if missing else [{"group_id": "C1:G1", "status": status,
            "reason": "验收已明确，但本组另有质保期限尚未确认。", "finding_ids": ["F01"]}]
        return json.dumps(data, ensure_ascii=False)

    def test_resolved_finding_does_not_pass_independent_group_remaining_requirements(self):
        self.independent_groups()
        untouched = copy.deepcopy(self.loaded["report"]["checklist_results"][0]["groups"][1])
        def runner(messages, config):
            self.calls.append((messages, config))
            packet = json.loads(messages[1]["content"])
            scopes = packet["affected_checklist_requirements"]
            self.assertEqual(scopes[0]["requirement"], "验收安排及质保期限分别确认。")
            self.assertEqual(scopes[0]["groups"][0]["requirement"], "核对验收安排；另外确认质保期限。")
            self.assertEqual(len(scopes[0]["groups"]), 1)
            self.assertIn("group_results", config["output_schema"]["required"])
            return self.group_response(messages)
        self.jobs.runner = runner
        self.gate.submit(self.loaded, self.body())
        self.assertEqual(self.done()["status"], "ready")
        result = self.gate.apply(self.loaded)
        self.assertEqual(result["report"]["findings"][0]["status"], "未检出")
        row = result["report"]["checklist_results"][0]
        self.assertEqual(row["groups"][0]["status"], "信息不足")
        self.assertIn("质保期限尚未确认", row["groups"][0]["reason"])
        self.assertEqual(row["groups"][1], untouched)
        self.assertEqual(row["status"], "发现风险")
        self.assertEqual(len(self.calls), 1)

    def test_missing_explicit_group_result_fails_and_only_explicit_retry_can_publish(self):
        self.independent_groups()
        def incomplete(messages, config):
            self.calls.append((messages, config))
            return self.group_response(messages, missing=True)
        self.jobs.runner = incomplete
        self.gate.submit(self.loaded, self.body())
        state = self.done()
        self.assertEqual(state["status"], "failed")
        with self.assertRaises(InformationGateRequired):
            self.gate.apply(self.loaded)
        self.assertEqual(self.loaded["report"]["checklist_results"][0]["groups"][0]["status"], "信息不足")
        self.assertEqual(len(self.calls), 1)
        self.jobs.runner = lambda messages, config: self.group_response(messages)
        self.gate.submit(self.loaded, {"expected_revision": state["revision"], "expected_facts_revision": state["facts_revision"],
            "retry": True, "consent": True, "config": {"model": "stub"}})
        self.assertEqual(self.done()["status"], "ready")
        self.assertEqual(self.gate.apply(self.loaded)["report"]["checklist_results"][0]["groups"][0]["status"], "信息不足")

    def test_group_result_scope_and_pending_link_consistency_are_validated(self):
        self.independent_groups()
        state = {"affected_ids": ["F01"]}
        packet = {"findings": [copy.deepcopy(self.loaded["report"]["findings"][0])], "group_results": [{
            "group_id": "C1:G1", "status": "未检出", "reason": "不应推定通过", "finding_ids": ["F01"]}]}
        packet["findings"][0]["status"] = "信息不足"
        with self.assertRaises(ValueError):
            self.gate._validate(self.loaded, state, json.dumps(packet))
        packet["group_results"][0]["finding_ids"] = []
        with self.assertRaises(ValueError):
            self.gate._validate(self.loaded, state, json.dumps(packet))
        packet["group_results"][0]["status"] = "信息不足"
        packet["group_results"][0]["finding_ids"] = ["F02"]
        with self.assertRaises(ValueError):
            self.gate._validate(self.loaded, state, json.dumps(packet))
        packet["group_results"][0]["finding_ids"] = ["F01"]
        packet["group_results"][0]["group_id"] = "C1:G2"
        with self.assertRaises(ValueError):
            self.gate._validate(self.loaded, state, json.dumps(packet))


class WidenedGateTests(unittest.TestCase):
    """Scope 2: an answer may revise any opinion it affects, each revision naming its answers."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.loaded = full_loaded()
        self.loaded["post_review_information_version"] = 1
        self.loaded["report"]["metadata"] = {"quote_validation_version": 1}
        self.loaded["report"]["findings"][0]["questions"] = ["实际需要几日验收？"]
        self.calls = []
        self.plan = {"extra": True, "basis_for_extra": True, "include_linked": True, "question_id": None}

        def runner(messages, config):
            self.calls.append((messages, config))
            return self.response(messages)
        self.jobs = Jobs(runner)
        self.facts = ContractFactsManager(Path(self.temp.name) / "facts")
        self.gate = PostReviewInformationManager(Path(self.temp.name) / "post-review", self.facts, self.jobs)
        self.facts.mutation_guard = self.gate.guard_mutation

    def response(self, messages):
        data = json.loads(messages[1]["content"])
        answered = data["answered_question_ids"]
        rows, basis = [], []
        for f in data["findings"]:
            linked = f["id"] in data["must_update_finding_ids"]
            if (linked and self.plan["include_linked"]) or (not linked and self.plan["extra"]):
                row = copy.deepcopy(f)
                row.update(reason="按补充的验收期限调整。", questions=[])
                rows.append(row)
                if linked or self.plan["basis_for_extra"]:
                    basis.append({"finding_id": f["id"], "question_ids": [self.plan["question_id"] or answered[0]],
                                  "reason": "验收期限定为十日后，" + ("验收安排" if linked else "尾款时点") + "随之调整。"})
        return json.dumps({"findings": rows, "group_results": [], "update_basis": basis}, ensure_ascii=False)

    def body(self):
        gate = self.gate.get(self.loaded)
        return {"expected_revision": gate["revision"], "expected_facts_revision": gate["facts_revision"],
                "answers": [{"question_id": q["id"], "status": "answered", "answer": "十日"} for q in gate["questions"]],
                "consent": True, "engine_key": "stub", "config": {"model": "stub", "api_key": "test-secret-never-save"}}

    def run_gate(self):
        self.assertTrue(self.gate.submit(self.loaded, self.body())[1])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.gate.get(self.loaded)
            if state["status"] != "running":
                return state
            time.sleep(.01)
        self.fail("correction did not finish")

    def test_answer_also_revises_another_affected_opinion_and_names_why(self):
        self.assertEqual(self.run_gate()["status"], "ready")
        packet = json.loads(self.calls[0][0][1]["content"])
        self.assertEqual(packet["must_update_finding_ids"], ["F01"])
        self.assertEqual([f["id"] for f in packet["findings"]], ["F01", "F02"])
        self.assertEqual(len(packet["answered_question_ids"]), 1)
        result = self.gate.apply(self.loaded)
        updated = {f["id"]: f for f in result["report"]["findings"]}
        for fid in ("F01", "F02"):
            self.assertEqual(updated[fid]["reason"], "按补充的验收期限调整。")
            self.assertEqual(updated[fid]["post_review_update"]["questions"], ["实际需要几日验收？"])
        self.assertIn("尾款时点", updated["F02"]["post_review_update"]["reason"])
        self.assertEqual(result["report"]["metadata"]["post_review_information"]["affected_finding_ids"], ["F01", "F02"])
        # Reading again replays the saved request and response without another model call.
        self.assertEqual(self.gate.apply(self.loaded), result)
        self.assertEqual(len(self.calls), 1)

    def test_unaffected_opinion_may_stay_untouched(self):
        self.plan["extra"] = False
        self.assertEqual(self.run_gate()["status"], "ready")
        result = self.gate.apply(self.loaded)
        self.assertNotIn("post_review_update", result["report"]["findings"][1])
        self.assertEqual(result["report"]["metadata"]["post_review_information"]["affected_finding_ids"], ["F01"])

    def test_revision_without_named_answer_is_not_delivered(self):
        self.plan["basis_for_extra"] = False
        self.assertEqual(self.run_gate()["status"], "failed")
        with self.assertRaises(InformationGateRequired):
            self.gate.apply(self.loaded)

    def test_basis_citing_an_unanswered_question_is_not_delivered(self):
        self.plan["question_id"] = "question-not-in-this-round"
        self.assertEqual(self.run_gate()["status"], "failed")

    def test_skipping_the_asking_opinion_is_not_delivered(self):
        self.plan["include_linked"] = False
        self.assertEqual(self.run_gate()["status"], "failed")
