"""Native lifecycle/bridge checks; fixtures use isolated data and no model calls."""
from __future__ import annotations

import http.client
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desktop_runtime import activate_existing_instance
from desktop_window import DesktopBridge, DesktopWindow, external_url, local_download, trusted_page
import test_desktop_runtime as runtime_tests


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.url = "http://127.0.0.1:41321/"
        self.runtime = SimpleNamespace(paths=SimpleNamespace(data=Path("isolated-data")),
                                       begin_close=Mock(return_value=True), cancel_close=Mock())
        self.controller = DesktopWindow(self.runtime, self.url)
        self.controller.window = Mock()
        self.controller.window.get_current_url.return_value = self.url
        self.bridge = DesktopBridge(self.controller)

    def test_trusted_pages_require_exact_origin_and_known_page(self):
        for good in (self.url, self.url + "?report_id=anything", self.url + "checklists.html#edit"):
            self.assertTrue(trusted_page(good, self.url), good)
        for bad in ("https://example.com/", "http://localhost:41321/", "http://127.0.0.1:41322/",
                    "http://bad@127.0.0.1:41321/", self.url + "api/reports", "data:text/html,anything",
                    "blob:" + self.url + "id", "file:///C:/anything", "http://127.0.0.1:bad/"):
            self.assertFalse(trusted_page(bad, self.url), bad)
        self.assertTrue(external_url("https://example.com/"))
        self.assertFalse(external_url("javascript:alert(1)"))

    def test_native_downloads_allow_local_attachments_and_blob_without_bridge(self):
        for good in (self.url + "api/reports/job-id/export.docx", self.url + "api/checklists/template.xlsx",
                     "blob:" + self.url + "some-id"):
            self.assertTrue(local_download(good, self.url))
            self.assertFalse(trusted_page(good, self.url))
        self.assertFalse(local_download("blob:https://example.com/some-id", self.url))
        self.assertFalse(local_download(self.url + "anything.html", self.url))

    def test_source_info_is_available_but_cannot_remove(self):
        with patch.object(sys, "frozen", False, create=True):
            info = self.bridge.get_desktop_info()
            self.assertTrue(info["available"])
            self.assertFalse(info["remove_allowed"])
            self.assertEqual(info["data_dir"], str(self.runtime.paths.data))
            self.assertFalse(self.bridge.request_remove(False)["ok"])
        self.runtime.begin_close.assert_not_called()

    def test_open_data_folder_shows_only_the_app_data_folder(self):
        with patch("desktop_window.os.startfile", create=True) as start, patch.object(Path, "is_dir", return_value=True):
            self.assertEqual(self.bridge.open_data_folder(), {"ok": True})
            start.assert_called_once_with(self.runtime.paths.data)
        with patch("desktop_window.os.startfile", create=True) as start, patch.object(Path, "is_dir", return_value=False):
            self.assertFalse(self.bridge.open_data_folder()["ok"])
            start.assert_not_called()
        self.controller.window.get_current_url.return_value = "https://example.com/"
        with patch("desktop_window.os.startfile", create=True) as start:
            self.assertFalse(self.bridge.open_data_folder()["ok"])
            start.assert_not_called()

    def test_bridge_rejects_external_current_url_and_invalid_boolean(self):
        self.controller.window.get_current_url.return_value = "https://example.com/"
        self.assertFalse(self.bridge.get_desktop_info()["available"])
        with patch.object(sys, "frozen", True, create=True):
            self.assertFalse(self.bridge.request_remove(False)["ok"])
            self.assertFalse(self.bridge.request_remove("false")["ok"])
        self.runtime.begin_close.assert_not_called()

    def test_close_blocks_busy_work_and_downloads_then_reserves_shutdown(self):
        self.runtime.begin_close.return_value = False
        with patch("desktop_window.show_error") as error:
            self.assertFalse(self.controller.closing())
            error.assert_called_once()
        self.runtime.begin_close.return_value = True
        self.controller._downloads.add("download")
        with patch("desktop_window.show_error"):
            self.assertFalse(self.controller.closing())
        self.controller._downloads.clear()
        self.assertTrue(self.controller.closing())

    def test_remove_cancel_and_error_restore_service(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch("desktop_removal.prepare_removal", return_value={}) as prepare, \
             patch("desktop_removal.launch_removal") as launch:
            self.controller.window.create_confirmation_dialog.return_value = False
            self.assertTrue(self.bridge.request_remove(False)["cancelled"])
            self.runtime.cancel_close.assert_called_once()
            launch.assert_not_called()
            self.runtime.cancel_close.reset_mock()
            self.controller.window.create_confirmation_dialog.return_value = True
            prepare.side_effect = RuntimeError("Invalid manifest")
            self.assertIn("Invalid manifest", self.bridge.request_remove(True)["error"])
            self.runtime.cancel_close.assert_called_once()

    def test_remove_schedules_then_delays_destroy(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch("desktop_removal.prepare_removal", return_value={"plan": True}), \
             patch("desktop_removal.launch_removal", return_value={"scheduled": True}) as launch, \
             patch("desktop_window.threading.Timer") as timer:
            self.controller.window.create_confirmation_dialog.return_value = True
            self.assertTrue(self.bridge.request_remove(True)["ok"])
            launch.assert_called_once_with({"plan": True}, show_result=True)
            timer.assert_called_once_with(0.5, self.controller.window.destroy)
            timer.return_value.start.assert_called_once()
            self.assertTrue(self.controller.closing())
            self.assertFalse(self.bridge.request_remove(False)["ok"])


class RuntimeWindowTests(unittest.TestCase):
    # Reuse the isolated resource fixture and HTTP helper, without rerunning
    # DesktopTests itself when this file is selected.
    setUp = runtime_tests.DesktopTests.setUp
    start = runtime_tests.DesktopTests.start
    request = runtime_tests.DesktopTests.request
    def test_activation_authenticated_and_queued_until_native_window_ready(self):
        runtime = self.start()
        connection = http.client.HTTPConnection("127.0.0.1", runtime.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("POST", "/api/desktop-activate", body="", headers={"X-Desktop-Token": "wrong"})
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        response.read()
        self.assertTrue(activate_existing_instance(self.paths))
        callback = Mock()
        runtime.set_activation_callback(callback)
        callback.assert_called_once()
        self.assertTrue(activate_existing_instance(self.paths))
        self.assertEqual(callback.call_count, 2)

    def test_pending_request_and_background_work_prevent_close(self):
        runtime = self.start()
        runtime._requests = 1
        self.assertFalse(runtime.begin_close())
        runtime._requests = 0
        for manager in (runtime.server.drafts, runtime.server.discussions,
                        runtime.server.revisions, runtime.server.information_gate):
            manager._active.add("native-test")
            self.assertFalse(runtime.begin_close())
            manager._active.remove("native-test")
        from review_jobs import _ACTIVE_RUNS, _ACTIVE_RUNS_LOCK
        key = str((runtime.server.jobs.root / "job-test").resolve()) + ":review"
        with _ACTIVE_RUNS_LOCK:
            _ACTIVE_RUNS.add(key)
        try:
            self.assertFalse(runtime.begin_close())
        finally:
            with _ACTIVE_RUNS_LOCK:
                _ACTIVE_RUNS.remove(key)
        self.assertTrue(runtime.begin_close())
        self.assertEqual(self.request(runtime, "/api/checklists")[0], 503)
        runtime.cancel_close()
        self.assertEqual(self.request(runtime, "/api/checklists")[0], 200)


if __name__ == "__main__":
    unittest.main()
