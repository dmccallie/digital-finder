# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path


project_root = Path(globals().get('SPECPATH', os.getcwd())).resolve()

datas = []
samples_dir = project_root / 'samples'
if samples_dir.exists():
    datas.append((str(samples_dir), 'samples'))

binaries = []

# Preferred override: set ASICAMERA2_DLL to a concrete DLL path before build.
env_dll = os.environ.get('ASICAMERA2_DLL')
if env_dll and Path(env_dll).is_file():
    binaries.append((env_dll, '.'))
else:
    candidates = [
        project_root / 'ASICamera2.dll',
        Path(os.environ.get('ProgramFiles', '')) / 'ASIStudio' / 'ASICamera2.dll',
        Path(os.environ.get('ProgramFiles', '')) / 'ZWO' / 'ASICamera2.dll',
        Path(os.environ.get('ProgramFiles', '')) / 'ZWO Design' / 'ASI Cameras' / 'ASICamera2.dll',
        Path(os.environ.get('ProgramFiles', '')) / 'ZWO Design' / 'ASI Cameras' / 'SDK' / 'lib' / 'x64' / 'ASICamera2.dll',
        Path(os.environ.get('ProgramFiles(x86)', '')) / 'ZWO Design' / 'ASI Cameras' / 'ASICamera2.dll',
        Path(os.environ.get('ProgramFiles(x86)', '')) / 'ZWO Design' / 'ASI Cameras' / 'SDK' / 'lib' / 'x64' / 'ASICamera2.dll',
    ]
    for candidate in candidates:
        if candidate.is_file():
            binaries.append((str(candidate), '.'))
            break

if not binaries:
    print('WARNING: ASICamera2.dll was not found during build. Set ASICAMERA2_DLL to include it in dist output.')


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
