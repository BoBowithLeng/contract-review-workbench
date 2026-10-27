from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from contract_facts import facts_digest
from draft_preparation import DraftPreparationManager, DraftPreparationConflict, DraftPreparationInput
from review_runner import output_schema
from contract_core import parse_docx
from tests.test_draft_preparation import FakeDelivery, loaded, model_json, wait_done, actions


class DraftPlanDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "drafts"
        self.pieces = {"P001": "第一期款旧文。", "P002": "另行约定。"}
        self.loaded = loaded(self.pieces, [("P001", self.pieces["P001"])], draft="第一期款新文。")
        self.delivery = FakeDelivery(self.pieces)
        self.facts = {"revision": 0, "facts": [], "questions": []}
        self.calls = []
        self.manager = DraftPreparationManager(self.root, self.delivery,
            completion=lambda config, messages: self.calls.append((config, messages)),
            facts_provider=lambda _: copy.deepcopy(self.facts))

    def plan(self, status="ready"):
        self.loaded["report"]["findings"][0]["draft_plan"] = {
            "status": status,
            "edits": [{"piece_id": "P001", "new_text": "第一期款新文。"}] if status == "ready" else [],
            "insertions": [], "paragraph_actions": actions("P001", action=(
                "modify" if status == "ready" else "retain")),
            "questions": ["请确认支付节点。"] if status == "needs_input" else [],
            "comment": "请确认本次约定的支付节点。",
        }

    def test_first_review_projection_is_readonly_and_ready_is_source_bound(self):
        self.plan()
        candidate = self.manager.get(self.loaded, "F03")
        self.assertEqual(candidate, self.manager.get(self.loaded, "F03"))
        self.assertTrue(candidate["coverage_complete"])
        self.assertEqual(candidate["edits"][0]["old_text"], self.pieces["P001"])
        self.assertEqual(candidate["edits"][0]["end"], len(self.pieces["P001"]))
        self.assertEqual(candidate["edits"][0]["paragraph_sha256"], self.delivery.pieces[0]["paragraph_sha256"])
        self.assertEqual(candidate["engine_label"], "初审结构化改稿")
        self.assertFalse(self.root.exists())
        self.assertEqual(self.calls, [])
        source = self.delivery.describe(self.loaded)["source"]
        state = self.delivery.describe(self.loaded)["state"]
        self.assertEqual(candidate, self.manager.resolve_plan_confirmation(
            self.loaded, "F03", candidate["candidate_id"], source, state))

    def test_pending_and_no_edit_plans_cannot_be_confirmed(self):
        for status in ("needs_input", "not_needed"):
            with self.subTest(status=status):
                self.plan(status)
                candidate = self.manager.get(self.loaded, "F03")
                self.assertEqual(candidate["status"], status)
                self.assertIsNone(candidate["candidate_id"])
                self.assertFalse(candidate["coverage_complete"])
        self.assertEqual(self.calls, [])
        self.assertFalse(self.root.exists())

    def test_existing_paragraph_change_is_never_overwritten(self):
        self.plan()
        self.delivery.edits = [{"piece_id": "P001", "new_text": "另一个已确认改动。"}]
        with self.assertRaisesRegex(DraftPreparationConflict, "已有确认修改"):
            self.manager.get(self.loaded, "F03")

    def test_new_unlinked_fact_keeps_initial_plan_usable_without_reminder(self):
        self.plan()
        self.facts["revision"] = 1
        self.facts["facts"] = [{"id": "fact-" + "a" * 32, "text": "款项已支付。", "active": True,
                                "source": {"kind": "manual", "report_id": self.loaded["id"], "quote": "款项已支付。"}}]
        candidate = self.manager.get(self.loaded, "F03")
        self.assertEqual(candidate["status"], "ready")
        self.assertEqual(candidate["fact_notice"], [])
        self.assertEqual(self.calls, [])

    def test_answer_tied_to_this_opinion_is_a_reminder_not_a_lock(self):
        self.plan()
        fact = {"id": "fact-" + "b" * 32, "text": "合同结束后数据如何处理？\n答：需要返还", "active": True,
                "source": {"kind": "answer", "report_id": self.loaded["id"], "quote": "需要返还", "question_id": "Q09"}}
        self.facts.update(revision=1, facts=[fact],
                          questions=[{"id": "Q09", "status": "answered", "question": "合同结束后数据如何处理？", "finding_ids": ["F03"]}])
        candidate = self.manager.get(self.loaded, "F03")
        self.assertEqual(candidate["status"], "ready")
        self.assertEqual(candidate["fact_notice"], ["合同结束后数据如何处理？ 答：需要返还"])
        source = self.delivery.describe(self.loaded)["source"]
        state = self.delivery.describe(self.loaded)["state"]
        self.assertEqual(candidate, self.manager.resolve_plan_confirmation(
            self.loaded, "F03", candidate["candidate_id"], source, state))
        # The same answer asked by another opinion leaves this one without a reminder.
        self.facts["questions"][0]["finding_ids"] = ["F07"]
        self.assertEqual(self.manager.get(self.loaded, "F03")["fact_notice"], [])
        # A withdrawn answer that had been part of the draft's basis is named as withdrawn.
        self.facts["questions"][0]["finding_ids"] = ["F03"]
        self.loaded["report"]["metadata"] = {
            "post_review_information": {"version": 1},
            "draft_plan_fact_basis": {"source": "post_review_information_publication", "facts_revision": 1,
                                      "facts": [fact]}}
        self.facts.update(revision=2, facts=[dict(fact, active=False)])
        self.assertEqual(self.manager.get(self.loaded, "F03")["fact_notice"],
                         ["已撤回：合同结束后数据如何处理？ 答：需要返还"])
        self.assertEqual(self.calls, [])

    def test_question_initialization_is_not_a_changed_initial_business_fact(self):
        self.plan()
        self.facts.update(revision=1, questions=[{"id": "Q01", "status": "pending", "question": "支付节点？"}])
        self.assertEqual(self.manager.get(self.loaded, "F03")["status"], "ready")

    def test_updated_plan_v1_basis_ignores_question_only_changes_but_candidate_expires(self):
        self.plan()
        self.loaded["report"]["findings"][0]["draft_plan_basis"] = {
            "facts_revision": 0, "facts_digest": facts_digest(self.facts, version=1),
            "facts_digest_version": 1, "delivery_revision": 0}
        before = self.manager.get(self.loaded, "F03")
        self.facts.update(revision=1, questions=[{"id": "Q01", "status": "pending", "question": "支付节点？"}])
        self.assertEqual(self.manager.get(self.loaded, "F03")["status"], "ready")
        with self.assertRaises(DraftPreparationConflict):
            self.manager.get(self.loaded, "F03", before["candidate_id"])

    def test_revised_plan_stays_usable_after_edits_elsewhere(self):
        # A revised opinion written at delivery revision 0; the contract changed
        # elsewhere since. Its own paragraphs are free, so no new model call.
        self.plan()
        self.loaded["report"]["findings"][0]["draft_plan_basis"] = {
            "facts_revision": 0, "facts_digest": facts_digest(self.facts, version=1),
            "facts_digest_version": 1, "delivery_revision": 0}
        self.delivery.revision = 1
        candidate = self.manager.get(self.loaded, "F03")
        self.assertEqual(candidate["status"], "ready")
        self.assertIn("合同其他地方", candidate["contract_notice"])
        self.assertEqual(self.calls, [])

    def test_delivery_and_opinion_versions_expire_preview_without_calling_model(self):
        self.plan()
        before = self.manager.get(self.loaded, "F03")
        self.delivery.revision = 1
        with self.assertRaises(DraftPreparationConflict):
            self.manager.get(self.loaded, "F03", before["candidate_id"])
        self.delivery.revision = 0
        self.loaded["opinion_revisions"] = {"F03": 1}
        with self.assertRaises(DraftPreparationConflict):
            self.manager.get(self.loaded, "F03", before["candidate_id"])
        self.assertEqual(self.calls, [])

    def api_payload(self):
        return {"source_sha256": self.delivery.sha, "expected_revision": 0,
                "draft_text": "第一期款新文。", "expected_opinion_revision": 0, "expected_facts_revision": 0,
                "config": {"base_url": "https://model.example/v1", "api_key": "unique-private-test-key", "model": "Configured Model"},
                "consent": True}

    def test_legacy_uses_explicit_api_and_saves_no_secret_and_current_context(self):
        payload = self.api_payload()
        def completion(config, messages):
            self.calls.append((config, messages))
            return model_json({"status": "ready", "edits": [{"piece_id": "P001", "new_text": "第一期款新文。"}],
                "questions": [], "comment": "请确认支付节点。", "paragraph_actions": actions("P001")}, messages)
        self.manager.completion = completion
        self.manager.source_context_provider = lambda *_: {"current_contract": [{"id": "P001", "text": "第一期款旧文。"}],
                                                          "facts_state": self.facts}
        started, fresh = self.manager.start(self.loaded, "F03", payload)
        self.assertTrue(fresh)
        candidate = wait_done(self.manager, self.loaded, candidate_id=started["candidate_id"])
        self.assertEqual(candidate["engine_label"], "API · Configured Model")
        self.assertEqual(candidate["status"], "ready")
        self.assertEqual(len(self.calls), 1)
        data = json.loads(self.calls[0][1][1]["content"].split("UNTRUSTED DATA:\n", 1)[1])
        schema = data["required_schema"]
        self.assertEqual(schema, output_schema("draft_preparation"))
        self.assertEqual(set(schema["required"]), {
            "status", "edits", "insertions", "questions", "comment", "paragraph_actions", "suggestion_actions"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]["suggestion_actions"]["items"]["required"]),
                         {"unit_id", "action", "edit_piece_ids", "insertion_indices", "reason"})
        self.assertEqual(set(schema["properties"]["paragraph_actions"]["items"]["required"]),
                         {"piece_id", "action", "reason"})
        self.assertIn("CONFIRMED_FACTS", data)
        self.assertEqual(data["contract_context"][0]["text"], "第一期款旧文。")
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(payload["config"]["api_key"], path.read_text(encoding="utf-8"))
        self.facts["revision"] = 1
        with self.assertRaises(DraftPreparationConflict):
            self.manager.get(self.loaded, "F03", candidate["candidate_id"])

    def test_legacy_default_does_not_launch_implicit_local_runner(self):
        payload = self.api_payload()
        payload.pop("config")
        payload.pop("consent")
        with self.assertRaises(DraftPreparationInput):
            self.manager.start(self.loaded, "F03", payload)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.root.exists())

    def test_legacy_drafting_can_use_numbers_from_already_confirmed_facts(self):
        self.facts["facts"] = [{"id": "fact-" + "a" * 32, "text": "第一期款在60日内支付。", "active": True}]
        def completion(config, messages):
            self.calls.append((config, messages))
            return model_json({"status": "ready", "edits": [{"piece_id": "P001", "new_text": "第一期款在60日内支付。"}],
                "questions": [], "comment": "请确认支付节点。", "paragraph_actions": actions("P001")}, messages)
        self.manager.completion = completion
        started, _ = self.manager.start(self.loaded, "F03", self.api_payload())
        candidate = wait_done(self.manager, self.loaded, candidate_id=started["candidate_id"])
        self.assertEqual(candidate["status"], "ready", candidate)
        self.assertEqual(candidate["questions"], [])
        self.assertIn("60日", self.calls[0][1][1]["content"])

    def test_failed_new_protocol_never_falls_back_to_legacy_model(self):
        for marker in ("finding", "context"):
            with self.subTest(marker=marker):
                current = copy.deepcopy(self.loaded)
                if marker == "finding":
                    current["report"]["findings"][0]["draft_plan_version"] = 1
                else:
                    current["report"]["context"] = {"draft_plan_version": 1}
                result = self.manager.get(current, "F03")
                self.assertEqual(result["status"], "failed")
                self.assertIsNone(result["candidate_id"])
                with self.assertRaises(DraftPreparationConflict):
                    self.manager.start(current, "F03", self.api_payload())
        self.assertFalse(self.root.exists())
        self.assertEqual(self.calls, [])


