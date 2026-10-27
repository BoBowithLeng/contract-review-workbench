from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from reader_server import create_server
from workbench_groups import WorkbenchGroupConflict, WorkbenchGroupCorrupt, WorkbenchGroupManager


DOC_A = "doc-" + "a" * 24
DOC_B = "doc-" + "b" * 24
DOC_C = "doc-" + "c" * 24
DOC_D = "doc-" + "d" * 24
DOC_E = "doc-" + "e" * 24
DOC_F = "doc-" + "f" * 24
JOB_A = "job-" + "1" * 32
JOB_B = "job-" + "2" * 32
JOB_PENDING = "job-" + "3" * 32


class Catalog:
    def __init__(self):
        self.value = {
            "documents": [
                {"id": DOC_A, "reviews": [{"id": JOB_A}, {"id": JOB_B}]},
                {"id": DOC_B, "reviews": [{"id": "minimal"}]},
            ],
            "pending_jobs": [{"id": JOB_PENDING}],
        }

    def __call__(self):
        return self.value


class WorkbenchGroupManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "state" / "workbench-groups.json"
        self.catalog = Catalog()
        self.manager = WorkbenchGroupManager(self.path, self.catalog)
        self.contract = self.root / "original-contract.docx"
        self.contract.write_bytes(b"original-contract-bytes")

    def create(self, revision, name):
        return self.manager.update({"expected_revision": revision, "action": "create", "name": name})

    def test_reserved_navigation_names_are_not_custom_groups(self):
        for name in ("全部合同", "未分组"):
            with self.assertRaises(ValueError):
                self.create(0, name)
        self.assertFalse(self.path.exists())

    def test_oversized_write_preserves_existing_file(self):
        value = self.create(0, "劳动用工")
        before = self.path.read_bytes()
        # Leave enough room for reading the current file, but not the next snapshot.
        with patch("workbench_groups.MAX_STATE_BYTES", len(before) + 5):
            with self.assertRaises(ValueError):
                self.create(value["revision"], "服务合作")
        self.assertEqual(self.path.read_bytes(), before)

    def test_empty_get_crud_assign_delete_and_history_leave_contract_untouched(self):
        before = self.contract.read_bytes()
        empty = self.manager.get()
        self.assertEqual(empty, {"revision": 0, "groups": [], "assignments": {}})
        self.assertFalse(self.path.exists())

        created = self.create(0, "  劳动合同  ")
        group_id = created["created_group_id"]
        self.assertRegex(group_id, r"^g-[0-9a-f]{32}$")
        self.assertEqual(created["groups"], [{"id": group_id, "name": "劳动合同"}])

        renamed = self.manager.update({
            "expected_revision": 1, "action": "rename", "group_id": group_id, "name": "人事合同",
        })
        self.assertEqual(renamed["revision"], 2)
        self.assertEqual(renamed["groups"][0]["name"], "人事合同")

        assigned = self.manager.update({
            "expected_revision": 2, "action": "assign", "group_id": group_id,
            "item_ids": [DOC_A, JOB_PENDING],
        })
        self.assertEqual(assigned["assignments"], {DOC_A: group_id, JOB_PENDING: group_id})

        # A pending job may later become a review of a document.  Moving that
        # document to ungrouped clears inherited review-job assignments too.
        self.catalog.value = {
            "documents": [
                {"id": DOC_A, "reviews": [{"id": JOB_A}, {"id": JOB_B}, {"id": JOB_PENDING}]},
                {"id": DOC_B, "reviews": []},
            ],
            "pending_jobs": [],
        }
        ungrouped = self.manager.update({
            "expected_revision": 3, "action": "assign", "group_id": None, "item_ids": [DOC_A],
        })
        self.assertEqual(ungrouped["assignments"], {})

        reassigned = self.manager.update({
            "expected_revision": 4, "action": "assign", "group_id": group_id, "item_ids": [DOC_B],
        })
        deleted = self.manager.update({
            "expected_revision": 5, "action": "delete", "group_id": group_id,
        })
        self.assertEqual(deleted, {"revision": 6, "groups": [], "assignments": {}})
        self.assertEqual(self.contract.read_bytes(), before)

        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["schema_version"], 1)
        self.assertEqual(len(stored["history"]), 6)
        self.assertEqual([item["revision"] for item in stored["history"]], list(range(6)))

    def test_document_assignment_clears_conflicting_review_assignments(self):
        first = self.create(0, "A")
        group_a = first["created_group_id"]
        second = self.create(1, "B")
        group_b = second["created_group_id"]
        # Seed a valid historical job assignment: it was pending at that time.
        self.catalog.value = {"documents": [], "pending_jobs": [{"id": JOB_A}]}
        state = self.manager.update({
            "expected_revision": 2, "action": "assign", "group_id": group_a, "item_ids": [JOB_A],
        })
        self.assertEqual(state["assignments"][JOB_A], group_a)
        self.catalog.value = {"documents": [{"id": DOC_A, "reviews": [{"id": JOB_A}]}], "pending_jobs": []}
        state = self.manager.update({
            "expected_revision": 3, "action": "assign", "group_id": group_b, "item_ids": [DOC_A],
        })
        self.assertEqual(state["assignments"], {DOC_A: group_b})

    def test_validation_revision_unknown_ids_and_normalized_duplicate_names(self):
        first = self.create(0, "Alpha")
        with self.assertRaisesRegex(ValueError, "同名"):
            self.create(1, "ＡＬＰＨＡ")
        with self.assertRaises(WorkbenchGroupConflict) as caught:
            self.manager.update({
                "expected_revision": 0, "action": "rename",
                "group_id": first["created_group_id"], "name": "Other",
            })
        self.assertEqual(caught.exception.current_revision, 1)
        with self.assertRaisesRegex(ValueError, "当前工作台"):
            self.manager.update({
                "expected_revision": 1, "action": "assign",
                "group_id": first["created_group_id"], "item_ids": ["doc-" + "f" * 24],
            })
        with self.assertRaises(ValueError):
            self.manager.update({
                "expected_revision": 1, "action": "assign",
                "group_id": first["created_group_id"], "item_ids": [DOC_A], "path": "C:/secret",
            })
        self.assertEqual(self.manager.get()["revision"], 1)

    def test_history_is_bounded_to_twenty_snapshots(self):
        state = self.create(0, "G0")
        group_id = state["created_group_id"]
        for index in range(1, 24):
            state = self.manager.update({
                "expected_revision": state["revision"], "action": "rename",
                "group_id": group_id, "name": f"G{index}",
            })
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(stored["history"]), 20)
        self.assertEqual(stored["history"][0]["revision"], 4)
        self.assertEqual(stored["history"][-1]["revision"], 23)

    def test_corrupt_state_is_preserved_and_not_silently_reset(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(WorkbenchGroupCorrupt):
            self.manager.get()
        with self.assertRaises(WorkbenchGroupCorrupt):
            self.create(0, "Should not replace")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")

    def test_legacy_source_group_fans_out_once_without_future_inheritance(self):
        a = self.create(0, "原组")
        group_a = a["created_group_id"]
        b = self.create(1, "单任务组")
        group_b = b["created_group_id"]
        self.catalog.value["documents"].append({"id": DOC_F, "reviews": []})
        old = self.manager.update({"expected_revision": 2, "action": "assign", "group_id": group_a,
                                   "item_ids": [DOC_A, DOC_F]})
        self.catalog.value = {"documents": [
            {"id": DOC_D, "reviews": [{"id": "minimal"}]},
        ], "pending_jobs": [{"id": JOB_B}]}
        own = self.manager.update({"expected_revision": old["revision"], "action": "assign",
                                   "group_id": group_b, "item_ids": [DOC_D, JOB_B]})
        self.catalog.value = {"documents": [
            {"id": DOC_B, "legacy_document_id": DOC_A, "reviews": [{"id": JOB_A}]},
            {"id": DOC_C, "legacy_document_id": DOC_A, "reviews": [{"id": JOB_B}]},
            {"id": DOC_D, "legacy_document_id": DOC_A, "reviews": [{"id": "minimal"}]},
        ], "pending_jobs": []}
        migrated = self.manager.migrate_legacy_assignments(own["revision"])
        self.assertTrue(migrated["migration"]["changed"])
        self.assertEqual(migrated["migration"]["legacy_keys_removed"], 1)
        self.assertEqual(migrated["migration"]["pending_group_preserved"], 1)
        self.assertEqual(migrated["migration"]["existing_preserved"], 1)
        self.assertEqual(migrated["migration"]["unmapped_document_keys_preserved"], 1)
        self.assertEqual(migrated["assignments"][DOC_B], group_a)
        self.assertEqual(migrated["assignments"][DOC_C], group_b)
        self.assertEqual(migrated["assignments"][DOC_D], group_b)
        self.assertNotIn(DOC_A, migrated["assignments"])
        self.assertEqual(migrated["assignments"][DOC_F], group_a)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["history"][-1]["assignments"][DOC_A], group_a)
        before = self.path.read_bytes()
        repeat = self.manager.migrate_legacy_assignments(migrated["revision"])
        self.assertFalse(repeat["migration"]["changed"])
        self.assertEqual(self.path.read_bytes(), before)
        self.catalog.value["documents"].append(
            {"id": DOC_E, "legacy_document_id": DOC_A, "reviews": [{"id": JOB_PENDING}]})
        self.assertNotIn(DOC_E, self.manager.get()["assignments"])
        ungrouped = self.manager.update({"expected_revision": migrated["revision"], "action": "assign",
                                         "group_id": None, "item_ids": [DOC_B]})
        self.assertNotIn(DOC_B, ungrouped["assignments"])
        self.assertEqual(ungrouped["assignments"][DOC_C], group_b)
        self.assertEqual(ungrouped["assignments"][DOC_D], group_b)
        self.assertNotIn(DOC_A, ungrouped["assignments"])

    def test_pending_completion_inherits_only_its_own_assignment_and_can_ungroup(self):
        first = self.create(0, "人事")
        group_id = first["created_group_id"]
        pending = self.manager.update({"expected_revision": 1, "action": "assign", "group_id": group_id,
                                       "item_ids": [JOB_PENDING]})
        self.catalog.value = {"documents": [
            {"id": DOC_B, "legacy_document_id": DOC_A, "reviews": [{"id": JOB_PENDING}]},
            {"id": DOC_C, "legacy_document_id": DOC_A, "reviews": [{"id": JOB_A}]},
        ], "pending_jobs": []}
        self.assertEqual(self.manager.get()["assignments"], {JOB_PENDING: group_id})
        moved = self.manager.update({"expected_revision": pending["revision"], "action": "assign",
                                     "group_id": None, "item_ids": [DOC_B]})
        self.assertEqual(moved["assignments"], {})
        self.assertEqual(self.manager.get()["assignments"], {})


class WorkbenchGroupHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.server = create_server(root=root, state_dir=root / "state", jobs_dir=root / "jobs", review_runner=None)
        self.server.workbench_groups.catalog_provider = lambda: {
            "documents": [{"id": DOC_A, "reviews": []}], "pending_jobs": [{"id": JOB_PENDING}],
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.server.server_port

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)

    def request(self, method, path, body=None, *, origin=True, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin:headers["Origin"] = f"http://127.0.0.1:{self.port}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        result = response.status, payload
        connection.close()
        return result

    def test_get_post_host_origin_and_conflict(self):
        path = "/api/workbench/groups"
        state_path = Path(self.temp.name) / "state" / "workbench-groups.json"
        self.assertEqual(self.request("GET", path, origin=False), (200, {"revision": 0, "groups": [], "assignments": {}}))
        self.assertFalse(state_path.exists())
        body = {"expected_revision": 0, "action": "create", "name": "服务合同"}
        self.assertEqual(self.request("POST", path, body, origin=False)[0], 403)
        self.assertEqual(self.request("POST", path, body, host="evil.example:8519")[0], 421)
        status, created = self.request("POST", path, body)
        self.assertEqual(status, 200)
        self.assertEqual(created["revision"], 1)
        status, conflict = self.request("POST", path, body)
        self.assertEqual(status, 409)
        self.assertEqual(conflict["current_revision"], 1)
        bad = {"expected_revision": 1, "action": "assign", "group_id": created["created_group_id"], "item_ids": ["doc-" + "f" * 24]}
        self.assertEqual(self.request("POST", path, bad)[0], 400)


if __name__ == "__main__":
    unittest.main()
