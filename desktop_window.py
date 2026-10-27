"""WebView2 window with a source-checked bridge and native lifecycle.

Native events target pinned pywebview 6.2.1's WinForms implementation:
https://github.com/r0x0r/pywebview/blob/6.2.1/webview/platforms/edgechromium.py
Its public before_show event runs before navigation. Since the public API has
no cancellable navigation event, WebView2 native events enforce origin here.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

TITLE = "合同 · 审查台"
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
PAGE_PATHS = {"/", "/index.html", "/checklists.html"}


def show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)


def trusted_page(candidate: str, base_url: str) -> bool:
    try:
        value, base = urlsplit(candidate), urlsplit(base_url)
        return (value.scheme == "http" and value.hostname == "127.0.0.1"
                and value.port == base.port and not value.username and not value.password
                and value.path in PAGE_PATHS)
    except ValueError:
        return False


def external_url(candidate: str) -> bool:
    try:
        value = urlsplit(candidate)
        return (value.scheme in {"http", "https"} and bool(value.hostname)) or value.scheme == "mailto"
    except ValueError:
        return False


def local_download(candidate: str, base_url: str) -> bool:
    """Allow attachments and same-origin Blob downloads, not a bridged page."""
    if candidate.startswith("blob:"):
        candidate = candidate[5:]
        blob = True
    else:
        blob = False
    try:
        value, base = urlsplit(candidate), urlsplit(base_url)
        return (value.scheme == "http" and value.hostname == "127.0.0.1"
                and value.port == base.port and not value.username and not value.password
                and (blob or (value.path.startswith("/api/") and value.path.endswith((".docx", ".xlsx")))))
    except ValueError:
        return False


def require_webview2(webview) -> None:
    """Fail before creating a window, rather than using the IE fallback."""
    if os.name != "nt":
        raise RuntimeError("独立桌面窗口只支持 Windows 10/11。")
    import winreg
    key_path = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    found = False
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | view) as key:
                    version = winreg.QueryValueEx(key, "pv")[0]
                    try:
                        found = tuple(int(part) for part in version.split(".")) >= (86, 0, 622, 0)
                    except (ValueError, AttributeError):
                        found = False
                    if found:
                        break
            except OSError:
                continue
        if found:
            break
    if not found:
        raise RuntimeError(f"缺少 Microsoft Edge WebView2 Runtime，请安装 x64 Evergreen Runtime 后重新打开。下载：{WEBVIEW2_URL}")
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as key:
            release = winreg.QueryValueEx(key, "Release")[0]
        if release < 394802:
            raise ValueError("older .NET Framework")
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("桌面窗口需要 Microsoft .NET Framework 4.6.2 或更高版本，请安装后重新打开。") from exc
    try:
        import clr
        clr.AddReference("System.Windows.Forms")
    except Exception as exc:
        raise RuntimeError("桌面窗口需要 Microsoft .NET Framework 4.6.2 或更高版本，请安装后重新打开。") from exc
    from webview.guilib import initialize
    backend = initialize("edgechromium")
    if backend.renderer != "edgechromium":
        raise RuntimeError(f"WebView2 未能启用。请检查 WebView2 Runtime 与 .NET Framework 4.6.2+，本程序不使用旧 IE 内核。下载：{WEBVIEW2_URL}")


class DesktopBridge:
    """Only these three public methods are exposed to JavaScript."""
    def __init__(self, controller):
        self._controller = controller

    def get_desktop_info(self) -> dict:
        c = self._controller
        if not c.is_trusted():
            return {"available": False, "remove_allowed": False, "reason": "仅程序本地页面可调用桌面功能。"}
        info = {"available": True, "app_dir": str(c.app_dir), "data_dir": str(c.runtime.paths.data),
                "version": "源码运行", "remove_allowed": False}
        if not getattr(sys, "frozen", False):
            info["reason"] = "源码运行不提供移除软件。"
            return info
        try:
            # This is eligibility only; prepare_removal performs full validation
            # again on the explicit action, before any helper is launched.
            import json
            from desktop_removal import MANIFEST_NAME, APP_ID
            value = json.loads((c.app_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
            if (value.get("schema") != 1 or value.get("app_id") != APP_ID
                    or not isinstance(value.get("files"), dict) or not value["files"]):
                raise ValueError("程序拥有清单无效，无法移除。")
            info.update(remove_allowed=True, version=value.get("version", ""))
        except Exception as exc:
            info["reason"] = str(exc)
        return info

    def open_data_folder(self) -> dict:
        """Show this app's own data folder in Explorer; the page cannot choose a path."""
        c = self._controller
        if not c.is_trusted():
            return {"ok": False, "error": "仅程序本地页面可调用桌面功能。"}
        folder = c.runtime.paths.data
        if not folder.is_dir():
            return {"ok": False, "error": "数据文件夹不存在，可能已被移动或删除。"}
        os.startfile(folder)
        return {"ok": True}

    def request_remove(self, delete_data: bool = False) -> dict:
        c = self._controller
        if type(delete_data) is not bool:
            return {"ok": False, "error": "删除数据选项必须为布尔值。"}
        if not c.is_trusted() or not getattr(sys, "frozen", False):
            return {"ok": False, "error": "仅正式便携包的本地程序页面可移除软件。"}
        with c._lock:
            if c._removing:
                return {"ok": False, "error": "已安排移除，请等待程序退出。"}
            if c._downloads or not c.runtime.begin_close():
                return {"ok": False, "error": "还有审查、下载或删除正在进行，请等任务完成后再移除。"}
            try:
                from desktop_removal import prepare_removal, launch_removal
                plan = prepare_removal(c.app_dir, c.runtime.paths.data, delete_data)
                action = ("合同、密钥、个人清单和操作记录将送入系统回收站，可从回收站恢复。"
                          if delete_data else "合同、密钥、个人清单和操作记录保留在原数据目录。")
                if not c.window.create_confirmation_dialog(TITLE,
                        f"移除软件并退出？\n\n程序目录：{c.app_dir}\n数据目录：{c.runtime.paths.data}\n\n{action}"):
                    c.runtime.cancel_close()
                    return {"ok": False, "cancelled": True}
                result = launch_removal(plan, show_result=True)
                if not result.get("scheduled"):
                    raise RuntimeError("未能安排移除。")
                c._removing = True
                timer = threading.Timer(0.5, c.window.destroy)
                timer.daemon = True
                timer.start()
                return {"ok": True, "scheduled": True}
            except Exception as exc:
                c.runtime.cancel_close()
                return {"ok": False, "error": str(exc)}


