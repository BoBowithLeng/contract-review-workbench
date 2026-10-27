"""Local job orchestration tests use a stub, never a delivery AI result."""

from __future__ import annotations

from tests.source_fixtures import labor_sources, service_sources, write_sources

import base64
import datetime as dt
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from docx import Document

from contract_core import ParsedContract
from reader_server import REPORTS, create_server
from tool_review import prepare


PROJECT = Path(__file__).resolve().parents[1]


def docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def result_for_piece(piece_id: str, quote: str) -> dict:
    return {
        "findings": [{
            "id": "F01", "rule_id": "LC-02", "title": "工资结算需核对",
            "status": "需要法务判断", "risk": None, "evidence_status": "适用待判断",
            "refs": [{"id": piece_id, "quote": quote}], "source_ids": ["LCL30_50"],
            "reason": "测试桩只验证任务接线，不代表真实模型法律意见。",
            "suggestion": "由真实模型运行后再阅读。", "clause_draft": "", "questions": [],
        }],
        "checks": [
            {"rule_id": "LC-01", "status": "未检出", "reason": "测试桩。"},
            {"rule_id": "LC-02", "status": "需要法务判断", "reason": "测试桩。"},
            {"rule_id": "LC-03", "status": "未检出", "reason": "测试桩。"},
        ],
        "options": [],
        "uncovered": ["测试桩输出不是实际AI审核。"],
    }


def general_result(messages: list[dict]) -> str:
    payload = json.loads(messages[1]["content"])
    piece = next(item for item in payload["contract"] if item["text"].strip())
    web_ids = [item["id"] for item in payload["sources"] if item["id"].startswith("WEB")]
    return json.dumps({
        "findings": [{
            "id": "F01", "rule_id": "G01", "theme": "履行安排", "title": "履行节点需要明确",
            "issue_kind": "商业", "status": "发现风险", "risk": "中", "evidence_status": "适用待判断",
            "refs": [{"id": piece["id"], "quote": piece["text"]}], "location_note": "",
            "source_ids": web_ids, "reason": "测试桩：条款可能影响履行。",
            "suggestion": "核对节点后协商。", "clause_draft": "双方应明确交付与验收节点。", "questions": [],
        }],
        "checks": [{"rule_id": "G01", "title": "履行安排", "status": "发现风险", "reason": "测试桩。"}],
        "options": [], "uncovered": [], "limitations": ["测试桩不是法律结论。"],
    }, ensure_ascii=False)


