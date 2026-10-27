"""Bounded whitespace equivalence and explicit saved-report replay; no AI calls."""
import base64
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from contract_core import parse_text
from document_view import reader_markup
from general_review import parse_response, review_detailed
from review_jobs import JobError, ReviewJobManager, _atomic_json, _review_detailed


PROJECT = Path(__file__).resolve().parents[1]
VERSION_FIELD = "quote_validation_version"
CONTEXT = {"contract_type": "general", "contract_type_label": "合作合同", "our_party": "甲方",
           "region": "中国大陆", "review_date": "2026-10-04", "input_kind": "拟签审阅"}


def finding(quote, *, fid="F01", pid="P000", **updates):
    value = {"id": fid, "rule_id": "G" + fid[1:], "theme": "付款", "title": "付款安排",
             "issue_kind": "商业", "status": "发现风险", "risk": "中", "evidence_status": "原文可核",
             "refs": [{"id": pid, "quote": quote}], "location_note": "", "source_ids": [],
             "reason": "核对付款安排。", "suggestion": "协商明确期限。", "clause_draft": "", "questions": []}
    value.update(updates)
    return value


def answer(findings):
    return json.dumps({"findings": findings,
                       "checks": [{"rule_id": f["rule_id"], "title": f["theme"], "status": f["status"],
                                   "reason": "核对原文。"} for f in findings],
                       "options": [], "uncovered": [], "limitations": []}, ensure_ascii=False)


class WhitespaceQuoteTests(unittest.TestCase):
    def validate(self, text, quote, *, version=1, **updates):
        parsed = parse_text(text)
        result = parse_response(answer([finding(quote, **updates)]), parsed, CONTEXT, [],
                                quote_validation_version=version)
        return parsed, result

    def test_only_equivalent_spaces_reanchor_to_actual_slice(self):
        cases = [("前缀：付款\u00a0\u202f100元。", "付款  100元。"),
                 ("付款 100元。", "付款\u00a0100元。"),
                 ("付款\u202f100元。", "付款\u00a0100元。")]
        for text, quote in cases:
            with self.subTest(text=text, quote=quote):
                parsed, result = self.validate(text, quote)
                item = result["findings"][0]
                self.assertEqual(item["status"], "发现风险")
                anchored = item["refs"][0]["quote"]
                self.assertIn(anchored, text)
                self.assertEqual(len(anchored), len(quote))
                self.assertNotEqual(anchored, quote)
                markup = reader_markup(parsed, result["findings"], "F01")
                self.assertEqual(markup["target"], "p-P000")
                self.assertIn("<mark>" + anchored + "</mark>", markup["html"])

    def test_exact_quotes_and_input_are_not_changed(self):
        text = "付款\u00a0100元。付款\u202f100元。"
        raw_finding = finding("付款\u00a0100元。")
        original = copy.deepcopy(raw_finding)
        raw = answer([raw_finding])
        result = parse_response(raw, parse_text(text), CONTEXT, [], quote_validation_version=1)
        self.assertEqual(result["findings"][0], original)
        self.assertEqual(json.loads(raw)["findings"][0], original)

    def test_legacy_algorithm_stays_strict_and_has_original_report_shape(self):
        _, old = self.validate("付款\u00a0100元。", "付款 100元。", version=0)
        self.assertEqual(old["findings"][0]["status"], "处理失败")
        raw = answer([finding("付款 100元。")])
        parsed = parse_text("付款\u00a0100元。")
        report = review_detailed(parsed, CONTEXT, [], {"model": "stub", VERSION_FIELD: 0},
                                 completion=lambda *_: raw)
        self.assertNotIn(VERSION_FIELD, report["metadata"])
        fresh = review_detailed(parsed, CONTEXT, [], {"model": "stub"}, completion=lambda *_: raw)
        self.assertEqual(fresh["metadata"][VERSION_FIELD], 2)
        self.assertEqual(fresh["review_id"], report["review_id"])

    def test_character_punctuation_amount_and_other_whitespace_changes_fail(self):
        cases = [("甲方的\u00a0付款。", "甲方 付款。"),
                 ("付款\u00a0100元。", "付款 100元，"),
                 ("付款\u00a0100元。", "付款 1000元。"),
                 ("付款\u00a0\u00a0100元。", "付款 100元。"),
                 ("付款\u00a0100元。", "付款100元。"),
                 ("付款\t100元。", "付款 100元。"),
                 ("付款\u3000100元。", "付款 100元。"),
                 ("付款\n100元。", "付款 100元。"),
                 ("付款\u00a0100元。", "付款\n100元。")]
        for text, quote in cases:
            with self.subTest(text=text, quote=quote):
                _, result = self.validate(text, quote)
                self.assertEqual(result["findings"][0]["status"], "处理失败")

    def test_ambiguous_relaxed_match_wrong_paragraph_and_empty_quote_fail(self):
        cases = [("付款\u00a0100元。付款\u202f100元。", "付款 100元。", "P000"),
                 ("付款\u00a0100元。\n另一段。", "付款 100元。", "P001"),
                 ("付款\u00a0100元。", "付款 100元。", "P999"),
                 ("付款\u00a0100元。", "", "P000")]
        for text, quote, pid in cases:
            with self.subTest(text=text, quote=quote, pid=pid):
                _, result = self.validate(text, quote, pid=pid)
                self.assertEqual(result["findings"][0]["status"], "处理失败")

    def test_legal_evidence_rule_is_not_relaxed(self):
        _, result = self.validate("付款\u00a0100元。", "付款 100元。", issue_kind="法律")
        self.assertEqual(result["findings"][0]["status"], "处理失败")
        self.assertIn("法律结论", result["findings"][0]["reason"])


class SavedReportWhitespaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = 0
        self.state_dir = Path(self.temp.name) / "reader-state"
        self.state_dir.mkdir()

        def runner(messages, config):
            self.calls += 1
            return answer([finding("甲方", fid="F01"), finding("付款 100元。", fid="F02")])

        self.manager = ReviewJobManager(Path(self.temp.name) / "jobs", None, runner, prompt_path=PROJECT / "prompts" / "initial_review.md")
        document = Document()
        document.add_paragraph("甲方付款\u00a0100元。")
        output = io.BytesIO()
        document.save(output)
        self.docx = output.getvalue()

    def make_saved(self, *, version=0):
        created = self.manager.create({"document_name": "测试.docx", "context": CONTEXT,
                                       "docx_base64": base64.b64encode(self.docx).decode()})
        job_id = created["job"]["job_id"]
        directory = self.manager._directory(job_id)
        status = self.manager._status(job_id)
        # A historical task fixture, not a production route around preparation.
        for key in ("workflow_version", "preparation", "preparation_input_digest", "checklist_snapshot", VERSION_FIELD):
            status.pop(key, None)
        for key in ("draft_plan_version", "draft_plan_insertion_anchors"):
            status["context"].pop(key, None)
        status.update(engine_key="injected", model="stub", status="running")
        _atomic_json(directory / "status.json", status)

        def with_version(parsed, context, sources, config, raw, **kwargs):
            return _review_detailed(parsed, context, sources, {**config, VERSION_FIELD: version}, raw, **kwargs)

        with patch("review_jobs._review_detailed", side_effect=with_version):
            self.manager._run(job_id, {"model": "stub", "engine_key": "injected"}, "原样保存的业务稿。")
        self.assertEqual(self.manager.get(job_id)["status"], "completed")
        return job_id, directory

    def test_saved_legacy_replays_without_migration_then_selected_revalidation(self):
        job_id, directory = self.make_saved()
        unrelated_id, unrelated_dir = self.make_saved()
        immutable = {path.relative_to(directory): path.read_bytes() for path in directory.rglob("*")
                     if path.is_file() and path.name not in {"status.json", "result.json"}}
        old_result = (directory / "result.json").read_bytes()
        unrelated_result = (unrelated_dir / "result.json").read_bytes()
        old = self.manager.load_report(job_id)
        self.assertEqual(old["report"]["findings"][-1]["status"], "处理失败")
        self.assertNotIn(VERSION_FIELD, old["report"]["metadata"])
        self.assertEqual((directory / "result.json").read_bytes(), old_result)
        self.manager.runner = lambda *_: self.fail("revalidation invoked a model")
        self.manager.research_completion = lambda *_: self.fail("revalidation invoked search")
        self.manager._research = lambda *_: self.fail("revalidation reran research")
        new = self.manager.revalidate_saved_report(job_id, state_dir=self.state_dir)
        self.assertEqual(new["report"]["metadata"][VERSION_FIELD], 2)
        self.assertEqual(new["report"]["review_id"], old["report"]["review_id"])
        self.assertEqual(new["request_digest"], old["request_digest"])
        self.assertTrue(all(f["status"] == "发现风险" for f in new["report"]["findings"]))
        self.assertEqual(new["report"]["findings"][1]["refs"][0]["quote"], "甲方付款\u00a0100元。")
        self.assertEqual((unrelated_dir / "result.json").read_bytes(), unrelated_result)
        for relative, data in immutable.items():
            self.assertEqual((directory / relative).read_bytes(), data)
        self.assertEqual(self.calls, 2)
        new_result = (directory / "result.json").read_bytes()
        new_status = (directory / "status.json").read_bytes()
        self.manager.revalidate_saved_report(job_id, state_dir=self.state_dir)
        self.assertEqual((directory / "result.json").read_bytes(), new_result)
        self.assertEqual((directory / "status.json").read_bytes(), new_status)
        self.assertNotIn(VERSION_FIELD, self.manager.load_report(unrelated_id)["report"]["metadata"])

    def test_fresh_saved_report_replays_with_new_version(self):
        job_id, _ = self.make_saved(version=1)
        report = self.manager.load_report(job_id)["report"]
        self.assertEqual(report["metadata"][VERSION_FIELD], 1)
        self.assertTrue(all(f["status"] == "发现风险" for f in report["findings"]))

    def test_version_tampering_and_existing_result_tampering_are_rejected(self):
        for tamper in ("version", "finding", "response", "unsupported"):
            with self.subTest(tamper=tamper):
                job_id, directory = self.make_saved()
                before_status = (directory / "status.json").read_bytes()
                bundle = json.loads((directory / "result.json").read_text(encoding="utf-8"))
                if tamper == "version":
                    bundle["report"]["metadata"][VERSION_FIELD] = 1
                elif tamper == "finding":
                    bundle["report"]["findings"][0]["reason"] = "伪造"
                elif tamper == "unsupported":
                    bundle["report"]["metadata"][VERSION_FIELD] = 99
                else:
                    response = json.loads((directory / "response.json").read_text(encoding="utf-8"))
                    response["raw_text"] += " "
                    _atomic_json(directory / "response.json", response)
                _atomic_json(directory / "result.json", bundle)
                before_result = (directory / "result.json").read_bytes()
                with self.assertRaises(JobError):
                    self.manager.revalidate_saved_report(job_id, state_dir=self.state_dir)
                self.assertEqual((directory / "result.json").read_bytes(), before_result)
                self.assertEqual((directory / "status.json").read_bytes(), before_status)

    def test_finding_bound_history_rejects_upgrade_before_any_write(self):
        from clause_discussion import ClauseDiscussionManager, _default_state
        from opinion_revisions import OpinionRevisionManager, _write_state
        from tests.test_clause_discussion import FakeDelivery
        from draft_preparation import DraftPreparationManager
        from review_jobs import JobConflict, _canonical, _sha256
        for kind in ("opinion-revisions", "discussions", "drafts"):
            with self.subTest(kind=kind):
                job_id, directory = self.make_saved(version=1)
                old = self.manager.load_report(job_id)
                delivery = FakeDelivery(configured=False)
                discussions = ClauseDiscussionManager(self.state_dir / "discussions", delivery)
                revisions = OpinionRevisionManager(self.state_dir / "opinion-revisions", discussions)
                fid = "F01"
                source = revisions._context(old, fid)
                finding = next(f for f in old["report"]["findings"] if f["id"] == fid)
                finding_sha = _sha256(_canonical(finding))
                if kind == "opinion-revisions":
                    path = revisions._path(old, fid)
                    _write_state(path, revisions._default(old, fid, source))
                elif kind == "discussions":
                    path = discussions._path(old, fid)
                    _atomic_json(path, _default_state(old, fid, finding_sha, source["source_binding"]))
                else:
                    drafts = DraftPreparationManager(self.state_dir / "drafts", delivery, None)
                    candidate_id = "f" * 64
                    path = drafts._directory(candidate_id) / "operation.json"
                    operation = {"candidate_id": candidate_id, "status": "ready", "report_id": job_id,
                                 "request_digest": old["request_digest"], "finding_id": fid,
                                 "finding_sha256": finding_sha, "source_sha256": "a" * 64,
                                 "based_on_revision": 0, "draft_sha256": "b" * 64,
                                 "clarification_sha256": "c" * 64, "edits": [], "questions": [],
                                 "comment": "", "error": None}
                    _atomic_json(path, operation)
                    self.assertEqual(drafts._operation(candidate_id), operation)
                before = {p: p.read_bytes() for p in [directory / "result.json", directory / "status.json", path]}
                with self.assertRaisesRegex(JobConflict, "历史"):
                    self.manager.revalidate_saved_report(job_id, state_dir=self.state_dir)
                for p, contents in before.items():
                    self.assertEqual(p.read_bytes(), contents)
                still_old = self.manager.load_report(job_id)
                self.assertEqual(still_old, old)
                if kind == "opinion-revisions":
                    self.assertEqual(revisions.apply(still_old)["report"], old["report"])
                elif kind == "discussions":
                    self.assertEqual(discussions.get(still_old, fid)["turns"], [])
                else:
                    drafts._assert_binding(operation, still_old, fid, finding_sha, "a" * 64)

    def test_upgrade_requires_real_state_root_and_keeps_unbound_cache(self):
        from review_jobs import JobError
        job_id, directory = self.make_saved(version=1)
        old = self.manager.load_report(job_id)
        cache = self.state_dir / "delivery" / "source-cache.json"
        _atomic_json(cache, {"request_digest": old["request_digest"], "source_sha256": "a" * 64})
        contents = cache.read_bytes()
        before = (directory / "result.json").read_bytes()
        with self.assertRaises(JobError):
            self.manager.revalidate_saved_report(job_id, state_dir=self.state_dir / "missing")
        self.assertEqual((directory / "result.json").read_bytes(), before)
        self.manager.revalidate_saved_report(job_id, state_dir=self.state_dir)
        self.assertEqual(cache.read_bytes(), contents)

    def test_status_write_failure_rolls_back_derived_files(self):
        job_id, directory = self.make_saved()
        original_result = (directory / "result.json").read_bytes()
        original_status = (directory / "status.json").read_bytes()
        failed = False

        def fail_status_once(path, value):
            nonlocal failed
            if path.name == "status.json" and not failed:
                failed = True
                raise OSError("injected write failure")
            _atomic_json(path, value)

        with patch("review_jobs._atomic_json", side_effect=fail_status_once):
            with self.assertRaises(OSError):
                self.manager.revalidate_saved_report(job_id, state_dir=self.state_dir)
        self.assertEqual((directory / "result.json").read_bytes(), original_result)
        self.assertEqual((directory / "status.json").read_bytes(), original_status)
        self.manager.load_report(job_id)


if __name__ == "__main__":
    unittest.main()
