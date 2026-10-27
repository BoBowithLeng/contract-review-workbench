"""Desktop isolation tests; no model, browser or external network is invoked."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app_paths import DesktopPaths, desktop_paths, ensure_writable_directory
from desktop_runtime import (DesktopRuntime, InstanceLock, INSTANCE_FILE, INITIALIZED_FILE,
                             existing_instance, initialize_data)
from reader_server import REPORTS, create_server
import review_runner

PROJECT = Path(__file__).resolve().parents[1]


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="desktop-runtime-test-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.resources = base / "程序资源"
        (self.resources / "reader_ui").mkdir(parents=True)
        (self.resources / "reader_ui" / "index.html").write_text("<h1>isolated desktop</h1>", encoding="utf-8")
        (self.resources / "prompts").mkdir()
        (self.resources / "prompts" / "initial_review.md").write_text("Only test prompt.", encoding="utf-8")
        (self.resources / "builtin_checklists.json").write_bytes((PROJECT / "builtin_checklists.json").read_bytes())
        seed = self.resources / "desktop_seed"
        seed.mkdir(parents=True)
        with zipfile.ZipFile(seed / "runtime.zip", "w") as archive:
            archive.writestr("合成输入.txt", "independent synthetic input")
        self.paths = DesktopPaths(self.resources, base / "我的数据")

    def start(self):
        runtime = DesktopRuntime(self.paths)
        self.addCleanup(runtime.stop)
        with patch.object(review_runner, "find_codex", side_effect=AssertionError("No Codex discovery")), \
             patch.object(review_runner.subprocess, "run", side_effect=AssertionError("No subprocess/model")):
            url, started = runtime.start()
        self.assertTrue(started)
        self.assertIn("127.0.0.1", url)
        return runtime

    def request(self, runtime, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", runtime.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()

    def test_resource_data_roots_and_source_defaults(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "_MEIPASS", str(self.resources), create=True), \
             patch.object(sys, "executable", str(self.paths.data.parent / "审查台.exe")):
            self.assertEqual(desktop_paths(), self.paths)
            self.assertEqual(desktop_paths(self.paths.data), self.paths)
            with self.assertRaises(ValueError):
                desktop_paths(self.resources)
            with self.assertRaises(ValueError):
                desktop_paths(self.resources / "runtime" / "anything")
        source = create_server(root=self.resources, review_runner=lambda *_: None)
        self.addCleanup(source.server_close)
        self.assertEqual(source.resource_root, source.project_root)
        self.assertEqual(source.legacy_reports, REPORTS)

    def test_seed_only_once_and_never_restore_deleted_samples(self):
        ensure_writable_directory(self.paths.data)
        self.assertTrue(initialize_data(self.paths))
        sample = self.paths.runtime / "合成输入.txt"
        sample.unlink()
        (self.paths.runtime / "user.txt").write_text("user edited data", encoding="utf-8")
        self.assertFalse(initialize_data(self.paths))
        self.assertFalse(sample.exists())
        self.assertEqual((self.paths.runtime / "user.txt").read_text(), "user edited data")
        self.assertTrue((self.paths.data / INITIALIZED_FILE).exists())

    def test_nonempty_directory_and_bad_data_never_trigger_seed_repair(self):
        ensure_writable_directory(self.paths.data)
        (self.paths.data / "existing.json").write_text("broken {", encoding="utf-8")
        self.assertFalse(initialize_data(self.paths))
        self.assertFalse(self.paths.runtime.exists())
        self.assertEqual((self.paths.data / "existing.json").read_text(), "broken {")

    def test_missing_seed_errors_and_releases_lock(self):
        seed = self.resources / "desktop_seed"
        seed.rename(self.resources / "removed_seed")
        runtime = DesktopRuntime(self.paths)
        with self.assertRaisesRegex(RuntimeError, "缺少示例数据"):
            runtime.start()
        lock = InstanceLock(self.paths.data)
        self.assertTrue(lock.acquire())
        lock.close()

    def test_unwritable_target_errors_without_fallback(self):
        file = self.paths.data.parent / "not-directory"
        file.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "无法写入数据目录"):
            ensure_writable_directory(file)
        self.assertEqual(file.read_text(), "keep")

    def test_http_assets_engines_checklists_and_empty_catalog_are_isolated(self):
        runtime = self.start()
        server = runtime.server
        self.assertEqual(server.project_root, self.paths.data)
        self.assertEqual(server.resource_root, self.resources)
        self.assertEqual(server.jobs.root, self.paths.runtime / "review-jobs")
        self.assertEqual(server.jobs._initial_prompt(), "Only test prompt.")
        self.assertEqual(set(server.delivery.source_paths.values()), {None})
        self.assertNotEqual(server.server_port, 8519)
        status, body = self.request(runtime, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"isolated desktop", body)
        for path in ("/api/workbench", "/api/reports", "/api/checklists"):
            status, body = self.request(runtime, path)
            self.assertEqual(status, 200, body)
            value = json.loads(body)
            if path == "/api/workbench":
                self.assertEqual(value["documents"], [])
            elif path == "/api/reports":
                self.assertEqual(value["reports"], [])
            else:
                self.assertTrue(value["checklists"])
        status, body = self.request(runtime, "/api/review-capabilities")
        self.assertEqual(status, 200)
        self.assertEqual([e["key"] for e in json.loads(body)["engines"]], ["compatible_api"])
        self.assertEqual(self.request(runtime, "/api/reports/detailed")[0], 404)
        self.assertFalse((self.paths.runtime / "reader-state" / "report-archive.json").exists())

    def test_second_launch_reuses_verified_instance_and_does_not_remove_its_marker(self):
        first = self.start()
        second = DesktopRuntime(self.paths)
        url, started = second.start()
        self.assertFalse(started)
        self.assertEqual(url, existing_instance(self.paths))
        self.assertIsNone(second.server)
        second.stop()
        self.assertTrue((self.paths.data / INSTANCE_FILE).exists())
        self.assertEqual(self.request(first, "/")[0], 200)

    def test_marker_needs_matching_identity_and_live_token(self):
        runtime = self.start()
        marker = self.paths.data / INSTANCE_FILE
        original = json.loads(marker.read_text())
        for field, wrong in (("identity", "different"), ("token", "a" * 64), ("port", 0)):
            modified = {**original, field: wrong}
            marker.write_text(json.dumps(modified), encoding="utf-8")
            self.assertIsNone(existing_instance(self.paths))
        marker.write_text(json.dumps(original), encoding="utf-8")
        self.assertIsNotNone(existing_instance(self.paths))
        self.assertEqual(self.request(runtime, "/api/desktop-instance")[0], 404)

    def test_stop_removes_temporary_port_marker_and_allows_new_instance(self):
        runtime = self.start()
        runtime.stop()
        self.assertFalse((self.paths.data / INSTANCE_FILE).exists())
        self.assertIsNone(existing_instance(self.paths))
        replacement = self.start()
        self.assertEqual(self.request(replacement, "/")[0], 200)

    def test_frozen_capabilities_do_not_discover_codex_and_refuse_cli(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(review_runner, "find_codex", side_effect=AssertionError("Must not discover")):
            self.assertEqual([e["key"] for e in review_runner.capabilities()["engines"]], ["compatible_api"])
            with self.assertRaisesRegex(review_runner.ReviewRunnerError, "只支持"):
                review_runner.run_review([{"role": "user", "content": "text"}], {"engine_key": "codex_cli"})

    def test_reserved_development_port_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "8519"):
            DesktopRuntime(self.paths, port=8519).start()
        self.assertFalse(self.paths.data.exists())

    def test_process_start_reopen_and_graceful_control_exit(self):
        exit_request = self.paths.data.parent / "close-test-control"
        # Simulate closing the native window, not a model or the reader server.
        bootstrap = ("import sys,time; from pathlib import Path; sys._MEIPASS=sys.argv[1]; "
                     "import desktop_launcher; "
                     "exec('def control(*args, **kwargs):\\n while not Path(sys.argv[2]).exists(): time.sleep(0.1)'); "
                     "desktop_launcher.native_window=control; "
                     "raise SystemExit(desktop_launcher.main(sys.argv[3:]))")
        command = [sys.executable, "-c",
                   bootstrap, str(self.resources), str(exit_request),
                   "--data-dir", str(self.paths.data), "--no-browser"]
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        process = subprocess.Popen(command, cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", **options)
        def cleanup_process():
            exit_request.touch()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
        self.addCleanup(cleanup_process)
        import time
        deadline = time.monotonic() + 15
        url = None
        while time.monotonic() < deadline:
            url = existing_instance(self.paths)
            if url or process.poll() is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(url)
        reopened = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(reopened.returncode, 0, reopened.stderr)
        self.assertIn(url, reopened.stdout)
        exit_request.touch()
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn(url, stdout)
        self.assertFalse((self.paths.data / INSTANCE_FILE).exists())


if __name__ == "__main__":
    unittest.main()


class SeedArchiveTests(unittest.TestCase):
    def make(self, members):
        temp = tempfile.TemporaryDirectory(prefix="desktop-seed-test-")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        (base / "资源" / "desktop_seed").mkdir(parents=True)
        with zipfile.ZipFile(base / "资源" / "desktop_seed" / "runtime.zip", "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        paths = DesktopPaths(base / "资源", base / "我的数据")
        ensure_writable_directory(paths.data)
        return paths

    def test_archive_extracts_nested_records_and_recovers_from_interrupted_staging(self):
        paths = self.make({"review-jobs/job-1/status.json": "{}", "reader-state/post-review/x/publication.json": "{}"})
        (paths.data / ".runtime-seed").mkdir()
        (paths.data / ".runtime-seed" / "half.json").write_text("partial", encoding="utf-8")
        self.assertTrue(initialize_data(paths))
        self.assertEqual((paths.runtime / "review-jobs" / "job-1" / "status.json").read_text(), "{}")
        self.assertTrue((paths.runtime / "reader-state" / "post-review" / "x" / "publication.json").is_file())
        self.assertFalse((paths.data / ".runtime-seed").exists())
        self.assertFalse((paths.runtime / "half.json").exists())

    def test_unsafe_member_paths_are_refused_before_writing(self):
        for name in ("../escape.json", "/abs.json", "C:/drive.json"):
            with self.subTest(name=name):
                paths = self.make({"ok.json": "{}", name: "{}"})
                with self.assertRaisesRegex(RuntimeError, "不安全"):
                    initialize_data(paths)
                self.assertFalse(paths.runtime.exists())
                self.assertFalse((paths.data / ".runtime-seed").exists())
