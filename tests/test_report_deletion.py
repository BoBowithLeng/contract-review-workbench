from __future__ import annotations

import http.client
import base64
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from reader_server import REPORTS, create_server
from reader_workbench import _document_id
from report_deletion import DeletionConflict, DeletionError
from review_jobs import _ACTIVE_RUNS, _ACTIVE_RUNS_LOCK, _atomic_json, JobConflict


class ReportDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root, self.states, self.jobs = (self.base / name for name in ("project", "states", "jobs"))
        self.root.mkdir()
        self.original = self.base / "external-original.docx"
        self.original.write_bytes(b"external original must never be removed")
        for spec in REPORTS.values():
            directory = self.root / "runtime" / spec["directory"]
            directory.mkdir(parents=True)
            for name in ("request.json", "response.json", "result.json", "validated-result.json"):
                _atomic_json(directory / name, {"private": "fixture report"})
            (directory / "fixture-original.docx").write_bytes(b"fixture original")
        self.server = self.make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.rid = "job-" + "a" * 32
        self.other = "job-" + "b" * 32
        self.seed_job(self.rid)
        self.seed_job(self.other)

    def make_server(self):
        return create_server(root=self.root, state_dir=self.states, jobs_dir=self.jobs,
                             source_paths={"detailed": self.original, "table": None},
                             review_runner=lambda *_: "", review_engines=[])

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def seed_job(self, rid, *, status="ready"):
        directory = self.jobs / rid
        directory.mkdir(parents=True)
        (directory / "source.docx").write_bytes(b"managed uploaded source")
        _atomic_json(directory / "status.json", {"job_id": rid, "status": status,
                     "document_name": "isolated test contract", "report_id": rid if status == "completed" else None})
        _atomic_json(directory / "result.json", {"private": "saved report"})

    def request(self, path, body=None, *, method="POST", origin=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.server.server_port}"
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def delete(self, item=None):
        return self.request("/api/workbench/delete", {"item_id": item or self.rid, "confirm": True})

    def file(self, relative, data=None):
        path = self.states / relative
        _atomic_json(path, data or {"private": "state"})
        return path

    def test_pending_upload_is_removed_and_original_and_other_round_survive(self):
        status, result = self.delete()
        self.assertEqual(status, 200, result)
        self.assertEqual(result["deleted_report_ids"], [self.rid])
        self.assertFalse((self.jobs / self.rid).exists())
        self.assertTrue((self.jobs / self.other / "source.docx").exists())
        self.assertEqual(self.original.read_bytes(), b"external original must never be removed")
        for path in (f"/api/jobs/{self.rid}", f"/api/reports/{self.rid}", f"/api/reports/{self.rid}/delivery"):
            self.assertEqual(self.request(path, method="GET")[0], 404)
        for path in (f"/api/jobs/{self.rid}/prepare", f"/api/jobs/{self.rid}/start",
                     f"/api/reports/{self.rid}/state", f"/api/reports/{self.rid}/facts/save",
                     f"/api/reports/{self.rid}/discussions/F01", f"/api/reports/{self.rid}/rereview"):
            self.assertEqual(self.request(path, {}, method="POST")[0], 404)
        self.assertFalse((self.jobs / self.rid).exists())
        self.assertEqual(self.delete()[0], 200)  # Explicit retry is idempotent.
        journal = self.server.deletions.path.read_text()
        self.assertNotIn("private", journal)
        self.assertNotIn("isolated test contract", journal)

    def test_completed_round_removes_every_owned_sidecar_and_group_history(self):
        _atomic_json(self.jobs / self.rid / "status.json", {
            "job_id": self.rid, "status": "completed", "report_id": self.rid,
            "document_name": "test completed report"})
        item = _document_id("report:" + self.rid)
        owned = [self.file(f"{self.rid}-digest.json")]
        for sub in ("delivery", "discussions", "opinion-revisions", "contract-facts"):
            owned.append(self.file(f"{sub}/{self.rid}-digest.json"))
            # A crashed atomic write can leave contract data in a temp file.
            temporary = self.states / sub / f".{self.rid}-digest.json.leftover.tmp"
            temporary.write_text("{partial private snapshot", encoding="utf-8")
            owned.append(temporary)
            self.file(f"{sub}/{self.other}-digest.json")
        owned.append(self.file(f"drafts/latest-{self.rid}-F01.json"))
        candidate = "c" * 64
        owned.append(self.file(f"drafts/{candidate}/operation.json", {
            "report_id": self.rid, "candidate_id": candidate, "status": "ready"}))
        self.file(f"drafts/{candidate}/request.json")
        self.file(f"rereviews/{self.rid}/own-request.json", {"job_id": self.other})
        owned.append(self.file(f"rereviews/{self.other}/child-request.json", {"job_id": self.rid}))
        keep_record = self.file(f"rereviews/{self.other}/other-request.json", {"job_id": "job-" + "d" * 32})
        grouped = self.server.workbench_groups.update({"action": "create", "expected_revision": 0, "name": "测试组"})
        gid = grouped["created_group_id"]
        self.server.workbench_groups.update({"action": "assign", "expected_revision": 1,
                                            "group_id": gid, "item_ids": [item, self.other]})
        # Make the deleted assignment part of prior undo history as well.
        self.server.workbench_groups.update({"action": "rename", "expected_revision": 2, "group_id": gid, "name": "改名"})
        status, result = self.delete(item)
        self.assertEqual(status, 200, result)
        self.assertTrue(all(not path.exists() for path in owned))
        self.assertFalse((self.states / "drafts" / candidate).exists())
        self.assertFalse((self.states / "rereviews" / self.rid).exists())
        self.assertTrue(keep_record.exists())
        self.assertTrue((self.jobs / self.other).exists())  # No chain cascade.
        groups = self.server.workbench_groups._load()
        self.assertEqual(groups["assignments"], {self.other: gid})
        self.assertTrue(all(item not in snap["assignments"] for snap in groups["history"]))
        self.assertEqual(result["groups"]["revision"], 4)

    def test_legacy_deletion_physically_removes_snapshots_and_survives_restart(self):
        item = _document_id("report:detailed")
        status, result = self.delete(item)
        self.assertEqual(status, 200, result)
        directory = self.root / "runtime" / REPORTS["detailed"]["directory"]
        self.assertEqual({p.name for p in directory.iterdir()}, {"fixture-original.docx"})
        self.assertTrue(self.original.exists())
        reopened = self.make_server()
        self.addCleanup(reopened.server_close)
        ids = {r["id"] for d in reopened.workbench_catalog()["documents"] for r in d["reviews"]}
        self.assertNotIn("detailed", ids)
        self.assertIn("minimal", ids)
        self.assertIn("table", ids)
        self.assertEqual(self.request("/api/reports/detailed", method="GET")[0], 404)

    def test_retained_child_exposes_deleted_parent_without_rewriting_child_snapshot(self):
        _atomic_json(self.jobs / self.other / "status.json", {"job_id": self.other, "status": "completed", "report_id": self.other})
        child_snapshot = self.jobs / self.other / "result.json"
        before = child_snapshot.read_bytes()
        self.assertEqual(self.delete()[0], 200)
        loaded = {"id": self.other, "title": "child round", "document_name": "child contract",
            "request_digest": "d" * 64, "source_path": self.jobs / self.other / "source.docx",
            "report": {"findings": [], "context": {}, "metadata": {"rereview": {"parent_report_id": self.rid, "round_number": 2}}},
            "contract": {"pieces": [], "fingerprint": "e" * 64}, "sources": [], "paragraphs": [], "completeness": {}}
        with patch.object(self.server.jobs, "load_report", return_value=loaded), patch.object(self.server.delivery, "action_context", return_value={"actions": {}}):
            status, payload = self.request(f"/api/reports/{self.other}", method="GET")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["parent_report_deleted"])
        self.assertEqual(payload["report"]["metadata"]["rereview"]["parent_report_id"], self.rid)
        self.assertEqual(child_snapshot.read_bytes(), before)

    def test_archived_same_source_round_keeps_legacy_group_assignment(self):
        source_sha = "e" * 64
        for rid in (self.rid, self.other):
            _atomic_json(self.jobs / rid / "status.json", {
                "job_id": rid, "status": "completed", "report_id": rid, "source_sha256": source_sha})
        legacy_key = _document_id("source:" + source_sha)
        group_id = "g-" + "f" * 32
        _atomic_json(self.server.workbench_groups.path, {"schema_version": 1, "revision": 0,
            "groups": [{"id": group_id, "name": "旧来源分组"}], "assignments": {legacy_key: group_id}, "history": []})
        self.server.archive.update([{"id": self.other, "document_name": "旧归档"}], archived=True, expected_revision=0)
        self.assertEqual(self.delete(_document_id("report:" + self.rid))[0], 200)
        self.assertEqual(self.server.workbench_groups.get()["assignments"], {legacy_key: group_id})
        self.assertTrue((self.jobs / self.other).exists())

    def test_retry_scrubs_legacy_group_keys_even_after_interrupted_group_write(self):
        _atomic_json(self.jobs / self.rid / "status.json", {
            "job_id": self.rid, "status": "completed", "report_id": self.rid, "source_sha256": "e" * 64})
        legacy_key = _document_id("source:" + "e" * 64)
        group_id = "g-" + "f" * 32
        _atomic_json(self.server.workbench_groups.path, {"schema_version": 1, "revision": 0,
            "groups": [{"id": group_id, "name": "旧来源分组"}], "assignments": {legacy_key: group_id}, "history": []})
        item = _document_id("report:" + self.rid)
        with patch("report_deletion._write_groups", side_effect=PermissionError("locked groups")):
            status, result = self.delete(item)
        self.assertEqual(status, 500, result)
        self.assertTrue(result["pending_deletion"])
        self.assertEqual(self.delete(item)[0], 200)
        self.assertEqual(self.server.workbench_groups.get()["assignments"], {})

    def test_running_prepare_review_and_finally_window_reject_without_changes(self):
        for phase in ("preparing", "running"):
            _atomic_json(self.jobs / self.rid / "status.json", {"job_id": self.rid, "status": phase})
            self.assertEqual(self.delete()[0], 409)
            self.assertFalse(self.server.deletions.path.exists())
        _atomic_json(self.jobs / self.rid / "status.json", {"job_id": self.rid, "status": "ready"})
        key = str((self.jobs / self.rid).resolve()) + ":review"
        with _ACTIVE_RUNS_LOCK:
            _ACTIVE_RUNS.add(key)
        try:
            self.assertEqual(self.delete()[0], 409)
        finally:
            with _ACTIVE_RUNS_LOCK:
                _ACTIVE_RUNS.discard(key)
        self.assertTrue((self.jobs / self.rid).exists())

    def test_real_prepare_and_review_workers_finish_without_resurrecting_deleted_task(self):
        project = Path(__file__).resolve().parents[1]
        (self.root / "prompts").mkdir()
        (self.root / "prompts" / "initial_review.md").write_bytes((project / "prompts" / "initial_review.md").read_bytes())
        document = Document()
        document.add_paragraph("甲方采购设备，乙方交付。")
        output = io.BytesIO()
        document.save(output)
        created = self.server.jobs.create({"document_name": "真实线程测试.docx",
            "docx_base64": base64.b64encode(output.getvalue()).decode(),
            "context": {"contract_type_label": "采购合同", "our_party": "甲方", "our_role": "采购方", "business_goal": ""}})
        rid = created["job"]["job_id"]
        self.server.jobs.engines["injected"] = {"key": "injected", "available": True, "config_fields": []}
        for phase in ("prepare", "review"):
            entered, release = threading.Event(), threading.Event()
            def runner(*_):
                entered.set()
                release.wait(3)
                return json.dumps({"questions": [], "requirements": [], "additional_checks": []}) if phase == "prepare" else "invalid result"
            self.server.jobs.runner = runner
            action = self.server.jobs.prepare if phase == "prepare" else self.server.jobs.start
            _, started = action(rid, {"engine_key": "injected", "consent": True})
            self.assertTrue(started)
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(self.delete(rid)[0], 409)
            finally:
                release.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with _ACTIVE_RUNS_LOCK:
                    active = any(str((self.jobs / rid).resolve()) + suffix in _ACTIVE_RUNS for suffix in (":preparation", ":review"))
                if not active:
                    break
                time.sleep(0.01)
            self.assertFalse(active)
        self.assertEqual(self.delete(rid)[0], 200)
        time.sleep(0.05)
        self.assertFalse((self.jobs / rid).exists())

    def test_deleted_child_submission_cannot_implicitly_replay_old_model_request(self):
        request_id = "01234567-89ab-cdef-0123-456789abcdef"
        submission = self.file(f"rereviews/{self.other}/{request_id}.json", {
            "parent_report_id": self.other, "request_id": request_id, "job_id": self.rid,
            "input_revision": "private input digest"})
        self.assertEqual(self.delete()[0], 200)
        self.assertFalse(submission.exists())
        with self.assertRaisesRegex(JobConflict, "已永久删除"):
            self.server.rereviews.start({"id": self.other}, {}, {"request_id": request_id})
        reopened = self.make_server()
        self.addCleanup(reopened.server_close)
        self.assertTrue(reopened.deletions.deleted_rereview_request(self.other, request_id))
        self.assertFalse(reopened.deletions.deleted_rereview_request(self.other, "11234567-89ab-cdef-0123-456789abcdef"))
        journal = self.server.deletions.path.read_text()
        self.assertNotIn("private input digest", journal)

    def test_running_discussion_revision_and_draft_reject(self):
        for sub, collection in (("discussions", "turns"), ("opinion-revisions", "candidates")):
            path = self.file(f"{sub}/{self.rid}-digest.json", {collection: [{"id": "worker", "status": "running"}]})
            self.assertEqual(self.delete()[0], 409)
            self.assertFalse(self.server.deletions.path.exists())
            path.unlink()
        candidate = "c" * 64
        self.file(f"drafts/{candidate}/operation.json", {"report_id": self.rid, "candidate_id": candidate, "status": "running"})
        self.assertEqual(self.delete()[0], 409)
        self.assertTrue((self.jobs / self.rid).exists())

    def test_active_finished_sidecar_worker_still_rejects(self):
        cases = [("discussions", "turns", self.server.discussions),
                 ("opinion-revisions", "candidates", self.server.revisions)]
        for sub, collection, manager in cases:
            path = self.file(f"{sub}/{self.rid}-digest.json", {collection: [{"id": "worker", "status": "completed"}]})
            manager._active.add("worker")
            try:
                self.assertEqual(self.delete()[0], 409)
            finally:
                manager._active.discard("worker")
                path.unlink()

    def test_invalid_input_and_cross_origin_never_delete(self):
        for payload in ({"item_id": self.rid}, {"item_id": self.rid, "confirm": False},
                        {"item_id": "../states", "confirm": True}, {"item_id": self.rid, "confirm": True, "scope": "all"}):
            self.assertEqual(self.request("/api/workbench/delete", payload)[0], 400)
        self.assertEqual(self.request("/api/workbench/delete", {"item_id": self.rid, "confirm": True}, origin=False)[0], 403)
        self.assertEqual(self.delete("job-" + "e" * 32)[0], 404)
        self.assertTrue((self.jobs / self.rid).exists())

    def test_existing_archive_records_not_affected(self):
        self.server.archive.update([{"id": self.other, "document_name": "old archive"}], archived=True, expected_revision=0)
        before = self.server.archive.path.read_bytes()
        self.assertEqual(self.delete()[0], 200)
        self.assertEqual(self.server.archive.path.read_bytes(), before)
        self.assertTrue((self.jobs / self.other).exists())

    def test_failed_filesystem_delete_blocks_stale_writes_and_explicit_retry_completes(self):
        with patch("report_deletion.shutil.rmtree", side_effect=PermissionError("busy file")):
            status, result = self.delete()
        self.assertEqual(status, 500, result)
        self.assertIn("尚未完成", result["error"])
        self.assertTrue(result["pending_deletion"])
        self.assertEqual(result["item_id"], self.rid)
        self.assertTrue((self.jobs / self.rid).exists())
        self.assertEqual(self.request(f"/api/jobs/{self.rid}/start", {})[0], 404)
        catalog = self.server.workbench_catalog()
        self.assertEqual(catalog["pending_deletions"], [{"item_id": self.rid, "report_id": self.rid}])
        self.assertNotIn(self.rid, {j["id"] for j in catalog["pending_jobs"]})
        self.assertEqual(self.delete()[0], 200)
        self.assertFalse((self.jobs / self.rid).exists())

    def test_retry_finishes_partially_removed_draft_even_after_binding_files_disappear(self):
        candidate = "c" * 64
        operation = self.file(f"drafts/{candidate}/operation.json", {"report_id": self.rid, "candidate_id": candidate, "status": "ready"})
        request = self.file(f"drafts/{candidate}/request.json", {"identity": {"report_id": self.rid}})
        raw = operation.parent / "raw-response.txt"
        raw.write_text("private candidate text", encoding="utf-8")
        import shutil
        original = shutil.rmtree
        def partial(path):
            if Path(path).name == candidate:
                operation.unlink()
                request.unlink()
                raise PermissionError("busy raw response")
            return original(path)
        with patch("report_deletion.shutil.rmtree", side_effect=partial):
            status, result = self.delete()
        self.assertEqual(status, 500, result)
        self.assertTrue(raw.exists())
        self.assertEqual(self.delete()[0], 200)
        self.assertFalse(operation.parent.exists())

    def test_concurrent_stale_request_cannot_recreate_after_delete(self):
        entered, release = threading.Event(), threading.Event()
        original = self.server.jobs.get

        def pause_get(rid):
            entered.set()
            release.wait(3)
            # Mimic a request that writes a cache after loading a task.
            _atomic_json(self.jobs / rid / "request-cache.json", {"cached": True})
            return {"job_id": rid, "status": "ready"}

        result = []
        with patch.object(self.server.jobs, "get", side_effect=pause_get):
            getter = threading.Thread(target=lambda: result.append(self.request(f"/api/jobs/{self.rid}", method="GET")))
            getter.start()
            self.assertTrue(entered.wait(2))
            deleter = threading.Thread(target=lambda: result.append(self.delete()))
            deleter.start()
            time.sleep(0.05)
            self.assertTrue(deleter.is_alive())
            release.set()
            getter.join(3)
            deleter.join(3)
        self.assertEqual([r[0] for r in result], [200, 200])
        self.assertFalse((self.jobs / self.rid).exists())
        self.assertEqual(self.request(f"/api/jobs/{self.rid}", method="GET")[0], 404)

    def test_corrupt_related_record_and_deleted_directory_fail_closed(self):
        bad = self.states / "discussions" / f"{self.rid}-digest.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.delete()[0], 500)
        self.assertTrue((self.jobs / self.rid).exists())
        self.assertFalse(self.server.deletions.path.exists())
        bad.unlink()
        self.server.deletions.path.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.delete()[0], 500)
        self.assertTrue((self.jobs / self.rid).exists())

    def test_symlink_or_junction_inside_managed_job_is_not_followed(self):
        link = self.jobs / self.rid / "linked-original"
        try:
            link.symlink_to(self.original)
        except (OSError, NotImplementedError):
            self.skipTest("Windows symlink privilege unavailable")
        status, _ = self.delete()
        self.assertEqual(status, 500)
        self.assertTrue(self.original.exists())
        self.assertTrue((self.jobs / self.rid).exists())


if __name__ == "__main__":
    unittest.main()