class PublishedInitialPlanHTTPTests(unittest.TestCase):
    def test_unaffected_initial_plan_survives_publication_and_later_fact_changes(self):
        from tests.test_structured_draft_http import StructuredDraftHTTPTests
        fixture = StructuredDraftHTTPTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.needs_input = True
        original_runner = fixture.f.server.jobs.runner
        untouched_text = "8.1 未尽事项由双方书面协商。"

        def runner(messages, config):
            result = json.loads(original_runner(messages, config))
            if config.get("review_phase", "review") == "review":
                unaffected = fixture.finding(False)
                unaffected.update(id="F02", rule_id="G02", theme="补充约定", title="补充约定采用书面方式",
                    refs=[{"id": "P002"}], questions=[], suggestion="补充约定采用书面方式。")
                unaffected["draft_plan"] = {"status": "ready",
                    "edits": [{"piece_id": "P002", "new_text": untouched_text}], "insertions": [],
                    "paragraph_actions": [{"piece_id": "P002", "action": "modify", "reason": "明确补充约定采用书面形式。"}],
                    "questions": [], "comment": "请确认补充约定的书面形式。"}
                result["findings"].append(unaffected)
                result["checks"].append({"rule_id": "G02", "title": "补充约定", "status": "发现风险", "reason": "书面约定。"})
            return json.dumps(result, ensure_ascii=False)

        fixture.f.server.jobs.runner = runner
        jid = fixture.initial()
        fixture.answer(jid, "收到发票再付款，通知使用双方指定邮箱。")
        endpoint = "/api/reports/" + jid
        report = fixture.f.request(endpoint)
        finding = next(f for f in report["report"]["findings"] if f["id"] == "F02")
        self.assertNotIn("draft_plan_basis", finding)
        metadata = report["report"]["metadata"]
        self.assertEqual(metadata["post_review_information"]["affected_finding_ids"], ["F01"])
        basis = metadata["draft_plan_fact_basis"]
        self.assertEqual(basis["source"], "post_review_information_publication")
        self.assertIn("收到发票再付款", json.dumps(basis["facts"], ensure_ascii=False))
        phases = list(fixture.phases)
        candidate = fixture.f.request(endpoint + "/drafts/F02")
        self.assertEqual(candidate["status"], "ready", candidate)
        delivery = fixture.f.request(endpoint + "/delivery")
        edits = [{k: v for k, v in dict(e, id="unaffected-initial", confirmed=True, finding_ids=["F02"]).items()
                  if k != "location"} for e in candidate["edits"]]
        saved = fixture.f.request(endpoint + "/delivery", {
            "expected_revision": delivery["state"]["revision"], "source_sha256": delivery["source"]["source_sha256"],
            "author": "合成验收", "edits": edits, "comments": [],
            "draft_confirmation": {"finding_id": "F02", "candidate_id": candidate["candidate_id"]}})["state"]
        self.assertTrue(saved["draft_confirmations"]["F02"]["coverage_complete"])
        status, raw = fixture.raw(endpoint + "/delivery/export", {
            "expected_revision": saved["revision"], "source_sha256": delivery["source"]["source_sha256"],
            "review_revision": report["state"]["revision"], "variants": ["clean"]})
        self.assertEqual(status, 200, raw[:1000])
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            clean = package.read(next(name for name in package.namelist() if "洁净版" in name))
        self.assertIn(untouched_text, [p.text for p in parse_docx(clean).pieces])
        facts = fixture.f.request(endpoint + "/facts")["facts"]
        fixture.f.request(endpoint + "/facts/save", {"expected_revision": facts["revision"], "text": "支付节点后来已再次变更。"})
        status, raw = fixture.raw(endpoint + "/drafts/F02")
        # The opinion's own confirmed edit does not block its plan (a newer version
        # replaces it on confirmation), and the later fact itself locks nothing.
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["status"], "ready")
        current = fixture.f.request(endpoint + "/delivery")["state"]
        status, raw = fixture.raw(endpoint + "/delivery/export", {
            "expected_revision": current["revision"], "source_sha256": delivery["source"]["source_sha256"],
            "review_revision": report["state"]["revision"], "variants": ["clean"]})
        self.assertEqual(status, 200, raw[:1000])
        self.assertEqual(fixture.phases, phases, "Unchanged initial-plan preview and confirmation must not call a model")


if __name__ == "__main__":
    unittest.main()