class DesktopWindow:
    def __init__(self, runtime, url: str):
        self.runtime, self.url = runtime, url
        self.app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        self.window = None
        self._lock = threading.RLock()
        self._downloads = set()
        self._removing = False
        self._failure = None
        self._native_callbacks = []

    def is_trusted(self) -> bool:
        return self.window is not None and trusted_page(self.window.get_current_url() or "", self.url)

    def activate(self) -> None:
        self.window.restore()
        self.window.show()
        from System import Action
        form = self.window.native
        def raise_window():
            form.Activate()
            form.BringToFront()
        form.BeginInvoke(Action(raise_window))

    def closing(self):
        with self._lock:
            if self._removing:
                return True
            if self._downloads or not self.runtime.begin_close():
                show_error("还有审查、下载或删除正在进行。窗口会保持打开；等任务完成后再退出，避免中断未完成结果。")
                return False
            return True

    def attach_native(self):
        def fail(exc):
            self._failure = exc
            # before_show runs on the UI thread, before pywebview's shown
            # event. window.destroy waits for that event, so queue native
            # closing instead of blocking the first Show on a startup error.
            from System import Action
            self.window.native.BeginInvoke(Action(self.window.native.Close))
        try:
            browser = self.window.native.browser
            view = browser.webview

            def navigation(sender, args):
                uri = str(args.Uri)
                if not trusted_page(uri, self.url) and not local_download(uri, self.url):
                    args.Cancel = True
                    if external_url(uri):
                        webbrowser.open(uri)

            def frame_navigation(sender, args):
                args.Cancel = True  # The application has no embedded frames.

            def new_window(sender, args):
                args.Handled = True
                uri = str(args.Uri)
                if external_url(uri):
                    webbrowser.open(uri)

            def bridge_message(sender, args):
                if trusted_page(str(args.Source), self.url):
                    browser.on_script_notify(sender, args)

            def download_started(sender, args):
                if args.Cancel:
                    return
                operation = args.DownloadOperation
                with self._lock:
                    self._downloads.add(operation)
                def state_changed(sender, args):
                    if str(operation.State) in {"Completed", "Interrupted"}:
                        with self._lock:
                            self._downloads.discard(operation)
                        operation.StateChanged -= state_changed
                operation.StateChanged += state_changed
                self._native_callbacks.append(state_changed)
                state_changed(None, None)

            def initialized(sender, args):
                if not args.IsSuccess:
                    fail(RuntimeError(f"WebView2 窗口初始化失败：{args.InitializationException}"))
                    return
                core = view.CoreWebView2
                core.FrameNavigationStarting += frame_navigation
                core.NewWindowRequested -= browser.on_new_window_request
                core.NewWindowRequested += new_window
                core.DownloadStarting += download_started

            view.NavigationStarting += navigation
            view.WebMessageReceived -= browser.on_script_notify
            view.WebMessageReceived += bridge_message
            view.CoreWebView2InitializationCompleted += initialized
            self._native_callbacks.extend([navigation, frame_navigation, new_window,
                                           bridge_message, download_started, initialized])
        except Exception as exc:
            fail(exc)


def run_window(runtime, url: str) -> None:
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("桌面窗口缺少 pywebview 组件，请重新解压完整便携包。") from exc
    require_webview2(webview)
    # Keep Explorer, the title bar and the taskbar on the same application icon.
    if os.name == 'nt':
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('ContractReview.Workbench')
    icon = runtime.paths.resources / 'desktop' / 'app-icon.ico'
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["ALLOW_FILE_URLS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    controller = DesktopWindow(runtime, url)
    controller.window = webview.create_window(TITLE, url, js_api=DesktopBridge(controller),
                                              width=1440, height=960, min_size=(960, 640),
                                              maximized=True,
                                              background_color="#f3efe7")
    controller.window.events.before_show += controller.attach_native
    controller.window.events.closing += controller.closing
    controller.window.events.shown += lambda: runtime.set_activation_callback(controller.activate)
    webview.start(gui="edgechromium", private_mode=False,
                  icon=str(icon) if icon.is_file() else None,
                  storage_path=str(runtime.paths.data / "webview2"))
    if controller._failure is not None:
        raise controller._failure
