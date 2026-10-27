from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from clause_discussion import ClauseDiscussionConflict, ClauseDiscussionCorrupt, ClauseDiscussionManager
from model_client import ModelClientError
from reader_server import create_server


SOURCE_SHA = "a" * 64
FINGERPRINT = "b" * 64


def make_loaded(report_id="job-" + "1" * 32):
    findings = [
        {
            "id": "F01", "title": "验收期", "refs": [{"id": "P001", "quote": "七日内验收"}],
            "reason": "验收时间可能不足。", "suggestion": "根据测试工作量确定。", "clause_draft": "十日内验收。",
        },
        {
            "id": "F02", "title": "付款", "refs": [{"id": "P002", "quote": "验收后付款"}],
            "reason": "付款节点需核对。", "suggestion": "对应交付成果。", "clause_draft": "",
        },
    ]
    return {
        "id": report_id,
        "request_digest": "c" * 64,
        "document_name": "服务合同.docx",
        "contract": {"fingerprint": FINGERPRINT, "pieces": [
            {"id": "P001", "text": "4.1 甲方应在七日内验收。"},
            {"id": "P002", "text": "4.2 甲方应在验收后付款。"},
        ]},
        "paragraphs": [
            {"id": "P001", "location": "第四条 / 4.1"}, {"id": "P002", "location": "第四条 / 4.2"},
        ],
        "report": {"fingerprint": FINGERPRINT, "context": {
            "contract_type": "software_service", "our_role": "client", "our_party": "甲方",
            "business_goal": "活动前稳定使用。", "region": "上海", "review_date": "2026-09-22",
        }, "findings": findings},
        "sources": [{
            "id": "CC509", "title": "民法典", "authority": "全国人大", "article": "第五百零九条",
            "text": "当事人应当按照约定全面履行义务。", "verified": True,
        }],
    }


class FakeDelivery:
    def __init__(self, configured=True):
        self.configured = configured
        self.lock = threading.RLock()
        self.revision = 0
        self.source_sha = SOURCE_SHA
        original = "4.1 甲方应在七日内验收。"
        start = original.index("七日内")
        self.edits = [{
            "id": "edit-F01-P001", "piece_id": "P001", "start": start, "end": start + len("七日内"),
            "new_text": "十个工作日内", "finding_ids": ["F01"],
        }]
        self.comments = []

    def has_configured_source(self, report_id):
        return self.configured

    def _inspect_current(self, loaded):
        return b"docx-source", {"source_sha256": self.source_sha}

    def _load_state(self, loaded, data, source_sha):
        return {"revision": self.revision, "edits": list(self.edits), "comments": list(self.comments), "created_at": None}


def request_body(message, request_id=None, *, revision=0, delivery_revision=0, key="secret-api-key"):
    return {
        "expected_revision": revision,
        "expected_delivery_revision": delivery_revision,
        "message": message,
        "request_id": request_id or str(uuid.uuid4()),
        "config": {"base_url": "http://127.0.0.1:9999/v1", "api_key": key, "model": "fixture-model"},
        "consent": True,
    }


