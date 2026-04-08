# -*- mode: python ; coding: utf-8 -*-

import os
import site
from pathlib import Path


project_root = Path(globals().get('SPECPATH', os.getcwd())).resolve()

datas = []
samples_dir = project_root / 'samples'
if samples_dir.exists():
    datas.append((str(samples_dir), 'samples'))

binaries = []
asi_dll_path = None


def _find_pyzwoasi_packaged_dll() -> Path | None:
    candidate_roots = []
    try:
        candidate_roots.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        candidate_roots.append(site.getusersitepackages())
    except Exception:
        pass

    for root in candidate_roots:
        candidate = Path(root) / 'pyzwoasi' / 'lib' / 'Windows' / 'x64' / 'ASICamera2.dll'
        if candidate.is_file():
            return candidate
    return None

# Optional override: set ASICAMERA2_DLL to a known-good SDK DLL.
# If not set, keep pyzwoasi's bundled DLL (usually best version match for wrappers).
env_dll = os.environ.get('ASICAMERA2_DLL')
if env_dll and Path(env_dll).is_file():
    asi_dll_path = Path(env_dll)
else:
    asi_dll_path = _find_pyzwoasi_packaged_dll()

if asi_dll_path is not None:
    # pyzwoasi currently hard-loads this exact in-package DLL path at import time.
    binaries.append((str(asi_dll_path), 'pyzwoasi/lib/Windows/x64'))
    print(f'Using ASICamera2 override DLL: {asi_dll_path}')
else:
    print('WARNING: Could not find pyzwoasi ASICamera2.dll; camera backend may fail in frozen build.')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DigitalFinder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DigitalFinder',
)
