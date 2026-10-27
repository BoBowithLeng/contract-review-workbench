"""General first-review contract and validation tests; no provider calls."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from contract_core import parse_text
from general_review import (
    build_messages, normalize_context, parse_response, profile_definition,
    response_schema, review_detailed, source_gate,
)


ROOT = Path(__file__).resolve().parents[1]


def context(**updates):
    value = {"contract_type": "general", "our_party": "承租方", "our_role": "承租人",
             "contract_type_label": "房屋租赁合同", "region": "中国大陆",
             "review_date": "2026-09-24", "input_kind": "拟签审阅", "business_goal": "减少押金争议"}
    value.update(updates)
    return value


def source(sid="LAW1", verified=True):
    return {"id": sid, "title": "中华人民共和国民法典", "authority": "全国人大",
            "region": "全国", "article": "第五百零九条", "text": "当事人应当按照约定全面履行自己的义务。",
            "url": "https://example.invalid/law", "effective_from": "2021-01-01",
            "effective_to": None, "checked_at": "2026-09-24", "verified": verified,
            "version_status": "有效", "notes": ""}


def finding(fid="F01", rule="G01", quote="押金不退", kind="商业", sources=None, **updates):
    value = {"id": fid, "rule_id": rule, "theme": "押金返还", "title": "押金不退",
             "issue_kind": kind, "status": "发现风险", "risk": "中",
             "evidence_status": "适用待判断", "refs": [{"id": "P000", "quote": quote}],
             "location_note": "", "source_ids": [] if sources is None else sources,
             "reason": "我方退租时可能损失押金。", "suggestion": "协商明确返还条件。",
             "clause_draft": "无应扣费用时七日内返还押金。", "questions": []}
    value.update(updates)
    return value


def payload(findings=None, checks=None, options=None):
    findings = [] if findings is None else findings
    if checks is None:
        checks = [{"rule_id": item["rule_id"], "title": item["theme"], "status": item["status"],
                   "reason": "按原文审查。"} for item in findings]
    return {"findings": findings, "checks": checks, "options": [] if options is None else options,
            "uncovered": [], "limitations": []}


class GeneralReviewTests(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_text("押金不退。\n租期一年。")

    def review(self, answer, *, ctx=None, sources=None, prompt_text=None):
        return review_detailed(self.parsed, ctx or context(), sources if sources is not None else [],
                               {"model": "fake", "quote_validation_version": 1}, completion=lambda config, messages: json.dumps(answer, ensure_ascii=False),
                               prompt_text=prompt_text)

    def test_context_allows_general_type_role_and_signed_review(self):
        clean, problems = normalize_context(context(contract_type_label="采购合同", our_party="采购方",
                                                   our_role="买方", input_kind="已签复核"))
        self.assertFalse(problems)
        self.assertEqual(clean["contract_type_label"], "采购合同")
        self.assertEqual(clean["our_role"], "买方")
        self.assertEqual(profile_definition()["default_context"]["region"], "中国大陆")
        self.assertEqual(normalize_context(context(our_party=" "))[1][0].split()[0], "our_party")

    def test_approved_prompt_used_verbatim_and_snapshot_stable(self):
        approved = (ROOT / "prompts" / "initial_review.md").read_text(encoding="utf-8")
        default = build_messages(self.parsed, context(), [])
        self.assertTrue(default[0]["content"].startswith(approved))
        old = "旧任务原样保存的业务稿。"
        self.assertTrue(build_messages(self.parsed, context(), [], prompt_text=old)[0]["content"].startswith(old))
        report = self.review(payload(), prompt_text=old)
        self.assertEqual(report["metadata"]["prompt_sha256"], hashlib.sha256(old.encode()).hexdigest())
        self.assertEqual(report["metadata"]["review_profile"], "general")
        self.assertEqual(report["schema_version"], 2)

    def test_zero_risk_is_valid_and_no_fixed_checks(self):
        report = self.review(payload())
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["checks"], [])
        self.assertEqual(report["options"], [])

    def test_many_themes_and_more_than_fifteen_findings(self):
        pieces = parse_text("\n".join(f"条款{i}有歧义" for i in range(1, 21)))
        findings = [finding(f"F{i:02d}", f"G{i:02d}", f"条款{i}有歧义", "文本",
                            refs=[{"id": f"P{i-1:03d}", "quote": f"条款{i}有歧义"}],
                            theme=f"主题{i}") for i in range(1, 21)]
        result = parse_response(json.dumps(payload(findings), ensure_ascii=False), pieces, context(), [])
        self.assertEqual(len(result["findings"]), 20)
        self.assertEqual(len(result["checks"]), 20)
        self.assertTrue(all(item["status"] != "处理失败" for item in result["findings"]))

    def test_missing_clause_can_have_no_fabricated_quote(self):
        item = finding(refs=[], location_note="已查阅全文及附件，未见押金返还期限。")
        report = self.review(payload([item]))
        self.assertEqual(report["findings"][0]["status"], "发现风险")
        item["location_note"] = ""
        self.assertEqual(self.review(payload([item]))["findings"][0]["status"], "处理失败")

    def test_commercial_issue_needs_no_law_and_different_party_is_used(self):
        item = finding(evidence_status="原文可核")
        report = self.review(payload([item]), ctx=context(our_party="出租方", our_role="出租人"))
        self.assertEqual(report["findings"][0]["source_ids"], [])
        self.assertEqual(report["findings"][0]["evidence_status"], "原文可核")
        self.assertEqual(report["context"]["our_party"], "出租方")
        user_data = json.loads(build_messages(self.parsed, context(our_party="出租方"), [])[1]["content"])
        self.assertEqual(user_data["context"]["our_party"], "出租方")
        self.assertIn("不预设其在合同中也叫委托方", build_messages(self.parsed, context(our_party="出租方"), [])[0]["content"])

    def test_original_text_evidence_does_not_validate_legal_conclusion(self):
        item = finding(kind="法律", evidence_status="原文可核")
        report = self.review(payload([item]))
        self.assertEqual(report["findings"][0]["status"], "处理失败")
        self.assertIn("原文可核", response_schema(quote_validation_version=1)["properties"]["findings"]["items"]["properties"]["evidence_status"]["enum"])

    def test_legal_issue_may_be_high_risk_while_basis_needs_review(self):
        item = finding(kind="法律", status="需要法务判断", risk="高", evidence_status="资料不足")
        report = self.review(payload([item]))
        self.assertEqual(report["findings"][0]["status"], "需要法务判断")
        self.assertEqual(report["findings"][0]["risk"], "高")

    def test_missing_information_may_have_assessed_business_risk(self):
        item = finding(kind="信息", status="信息不足", risk="中", evidence_status="原文可核",
                       refs=[], location_note="全文未见维修责任分配。")
        report = self.review(payload([item]))
        self.assertEqual(report["findings"][0]["status"], "信息不足")
        self.assertEqual(report["findings"][0]["risk"], "中")

    def test_bad_quote_unknown_source_and_duplicate_id_fail(self):
        cases = [finding(quote="从未写过"), finding(sources=["FAKE"]),
                 finding(risk="极高"), finding(status="保证有效"),
                 [finding(), finding(fid="F01", rule="G02")]]
        for bad in cases:
            with self.subTest(bad=bad):
                answer = payload(bad if isinstance(bad, list) else [bad])
                report = self.review(answer)
                self.assertTrue(any(item["status"] == "处理失败" for item in report["findings"]))

    def test_duplicate_option_id_and_unknown_link_are_reported(self):
        option = {"id": "O1", "title": "返还方案", "finding_ids": ["F01"],
                  "preconditions": [], "steps": ["改押金条款"], "cost": "谈判成本",
                  "remaining_risk": "可能谈不成", "source_ids": [], "clause_draft": "七日内返还。",
                  "relationship": "单独使用"}
        report = self.review(payload([finding()], options=[option, dict(option)]))
        self.assertEqual(len(report["options"]), 1)
        self.assertTrue(any(check["status"] == "处理失败" for check in report["checks"]))
        self.assertTrue(report["limitations"])
        option["finding_ids"] = ["F99"]
        report = self.review(payload([finding()], options=[option]))
        self.assertFalse(report["options"])

    def test_web_candidate_is_not_verified_law(self):
        web = {"id": "WEB001", "title": "检索结果", "url": "https://example.invalid/one",
               "verified": False, "text": "仅获标题与链接，未核对页面正文", "version_status": "检索候选，正文未核",
               "checked_at": None, "authority": "联网检索结果", "article": "", "effective_from": None,
               "effective_to": None, "region": "全国", "notes": ""}
        clean, checked, problems = source_gate(context(), [web])
        self.assertFalse(problems)
        self.assertEqual(checked[0]["id"], "WEB001")
        item = finding(kind="法律", sources=["WEB001"], evidence_status="依据已核")
        report = self.review(payload([item]), sources=[web])
        self.assertEqual(report["findings"][0]["status"], "处理失败")
        item["evidence_status"] = "适用待判断"
        item["risk"] = None
        item["status"] = "需要法务判断"
        report = self.review(payload([item]), sources=[web])
        self.assertEqual(report["findings"][0]["status"], "需要法务判断")

    def test_historically_checked_source_is_usable_but_future_or_expired_is_not(self):
        item = finding(kind="法律", sources=["LAW1"], evidence_status="依据已核")
        past = source()
        past["checked_at"] = "2026-09-22"
        report = self.review(payload([item]), sources=[past])
        self.assertEqual(report["findings"][0]["status"], "发现风险")
        instructions = build_messages(self.parsed, context(), [past], quote_validation_version=1)[0]["content"]
        self.assertIn("不等于本轮重新核对", instructions)
        for changes in ({"checked_at": "2026-09-25"}, {"effective_to": "2026-09-23"},
                        {"effective_from": "2026-09-25"}):
            with self.subTest(changes=changes):
                invalid = {**past, **changes}
                report = self.review(payload([item]), sources=[invalid])
                self.assertEqual(report["findings"][0]["status"], "处理失败")

    def test_schema_has_dynamic_rule_and_capacity(self):
        schema = response_schema()
        self.assertEqual(schema["properties"]["findings"]["maxItems"], 60)
        self.assertEqual(schema["properties"]["checks"]["maxItems"], 60)
        self.assertNotIn("minItems", schema["properties"]["checks"])
        self.assertEqual(schema["properties"]["findings"]["items"]["properties"]["rule_id"], {"type": "string"})
        instructions = build_messages(self.parsed, context(), [])[0]["content"]
        for marker in ("F01-F99", "不用 F1", "O1-O30", "不用 O01", "G01-G999", "不用 G1"):
            self.assertIn(marker, instructions)

    def test_same_theme_multiple_findings_have_distinct_ids(self):
        first = finding(fid="F01", rule="G01")
        second = finding(fid="F02", rule="G01", title="另一押金问题")
        answer = payload([first, second], checks=[{"rule_id": "G01", "title": "押金返还",
                                                    "status": "发现风险", "reason": "两处问题。"}])
        report = self.review(answer)
        self.assertEqual([item["id"] for item in report["findings"]], ["F01", "F02"])
        self.assertEqual(len(report["checks"]), 1)


if __name__ == "__main__":
    unittest.main()