def wait_done(manager, loaded, finding_id="F01", timeout=3, session_id=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = manager.get(loaded, finding_id, session_id=session_id)
        if state["pending_turn_id"] is None:
            return state
        time.sleep(0.01)
    raise AssertionError("discussion did not finish")


class ClauseDiscussionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "discussions"
        self.loaded = make_loaded()
        self.delivery = FakeDelivery()

    def test_two_rounds_carry_bounded_source_context_and_history(self):
        calls = []

        def completion(config, messages):
            calls.append((dict(config), [dict(item) for item in messages]))
            return "我不盲目同意：需结合测试量判断。" if len(calls) == 1 else "补充事实后，可以修正为十个工作日。"

        manager = ClauseDiscussionManager(self.root, self.delivery, completion)
        first_body = request_body("七日为什么不够？")
        first, started = manager.start(self.loaded, "F01", first_body)
        self.assertTrue(started)
        self.assertEqual(first["revision"], 1)
        done = wait_done(manager, self.loaded)
        self.assertEqual(done["revision"], 2)
        self.assertEqual(done["turns"][0]["status"], "completed")
        self.assertEqual(done["context"]["location"], "第四条 / 4.1")
        self.assertEqual(done["context"]["party_label"], "客户 · 甲方")
        self.assertEqual(done["context"]["source_count"], 1)

        second_body = request_body("我们确实需要更多测试时间。", revision=2)
        manager.start(self.loaded, "F01", second_body)
        final = wait_done(manager, self.loaded)
        self.assertEqual(final["revision"], 4)
        self.assertEqual(len(final["turns"]), 2)
        self.assertEqual(len(calls), 2)
        second_messages = calls[1][1]
        self.assertEqual([item["role"] for item in second_messages[-3:]], ["user", "assistant", "user"])
        self.assertEqual(second_messages[-3]["content"], first_body["message"])
        self.assertIn("我不盲目同意", second_messages[-2]["content"])
        bound_data = second_messages[1]["content"]
        self.assertIn("P002", bound_data)  # cross-clause original context is present
        self.assertIn('"original_text": "4.1 甲方应在七日内验收。"', bound_data)
        self.assertIn('"current_text": "4.1 甲方应在十个工作日内验收。"', bound_data)
        self.assertIn("PROVIDED_SOURCES", bound_data)

    def test_idempotency_changed_request_and_isolation(self):
        calls = 0

        def completion(config, messages):
            nonlocal calls
            calls += 1
            return "回答"

        manager = ClauseDiscussionManager(self.root, self.delivery, completion)
        body = request_body("请解释。")
        manager.start(self.loaded, "F01", body)
        done = wait_done(manager, self.loaded)
        duplicate = dict(body, expected_revision=0, expected_delivery_revision=999)
        cached, started = manager.start(self.loaded, "F01", duplicate)
        self.assertFalse(started)
        self.assertEqual(cached, done)
        self.assertEqual(calls, 1)
        with self.assertRaises(ClauseDiscussionConflict):
            manager.start(self.loaded, "F01", dict(duplicate, message="换一个问题"))
        self.assertEqual(manager.get(self.loaded, "F02")["turns"], [])
        other = make_loaded("job-" + "9" * 32)
        self.assertEqual(manager.get(other, "F01")["turns"], [])

    def test_credentials_are_not_persisted_and_echo_is_redacted(self):
        secret = "top-secret-provider-key"

        def completion(config, messages):
            return "provider echoed " + config["api_key"]

        manager = ClauseDiscussionManager(self.root, self.delivery, completion)
        manager.start(self.loaded, "F01", request_body("会泄露密钥吗？", key=secret))
        done = wait_done(manager, self.loaded)
        self.assertIn("[已隐藏 API Key]", done["turns"][0]["assistant"])
        disk = "\n".join(path.read_text(encoding="utf-8") for path in self.root.glob("*.json"))
        self.assertNotIn(secret, disk)
        self.assertNotIn("127.0.0.1:9999", disk)
        self.assertIn("fixture-model", disk)

    def test_failed_turn_is_safe_cached_and_not_retried(self):
        calls = 0

        def completion(config, messages):
            nonlocal calls
            calls += 1
            raise RuntimeError("private path C:/secret and key")

        manager = ClauseDiscussionManager(self.root, self.delivery, completion)
        body = request_body("请回答。")
        manager.start(self.loaded, "F01", body)
        failed = wait_done(manager, self.loaded)
        self.assertEqual(failed["turns"][0]["status"], "failed")
        self.assertEqual(failed["turns"][0]["error"], "这次讨论没有完成，可以重新发送问题。")
        cached, started = manager.start(self.loaded, "F01", dict(body, expected_revision=0))
        self.assertFalse(started)
        self.assertEqual(cached, failed)
        self.assertEqual(calls, 1)

    def test_model_client_error_is_controlled(self):
        manager = ClauseDiscussionManager(
            self.root, self.delivery,
            lambda config, messages: (_ for _ in ()).throw(ModelClientError("模型服务返回 HTTP 429。")),
        )
        manager.start(self.loaded, "F01", request_body("请回答。"))
        failed = wait_done(manager, self.loaded)
        self.assertEqual(failed["turns"][0]["error"], "模型服务返回 HTTP 429。")

    def test_restart_marks_pending_failed_and_concurrent_or_stale_rejected(self):
        gate = threading.Event()
        calls = 0

        def completion(config, messages):
            nonlocal calls
            calls += 1
            gate.wait(3)
            return "late answer"

        manager = ClauseDiscussionManager(self.root, self.delivery, completion)
        first_body = request_body("第一问")
        running, _ = manager.start(self.loaded, "F01", first_body)
        with self.assertRaises(ClauseDiscussionConflict):
            manager.start(self.loaded, "F01", request_body("并发第二问", revision=1))
        restarted = ClauseDiscussionManager(self.root, self.delivery, completion)
        recovered = restarted.get(self.loaded, "F01")
        self.assertEqual(recovered["turns"][0]["status"], "failed")
        self.assertEqual(recovered["revision"], 2)
        duplicate, started = restarted.start(self.loaded, "F01", dict(first_body, expected_revision=0))
        self.assertFalse(started)
        self.assertEqual(duplicate, recovered)
        gate.set()
        deadline = time.time() + 3
        while manager._active and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(manager._active)
        self.assertEqual(calls, 1)

    def test_stop_discards_late_answer_and_allows_next_question(self):
        gate = threading.Event()
        calls = []

        def completion(config, messages):
            calls.append([dict(item) for item in messages])
            if len(calls) == 1:
                gate.wait(3)
                return "late answer"
            return "second answer"

        manager = ClauseDiscussionManager(self.root, self.delivery, completion)
        running, _ = manager.start(self.loaded, "F01", request_body("第一问"))
        turn_id = running["pending_turn_id"]
        stopped, changed = manager.stop(self.loaded, "F01", {"turn_id": turn_id})
        self.assertTrue(changed)
        self.assertIsNone(stopped["pending_turn_id"])
        self.assertEqual(stopped["turns"][0]["status"], "failed")
        self.assertTrue(stopped["turns"][0]["error"].startswith("已停止回答"))
        again, changed = manager.stop(self.loaded, "F01", {"turn_id": turn_id})
        self.assertFalse(changed)
        self.assertEqual(again["revision"], stopped["revision"])

        manager.start(self.loaded, "F01", request_body("第二问", revision=stopped["revision"]))
        gate.set()
        final = wait_done(manager, self.loaded)
        deadline = time.time() + 3
        while manager._active and time.time() < deadline:
            time.sleep(0.01)
        final = manager.get(self.loaded, "F01")
        self.assertEqual([t["status"] for t in final["turns"]], ["failed", "completed"])
        self.assertIsNone(final["turns"][0]["assistant"])
        self.assertEqual(final["turns"][1]["assistant"], "second answer")
        self.assertNotIn("第一问", [m["content"] for m in calls[1][2:]])
        with self.assertRaises(ValueError):
            manager.stop(self.loaded, "F01", {"turn_id": "bad"})

    def test_stale_revisions_and_invalid_config_rejected_before_model_or_write(self):
        calls = 0

        def completion(config, messages):
            nonlocal calls
            calls += 1
            return "answer"

        manager = ClauseDiscussionManager(self.root, self.delivery, completion)
        invalid = request_body("问题")
        invalid["config"]["base_url"] = "http://example.com/v1"
        with self.assertRaises(ValueError):
            manager.start(self.loaded, "F01", invalid)
        self.assertFalse(self.root.exists())
        self.assertEqual(calls, 0)

        body = request_body("问题", revision=1)
        with self.assertRaises(ClauseDiscussionConflict) as stale:
            manager.start(self.loaded, "F01", body)
        self.assertEqual(stale.exception.current_revision, 0)
        self.delivery.revision = 1
        with self.assertRaises(ClauseDiscussionConflict) as delivery_stale:
            manager.start(self.loaded, "F01", request_body("问题", delivery_revision=0))
        self.assertEqual(delivery_stale.exception.current_delivery_revision, 1)
        self.assertEqual(manager.get(self.loaded, "F01")["context"]["delivery_revision"], 1)
        self.assertEqual(self.delivery.edits[0]["new_text"], "十个工作日内")

    def test_plaintext_legacy_report_supported(self):
        delivery = FakeDelivery(configured=False)
        calls = []
        manager = ClauseDiscussionManager(self.root, delivery, lambda config, messages: calls.append(messages) or "文本回答")
        body = request_body("纯文本问题")
        manager.start(self.loaded, "F02", body)
        done = wait_done(manager, self.loaded, "F02")
        self.assertEqual(done["turns"][0]["status"], "completed")
        self.assertEqual(done["context"]["delivery_revision"], 0)

    def test_source_or_finding_change_conflicts_and_corrupt_file_is_preserved(self):
        manager = ClauseDiscussionManager(self.root, self.delivery, lambda config, messages: "回答")
        manager.start(self.loaded, "F01", request_body("问题"))
        wait_done(manager, self.loaded)
        path = manager._path(self.loaded, "F01")
        self.delivery.source_sha = "d" * 64
        with self.assertRaises(ClauseDiscussionConflict):
            manager.get(self.loaded, "F01")
        self.delivery.source_sha = SOURCE_SHA
        self.loaded["report"]["findings"][0]["reason"] = "意见已变化。"
        with self.assertRaises(ClauseDiscussionConflict):
            manager.get(self.loaded, "F01")
        self.loaded["report"]["findings"][0]["reason"] = "验收时间可能不足。"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ClauseDiscussionCorrupt):
            manager.get(self.loaded, "F01")
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_native_search_result_persists_only_metadata_and_informs_next_turn(self):
        regular_calls = []
        search_calls = []

        def native(config, messages):
            search_calls.append(messages)
            return {"text": "已查询后回答。", "search": {
                "status": "searched", "queries": ["民法典 509条"],
                "sources": [{"title": "全国人大法条", "url": "https://www.npc.gov.cn/law"}],
                "request_count": 1,
            }}

        manager = ClauseDiscussionManager(self.root, self.delivery,
                                          lambda config, messages: regular_calls.append(messages) or "普通回答",
                                          search_completion=native)
        first = request_body("检索法条")
        first["config"].update(base_url="https://api.deepseek.com/v1", model="deepseek-flash")
        first["web_search"] = True
        manager.start(self.loaded, "F01", first)
        done = wait_done(manager, self.loaded)
        self.assertEqual(done["turns"][0]["web_search"], True)
        self.assertEqual(done["turns"][0]["search"]["status"], "searched")
        self.assertEqual(len(search_calls), 1)
        self.assertEqual(regular_calls, [])
        disk = manager._path(self.loaded, "F01").read_text(encoding="utf-8")
        self.assertNotIn(first["config"]["api_key"], disk)
        self.assertNotIn("encrypted_content", disk)
        with self.assertRaises(ClauseDiscussionConflict):
            manager.start(self.loaded, "F01", {**first, "web_search": False})
        next_body = request_body("再解释一下", revision=2)
        manager.start(self.loaded, "F01", next_body)
        wait_done(manager, self.loaded)
        # Earlier search results travel as bound data, not inside the model's own past
        # answer, so the model has no raw dump to imitate at the end of its reply.
        self.assertNotIn("全国人大法条", regular_calls[0][-2]["content"])
        bound = json.loads(regular_calls[0][1]["content"].split("\n", 1)[1])
        self.assertEqual(bound["PREVIOUS_SEARCH_SOURCES"][0]["turn"], 1)
        self.assertEqual(bound["PREVIOUS_SEARCH_SOURCES"][0]["sources"][0]["title"], "全国人大法条")

    def test_search_rejects_third_party_key_before_write(self):
        manager = ClauseDiscussionManager(self.root, self.delivery, lambda *_: "普通", search_completion=lambda *_: "unexpected")
        body = request_body("联网查")
        body["web_search"] = True
        with self.assertRaisesRegex(ValueError, "DeepSeek 官方"):
            manager.start(self.loaded, "F01", body)
        self.assertFalse(self.root.exists())

    def test_search_partial_preserves_text_without_claiming_complete(self):
        manager = ClauseDiscussionManager(self.root, self.delivery, lambda *_: "ordinary", search_completion=lambda *_: {
            "text": "半截回答", "search": {"status": "partial", "queries": ["民法典"],
                                  "sources": [{"title": "全国人大", "url": "https://www.npc.gov.cn"}], "request_count": 1},
        })
        body = request_body("请查")
        body["web_search"] = True
        body["config"].update(base_url="https://api.deepseek.com", model="deepseek-flash")
        manager.start(self.loaded, "F01", body)
        done = wait_done(manager, self.loaded)
        self.assertEqual(done["turns"][0]["status"], "failed")
        self.assertEqual(done["turns"][0]["assistant"], "半截回答")
        self.assertEqual(done["turns"][0]["search"]["status"], "partial")
        self.assertEqual(done["turns"][0]["search"]["sources"][0]["title"], "全国人大")

    def test_complete_answer_survives_partial_search_error_and_is_in_history(self):
        calls = []
        manager = ClauseDiscussionManager(self.root, self.delivery,
            lambda config, messages: calls.append(messages) or "继续解释",
            search_completion=lambda *_: {
                "text": "根据已取得的资料，这是完整回答。", "answer_complete": True,
                "search": {"status": "partial", "queries": ["民法典"],
                           "sources": [{"title": "全国人大", "url": "https://www.npc.gov.cn"}],
                           "request_count": 4, "error_codes": ["max_uses_exceeded"]},
            })
        body = request_body("请查")
        body["web_search"] = True
        body["config"].update(base_url="https://api.deepseek.com", model="deepseek-flash")
        manager.start(self.loaded, "F01", body)
        done = wait_done(manager, self.loaded)
        turn = done["turns"][0]
        self.assertEqual(turn["status"], "completed")
        self.assertIn("完整回答", turn["assistant"])
        self.assertIsNone(turn["error"])
        self.assertEqual(turn["search"]["error_codes"], ["max_uses_exceeded"])
        manager.start(self.loaded, "F01", request_body("继续", revision=2))
        wait_done(manager, self.loaded)
        self.assertIn("完整回答", calls[0][-2]["content"])

    def test_search_error_does_not_echo_key(self):
        secret = "sk-search-secret"
        manager = ClauseDiscussionManager(self.root, self.delivery, search_completion=lambda *_: (
            _ for _ in ()
        ).throw(ModelClientError("provider echoed " + secret)))
        body = request_body("查一下", key=secret)
        body["web_search"] = True
        body["config"].update(base_url="https://api.deepseek.com", model="deepseek-flash")
        manager.start(self.loaded, "F01", body)
        done = wait_done(manager, self.loaded)
        self.assertEqual(done["turns"][0]["search"]["status"], "error")
        self.assertNotIn(secret, json.dumps(done))
        self.assertNotIn(secret, manager._path(self.loaded, "F01").read_text(encoding="utf-8"))

    def test_schema1_legacy_turn_without_search_fields_is_read(self):
        manager = ClauseDiscussionManager(self.root, self.delivery, lambda *_: "旧回答")
        manager.start(self.loaded, "F01", request_body("旧问题"))
        wait_done(manager, self.loaded)
        path = manager._path(self.loaded, "F01")
        state = json.loads(path.read_text(encoding="utf-8"))
        state["turns"][0].pop("web_search")
        state["turns"][0].pop("search")
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        old = manager.get(self.loaded, "F01")["turns"][0]
        self.assertFalse(old["web_search"])
        self.assertIsNone(old["search"])


