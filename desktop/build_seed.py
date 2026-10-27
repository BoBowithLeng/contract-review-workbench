"""Build an offline, reproducible empty first-run seed for public desktop builds.

Usage: python desktop/build_seed.py --output-dir <empty staging directory>
Only a manifest is written; no existing runtime, contract or review is read.
"""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SEED_VERSION = 3
SEED_NOTICE = "首次启动工作台为空，请上传自己的合同并配置模型。"


def build_seed(output_dir: str | Path) -> dict:
    """Create an empty runtime seed without replacing existing destination data."""
    destination = Path(output_dir).resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Seed output directory must be empty; existing data is never replaced.")
    destination.mkdir(parents=True, exist_ok=True)
    runtime = destination / "runtime"
    runtime.mkdir()
    manifest = {"seed_version": SEED_VERSION, "notice": SEED_NOTICE, "samples": [], "files": {}}
    (runtime / "demo-seed.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def pack_runtime(runtime: Path, archive: Path) -> None:
    """Pack the seed with fixed member ordering and timestamps for reproducible builds."""
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as target:
        for path in sorted(p for p in Path(runtime).rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(runtime).as_posix(), (2026, 10, 7, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, path.read_bytes())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build an empty first-run desktop seed")
    parser.add_argument("--output-dir", required=True, help="Empty output directory; creates its runtime child")
    args = parser.parse_args()
    result = build_seed(args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
