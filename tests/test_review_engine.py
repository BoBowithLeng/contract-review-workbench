"""Detailed engine and v1/v2 handoff compatibility tests."""

from __future__ import annotations

from tests.source_fixtures import labor_sources

import json
import tempfile
import unittest
from pathlib import Path

from contract_core import ParsedContract, _ai_prompt, parse_text
from review_engine import build_messages, review_detailed
from tool_review import complete, hash_request, load_review, prepare


ROOT = Path(__file__).resolve().parents[1]


def sources():
    return labor_sources()


def context(**changes):
    value = {
        "region": "上海",
        "review_date": "2026-09-20",
        "input_kind": "拟签文本审阅",
        "payday": 10,
    }
    value.update(changes)
    return value


def old_answer(contract: ParsedContract):
    quote = contract.pieces[0].text
    return {
        "findings": [
            {
                "rule_id": rule_id,
                "status": "需要法务判断",
                "risk": None,
                "evidence_status": "适用待判断",
                "refs": [{"id": contract.pieces[0].id, "quote": quote}],
                "source_ids": [],
                "reason": "旧版测试夹具",
                "suggestion": "人工核对",
                "clause_draft": "",
                "questions": [],
            }
            for rule_id in ("LC-01", "LC-02", "LC-03")
        ],
        "uncovered": [],
    }


def detailed_answer(contract: ParsedContract):
    quote = contract.pieces[0].text
    return {
        "findings": [
            {
                "id": "F01",
                "rule_id": "LC-02",
                "title": "工资支付与交接绑定",
                "status": "发现风险",
                "risk": "高",
                "evidence_status": "依据已核",
                "refs": [{"id": contract.pieces[0].id, "quote": quote}],
                "source_ids": ["LCL30_50"],
                "reason": "劳动合同法第三十条、第五十条要求分别核对工资与交接义务。",
                "suggestion": "工资按明确节点支付，交接损失另行留证主张。",
                "clause_draft": "公司依法、依约支付工资；交接责任另行处理。",
                "questions": [],
            }
        ],
        "checks": [
            {"rule_id": "LC-01", "status": "未检出", "reason": "已审，未见明确放弃文字。"},
            {"rule_id": "LC-02", "status": "发现风险", "reason": "已审，存在一项明确风险。"},
            {"rule_id": "LC-03", "status": "未检出", "reason": "已审，未见离职后业务限制。"},
        ],
        "options": [
            {
                "id": "O1",
                "title": "工资与交接分开",
                "finding_ids": ["F01"],
                "preconditions": ["删除交接作为工资支付前提"],
                "steps": ["确定工资节点", "另设交接清单"],
                "cost": "财务需单独核算。",
                "remaining_risk": "工资口径仍需按制度核对。",
                "source_ids": ["LCL30_50"],
                "clause_draft": "公司依法、依约支付工资；交接责任另行处理。",
                "relationship": "可与解除生效前交接流程并用。",
            }
        ],
        "uncovered": [],
    }


class DetailedLocalTests(unittest.TestCase):
    def test_same_rule_is_split_into_multiple_findings(self):
        contract = parse_text(
            "未发放工资必须在办理完工作交接以后才能结算，迟发不构成拖欠。\n"
            "办理完毕工作交接后，公司于下个工资支付日结算离职工资。\n"
            "公司可暂不支付经济补偿至员工完成交接为止（工资除外）。"
        )
        report = review_detailed(contract, context(), sources())
        lc02 = [item for item in report["findings"] if item["rule_id"] == "LC-02"]
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["mode"], "local_rules")
        self.assertFalse(report["metadata"]["model_called"])
        self.assertGreaterEqual(len(lc02), 3)
        self.assertIn("高", {item["risk"] for item in lc02})
        self.assertIn(None, {item["risk"] for item in lc02})
        self.assertEqual(len({ref["id"] for item in lc02 for ref in item["refs"]}), 3)
        self.assertTrue(all(option["finding_ids"] for option in report["options"]))

    def test_short_contract_does_not_invent_optional_chapter_defects(self):
        report = review_detailed(parse_text("乙方应提前三十日以书面形式通知甲方解除合同。"), context(), sources())
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["options"], [])
        self.assertEqual({item["rule_id"] for item in report["checks"]}, {"LC-01", "LC-02", "LC-03"})
        self.assertNotIn("发现风险", {item["status"] for item in report["checks"]})

    def test_input_kind_alias_is_normalized(self):
        report = review_detailed(parse_text("普通合同文字"), context(), sources())
        self.assertEqual(report["context"]["input_kind"], "拟签审阅")


