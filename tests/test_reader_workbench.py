from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from contract_core import parse_docx
from reader_server import create_server
from reader_workbench import build_workbench, _pending_item


def make_docx(path: Path, text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    document.save(path)
    return path.read_bytes()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def loaded(report_id: str, document_name: str, source: bytes | None, *, context=None, findings=3, title=None):
    if source is None:
        fingerprint = hashlib.sha256((report_id + "-text").encode()).hexdigest()
    else:
        fingerprint = parse_docx(source).fingerprint
    values = [{"id": f"F{i + 1}", "refs": []} for i in range(findings)]
    return {
        "id": report_id,
        "title": title or f"审查 {report_id}",
        "document_name": document_name,
        "request_digest": hashlib.sha256(report_id.encode()).hexdigest(),
        "contract": {"fingerprint": fingerprint, "pieces": []},
        "report": {"fingerprint": fingerprint, "context": context or {}, "findings": values},
    }


class FakeJobs:
    def __init__(self, root: Path, reports):
        self.root = root
        self.reports = reports
        self.load_calls = []
        self.model_calls = 0

    def load_report(self, job_id):
        self.load_calls.append(job_id)
        value = self.reports[job_id]
        if isinstance(value, Exception):
            raise value
        return value


class FakeDelivery:
    def __init__(self, states):
        self.states = states
        self.lock = threading.RLock()
        self.describe_calls = 0

    def describe(self, loaded):
        self.describe_calls += 1
        raise AssertionError("workbench must not call describe")

    def _load_state(self, loaded, data, source_sha):
        value = self.states.get(loaded["id"], {"edits": [], "comments": [], "created_at": None})
        if isinstance(value, Exception):
            raise value
        return value


class WorkbenchMetadataTests(unittest.TestCase):
    def test_excluded_archived_records_are_not_loaded_and_active_records_are_reused(self):
        first = self.add_status("a", "completed", name="已归档.docx", source=self.service_a)
        second = self.add_status("b", "completed", name="当前合同.docx", source=self.service_b)
        active = loaded(second, "当前合同.docx", self.service_b)
        jobs = FakeJobs(self.jobs_root, {first: AssertionError("archived should not load"), second: active})
        seen = {}
        result = build_workbench(legacy_ids=["detailed"], legacy_specs={},
            load_legacy=lambda _: self.fail("archived legacy should not load"), jobs=jobs,
            state_dir=self.root / "state", source_paths={}, delivery=FakeDelivery({}),
            load_review_state=lambda _: {"reviews": {}}, excluded_ids={first, "detailed"}, loaded_reports=seen)
        self.assertEqual(jobs.load_calls, [second])
        self.assertIs(seen[second], active)
        self.assertEqual([r["id"] for d in result["documents"] for r in d["reviews"]], [second])

    def test_preparation_metadata_reaches_pending_row(self):
        item = _pending_item({
            "job_id": "job-" + "a" * 32, "status": "failed", "workflow_version": 2,
            "context": {"contract_type": "general", "our_party": "甲方", "our_role": "客户"},
            "preparation": {"status": "failed"}, "report_id": None,
        })
        self.assertEqual(item["preparation"]["status"], "failed")
        self.assertEqual(item["our_party"], "甲方")
        self.assertEqual(item["our_role"], "客户")
        self.assertEqual(item["contract_type"], "general")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs_root = self.root / "jobs"
        self.jobs_root.mkdir()
        self.service_a = make_docx(self.root / "service-a.docx", "同一份服务合同。")
        self.service_b = make_docx(self.root / "service-b.docx", "同名但内容不同的服务合同。")
        self.labor = make_docx(self.root / "labor.docx", "劳动合同原文。")

    def add_status(self, suffix, status, *, name, source, context=None, created="2026-09-20T10:00:00Z", updated="2026-09-20T10:01:00Z", error=None):
        job_id = "job-" + suffix * 32
        directory = self.jobs_root / job_id
        directory.mkdir()
        if source is not None:
            (directory / "source.docx").write_bytes(source)
        value = {
            "job_id": job_id, "status": status, "report_id": job_id if status == "completed" else None,
            "document_name": name, "created_at": created, "updated_at": updated,
            "source_sha256": sha(source) if source is not None else None,
            "findings_count": 3, "context": context or {}, "error": error,
        }
        (directory / "status.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return job_id

    def test_groups_exact_sources_and_reports_actual_progress_without_writes(self):
        client_context = {"contract_type": "software_service", "our_role": "client", "our_party": "甲方"}
        provider_context = {"contract_type": "software_service", "our_role": "provider", "our_party": "乙方"}
        client = self.add_status("1", "completed", name="服务合同A.docx", source=self.service_a,
                                 context=client_context, updated="2026-09-20T10:03:00Z")
        provider = self.add_status("2", "completed", name="同原稿的另一文件名.docx", source=self.service_a,
                                   context=provider_context, updated="2026-09-20T10:04:00Z")
        repeated = self.add_status("9", "completed", name="同立场再审.docx", source=self.service_a,
                                   context=client_context, updated="2026-09-20T10:04:30Z")
        different = self.add_status("3", "completed", name="服务合同A.docx", source=self.service_b,
                                    context=client_context, updated="2026-09-20T10:05:00Z")
        labor_job = self.add_status("4", "completed", name="劳动合同另名.docx", source=self.labor,
                                    updated="2026-09-20T10:06:00Z")
        broken = self.add_status("5", "completed", name="损坏结果.docx", source=self.service_b,
                                 context=provider_context, updated="2026-09-20T10:07:00Z")
        ready = self.add_status("6", "ready", name="待开始.docx", source=self.service_b, context=client_context)
        running = self.add_status("7", "running", name="正在审核.docx", source=self.service_b, context=provider_context)
        failed = self.add_status("8", "failed", name="审核失败.docx", source=self.service_b, context=client_context,
                                 error="审核执行因服务重启或线程中断而停止；未自动重试。")

        reports = {
            client: loaded(client, "服务合合A.docx", self.service_a, context=client_context),
            provider: loaded(provider, "另名.docx", self.service_a, context=provider_context),
            repeated: loaded(repeated, "同立场再审.docx", self.service_a, context=client_context),
            different: loaded(different, "服务合同A.docx", self.service_b, context=client_context),
            labor_job: loaded(labor_job, "劳动合同另名.docx", self.labor),
            broken: RuntimeError("private path and secret must not escape"),
        }
        jobs = FakeJobs(self.jobs_root, reports)
        legacy_loaded = {
            "detailed": loaded("detailed", "劳动合同模板.docx", self.labor, title="完整模板 · 详细审查"),
            "minimal": loaded("minimal", "纯文本合同", None, findings=1, title="纯文本 · 审查"),
        }
        delivery = FakeDelivery({
            client: {
                "created_at": "2026-09-22T09:28:41Z",
                "edits": [{"id": "edit-F1-P001", "finding_ids": ["F1"]}],
                "comments": [{"id": "note-F1", "edit_id": "edit-F1-P001"}],
            },
        })
        review_states = {
            client: {"reviews": {
                "F1": {"decision": "未复核"},
                "F2": {"decision": "驳回"},
                "F3": {"decision": "接受"},  # historical accept is not an actual document action
            }},
        }
        source_paths = {"detailed": self.root / "labor.docx", "minimal": None}
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result = build_workbench(
            legacy_ids=["detailed", "minimal"],
            legacy_specs={
                "detailed": {"title": "完整模板 · 详细审查", "document_name": "劳动合同模板.docx"},
                "minimal": {"title": "纯文本 · 审查", "document_name": "纯文本合同"},
            },
            load_legacy=lambda report_id: legacy_loaded[report_id],
            jobs=jobs,
            state_dir=self.root / "state",
            source_paths=source_paths,
            delivery=delivery,
            load_review_state=lambda item: review_states.get(item["id"], {"reviews": {}}),
        )
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(delivery.describe_calls, 0)
        self.assertEqual(jobs.model_calls, 0)

        documents = result["documents"]
        by_report = {item["reviews"][0]["id"]: item for item in documents}
        self.assertEqual(len(documents), 8)
        self.assertTrue(all(item["review_count"] == 1 and len(item["reviews"]) == 1 for item in documents))
        self.assertEqual(len({item["id"] for item in documents}), len(documents))
        customer_document = by_report[client]
        self.assertEqual(customer_document["id"], "doc-" + sha(("report:" + client).encode())[:24])
        self.assertNotEqual(customer_document["id"], by_report[provider]["id"])
        self.assertNotEqual(customer_document["id"], by_report[repeated]["id"])
        self.assertEqual(customer_document["legacy_document_id"], by_report[provider]["legacy_document_id"])
        self.assertEqual(customer_document["legacy_document_id"], by_report[repeated]["legacy_document_id"])
        self.assertEqual(customer_document["document_name"], "服务合同A.docx")
        self.assertEqual(customer_document["contract_type"], "software_service")
        self.assertNotEqual(customer_document["id"], by_report[different]["id"])
        client_review = customer_document["reviews"][0]
        self.assertEqual(client_review["party_label"], "客户 · 甲方")
        self.assertEqual(client_review["status"], "复核中")
        self.assertEqual(client_review["handled_count"], 2)
        self.assertEqual(client_review["modified_count"], 1)
        self.assertEqual(client_review["comment_count"], 1)
        self.assertEqual(client_review["updated_at"], "2026-09-22T09:28:41Z")
        self.assertNotIn("contract_type", client_review)

        self.assertNotEqual(by_report["detailed"]["id"], by_report[labor_job]["id"])
        self.assertEqual(by_report["detailed"]["legacy_document_id"], by_report[labor_job]["legacy_document_id"])
        text_review = next(r for d in documents for r in d["reviews"] if r["id"] == "minimal")
        self.assertIsNone(text_review["created_at"])
        self.assertIsNone(text_review["updated_at"])

        broken_review = next(r for d in documents for r in d["reviews"] if r["id"] == broken)
        self.assertEqual(by_report[broken]["id"], "doc-" + sha(("report:" + broken).encode())[:24])
        self.assertFalse(broken_review["available"])
        self.assertEqual(broken_review["status"], "结果已生成")
        self.assertNotIn("private path", broken_review["error"])
        self.assertTrue(client_review["available"])

        pending = {item["id"]: item for item in result["pending_jobs"]}
        self.assertEqual(set(pending), {ready, running, failed})
        self.assertEqual(pending[failed]["error"], "上次审查中断了，需要重新运行。")
        self.assertEqual(pending[ready]["party_label"], "客户 · 甲方")

    def test_one_broken_delivery_state_does_not_drop_other_reviews(self):
        first = self.add_status("a", "completed", name="A.docx", source=self.service_a)
        second = self.add_status("b", "completed", name="B.docx", source=self.service_b)
        jobs = FakeJobs(self.jobs_root, {
            first: loaded(first, "A.docx", self.service_a),
            second: loaded(second, "B.docx", self.service_b),
        })
        delivery = FakeDelivery({first: RuntimeError("broken")})
        result = build_workbench(
            legacy_ids=[], legacy_specs={}, load_legacy=lambda _: None,
            jobs=jobs, state_dir=self.root / "state", source_paths={}, delivery=delivery,
            load_review_state=lambda _: {"reviews": {}},
        )
        reviews = {review["id"]: review for document in result["documents"] for review in document["reviews"]}
        self.assertFalse(reviews[first]["available"])
        self.assertTrue(reviews[second]["available"])


class WorkbenchHttpTests(unittest.TestCase):
    def test_get_route_returns_catalog_without_runner_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = 0

            def runner(messages, config):
                nonlocal calls
                calls += 1
                raise AssertionError("GET must not run a model")

            server = create_server(root=root, state_dir=root / "state", jobs_dir=root / "jobs", review_runner=runner)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                expected = {"documents": [], "pending_jobs": []}
                with patch("reader_server.build_workbench", return_value=expected) as build:
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    connection.request("GET", "/api/workbench", headers={"Host": f"127.0.0.1:{server.server_port}"})
                    response = connection.getresponse()
                    body = json.loads(response.read().decode("utf-8"))
                    connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(body, expected)
                build.assert_called_once()
                self.assertEqual(calls, 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
