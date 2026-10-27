"""Removal only against temporary simulated packages; never house data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from desktop_removal import (MANIFEST_NAME, RemovalError,
                             prepare_removal, write_program_manifest)

PROJECT = Path(__file__).resolve().parents[1]


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portable-removal-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.app = self.base / "模拟程序"
        self.app.mkdir()
        (self.app / "程序.exe").write_bytes(b"synthetic executable")
        (self.app / "_internal").mkdir()
        (self.app / "_internal" / "resource.txt").write_text("immutable resource")
        write_program_manifest(self.app, version="test.1")
        self.data = self.app / "我的数据"
        (self.data / "runtime" / "reader-state").mkdir(parents=True)
        (self.data / ".desktop-initialized").write_text("1\n")
        (self.data / "runtime" / "reader-state" / "state.json").write_text('{"test":true}')

    def test_manifest_excludes_itself_and_records_content(self):
        manifest = json.loads((self.app / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertNotIn(MANIFEST_NAME, manifest["files"])
        self.assertEqual(manifest["files"]["程序.exe"]["size"], 20)
        self.assertEqual(len(manifest["files"]["程序.exe"]["sha256"]), 64)

    def test_default_preserves_data_modified_and_unknown_files(self):
        (self.app / "_internal" / "resource.txt").write_text("my edited resource")
        (self.app / "我的私人材料.txt").write_text("not program-owned")
        plan = prepare_removal(self.app, self.data)
        self.assertEqual(plan["preserved_data"], str(self.data))
        self.assertEqual(plan["data_files"], [])
        self.assertEqual(len(plan["preserved_files"]), 2)
        self.assertNotIn("_internal/resource.txt", {r["relative"] for r in plan["program_files"]})

    def test_recycle_data_is_finite_and_leaves_unmanaged_data(self):
        (self.data / "私人文件.txt").write_text("keep")
        plan = prepare_removal(self.app, self.data, True)
        self.assertEqual({r["relative"] for r in plan["data_files"]},
                         {".desktop-initialized", "runtime/reader-state/state.json"})
        self.assertIn("非受管数据", "".join(plan["preserved_files"]))

    def test_refuses_source_external_data_and_traversal(self):
        with self.assertRaises(RemovalError):
            prepare_removal(PROJECT, PROJECT / "runtime")
        external = self.base / "external"
        external.mkdir()
        with self.assertRaises(RemovalError):
            prepare_removal(self.app, external, True)
        self.assertEqual(prepare_removal(self.app, external)["preserved_data"], str(external))
        manifest = json.loads((self.app / MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["files"]["../escape"] = next(iter(manifest["files"].values()))
        (self.app / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(RemovalError):
            prepare_removal(self.app, self.data)

    @unittest.skipUnless(os.name == "nt", "Windows junction safety")
    def test_junction_target_is_not_followed(self):
        # Native PowerShell only; targets remain inside this test's temp tree.
        target = self.base / "outside"
        target.mkdir()
        (target / "resource.txt").write_text("outside")
        # Replace the owned file by a symlink where privilege is available, or
        # create a directory junction to test the same reparse-point fence.
        junction = self.app / "linked-data"
        escaped = str(junction).replace("'", "''")
        escaped_target = str(target).replace("'", "''")
        subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                        f"New-Item -ItemType Junction -Path '{escaped}' -Target '{escaped_target}' | Out-Null"], check=True)
        self.addCleanup(lambda: os.rmdir(junction) if junction.exists() else None)
        with self.assertRaises(RemovalError):
            prepare_removal(self.app, junction, True)
        self.assertTrue((target / "resource.txt").exists())

    @unittest.skipUnless(os.name == "nt", "Actual deferred Windows helper")
    def test_delayed_helper_keep_data(self):
        self._run_delayed(False)

    @unittest.skipUnless(os.name == "nt", "Actual Windows Recycle Bin")
    def test_delayed_helper_recycle_data(self):
        self._run_delayed(True)

    @unittest.skipUnless(os.name == "nt", "Actual deferred Windows helper")
    def test_helper_preserves_files_modified_after_scheduling(self):
        self._run_delayed(False, mutate_program=True)

    @unittest.skipUnless(os.name == "nt", "Actual WebView cache shutdown snapshot")
    def test_helper_recycles_profile_modified_during_shutdown(self):
        self._run_delayed(True, profile=True)

    @unittest.skipUnless(os.name == "nt", "Actual Windows data lock")
    def test_helper_recycles_after_runtime_releases_byte_lock(self):
        self._run_delayed(True, lock_file=True)

    @unittest.skipUnless(os.name == "nt", "Actual Windows removal mutual exclusion")
    def test_helper_keeps_lock_path_until_program_cleanup_finishes(self):
        self._run_delayed(True, lock_file=True, lock_checkpoint=True)

    @unittest.skipUnless(os.name == "nt", "Actual Windows process handle verification")
    def test_wrong_process_identity_reports_failure_without_deletion(self):
        plan = prepare_removal(self.app, self.data)
        helper = self.base / "helper.ps1"
        helper.write_text((PROJECT / "desktop" / "remove_helper.ps1").read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
        report = self.base / "failure.json"
        plan.update(pid=os.getpid(), creation_filetime="0", report_path=str(report),
                    ready_path=str(self.base / "ready"), show_result=False)
        request = self.base / "request.json"
        request.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(helper),
                        "-RequestPath", str(request)], check=True, timeout=20, capture_output=True)
        result = json.loads(report.read_text(encoding="utf-8-sig"))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["errors"])
        self.assertTrue((self.app / "程序.exe").exists())
        self.assertFalse((self.base / "ready").exists())

    def _run_delayed(self, delete_data, *, mutate_program=False, profile=False, lock_file=False,
                     lock_checkpoint=False):
        (self.app / "unknown.txt").write_text("must remain")
        if profile:
            (self.data / "webview2").mkdir()
            (self.data / "webview2" / "cache.bin").write_bytes(b"cache before exit")
        if lock_file:
            (self.data / ".desktop.lock").write_bytes(b"\0")
        helper_home = ""
        if lock_checkpoint:
            # Preserve this unrelated file so even the old, premature unlink
            # leaves a directory in which a second instance could recreate lock.
            (self.data / "私人保留.txt").write_text("not application data")
            helper_home = self.base / "instrumented-helper"
            (helper_home / "desktop").mkdir(parents=True)
            script = (PROJECT / "desktop" / "remove_helper.ps1").read_text(encoding="utf-8-sig")
            needle = "    foreach ($row in $request.program_files) {"
            self.assertEqual(script.count(needle), 1)
            pause = """    [System.IO.File]::WriteAllText($request.report_path + '.program-phase', 'paused')
    while (-not (Test-Path -LiteralPath ($request.report_path + '.continue'))) { Start-Sleep -Milliseconds 20 }
