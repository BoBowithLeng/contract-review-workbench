from __future__ import annotations

import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from model_client import ModelClientError
from native_web_search import (_parse_response, deepseek_search_completion, validate_deepseek_address,
                               sanitize_search_diagnostics)


def response(*, stop="end_turn", result=True, sources=True):
    blocks = [
        {"type": "server_tool_use", "id": "call-1", "name": "web_search", "input": {"query": "民法典 法条"}},
    ]
    if result:
        blocks.append({"type": "web_search_tool_result", "tool_use_id": "call-1", "content": [
            {"type": "web_search_result", "title": "全国人大", "url": "https://www.npc.gov.cn/one", "encrypted_content": "opaque"},
            {"type": "web_search_result", "title": "evil", "url": "javascript:alert(1)"},
            {"type": "web_search_result", "title": "credential", "url": "https://name:pass@example.com"},
            {"type": "web_search_result", "title": "duplicate", "url": "https://www.npc.gov.cn/one"},
        ] if sources else []})
    blocks.append({"type": "text", "text": "核对后回答。"})
    return {"content": blocks, "stop_reason": stop, "usage": {"server_tool_use": {"web_search_requests": 1}}}


class SearchHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.calls.append((self.path, self.headers.get("x-api-key"), json.loads(body)))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/stolen")
            self.end_headers()
            return
        if self.path == "/echo401":
            payload, status = b"echo sk-private-test", 401
        elif self.path == "/oversized":
            payload, status = b"x" * 101, 200
        else:
            payload, status = json.dumps(response()).encode("utf-8"), 200
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class NativeSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SearchHandler)
        cls.server.calls = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.server.calls.clear()
        self.config = {"base_url": "https://api.deepseek.com/v1/chat/completions", "api_key": "sk-private-test", "model": "deepseek-flash"}
        self.messages = [{"role": "system", "content": "System"}, {"role": "user", "content": "查民法典"}]
        self.fixture = f"http://127.0.0.1:{self.server.server_port}"

    def test_only_official_origin_and_allowed_paths(self):
        for path in ("", "/", "/v1", "/chat/completions", "/v1/chat/completions"):
            validate_deepseek_address("https://api.deepseek.com" + path)
        for url in ("http://api.deepseek.com", "https://evil.example/v1", "https://api.deepseek.com.evil.example",
                    "https://api.deepseek.com:8443", "https://name:pass@api.deepseek.com",
                    "https://api.deepseek.com/anthropic", "https://api.deepseek.com/v1?key=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_deepseek_address(url)
        with self.assertRaises(ValueError):
            deepseek_search_completion({**self.config, "base_url": "https://evil.example/v1"}, self.messages)
        self.assertEqual(self.server.calls, [])

    def test_local_http_fixture_checks_wire_and_true_sources(self):
        with patch("native_web_search.ENDPOINT", self.fixture + "/success"):
            result = deepseek_search_completion(self.config, self.messages)
        path, secret, body = self.server.calls[0]
        self.assertEqual(path, "/success")
        self.assertEqual(secret, self.config["api_key"])
        self.assertEqual(body["system"], "System")
        self.assertEqual(body["messages"], [{"role": "user", "content": "查民法典"}])
        self.assertEqual(body["thinking"]["type"], "enabled")
        self.assertEqual(body["max_tokens"], 131072)
        self.assertEqual(body["tools"], [{"type": "web_search_20250305", "name": "web_search"}])
        self.assertNotIn("max_uses", body["tools"][0])
        self.assertEqual(result["search"], {
            "status": "searched", "queries": ["民法典 法条"],
            "sources": [{"title": "全国人大", "url": "https://www.npc.gov.cn/one"}],
            "request_count": 1,
        })
        self.assertNotIn("opaque", json.dumps(result))

    def test_not_used_no_results_error_and_partial_are_distinct(self):
        secret = self.config["api_key"]
        not_used = {"content": [{"type": "text", "text": "没有用工具"}], "stop_reason": "end_turn"}
        self.assertEqual(_parse_response(not_used, secret)["search"]["status"], "not_used")
        self.assertEqual(_parse_response(response(sources=False), secret)["search"]["status"], "no_results")
        failed_tool = response()
        failed_tool["content"][1]["content"] = [{"type": "web_search_tool_result_error", "error_code": "failed"}]
        self.assertEqual(_parse_response(failed_tool, secret)["search"]["status"], "error")
        self.assertEqual(_parse_response(response(stop="max_tokens"), secret)["search"]["status"], "partial")
        self.assertEqual(_parse_response(response(result=False), secret)["search"]["status"], "partial")

    def test_no_redirect_http_body_echo_or_oversized_response(self):
        for path, size in (("/redirect", None), ("/echo401", None), ("/oversized", 100)):
            with self.subTest(path=path), patch("native_web_search.ENDPOINT", self.fixture + path):
                if size:
                    with patch("native_web_search.MAX_RESPONSE_BYTES", size):
                        with self.assertRaises(ModelClientError) as caught:
                            deepseek_search_completion(self.config, self.messages)
                else:
                    with self.assertRaises(ModelClientError) as caught:
                        deepseek_search_completion(self.config, self.messages)
                self.assertNotIn(self.config["api_key"], str(caught.exception))
        self.assertEqual([call[0] for call in self.server.calls], ["/redirect", "/echo401", "/oversized"])

    def test_mixed_results_and_tool_error_keep_final_answer_without_second_call(self):
        data = response()
        data["content"][-1:-1] = [
            {"type": "server_tool_use", "id": "call-2", "name": "web_search", "input": {"query": "第二次检索"}},
            {"type": "web_search_tool_result", "tool_use_id": "call-2", "content": {
                "type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}},
        ]
        data["usage"]["server_tool_use"]["web_search_requests"] = 2
        with patch("native_web_search._send", return_value=data) as send:
            result = deepseek_search_completion(self.config, self.messages)
        self.assertEqual(send.call_count, 1)
        self.assertTrue(result["answer_complete"])
        self.assertEqual(result["text"], "核对后回答。")
        self.assertEqual(result["search"]["status"], "partial")
        self.assertEqual(result["search"]["error_codes"], ["max_uses_exceeded"])
        self.assertEqual(len(result["search"]["sources"]), 1)

    def test_one_search_disabled_wrapup_preserves_opaque_blocks_in_memory(self):
        first = response()
        first["content"].pop()  # results exist but there is no final answer
        first["content"].insert(0, {"type": "text", "text": "我先检索。"})
        final = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "已有材料支持的回答。"}]}
        with patch("native_web_search._send", side_effect=[first, final]) as send:
            result = deepseek_search_completion(self.config, self.messages)
        followup = send.call_args_list[1].args[0]
        self.assertEqual(followup["tool_choice"], {"type": "none"})
        self.assertEqual(followup["messages"][-2]["content"], first["content"])
        self.assertEqual(followup["tools"], send.call_args_list[0].args[0]["tools"])
        self.assertNotIn("max_uses", followup["tools"][0])
        self.assertTrue(result["answer_complete"])
        self.assertEqual(result["text"], "已有材料支持的回答。")
        self.assertEqual(result["search"]["request_count"], 1)
        self.assertEqual(result["search"]["answer_requests"], 1)
        self.assertNotIn("opaque", json.dumps(result))

    def test_pause_resumes_unchanged_once_and_preserves_incomplete_text(self):
        first = response(stop="pause_turn")
        final = response(stop="max_tokens")
        final["content"][-1]["text"] = "仍未完成的部分回答。"
        with patch("native_web_search._send", side_effect=[first, final]) as send:
            result = deepseek_search_completion(self.config, self.messages)
        self.assertEqual(send.call_count, 2)
        followup = send.call_args_list[1].args[0]
        self.assertNotIn("tool_choice", followup)
        self.assertEqual(followup["messages"][-1], {"role": "assistant", "content": first["content"]})
        self.assertFalse(result["answer_complete"])
        self.assertEqual(result["text"], "仍未完成的部分回答。")

    def test_wrapup_network_failure_does_not_discard_first_result(self):
        first = response(stop="max_tokens")
        with patch("native_web_search._send", side_effect=[first, ModelClientError("连接失败")]) as send:
            result = deepseek_search_completion(self.config, self.messages)
        self.assertEqual(send.call_count, 2)
        self.assertFalse(result["answer_complete"])
        self.assertEqual(result["text"], "核对后回答。")
        self.assertEqual(result["completion_error"], "连接失败")
        self.assertEqual(len(result["search"]["sources"]), 1)

    def test_pause_before_first_result_is_resumed_once(self):
        first = response(stop="pause_turn", result=False)
        first["content"].pop()
        with patch("native_web_search._send", side_effect=[first, response()]) as send:
            result = deepseek_search_completion(self.config, self.messages)
        self.assertEqual(send.call_count, 2)
        self.assertTrue(result["answer_complete"])
        self.assertNotIn("tool_choice", send.call_args_list[1].args[0])

    def test_missing_one_of_multiple_tool_results_stays_partial(self):
        data = response()
        data["content"].insert(0, {"type": "server_tool_use", "id": "call-2", "name": "web_search",
                                    "input": {"query": "第二项检索"}})
        with patch("native_web_search._send", return_value=data) as send:
            result = deepseek_search_completion(self.config, self.messages)
        self.assertEqual(send.call_count, 1)
        self.assertTrue(result["answer_complete"])
        self.assertEqual(result["search"]["status"], "partial")
        self.assertIn("missing_tool_result", result["diagnostics"]["partial_reasons"])
        self.assertEqual(result["diagnostics"]["stages"][0]["pending_tool_count"], 1)

    def test_resume_pairs_results_to_prior_tool_without_repeated_tool_use(self):
        first = response(stop="pause_turn", result=False)
        first["content"].pop()
        final = response()
        final["content"].pop(0)  # Same tool result, no repeated server_tool_use.
        with patch("native_web_search._send", side_effect=[first, final]) as send:
            result = deepseek_search_completion(self.config, self.messages)
        self.assertEqual(send.call_count, 2)
        self.assertTrue(result["answer_complete"])
        self.assertEqual(result["search"]["status"], "searched")
        self.assertEqual(len(result["search"]["sources"]), 1)
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["continuation_count"], 1)
        self.assertEqual(diagnostics["continuation_mode"], "resume_search")
        self.assertEqual([s["stop_reason"] for s in diagnostics["stages"]], ["pause_turn", "end_turn"])
        self.assertEqual(diagnostics["stages"][-1]["matched_result_count"], 1)
        self.assertEqual(diagnostics["partial_reasons"], [])
        self.assertEqual(sanitize_search_diagnostics(diagnostics, self.config["api_key"]), diagnostics)
        self.assertNotIn("opaque", json.dumps(diagnostics))
        self.assertNotIn("call-1", json.dumps(diagnostics))

    def test_final_answer_does_not_erase_still_missing_first_stage_result(self):
        first = response(stop="pause_turn")
        first["content"].insert(0, {"type": "server_tool_use", "id": "call-missing", "name": "web_search",
                                    "input": {"query": "无结果检索"}})
        final = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "已有材料回答。"}]}
        with patch("native_web_search._send", side_effect=[first, final]) as send:
            result = deepseek_search_completion(self.config, self.messages)
        self.assertEqual(send.call_count, 2)
        self.assertTrue(result["answer_complete"])
        self.assertEqual(result["search"]["status"], "partial")
        self.assertEqual(len(result["search"]["sources"]), 1)
        self.assertIn("missing_tool_result", result["diagnostics"]["partial_reasons"])

    def test_protocol_diagnostics_distinguish_token_limit_tool_failure_and_missing_text(self):
        data = response(stop="max_tokens")
        data["content"][1]["content"] = {"type": "web_search_tool_result_error", "error_code": "unavailable"}
        data["content"].pop()
        data["usage"].update(input_tokens=123456, output_tokens=8192)
        result = _parse_response(data, self.config["api_key"])
        stage = result["diagnostics"]["stages"][0]
        self.assertEqual(stage["stop_reason"], "max_tokens")
        self.assertEqual(stage["error_codes"], ["unavailable"])
        self.assertEqual(stage["usage"], {"input_tokens": 123456, "output_tokens": 8192})
        self.assertEqual(set(stage["partial_reasons"]), {"nonterminal_stop", "tool_error", "missing_final_text"})

    def test_initial_transport_failure_has_safe_diagnostics_without_retry(self):
        with patch("native_web_search._send", side_effect=ModelClientError("连接失败")) as send:
            with self.assertRaises(ModelClientError) as caught:
                deepseek_search_completion(self.config, self.messages)
        self.assertEqual(send.call_count, 1)
        diagnostic = sanitize_search_diagnostics(caught.exception.search_diagnostics, self.config["api_key"])
        self.assertEqual(diagnostic["continuation_count"], 0)
        self.assertEqual(diagnostic["partial_reasons"], ["request_failed"])
        self.assertEqual(diagnostic["stages"][0]["outcome"], "failed")

    def test_wrapup_failure_records_attempt_and_retains_first_evidence(self):
        first = response(stop="max_tokens")
        with patch("native_web_search._send", side_effect=[first, ModelClientError("连接失败")]) as send:
            result = deepseek_search_completion(self.config, self.messages)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(len(result["search"]["sources"]), 1)
        diagnostic = sanitize_search_diagnostics(result["diagnostics"], self.config["api_key"])
        self.assertEqual(diagnostic["continuation_count"], 1)
        self.assertEqual(diagnostic["completion_error"], "连接失败")
        self.assertEqual(diagnostic["stages"][-1]["outcome"], "failed")
        self.assertIn("continuation_failed", diagnostic["partial_reasons"])

    def test_diagnostics_normalizer_drops_extra_payload_and_rejects_invalid_shape(self):
        valid = _parse_response(response(), self.config["api_key"])["diagnostics"]
        copied = copy.deepcopy(valid)
        copied["opaque_blocks"] = [{"encrypted_content": "secret tool payload"}]
        copied["stages"][0]["raw_text"] = "private contract data"
        copied["stages"][0]["usage"] = {"input_tokens": 9, "private_prompt": "private contract data"}
        sanitized = sanitize_search_diagnostics(copied)
        self.assertNotIn("private", json.dumps(sanitized))
        self.assertNotIn("opaque", json.dumps(sanitized))
        self.assertEqual(sanitized["stages"][0]["usage"], {"input_tokens": 9})
        for change in ({"continuation_mode": []}, {"stages": []}, {"version": True},
                       {"partial_reasons": ["guessed_network_issue"]}, {"continuation_count": 2}):
            with self.subTest(change=change):
                self.assertIsNone(sanitize_search_diagnostics({**valid, **change}))
        key_error = {**valid, "completion_error": "failure " + self.config["api_key"]}
        self.assertNotIn(self.config["api_key"], json.dumps(sanitize_search_diagnostics(key_error, self.config["api_key"])))

    def test_all_valid_sources_from_later_searches_are_retained(self):
        blocks = []
        for i in range(4):
            blocks.extend([
                {"type": "server_tool_use", "id": f"call-{i}", "name": "web_search", "input": {"query": f"query-{i}"}},
                {"type": "web_search_tool_result", "tool_use_id": f"call-{i}", "content": [
                    {"type": "web_search_result", "title": f"Source {j}", "url": f"https://example.com/{j}",
                     "encrypted_content": "opaque"} for j in range(i * 10, (i + 1) * 10)]}])
        blocks[-1]["content"].extend([
            {"type": "web_search_result", "title": "Duplicate", "url": "https://example.com/0"},
            {"type": "web_search_result", "title": "Invalid", "url": "javascript:alert(1)"},
            {"type": "web_search_result", "title": "Credential", "url": "https://name:pass@example.com"},
            {"type": "web_search_result", "title": self.config["api_key"], "url": "https://example.com/key"}])
        blocks.append({"type": "text", "text": "已完成检索。"})
        envelope = {"content": blocks, "stop_reason": "end_turn", "usage": {"server_tool_use": {"web_search_requests": 4}}}
        with patch("native_web_search._send", return_value=envelope) as send:
            result = deepseek_search_completion(self.config, self.messages)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["search"]["status"], "searched")
        self.assertEqual([s["url"] for s in result["search"]["sources"]], [f"https://example.com/{i}" for i in range(40)])
        self.assertNotIn("opaque", json.dumps(result))
        self.assertNotIn(self.config["api_key"], json.dumps(result))

    def test_continuation_keeps_sources_beyond_twenty_and_deduplicates_overlap(self):
        first = response(stop="pause_turn")
        final = response()
        final["content"][0]["id"] = "call-2"
        final["content"][1]["tool_use_id"] = "call-2"
        for envelope, indexes in ((first, range(25)), (final, range(20, 45))):
            envelope["content"][1]["content"] = [
                {"type": "web_search_result", "title": f"Source {i}", "url": f"https://example.com/{i}"} for i in indexes]
        with patch("native_web_search._send", side_effect=[first, final]) as send:
            result = deepseek_search_completion(self.config, self.messages)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(result["search"]["status"], "searched")
        self.assertEqual(result["diagnostics"]["continuation_count"], 1)
        self.assertEqual([s["url"] for s in result["search"]["sources"]], [f"https://example.com/{i}" for i in range(45)])


if __name__ == "__main__":
    unittest.main()
