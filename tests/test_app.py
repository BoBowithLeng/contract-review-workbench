"""Interaction tests: no network requests or user secrets needed."""
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from review_engine import review_detailed as legacy_review_detailed
from tests.source_fixtures import labor_sources

APP = Path(__file__).resolve().parents[1] / "app.py"


def button(app, label):
    return next(item for item in app.button if item.label == label)


class AppTests(unittest.TestCase):
    def new_app(self):
        app = AppTest.from_file(str(APP)).run(timeout=20)
        self.assertEqual(list(app.exception), [])
        app.sidebar.radio[0].set_value("审核合同").run()
        return app

    def test_local_review_manual_decision_and_stale_input(self):
        # This UI-action regression deliberately supplies a synthetic legacy
        # source snapshot; production app reviews have no built-in corpus.
        with patch("review_engine.review_detailed", side_effect=lambda parsed, context, _sources, *args, **kw:
                   legacy_review_detailed(parsed, context, labor_sources(), *args, **kw)):
            app = self.new_app()
            next(w for w in app.radio if w.label == '合同来源').set_value('表格合同样例').run(timeout=15)
            button(app, "读取合同").click().run()
            button(app, "本地规则预检（不调用 AI）").click().run()
            self.assertEqual(list(app.exception), [])
            report = app.session_state["report"]
            self.assertEqual(report["mode"], "local_rules")
            self.assertTrue(any(f['rule_id']=='LC-02' for f in report['findings']))
            first_id = report['findings'][0]['id']
            button(app, "查看审查报告").click().run()
            selectors = [w for w in app.selectbox if w.label == "你的处理"]
            selectors[0].select("接受").run()
            next(b for b in app.button if b.label == "保存这条复核").click().run()
            self.assertEqual(app.session_state["reviews"][first_id]["decision"], "接受")
            app.sidebar.radio[0].set_value("审核合同").run()
            next(w for w in app.number_input if "发薪日" in w.label).set_value(10).run()
            self.assertTrue(any("已过期" in w.value for w in app.warning))
            self.assertEqual(list(app.exception), [])

    def test_settings_are_explicit_and_clearable(self):
        with patch("model_client.chat_completion", return_value="OK") as call:
            app = self.new_app()
            app.sidebar.radio[0].set_value("模型设置").run()
            next(w for w in app.text_input if w.label == "API 密钥").set_value("TEST_KEY_NOT_A_SECRET")
            button(app, "保存为当前模型").click().run()
            self.assertEqual(call.call_count, 0)
            self.assertEqual(app.session_state["model_config"]["model"], "deepseek-flash")
            button(app, "测试连接（发一次请求）").click().run()
            self.assertEqual(call.call_count, 1)
            app.run()
            self.assertEqual(call.call_count, 1)
            button(app, "清除会话内所有模型密钥").click().run()
            self.assertNotIn("model_config", app.session_state)
            self.assertEqual(next(w for w in app.text_input if w.label == "API 密钥").value, "")
            self.assertEqual(list(app.exception), [])

    def test_provider_switch_never_reuses_another_key(self):
        app = self.new_app()
        app.sidebar.radio[0].set_value("模型设置").run()
        next(w for w in app.text_input if w.label == "API 密钥").set_value("TEST_KEY_NOT_A_SECRET")
        button(app, "保存为当前模型").click().run()
        next(w for w in app.radio if w.label == "服务商").set_value("OpenRouter").run()
        self.assertEqual(next(w for w in app.text_input if w.label == "API 密钥").value, "")
        self.assertEqual(next(w for w in app.text_input if "Base URL" in w.label).value, "https://openrouter.ai/api/v1")
        self.assertEqual(list(app.exception), [])

    def test_ai_scope_gate_does_not_claim_a_model_call(self):
        with patch("contract_core.chat_completion") as call:
            app = self.new_app()
            app.session_state["model_config"] = {"provider": "Test", "base_url": "https://example.invalid/v1", "api_key": "TEST", "model": "test-model"}
            button(app, "读取合同").click().run()
            next(w for w in app.selectbox if w.label == "适用地区").set_value("其他 / 尚未确认").run()
            app.checkbox[0].check().run()
            button(app, "开始 AI 审核").click().run()
            self.assertEqual(call.call_count, 0)
            button(app, "查看审查报告").click().run()
            self.assertTrue(any("模型没有运行" in w.value for w in app.warning))
            self.assertEqual(list(app.exception), [])

    def test_report_landing_has_no_model_controls_or_truncated_grid(self):
        app = AppTest.from_file(str(APP)).run(timeout=20)
        if not any("合同审查" == t.value for t in app.title):
            self.skipTest("本机没有保存的工具报告")
        self.assertEqual(list(app.exception), [])
        self.assertEqual(len(app.dataframe), 0)
        self.assertFalse(any("开始 AI" in b.label or "重新载入" in b.label for b in app.button))
        self.assertFalse(any(w.label == "每月发薪日（0 表示未确认）" for w in app.number_input))
        self.assertTrue(any(w.label == '查看意见或必备项' for w in app.selectbox))
        self.assertTrue(any(h.value == '合同原稿' for h in app.subheader))


if __name__ == "__main__":
    unittest.main()
