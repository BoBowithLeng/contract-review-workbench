import copy
import json
import unittest
from pathlib import Path

from checklist_library import cell_markdown, parse_checklist, parse_rules
from checklist_store import ChecklistError, normalize_checklist_snapshot


SAMPLE = '''---
版本: "0.1"
---
# 名称
**适用范围**：境内普通交易。
**审查立场**：按我方立场。
## 目录
| 检查范围 | 对应事项 |
|---|---|
| 钱 | G01 付款 |
## 正文
| 检查事项（编号） | 检查内容 | 适用情形 | 备注 |
|---|---|---|---|
| **G01**<br>付款 | **何时支付**<br>• 核对付款条件。<br><br>**例外**<br>• 不凭空补比例。 | 有价款时 | — |
## 编制记录
不应进入模型的私人编制文字。
'''


def parse(text):
    return parse_checklist(text, checklist_id="test", name="测试清单", contract_type="通用",
                           prefix="G", expected_count=1)


class ChecklistLibraryTests(unittest.TestCase):
    def test_only_business_fields_and_item_rows(self):
        item = parse(SAMPLE)
        self.assertEqual(item["scope"], "境内普通交易。")
        self.assertEqual(item["stance"], "按我方立场。")
        self.assertEqual(len(item["items"]), 1)
        self.assertEqual(item["items"][0]["requirement"], "### 何时支付\n- 核对付款条件。\n\n### 例外\n- 不凭空补比例。")
        self.assertEqual(item["items"][0]["notes"], "")
        self.assertNotIn("私人", json.dumps(item, ensure_ascii=False))
        self.assertEqual(normalize_checklist_snapshot(item), item)

    def test_line_break_variants_and_literal_pipe(self):
        self.assertEqual(cell_markdown("**小标题**<br />• A &#124; B<br>2. C"), "### 小标题\n- A | B\n2. C")
        self.assertEqual(parse(SAMPLE.replace("核对付款条件。", r"核对 A\|B。"))["items"][0]["requirement"].splitlines()[1], "- 核对 A|B。")

    def test_reject_incomplete_duplicate_or_missing_identity(self):
        bad = [SAMPLE.replace("**G01**", "**G02**"),
               SAMPLE.replace(" | 有价款时 | — |", " | 有价款时 |"),
               SAMPLE.replace(" | 有价款时 | — |", " | 有价款时 | — | 多余 |"),
               SAMPLE.replace("**G01**", "无编号"),
               SAMPLE.replace("**审查立场**：按我方立场。", ""),
               SAMPLE.replace('版本: "0.1"', "版本: 无效"),
               SAMPLE.replace("核对付款条件。", "x" * 8001)]
        for text in bad:
            with self.subTest(text=text[:50]), self.assertRaises(ChecklistError):
                parse(text)

    def test_rules_exclude_maintenance_and_do_not_require_fixed_count(self):
        self.assertEqual(parse_rules("# 标题\n## 规则正文\n### 1. 规则\n检查全文。\n## 维护说明\n不要导入。"), "### 1. 规则\n检查全文。\n")
        with self.assertRaises(ChecklistError):
            parse_rules("没有规则正文")

    def test_committed_builtins_have_expected_coverage_and_no_cross_list_dependency(self):
        data = json.loads((Path(__file__).resolve().parents[1] / "builtin_checklists.json").read_text(encoding="utf-8"))
        general, software = data["checklists"]
        self.assertEqual((len(general["items"]), len(software["items"])), (24, 31))
        self.assertEqual(software["items"][-1]["id"], "C31")
        self.assertNotRegex(json.dumps(software, ensure_ascii=False), r"\bG\d{2}\b")
        self.assertNotIn("编制与复核", json.dumps(data, ensure_ascii=False))
        self.assertEqual(copy.deepcopy(data), {"checklists": [normalize_checklist_snapshot(x) for x in data["checklists"]]})


if __name__ == "__main__":
    unittest.main()
