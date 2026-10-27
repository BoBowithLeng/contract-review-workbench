"""No packaged corpus is used; synthetic snapshots still replay offline."""
from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from contract_core import RULE_META, _sources_digest
from review_jobs import ReviewJobManager
from tool_review import load_review, prepare


def docx_bytes() -> bytes:
    document = Document()
    document.add_paragraph("双方应协商确认验收时间。")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def payload() -> dict:
    return {
        "document_name": "synthetic.docx",
        "docx_base64": base64.b64encode(docx_bytes()).decode("ascii"),
        "context": {"our_party": "甲方", "our_role": "采购方", "business_goal": "明确验收节点"},
    }


class NoBuiltinSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_general_ignores_even_explicit_legacy_source_paths(self):
        source_path = self.root / "sources.json"
        service_path = self.root / "sources_service.json"
        source_path.write_text("not JSON", encoding="utf-8")
        service_path.write_text("not JSON", encoding="utf-8")
        manager = ReviewJobManager(self.root / "jobs", source_path, service_sources_path=service_path)
        created = manager.create(payload())
        directory = self.root / "jobs" / created["job"]["job_id"]
        self.assertEqual(json.loads((directory / "sources.json").read_text(encoding="utf-8")), [])
        status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["sources_digest"], _sources_digest([]))

    def test_default_capabilities_and_new_job_need_no_source_files(self):
        manager = ReviewJobManager(self.root / "jobs")
        self.assertEqual(manager.capabilities()["profiles"][0]["key"], "general")
        self.assertEqual(manager._sources(), [])
        self.assertEqual(manager._sources("service"), [])
        self.assertEqual(manager.create(payload())["job"]["context"]["contract_type"], "general")
        self.assertFalse((self.root / "sources.json").exists())
        self.assertFalse((self.root / "sources_service.json").exists())

    def test_source_path_does_not_implicitly_load_service_sibling(self):
        source_path = self.root / "sources.json"
        source_path.write_text("[]", encoding="utf-8")
        (self.root / "sources_service.json").write_text("not JSON", encoding="utf-8")
        manager = ReviewJobManager(self.root / "jobs", source_path)
        self.assertEqual(manager._sources("service"), [])

    def test_prompt_path_is_independent_of_source_table(self):
        prompt_path = self.root / "instructions.md"
        prompt_path.write_text("synthetic prompt", encoding="utf-8")
        manager = ReviewJobManager(self.root / "jobs", prompt_path=prompt_path)
        self.assertEqual(manager._initial_prompt(), "synthetic prompt")

    def _run_frozen_general(self, sources):
        calls = []

        def runner(messages, config):
            calls.append(json.loads(messages[1]["content"]))
            return json.dumps({"findings": [], "checks": [{
                "rule_id": "G01", "title": "验收安排", "status": "未检出", "reason": "synthetic stub"
            }], "options": [], "uncovered": [], "limitations": ["synthetic test only"]})

        manager = ReviewJobManager(self.root / "jobs", runner=runner)
        job_id = manager.create(payload())["job"]["job_id"]
        directory = self.root / "jobs" / job_id
        status_path = directory / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        # Model an already-persisted pre-preparation job, not a public bypass.
        for key in ("workflow_version", "preparation", "preparation_input_digest", "checklist_snapshot"):
            status.pop(key, None)
        status.update(sources_digest=_sources_digest(sources), engine_key="injected", model="stub")
        status_path.write_text(json.dumps(status), encoding="utf-8")
        (directory / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
        manager._run(job_id, {"model": "stub", "web_search": False}, "synthetic frozen prompt")
        self.assertEqual(manager.get(job_id)["status"], "completed")
        return manager, job_id, calls

    def test_no_search_review_passes_empty_sources_and_replays_empty(self):
        manager, job_id, calls = self._run_frozen_general([])
        self.assertEqual(calls[0]["sources"], [])
        loaded = manager.load_report(job_id)
        self.assertEqual(loaded["sources"], [])
        self.assertEqual(loaded["report"]["metadata"]["research"]["status"], "not_requested")

    def test_existing_general_snapshot_survives_without_current_root_tables(self):
        frozen = [{"id": "OLD-FIXTURE", "title": "synthetic historical source", "verified": False}]
        manager, job_id, calls = self._run_frozen_general(frozen)
        self.assertEqual(calls[0]["sources"], frozen)
        replay = ReviewJobManager(self.root / "jobs", runner=lambda *_: self.fail("replay made a model call"))
        self.assertEqual(replay.load_report(job_id)["sources"], frozen)
        self.assertEqual(replay.load_report(job_id), manager.load_report(job_id))

    def test_tool_prepare_defaults_empty_without_any_corpus_read(self):
        text = self.root / "input.txt"
        text.write_text("synthetic contract", encoding="utf-8")
        with patch("tool_review.read_json", side_effect=AssertionError("unexpected corpus read")):
            packet = prepare(text, self.root / "request.json")
        self.assertEqual(packet["sources"], [])
        self.assertEqual(json.loads(packet["messages"][1]["content"])["sources"], [])

    def test_tool_replay_uses_explicit_frozen_legacy_sources_only(self):
        text = self.root / "input.txt"
        text.write_text("双方应协商确认验收时间。", encoding="utf-8")
        sources = [{
            "id": sid, "title": "synthetic fixture", "verified": True, "region": "全国",
            "effective_from": "2020-01-01", "effective_to": None, "checked_at": "2026-09-20",
            "text": "synthetic fixture, not legal authority",
        } for sid in sorted({sid for rule in RULE_META.values() for sid in rule["sources"]})]
        request_path = self.root / "request.json"
        packet = prepare(text, request_path, sources=sources)
        response_path = self.root / "response.json"
        response_path.write_text(json.dumps({"request_digest": packet["request_digest"], "model": "stub", "result": {
            "findings": [{"rule_id": rid, "status": "需要法务判断", "risk": None,
                "evidence_status": "适用待判断", "refs": [{"id": "P000", "quote": text.read_text(encoding="utf-8")}],
                "source_ids": [], "reason": "synthetic stub", "suggestion": "待复核", "clause_draft": "", "questions": []}
                for rid in RULE_META], "uncovered": []
        }}), encoding="utf-8")
        from tool_review import read_json

        def frozen_only(path):
            self.assertIn(Path(path), {request_path, response_path})
            return read_json(path)

        with patch("tool_review.read_json", side_effect=frozen_only):
            bundle = load_review(request_path, response_path, model="stub")
        self.assertEqual(bundle["sources"], sources)
        self.assertTrue(bundle["report"]["metadata"]["model_called"])
        self.assertIsNone(bundle["report"]["metadata"]["source_snapshot_current"])


if __name__ == "__main__":
    unittest.main()
