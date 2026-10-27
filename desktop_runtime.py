"""Portable loopback runtime; no model calls or browser actions on startup."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import shutil
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

from app_paths import DesktopPaths, ensure_writable_directory
from reader_server import create_server
from review_runner import capabilities, run_review

LOCK_FILE = ".desktop.lock"
INSTANCE_FILE = ".desktop-instance.json"
INITIALIZED_FILE = ".desktop-initialized"
SEED_ARCHIVE = "runtime.zip"
SEED_STAGING = ".runtime-seed"


class DesktopAlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """The OS releases this lock on exit/crash; PIDs are never used to kill."""

    def __init__(self, data: Path):
        self.path = data / LOCK_FILE
        self.stream = None

    def acquire(self) -> bool:
        stream = self.path.open("a+b")
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def close(self) -> None:
        if self.stream is not None:
            self.stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None


def instance_identity(paths: DesktopPaths) -> str:
    value = str(paths.resources) + "\n" + str(paths.data)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def existing_instance(paths: DesktopPaths) -> str | None:
    """Validate a live endpoint, identity and random token, never just its PID."""
    try:
        record = json.loads((paths.data / INSTANCE_FILE).read_text(encoding="utf-8"))
        port, token = record["port"], record["token"]
        if (record["identity"] != instance_identity(paths) or not isinstance(port, int)
                or isinstance(port, bool) or not 1 <= port <= 65535
                or not isinstance(token, str) or len(token) != 64):
            return None
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", "/api/desktop-instance", headers={"X-Desktop-Token": token})
            response = connection.getresponse()
            raw = response.read(4097)
            if response.status != 200 or len(raw) > 4096 or json.loads(raw) != record:
                return None
        finally:
            connection.close()
        return f"http://127.0.0.1:{port}/"
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
        return None


def activate_existing_instance(paths: DesktopPaths) -> bool:
    """Ask the verified owning process to raise its window, never open a browser."""
    if existing_instance(paths) is None:
        return False
    try:
        record = json.loads((paths.data / INSTANCE_FILE).read_text(encoding="utf-8"))
        connection = http.client.HTTPConnection("127.0.0.1", record["port"], timeout=3)
        try:
            connection.request("POST", "/api/desktop-activate", body=b"",
                               headers={"X-Desktop-Token": record["token"]})
            response = connection.getresponse()
            response.read()
            return response.status == 200
        finally:
            connection.close()
    except (OSError, ValueError, KeyError, http.client.HTTPException):
        return False


def initialize_data(paths: DesktopPaths) -> bool:
    """Only a completely new directory gets the template. Never repair/refill."""
    # A staging folder left by an interrupted first start does not count as user data.
    if any(item.name not in {LOCK_FILE, SEED_STAGING} for item in paths.data.iterdir()):
        return False
    # The examples ship as one archive so the distributed ZIP keeps short paths.
    seed = paths.resources / "desktop_seed" / SEED_ARCHIVE
    if not seed.is_file():
        raise RuntimeError("试用包缺少示例数据 desktop_seed/runtime.zip，请重新解压完整试用包。")
    staging = paths.data / SEED_STAGING
    with zipfile.ZipFile(seed) as archive:
        for info in archive.infolist():
            parts = PurePosixPath(info.filename).parts
            if (not parts or info.filename.startswith(("/", "\\")) or ".." in parts or ":" in info.filename
                    or "\\" in info.filename or (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise RuntimeError("示例数据含不安全的路径，无法初始化。")
        if staging.exists():
            shutil.rmtree(staging)
        archive.extractall(staging)
    staging.rename(paths.runtime)
    (paths.data / INITIALIZED_FILE).write_text("1\n", encoding="utf-8")
    return True


class DesktopRuntime:
    def __init__(self, paths: DesktopPaths, *, port: int = 0):
        self.paths = paths
        self.port = port
        self.lock = InstanceLock(paths.data)
        self.server = None
        self.thread = None
        self.record = None
        self._actions = threading.RLock()
        self._requests = 0
        self._closing = False
        self._activate = None
        self._activation_pending = False

    def set_activation_callback(self, callback) -> None:
        with self._actions:
            self._activate = callback
            pending = self._activation_pending
            self._activation_pending = False
        if pending:
            callback()

    def activate(self) -> None:
        with self._actions:
            callback = self._activate
            if callback is None:
                self._activation_pending = True
        if callback is not None:
            callback()

    def busy(self) -> bool:
        """Check live work, not saved orphan statuses; no recovery writes here."""
        from review_jobs import _ACTIVE_RUNS, _ACTIVE_RUNS_LOCK
        server = self.server
        with self._actions:
            if self._requests:
                return True
            if server is None:
                return False
            with _ACTIVE_RUNS_LOCK:
                prefix = str(server.jobs.root.resolve()) + os.sep
                if any(key.startswith(prefix) for key in _ACTIVE_RUNS):
                    return True
            return any(bool(manager._active) for manager in (
                server.drafts, server.discussions, server.revisions, server.information_gate))

    def begin_close(self) -> bool:
        """Atomically reject new requests once all work is idle."""
        with self._actions:
            if self.busy():
                return False
            self._closing = True
            return True

    def cancel_close(self) -> None:
        with self._actions:
            self._closing = False

    def _install_desktop_handler(self) -> None:
        owner = self
        base = self.server.RequestHandlerClass

        class DesktopHandler(base):
            def do_POST(self):
                if self.path == "/api/desktop-activate":
                    if self.headers.get("X-Desktop-Token") != owner.record["token"]:
                        self._error(404, "资源不存在。")
                        return
                    owner.activate()
                    self._json(200, {"activated": True})
                    return
                self._desktop_request(super().do_POST)

            def do_GET(self):
                if self.path == "/api/desktop-instance":
                    super().do_GET()
                else:
                    self._desktop_request(super().do_GET)

            def _desktop_request(self, callback):
                with owner._actions:
                    if owner._closing:
                        self._error(503, "审查台正在退出，请重新启动后继续。")
                        return
                    owner._requests += 1
                try:
                    callback()
                finally:
                    with owner._actions:
                        owner._requests -= 1

        self.server.RequestHandlerClass = DesktopHandler

    def start(self) -> tuple[str, bool]:
        if self.port == 8519:
            raise ValueError("试用包不使用源开发服务的8519端口；请使用默认自动端口或另选端口。")
        try:
            self.paths.data.mkdir(parents=True, exist_ok=True)
            acquired = self.lock.acquire()
        except OSError as exc:
            raise RuntimeError(f"无法写入数据目录：{self.paths.data}。请把试用包解压到可写文件夹，或用 --data-dir 指定目录。") from exc
        if not acquired:
            for _ in range(50):
                url = existing_instance(self.paths)
                if url:
                    return url, False
                time.sleep(0.1)
            raise DesktopAlreadyRunning("这个数据目录已被占用，但未确认属于此试用包的运行实例。请查看已有审查台窗口；未启动第二份，也未终止任何进程。")
        try:
            # Check writability under the lock so another launch's temporary
            # check file cannot make a brand-new directory look nonempty.
            ensure_writable_directory(self.paths.data)
            initialize_data(self.paths)
            self.paths.runtime.mkdir(exist_ok=True)
            self.server = create_server(
                port=self.port, root=self.paths.data, resource_root=self.paths.resources,
                include_legacy_reports=False,
                source_paths={"detailed": None, "minimal": None, "table": None},
                review_runner=run_review, review_engines=capabilities(allow_codex=False)["engines"],
            )
            self.record = {"identity": instance_identity(self.paths), "token": secrets.token_hex(32),
                           "pid": os.getpid(), "port": self.server.server_port}
            self.server.desktop_instance = self.record
            self._install_desktop_handler()
            self.thread = threading.Thread(target=self.server.serve_forever, name="desktop-reader", daemon=True)
            self.thread.start()
            temporary = self.paths.data / (INSTANCE_FILE + ".tmp")
            temporary.write_text(json.dumps(self.record), encoding="utf-8")
            temporary.replace(self.paths.data / INSTANCE_FILE)
            return f"http://127.0.0.1:{self.server.server_port}/", True
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self.server is not None:
            if self.thread is not None and self.thread.is_alive():
                self.server.shutdown()
                self.thread.join(timeout=5)
            self.server.server_close()
            self.server = None
            self.thread = None
        if self.record is not None:
            marker = self.paths.data / INSTANCE_FILE
            try:
                if json.loads(marker.read_text(encoding="utf-8")) == self.record:
                    marker.unlink()
            except (OSError, ValueError):
                pass
            self.record = None
        self.lock.close()
