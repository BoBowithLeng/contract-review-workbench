"""Finite, ownership-checked removal for an extracted Windows desktop package.

The helper is deliberately outside the package and waits on a verified process
handle. No HTTP endpoint, process killing, recursive root deletion or external
data-directory deletion is provided here.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import time
import uuid

APP_ID = "contract-review-desktop"
MANIFEST_NAME = ".desktop-program-manifest.json"
# These are application-created namespaces, not an arbitrary data-root walk.
MANAGED_DATA_DIRS = ("runtime/reader-state", "runtime/review-jobs", "runtime/demo-contracts")
MANAGED_DATA_FILES = ("runtime/demo-seed.json", ".desktop-initialized")
TRANSIENT_DATA_FILES = (".desktop.lock", ".desktop-instance.json")
WEBVIEW_DATA_DIR = "webview2"


class RemovalError(RuntimeError):
    pass


def _is_link(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _check_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for ancestor in (path, *path.parents):
        if ancestor.exists() and _is_link(ancestor):
            raise RemovalError(f"路径含符号链接或junction，保留原位：{ancestor}")
    return path


def _relative(value: str) -> str:
    if not isinstance(value, str) or "\\" in value or ":" in value:
        raise RemovalError("文件清单含无效相对路径。")
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or any(p in (".", "..", "") for p in value.split("/"))
            or any(p.endswith((" ", ".")) for p in path.parts)):
        raise RemovalError("文件清单不能指向包目录以外。")
    return path.as_posix()


def _fingerprint(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"size": path.stat().st_size, "sha256": digest.hexdigest()}


def _walk_files(root: Path):
    """Reject links before descending; never follow a junction with rglob."""
    for entry in root.iterdir():
        if _is_link(entry):
            raise RemovalError(f"目录含符号链接或junction，移除停止：{entry}")
        if entry.is_dir():
            yield from _walk_files(entry)
        elif entry.is_file():
            yield entry
        else:
            raise RemovalError(f"无法确认条目类型：{entry}")


def write_program_manifest(app_dir: str | Path, *, version: str,
                           recovery_archive: str | Path | None = None) -> Path:
    """Builder: call after every immutable asset is copied, before creating ZIP.

    The manifest excludes itself and 我的数据. All other regular files are
    recorded by relative POSIX path, size and SHA256; build must start clean.
    """
    root = _check_path(Path(app_dir))
    if (root / "我的数据").exists():
        raise RemovalError("程序清单不能在已产生我的数据的包上生成。")
    files = {p.relative_to(root).as_posix(): _fingerprint(p)
             for p in _walk_files(root) if p != root / MANIFEST_NAME}
    manifest = {"schema": 1, "app_id": APP_ID, "version": version,
                "recovery_archive": Path(recovery_archive).name if recovery_archive else None,
                "files": files}
    destination = root / MANIFEST_NAME
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


def prepare_removal(app_dir: str | Path, data_dir: str | Path,
                    delete_data: bool = False, *, recovery_archive=None) -> dict:
    """Return an exact immutable snapshot or raise; this does not delete anything.

    Only package-owned unchanged program files qualify. Unknown/modified files
    are returned as preserved. Custom external data can be kept, never deleted.
    """
    if not isinstance(delete_data, bool):
        raise RemovalError("同时移除数据必须是明确的勾选值。")
    root, data = _check_path(Path(app_dir)), _check_path(Path(data_dir))
    protected = {Path.home(), Path.home() / "Desktop", Path.home() / "桌面"}
    for name in ("USERPROFILE", "PUBLIC", "OneDrive"):
        if os.environ.get(name):
            protected.add(Path(os.environ[name]) / "Desktop")
    if os.name == "nt":
        desktop = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0, None, 0, desktop) == 0:
            protected.add(Path(desktop.value))
    if root == Path(root.anchor) or root in protected or (root / ".git").exists():
        raise RemovalError("不能移除盘根、桌面、个人目录或项目仓库。请从独立解压的试用包操作。")
    if data == root or data == Path(data.anchor) or root.is_relative_to(data):
        raise RemovalError("数据目录不能覆盖程序目录或盘根。")
    manifest_path = root / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RemovalError("没有有效的程序拥有文件清单；源开发目录和旧包不会被移除。") from exc
    if manifest.get("schema") != 1 or manifest.get("app_id") != APP_ID or not isinstance(manifest.get("files"), dict):
        raise RemovalError("程序拥有文件清单格式不匹配。")
    files, preserved = [], []
    seen = set()
    for name, expected in manifest["files"].items():
        name = _relative(name)
        if name == MANIFEST_NAME or name.split("/")[0] in ("我的数据", ".git") or name.casefold() in seen:
            raise RemovalError("程序清单包含数据、仓库、重复路径或清单自身。")
        seen.add(name.casefold())
        if (not isinstance(expected, dict) or not isinstance(expected.get("size"), int)
                or expected["size"] < 0 or not isinstance(expected.get("sha256"), str)
                or len(expected["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in expected["sha256"])):
            raise RemovalError("程序清单的文件校验值无效。")
        path = _check_path(root / name)
        if path.exists():
            if not path.is_file() or _fingerprint(path) != expected:
                preserved.append(name + "（已修改或类型变化）")
            else:
                files.append({"relative": name, **expected})
    if not manifest["files"]:
        raise RemovalError("空程序文件清单不能证明拥有这个目录。")
    # Enumerate program area for honest reporting, while data is separate.
    def collect(directory):
        for path in directory.iterdir():
            if path == data:
                continue
            if _is_link(path):
                preserved.append(path.relative_to(root).as_posix() + "（链接，保留）")
            elif path.is_dir():
                collect(path)
            elif path != manifest_path and path.relative_to(root).as_posix().casefold() not in seen:
                preserved.append(path.relative_to(root).as_posix() + "（非程序文件）")
    collect(root)
    data_files = []
    if delete_data:
        if data != root / "我的数据":
            raise RemovalError(f"自定义数据目录需要独立处理，不能随程序移除：{data}")
        if data.exists():
            if not (data / ".desktop-initialized").is_file():
                raise RemovalError("我的数据缺少本应用初始化标记，不能确认归属；数据保留原位。")
            owned = []
            for name in MANAGED_DATA_DIRS:
                directory = _check_path(data / name)
                if directory.exists():
                    owned.extend(_walk_files(directory))
            for name in MANAGED_DATA_FILES:
                path = _check_path(data / name)
                if path.is_file():
                    owned.append(path)
            profile = _check_path(data / WEBVIEW_DATA_DIR)
            if profile.is_dir():
                # WebView holds locks and continually changes its cache while
                # the native window is alive. Check links now, hash after exit.
                for _ in _walk_files(profile):
                    pass
            for path in owned:
                data_files.append({"relative": path.relative_to(data).as_posix(), **_fingerprint(path)})
            names = {row["relative"] for row in data_files}
            for path in _walk_files(data):
                relative = path.relative_to(data).as_posix()
                if (relative not in names and relative not in TRANSIENT_DATA_FILES
                        and not relative.startswith(WEBVIEW_DATA_DIR + "/")):
                    preserved.append("我的数据/" + path.relative_to(data).as_posix() + "（非受管数据）")
    # The manifest itself is owned by format/identity and separately snapshot-checked.
    files.append({"relative": MANIFEST_NAME, **_fingerprint(manifest_path)})
    return {"schema": 1, "app_id": APP_ID, "app_dir": str(root), "data_dir": str(data),
            "delete_data": bool(delete_data), "program_files": files, "data_files": data_files,
            "refresh_data_dirs": [WEBVIEW_DATA_DIR] if delete_data else [],
            "preserved_files": preserved, "preserved_data": None if delete_data else str(data),
            "recovery_note": "程序可重新解压同版本试用ZIP恢复。"}


def _current_process_creation() -> str:
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def launch_removal(plan: dict, *, show_result: bool = True) -> dict:
    """Start hidden helper, verify it holds the live process, then return to exit.

    The final result is written to report_path and shown by the helper after
    exit. A scheduled response is never a claim that removal has completed.
    """
    if os.name != "nt":
        raise RemovalError("实际移除helper只在Windows运行。")
    # Rebuild rather than trusting mutable bridge input as a deletion plan.
    current = prepare_removal(plan["app_dir"], plan["data_dir"], plan["delete_data"])
    # Revalidation preserves a program file modified after prepare. Data intent
    # covers application records as a whole; its final snapshot is taken now.
    # Comparing the old dict would spuriously reject normal WebView cache writes.
    plan = current
    resources = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    helper = resources / "desktop" / "remove_helper.ps1"
    if not helper.is_file():
        raise RemovalError("缺少Windows移除helper，请重新解压完整试用包。")
    run = Path(tempfile.mkdtemp(prefix="contract-review-removal-"))
    copied = run / "remove_helper.ps1"
    # Windows PowerShell 5.1 reads -File scripts as ANSI without a UTF-8 BOM.
    copied.write_text(helper.read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
    report = run / "移除结果.json"
    ready = run / "ready"
    request = {**plan, "pid": os.getpid(), "creation_filetime": _current_process_creation(),
               "report_path": str(report), "ready_path": str(ready), "show_result": bool(show_result),
               "recovery_dir": str(Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) /
                                    "合同审查台" / "移除恢复" / uuid.uuid4().hex)}
    payload = run / "request.json"
    payload.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                      "-File", str(copied), "-RequestPath", str(payload)],
                     cwd=run, creationflags=subprocess.CREATE_NO_WINDOW,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if ready.is_file():
            return {"scheduled": True, "report_path": str(report), "preserved_data": plan["preserved_data"],
                    "data_recycle_requested": plan["delete_data"], "preserved_files": plan["preserved_files"]}
        if report.is_file():
            raise RemovalError(f"移除helper未能准备：{report.read_text(encoding='utf-8-sig')}")
        time.sleep(.05)
    # No ready acknowledgement means the window must stay open; helper times out
    # without taking any action when the current process does not exit.
    raise RemovalError(f"移除helper未就绪，程序保持运行；结果文件：{report}")