class FakeDiscussions:
    def __init__(self):
        self.started = 0

    def get(self, loaded, finding_id, session_id=None):
        return {"revision": 0, "turns": [], "context": {"location": "4.1", "party_label": "客户", "delivery_revision": 0, "document_name": "fixture.docx", "source_count": 1}, "pending_turn_id": None}

    def start(self, loaded, finding_id, body):
        self.started += 1
        return ({"revision": 1, "turns": [{"id": "turn-x", "status": "running"}], "context": {}, "pending_turn_id": "turn-x"}, True)


class ClauseDiscussionHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.server = create_server(root=root, state_dir=root / "state", jobs_dir=root / "jobs", review_runner=None)
        self.server.discussions = FakeDiscussions()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True);self.thread.start();self.addCleanup(self.stop)
        self.port = self.server.server_port

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)

    def request(self, method, path, body=None, *, origin=True, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin:headers["Origin"] = f"http://127.0.0.1:{self.port}"
        data = None
        if body is not None:
            data = json.dumps(body).encode();headers["Content-Type"] = "application/json"
        connection.request(method, path, data, headers);response=connection.getresponse()
        payload=json.loads(response.read().decode());status=response.status;connection.close();return status,payload

    def test_routes_host_origin_and_static_mapping(self):
        route = "/api/reports/detailed/discussions/F01"
        status, presets = self.request("GET", "/api/model-presets", origin=False)
        self.assertEqual(status, 200)
        self.assertEqual(len(presets["providers"]), 4)
        self.assertEqual(set(presets["providers"][0]), {"id", "label", "base_url", "model"})
        with patch("reader_server._load_report", return_value=make_loaded("detailed")):
            self.assertEqual(self.request("GET", route, origin=False)[0], 200)
            self.assertEqual(self.request("POST", route, {}, origin=False)[0], 403)
            self.assertEqual(self.request("POST", route, {}, host="evil.example:8519")[0], 421)
            self.assertEqual(self.request("POST", route, {})[0], 202)
        self.assertEqual(self.server.discussions.started, 1)

    def test_reader_server_passes_native_search_injection(self):
        fixture = lambda config, messages: {"text": "fixture", "search": {
            "status": "not_used", "queries": [], "sources": [], "request_count": 0,
        }}
        server = create_server(root=Path(self.temp.name), state_dir=Path(self.temp.name) / "isolated-state",
                               jobs_dir=Path(self.temp.name) / "isolated-jobs", search_completion=fixture)
        try:
            self.assertIs(server.discussions.search_completion, fixture)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
