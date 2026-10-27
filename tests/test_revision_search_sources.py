import unittest

from clause_discussion import strip_echoed_search_block
from opinion_revisions import _original_sources, _validation_sources


class AcceptedSearchSourcesTest(unittest.TestCase):
    def test_merged_sources_are_counted_once(self):
        # apply() already merged W1 from the accepted version into the report.
        loaded = {"sources": [{"id": "WEB001"}, {"id": "WEB-W1"}], "original_sources": [{"id": "WEB001"}]}
        item = {"source_candidates": [{"id": "WEB-W1"}, {"id": "WEB-W2"}]}
        self.assertEqual([s["id"] for s in _validation_sources(loaded, item)], ["WEB001", "WEB-W1", "WEB-W2"])
        self.assertEqual(_original_sources(loaded), [{"id": "WEB001"}])

    def test_unapplied_report_keeps_its_sources(self):
        loaded = {"sources": [{"id": "WEB001"}]}
        self.assertEqual(_original_sources(loaded), [{"id": "WEB001"}])


class EchoedSearchBlockTest(unittest.TestCase):
    def test_trailing_dump_is_removed(self):
        text = '结论如上，仍需核对原文。\n\n[本轮联网检索结果，仅作讨论历史，未经人工核验] [{"title":"民法典第568条","url":"http://x"}]'
        self.assertEqual(strip_echoed_search_block(text), "结论如上，仍需核对原文。")

    def test_ordinary_brackets_stay(self):
        for text in ("参考：[民法典第568条]（未核）", "见意见[1]", "末尾\n\n[本轮联网检索结果]\n[]"):
            self.assertEqual(strip_echoed_search_block(text), text)


class TargetLabelTest(unittest.TestCase):
    def test_cross_reference_is_not_a_target(self):
        from draft_preparation import _target_label
        self.assertIsNone(_target_label("因甲方主动提出新增需求而产生的费用，按本合同第2.2条及《变更确认单》的约定处理。"))
        self.assertIsNone(_target_label("相关费用依照第3.1条执行。"))
        # 第19条（F18）实例：并行适用说明与勾选项里的条号都只是引用。
        self.assertIsNone(_target_label("双方确认：多选时各款叠加适用，不重复约定；本条与9.1、9.2并行适用，9.1、9.2已约定的资料返还不再重复。"))
        self.assertIsNone(_target_label("□（1）本项目不涉及个人信息及甲方业务数据的处理，9.3.2至9.3.6不适用；"))

    def test_changed_clause_is_still_a_target(self):
        from draft_preparation import _target_label
        self.assertEqual(_target_label("将第3.2条修改为：甲方应在验收后付款。"), "3.2")
        self.assertEqual(_target_label("2.2 费用调整：增加的费用按变更单支付。"), "2.2")
        self.assertEqual(_target_label("第3.2条：甲方应在验收后付款。"), "3.2")
        self.assertEqual(_target_label("原第4.1条后新增一款：乙方应提交测试报告。"), "4.1")
        self.assertEqual(_target_label("在“保密”之后新增“9.3 数据与个人信息处理”。"), "9.3")


if __name__ == "__main__":
    unittest.main()
