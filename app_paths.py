"""Read-only bundled resources and portable, explicitly separate desktop data."""
from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DesktopPaths:
    resources: Path
    data: Path

    @property
    def runtime(self) -> Path:
        return self.data / "runtime"


def desktop_paths(data_dir: str | Path | None = None) -> DesktopPaths:
    frozen = getattr(sys, "frozen", False)
    resources = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()
    base = Path(sys.executable).resolve().parent if frozen else Path(__file__).resolve().parent
    data = Path(data_dir).expanduser().resolve() if data_dir is not None else base / "我的数据"
    if data == resources or data.is_relative_to(resources / "runtime"):
        raise ValueError("数据目录不能是程序资源目录或源开发的runtime目录；请另选空文件夹。")
    return DesktopPaths(resources, data)


def ensure_writable_directory(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory) as stream:
            stream.write(b"desktop-write-check")
            stream.flush()
    except OSError as exc:
        raise RuntimeError(f"无法写入数据目录：{directory}。请把试用包解压到可写文件夹，或用 --data-dir 指定目录。") from exc
