from __future__ import annotations

from tests.source_fixtures import labor_sources, write_sources

import copy
import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from docx import Document

from contract_core import ParsedContract
from draft_preparation import (
    DraftPreparationConflict,
    DraftPreparationError,
    DraftPreparationManager,
    DRAFT_SCHEMA_VERSION,
    _bound_targets,
    _insertion_anchors,
    _validate_result,
)
from review_runner import output_schema
from reader_server import REPORTS, create_server
from tool_review import prepare


PROJECT = Path(__file__).resolve().parents[1]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def actions(*piece_ids, action="modify"):
    return [{"piece_id": key, "action": action,
             "reason": "依本次意见明确修改该段约定。" if action == "modify" else "该段业务期限尚需业务人员确认。"}
            for key in piece_ids]


def model_json(value, messages):
    value = copy.deepcopy(value)
    data = json.loads(messages[1]["content"].split("UNTRUSTED DATA:\n", 1)[1])
    value.setdefault("insertions", [])
    value.setdefault("suggestion_actions", [{
        "unit_id": unit["unit_id"],
        "action": "implemented" if value["status"] == "ready" else "needs_confirmation",
        "edit_piece_ids": [item["piece_id"] for item in value["edits"]],
        "insertion_indices": list(range(len(value["insertions"]))),
        "reason": "已按建议落实到对应条款。" if value["status"] == "ready" else "业务期限尚未确认，暂不能落实。",
    } for unit in data["suggestion_units"]])
    return json.dumps(value, ensure_ascii=False)


class FakeDelivery:
    def __init__(self, pieces):
        self.sha = digest("source")
        self.revision = 0
        self.edits = []
        self.pieces = [
            {"id": key, "text": text, "paragraph_sha256": digest(text), "location": f"正文{key}",
             "supports_paragraph_insertion": True}
            for key, text in pieces.items()
        ]

    def describe(self, loaded):
        return {
            "available": True,
            "reason": "",
            "source": {"source_sha256": self.sha, "pieces": self.pieces},
            "state": {
                "revision": self.revision,
                "source_sha256": self.sha,
                "author": "",
                "created_at": None,
                "edits": self.edits,
                "comments": [],
                "can_undo": False,
                "undo_action": None,
            },
        }


def loaded(pieces, refs, *, finding_id="F03", draft="将已指明条款修改为：第二期改为30%。"):
    finding = {
        "id": finding_id,
        "title": "付款与验收",
        "refs": [{"id": piece_id, "quote": quote} for piece_id, quote in refs],
        "suggestion": "只调整已指明的付款节点。",
        "clause_draft": draft,
        "reason": "内部风险理由不应进入批注。",
    }
    return {
        "id": "job-" + "1" * 32,
        "request_digest": "2" * 64,
        "report": {"findings": [finding]},
        "contract": {"pieces": [{"id": key, "text": text} for key, text in pieces.items()]},
        "paragraphs": [{"id": key, "location": f"第{index + 1}段"} for index, key in enumerate(pieces)],
    }


def wait_done(manager, bundle, finding_id="F03", candidate_id=None):
    deadline = time.time() + 3
    while time.time() < deadline:
        value = manager.get(bundle, finding_id, candidate_id)
        if value["status"] != "running":
            return value
        time.sleep(0.01)
    raise AssertionError("draft worker did not finish")


class DraftPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "drafts"

    def test_partial_quote_returns_full_paragraph_and_preserves_edges(self):
        pieces = {"P014": "3.2 首款40%。第二期旧文。发票照常开具。"}
        bundle = loaded(pieces, [("P014", "第二期旧文。")])
        delivery = FakeDelivery(pieces)
        calls = []

        def runner(messages, config):
            calls.append((messages, config))
            return model_json({
                "status": "ready",
                "edits": [{"piece_id": "P014", "new_text": "3.2 首款40%。第二期改为30%。发票照常开具。"}],
                "questions": [],
                "comment": "请确认第二期付款节点，其余约定保持不变。",
                "paragraph_actions": actions("P014"),
            }, messages)

        manager = DraftPreparationManager(self.root, delivery, runner)
        request = {"source_sha256": delivery.sha, "expected_revision": 0,
                   "draft_text": bundle["report"]["findings"][0]["clause_draft"]}
        started, is_new = manager.start(bundle, "F03", request)
        self.assertTrue(is_new)
        self.assertEqual(started["status"], "running")
        result = wait_done(manager, bundle, candidate_id=started["candidate_id"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["edits"][0]["old_text"], pieces["P014"])
        self.assertEqual(result["edits"][0]["start"], 0)
        self.assertEqual(result["edits"][0]["end"], len(pieces["P014"]))
        self.assertTrue(result["edits"][0]["new_text"].startswith("3.2 首款40%。"))
        self.assertTrue(result["edits"][0]["new_text"].endswith("发票照常开具。"))
        self.assertEqual(result["engine_label"], "显式整理引擎")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], {"engine_key": "explicit_runner", "profile_key": "draft_preparation"})
        self.assertTrue((self.root / started["candidate_id"] / "request.json").is_file())
        self.assertTrue((self.root / started["candidate_id"] / "raw-response.txt").is_file())

        cached, is_new = manager.start(bundle, "F03", request)
        self.assertFalse(is_new)
        self.assertEqual(cached, result)
        self.assertEqual(len(calls), 1)
        delivery.revision = 1
        with self.assertRaises(DraftPreparationConflict):
            manager.get(bundle, "F03", started["candidate_id"])
        body_at_new_revision = dict(request, expected_revision=1)
        cached_after_unrelated_change, is_new = manager.start(bundle, "F03", body_at_new_revision)
        self.assertTrue(is_new)
        self.assertNotEqual(cached_after_unrelated_change["candidate_id"], result["candidate_id"])
        current = wait_done(manager, bundle, candidate_id=cached_after_unrelated_change["candidate_id"])
        self.assertEqual(current["based_on_revision"], 1)
        self.assertEqual(len(calls), 2)
        with self.assertRaises(DraftPreparationConflict):
            manager.start(bundle, "F03", request)
        delivery.sha = digest("changed-source")
        with self.assertRaises(DraftPreparationConflict):
            manager.get(bundle, "F03", started["candidate_id"])
        delivery.sha = digest("source")
        bundle["report"]["findings"][0]["suggestion"] = "意见已改变。"
        with self.assertRaises(DraftPreparationConflict):
            manager.get(bundle, "F03", started["candidate_id"])

    def test_multi_paragraph_candidate_is_bounded_to_referenced_ids(self):
        pieces = {"P016": "4.1 验收旧文。", "P017": "4.2 复验旧文。", "P018": "4.3 交付不动。"}
        bundle = loaded(
            pieces,
            [("P016", pieces["P016"]), ("P017", pieces["P017"])],
            finding_id="F02",
            draft="将第4.1、4.2条修改为更明确的验收与复验约定。",
        )

        def runner(messages, config):
            return model_json({
                "status": "ready",
                "edits": [
                    {"piece_id": "P016", "new_text": "4.1 验收新文。"},
                    {"piece_id": "P017", "new_text": "4.2 复验新文。"},
                ],
                "questions": [],
                "comment": "请共同确认验收和复验流程。",
                "paragraph_actions": actions("P016", "P017"),
            }, messages)

        delivery = FakeDelivery(pieces)
        manager = DraftPreparationManager(self.root, delivery, runner)
        first, _ = manager.start(bundle, "F02", {
            "source_sha256": delivery.sha, "expected_revision": 0,
            "draft_text": bundle["report"]["findings"][0]["clause_draft"],
        })
        result = wait_done(manager, bundle, "F02", first["candidate_id"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual({item["piece_id"] for item in result["edits"]}, {"P016", "P017"})

    def test_out_of_scope_or_instruction_output_fails_without_retry(self):
        pieces = {"P016": "4.1 前文。验收旧文。", "P099": "9.9 不属于本意见。"}
        bundle = loaded(pieces, [("P016", "验收旧文。")], finding_id="F04",
                        draft="将第4.1条修改为：验收新文。")
        delivery = FakeDelivery(pieces)
        calls = 0

        def runner(messages, config):
            nonlocal calls
            calls += 1
            return model_json({
                "status": "ready",
                "edits": [{"piece_id": "P099", "new_text": "9.9 偷改。"}],
                "questions": [], "comment": "请确认。",
                "paragraph_actions": actions("P016"),
            }, messages)

        manager = DraftPreparationManager(self.root, delivery, runner)
        body = {"source_sha256": delivery.sha, "expected_revision": 0,
                "draft_text": bundle["report"]["findings"][0]["clause_draft"]}
        first, _ = manager.start(bundle, "F04", body)
        failed = wait_done(manager, bundle, "F04", first["candidate_id"])
        self.assertEqual(failed["status"], "failed")
        again, is_new = manager.start(bundle, "F04", body)
        self.assertFalse(is_new)
        self.assertEqual(again["status"], "failed")
        self.assertEqual(calls, 1)

        # A different explicit input receives a distinct identity, but model
        # operation prose is still rejected after the structural label.
        def instruction(messages, config):
            return model_json({
                "status": "ready",
                "edits": [{"piece_id": "P016", "new_text": "4.1 将第4.1条修改为：验收新文。"}],
                "questions": [], "comment": "请确认。",
                "paragraph_actions": actions("P016"),
            }, messages)
        manager.runner = instruction
        body["clarification"] = "明确依原建议整理。"
        second, _ = manager.start(bundle, "F04", body)
        self.assertEqual(wait_done(manager, bundle, "F04", second["candidate_id"])["status"], "failed")

    def test_needs_input_then_explicit_clarification_creates_new_candidate(self):
        pieces = {"P016": "4.1 应在约定期限内验收。"}
        bundle = loaded(pieces, [("P016", pieces["P016"])], finding_id="F04",
                        draft="将第4.1条的验收期限改为业务确认期限。")
        delivery = FakeDelivery(pieces)
        calls = 0

        def runner(messages, config):
            nonlocal calls
            calls += 1
            if calls == 1:
                return model_json({
                    "status": "needs_input", "edits": [],
                    "questions": ["验收期限确定为多少个工作日？"], "comment": "",
                    "paragraph_actions": actions("P016", action="needs_confirmation"),
                }, messages)
            return model_json({
                "status": "ready",
                "edits": [{"piece_id": "P016", "new_text": "4.1 应在15个工作日内验收。"}],
                "questions": [], "comment": "请确认验收期限。",
                "paragraph_actions": actions("P016"),
            }, messages)

        manager = DraftPreparationManager(self.root, delivery, runner)
        body = {"source_sha256": delivery.sha, "expected_revision": 0,
                "draft_text": bundle["report"]["findings"][0]["clause_draft"]}
        first, _ = manager.start(bundle, "F04", body)
        need = wait_done(manager, bundle, "F04", first["candidate_id"])
        self.assertEqual(need["status"], "needs_input")
        self.assertEqual(calls, 1)
        self.assertEqual(manager.get(bundle, "F04", first["candidate_id"]), need)
        self.assertEqual(calls, 1)

        body["clarification"] = "验收期限确定为15个工作日。"
        second, is_new = manager.start(bundle, "F04", body)
        self.assertTrue(is_new)
        self.assertNotEqual(first["candidate_id"], second["candidate_id"])
        ready = wait_done(manager, bundle, "F04", second["candidate_id"])
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(calls, 2)

    def test_stale_revision_and_existing_edit_conflict_before_runner(self):
        pieces = {"P016": "4.1 验收旧文。"}
        bundle = loaded(pieces, [("P016", pieces["P016"])], finding_id="F04")
        delivery = FakeDelivery(pieces)
        calls = 0

        def runner(messages, config):
            nonlocal calls
            calls += 1
            return "{}"

        manager = DraftPreparationManager(self.root, delivery, runner)
        body = {"source_sha256": delivery.sha, "expected_revision": 1,
                "draft_text": bundle["report"]["findings"][0]["clause_draft"]}
        with self.assertRaises(DraftPreparationConflict):
            manager.start(bundle, "F04", body)
        delivery.edits = [{"id": "edit-F04-1", "finding_ids": ["F04"]}]
        body["expected_revision"] = 0
        with self.assertRaises(DraftPreparationConflict):
            manager.start(bundle, "F04", body)
        self.assertEqual(calls, 0)

    def test_three_references_explain_modified_and_retained_paragraphs(self):
        pieces = {"P017": "1.7 原约定。", "P163": "16.3 旧约定。", "P047": "4.7 另一约定。"}
        bundle = loaded(pieces, list(pieces.items()))
        delivery = FakeDelivery(pieces)
        raw = {
            "status": "ready", "questions": [], "comment": "请确认约定调整。",
            "edits": [{"piece_id": "P163", "new_text": "16.3 新约定。"}],
            "paragraph_actions": [
                {"piece_id": "P017", "action": "retain", "reason": "第1段是主体定义，本次调整不改变定义。"},
                {"piece_id": "P163", "action": "modify", "reason": "第2段补齐当前意见指明的责任约定。"},
                {"piece_id": "P047", "action": "retain", "reason": "第3段另行约定已与本次新文一致，不需重复修改。"},
            ],
        }
        captured = []
        def runner(messages, config):
            captured.append(messages)
            return model_json(raw, messages)
        manager = DraftPreparationManager(self.root, delivery, runner)
        draft = bundle["report"]["findings"][0]["clause_draft"]
        first, _ = manager.start(bundle, "F03", {
            "source_sha256": delivery.sha, "expected_revision": 0, "draft_text": draft,
        })
        result = wait_done(manager, bundle, candidate_id=first["candidate_id"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["draft_sha256"], digest(draft))
        self.assertEqual([a["action"] for a in result["paragraph_actions"]], ["retain", "modify", "retain"])
        self.assertEqual([a["location"] for a in result["paragraph_actions"]], ["第1段", "第2段", "第3段"])
        request_path = self.root / first["candidate_id"] / "request.json"
        frozen = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(frozen["schema_version"], DRAFT_SCHEMA_VERSION)
        self.assertIn("引用了多段不代表必须修改所有段", captured[0][0]["content"])
        schema = output_schema("draft_preparation")
        self.assertIn("paragraph_actions", schema["required"])
        self.assertEqual(schema["properties"]["paragraph_actions"]["items"]["required"],
                         ["piece_id", "action", "reason"])

        # Simulate the old frozen cache format; GET annotates but never rewrites
        # or calls the model, and omission is not retroactively called retention.
        operation = json.loads(request_path.with_name("operation.json").read_text(encoding="utf-8"))
        del operation["schema_version"]
        del operation["paragraph_actions"]
        del operation["insertions"]
        del operation["suggestion_actions"]
        frozen["schema_version"] = frozen["identity"]["schema_version"] = "2026-09-22.1"
        legacy_id = digest(json.dumps(frozen["identity"], ensure_ascii=False,
                                      sort_keys=True, separators=(",", ":")))
        frozen["candidate_id"] = operation["candidate_id"] = legacy_id
        legacy_directory = self.root / legacy_id
        legacy_directory.mkdir()
        operation_path = legacy_directory / "operation.json"
        operation_path.write_text(json.dumps(operation, ensure_ascii=False), encoding="utf-8")
        (legacy_directory / "request.json").write_text(json.dumps(frozen, ensure_ascii=False), encoding="utf-8")
        (legacy_directory / "raw-response.txt").write_text(json.dumps({
            key: value for key, value in raw.items() if key != "paragraph_actions"
        }, ensure_ascii=False), encoding="utf-8")
        pointer = self.root / f"latest-{bundle['id']}-F03.json"
        pointer.write_text(json.dumps({"candidate_id": legacy_id}), encoding="utf-8")
        before = {path: path.read_bytes() for path in legacy_directory.iterdir() if path.is_file()}
        before[pointer] = pointer.read_bytes()
        manager.runner = lambda *_: self.fail("legacy cache must not invoke a model")
        legacy = manager.get(bundle, "F03")
        self.assertEqual([a["action"] for a in legacy["paragraph_actions"]],
                         ["unexplained", "modify", "unexplained"])
        self.assertEqual(legacy["edits"], result["edits"])
        self.assertFalse(legacy["coverage_complete"])
        self.assertEqual(legacy["suggestion_actions"][0]["action"], "unexplained")
        self.assertEqual(manager.get(bundle, "F03", legacy_id), legacy)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_invalid_paragraph_action_coverage_and_edit_consistency_rejected(self):
        pieces = {"P016": "4.1 验收旧文。", "P017": "4.2 复验原文。"}
        bundle = loaded(pieces, list(pieces.items()))
        finding = bundle["report"]["findings"][0]
        targets = _bound_targets(bundle, FakeDelivery(pieces).describe(bundle)["source"], finding)
        good = {
            "status": "ready", "questions": [], "comment": "请确认验收流程。",
            "insertions": [], "suggestion_actions": [{"unit_id": "S001", "action": "implemented",
                "edit_piece_ids": ["P016"], "insertion_indices": [], "reason": "已修改验收约定。"}],
            "edits": [{"piece_id": "P016", "new_text": "4.1 验收新文。"}],
            "paragraph_actions": actions("P016") + [
                {"piece_id": "P017", "action": "retain", "reason": "复验独立约定不受本次验收调整影响。"}],
        }
        def validate(value):
            return _validate_result(json.dumps(value, ensure_ascii=False), targets, finding, finding["clause_draft"], "")
        self.assertEqual(validate(good)["status"], "ready")
        cases = []
        for label, change in (
            ("missing", lambda v: v["paragraph_actions"].pop()),
            ("duplicate", lambda v: v["paragraph_actions"][1].update(piece_id="P016")),
            ("unknown", lambda v: v["paragraph_actions"][1].update(piece_id="P999")),
            ("empty reason", lambda v: v["paragraph_actions"][1].update(reason=" ")),
            ("unsupported action", lambda v: v["paragraph_actions"][1].update(action="resolved")),
            ("edit marked retain", lambda v: v["paragraph_actions"][0].update(action="retain")),
            ("modify without edit", lambda v: v["paragraph_actions"][1].update(action="modify")),
            ("ready yet unresolved", lambda v: v["paragraph_actions"][1].update(action="needs_confirmation")),
        ):
            value = copy.deepcopy(good)
            change(value)
            cases.append((label, value))
        value = copy.deepcopy(good)
        del value["paragraph_actions"]
        cases.append(("old model response rejected for new operation", value))
        for label, value in cases:
            with self.subTest(label=label), self.assertRaises(DraftPreparationError):
                validate(value)
        pending = {
            "status": "needs_input", "edits": [], "questions": ["验收期限应为多少个工作日？"],
            "insertions": [], "suggestion_actions": [{"unit_id": "S001", "action": "needs_confirmation",
                "edit_piece_ids": [], "insertion_indices": [], "reason": "验收期限尚待确认。"}],
            "comment": "", "paragraph_actions": actions("P016", action="needs_confirmation") + good["paragraph_actions"][1:],
        }
        self.assertEqual(validate(pending)["paragraph_actions"][0]["action"], "needs_confirmation")
        pending["suggestion_actions"][0]["action"] = "implemented"
        with self.assertRaises(DraftPreparationError):
            validate(pending)

    def test_restart_marks_running_operation_failed_without_new_call(self):
        pieces = {"P016": "4.1 验收旧文。"}
        bundle = loaded(pieces, [("P016", pieces["P016"])], finding_id="F04")
        delivery = FakeDelivery(pieces)
        gate = threading.Event()
        calls = 0

        def runner(messages, config):
            nonlocal calls
            calls += 1
            gate.wait(2)
            return model_json({
                "status": "ready", "edits": [{"piece_id": "P016", "new_text": "4.1 验收新文。"}],
                "questions": [], "comment": "请确认验收约定。",
                "paragraph_actions": actions("P016"),
            }, messages)

        first_manager = DraftPreparationManager(self.root, delivery, runner)
        first, _ = first_manager.start(bundle, "F04", {
            "source_sha256": delivery.sha, "expected_revision": 0,
            "draft_text": bundle["report"]["findings"][0]["clause_draft"],
        })
        restarted = DraftPreparationManager(self.root, delivery, runner)
        recovered = restarted.get(bundle, "F04", first["candidate_id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertIn("中断", recovered["error"])
        self.assertEqual(calls, 1)
        gate.set()
        deadline = time.time() + 3
        while first_manager._active and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(first_manager._active)

    def test_new_numbered_clause_cannot_disappear_and_uses_source_bound_insertion(self):
        pieces = {"P008": "1.3 原主体约定。", "P002": "甲方公司。", "P003": "乙方公司。", "P020": "8.1 原送达约定。"}
        draft = "1.3 明确合同主体约定。\n8.2 指定邮箱发送成功视为送达。"
        bundle = loaded(pieces, [(key, pieces[key]) for key in ("P008", "P002", "P003")], draft=draft)
        delivery = FakeDelivery(pieces)
        finding = bundle["report"]["findings"][0]
        source = delivery.describe(bundle)["source"]
        targets = _bound_targets(bundle, source, finding)
        anchors = _insertion_anchors(bundle, source)
        good = {
            "status": "ready", "questions": [], "comment": "请确认主体及送达约定。",
            "edits": [{"piece_id": "P008", "new_text": "1.3 明确合同主体约定。"}],
            "insertions": [{"after_piece_id": "P020", "new_text": "8.2 指定邮箱发送成功视为送达。"}],
            "paragraph_actions": actions("P008") + [
                {"piece_id": key, "action": "retain", "reason": "签约方名称在主体条款已说明，本处原文保留。"}
                for key in ("P002", "P003")],
            "suggestion_actions": [
                {"unit_id": "S001", "action": "implemented", "edit_piece_ids": ["P008"],
                 "insertion_indices": [], "reason": "1.3主体条款已按建议修改。"},
                {"unit_id": "S002", "action": "implemented", "edit_piece_ids": [],
                 "insertion_indices": [0], "reason": "在8.1后新增独立8.2条送达约定。"},
            ],
        }
        def validate(value, selected_anchors=anchors):
            return _validate_result(json.dumps(value, ensure_ascii=False), targets, finding, draft, "", selected_anchors)
        validated = validate(good)
        self.assertEqual(validated["insertions"][0]["kind"], "insert_paragraph_after")
        self.assertEqual(validated["insertions"][0]["start"], len(pieces["P020"]))
        self.assertEqual(validated["insertions"][0]["paragraph_sha256"], digest(pieces["P020"]))
        missing = copy.deepcopy(good)
        missing["insertions"] = []
        missing["suggestion_actions"].pop()
        with self.assertRaises(DraftPreparationError):
            validate(missing)
        dishonest = copy.deepcopy(missing)
        dishonest["suggestion_actions"].append(dict(good["suggestion_actions"][1], edit_piece_ids=["P008"], insertion_indices=[]))
        with self.assertRaisesRegex(DraftPreparationError, "8.2"):
            validate(dishonest)
        with self.assertRaises(DraftPreparationError):
            validate(good, [])
        duplicate = copy.deepcopy(good)
        duplicate["insertions"].append(dict(duplicate["insertions"][0]))
        with self.assertRaises(DraftPreparationError):
            validate(duplicate)
        unknown = copy.deepcopy(good)
        unknown["insertions"][0]["after_piece_id"] = "P999"
        with self.assertRaises(DraftPreparationError):
            validate(unknown)

        manager = DraftPreparationManager(self.root, delivery, lambda *_: json.dumps(good, ensure_ascii=False))
        first, _ = manager.start(bundle, "F03", {"source_sha256": delivery.sha, "expected_revision": 0, "draft_text": draft})
        ready = wait_done(manager, bundle, candidate_id=first["candidate_id"])
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["coverage_complete"])
        self.assertEqual(ready["suggestion_actions"][1]["text"], draft.splitlines()[1])
        frozen = json.loads((self.root / first["candidate_id"] / "request.json").read_text(encoding="utf-8"))
        self.assertIn("P020", [a["piece_id"] for a in json.loads(frozen["messages"][1]["content"].split("UNTRUSTED DATA:\n")[1])["allowed_insertion_anchors"]])
        before = {path: path.read_bytes() for path in (self.root / first["candidate_id"]).iterdir()}
        # Resolver must neither recurse into delivery.describe nor change cache.
        delivery.describe = lambda *_: self.fail("read-only resolver must not describe delivery")
        self.assertEqual(manager.resolve_confirmation(bundle, "F03", first["candidate_id"], delivery.sha), ready)
        self.assertEqual(manager.resolve_confirmation(bundle, "F03", None, delivery.sha), ready)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        with self.assertRaises(DraftPreparationConflict):
            manager.resolve_confirmation(bundle, "F03", first["candidate_id"], "0" * 64)

    def test_previous_schema_scope_cache_remains_readable_without_coverage_inference(self):
        pieces = {"P008": "1.3 原主体约定。", "P020": "8.1 原通知。"}
        draft = "1.3 更新主体。\n8.2 新增通知机制。"
        bundle = loaded(pieces, [("P008", pieces["P008"])], draft=draft)
        delivery = FakeDelivery(pieces)
        manager = DraftPreparationManager(self.root, delivery, None)
        finding = bundle["report"]["findings"][0]
        identity = {"schema_version": "2026-10-04.1", "draft_text": draft}
        candidate_id = digest(json.dumps(identity, sort_keys=True))
        directory = self.root / candidate_id
        directory.mkdir(parents=True)
        targets = _bound_targets(bundle, delivery.describe(bundle)["source"], finding)
        operation = {
            "schema_version": "2026-10-04.1", "candidate_id": candidate_id, "status": "ready",
            "report_id": bundle["id"], "request_digest": bundle["request_digest"], "finding_id": "F03",
            "finding_sha256": digest(json.dumps(finding, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            "source_sha256": delivery.sha, "based_on_revision": 0, "draft_sha256": digest(draft),
            "clarification_sha256": digest(""), "edits": [], "questions": [], "comment": "请确认主体。", "error": None,
            "paragraph_actions": [{"piece_id": "P008", "action": "retain", "location": "第1段", "reason": "旧版保存的具体保留理由。"}],
        }
        (directory / "operation.json").write_text(json.dumps(operation, ensure_ascii=False), encoding="utf-8")
        (directory / "request.json").write_text(json.dumps({"identity": identity, "messages": [
            {"role": "system", "content": "old instructions"},
            {"role": "user", "content": "UNTRUSTED DATA:\n" + json.dumps({"allowed_targets": targets})}
        ]}), encoding="utf-8")
        (self.root / f"latest-{bundle['id']}-F03.json").write_text(json.dumps({"candidate_id": candidate_id}), encoding="utf-8")
        before = {path: path.read_bytes() for path in directory.iterdir()}
        old = manager.get(bundle, "F03")
        self.assertEqual(old["paragraph_actions"], operation["paragraph_actions"])
        self.assertEqual([a["action"] for a in old["suggestion_actions"]], ["unexplained", "unexplained"])
        self.assertFalse(old["coverage_complete"])
        self.assertEqual(manager.resolve_confirmation(bundle, "F03", None, delivery.sha), old)
        self.assertEqual(before, {path: path.read_bytes() for path in before})


class DraftPreparationHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        write_sources(self.project / "sources.json", labor_sources())
        source = self.project / "source.docx"
        document = Document()
        document.add_paragraph("3.2 首款40%。第二期旧文。发票照常开具。")
        document.save(source)

        for spec in REPORTS.values():
            directory = self.project / "runtime" / spec["directory"]
            directory.mkdir(parents=True)
            packet = prepare(source, directory / "request.json", detailed=True, sources=labor_sources())
            contract = ParsedContract.model_validate(packet["contract"])
            result = {
                "findings": [{
                    "id": "F03", "rule_id": "LC-02", "title": "付款节点",
                    "status": "发现风险", "risk": "中", "evidence_status": "依据已核",
                    "refs": [{"id": contract.pieces[0].id, "quote": "第二期旧文。"}],
                    "source_ids": ["LCL30_50"], "reason": "付款节点需核对。",
                    "suggestion": "第二期调整为30%。",
                    "clause_draft": "将第3.2条修改为：第二期调整为30%。", "questions": [],
                }],
                "checks": [
                    {"rule_id": "LC-01", "status": "未检出", "reason": "已审。"},
                    {"rule_id": "LC-02", "status": "发现风险", "reason": "发现付款问题。"},
                    {"rule_id": "LC-03", "status": "未检出", "reason": "已审。"},
                ],
                "options": [], "uncovered": [],
            }
            response = {"request_digest": packet["request_digest"], "model": "test-sol", "result": result}
            (directory / "response.json").write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")

        self.calls = 0

        def runner(messages, config):
            self.calls += 1
            return model_json({
                "status": "ready",
                "edits": [{
                    "piece_id": "P000",
                    "new_text": "3.2 首款40%。第二期调整为30%。发票照常开具。",
                }],
                "questions": [], "comment": "请确认第二期付款节点。",
                "paragraph_actions": actions("P000"),
            }, messages)

        source_paths = {key: source for key in REPORTS}
        self.server = create_server(
            root=self.project,
            state_dir=Path(self.temp.name) / "state",
            source_paths=source_paths,
            review_runner=runner,
            draft_runner=runner,
            review_engines=[{"key": "codex_cli", "label": "test", "available": True}],
            jobs_dir=Path(self.temp.name) / "jobs",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.port = self.server.server_port

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None, *, origin=True, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        value = json.loads(response.read().decode("utf-8"))
        status = response.status
        connection.close()
        return status, value

    def test_http_binding_origin_validation_poll_and_cache(self):
        status, delivery = self.request("GET", "/api/reports/detailed/delivery", origin=False)
        self.assertEqual(status, 200)
        body = {
            "source_sha256": delivery["source"]["source_sha256"],
            "expected_revision": delivery["state"]["revision"],
            "draft_text": "将第3.2条修改为：第二期调整为30%。",
        }
        path = "/api/reports/detailed/drafts/F03"
        self.assertEqual(self.request("POST", path, body, origin=False)[0], 403)
        self.assertEqual(self.request("POST", path, body, host="evil.example:8519")[0], 421)
        self.assertEqual(self.request("POST", path, {"source_sha256": "bad"})[0], 400)
        self.assertEqual(self.calls, 0)

        status, started = self.request("POST", path, body)
        self.assertEqual(status, 202)
        candidate_id = started["candidate_id"]
        deadline = time.time() + 3
        while time.time() < deadline:
            status, result = self.request("GET", path + "?candidate_id=" + candidate_id, origin=False)
            if result["status"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual((status, result["status"]), (200, "ready"))
        self.assertEqual(result["source_sha256"], body["source_sha256"])
        self.assertEqual(result["based_on_revision"], 0)
        self.assertEqual(self.calls, 1)

        status, cached = self.request("POST", path, body)
        self.assertEqual((status, cached["status"]), (200, "ready"))
        self.assertEqual(cached["candidate_id"], candidate_id)
        self.assertEqual(self.calls, 1)


if __name__ == "__main__":
    unittest.main()
