"""Program-owned original paragraphs and frozen-source evidence; no model calls."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from contract_core import parse_text
from general_review import build_messages, parse_response, response_schema
from review_jobs import ReviewJobManager
from tests.test_general_review import context, finding, payload, source
from tests.test_opinion_revisions import full_loaded
from opinion_revisions import OpinionRevisionManager

PROJECT = Path(__file__).resolve().parents[1]


class ProgramEvidenceTests(unittest.TestCase):
    def parse(self, item=None, sources=None, **kw):
        item = item or finding(refs=[{"id": "P000"}])
        return parse_response(json.dumps(payload([item]), ensure_ascii=False),
                              parse_text("押金的退还条件。\n另一段真实文字。"), context(), sources or [], **kw)["findings"][0]

    def test_model_quote_and_evidence_are_non_authoritative(self):
        item = finding(refs=[{"id": "P000", "quote": "恶意假引文", "quote_source": "verified-law"}],
                       evidence_status="模型自创的已核标签")
        before = copy.deepcopy(item)
        got = self.parse(item)
        self.assertEqual(got["refs"], [{"id": "P000", "quote": "押金的退还条件。", "quote_source": "original_paragraph"}])
        self.assertEqual(got["reason"], before["reason"])
        self.assertEqual(item, before)
        self.assertEqual(got["evidence"]["source_state"], "none")
        legal = self.parse(finding(kind="法律", evidence_status="依据已核", refs=[{"id": "P000"}]))
        self.assertEqual(legal["status"], "发现风险")
        self.assertEqual(legal["evidence_status"], "资料不足")
        self.assertEqual(legal["evidence"]["legal_applicability"], "not_assessed")

    def test_ids_and_location_are_still_required(self):
        for item in (finding(refs=[{"id": "P999"}]), finding(refs=[{"id": 7}]),
                     finding(refs=[]), finding(sources=["FAKE"], refs=[{"id": "P000"}])):
            with self.subTest(item=item):
                got = self.parse(item)
                self.assertEqual(got["status"], "处理失败")
                self.assertTrue(got["validation_errors"])
                self.assertEqual(got["title"], item["title"])
                self.assertEqual(got["model_reason"], item["reason"])
        missing = self.parse(finding(refs=[], location_note="已查阅全文，未见期限。"))
        self.assertEqual(missing["status"], "发现风险")
        # A valid but semantically mistaken ID must show the actual selected paragraph,
        # never the model's unrelated quote or a guessed nearby paragraph.
        selected = self.parse(finding(refs=[{"id": "P001", "quote": "押金的退还条件。"}]))
        self.assertEqual(selected["refs"][0]["quote"], "另一段真实文字。")

    def test_frozen_source_states(self):
        good, unchecked = source(), source("LAW2", verified=False)
        for records, state in (([good], "verified_snapshot"), ([unchecked], "unverified"),
                               ([good, unchecked], "mixed"), ([], "none")):
            got = self.parse(finding(kind="法律", sources=[s["id"] for s in records], refs=[{"id": "P000"}]), records)
            self.assertEqual(got["status"], "发现风险")
            self.assertEqual(got["evidence"]["source_state"], state)
            self.assertEqual(got["evidence"]["legal_applicability"], "not_assessed")
        candidate = source("WEB001")  # Even a forged verified flag cannot promote a retrieved snippet.
        got = self.parse(finding(kind="法律", sources=[candidate["id"]], refs=[{"id": "P000"}]), [candidate])
        self.assertEqual(got["evidence"]["sources"][0]["verification_state"], "candidate")
        self.assertFalse(got["evidence"]["sources"][0]["snapshot_verified"])

    def test_incomplete_or_inapplicable_records_never_become_verified(self):
        for changes in ({"text": ""}, {"checked_at": None}, {"region": None}, {"effective_from": None},
                        {"effective_from": "garbage"}, {"checked_at": "2026-09-25"},
                        {"effective_to": "2026-09-23"}, {"region": "美国"}):
            with self.subTest(changes=changes):
                record = {**source(), **changes}
                got = self.parse(finding(kind="法律", sources=["LAW1"], refs=[{"id": "P000"}]), [record])
                self.assertEqual(got["status"], "发现风险")
                self.assertFalse(got["evidence"]["sources"][0]["snapshot_verified"])
                self.assertTrue(got["evidence"]["sources"][0]["verification_notes"])

    def test_option_sources_have_same_program_owned_evidence_as_findings(self):
        base = finding(refs=[{"id": "P000"}])
        option = {"id": "O1", "title": "处理方案", "finding_ids": ["F01"], "preconditions": [],
                  "steps": [], "cost": "", "remaining_risk": "", "clause_draft": "", "relationship": ""}
        for records, expected in (([source(), source("LAW2", verified=False)], "mixed"),
                                  ([{**source(), "text": "", "checked_at": None}], "unverified"),
                                  ([{**source(), "checked_at": "2026-09-25"}], "unverified"),
                                  ([], "none")):
            with self.subTest(expected=expected, records=records):
                option["source_ids"] = [s["id"] for s in records]
                raw = json.dumps(payload([base], options=[option]), ensure_ascii=False)
                result = parse_response(raw, parse_text("押金的退还条件。"), context(), records)
                self.assertEqual(result["options"][0]["evidence"]["source_state"], expected)
                self.assertEqual(result["options"][0]["evidence"]["legal_applicability"], "not_assessed")
        option["source_ids"] = []
        old = parse_response(json.dumps(payload([finding()], options=[option])),
                             parse_text("押金不退。"), context(), [], quote_validation_version=1)
        self.assertNotIn("evidence", old["options"][0])

    def test_new_schema_is_ids_only_but_old_protocol_stays_frozen(self):
        new = response_schema()["properties"]["findings"]["items"]
        self.assertNotIn("evidence_status", new["properties"])
        self.assertEqual(new["properties"]["refs"]["items"]["required"], ["id"])
        old = response_schema(quote_validation_version=0)["properties"]["findings"]["items"]
        self.assertIn("evidence_status", old["properties"])
        self.assertEqual(old["properties"]["refs"]["items"]["required"], ["id", "quote"])
        old_messages = build_messages(parse_text("合同"), context(), [], quote_validation_version=0)
        self.assertIn("refs 的 quote 必须", old_messages[0]["content"])
        new_messages = build_messages(parse_text("合同"), context(), [])
        self.assertIn("不抄写 quote", new_messages[0]["content"])

    def test_opinion_new_candidate_ids_only_and_historical_validation_stays_old(self):
        loaded = full_loaded()
        manager = OpinionRevisionManager.__new__(OpinionRevisionManager)
        base = loaded["report"]["findings"][0]
        item = {"base_finding": base, "source_candidates": [], "quote_validation_version": 2}
        revised = copy.deepcopy(base)
        revised["refs"] = [{"id": "P001"}]
        revised.pop("evidence_status")
        data = {"action": "revise", "finding": revised, "change_summary": "核对定位。", "related_finding_ids": []}
        new = manager._validate_output(json.dumps(data), item, loaded, {})[1]
        self.assertEqual(new["refs"][0]["quote"], loaded["contract"]["pieces"][0]["text"])
        self.assertEqual(new["refs"][0]["quote_source"], "original_paragraph")
        old_item = {"base_finding": base, "source_candidates": []}
        revised["evidence_status"] = "原文可核"
        revised["refs"][0]["quote"] = "伪造引文"
        with self.assertRaisesRegex(ValueError, "逐字"):
            manager._validate_output(json.dumps(data), old_item, loaded, {})


class FrozenCurrentReportTests(unittest.TestCase):
    def test_real_f14_f18_upgrade_only_on_temporary_copy(self):
        job_id = "job-3f6a3da1312e41789a25c80a53eeb72f"
        original = PROJECT / "runtime" / "review-jobs" / job_id
        if not original.is_dir():
            self.skipTest("local saved report fixture not available")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "jobs"
            shutil.copytree(original, root / job_id)
            directory = root / job_id
            state_dir = Path(temp) / "reader-state"
            shutil.copytree(PROJECT / "runtime" / "reader-state", state_dir)
            manager = ReviewJobManager(root, None, lambda *_: self.fail("paid model call"), prompt_path=PROJECT / "prompts" / "initial_review.md")
            immutable = {path.relative_to(directory): path.read_bytes() for path in directory.rglob("*")
                         if path.is_file() and path.name not in {"result.json", "status.json"}}
            old = manager.load_report(job_id)
            if old["report"]["metadata"].get("quote_validation_version", 0) >= 2:
                self.skipTest("production explicitly upgraded already")
            before = (directory / "result.json").read_bytes()
            self.assertEqual(manager.load_report(job_id), old)
            self.assertEqual((directory / "result.json").read_bytes(), before)
            updated = manager.revalidate_saved_report(job_id, state_dir=state_dir)
            wanted = {f["id"]: f for f in updated["report"]["findings"]}
            for key in ("findings", "checks", "checklist_results"):
                self.assertFalse(any(row["status"] == "处理失败" for row in updated["report"].get(key, [])), key)
            self.assertEqual(manager.get(job_id)["status"], "completed")
            self.assertNotEqual(wanted["F14"]["status"], "处理失败")
            self.assertNotEqual(wanted["F18"]["status"], "处理失败")
            self.assertEqual(wanted["F14"]["evidence"]["source_state"], "verified_snapshot")
            self.assertEqual(wanted["F18"]["refs"][0]["quote_source"], "original_paragraph")
            self.assertEqual(updated["report"]["review_id"], old["report"]["review_id"])
            for relative, contents in immutable.items():
                self.assertEqual((directory / relative).read_bytes(), contents)
            committed = {(directory / name).name: (directory / name).read_bytes() for name in ("result.json", "status.json")}
            manager.revalidate_saved_report(job_id, state_dir=state_dir)
            for name, contents in committed.items():
                self.assertEqual((directory / name).read_bytes(), contents)


if __name__ == "__main__":
    unittest.main()