"""
            (helper_home / "desktop" / "remove_helper.ps1").write_text(script.replace(needle, pause + needle), encoding="utf-8-sig")
        release = self.base / "exit-request"
        scheduled_file = self.base / "scheduled.json"
        code = """import json,time,sys
from pathlib import Path
import desktop_removal
from desktop_removal import prepare_removal,launch_removal
if sys.argv[6]: desktop_removal.__file__=str(Path(sys.argv[6])/'desktop_removal.py')
lock=None
if (Path(sys.argv[2])/'.desktop.lock').exists():
    import msvcrt
    lock=(Path(sys.argv[2])/'.desktop.lock').open('r+b')
    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
p=prepare_removal(Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]=='1')
r=launch_removal(p,show_result=False)
Path(sys.argv[4]).write_text(json.dumps(r),encoding='utf-8')
while not Path(sys.argv[5]).exists(): time.sleep(.02)
profile=Path(sys.argv[2])/'webview2'
if profile.exists():
    (profile/'cache.bin').write_bytes(b'cache flushed on exit')
    (profile/'new-cache.bin').write_bytes(b'new cache on exit')
if lock:
    lock.seek(0)
    msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
    lock.close()
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(self.app), str(self.data),
                                    "1" if delete_data else "0", str(scheduled_file), str(release), str(helper_home)],
                                   cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 25
        while not scheduled_file.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        if not scheduled_file.exists():
            release.touch()
            output, error = process.communicate(timeout=10)
            self.fail(f"Helper not scheduled: {output!r} {error!r}")
        scheduled = json.loads(scheduled_file.read_text(encoding="utf-8"))
        report = Path(scheduled["report_path"])
        self.assertTrue(scheduled["scheduled"])
        time.sleep(.3)
        self.assertFalse(report.exists(), "Helper must not delete before actual process exit")
        self.assertTrue((self.app / "程序.exe").exists())
        if mutate_program:
            (self.app / "_internal" / "resource.txt").write_text("modified after confirmation")
        release.touch()
        _, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        if lock_checkpoint:
            from desktop_runtime import InstanceLock
            phase = Path(str(report) + ".program-phase")
            resume = Path(str(report) + ".continue")
            deadline = time.monotonic() + 20
            while not phase.exists() and not report.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            try:
                self.assertTrue(phase.exists(), "Helper must reach real program deletion phase")
                self.assertTrue((self.app / "程序.exe").exists())
                contender = InstanceLock(self.data)
                try:
                    self.assertFalse(contender.acquire(), "Second instance bypassed helper lock during program cleanup")
                finally:
                    contender.close()
            finally:
                resume.touch()
        deadline = time.monotonic() + 35
        while not report.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertTrue(report.exists(), "Helper must produce final visible report")
        result = json.loads(report.read_text(encoding="utf-8-sig"))
        self.assertFalse(result["errors"], result)
        self.assertEqual(result["status"], "partial")  # unknown.txt preserved
        self.assertFalse((self.app / "程序.exe").exists())
        self.assertEqual((self.app / "_internal").exists(), mutate_program)
        if mutate_program:
            self.assertEqual((self.app / "_internal" / "resource.txt").read_text(), "modified after confirmation")
        self.assertTrue((self.app / "unknown.txt").exists())
        if delete_data:
            self.assertEqual(self.data.exists(), lock_checkpoint, result)
            if lock_checkpoint:
                self.assertTrue((self.data / "私人保留.txt").exists())
                self.assertFalse((self.data / ".desktop.lock").exists())
            self.assertEqual(len(result["recycled_data"]), 4 if profile else 2)
            self.assertIsNone(result["recovery_zip"], "Verified recycling must not retain another copy")
        else:
            self.assertTrue((self.data / "runtime" / "reader-state" / "state.json").exists())
            self.assertFalse(result["recycled_data"])


if __name__ == "__main__":
    unittest.main()