class ReviewJobTests(unittest.TestCase):
    def test_trace_excludes_credentials_in_invalid_provider_url(self):
        from review_jobs import ReviewJobManager
        for url in ['https://user:secret@example.com', 'https://example.com?key=secret']:
            saved = ReviewJobManager._safe_config({'base_url': url, 'api_key': 'secret', 'model': 'test'})
            self.assertNotIn('secret', json.dumps(saved))
            self.assertEqual(saved['model'], 'test')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "project"
        (self.root / "reader_ui").mkdir(parents=True)
        for name in ("index.html", "style.css", "app.js", "workspace.js", "delivery.js", "right-panel.css"):
            (self.root / "reader_ui" / name).write_text(name, encoding="utf-8")
        write_sources(self.root / "sources.json", labor_sources())
        write_sources(self.root / "sources_service.json", service_sources())
        (self.root / "prompts").mkdir()
        (self.root / "prompts" / "initial_review.md").write_bytes((PROJECT / "prompts" / "initial_review.md").read_bytes())
        self._make_fixed_reports()
        self.jobs_dir = base / "jobs"
        self.state_dir = base / "states"
        self.calls = []
        self.runner_started = threading.Event()
        self.runner_release = threading.Event()

        def stub_runner(messages, config):
            self.calls.append((messages, dict(config)))
            self.runner_started.set()
            self.runner_release.wait(timeout=10)
            payload = json.loads(messages[1]["content"])
            first = next(item for item in payload["contract"] if item["text"].strip())
            return json.dumps(result_for_piece(first["id"], first["text"]), ensure_ascii=False)

        self.server = create_server(
            root=self.root,
            state_dir=self.state_dir,
            jobs_dir=self.jobs_dir,
            source_paths={"detailed": None, "minimal": None, "table": None},
            review_runner=stub_runner,
            review_engines=[{
                "key": "stub",
                "label": "测试桩（不是实际AI）",
                "available": True,
                "config_fields": ["model", "api_key"],
                "model": "stub-fixed",
            }],
        )
        self.server.jobs.sources_path = self.root / "sources.json"
        self.server.jobs.service_sources_path = self.root / "sources_service.json"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.port = self.server.server_port

    def _make_fixed_reports(self):
        for key, spec in REPORTS.items():
            run = self.root / "runtime" / spec["directory"]
            run.mkdir(parents=True)
            input_path = run / "fixed.txt"
            input_path.write_text("固定报告测试文字。", encoding="utf-8")
            packet = prepare(input_path, run / "request.json", detailed=True, sources=labor_sources())
            contract = ParsedContract.model_validate(packet["contract"])
            response = {
                "request_digest": packet["request_digest"],
                "model": "fixed-fixture",
                "result": result_for_piece("P000", contract.pieces[0].text),
            }
            (run / "response.json").write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")

    def _stop(self):
        self.runner_release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), raw
        connection.close()
        return result

    def json_request(self, method, path, body=None):
        status, headers, raw = self.request(method, path, body)
        return status, headers, json.loads(raw.decode("utf-8"))

    def create_job(self, *, review_date="2026-09-20"):
        return self.json_request(
            "POST",
            "/api/jobs",
            {
                "document_name": "新上传合同.docx",
                "docx_base64": base64.b64encode(docx_bytes("新合同工资结算条款。")).decode("ascii"),
                "context": {
                    "contract_type": "labor",
                    "region": "上海",
                    "review_date": review_date,
                    "input_kind": "拟签审阅",
                    "payday": 10,
                    "business_goal": "签约前审核现有条款。",
                },
            },
        )

    def create_general(self, *, legacy=False):
        result = self.json_request("POST", "/api/jobs", {
            "document_name": "任意合作合同.docx",
            "docx_base64": base64.b64encode(docx_bytes("甲方应在交付后验收并付款。" )).decode("ascii"),
            "context": {"contract_type_label": "跨行业合作合同", "our_party": "甲方公司",
                        "our_role": "采购决策方", "business_goal": "控制交付风险。"},
        })
        if legacy:
            # Explicit old persisted-task fixture; production create never
            # accepts a payload that disables the workflow-2 gate.
            path = self.jobs_dir / result[2]["job"]["job_id"] / "status.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            for field in ("workflow_version", "preparation", "preparation_input_digest", "checklist_snapshot"):
                value.pop(field, None)
            value["context"].pop("business_facts", None)
            value["context"].pop("draft_plan_version", None)
            value["context"].pop("draft_plan_insertion_anchors", None)
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return result

    def test_general_is_new_default_with_current_date_and_no_labor_fields(self):
        _, _, capabilities = self.json_request("GET", "/api/review-capabilities")
        self.assertEqual([profile["key"] for profile in capabilities["profiles"]], ["general"])
        self.assertEqual(capabilities["default_context"]["review_date"], dt.date.today().isoformat())
        status, _, created = self.create_general()
        self.assertEqual(status, 201)
        self.assertEqual(created["job"]["workflow_version"], 2)
        self.assertEqual(created["job"]["preparation"]["status"], "pending")
        context = created["job"]["context"]
        self.assertEqual(context["contract_type"], "general")
        self.assertEqual(context["contract_type_label"], "跨行业合作合同")
        self.assertEqual(context["our_party"], "甲方公司")
        self.assertEqual(context["our_role"], "采购决策方")
        self.assertEqual(context["review_date"], dt.date.today().isoformat())
        self.assertEqual(context["region"], "中国大陆")
        self.assertNotIn("payday", context)
        self.assertEqual(created["preview"]["scope"]["our_party"], "甲方公司")
        path = self.jobs_dir / created["job"]["job_id"] / "sources.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])
        self.assertEqual(self.calls, [])

    def test_general_fake_research_then_review_and_prompt_snapshot_reloads(self):
        self.server.jobs.engines["compatible_api"] = {
            "key": "compatible_api", "available": True, "config_fields": ["base_url", "api_key", "model"],
        }
        runner_calls = []
        self.server.jobs.runner = lambda messages, config: runner_calls.append((messages, config)) or general_result(messages)
        search_calls = []
        self.server.jobs.research_completion = lambda config, messages: search_calls.append((config, messages)) or {
            "text": "只见检索摘要，法条原文未核。模型另写 https://invented.example 不应升级为来源。",
            "search": {"status": "searched", "queries": ["履行 验收 民法典"],
                       "sources": [{"title": "官方检索候选", "url": "https://www.npc.gov.cn/candidate"}],
                       "request_count": 1},
        }
        status, _, created = self.create_general(legacy=True)
        self.assertEqual(status, 201)
        job_id = created["job"]["job_id"]
        secret = "sk-test-research-key"
        status, _, started = self.json_request("POST", f"/api/jobs/{job_id}/start", {
            "engine_key": "compatible_api", "consent": True, "web_search": True,
            "config": {"base_url": "https://api.deepseek.com/v1", "api_key": secret, "model": "deepseek-flash"},
        })
        self.assertEqual(status, 202)
        completed = self.wait_status(job_id, "completed")
        self.assertEqual(completed["research_status"], "searched")
        self.assertEqual(len(search_calls), 1)
        self.assertEqual(len(runner_calls), 1)
        self.assertIn("初次合同审核", search_calls[0][1][0]["content"])
        self.assertIn("research_memo", runner_calls[0][0][1]["content"])
        directory = self.jobs_dir / job_id
        packet = json.loads((directory / "final-sources" / "research.json").read_text(encoding="utf-8"))
        request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(packet["request_digest"], request["request_digest"])
        self.assertEqual(packet["sources"][0]["id"], "WEB001")
        self.assertFalse(packet["sources"][0]["verified"])
        self.assertNotIn("invented.example", json.dumps(packet["sources"]))
        for path in directory.rglob("*.json"):
            self.assertNotIn(secret, path.read_text(encoding="utf-8"), path)
        (self.root / "prompts" / "initial_review.md").write_text("母本已修改，不应影响旧报告", encoding="utf-8")
        status, _, loaded = self.json_request("GET", f"/api/reports/{job_id}")
        self.assertEqual(status, 200)
        self.assertEqual(loaded["report"]["metadata"]["research"]["status"], "searched")
        self.assertEqual(loaded["report"]["findings"][0]["rule_id"], "G01")
        self.assertEqual(loaded["report"]["findings"][0]["source_ids"], ["WEB001"])
        self.assertEqual(loaded["sources"][-1]["id"], "WEB001")

    def test_general_research_failure_keeps_honest_status_and_no_sources(self):
        self.server.jobs.engines["compatible_api"] = {
            "key": "compatible_api", "available": True, "config_fields": ["base_url", "api_key", "model"],
        }
        self.server.jobs.runner = lambda messages, config: general_result(messages)
        self.server.jobs.research_completion = lambda config, messages: (_ for _ in ()).throw(RuntimeError("fixture unavailable"))
        _, _, created = self.create_general(legacy=True)
        job_id = created["job"]["job_id"]
        status, _, _ = self.json_request("POST", f"/api/jobs/{job_id}/start", {
            "engine_key": "compatible_api", "consent": True, "web_search": True,
            "config": {"base_url": "https://api.deepseek.com", "api_key": "sk-fail-test", "model": "deepseek-flash"},
        })
        self.assertEqual(status, 202)
        completed = self.wait_status(job_id, "completed")
        self.assertEqual(completed["research_status"], "error")
        loaded = self.server.jobs.load_report(job_id)
        self.assertEqual(loaded["report"]["metadata"]["research"]["status"], "error")
        self.assertFalse(any(item["id"].startswith("WEB") for item in loaded["sources"]))
        self.assertEqual(loaded["sources"], [])

    def test_general_without_search_has_no_initial_sources(self):
        self.server.jobs.runner = lambda messages, config: general_result(messages)
        _, _, created = self.create_general(legacy=True)
        job_id = created["job"]["job_id"]
        status, _, _ = self.json_request("POST", f"/api/jobs/{job_id}/start", {
            "engine_key": "stub", "consent": True, "config": {},
        })
        self.assertEqual(status, 202)
        completed = self.wait_status(job_id, "completed")
        self.assertEqual(completed["research_status"], "not_requested")
        loaded = self.server.jobs.load_report(job_id)
        self.assertEqual(loaded["report"]["metadata"]["research"]["status"], "not_requested")
        self.assertFalse(any(item["id"].startswith("WEB") for item in loaded["sources"]))

    def test_partial_research_keeps_only_actual_candidate_as_unverified(self):
        self.server.jobs.engines["compatible_api"] = {
            "key": "compatible_api", "available": True, "config_fields": ["base_url", "api_key", "model"],
        }
        self.server.jobs.runner = lambda messages, config: general_result(messages)
        self.server.jobs.research_completion = lambda config, messages: {
            "text": "工具未完整结束；仅把见到的摘要作为线索。https://model-invented.example 不算来源。",
            "search": {"status": "partial", "queries": ["民法典 履行"],
                       "sources": [{"title": "实际工具候选", "url": "https://www.npc.gov.cn/partial"}],
                       "request_count": 1},
        }
        _, _, created = self.create_general(legacy=True)
        job_id = created["job"]["job_id"]
        status, _, _ = self.json_request("POST", f"/api/jobs/{job_id}/start", {
            "engine_key": "compatible_api", "consent": True, "web_search": True,
            "config": {"base_url": "https://api.deepseek.com", "api_key": "sk-partial-test", "model": "deepseek-flash"},
        })
        self.assertEqual(status, 202)
        completed = self.wait_status(job_id, "completed")
        self.assertEqual(completed["research_status"], "partial")
        loaded = self.server.jobs.load_report(job_id)
        self.assertEqual(loaded["sources"][-1]["url"], "https://www.npc.gov.cn/partial")
        self.assertFalse(loaded["sources"][-1]["verified"])
        self.assertNotIn("model-invented.example", json.dumps(loaded["sources"]))

    def test_general_search_rejects_non_deepseek_without_start(self):
        self.server.jobs.engines["compatible_api"] = {
            "key": "compatible_api", "available": True, "config_fields": ["base_url", "api_key", "model"],
        }
        _, _, created = self.create_general(legacy=True)
        job_id = created["job"]["job_id"]
        status, _, _ = self.json_request("POST", f"/api/jobs/{job_id}/start", {
            "engine_key": "compatible_api", "consent": True, "web_search": True,
            "config": {"base_url": "https://api.other.example/v1", "api_key": "sk-other", "model": "other"},
        })
        self.assertEqual(status, 400)
        self.assertEqual(self.server.jobs.get(job_id)["status"], "ready")
        self.assertEqual(self.calls, [])

    def test_labor_upload_without_payday_uses_unknown_not_default(self):
        status, _, result = self.json_request("POST", "/api/jobs", {
            "document_name": "无发薪日输入.docx",
            "docx_base64": base64.b64encode(docx_bytes("工资按合同约定结算。")).decode("ascii"),
            "context": {"contract_type": "labor", "region": "上海", "review_date": "2026-09-20", "input_kind": "拟签审阅"},
        })
        self.assertEqual(status, 201)
        self.assertEqual(result["job"]["status"], "ready")
        packet = json.loads((self.jobs_dir / result["job"]["job_id"] / "status.json").read_text(encoding="utf-8"))
        self.assertIsNone(packet["context"]["payday"])
        self.assertEqual(self.calls, [])
        _, _, caps = self.json_request("GET", "/api/review-capabilities")
        self.assertNotIn("payday", caps["default_context"])

    def wait_status(self, job_id, wanted, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _, payload = self.json_request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(status, 200)
            if payload["job"]["status"] == wanted:
                return payload["job"]
            time.sleep(0.05)
        self.fail(f"job did not reach {wanted}")

    def test_create_is_free_start_is_explicit_and_duplicate_start_is_idempotent(self):
        status, _, capabilities = self.json_request("GET", "/api/review-capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(capabilities["engines"][0]["key"], "stub")
        status, _, created = self.create_job()
        self.assertEqual(status, 201)
        self.assertEqual(created["job"]["status"], "ready")
        self.assertTrue(created["preview"]["head"])
        self.assertEqual(created["preview"]["pieces_count"], len(created["preview"]["pieces"]))
        self.assertEqual(self.calls, [])
        job_id = created["job"]["job_id"]
        status, _, refreshed_ready = self.json_request("GET", f"/api/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertEqual(refreshed_ready["job"]["preview"], created["preview"])

        secret = "sk-memory-only"
        start_body = {
            "engine_key": "stub",
            "consent": True,
            "config": {"model": "stub-model", "api_key": secret, "executable": "evil.exe"},
        }
        status, _, running = self.json_request("POST", f"/api/jobs/{job_id}/start", start_body)
        self.assertEqual(status, 202)
        self.assertEqual(running["job"]["status"], "running")
        self.assertTrue(self.runner_started.wait(timeout=3))
        status, _, duplicate = self.json_request("POST", f"/api/jobs/{job_id}/start", start_body)
        self.assertEqual(status, 202)
        self.assertEqual(duplicate["job"]["status"], "running")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("executable", self.calls[0][1])
        self.assertEqual(self.calls[0][1]["engine_key"], "stub")
        self.assertEqual(self.calls[0][1]["model"], "stub-fixed")

        self.runner_release.set()
        completed = self.wait_status(job_id, "completed")
        self.assertEqual(completed["report_id"], job_id)
        status, _, again = self.json_request("POST", f"/api/jobs/{job_id}/start", start_body)
        self.assertEqual(status, 200)
        self.assertEqual(again["job"]["status"], "completed")
        self.assertEqual(len(self.calls), 1)

        _, _, listing = self.json_request("GET", "/api/reports")
        self.assertIn(job_id, {item["id"] for item in listing["reports"]})
        status, _, report = self.json_request("GET", f"/api/reports/{job_id}")
        self.assertEqual(status, 200)
        self.assertEqual(report["id"], job_id)
        self.assertEqual(report["report"]["mode"], "job_ai")
        status, _, delivery = self.json_request("GET", f"/api/reports/{job_id}/delivery")
        self.assertEqual(status, 200)
        self.assertTrue(delivery["available"])

        for path in self.jobs_dir.rglob("*.json"):
            self.assertNotIn(secret, path.read_text(encoding="utf-8"), path)

    def test_stale_source_date_is_rejected_before_runner(self):
        status, _, error = self.create_job(review_date="2026-09-21")
        self.assertEqual(status, 400)
        self.assertIn("版本待复核", error["error"])
        self.assertEqual(self.calls, [])

    def test_invalid_raw_response_fails_without_report(self):
        # Replace the injected runner before creating this job; this remains a
        # negative orchestration fixture, not a model result.
        self.server.jobs.runner = lambda messages, config: "not-json"
        status, _, created = self.create_job()
        self.assertEqual(status, 201)
        job_id = created["job"]["job_id"]
        status, _, _ = self.json_request(
            "POST", f"/api/jobs/{job_id}/start",
            {"engine_key": "stub", "consent": True, "config": {"model": "broken"}},
        )
        self.assertEqual(status, 202)
        failed = self.wait_status(job_id, "failed")
        self.assertIsNone(failed["report_id"])
        _, _, listing = self.json_request("GET", "/api/reports")
        self.assertNotIn(job_id, {item["id"] for item in listing["reports"]})
        status, _, _ = self.json_request("GET", f"/api/reports/{job_id}")
        self.assertEqual(status, 404)

    def test_orphaned_running_status_becomes_failed_without_retry(self):
        status, _, created = self.create_job()
        self.assertEqual(status, 201)
        job_id = created["job"]["job_id"]
        status_path = self.jobs_dir / job_id / "status.json"
        raw = json.loads(status_path.read_text(encoding="utf-8"))
        raw.update({"status": "running", "owner_pid": os.getpid(), "engine_key": "stub", "attempt": 1})
        status_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        status, _, recovered = self.json_request("GET", f"/api/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertEqual(recovered["job"]["status"], "failed")
        self.assertIn("未自动重试", recovered["job"]["error"])
        self.assertEqual(self.calls, [])

    def test_legacy_running_without_owner_gets_grace_then_fails_stale(self):
        status, _, created = self.create_job()
        self.assertEqual(status, 201)
        job_id = created["job"]["job_id"]
        status_path = self.jobs_dir / job_id / "status.json"
        raw = json.loads(status_path.read_text(encoding="utf-8"))
        raw.update({"status": "running", "engine_key": "stub", "attempt": 1})
        raw.pop("owner_pid", None)
        status_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        status, _, live = self.json_request("GET", f"/api/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertEqual(live["job"]["status"], "running")
        raw["updated_at"] = "2000-01-01T00:00:00Z"
        status_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        status, _, stale = self.json_request("GET", f"/api/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertEqual(stale["job"]["status"], "failed")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
