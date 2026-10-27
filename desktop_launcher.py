"""Double-click entry point for the portable Windows review workbench."""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import webbrowser

from app_paths import desktop_paths
from desktop_runtime import DesktopRuntime, activate_existing_instance


def native_window(runtime: DesktopRuntime, url: str) -> None:
    from desktop_window import run_window
    run_window(runtime, url)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="合同审查台 · Windows便携试用包")
    parser.add_argument("--data-dir", help="数据目录；默认在程序旁的‘我的数据’")
    parser.add_argument("--port", type=int, default=0, help="本机端口，默认自动选择空闲端口")
    parser.add_argument("--headless", action="store_true", help="不显示应用窗口，按Ctrl+C退出")
    parser.add_argument("--no-browser", action="store_true", help="headless验收时不打开浏览器")
    args = parser.parse_args(argv)
    runtime = None
    try:
        paths = desktop_paths(args.data_dir)
        runtime = DesktopRuntime(paths, port=args.port)
        url, started = runtime.start()
        if sys.stdout is not None:
            print(f"Reader: {url}", flush=True)
        if not started:
            if not args.headless and not activate_existing_instance(paths):
                raise RuntimeError("已有审查台服务，但无法激活它的应用窗口，请查看原窗口。")
            return 0
        if args.headless:
            if not args.no_browser:
                webbrowser.open(url)
            stopped = threading.Event()
            def request_stop(*_):
                if runtime.begin_close():
                    stopped.set()
                elif sys.stderr is not None:
                    print("还有审查、下载或删除正在进行，请等任务完成后再退出。", file=sys.stderr)
            signal.signal(signal.SIGINT, request_stop)
            signal.signal(signal.SIGTERM, request_stop)
            if hasattr(signal, "SIGBREAK"):
                signal.signal(signal.SIGBREAK, request_stop)
            stopped.wait()
        else:
            native_window(runtime, url)
        return 0
    except Exception as exc:
        message = f"审查台没有启动：{exc}"
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        if not args.headless:
            from desktop_window import show_error
            show_error(message)
        return 1
    finally:
        if runtime is not None:
            runtime.stop()


if __name__ == "__main__":
    raise SystemExit(main())
