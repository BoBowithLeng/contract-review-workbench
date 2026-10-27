"""Build a distributable onedir ZIP using explicit resources, never house runtime."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = '合同审查台'


def copy_licenses(target: Path) -> None:
    target.mkdir()
    rows = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata['Name']
        if name.lower() == 'pip':
            continue
        rows.append(f'{name} {distribution.version}')
        for member in distribution.files or []:
            filename = member.name.lower()
            if not (filename.startswith(('license', 'licence', 'copying', 'notice')) or 'licenses' in member.parts):
                continue
            source = Path(distribution.locate_file(member))
            if source.is_file():
                destination = target / name / Path(*member.parts)
                if not destination.resolve().is_relative_to(target.resolve()):
                    raise RuntimeError('License path escapes distribution directory')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if not python_license.is_file():
        raise RuntimeError('Python license file not found')
    shutil.copyfile(python_license, target / 'Python-LICENSE.txt')
    for name in ('tcl8.6', 'tk8.6'):
        license_file = Path(sys.base_prefix) / 'tcl' / name / 'license.terms'
        if license_file.is_file():
            shutil.copyfile(license_file, target / (name + '-license.terms'))
    # Tcl 8.6.12 in the bundled Python lacks its license file; retain the
    # unmodified upstream core-8-6-12/license.terms with the distribution.
    shutil.copyfile(ROOT / 'desktop/third_party/Tcl-8.6.12-license.terms', target / 'Tcl-8.6.12-license.terms')
    shutil.copyfile(ROOT / 'desktop/third_party/WebView2-SDK-1.0.3856.49-LICENSE.txt', target / 'WebView2-SDK-1.0.3856.49-LICENSE.txt')
    (target / '组件版本.txt').write_text('\n'.join(sorted(rows)) + '\n', encoding='utf-8')


def build(output: Path, version: str) -> Path:
    if os.name != 'nt' or sys.maxsize < 2**32:
        raise RuntimeError('Build on Windows x64 with Python x64')
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError('Output directory must be empty; do not overwrite a used trial bundle')
    output.mkdir(parents=True, exist_ok=True)
    build_root = ROOT / 'runtime' / 'desktop-build'
    build_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='build-', dir=build_root))
    sys.path.insert(0, str(ROOT))
    from desktop.build_seed import build_seed, pack_runtime
    seed_tree = work / 'seed-tree'
    build_seed(seed_tree)
    seed = work / 'desktop_seed'
    seed.mkdir()
    pack_runtime(seed_tree / 'runtime', seed / 'runtime.zip')
    version_parts = tuple(int(part) for part in version.split('.'))
    if len(version_parts) != 3:
        raise ValueError('Version must be three dot-separated integers')
    version_file = work / 'version.txt'
    version_file.write_text(f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={version_parts + (0,)}, prodvers={version_parts + (0,)}, mask=0x3f, flags=0, OS=0x40004, fileType=0x1, subtype=0, date=(0,0)),
  kids=[StringFileInfo([StringTable('080404b0', [StringStruct('FileDescription','合同审查台 Windows 试用版'), StringStruct('FileVersion',{version!r}), StringStruct('ProductName','合同审查台'), StringStruct('ProductVersion',{version!r})])]), VarFileInfo([VarStruct('Translation',[2052,1200])])])
''', encoding='utf-8')
    spec = work / 'desktop.spec'
    # Some Python distributions patch ssl.py to read this sibling resource.
    # Bundle it when present so frozen SSLContext creation keeps the same trust policy.
    ssl_runtime_data = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
    ssl_datas = [(str(ssl_runtime_data), '.')] if ssl_runtime_data.is_file() else []
    spec.write_text(f'''from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs
root = {str(ROOT)!r}
datas = [({str(ROOT / 'reader_ui')!r}, 'reader_ui'), ({str(ROOT / 'prompts')!r}, 'prompts'), ({str(ROOT / 'builtin_checklists.json')!r}, '.'), ({str(seed)!r}, 'desktop_seed')]
datas += {ssl_datas!r}
datas += collect_data_files('docx')
datas += collect_data_files('webview')
datas += [({str(ROOT / 'desktop' / 'remove_helper.ps1')!r}, 'desktop')]
datas += [({str(ROOT / 'desktop' / 'app-icon.ico')!r}, 'desktop')]
binaries = collect_dynamic_libs('webview')
a = Analysis([{str(ROOT / 'desktop_launcher.py')!r}], pathex=[root], binaries=binaries, datas=datas, hiddenimports=['webview.platforms.winforms', 'webview.platforms.edgechromium', 'clr_loader'], hookspath=[], excludes=['streamlit', 'source_codex_config', 'pytest', 'IPython', 'notebook'], noarchive=False, optimize=1)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name={APP_NAME!r}, debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False, disable_windowed_traceback=True, version={str(version_file)!r}, icon={str(ROOT / 'desktop' / 'app-icon.ico')!r})
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name={APP_NAME!r})
''', encoding='utf-8')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(output),
                    '--workpath', str(work / 'pyinstaller'), str(spec)], cwd=ROOT, check=True)
    app = output / APP_NAME
    shutil.copyfile(ROOT / 'desktop' / '使用说明.txt', app / '先读我.txt')
    shutil.copyfile(ROOT / 'LICENSE', app / 'LICENSE.txt')
    copy_licenses(app / '第三方许可')
    from desktop_removal import write_program_manifest
    write_program_manifest(app, version=version)
    # The archive must be assembled before anyone starts the app. It contains
    # only immutable program resources and an empty first-run seed, never "我的数据".
    if (app / '我的数据').exists():
        raise RuntimeError('Do not distribute a bundle containing runtime user data')
    forbidden = {'source_codex_config.py', 'model-settings.json', '.env', 'auth.json', 'config.toml'}
    for path in app.rglob('*'):
        if path.is_symlink() or path.name in forbidden:
            raise RuntimeError(f'Unexpected distribution entry: {path.name}')
    archive_path = output / f'{APP_NAME}-Windows-x64-试用版-{version}.zip'
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(app.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(output))
    manifest = {'version': version, 'archive': archive_path.name, 'sha256': hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                'files': sum(1 for p in app.rglob('*') if p.is_file()), 'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'source_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
                'python': sys.version.split()[0], 'pyinstaller': importlib.metadata.version('pyinstaller')}
    (output / 'build-info.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False))
    return archive_path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Create a clean Windows trial ZIP with an empty first-run seed')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--version', default='0.1.3')
    args = parser.parse_args()
    build(args.output_dir, args.version)