class DetailedValidationTests(unittest.TestCase):
    def test_fake_quote_and_unknown_option_relation_fail_safely(self):
        contract = parse_text("未完成交接，公司不支付工资。")
        answer = detailed_answer(contract)
        answer["findings"][0]["refs"][0]["quote"] = "原文没有这句话"
        answer["options"][0]["finding_ids"] = ["F99"]

        def completion(config, messages):
            self.assertEqual(json.loads(messages[1]["content"])["schema_version"], 2)
            return json.dumps(answer, ensure_ascii=False)

        report = review_detailed(contract, context(), sources(), {"model": "fixture"}, completion=completion)
        self.assertEqual(report["findings"][0]["status"], "处理失败")
        self.assertEqual(report["options"], [])
        self.assertTrue(all(item["status"] == "处理失败" for item in report["checks"]))
        self.assertTrue(any("未知 finding id" in item for item in report["uncovered"]))

    def test_option_can_keep_a_valid_no_issue_finding_as_context(self):
        contract = parse_text("未完成交接，公司不支付工资。")
        answer = detailed_answer(contract)
        answer["findings"].append({
            "id": "F02",
            "rule_id": "LC-02",
            "title": "合法交接机制保留",
            "status": "未检出",
            "risk": None,
            "evidence_status": "依据已核",
            "refs": [{"id": "P000", "quote": "未完成交接，公司不支付工资。"}],
            "source_ids": ["LCL30_50"],
            "reason": "劳动合同法第五十条所涉交接机制可与工资问题分开保留。",
            "suggestion": "保留交接，修改工资付款前提。",
            "clause_draft": "",
            "questions": [],
        })
        answer["options"][0]["finding_ids"] = ["F01", "F02"]
        answer["checks"][1] = {
            "rule_id": "LC-02",
            "status": "需要法务判断",
            "reason": "本主题同时有明确风险、待判断事项和可保留机制，整体仍需法务判断。",
        }

        report = review_detailed(
            contract,
            context(),
            sources(),
            {"model": "fixture"},
            completion=lambda config, messages: json.dumps(answer, ensure_ascii=False),
        )
        self.assertEqual(len(report["options"]), 1)
        self.assertEqual(report["options"][0]["finding_ids"], ["F01", "F02"])
        self.assertEqual(next(item for item in report["checks"] if item["rule_id"] == "LC-02")["status"], "需要法务判断")

    def test_option_with_only_no_issue_findings_is_rejected(self):
        contract = parse_text("双方约定完成交接。")
        answer = detailed_answer(contract)
        finding = answer["findings"][0]
        finding.update({
            "title": "交接机制保留",
            "status": "未检出",
            "risk": None,
            "refs": [{"id": "P000", "quote": "双方约定完成交接。"}],
            "reason": "未检出需要修改的问题。",
        })
        answer["checks"][1] = {"rule_id": "LC-02", "status": "未检出", "reason": "本主题已审，未检出问题。"}

        report = review_detailed(
            contract,
            context(),
            sources(),
            {"model": "fixture"},
            completion=lambda config, messages: json.dumps(answer, ensure_ascii=False),
        )
        self.assertEqual(report["options"], [])
        lc02 = next(item for item in report["checks"] if item["rule_id"] == "LC-02")
        self.assertEqual(lc02["status"], "处理失败")
        self.assertTrue(any("至少需要关联一项真正待处理的问题" in item for item in report["uncovered"]))


class ToolCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.text = self.root / "contract.txt"
        self.text.write_text("未完成交接，公司不支付工资。", encoding="utf-8")

    def test_old_packet_uses_its_own_source_snapshot(self):
        packet_path = self.root / "v1-request.json"
        response_path = self.root / "v1-response.json"
        packet = prepare(self.text, packet_path, sources=labor_sources())
        # Simulate a real older trace made before the newest source was added.
        packet["sources"] = [item for item in packet["sources"] if item["id"] != "LCL17"]
        contract = ParsedContract.model_validate(packet["contract"])
        packet["messages"] = _ai_prompt(contract, packet["context"], packet["sources"])
        packet["request_digest"] = hash_request(packet["messages"])
        packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
        response_path.write_text(
            json.dumps({"request_digest": packet["request_digest"], "model": "old-sol", "result": old_answer(contract)}, ensure_ascii=False),
            encoding="utf-8",
        )
        bundle = load_review(packet_path, response_path, model="old-sol")
        self.assertEqual(bundle["report"]["mode"], "host_ai")
        self.assertIsNone(bundle["report"]["metadata"]["source_snapshot_current"])
        self.assertNotIn("LCL17", {item["id"] for item in bundle["sources"]})

    def test_v2_prepare_complete_full_flow(self):
        packet_path = self.root / "v2-request.json"
        response_path = self.root / "v2-response.json"
        output_path = self.root / "v2-result.json"
        packet = prepare(self.text, packet_path, detailed=True, sources=labor_sources())
        contract = ParsedContract.model_validate(packet["contract"])
        self.assertEqual(packet["schema_version"], 2)
        self.assertEqual(packet["messages"], build_messages(contract, packet["context"], packet["sources"]))
        response_path.write_text(
            json.dumps({"request_digest": packet["request_digest"], "model": "new-sol", "result": detailed_answer(contract)}, ensure_ascii=False),
            encoding="utf-8",
        )
        bundle = complete(packet_path, response_path, output_path, model="new-sol")
        self.assertTrue(output_path.exists())
        self.assertEqual(bundle["report"]["schema_version"], 2)
        self.assertEqual(bundle["report"]["mode"], "host_ai")
        self.assertTrue(bundle["report"]["metadata"]["model_called"])
        self.assertEqual(bundle["report"]["findings"][0]["id"], "F01")
        self.assertEqual(bundle["report"]["options"][0]["finding_ids"], ["F01"])


if __name__ == "__main__":
    unittest.main()
