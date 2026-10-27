"""Synthetic fixtures test the handoff integrity, not legal model quality."""
from tests.source_fixtures import labor_sources
import json
import tempfile
import unittest
from pathlib import Path

from tool_review import prepare, load_review


class ToolReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.text = self.root / "synthetic.txt"
        self.packet = self.root / "request.json"
        self.response = self.root / "response.json"
        self.text.write_text("双方约定完成交接。", encoding="utf-8")
        self.data = prepare(self.text, self.packet, sources=labor_sources())
        self.answer = {"request_digest": self.data["request_digest"], "model": "test-fixture", "result": {
            "findings": [{"rule_id": r, "status": "需要法务判断", "risk": None,
                "evidence_status": "适用待判断", "refs": [{"id": "P000", "quote": "双方约定完成交接。"}],
                "source_ids": [], "reason": "测试夹具", "suggestion": "待复核", "clause_draft": "", "questions": []}
                for r in ("LC-01", "LC-02", "LC-03")], "uncovered": []}}

    def write_response(self):
        self.response.write_text(json.dumps(self.answer, ensure_ascii=False), encoding="utf-8")

    def test_host_route_uses_same_validation_without_claiming_api_connection(self):
        self.write_response()
        result = load_review(self.packet, self.response, model="test-fixture")
        self.assertEqual(result["report"]["mode"], "host_ai")
        self.assertFalse(result["report"]["metadata"]["api_connection_tested"])
        self.assertEqual(len(result["report"]["findings"]), 3)

    def test_hallucinated_quote_does_not_bypass_shared_validator(self):
        self.answer["result"]["findings"][0]["refs"][0]["quote"] = "合同没有这句话"
        self.write_response()
        result = load_review(self.packet, self.response, model="test-fixture")
        self.assertEqual(result["report"]["findings"][0]["status"], "处理失败")

    def test_old_response_cannot_attach_to_changed_contract(self):
        self.write_response()
        self.data["contract"]["pieces"][0]["text"] = "这是另一份合同"
        self.packet.write_text(json.dumps(self.data), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_review(self.packet, self.response, model="test-fixture")

    def test_wrong_model_and_overwriting_trace_are_rejected(self):
        self.write_response()
        with self.assertRaises(ValueError):
            load_review(self.packet, self.response, model="a-different-model")
        with self.assertRaises(FileExistsError):
            prepare(self.text, self.packet, sources=labor_sources())

    def test_changed_messages_are_rejected_even_if_contract_is_unchanged(self):
        self.write_response()
        self.data["messages"][0]["content"] = "这是被改过的提示"
        self.packet.write_text(json.dumps(self.data), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_review(self.packet, self.response, model="test-fixture")

    def test_business_goal_survives_prepare_runner_and_report(self):
        goal_packet = self.root / "goal_request.json"
        goal = "公司希望签约前约定首个正常工资支付日结清；不要保证必然有效。"
        data = prepare(self.text, goal_packet, business_goal=goal, sources=labor_sources())
        self.assertEqual(json.loads(data["messages"][1]["content"])["context"]["business_goal"], goal)
        self.answer["request_digest"] = data["request_digest"]
        self.write_response()
        result = load_review(goal_packet, self.response, model="test-fixture")
        self.assertEqual(result["report"]["context"]["business_goal"], goal)
        self.assertTrue(result["report"]["metadata"]["model_called"])


if __name__ == "__main__":
    unittest.main()
