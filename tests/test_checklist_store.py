import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from checklist_store import (ChecklistStore, ChecklistError, ChecklistConflict, ChecklistCorrupt,
                             normalize_checklist_snapshot, legacy_content, convert_checklist_for_edit)


def personal():
    return {"id": "", "name": "测试清单", "contract_type": "服务", "stance": "客户", "version": 1,
            "builtin": False, "items": [{"id": "C01", "title": "事项", "requirement": "核对给付范围",
            "when": "", "facts": "使用目的", "guidance": "按具体约定检查", "notes": "", "enabled": True}]}


class ChecklistStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state" / "checklists.json"
        self.store = ChecklistStore(self.path)

    def save(self, item=None, revision=0):
        return self.store.save({"checklist": item or personal(), "expected_revision": revision})

    def test_list_and_get_do_not_create_state(self):
        result = self.store.list()
        self.assertEqual(result["revision"], 0)
        builtin = result["checklists"][0]
        self.assertEqual(len(builtin["items"]), 24)
        self.assertEqual(self.store.get(builtin["id"]), builtin)
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(builtin["id"], "builtin-general-v01")
        self.assertEqual(builtin["version"], "0.2")
        self.assertEqual([row["id"] for row in builtin["items"]], [f"G{i:02}" for i in range(1, 25)])
        self.assertIn("中国大陆", builtin["scope"])
        self.assertTrue(all(not row["facts"] and not row["guidance"] for row in builtin["items"]))
        self.assertTrue(all("\n2." in row["requirement"] for row in builtin["items"]))
        self.assertNotIn("通过：", json.dumps(builtin, ensure_ascii=False))
        self.assertNotIn("PDF", json.dumps(builtin, ensure_ascii=False))
        software = result["checklists"][1]
        self.assertEqual(software["id"], "builtin-software-client-v02")
        self.assertEqual(software["version"], "0.6")
        self.assertEqual([row["id"] for row in software["items"]], [f"C{i:02}" for i in range(1,32)])

    def test_c14_keeps_settlement_scope_and_judgment_boundaries(self):
        item = self.store.get("builtin-software-client-v02")["items"][13]
        for phrase in ("已完成服务怎样计价", "退款或补款", "必要代码、权限", "不强制阶段定价",
                       "不机械增加任意退出权", "不默认提前结束就必退或不退", "其效力另按"):
            self.assertIn(phrase, item["requirement"])

    def test_scope_is_optional_for_old_snapshots_and_roundtrips_for_new(self):
        old = normalize_checklist_snapshot(personal())
        self.assertNotIn("scope", old)
        self.assertEqual(normalize_checklist_snapshot(old), old)
        first = self.save(old)["checklist"]
        edited = {**first, "scope": "仅适用境内定制软件。"}
        second = self.save(edited, 1)["checklist"]
        self.assertEqual(second["scope"], edited["scope"])
        store = ChecklistStore(self.path)
        self.assertEqual(store.get(second["id"])["scope"], edited["scope"])
        history = json.loads(self.path.read_text(encoding="utf-8"))["history"][-1]
        self.assertNotIn("scope", history["checklists"][0])

    def test_scope_invalid_type_and_long_scope_rejected(self):
        for scope in (None, [], 4, "x" * 8001):
            with self.assertRaises(ChecklistError):
                normalize_checklist_snapshot({**personal(), "scope": scope})

    def test_legacy_edit_conversion_is_explicit_lossless_and_idempotent(self):
        raw = personal()
        raw["items"][0].update(requirement="  ### 基线\n- 原要求  ", facts="  原事实\n第二行  ", guidance="原要求")
        before = copy.deepcopy(raw)
        normalized = normalize_checklist_snapshot(raw)
        self.assertEqual(normalized["items"][0]["requirement"], "### 基线\n- 原要求")
        self.assertEqual(normalized["items"][0]["facts"], "原事实\n第二行")
        self.assertEqual(normalized["items"][0]["guidance"], "原要求")
        editable = convert_checklist_for_edit(raw)
        self.assertEqual(editable["items"][0]["requirement"],
                         "### 基线\n- 原要求\n\n### 原可能需要的信息\n\n原事实\n第二行\n\n### 原处理建议\n\n原要求")
        self.assertEqual(editable["items"][0]["facts"], "")
        self.assertEqual(editable["items"][0]["guidance"], "")
        self.assertEqual(convert_checklist_for_edit(editable), editable)
        self.assertEqual(raw, before)
        self.assertIsNone(convert_checklist_for_edit(None))

    def test_read_and_save_history_do_not_rewrite_legacy_snapshots(self):
        old = self.save()["checklist"]
        before = self.path.read_bytes()
        self.assertEqual(self.store.get(old["id"]), old)
        self.assertEqual(self.path.read_bytes(), before)
        editable = convert_checklist_for_edit(old)
        current = self.save(editable, 1)["checklist"]
        self.assertEqual(current["items"][0]["facts"], "")
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(state["history"][-1]["checklists"][0], old)
        self.assertEqual(normalize_checklist_snapshot(old), old)

    def test_legacy_merge_limit_rejects_without_truncating_or_mutating(self):
        raw = personal()
        raw["items"][0].update(requirement="a" * 7990, facts="b" * 100)
        before = copy.deepcopy(raw)
        with self.assertRaises(ChecklistError):
            convert_checklist_for_edit(raw)
        self.assertEqual(raw, before)
        self.assertFalse(self.path.exists())
        self.assertEqual(legacy_content({"requirement": "内容", "facts": " ", "guidance": ""}), "内容")

    def test_save_reload_update_item_ids_and_version(self):
        result = self.save()
        item = result["checklist"]
        self.assertTrue(item["id"].startswith("cl-"))
        self.assertEqual(item["version"], 1)
        item["items"][0]["requirement"] = "新要求"
        updated = self.save(item, 1)
        self.assertEqual(updated["checklist"]["version"], 2)
        self.assertEqual(updated["checklist"]["items"][0]["id"], "C01")
        self.assertEqual(ChecklistStore(self.path).get(item["id"]), updated["checklist"])

    def test_conflict_never_changes_disk(self):
        self.save()
        before = self.path.read_bytes()
        with self.assertRaises(ChecklistConflict) as caught:
            self.save(revision=0)
        self.assertEqual(caught.exception.current_revision, 1)
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaises(ChecklistConflict):
            ChecklistStore(self.path).delete(self.store.list()["checklists"][-1]["id"], 0)

    def test_corrupt_never_reset_or_overwritten(self):
        self.path.parent.mkdir()
        self.path.write_text('{broken', encoding="utf-8")
        before = self.path.read_bytes()
        for action in (self.store.list, self.save):
            with self.assertRaises(ChecklistCorrupt):
                action()
        self.assertEqual(before, self.path.read_bytes())

    def test_builtin_protection_and_copy(self):
        builtin = self.store.list()["checklists"][0]
        with self.assertRaises(ChecklistError):
            self.save(builtin)
        with self.assertRaises(ChecklistError):
            self.store.delete(builtin["id"], 0)
        builtin.update(id="", builtin=False)
        self.assertFalse(self.save(builtin)["checklist"]["builtin"])

    def test_snapshot_independent_of_catalog_and_caller(self):
        saved = self.save()["checklist"]
        temporary = copy.deepcopy(saved)
        temporary["items"][0]["requirement"] = "仅本次"
        frozen = normalize_checklist_snapshot(temporary)
        temporary["items"][0]["requirement"] = "改了"
        saved["items"][0]["requirement"] = "常用更新"
        self.save(saved, 1)
        self.assertEqual(frozen["items"][0]["requirement"], "仅本次")
        self.assertEqual(frozen["version"], 1)
        for raw in (None, {}, ""):
            self.assertIsNone(normalize_checklist_snapshot(raw))

    def test_validation_rejects_hidden_duplicate_and_wrong_types(self):
        bad = personal()
        bad["items"][0]["sources"] = "书页"
        with self.assertRaises(ChecklistError):
            normalize_checklist_snapshot(bad)
        for mutate in (lambda x: x["items"].append(copy.deepcopy(x["items"][0])),
                       lambda x: x["items"][0].update(enabled="false"),
                       lambda x: x["items"][0].update(title=" "),
                       lambda x: x.update(items=[])):
            bad = personal()
            mutate(bad)
            with self.assertRaises(ChecklistError):
                self.save(bad)
        self.assertFalse(self.path.exists())

    def test_delete_history_recoverable_and_bounded(self):
        result = self.save()
        id = result["checklist"]["id"]
        deleted = self.store.delete(id, 1)
        self.assertEqual(deleted, {"revision": 2, "deleted_id": id})
        state = json.loads(self.path.read_text(encoding="utf-8"))
        restored = state["history"][-1]["checklists"][0]
        restored.update(id="")
        self.save(restored, 2)
        for revision in range(3, 26):
            item = self.store.list()["checklists"][-1]
            self.save(item, revision)
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["history"]), 20)

    def test_failed_atomic_replace_preserves_old_file(self):
        self.save()
        before = self.path.read_bytes()
        with patch("checklist_store.os.replace", side_effect=OSError("blocked")):
            with self.assertRaises(OSError):
                self.save(revision=1)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(list(self.path.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
