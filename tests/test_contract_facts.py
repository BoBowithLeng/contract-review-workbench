from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from clause_discussion import ClauseDiscussionManager, _fact_candidates, _split_fact_response
from contract_facts import (
    ContractFactsConflict, ContractFactsCorrupt, ContractFactsManager, effective_facts, sanitize_fact_history,
)
from opinion_revisions import OpinionRevisionConflict, OpinionRevisionManager
from tests.test_clause_discussion import FakeDelivery, make_loaded, request_body, wait_done
from tests.test_opinion_revisions import answer as opinion_answer, body as opinion_body, full_loaded, wait_revision


def candidate(loaded, text, *, index=1, kind="fact", replace_id=None):
    turn_id = "turn-" + f"{index:032x}"
    item = {"text": text, "source_quote": text, "kind": kind}
    if replace_id:
        item["replace_id"] = replace_id
    return _fact_candidates([item], text, turn_id, {
        "report_id": loaded["id"], "finding_id": "F01", "session_id": "initial",
    })[0]


def suffix(items):
    return "答复仍然可读。\n<contract-fact-candidates>\n" + json.dumps(items, ensure_ascii=False) + "\n</contract-fact-candidates>"


class ContractFactsTests(unittest.TestCase):
    def test_unknown_requires_explicit_response_and_withdrawal_reopens_question(self):
        self.loaded['report']['findings'][0]['questions'] = ['服务器由谁提供？']
        q = self.manager.get(self.loaded)['questions'][0]
        self.assertFalse(q.get('response_recorded', False))
        payload = dict(expected_revision=0, question_id=q['id'], status='unknown', answer='')
        first = self.manager.answer(self.loaded, payload)
        self.assertTrue(first['questions'][0]['response_recorded'])
        self.assertEqual(first['revision'], 1)
        again = self.manager.answer(self.loaded, dict(payload, expected_revision=1))
        self.assertEqual(again['revision'], 1)
        answered = self.manager.answer(self.loaded, dict(payload, expected_revision=1, status='answered', answer='开发方'))
        withdrawn = self.manager.withdraw(self.loaded, dict(expected_revision=answered['revision'], fact_id=answered['questions'][0]['fact_id']))
        self.assertFalse(withdrawn['questions'][0]['response_recorded'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.loaded = make_loaded()
        self.manager = ContractFactsManager(self.root / "facts")

    def confirm(self, item, *, revision=0, replace=None):
        payload = {"expected_revision": revision, "candidate_id": item["id"]}
        if replace:
            payload["replace_id"] = replace
        return self.manager.confirm(self.loaded, payload, item)

    def test_legacy_seeds_are_read_only_and_preserve_question_context(self):
        self.loaded["report"]["context"]["business_facts"] = "本项目实际测试至少需要七日。"
        self.loaded["report"]["preparation"] = {"questions": [
            {"id": "Q01", "question": "需在活动前启用吗？", "answer": "是", "status": "answered"},
            {"id": "Q02", "question": "活动日？", "answer": "不知道", "status": "unknown"},
        ]}
        before = copy.deepcopy(self.loaded)
        state = self.manager.get(self.loaded)
        self.assertEqual(state["revision"], 0)
        self.assertEqual(len(state["facts"]), 2)
        self.assertEqual(state["facts"][1]["text"], "需在活动前启用吗？\n答：是")
        self.assertEqual(state["facts"][1]["source"]["quote"], "是")
        self.assertFalse((self.root / "facts").exists())
        self.assertEqual(self.loaded, before)
        self.loaded["report"]["context"]["business_facts"] = "事实" * 6_000
        self.loaded["report"]["preparation"]["questions"][0].update(question="问题" * 3_000, answer="答复" * 6_000)
        long = self.manager.get(self.loaded)
        self.assertEqual(len(long["facts"][0]["text"]), 12_000)
        self.assertGreater(len(long["facts"][1]["text"]), 18_000)

    def test_manual_seed_correction_withdrawal_and_revision_binding(self):
        self.loaded["report"]["context"]["business_facts"] = "旧业务事实"
        original = copy.deepcopy(self.loaded)
        state = self.manager.get(self.loaded)
        seed = state["facts"][0]
        revised = self.manager.save(self.loaded, {"expected_revision": 0, "text": "新业务事实", "replace_id": seed["id"]})
        self.assertTrue(revised["has_updates_since_review"])
        self.assertEqual(seed["source"]["quote"], "旧业务事实")
        self.assertEqual([f["text"] for f in effective_facts(revised)], ["新业务事实"])
        with self.assertRaises(ContractFactsConflict):
            self.manager.save(self.loaded, {"expected_revision": 0, "text": "并发编辑"})
        current = self.manager.withdraw(self.loaded, {"expected_revision": 1, "fact_id": effective_facts(revised)[0]["id"]})
        self.assertEqual(effective_facts(current), [])
        self.assertEqual(self.loaded, original)
        rebound = copy.deepcopy(self.loaded)
        rebound["contract"]["fingerprint"] = "e" * 64
        with self.assertRaises(ContractFactsConflict):
            self.manager.get(rebound)

    def test_question_answer_dedup_correction_and_inheritance_do_not_revive_old_question(self):
        self.loaded["report"]["preparation"] = {"questions": [
            {"id": "Q01", "question": "项目日期？", "answer": "", "status": "unknown", "required": True}]}
        self.loaded["report"]["findings"][0]["questions"] = ["项目日期?"]
        state = self.manager.get(self.loaded)
        self.assertEqual(len(state["questions"]), 1)
        q = state["questions"][0]
        self.assertEqual(q["finding_id"], "F01")
        payload = {"expected_revision": 0, "question_id": q["id"], "status": "answered", "answer": "10月5日"}
        answered = self.manager.answer(self.loaded, payload)
        self.assertEqual(answered["questions"][0]["status"], "answered")
        self.assertEqual(self.manager.answer(self.loaded, {**payload, "expected_revision": 1})["revision"], 1)
        self.assertEqual(len(effective_facts(answered)), 1)
        updated = self.manager.answer(self.loaded, {**payload, "expected_revision": 1, "answer": "10月6日"})
        self.assertEqual([f["source"]["quote"] for f in effective_facts(updated)], ["10月6日"])
        self.assertEqual(sanitize_fact_history({"questions": ["项目日期？"]}, updated)["questions"], [])
        child = copy.deepcopy(self.loaded)
        child["id"] = "minimal"
        child["request_digest"] = "d" * 64
        child["report"]["confirmed_facts"] = {"revision": updated["revision"], "facts": effective_facts(updated), "questions": updated["questions"]}
        inherited = self.manager.get(child)
        self.assertEqual(inherited["revision"], 0)
        self.assertFalse(inherited["has_updates_since_review"])
        self.assertEqual(len(inherited["questions"]), 1)
        self.assertEqual(inherited["questions"][0]["answer"], "10月6日")
        self.assertEqual(effective_facts(inherited)[0]["source"]["quote"], "10月6日")

    def test_inherited_question_rebinds_to_child_finding_and_preserves_parent_source(self):
        self.loaded["report"]["findings"][0]["questions"] = ["测试需要几日？"]
        parent = self.manager.get(self.loaded)
        q = parent["questions"][0]
        self.assertEqual(q["finding_id"], "F01")
        child = copy.deepcopy(self.loaded)
        child["id"] = "minimal"
        child["request_digest"] = "d" * 64
        child["report"]["confirmed_facts"] = {"revision": parent["revision"], "facts": effective_facts(parent), "questions": parent["questions"]}
        child["report"]["findings"] = [
            {"id": "F01", "title": "与测试无关的付款风险", "questions": []},
            {"id": "F03", "title": "测试期限", "questions": ["测试需要几日?"]},
        ]
        before = copy.deepcopy(child)
        current = self.manager.get(child)
        self.assertEqual(len(current["questions"]), 1)
        self.assertEqual(current["questions"][0]["id"], q["id"])
        self.assertEqual(current["questions"][0]["finding_id"], "F03")
        self.assertEqual(current["questions"][0]["source"], q["source"])
        self.assertEqual(current["questions"][0]["source"]["finding_id"], "F01")
        self.assertEqual(current["questions"][0]["source"]["report_id"], self.loaded["id"])
        self.assertEqual(child, before)
        self.assertEqual(self.manager.get(self.loaded)["questions"][0]["finding_id"], "F01")

    def test_inherited_historical_question_does_not_link_to_reused_unrelated_finding_id(self):
        self.loaded["report"]["findings"][0]["questions"] = ["测试需要几日？"]
        parent = self.manager.get(self.loaded)
        q = parent["questions"][0]
        child = copy.deepcopy(self.loaded)
        child["id"] = "minimal"
        child["request_digest"] = "d" * 64
        child["report"]["confirmed_facts"] = {"revision": 0, "facts": [], "questions": parent["questions"]}
        child["report"]["findings"] = [{"id": "F01", "title": "与测试无关的付款风险", "questions": []}]
        current = self.manager.get(child)
        self.assertEqual(current["questions"][0]["finding_id"], None)
        self.assertEqual(current["questions"][0]["source"], q["source"])
        answered = self.manager.answer(child, {"expected_revision": 0, "question_id": q["id"], "status": "answered", "answer": "十日"})
        self.assertEqual(answered["questions"][0]["status"], "answered")
        self.assertEqual(answered["questions"][0]["finding_id"], None)
        self.assertEqual(len(effective_facts(answered)), 1)
        self.assertEqual(self.manager.get(child)["questions"][0]["finding_id"], None)

    def test_legacy_sidecar_migrates_on_first_mutation_without_reset(self):
        item = candidate(self.loaded, "既有讨论事实")
        state = self.confirm(item)
        path = self.manager._path(self.loaded)
        old = json.loads(path.read_text(encoding="utf-8"))
        old.update(schema_version=1)
        old.pop("questions")
        path.write_text(json.dumps(old), encoding="utf-8")
        current = self.manager.get(self.loaded)
        self.assertEqual(current["revision"], state["revision"])
        self.assertEqual(current["facts"], state["facts"])
        self.manager.save(self.loaded, {"expected_revision": 1, "text": "手动补充"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 2)

    def test_correcting_an_answer_keeps_the_question_beside_it(self):
        self.loaded["report"]["preparation"] = {"questions": [
            {"id": "Q01", "question": "需要几日？", "answer": "七日", "status": "answered"}]}
        initial = self.manager.get(self.loaded)
        revised = self.manager.save(self.loaded, {"expected_revision": 0, "text": "需要十日",
                                                  "replace_id": initial["facts"][0]["id"]})
        fact = effective_facts(revised)[0]
        self.assertEqual(fact["text"], "需要几日？\n答：需要十日")
        self.assertEqual(fact["source"]["quote"], "需要十日")
        self.assertEqual(revised["questions"][0]["answer"], "需要十日")
        # An editor that still sends the question along is not prefixed twice.
        again = self.manager.save(self.loaded, {"expected_revision": 1, "text": "需要几日？\n答：需要十二日",
                                                "replace_id": fact["id"]})
        self.assertEqual(effective_facts(again)[0]["text"], "需要几日？\n答：需要十二日")
        self.assertEqual(again["questions"][0]["answer"], "需要十二日")
        unchanged = self.manager.save(self.loaded, {"expected_revision": 2, "text": "需要十二日",
                                                    "replace_id": effective_facts(again)[0]["id"]})
        self.assertEqual(unchanged["revision"], 2)

    def test_legacy_sidecar_correction_does_not_reseed_old_preparation_answer(self):
        self.loaded["report"]["preparation"] = {"questions": [
            {"id": "Q01", "question": "需要几日？", "answer": "七日", "status": "answered"}]}
        initial = self.manager.get(self.loaded)
        revised = self.manager.save(self.loaded, {"expected_revision": 0, "text": "需要十日", "replace_id": initial["facts"][0]["id"]})
        path = self.manager._path(self.loaded)
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy.pop("questions")
        legacy["schema_version"] = 1
        path.write_text(json.dumps(legacy), encoding="utf-8")
        current = self.manager.get(self.loaded)
        self.assertEqual(current["questions"][0]["answer"], "需要十日")
        self.assertEqual(current["questions"][0]["fact_id"], effective_facts(revised)[0]["id"])

    def test_discussion_confirm_answers_the_bound_question_without_duplicate_record(self):
        self.loaded["report"]["findings"][0]["questions"] = ["测试需几日？"]
        q = self.manager.get(self.loaded)["questions"][0]
        text = "实际测试需要十日。"
        item = _fact_candidates([{"text": text, "source_quote": text, "kind": "fact", "question_id": q["id"]}], text,
            "turn-" + "7" * 32, {"report_id": self.loaded["id"], "finding_id": "F01", "session_id": "initial"},
            question_ids={q["id"]})[0]
        state = self.confirm(item)
        self.assertEqual(len(state["questions"]), 1)
        self.assertEqual(state["questions"][0]["status"], "answered")
        self.assertEqual(state["questions"][0]["fact_id"], effective_facts(state)[0]["id"])
        self.assertEqual(state["questions"][0]["answer"], text)
        self.assertEqual(self.confirm(item)["revision"], 1)
        forged = _fact_candidates([{"text": text, "source_quote": text, "kind": "fact", "question_id": q["id"]}], text,
            "turn-" + "8" * 32, {"report_id": self.loaded["id"], "finding_id": "F02", "session_id": "initial"})[0]
        with self.assertRaises(ValueError):
            self.confirm(forged, revision=1)
        self.assertEqual(_fact_candidates([{"text": text, "source_quote": text, "kind": "fact", "question_id": "question-" + "f" * 32}], text,
            "turn-" + "9" * 32, question_ids={q["id"]}), [])

    def test_confirm_repeat_restart_and_exact_duplicate_are_idempotent(self):
        item = candidate(self.loaded, "本项目实际测试至少需要七日。")
        state = self.confirm(item)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(self.confirm(item), state)  # old expected_revision is still accepted for the same action
        restarted = ContractFactsManager(self.root / "facts")
        self.assertEqual(restarted.get(self.loaded), state)
        second = candidate(self.loaded, item["text"], index=2)
        duplicate = self.confirm(second, revision=1)
        self.assertEqual(len(duplicate["facts"]), 1)
        self.assertEqual(len(duplicate["confirmations"]), 2)
        self.assertEqual(duplicate["revision"], 2)

    def test_correction_retains_history_but_only_new_value_is_effective(self):
        old = candidate(self.loaded, "本项目实际测试至少需要七日。")
        first = self.confirm(old)
        old_id = first["facts"][0]["id"]
        new = candidate(self.loaded, "本项目实际测试至少需要十个工作日。", index=2, kind="correction", replace_id=old_id)
        with self.assertRaisesRegex(ValueError, "明确选择"):
            self.confirm(new, revision=1)
        second = self.confirm(new, revision=1, replace=old_id)
        self.assertEqual([item["text"] for item in effective_facts(second)], [new["text"]])
        self.assertFalse(second["facts"][0]["active"])
        self.assertEqual(second["facts"][0]["superseded_by"], second["facts"][1]["id"])
        self.assertEqual(self.confirm(new, revision=0, replace=old_id), second)
        with self.assertRaises(ContractFactsConflict):
            self.confirm(candidate(self.loaded, "本项目实际测试至少需要十五日。", index=3), revision=2, replace=old_id)
        history = sanitize_fact_history("前次说本项目实际测试至少需要七日。", second)
        self.assertNotIn(old["text"], history)
        self.assertIn("此前信息已更正", history)

    def test_confirmation_cannot_rebind_or_take_browser_text(self):
        item = candidate(self.loaded, "本项目实际测试至少需要七日。")
        invalid = copy.deepcopy(item)
        invalid["source"]["report_id"] = "another-report"
        with self.assertRaises(ValueError):
            self.confirm(invalid)
        invalid = copy.deepcopy(item)
        invalid["source"]["quote"] = "模型建议"
        with self.assertRaises(ValueError):
            self.confirm(invalid)
        with self.assertRaises(ValueError):
            self.manager.confirm(self.loaded, {"expected_revision": 0, "candidate_id": item["id"], "text": "浏览器正文"}, item)

    def test_stale_revision_and_shared_root_parallel_confirmation(self):
        barrier = threading.Barrier(2)
        outcomes = []
        second = ContractFactsManager(self.root / "facts")

        def confirm(manager, number):
            item = candidate(self.loaded, f"项目已安排{number}个测试人员。", index=number)
            barrier.wait()
            try:
                manager.confirm(self.loaded, {"expected_revision": 0, "candidate_id": item["id"]}, item)
                outcomes.append("saved")
            except ContractFactsConflict as exc:
                outcomes.append(exc.current_revision)

        threads = [threading.Thread(target=confirm, args=(manager, number))
                   for number, manager in enumerate((self.manager, second), 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertCountEqual(outcomes, ["saved", 1])
        self.assertEqual(len(effective_facts(self.manager.get(self.loaded))), 1)

    def test_parent_snapshot_overrides_old_seeds_and_child_confirmation_is_independent(self):
        parent = self.confirm(candidate(self.loaded, "本项目实际测试至少需要十个工作日。"))
        child = copy.deepcopy(self.loaded)
        child["id"] = "job-" + "2" * 32
        child["report"]["context"]["business_facts"] = "旧错误事实：只需七日。"
        child["report"]["preparation"] = {"questions": [{"id": "Q01", "question": "需几日？", "answer": "七日", "status": "answered"}]}
        child["report"]["confirmed_facts"] = {"revision": parent["revision"], "facts": effective_facts(parent)}
        inherited = self.manager.get(child)
        self.assertEqual(inherited["revision"], 0)
        self.assertEqual(inherited["inherited_revision"], 1)
        self.assertNotIn("七日", json.dumps(inherited, ensure_ascii=False))
        self.assertEqual(inherited["facts"][0]["source"]["report_id"], self.loaded["id"])
        old_id = inherited["facts"][0]["id"]
        correction = candidate(child, "本项目实际测试需要十五个工作日。", kind="correction", replace_id=old_id)
        saved = self.manager.confirm(child, {"expected_revision": 0, "candidate_id": correction["id"], "replace_id": old_id}, correction)
        self.assertEqual(len(effective_facts(saved)), 1)
        self.assertEqual(self.manager.get(self.loaded), parent)
        child["report"]["confirmed_facts"] = {"revision": 1, "facts": []}
        fresh = ContractFactsManager(self.root / "other-facts").get(child)
        self.assertEqual(fresh["facts"], [])  # empty snapshot is meaningful, not a legacy fallback
        child["report"]["confirmed_facts"] = {"revision": 1, "facts": "broken"}
        with self.assertRaises(ContractFactsCorrupt):
            ContractFactsManager(self.root / "other-facts").get(child)

    def test_corruption_and_changed_contract_fail_without_reset(self):
        self.confirm(candidate(self.loaded, "本项目实际测试至少需要七日。"))
        changed = copy.deepcopy(self.loaded)
        changed["contract"]["fingerprint"] = "e" * 64
        with self.assertRaises(ContractFactsConflict):
            self.manager.get(changed)
        path = self.manager._path(self.loaded)
        path.write_text("broken-json", encoding="utf-8")
        with self.assertRaises(ContractFactsCorrupt):
            self.manager.get(self.loaded)
        self.assertEqual(path.read_text(encoding="utf-8"), "broken-json")


class DiscussionFactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.loaded = make_loaded()
        self.facts = ContractFactsManager(self.root / "facts")
        self.calls = []

    def manager(self, response, **kwargs):
        def completion(_, messages):
            self.calls.append(messages)
            return response
        return ClauseDiscussionManager(self.root / "discussions", FakeDelivery(), completion,
                                       facts_provider=self.facts.get, **kwargs)

    def test_legacy_report_instruction_snapshot_is_used_in_discussion_not_live_rules(self):
        from unittest.mock import patch
        self.loaded["initial_prompt_text"] = "旧报告首次请求的完整业务规则"
        manager = self.manager("已沿原规则讨论。")
        with patch("clause_discussion._initial_prompt", side_effect=AssertionError("不可读取现行规则")):
            manager.start(self.loaded, "F01", request_body("继续看验收条款。"))
            done = wait_done(manager, self.loaded)
        self.assertTrue(self.calls[0][0]["content"].startswith(self.loaded["initial_prompt_text"]))
        path = manager._path(self.loaded, "F01")
        snapshot = json.loads(path.read_text(encoding="utf-8"))["turns"][0]["prompt_snapshot"]
        self.assertEqual(snapshot["initial_text"], self.loaded["initial_prompt_text"])
        self.assertEqual(done["turns"][0]["assistant"], "已沿原规则讨论。")

    def test_discussion_candidate_question_link_survives_persistence_and_confirmation(self):
        self.loaded["report"]["findings"][0]["questions"] = ["实际测试几日？"]
        q = self.facts.get(self.loaded)["questions"][0]
        text = "实际测试需要十日。"
        manager = self.manager(suffix([{"text": text, "source_quote": text, "kind": "fact", "question_id": q["id"]}]))
        manager.start(self.loaded, "F01", request_body(text))
        done = wait_done(manager, self.loaded)
        item = done["turns"][0]["fact_candidates"][0]
        self.assertEqual(item["question_id"], q["id"])
        state = self.facts.confirm(self.loaded, {"expected_revision": 0, "candidate_id": item["id"]}, item)
        self.assertEqual(state["questions"][0]["status"], "answered")
        self.assertEqual(state["questions"][0]["answer"], text)
        self.assertEqual(manager.get(self.loaded, "F01")["turns"][0]["fact_candidates"][0]["question_id"], q["id"])

    def test_real_human_quote_is_bound_to_turn_and_machine_suffix_is_not_displayed(self):
        message = "本项目实际测试至少需要十个工作日。"
        manager = self.manager(suffix([{"text": message, "source_quote": message, "kind": "fact"}]))
        manager.start(self.loaded, "F01", request_body(message))
        done = wait_done(manager, self.loaded)
        turn = done["turns"][0]
        self.assertEqual(turn["assistant"], "答复仍然可读。")
        item = turn["fact_candidates"][0]
        self.assertEqual(item["source"], {"kind": "discussion", "report_id": self.loaded["id"], "finding_id": "F01",
                                         "session_id": "initial", "turn_id": turn["id"], "quote": message})
        self.assertEqual(self.facts.get(self.loaded)["facts"], [])  # not saved by a model reply
        saved = self.facts.confirm(self.loaded, {"expected_revision": 0, "candidate_id": item["id"]}, item)
        source = manager._source_context(self.loaded, self.loaded["report"]["findings"][1])
        followup = manager._messages(source, [], "付款是否受影响？")
        self.assertIn(message, followup[1]["content"])
        self.assertEqual(source["facts_state"]["revision"], saved["revision"])
        self.assertEqual(len(self.calls), 1)

    def test_assumptions_demands_and_model_quotes_do_not_become_candidates(self):
        turn_id = "turn-" + "a" * 32
        for message, quote in [
            ("假如本项目实际需要十日，那么七日不足。", "本项目实际需要十日"),
            ("如果本项目实际需要十日，如何处理？", "本项目实际需要十日"),
            ("我希望验收期为十日。", "验收期为十日"),
            ("请将验收期改为十日。", "验收期改为十日"),
            ("本项目实际需要十日？", "本项目实际需要十日"),
            ("我们有两个测试人员。", "模型建议验收期为十日。"),
        ]:
            with self.subTest(message=message):
                _, candidates = _split_fact_response(suffix([{"text": "十日", "source_quote": quote, "kind": "fact"}]),
                                                     message, turn_id)
                self.assertEqual(candidates, [])

    def test_corrupt_candidates_preserve_answer_and_old_turn_schema(self):
        manager = self.manager("答复仍然可读。\n<contract-fact-candidates>{broken")
        manager.start(self.loaded, "F01", request_body("我们有两个测试人员。"))
        done = wait_done(manager, self.loaded)
        self.assertEqual(done["turns"][0]["status"], "completed")
        self.assertEqual(done["turns"][0]["assistant"], "答复仍然可读。")
        self.assertEqual(done["turns"][0]["fact_candidates"], [])
        path = manager._path(self.loaded, "F01")
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["turns"][0]["fact_candidate_audit"]["status"], "unterminated")
        self.assertIn("{broken", state["turns"][0]["fact_candidate_audit"]["raw_suffix"])
        self.assertNotIn("fact_candidate_audit", done["turns"][0])
        state["turns"][0].pop("fact_candidates")
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(manager.get(self.loaded, "F01")["turns"][0]["fact_candidates"], [])
        state["turns"][0]["fact_candidates"] = {"not": "a candidate list"}
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(manager.get(self.loaded, "F01")["turns"][0]["assistant"], "答复仍然可读。")
        for broken_kind in ({"bad": "kind"}, ["fact"], None):
            state["turns"][0]["fact_candidates"] = [{"text": "我们有两个测试人员。", "source_quote": "我们有两个测试人员。", "kind": broken_kind}]
            path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(manager.get(self.loaded, "F01")["turns"][0]["fact_candidates"], [])

    def test_complete_quote_does_not_swallow_following_request_for_advice(self):
        message = ("补充已确认信息：双方实际商定第二笔款在可测试版本通过甲方书面确认后支付，不是仅发测试地址就付款。"
                   "请结合这个信息重新看这条建议；把这项事实整理供我确认保存。")
        quote = "双方实际商定第二笔款在可测试版本通过甲方书面确认后支付，不是仅发测试地址就付款。"
        manager = self.manager(suffix([{"text": quote, "source_quote": quote, "kind": "fact"}]))
        manager.start(self.loaded, "F01", request_body(message))
        done = wait_done(manager, self.loaded)
        self.assertEqual(len(done["turns"][0]["fact_candidates"]), 1)
        disk = json.loads(manager._path(self.loaded, "F01").read_text(encoding="utf-8"))
        audit = disk["turns"][0]["fact_candidate_audit"]
        self.assertEqual(audit["status"], "parsed")
        self.assertEqual(audit["supplied_count"], 1)
        self.assertEqual(audit["accepted_count"], 1)
        self.assertIn(quote, audit["raw_suffix"])

    def test_native_search_reply_extracts_without_extra_completion_or_search_request(self):
        message = "本项目实际需要十个工作日测试。"
        calls = []
        def search(_, messages):
            calls.append(messages)
            return {"text": suffix([{"text": message, "source_quote": message, "kind": "fact"}]),
                    "search": {"status": "not_used", "queries": [], "sources": [], "request_count": 0}}
        manager = self.manager("不应调用普通completion", search_completion=search)
        payload = request_body(message)
        payload.update(web_search=True)
        payload["config"]["base_url"] = "https://api.deepseek.com/v1"
        manager.start(self.loaded, "F01", payload)
        done = wait_done(manager, self.loaded)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(done["turns"][0]["fact_candidates"]), 1)
        self.assertEqual(done["turns"][0]["assistant"], "答复仍然可读。")

    def test_superseded_fact_is_not_reinjected_by_old_discussion_or_opinion(self):
        old_text, new_text = "本项目实际测试至少需要七日。", "本项目实际测试至少需要十个工作日。"
        manager = self.manager(suffix([{"text": old_text, "source_quote": old_text, "kind": "fact"}]))
        manager.start(self.loaded, "F01", request_body(old_text))
        done = wait_done(manager, self.loaded)
        item = done["turns"][0]["fact_candidates"][0]
        old = self.facts.confirm(self.loaded, {"expected_revision": 0, "candidate_id": item["id"]}, item)
        new = candidate(self.loaded, new_text, index=2, kind="correction", replace_id=old["facts"][0]["id"])
        self.facts.confirm(self.loaded, {"expected_revision": 1, "candidate_id": new["id"], "replace_id": old["facts"][0]["id"]}, new)
        finding = {**self.loaded["report"]["findings"][0], "reason": "因为" + old_text}
        source = manager._source_context(self.loaded, finding)
        messages = manager._messages(source, done["turns"], "继续讨论")
        self.assertNotIn(old_text, json.dumps(messages, ensure_ascii=False))
        self.assertIn(new_text, json.dumps(messages, ensure_ascii=False))
        source.update(turns=done["turns"], other_findings=[])
        messages, _ = OpinionRevisionManager._messages(source, finding, finding, [])
        self.assertNotIn(old_text, json.dumps(messages, ensure_ascii=False))
        self.assertIn(new_text, json.dumps(messages, ensure_ascii=False))

    def test_legacy_opinion_digest_accepts_unchanged_but_rejects_fact_or_question_updates(self):
        import hashlib
        for change in (None, "fact", "fact_restored", "question", "wrong_digest"):
            with self.subTest(change=change):
                loaded = full_loaded()
                loaded["report"]["findings"][0]["questions"] = ["测试用途？"]
                facts = ContractFactsManager(self.root / str(change) / "facts")
                original = facts.get(loaded)
                discussions = ClauseDiscussionManager(self.root / str(change) / "discussions", FakeDelivery(), facts_provider=facts.get)
                revisions = OpinionRevisionManager(self.root / str(change) / "opinions", discussions, completion=lambda _, messages: opinion_answer(messages))
                initial = revisions.get(loaded, "F01")
                revisions.start(loaded, "F01", opinion_body(initial))
                done = wait_revision(revisions, loaded)
                self.assertEqual(done["candidates"][0]["facts_digest_version"], 2)
                path = revisions._path(loaded, "F01")
                persisted = json.loads(path.read_text(encoding="utf-8"))
                legacy = persisted["candidates"][0]
                legacy.pop("facts_digest_version")
                legacy["facts_digest"] = hashlib.sha256(json.dumps(effective_facts(original), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                if change == "wrong_digest":
                    legacy["facts_digest"] = "f" * 64
                path.write_text(json.dumps(persisted, ensure_ascii=False), encoding="utf-8")
                if change in {"fact", "fact_restored"}:
                    added = facts.save(loaded, {"expected_revision": 0, "text": "项目实际测试用途已明确"})
                    if change == "fact_restored":
                        facts.withdraw(loaded, {"expected_revision": 1, "fact_id": effective_facts(added)[0]["id"]})
                        self.assertEqual(effective_facts(facts.get(loaded)), effective_facts(original))
                elif change == "question":
                    facts.answer(loaded, {"expected_revision": 0, "question_id": original["questions"][0]["id"],
                        "answer": "暂待业务确认", "status": "unknown"})
                    self.assertEqual(effective_facts(facts.get(loaded)), effective_facts(original))
                current = revisions.get(loaded, "F01")
                payload = {"expected_revision": current["revision"], "expected_opinion_revision": 0,
                    "expected_discussion_revision": 0, "expected_delivery_revision": 0,
                    "candidate_id": current["candidates"][0]["id"]}
                if change is None:
                    accepted = revisions.accept(loaded, "F01", payload)
                    self.assertEqual(accepted["opinion_revision"], 1)
                    self.assertEqual(accepted["accepted_candidate_id"], payload["candidate_id"])
                else:
                    with self.assertRaisesRegex(OpinionRevisionConflict, "已确认的合同信息已变化"):
                        revisions.accept(loaded, "F01", payload)
                    self.assertIsNone(revisions.get(loaded, "F01")["accepted_candidate_id"])

    def test_opinion_candidate_cannot_be_accepted_after_effective_facts_change(self):
        loaded = full_loaded()
        discussions = ClauseDiscussionManager(self.root / "discussions", FakeDelivery(), facts_provider=self.facts.get)
        revisions = OpinionRevisionManager(self.root / "opinions", discussions, completion=lambda _, messages: opinion_answer(messages))
        start = revisions.get(loaded, "F01")
        revisions.start(loaded, "F01", opinion_body(start))
        done = wait_revision(revisions, loaded)
        item = candidate(loaded, "本项目实际测试至少需要十个工作日。")
        self.facts.confirm(loaded, {"expected_revision": 0, "candidate_id": item["id"]}, item)
        with self.assertRaisesRegex(OpinionRevisionConflict, "已确认的合同信息已变化"):
            revisions.accept(loaded, "F01", {"expected_revision": done["revision"], "expected_opinion_revision": 0,
                "expected_discussion_revision": 0, "expected_delivery_revision": 0, "candidate_id": done["candidates"][0]["id"]})


if __name__ == "__main__":
    unittest.main()
