from __future__ import annotations

import os
import sys
import unittest


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import contract_core
from completeness import check_completeness
import demo_contracts


def by_id(results: list[dict], item_id: str) -> dict:
    return next(item for item in results if item["id"] == item_id)


class CompletenessTests(unittest.TestCase):
    def test_short_contract_with_all_eight_subjects_and_varied_phrasing(self):
        text = """劳动合同
甲方：星河科技有限公司；注册地址：上海市浦东新区世纪大道1号；法定代表人为张三。
员工李四，现住址为上海市静安区安远路2号，身份证号310101199901011234。
双方订立固定期限劳动合同，自2026年10月1日至2029年9月30日。
乙方担任产品经理，办公地点位于上海市徐汇区。
公司实行标准工时制，乙方依法享受法定节假日和年休假。
月工资人民币12000元，于每月十日发放。
劳动者知悉公司依法缴纳社会保险。
甲方提供劳动防护用品和符合规定的劳动条件，如实告知职业危害并采取防护措施。"""
        results = check_completeness(contract_core.parse_text(text), {"input_kind": "拟签文本审阅"})
        self.assertEqual([item["status"] for item in results], ["已找到"] * 8)
        self.assertTrue(all(item["missing_fields"] == [] for item in results))
        self.assertIn("员工李四", by_id(results, "B02")["refs"][0]["quote"])

    def test_contract_title_alone_is_not_content(self):
        results = check_completeness(contract_core.parse_text("劳动合同\n\n"), {"input_kind": "模板审阅"})
        self.assertEqual([item["status"] for item in results], ["未找到"] * 8)
        self.assertTrue(all(item["refs"] == [] for item in results))

    def test_template_blanks_are_to_be_filled_but_signed_blanks_need_confirmation(self):
        text = "甲方名称：________\n住所：【待填】\n法定代表人：____"
        template = by_id(check_completeness(contract_core.parse_text(text), {"input_kind": "模板审阅"}), "B01")
        signed = by_id(check_completeness(contract_core.parse_text(text), {"input_kind": "拟签文本审阅"}), "B01")
        self.assertEqual(template["status"], "待填写")
        self.assertEqual(signed["status"], "需确认")
        self.assertEqual(template["missing_fields"], ["用人单位名称", "用人单位住所", "法定代表人或主要负责人"])

    def test_adjacent_table_cells_supply_labels_and_values(self):
        parsed = contract_core.ParsedContract(
            pieces=[
                contract_core.ContractPiece(id="T0R0C0P0", text="甲方名称"),
                contract_core.ContractPiece(id="T0R0C1P0", text="星河科技有限公司"),
                contract_core.ContractPiece(id="T0R1C0P0", text="注册地址"),
                contract_core.ContractPiece(id="T0R1C1P0", text="上海市浦东新区世纪大道1号"),
                contract_core.ContractPiece(id="T0R2C0P0", text="主要负责人"),
                contract_core.ContractPiece(id="T0R2C1P0", text="张三"),
            ],
            warnings=[],
            fingerprint="test",
        )
        item = by_id(check_completeness(parsed, {"input_kind": "拟签文本审阅"}), "B01")
        self.assertEqual(item["status"], "已找到")
        self.assertEqual([ref["id"] for ref in item["refs"]], [
            "T0R0C0P0", "T0R0C1P0", "T0R1C0P0", "T0R1C1P0", "T0R2C0P0", "T0R2C1P0"
        ])

    def test_demo_table_unit_residence_is_a_complete_employer_field(self):
        parsed = contract_core.parse_docx(demo_contracts.table_docx())
        item = by_id(check_completeness(parsed, {"input_kind": "模板审阅"}), "B01")
        self.assertEqual(item["status"], "已找到")
        self.assertEqual(item["missing_fields"], [])
        self.assertTrue(any(ref["quote"] == "单位住所" for ref in item["refs"]))

    def test_blank_primary_term_and_wage_fields_override_explanatory_mentions(self):
        text = """第一条 合同期限
固定期限：自【】年【】月【】日起至【】年【】月【】日止。
另有无固定期限和以完成一定工作任务为期限的合同形式说明。
第六条 劳动报酬
乙方基本工资为           元/月。
正常工作时间工资作为加班工资计算基数，工资每月发放。"""
        template = check_completeness(contract_core.parse_text(text), {"input_kind": "模板审阅"})
        signed = check_completeness(contract_core.parse_text(text), {"input_kind": "拟签文本审阅"})
        self.assertEqual(by_id(template, "B03")["status"], "待填写")
        self.assertEqual(by_id(template, "B06")["status"], "待填写")
        self.assertEqual(by_id(signed, "B03")["status"], "需确认")
        self.assertEqual(by_id(signed, "B06")["status"], "需确认")

    def test_indefinite_term_needs_no_end_date(self):
        text = "双方订立无固定期限劳动合同，自2026年10月1日起。"
        item = by_id(check_completeness(contract_core.parse_text(text), {"input_kind": "拟签文本审阅"}), "B03")
        self.assertEqual(item["status"], "已找到")
        self.assertEqual(item["missing_fields"], [])

    def test_optional_confidentiality_trial_and_noncompete_are_not_invented(self):
        results = check_completeness(contract_core.parse_text("工作地点：上海市。月工资10000元。"), {"input_kind": "拟签文本审阅"})
        self.assertEqual(len(results), 8)
        combined = repr(results)
        self.assertNotIn("保密", combined)
        self.assertNotIn("竞业", combined)
        self.assertNotIn("试用期", combined)

    def test_partial_item_is_not_reported_as_found(self):
        item = by_id(
            check_completeness(contract_core.parse_text("乙方从事软件开发工作。"), {"input_kind": "拟签文本审阅"}),
            "B04",
        )
        self.assertEqual(item["status"], "需确认")
        self.assertEqual(item["missing_fields"], ["工作地点"])

    def test_party_words_in_unrelated_sentences_do_not_fill_identity_fields(self):
        text = "甲方提供劳动条件。乙方依法享受年休假。"
        results = check_completeness(contract_core.parse_text(text), {"input_kind": "拟签文本审阅"})
        self.assertEqual(by_id(results, "B01")["status"], "未找到")
        self.assertEqual(by_id(results, "B02")["status"], "未找到")

    def test_refs_are_always_exact_substrings_and_empty_text_is_safe(self):
        parsed = contract_core.parse_text("社会保险\n甲方依法为乙方缴纳社会保险。")
        results = check_completeness(parsed, {"input_kind": "拟签文本审阅"})
        source = {piece.id: piece.text for piece in parsed.pieces}
        for item in results:
            for ref in item["refs"]:
                self.assertIn(ref["quote"], source[ref["id"]])
        empty = check_completeness(contract_core.parse_text(""), {})
        self.assertEqual(len(empty), 8)
        self.assertTrue(all(item["status"] == "未找到" for item in empty))

    def test_schema_and_social_insurance_note_are_ui_ready(self):
        results = check_completeness(contract_core.parse_text("公司依法为员工缴纳社保。"), {"input_kind": "拟签文本审阅"})
        expected = {"id", "title", "status", "refs", "note", "source_ids", "missing_fields"}
        self.assertTrue(all(set(item) == expected for item in results))
        self.assertEqual(by_id(results, "B07")["source_ids"], ["LCL17"])
        self.assertIn("不代表", by_id(results, "B07")["note"])


if __name__ == "__main__":
    unittest.main()
